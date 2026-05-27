"""Historical backfill engine for learning-protocol snapshots and outcomes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path

import pandas as pd

from stock_analyzer.config import StockAnalyzerConfig
from stock_analyzer.data.provider import MarketDataProvider
from stock_analyzer.feature.engineer import FeatureEngineer
from stock_analyzer.feature.market_context import build_market_relative_frame
from stock_analyzer.learning.dataset_manifest import DatasetManifestBuilder
from stock_analyzer.learning.feedback_features import (
    ensure_feedback_feature_frame,
    merge_feedback_feature_vector,
)
from stock_analyzer.learning.feature_schema_registry import (
    FeatureSchemaRecord,
    FeatureSchemaRegistry,
)
from stock_analyzer.learning.label_policy_registry import LabelPolicyRecord, LabelPolicyRegistry
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    FeatureCaptureMode,
    MaturityStatus,
    OutcomeRecord,
    SignalSnapshot,
)
from stock_analyzer.learning.sample_store import SampleStore

_BUSINESS_CLOSE_UTC = time(15, 0, tzinfo=UTC)
_LabelPolicyResolver = Callable[[SignalSnapshot], LabelPolicyRecord]


@dataclass(slots=True)
class _OutcomeMetrics:
    label_mature_time: datetime
    realized_return: float
    max_favorable_excursion: float
    max_adverse_excursion: float


@dataclass(slots=True)
class _TrainableManifestCandidate:
    feature_schema: FeatureSchemaRecord
    label_policy: LabelPolicyRecord
    snapshot_ids: list[str]
    row_count: int
    feature_count: int
    schema_created_at: datetime
    label_policy_created_at: datetime
    latest_decision_time: datetime
    time_window_start: datetime
    time_window_end: datetime


class LearningBackfillEngine:
    """Build and repair historical samples without mutating observed snapshots."""

    def __init__(
        self,
        *,
        config: StockAnalyzerConfig,
        provider: MarketDataProvider,
        sample_store: SampleStore,
        feature_schema_registry: FeatureSchemaRegistry,
        label_policy_registry: LabelPolicyRegistry,
        feature_engineer: FeatureEngineer | None = None,
    ) -> None:
        self._config = config
        self._provider = provider
        self._sample_store = sample_store
        self._feature_schema_registry = feature_schema_registry
        self._label_policy_registry = label_policy_registry
        self._feature_engineer = feature_engineer or FeatureEngineer()
        self._runtime_config_hash = _stable_config_hash(config)
        self._code_version = str(config.evolution.code_commit_id).strip() or "unknown"

    def bootstrap_backfill(
        self,
        *,
        symbols: Sequence[str],
        strategy: str = "trend",
        lookback_days: int = 240,
        end_date: date | None = None,
        min_history_rows: int | None = None,
        source: str = "bootstrap_backfill",
    ) -> dict[str, object]:
        normalized_symbols = _normalize_symbols(symbols)
        normalized_strategy = strategy.strip().lower() or "trend"
        normalized_source = source.strip().lower() or "bootstrap_backfill"
        effective_end_date = end_date or datetime.now(UTC).date()
        if not normalized_symbols:
            return {
                "ok": False,
                "mode": "bootstrap_backfill",
                "symbols": [],
                "errors": ["no_symbols"],
            }

        label_policy = self._label_policy_registry.register_from_config(self._config.labels)
        min_rows = max(5, min_history_rows or max(20, label_policy.horizon_days + 5))
        inserted_snapshots = 0
        skipped_existing = 0
        updated_outcomes = 0
        candidate_rows = 0
        errors: list[str] = []
        feature_schema_ids: set[str] = set()
        feature_schema_hashes: set[str] = set()
        fidelity_breakdown: dict[str, int] = {}
        decision_times: list[datetime] = []

        for symbol in normalized_symbols:
            try:
                bars = self._fetch_daily_bars(
                    symbol=symbol,
                    lookback_days=lookback_days,
                    end_date=effective_end_date,
                )
                if bars.empty:
                    errors.append(f"{symbol}:bars_empty")
                    continue
                market_index = (
                    build_market_relative_frame(
                        self._provider,
                        bars=bars,
                        config=self._config.market_relative_feature,
                    )
                    if bool(self._config.market_relative_feature.enabled)
                    else None
                )
                features = self._feature_engineer.transform(
                    bars,
                    market_index=market_index,
                )
                if features.empty:
                    errors.append(f"{symbol}:features_empty")
                    continue
                features = ensure_feedback_feature_frame(features)

                feature_schema = self._feature_schema_registry.register_from_frame(
                    features,
                    feature_engineer_name=type(self._feature_engineer).__name__,
                    feature_engineer_version="transform_t1_v1",
                    code_version=self._code_version,
                    fillna_policy="fill_zero_after_shift",
                    normalization_hint="t1_shifted",
                )
                feature_schema_ids.add(feature_schema.feature_schema_id)
                feature_schema_hashes.add(feature_schema.feature_schema_hash)

                for row_position in range(min_rows, len(features)):
                    metrics = _compute_outcome_metrics_for_row(
                        bars=bars,
                        row_position=row_position,
                        label_policy=label_policy,
                    )
                    if metrics is None:
                        continue

                    candidate_rows += 1
                    decision_time = _decision_time_from_index(features.index[row_position])
                    decision_times.append(decision_time)
                    snapshot_id = _build_backfill_snapshot_id(
                        symbol=symbol,
                        strategy=normalized_strategy,
                        decision_time=decision_time,
                        feature_schema_id=feature_schema.feature_schema_id,
                        label_policy_id=label_policy.label_policy_id,
                        source=normalized_source,
                    )
                    stored_snapshot = self._sample_store.get_snapshot(snapshot_id)
                    if stored_snapshot is None:
                        stored_snapshot = SignalSnapshot(
                            snapshot_id=snapshot_id,
                            code_version=self._code_version,
                            symbol=symbol,
                            strategy=normalized_strategy,
                            decision_time=decision_time,
                            feature_vector=merge_feedback_feature_vector(
                                _feature_vector_from_row(features.iloc[row_position]),
                                risk_context={
                                    "backfill_source": normalized_source,
                                    "historical_recompute": True,
                                },
                                news_context={
                                    "backfill_source": normalized_source,
                                    "historical_recompute": True,
                                },
                                regime_context={"backfill_source": normalized_source},
                                add_missing_columns=True,
                            ),
                            feature_schema_id=feature_schema.feature_schema_id,
                            feature_schema_hash=feature_schema.feature_schema_hash,
                            feature_capture_mode=FeatureCaptureMode.REPLAYED_RECOMPUTE,
                            model_outputs={},
                            score_breakdown={},
                            risk_context={
                                "backfill_source": normalized_source,
                                "historical_recompute": True,
                            },
                            news_context={
                                "backfill_source": normalized_source,
                                "historical_recompute": True,
                            },
                            regime_context={"backfill_source": normalized_source},
                            watchlist_source=normalized_source,
                            data_quality_score=_estimate_data_quality_score(
                                features.iloc[row_position]
                            ),
                            sample_weight=1.0,
                            runtime_config_hash=self._runtime_config_hash,
                            label_policy_id=label_policy.label_policy_id,
                            label_policy_hash=label_policy.label_policy_hash,
                        )
                        self._sample_store.write_snapshot(stored_snapshot)
                        inserted_snapshots += 1
                    else:
                        skipped_existing += 1

                    rebuilt = self._rebuild_outcome_for_snapshot(
                        snapshot=stored_snapshot,
                        label_policy=label_policy,
                        bars=bars,
                        row_position=row_position,
                        as_of=datetime.combine(effective_end_date, _BUSINESS_CLOSE_UTC),
                        source=normalized_source,
                        preferred_fidelity=BackfillFidelityTier.BRONZE,
                    )
                    if bool(rebuilt["updated"]):
                        updated_outcomes += 1
                        tier = str(rebuilt.get("fidelity_tier", "")).strip().lower()
                        if tier:
                            fidelity_breakdown[tier] = fidelity_breakdown.get(tier, 0) + 1
            except Exception as exc:
                errors.append(f"{symbol}:{exc}")

        return {
            "ok": candidate_rows > 0,
            "mode": "bootstrap_backfill",
            "symbols": normalized_symbols,
            "strategy": normalized_strategy,
            "lookback_days": max(1, int(lookback_days)),
            "end_date": effective_end_date.isoformat(),
            "candidate_rows": candidate_rows,
            "snapshots_inserted": inserted_snapshots,
            "snapshots_skipped_existing": skipped_existing,
            "outcomes_updated": updated_outcomes,
            "feature_schema_ids": sorted(feature_schema_ids),
            "feature_schema_hashes": sorted(feature_schema_hashes),
            "label_policy_id": label_policy.label_policy_id,
            "label_policy_hash": label_policy.label_policy_hash,
            "fidelity_breakdown": fidelity_breakdown,
            "time_window_start": min(decision_times).isoformat() if decision_times else "",
            "time_window_end": max(decision_times).isoformat() if decision_times else "",
            "errors": errors,
        }

    def incremental_backfill(
        self,
        *,
        symbols: Sequence[str] | None = None,
        as_of: datetime | None = None,
        source: str = "incremental_backfill",
    ) -> dict[str, object]:
        effective_as_of = _ensure_utc(as_of or datetime.now(UTC))
        normalized_source = source.strip().lower() or "incremental_backfill"
        normalized_symbols = set(_normalize_symbols(symbols or []))
        snapshots = self._sample_store.list_snapshots()
        if normalized_symbols:
            snapshots = [
                snapshot
                for snapshot in snapshots
                if _normalize_symbol(snapshot.symbol) in normalized_symbols
            ]
        if not snapshots:
            return {
                "ok": True,
                "mode": "incremental_backfill",
                "symbols": sorted(normalized_symbols),
                "as_of": effective_as_of.isoformat(),
                "pending_candidates": 0,
                "outcomes_updated": 0,
                "promoted_label_matured": 0,
                "still_pending": 0,
                "errors": [],
            }

        outcome_map = {
            outcome.snapshot_id: outcome
            for outcome in self._sample_store.list_outcomes(
                snapshot_ids=[snapshot.snapshot_id for snapshot in snapshots]
            )
        }
        pending_snapshots = [
            snapshot
            for snapshot in snapshots
            if outcome_map.get(snapshot.snapshot_id) is not None
            and outcome_map[snapshot.snapshot_id].maturity_status == MaturityStatus.PENDING
        ]
        grouped = _group_snapshots_by_symbol(pending_snapshots)
        updated = 0
        promoted_label_matured = 0
        still_pending = 0
        errors: list[str] = []

        for symbol, symbol_snapshots in grouped.items():
            try:
                lookback_days = _required_lookback_days(
                    snapshots=symbol_snapshots,
                    as_of=effective_as_of,
                    label_policy_resolver=self._label_policy_for_snapshot,
                )
                bars = self._fetch_daily_bars(
                    symbol=symbol,
                    lookback_days=lookback_days,
                    end_date=effective_as_of.date(),
                )
                for snapshot in symbol_snapshots:
                    outcome = outcome_map.get(snapshot.snapshot_id)
                    if outcome is None:
                        continue
                    repaired = self._repair_snapshot_with_bars(
                        snapshot=snapshot,
                        outcome=outcome,
                        bars=bars,
                        as_of=effective_as_of,
                        source=normalized_source,
                    )
                    if bool(repaired["updated"]):
                        updated += 1
                    if bool(repaired["promoted_label_matured"]):
                        promoted_label_matured += 1
                    if not bool(repaired["matured"]):
                        still_pending += 1
            except Exception as exc:
                errors.append(f"{symbol}:{exc}")
                still_pending += len(symbol_snapshots)

        return {
            "ok": not errors,
            "mode": "incremental_backfill",
            "symbols": sorted(grouped.keys()),
            "as_of": effective_as_of.isoformat(),
            "pending_candidates": len(pending_snapshots),
            "outcomes_updated": updated,
            "promoted_label_matured": promoted_label_matured,
            "still_pending": still_pending,
            "errors": errors,
        }

    def repair_backfill(
        self,
        *,
        snapshot_ids: Sequence[str],
        as_of: datetime | None = None,
        source: str = "repair_backfill",
    ) -> dict[str, object]:
        normalized_snapshot_ids = [
            str(item).strip() for item in snapshot_ids if str(item).strip()
        ]
        if not normalized_snapshot_ids:
            return {
                "ok": False,
                "mode": "repair_backfill",
                "snapshot_ids": [],
                "errors": ["no_snapshot_ids"],
            }

        effective_as_of = _ensure_utc(as_of or datetime.now(UTC))
        normalized_source = source.strip().lower() or "repair_backfill"
        snapshots = self._sample_store.list_snapshots(snapshot_ids=normalized_snapshot_ids)
        if not snapshots:
            return {
                "ok": False,
                "mode": "repair_backfill",
                "snapshot_ids": normalized_snapshot_ids,
                "as_of": effective_as_of.isoformat(),
                "snapshots_considered": 0,
                "missing_snapshot_ids": normalized_snapshot_ids,
                "outcomes_updated": 0,
                "promoted_label_matured": 0,
                "promoted_fully_matured": 0,
                "errors": ["snapshots_not_found"],
            }

        found_snapshot_ids = {snapshot.snapshot_id for snapshot in snapshots}
        missing_snapshot_ids = sorted(
            snapshot_id
            for snapshot_id in normalized_snapshot_ids
            if snapshot_id not in found_snapshot_ids
        )
        outcome_map = {
            outcome.snapshot_id: outcome
            for outcome in self._sample_store.list_outcomes(snapshot_ids=normalized_snapshot_ids)
        }
        grouped = _group_snapshots_by_symbol(snapshots)
        updated = 0
        promoted_label_matured = 0
        promoted_fully_matured = 0
        errors: list[str] = []

        for symbol, symbol_snapshots in grouped.items():
            try:
                lookback_days = _required_lookback_days(
                    snapshots=symbol_snapshots,
                    as_of=effective_as_of,
                    label_policy_resolver=self._label_policy_for_snapshot,
                )
                bars = self._fetch_daily_bars(
                    symbol=symbol,
                    lookback_days=lookback_days,
                    end_date=effective_as_of.date(),
                )
                for snapshot in symbol_snapshots:
                    repaired = self._repair_snapshot_with_bars(
                        snapshot=snapshot,
                        outcome=outcome_map.get(snapshot.snapshot_id),
                        bars=bars,
                        as_of=effective_as_of,
                        source=normalized_source,
                    )
                    if bool(repaired["updated"]):
                        updated += 1
                    if bool(repaired["promoted_label_matured"]):
                        promoted_label_matured += 1
                    if bool(repaired["promoted_fully_matured"]):
                        promoted_fully_matured += 1
            except Exception as exc:
                errors.append(f"{symbol}:{exc}")

        return {
            "ok": not errors and not missing_snapshot_ids,
            "mode": "repair_backfill",
            "snapshot_ids": normalized_snapshot_ids,
            "as_of": effective_as_of.isoformat(),
            "snapshots_considered": len(snapshots),
            "missing_snapshot_ids": missing_snapshot_ids,
            "outcomes_updated": updated,
            "promoted_label_matured": promoted_label_matured,
            "promoted_fully_matured": promoted_fully_matured,
            "errors": errors,
        }

    def build_trainable_manifest(
        self,
        *,
        symbols: Sequence[str] | None = None,
        feature_schema_id: str = "",
        feature_schema_hash: str = "",
        label_policy_id: str = "",
        label_policy_hash: str = "",
        time_window_start: datetime | None = None,
        time_window_end: datetime | None = None,
        fidelity_filter: Sequence[BackfillFidelityTier] | None = None,
        maturity_statuses: Sequence[MaturityStatus] | None = None,
        calibration_ratio: float | None = None,
        test_ratio: float | None = None,
        sample_selection_rule: str = "",
    ) -> dict[str, object]:
        normalized_symbols = _normalize_symbols(symbols or [])
        effective_time_window_start = _ensure_optional_utc(time_window_start)
        effective_time_window_end = _ensure_optional_utc(time_window_end)
        effective_fidelity = _normalize_trainable_manifest_fidelity_filter(fidelity_filter)
        effective_maturity = _normalize_trainable_manifest_maturity_statuses(maturity_statuses)
        effective_calibration_ratio = float(
            self._config.training.calibration_ratio
            if calibration_ratio is None
            else calibration_ratio
        )
        effective_test_ratio = float(
            self._config.training.test_ratio if test_ratio is None else test_ratio
        )

        try:
            requested_feature_schema = self._resolve_requested_feature_schema_record(
                feature_schema_id=feature_schema_id,
                feature_schema_hash=feature_schema_hash,
            )
            requested_label_policy = self._resolve_requested_label_policy_record(
                label_policy_id=label_policy_id,
                label_policy_hash=label_policy_hash,
            )
        except ValueError as exc:
            return {
                "ok": False,
                "mode": "build_trainable_manifest",
                "selection_mode": "invalid_request",
                "symbols": normalized_symbols,
                "dataset_manifest_id": "",
                "included_snapshot_count": 0,
                "feature_schema_id": "",
                "feature_schema_hash": "",
                "label_policy_id": "",
                "label_policy_hash": "",
                "split_counts": {},
                "fidelity_breakdown": {},
                "dropped_reason_breakdown": {},
                "errors": [str(exc)],
            }

        snapshots = self._sample_store.list_snapshots(
            time_window_start=effective_time_window_start,
            time_window_end=effective_time_window_end,
        )
        if normalized_symbols:
            normalized_symbol_set = set(normalized_symbols)
            snapshots = [
                snapshot
                for snapshot in snapshots
                if _normalize_symbol(snapshot.symbol) in normalized_symbol_set
            ]
        if not snapshots:
            return {
                "ok": False,
                "mode": "build_trainable_manifest",
                "selection_mode": _resolve_manifest_selection_mode(
                    requested_feature_schema=requested_feature_schema,
                    requested_label_policy=requested_label_policy,
                ),
                "symbols": normalized_symbols,
                "dataset_manifest_id": "",
                "included_snapshot_count": 0,
                "feature_schema_id": "",
                "feature_schema_hash": "",
                "label_policy_id": "",
                "label_policy_hash": "",
                "split_counts": {},
                "fidelity_breakdown": {},
                "dropped_reason_breakdown": {},
                "errors": ["no_snapshots"],
            }

        outcome_map = {
            outcome.snapshot_id: outcome
            for outcome in self._sample_store.list_outcomes(
                snapshot_ids=[snapshot.snapshot_id for snapshot in snapshots]
            )
        }
        candidate = self._select_trainable_manifest_candidate(
            snapshots=snapshots,
            outcome_map=outcome_map,
            fidelity_filter=effective_fidelity,
            maturity_statuses=effective_maturity,
            requested_feature_schema=requested_feature_schema,
            requested_label_policy=requested_label_policy,
        )
        selection_mode = _resolve_manifest_selection_mode(
            requested_feature_schema=requested_feature_schema,
            requested_label_policy=requested_label_policy,
        )
        if candidate is None:
            return {
                "ok": False,
                "mode": "build_trainable_manifest",
                "selection_mode": selection_mode,
                "symbols": normalized_symbols,
                "dataset_manifest_id": "",
                "included_snapshot_count": 0,
                "feature_schema_id": (
                    requested_feature_schema.feature_schema_id
                    if requested_feature_schema is not None
                    else ""
                ),
                "feature_schema_hash": (
                    requested_feature_schema.feature_schema_hash
                    if requested_feature_schema is not None
                    else ""
                ),
                "label_policy_id": (
                    requested_label_policy.label_policy_id
                    if requested_label_policy is not None
                    else ""
                ),
                "label_policy_hash": (
                    requested_label_policy.label_policy_hash
                    if requested_label_policy is not None
                    else ""
                ),
                "split_counts": {},
                "fidelity_breakdown": {},
                "dropped_reason_breakdown": {},
                "errors": ["no_trainable_candidates"],
            }

        manifest_time_window_start = effective_time_window_start or candidate.time_window_start
        manifest_time_window_end = effective_time_window_end or candidate.time_window_end
        selection_rule = sample_selection_rule.strip() or _build_trainable_manifest_selection_rule(
            selection_mode=selection_mode,
            symbols=normalized_symbols,
            fidelity_filter=effective_fidelity,
            maturity_statuses=effective_maturity,
            feature_schema_id=candidate.feature_schema.feature_schema_id,
            label_policy_id=candidate.label_policy.label_policy_id,
            time_window_start=manifest_time_window_start,
            time_window_end=manifest_time_window_end,
        )
        manifest = DatasetManifestBuilder(
            store=self._sample_store,
            feature_schema_registry=self._feature_schema_registry,
        ).create_manifest(
            feature_schema_id=candidate.feature_schema.feature_schema_id,
            feature_schema_hash=candidate.feature_schema.feature_schema_hash,
            label_policy_id=candidate.label_policy.label_policy_id,
            label_policy_hash=candidate.label_policy.label_policy_hash,
            snapshot_ids=candidate.snapshot_ids,
            sample_selection_rule=selection_rule,
            time_window_start=manifest_time_window_start,
            time_window_end=manifest_time_window_end,
            fidelity_filter=effective_fidelity,
            maturity_statuses=effective_maturity,
            calibration_ratio=effective_calibration_ratio,
            test_ratio=effective_test_ratio,
        )
        split_counts = {
            item.split_name: int(item.row_count) for item in manifest.split_plan
        }
        return {
            "ok": True,
            "mode": "build_trainable_manifest",
            "selection_mode": selection_mode,
            "symbols": normalized_symbols,
            "dataset_manifest_id": manifest.dataset_manifest_id,
            "included_snapshot_count": manifest.included_snapshot_count,
            "included_outcome_count": manifest.included_outcome_count,
            "candidate_rows": candidate.row_count,
            "feature_schema_id": manifest.feature_schema_id,
            "feature_schema_hash": manifest.feature_schema_hash,
            "label_policy_id": manifest.label_policy_id,
            "label_policy_hash": manifest.label_policy_hash,
            "split_counts": split_counts,
            "fidelity_breakdown": dict(manifest.fidelity_breakdown),
            "dropped_reason_breakdown": dict(manifest.dropped_reason_breakdown),
            "time_window_start": (
                manifest.time_window_start.isoformat()
                if manifest.time_window_start is not None
                else ""
            ),
            "time_window_end": (
                manifest.time_window_end.isoformat()
                if manifest.time_window_end is not None
                else ""
            ),
            "errors": [],
        }

    def backfill_from_runtime_history_archive(
        self,
        *,
        archive_path: str | Path = "",
        archive_payload: Mapping[str, object] | None = None,
        source: str = "runtime_history_archive",
    ) -> dict[str, object]:
        normalized_source = source.strip().lower() or "runtime_history_archive"
        normalized_archive_path = Path(str(archive_path)).expanduser() if str(archive_path).strip() else None
        if archive_payload is None and normalized_archive_path is None:
            return {
                "ok": False,
                "mode": "runtime_history_archive_backfill",
                "archive_path": "",
                "archive_day": "",
                "contexts_enriched": 0,
                "execution_updates": 0,
                "command_events_linked": 0,
                "reconcile_updates": 0,
                "reconcile_promoted": 0,
                "symbols": [],
                "snapshot_ids": [],
                "missing_snapshot_ids": [],
                "errors": ["no_archive_source"],
            }

        payload: Mapping[str, object]
        if archive_payload is not None:
            payload = archive_payload
        else:
            try:
                raw = json.loads(normalized_archive_path.read_text(encoding="utf-8"))
            except OSError as exc:
                return {
                    "ok": False,
                    "mode": "runtime_history_archive_backfill",
                    "archive_path": str(normalized_archive_path),
                    "archive_day": "",
                    "contexts_enriched": 0,
                    "execution_updates": 0,
                    "command_events_linked": 0,
                    "reconcile_updates": 0,
                    "reconcile_promoted": 0,
                    "symbols": [],
                    "snapshot_ids": [],
                    "missing_snapshot_ids": [],
                    "errors": [f"archive_read_failed:{exc}"],
                }
            except json.JSONDecodeError as exc:
                return {
                    "ok": False,
                    "mode": "runtime_history_archive_backfill",
                    "archive_path": str(normalized_archive_path),
                    "archive_day": "",
                    "contexts_enriched": 0,
                    "execution_updates": 0,
                    "command_events_linked": 0,
                    "reconcile_updates": 0,
                    "reconcile_promoted": 0,
                    "symbols": [],
                    "snapshot_ids": [],
                    "missing_snapshot_ids": [],
                    "errors": [f"archive_json_invalid:{exc}"],
                }
            if not isinstance(raw, dict):
                return {
                    "ok": False,
                    "mode": "runtime_history_archive_backfill",
                    "archive_path": str(normalized_archive_path),
                    "archive_day": "",
                    "contexts_enriched": 0,
                    "execution_updates": 0,
                    "command_events_linked": 0,
                    "reconcile_updates": 0,
                    "reconcile_promoted": 0,
                    "symbols": [],
                    "snapshot_ids": [],
                    "missing_snapshot_ids": [],
                    "errors": ["archive_payload_not_mapping"],
                }
            payload = raw

        archive_day = str(payload.get("day", "")).strip()
        generated_at = _parse_optional_datetime(payload.get("generated_at")) or datetime.now(UTC)
        recommendation_snapshot_ids: dict[str, str] = {}
        snapshot_ids_by_symbol: dict[str, str] = {}
        latest_signal_by_symbol: dict[str, dict[str, object]] = {}
        touched_symbols: set[str] = set()
        touched_snapshot_ids: set[str] = set()
        missing_snapshot_ids: set[str] = set()
        contexts_enriched = 0
        execution_updates = 0
        command_events_linked = 0
        portfolio_trade_events_linked = 0
        reconcile_updates = 0
        reconcile_promoted = 0
        errors: list[str] = []

        raw_latest_signals = payload.get("latest_signals")
        latest_signals = _coerce_mapping(raw_latest_signals)
        raw_signal_rows = latest_signals.get("signals")
        if isinstance(raw_signal_rows, list):
            for item in raw_signal_rows:
                signal_row = _coerce_mapping(item)
                if not signal_row:
                    continue
                symbol = _normalize_symbol(signal_row.get("symbol"))
                recommendation_id = str(signal_row.get("recommendation_id", "")).strip().upper()
                snapshot_id = _extract_archive_snapshot_id(signal_row)
                if symbol and snapshot_id:
                    snapshot_ids_by_symbol[symbol] = snapshot_id
                    latest_signal_by_symbol[symbol] = signal_row
                if recommendation_id and snapshot_id:
                    recommendation_snapshot_ids[recommendation_id] = snapshot_id

                raw_decision_trace = signal_row.get("decision_trace")
                decision_trace = _coerce_mapping(raw_decision_trace)
                runtime_feedback = _coerce_mapping(decision_trace.get("runtime_feedback"))
                risk_context = _coerce_mapping(runtime_feedback.get("m1"))
                regime_context = _coerce_mapping(runtime_feedback.get("m3"))
                news_context = _coerce_mapping(runtime_feedback.get("m7"))
                if not snapshot_id or (
                    not risk_context and not regime_context and not news_context
                ):
                    continue
                enriched = self._enrich_snapshot_contexts_from_runtime_archive(
                    snapshot_id=snapshot_id,
                    risk_context=risk_context,
                    news_context=news_context,
                    regime_context=regime_context,
                )
                if enriched["missing_snapshot_id"]:
                    missing_snapshot_ids.add(str(enriched["missing_snapshot_id"]))
                if not enriched["updated"]:
                    continue
                contexts_enriched += 1
                if symbol:
                    touched_symbols.add(symbol)
                touched_snapshot_ids.add(snapshot_id)

        raw_runtime = payload.get("runtime")
        runtime_section = _coerce_mapping(raw_runtime)
        raw_audit_events = runtime_section.get("audit_events")
        if isinstance(raw_audit_events, list):
            for item in raw_audit_events:
                event = _coerce_mapping(item)
                if not event:
                    continue
                try:
                    result = self._apply_runtime_archive_command_event(
                        event=event,
                        recommendation_snapshot_ids=recommendation_snapshot_ids,
                        default_timestamp=generated_at,
                        source=normalized_source,
                    )
                except Exception as exc:
                    errors.append(f"command_event:{exc}")
                    continue
                execution_updates += result["updated"]
                command_events_linked += result["linked"]
                if result["missing_snapshot_id"]:
                    missing_snapshot_ids.add(str(result["missing_snapshot_id"]))
                for symbol in result["symbols"]:
                    touched_symbols.add(symbol)
                for snapshot_id in result["snapshot_ids"]:
                    touched_snapshot_ids.add(snapshot_id)

        try:
            result = self._apply_runtime_archive_portfolio_trades(
                archive_payload=payload,
                snapshot_ids_by_symbol=snapshot_ids_by_symbol,
                latest_signal_by_symbol=latest_signal_by_symbol,
                default_timestamp=generated_at,
                source=normalized_source,
            )
        except Exception as exc:
            errors.append(f"portfolio_trades:{exc}")
        else:
            execution_updates += result["updated"]
            portfolio_trade_events_linked += result["linked"]
            for symbol in result["symbols"]:
                touched_symbols.add(symbol)
            for snapshot_id in result["snapshot_ids"]:
                touched_snapshot_ids.add(snapshot_id)
            for snapshot_id in result["missing_snapshot_ids"]:
                missing_snapshot_ids.add(str(snapshot_id))

        reconcile_report = self._select_runtime_archive_reconcile_report(payload=payload)
        if reconcile_report:
            try:
                result = self._apply_runtime_archive_reconcile_report(
                    archive_payload=payload,
                    report=reconcile_report,
                    snapshot_ids_by_symbol=snapshot_ids_by_symbol,
                    default_timestamp=generated_at,
                    source=normalized_source,
                )
            except Exception as exc:
                errors.append(f"reconcile_report:{exc}")
            else:
                reconcile_updates += result["updated"]
                reconcile_promoted += result["promoted"]
                for symbol in result["symbols"]:
                    touched_symbols.add(symbol)
                for snapshot_id in result["snapshot_ids"]:
                    touched_snapshot_ids.add(snapshot_id)
                if result["missing_snapshot_id"]:
                    missing_snapshot_ids.add(str(result["missing_snapshot_id"]))

        return {
            "ok": not errors,
            "mode": "runtime_history_archive_backfill",
            "archive_path": str(normalized_archive_path) if normalized_archive_path is not None else "",
            "archive_day": archive_day,
            "generated_at": generated_at.isoformat(),
            "contexts_enriched": contexts_enriched,
            "execution_updates": execution_updates,
            "command_events_linked": command_events_linked,
            "portfolio_trade_events_linked": portfolio_trade_events_linked,
            "reconcile_updates": reconcile_updates,
            "reconcile_promoted": reconcile_promoted,
            "symbols": sorted(touched_symbols),
            "snapshot_ids": sorted(touched_snapshot_ids),
            "missing_snapshot_ids": sorted(missing_snapshot_ids),
            "errors": errors,
        }

    def backfill_from_runtime_history_archives(
        self,
        *,
        archive_dir: str | Path = "",
        archive_paths: Sequence[str | Path] | None = None,
        source: str = "runtime_history_archive_batch",
    ) -> dict[str, object]:
        normalized_source = source.strip().lower() or "runtime_history_archive_batch"
        resolved_paths = _resolve_runtime_history_archive_paths(
            archive_dir=archive_dir,
            archive_paths=archive_paths,
        )
        if not resolved_paths:
            return {
                "ok": False,
                "mode": "runtime_history_archive_batch_backfill",
                "archive_dir": str(Path(str(archive_dir)).expanduser())
                if str(archive_dir).strip()
                else "",
                "processed_archives": 0,
                "processed_archive_paths": [],
                "processed_archive_days": [],
                "contexts_enriched": 0,
                "execution_updates": 0,
                "command_events_linked": 0,
                "portfolio_trade_events_linked": 0,
                "reconcile_updates": 0,
                "reconcile_promoted": 0,
                "symbols": [],
                "snapshot_ids": [],
                "missing_snapshot_ids": [],
                "errors": ["no_archives_found"],
            }

        contexts_enriched = 0
        execution_updates = 0
        command_events_linked = 0
        portfolio_trade_events_linked = 0
        reconcile_updates = 0
        reconcile_promoted = 0
        touched_symbols: set[str] = set()
        touched_snapshot_ids: set[str] = set()
        missing_snapshot_ids: set[str] = set()
        processed_archive_paths: list[str] = []
        processed_archive_days: list[str] = []
        errors: list[str] = []

        for path in resolved_paths:
            result = self.backfill_from_runtime_history_archive(
                archive_path=path,
                source=normalized_source,
            )
            processed_archive_paths.append(str(path))
            archive_day = str(result.get("archive_day", "")).strip()
            if archive_day:
                processed_archive_days.append(archive_day)
            contexts_enriched += int(result.get("contexts_enriched", 0))
            execution_updates += int(result.get("execution_updates", 0))
            command_events_linked += int(result.get("command_events_linked", 0))
            portfolio_trade_events_linked += int(
                result.get("portfolio_trade_events_linked", 0)
            )
            reconcile_updates += int(result.get("reconcile_updates", 0))
            reconcile_promoted += int(result.get("reconcile_promoted", 0))
            for item in result.get("symbols", []):
                symbol = _normalize_symbol(item)
                if symbol:
                    touched_symbols.add(symbol)
            for item in result.get("snapshot_ids", []):
                snapshot_id = str(item).strip()
                if snapshot_id:
                    touched_snapshot_ids.add(snapshot_id)
            for item in result.get("missing_snapshot_ids", []):
                snapshot_id = str(item).strip()
                if snapshot_id:
                    missing_snapshot_ids.add(snapshot_id)
            for item in result.get("errors", []):
                text = str(item).strip()
                if text:
                    errors.append(f"{path.name}:{text}")

        return {
            "ok": not errors,
            "mode": "runtime_history_archive_batch_backfill",
            "archive_dir": str(Path(str(archive_dir)).expanduser())
            if str(archive_dir).strip()
            else "",
            "processed_archives": len(processed_archive_paths),
            "processed_archive_paths": processed_archive_paths,
            "processed_archive_days": processed_archive_days,
            "contexts_enriched": contexts_enriched,
            "execution_updates": execution_updates,
            "command_events_linked": command_events_linked,
            "portfolio_trade_events_linked": portfolio_trade_events_linked,
            "reconcile_updates": reconcile_updates,
            "reconcile_promoted": reconcile_promoted,
            "symbols": sorted(touched_symbols),
            "snapshot_ids": sorted(touched_snapshot_ids),
            "missing_snapshot_ids": sorted(missing_snapshot_ids),
            "errors": errors,
        }

    def _enrich_snapshot_contexts_from_runtime_archive(
        self,
        *,
        snapshot_id: str,
        risk_context: Mapping[str, object],
        news_context: Mapping[str, object],
        regime_context: Mapping[str, object],
    ) -> dict[str, object]:
        snapshot = self._sample_store.get_snapshot(snapshot_id)
        if snapshot is None:
            return {"updated": False, "missing_snapshot_id": snapshot_id}
        merged_risk_context = _merge_mapping(snapshot.risk_context, risk_context)
        merged_news_context = _merge_mapping(snapshot.news_context, news_context)
        merged_regime_context = _merge_mapping(snapshot.regime_context, regime_context)
        if (
            merged_risk_context == snapshot.risk_context
            and merged_news_context == snapshot.news_context
            and merged_regime_context == snapshot.regime_context
        ):
            return {"updated": False, "missing_snapshot_id": ""}
        self._sample_store.enrich_snapshot_contexts(
            snapshot_id=snapshot_id,
            risk_context=merged_risk_context,
            news_context=merged_news_context,
            regime_context=merged_regime_context,
        )
        return {"updated": True, "missing_snapshot_id": ""}

    def _apply_runtime_archive_command_event(
        self,
        *,
        event: Mapping[str, object],
        recommendation_snapshot_ids: Mapping[str, str],
        default_timestamp: datetime,
        source: str,
    ) -> dict[str, object]:
        if str(event.get("event_type", "")).strip().lower() != "command_accepted":
            return {
                "updated": 0,
                "linked": 0,
                "symbols": [],
                "snapshot_ids": [],
                "missing_snapshot_id": "",
            }
        payload = _coerce_mapping(event.get("payload"))
        if str(payload.get("action", "")).strip().upper() != "SET_POSITION":
            return {
                "updated": 0,
                "linked": 0,
                "symbols": [],
                "snapshot_ids": [],
                "missing_snapshot_id": "",
            }
        command_update = _coerce_mapping(payload.get("command_update"))
        status = str(command_update.get("status", "")).strip().lower()
        if status not in {"opened", "adjusted"}:
            return {
                "updated": 0,
                "linked": 0,
                "symbols": [],
                "snapshot_ids": [],
                "missing_snapshot_id": "",
            }

        recommendation_reference = _coerce_mapping(command_update.get("recommendation_reference"))
        snapshot_id = _extract_archive_snapshot_id(recommendation_reference)
        if not snapshot_id:
            recommendation_id = str(
                recommendation_reference.get("recommendation_id", "")
            ).strip().upper()
            snapshot_id = str(recommendation_snapshot_ids.get(recommendation_id, "")).strip()
        if not snapshot_id:
            return {
                "updated": 0,
                "linked": 0,
                "symbols": [],
                "snapshot_ids": [],
                "missing_snapshot_id": "",
            }

        event_time = _parse_optional_datetime(event.get("timestamp")) or default_timestamp
        manual_fill = _coerce_mapping(command_update.get("manual_fill"))
        update_payload: dict[str, object] = {"execution_fill_ratio": 1.0}
        slippage_bp = _calculate_archive_execution_slippage_bp(
            side="buy",
            execution_price=_as_float(manual_fill.get("entry_price"), default=0.0),
            reference_price=_as_float(recommendation_reference.get("reference_price"), default=0.0),
        )
        if slippage_bp is not None:
            update_payload["realized_slippage_bp"] = slippage_bp
        updated, missing_snapshot_id = self._update_outcome_from_runtime_archive(
            snapshot_id=snapshot_id,
            timestamp=event_time,
            updates=update_payload,
            source=source,
        )
        symbol = _normalize_symbol(command_update.get("symbol"))
        return {
            "updated": 1 if updated else 0,
            "linked": 1,
            "symbols": [symbol] if updated and symbol else [],
            "snapshot_ids": [snapshot_id] if updated else [],
            "missing_snapshot_id": missing_snapshot_id,
        }

    def _apply_runtime_archive_portfolio_trades(
        self,
        *,
        archive_payload: Mapping[str, object],
        snapshot_ids_by_symbol: Mapping[str, str],
        latest_signal_by_symbol: Mapping[str, Mapping[str, object]],
        default_timestamp: datetime,
        source: str,
    ) -> dict[str, object]:
        raw_portfolio = _coerce_mapping(archive_payload.get("portfolio"))
        raw_trades = raw_portfolio.get("trades")
        if not isinstance(raw_trades, list):
            return {
                "updated": 0,
                "linked": 0,
                "symbols": [],
                "snapshot_ids": [],
                "missing_snapshot_ids": [],
            }

        updated = 0
        linked = 0
        touched_symbols: list[str] = []
        touched_snapshot_ids: list[str] = []
        missing_snapshot_ids: list[str] = []
        seen_snapshot_ids: set[str] = set()

        for item in raw_trades:
            trade = _coerce_mapping(item)
            side = str(trade.get("side", "")).strip().lower()
            if side != "buy":
                continue
            symbol = _normalize_symbol(trade.get("symbol"))
            if not symbol:
                continue
            snapshot_id = _runtime_archive_snapshot_id_for_trade(
                trade=trade,
                snapshot_ids_by_symbol=snapshot_ids_by_symbol,
                latest_signal_by_symbol=latest_signal_by_symbol,
            )
            if not snapshot_id or snapshot_id in seen_snapshot_ids:
                continue
            seen_snapshot_ids.add(snapshot_id)

            reference_price = _runtime_archive_reference_price_for_trade(
                trade=trade,
                latest_signal=latest_signal_by_symbol.get(symbol, {}),
            )
            execution_price = _as_float(trade.get("entry_price"), default=0.0)
            update_payload: dict[str, object] = {"execution_fill_ratio": 1.0}
            slippage_bp = _calculate_archive_execution_slippage_bp(
                side="buy",
                execution_price=execution_price,
                reference_price=reference_price,
            )
            if slippage_bp is not None:
                update_payload["realized_slippage_bp"] = slippage_bp

            event_time = _parse_optional_datetime(trade.get("timestamp")) or default_timestamp
            changed, missing_snapshot_id = self._update_outcome_from_runtime_archive(
                snapshot_id=snapshot_id,
                timestamp=event_time,
                updates=update_payload,
                source=source,
            )
            linked += 1
            if missing_snapshot_id:
                missing_snapshot_ids.append(missing_snapshot_id)
                continue
            if not changed:
                continue
            updated += 1
            touched_symbols.append(symbol)
            touched_snapshot_ids.append(snapshot_id)

        return {
            "updated": updated,
            "linked": linked,
            "symbols": sorted(set(touched_symbols)),
            "snapshot_ids": sorted(set(touched_snapshot_ids)),
            "missing_snapshot_ids": sorted(set(missing_snapshot_ids)),
        }

    def _select_runtime_archive_reconcile_report(
        self,
        *,
        payload: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        reconcile_section = _coerce_mapping(payload.get("reconcile"))
        latest = _coerce_mapping(reconcile_section.get("latest"))
        if latest:
            return latest
        raw_history = reconcile_section.get("history")
        if not isinstance(raw_history, list) or not raw_history:
            return None
        for item in reversed(raw_history):
            report = _coerce_mapping(item)
            if report:
                return report
        return None

    def _apply_runtime_archive_reconcile_report(
        self,
        *,
        archive_payload: Mapping[str, object],
        report: Mapping[str, object],
        snapshot_ids_by_symbol: Mapping[str, str],
        default_timestamp: datetime,
        source: str,
    ) -> dict[str, object]:
        status = str(report.get("status", "")).strip().lower()
        if not status:
            return {
                "updated": 0,
                "promoted": 0,
                "symbols": [],
                "snapshot_ids": [],
                "missing_snapshot_id": "",
            }

        strategy_positions = _runtime_archive_strategy_position_map(archive_payload)
        broker_positions = _runtime_archive_broker_position_map(archive_payload)

        diff_by_symbol: dict[str, float] = {}
        raw_diffs = report.get("diffs")
        if isinstance(raw_diffs, list):
            for item in raw_diffs:
                diff_item = _coerce_mapping(item)
                symbol = _normalize_symbol(diff_item.get("symbol"))
                if not symbol:
                    continue
                diff_by_symbol[symbol] = abs(_as_float(diff_item.get("diff"), default=0.0))

        missing_in_broker = {
            symbol
            for item in (
                report.get("missing_in_broker")
                if isinstance(report.get("missing_in_broker"), list)
                else []
            )
            if (symbol := _normalize_symbol(item))
        }
        missing_in_strategy = {
            symbol
            for item in (
                report.get("missing_in_strategy")
                if isinstance(report.get("missing_in_strategy"), list)
                else []
            )
            if (symbol := _normalize_symbol(item))
        }

        relevant_symbols = set(diff_by_symbol) | missing_in_broker | missing_in_strategy
        if status in {"ok", "mismatch"}:
            relevant_symbols.update(snapshot_ids_by_symbol.keys())

        updated = 0
        promoted = 0
        touched_symbols: list[str] = []
        touched_snapshot_ids: list[str] = []
        missing_snapshot_id = ""
        event_time = _parse_optional_datetime(report.get("timestamp")) or default_timestamp
        for symbol in sorted(relevant_symbols):
            snapshot_id = str(snapshot_ids_by_symbol.get(symbol, "")).strip()
            if not snapshot_id:
                continue
            current_outcome = self._sample_store.get_outcome(snapshot_id)
            next_status: MaturityStatus | None = None
            if (
                current_outcome is not None
                and current_outcome.maturity_status == MaturityStatus.LABEL_MATURED
                and status in {"ok", "mismatch"}
            ):
                next_status = MaturityStatus.RECONCILED

            diff_value: float | None = None
            if symbol in diff_by_symbol:
                diff_value = diff_by_symbol[symbol]
            elif symbol in missing_in_broker:
                diff_value = abs(strategy_positions.get(symbol, 0.0))
            elif symbol in missing_in_strategy:
                diff_value = abs(broker_positions.get(symbol, 0.0))
            elif status == "ok":
                diff_value = 0.0
            elif status == "mismatch":
                diff_value = abs(
                    strategy_positions.get(symbol, 0.0) - broker_positions.get(symbol, 0.0)
                )

            update_payload: dict[str, object] = {"reconcile_status": status}
            if diff_value is not None:
                update_payload["sim_vs_broker_diff"] = round(diff_value, 6)
            changed, current_missing_snapshot_id = self._update_outcome_from_runtime_archive(
                snapshot_id=snapshot_id,
                timestamp=event_time,
                updates=update_payload,
                maturity_status=next_status,
                source=source,
            )
            if current_missing_snapshot_id and not missing_snapshot_id:
                missing_snapshot_id = current_missing_snapshot_id
            if not changed:
                continue
            updated += 1
            if next_status == MaturityStatus.RECONCILED:
                promoted += 1
            touched_symbols.append(symbol)
            touched_snapshot_ids.append(snapshot_id)

        return {
            "updated": updated,
            "promoted": promoted,
            "symbols": sorted(set(touched_symbols)),
            "snapshot_ids": sorted(set(touched_snapshot_ids)),
            "missing_snapshot_id": missing_snapshot_id,
        }

    def _update_outcome_from_runtime_archive(
        self,
        *,
        snapshot_id: str,
        timestamp: datetime,
        updates: Mapping[str, object],
        source: str,
        maturity_status: MaturityStatus | None = None,
    ) -> tuple[bool, str]:
        snapshot = self._sample_store.get_snapshot(snapshot_id)
        if snapshot is None:
            return False, snapshot_id
        current = self._sample_store.get_outcome(snapshot_id)
        preferred_fidelity = _prefer_backfill_fidelity_tier(
            current=current.backfill_fidelity_tier if current is not None else None,
            candidate=_default_fidelity_tier_for_snapshot(snapshot),
        )
        candidate_source = str(current.backfill_source).strip() if current is not None else ""
        if not candidate_source or (
            current is not None
            and _backfill_fidelity_rank(preferred_fidelity)
            > _backfill_fidelity_rank(current.backfill_fidelity_tier)
        ):
            candidate_source = source
        candidate = (current or OutcomeRecord(snapshot_id=snapshot_id)).model_copy(
            update={
                **{str(key): value for key, value in updates.items()},
                "maturity_status": maturity_status
                if maturity_status is not None
                else (current.maturity_status if current is not None else MaturityStatus.PENDING),
                "outcome_updated_at": timestamp,
                "last_backfill_at": timestamp,
                "backfill_fidelity_tier": preferred_fidelity,
                "backfill_source": candidate_source,
            },
            deep=True,
        )
        if current is not None and candidate.model_dump(mode="json") == current.model_dump(
            mode="json"
        ):
            return False, ""
        self._sample_store.upsert_outcome(candidate)
        return True, ""

    def _repair_snapshot_with_bars(
        self,
        *,
        snapshot: SignalSnapshot,
        outcome: OutcomeRecord | None,
        bars: pd.DataFrame,
        as_of: datetime,
        source: str,
    ) -> dict[str, object]:
        label_policy = self._label_policy_for_snapshot(snapshot)
        row_position = _locate_snapshot_row_position(snapshot=snapshot, bars=bars)
        if row_position is None:
            return {
                "updated": False,
                "matured": False,
                "promoted_label_matured": False,
                "promoted_fully_matured": False,
            }
        return self._rebuild_outcome_for_snapshot(
            snapshot=snapshot,
            label_policy=label_policy,
            bars=bars,
            row_position=row_position,
            as_of=as_of,
            source=source,
            preferred_fidelity=_default_fidelity_tier_for_snapshot(snapshot),
            existing_outcome=outcome,
        )

    def _rebuild_outcome_for_snapshot(
        self,
        *,
        snapshot: SignalSnapshot,
        label_policy: LabelPolicyRecord,
        bars: pd.DataFrame,
        row_position: int,
        as_of: datetime,
        source: str,
        preferred_fidelity: BackfillFidelityTier,
        existing_outcome: OutcomeRecord | None = None,
    ) -> dict[str, object]:
        metrics = _compute_outcome_metrics_for_row(
            bars=bars,
            row_position=row_position,
            label_policy=label_policy,
        )
        if metrics is None:
            return {
                "updated": False,
                "matured": False,
                "promoted_label_matured": False,
                "promoted_fully_matured": False,
            }

        current = existing_outcome or self._sample_store.get_outcome(snapshot.snapshot_id)
        current_status = current.maturity_status if current is not None else MaturityStatus.PENDING
        next_status = _merge_label_maturity_status(
            current_status=current_status,
            current_outcome=current,
        )
        candidate = (current or OutcomeRecord(snapshot_id=snapshot.snapshot_id)).model_copy(
            update={
                "maturity_status": next_status,
                "label_mature_time": metrics.label_mature_time,
                "realized_return": metrics.realized_return,
                "max_favorable_excursion": metrics.max_favorable_excursion,
                "max_adverse_excursion": metrics.max_adverse_excursion,
                "outcome_updated_at": as_of,
                "last_backfill_at": as_of,
                "backfill_fidelity_tier": (
                    current.backfill_fidelity_tier if current is not None else None
                )
                or preferred_fidelity,
                "backfill_source": (
                    str(current.backfill_source).strip() if current is not None else ""
                )
                or source,
                "recomputed_feature_schema_id": (
                    str(current.recomputed_feature_schema_id).strip()
                    if current is not None
                    else ""
                )
                or (
                    snapshot.feature_schema_id
                    if snapshot.feature_capture_mode != FeatureCaptureMode.OBSERVED_SNAPSHOT
                    else ""
                ),
            },
            deep=True,
        )
        if current is not None and candidate.model_dump(mode="json") == current.model_dump(
            mode="json"
        ):
            return {
                "updated": False,
                "matured": True,
                "promoted_label_matured": False,
                "promoted_fully_matured": False,
                "fidelity_tier": (
                    candidate.backfill_fidelity_tier.value
                    if candidate.backfill_fidelity_tier is not None
                    else ""
                ),
            }

        self._sample_store.upsert_outcome(candidate)
        return {
            "updated": True,
            "matured": True,
            "promoted_label_matured": current_status == MaturityStatus.PENDING,
            "promoted_fully_matured": (
                current_status != MaturityStatus.FULLY_MATURED
                and candidate.maturity_status == MaturityStatus.FULLY_MATURED
            ),
            "fidelity_tier": (
                candidate.backfill_fidelity_tier.value
                if candidate.backfill_fidelity_tier is not None
                else ""
            ),
        }

    def _label_policy_for_snapshot(self, snapshot: SignalSnapshot) -> LabelPolicyRecord:
        record = self._label_policy_registry.get_by_id(snapshot.label_policy_id)
        if record is None:
            raise ValueError(f"missing_label_policy:{snapshot.label_policy_id}")
        return record

    def _resolve_requested_feature_schema_record(
        self,
        *,
        feature_schema_id: str,
        feature_schema_hash: str,
    ) -> FeatureSchemaRecord | None:
        normalized_id = str(feature_schema_id).strip()
        normalized_hash = str(feature_schema_hash).strip()
        if not normalized_id and not normalized_hash:
            return None
        if normalized_id:
            record = self._feature_schema_registry.get_by_id(normalized_id)
            if record is None:
                raise ValueError(f"unknown_feature_schema_id:{normalized_id}")
            if normalized_hash and record.feature_schema_hash != normalized_hash:
                raise ValueError(f"feature_schema_hash_mismatch:{normalized_id}")
            return record
        record = self._feature_schema_registry.get_by_hash(normalized_hash)
        if record is None:
            raise ValueError(f"unknown_feature_schema_hash:{normalized_hash}")
        return record

    def _resolve_requested_label_policy_record(
        self,
        *,
        label_policy_id: str,
        label_policy_hash: str,
    ) -> LabelPolicyRecord | None:
        normalized_id = str(label_policy_id).strip()
        normalized_hash = str(label_policy_hash).strip()
        if not normalized_id and not normalized_hash:
            return None
        if normalized_id:
            record = self._label_policy_registry.get_by_id(normalized_id)
            if record is None:
                raise ValueError(f"unknown_label_policy_id:{normalized_id}")
            if normalized_hash and record.label_policy_hash != normalized_hash:
                raise ValueError(f"label_policy_hash_mismatch:{normalized_id}")
            return record
        record = self._label_policy_registry.get_by_hash(normalized_hash)
        if record is None:
            raise ValueError(f"unknown_label_policy_hash:{normalized_hash}")
        return record

    def _select_trainable_manifest_candidate(
        self,
        *,
        snapshots: Sequence[SignalSnapshot],
        outcome_map: dict[str, OutcomeRecord],
        fidelity_filter: Sequence[BackfillFidelityTier],
        maturity_statuses: Sequence[MaturityStatus],
        requested_feature_schema: FeatureSchemaRecord | None,
        requested_label_policy: LabelPolicyRecord | None,
    ) -> _TrainableManifestCandidate | None:
        feature_records = (
            [requested_feature_schema]
            if requested_feature_schema is not None
            else self._feature_schema_registry.list_records()
        )
        label_records = (
            [requested_label_policy]
            if requested_label_policy is not None
            else self._label_policy_registry.list_records()
        )
        allowed_fidelity = set(fidelity_filter)
        allowed_maturity = set(maturity_statuses)
        best: _TrainableManifestCandidate | None = None

        for feature_record in feature_records:
            if feature_record is None:
                continue
            compatible_records = self._feature_schema_registry.resolve_projection_compatible_records(
                feature_record.feature_schema_id
            )
            allowed_feature_schemas = {
                record.feature_schema_id: record.feature_schema_hash
                for record in compatible_records
            }
            if not allowed_feature_schemas:
                allowed_feature_schemas = {
                    feature_record.feature_schema_id: feature_record.feature_schema_hash
                }
            for label_record in label_records:
                if label_record is None:
                    continue
                candidate_snapshots = [
                    snapshot
                    for snapshot in snapshots
                    if _snapshot_matches_trainable_contract(
                        snapshot=snapshot,
                        outcome=outcome_map.get(snapshot.snapshot_id),
                        allowed_feature_schemas=allowed_feature_schemas,
                        label_policy=label_record,
                        allowed_fidelity=allowed_fidelity,
                        allowed_maturity=allowed_maturity,
                    )
                ]
                if not candidate_snapshots:
                    continue
                ordered_snapshots = sorted(
                    candidate_snapshots,
                    key=lambda item: (item.decision_time, item.snapshot_id),
                )
                candidate = _TrainableManifestCandidate(
                    feature_schema=feature_record,
                    label_policy=label_record,
                    snapshot_ids=[item.snapshot_id for item in ordered_snapshots],
                    row_count=len(ordered_snapshots),
                    feature_count=len(feature_record.feature_names),
                    schema_created_at=feature_record.created_at,
                    label_policy_created_at=label_record.created_at,
                    latest_decision_time=max(item.decision_time for item in ordered_snapshots),
                    time_window_start=min(item.decision_time for item in ordered_snapshots),
                    time_window_end=max(item.decision_time for item in ordered_snapshots),
                )
                if _is_better_trainable_manifest_candidate(
                    candidate=candidate,
                    current_best=best,
                ):
                    best = candidate
        return best

    def _fetch_daily_bars(
        self,
        *,
        symbol: str,
        lookback_days: int,
        end_date: date | None,
    ) -> pd.DataFrame:
        frame = self._provider.fetch_daily_bars(
            symbol=symbol,
            lookback_days=max(1, int(lookback_days)),
            end_date=end_date,
        )
        if not isinstance(frame, pd.DataFrame):
            raise ValueError("provider did not return a dataframe")
        ordered = frame.sort_index().copy()
        if end_date is None or ordered.empty:
            return ordered
        ordered_index = pd.DatetimeIndex(ordered.index)
        return ordered.loc[ordered_index.date <= end_date]


def _compute_outcome_metrics_for_row(
    *,
    bars: pd.DataFrame,
    row_position: int,
    label_policy: LabelPolicyRecord,
) -> _OutcomeMetrics | None:
    ordered = bars.sort_index()
    if row_position < 0 or row_position >= len(ordered):
        return None

    entry_position, entry_price = _resolve_entry_position(
        bars=ordered,
        row_position=row_position,
        label_policy=label_policy,
    )
    if entry_position is None or entry_price <= 0:
        return None

    last_position = entry_position + label_policy.horizon_days - 1
    if last_position >= len(ordered):
        return None

    high_window = ordered["high"].iloc[entry_position : last_position + 1].astype(float)
    low_window = ordered["low"].iloc[entry_position : last_position + 1].astype(float)
    close_window = ordered["close"].iloc[entry_position : last_position + 1].astype(float)
    realized_return = float(close_window.iloc[-1] / entry_price - 1.0)
    max_favorable_excursion = float(high_window.max() / entry_price - 1.0)
    max_adverse_excursion = float(low_window.min() / entry_price - 1.0)
    mature_time = _decision_time_from_index(ordered.index[last_position])
    return _OutcomeMetrics(
        label_mature_time=mature_time,
        realized_return=round(realized_return, 6),
        max_favorable_excursion=round(max_favorable_excursion, 6),
        max_adverse_excursion=round(max_adverse_excursion, 6),
    )


def _resolve_entry_position(
    *,
    bars: pd.DataFrame,
    row_position: int,
    label_policy: LabelPolicyRecord,
) -> tuple[int | None, float]:
    basis = label_policy.price_basis.strip().lower()
    if basis != "next_tradable_vwap":
        close_value = float(bars["close"].iloc[row_position])
        return row_position, close_value if close_value > 0 else 0.0

    max_search = min(len(bars), row_position + label_policy.horizon_days + 1)
    for candidate_position in range(row_position + 1, max_search):
        row = bars.iloc[candidate_position]
        if bool(row.get("suspended", False)):
            continue
        vwap_raw = row.get("vwap")
        if isinstance(vwap_raw, (int, float)) and float(vwap_raw) > 0:
            return candidate_position, float(vwap_raw)
        close_value = float(row.get("close", 0.0) or 0.0)
        if close_value > 0:
            return candidate_position, close_value
    if label_policy.exclude_untradable:
        return None, 0.0
    close_value = float(bars["close"].iloc[row_position])
    return row_position, close_value if close_value > 0 else 0.0


def _required_lookback_days(
    *,
    snapshots: Sequence[SignalSnapshot],
    as_of: datetime,
    label_policy_resolver: _LabelPolicyResolver,
) -> int:
    max_required = 120
    for snapshot in snapshots:
        policy = label_policy_resolver(snapshot)
        age_days = max(0, (as_of.date() - snapshot.decision_time.date()).days)
        max_required = max(max_required, age_days + policy.horizon_days + 30)
    return max_required


def _locate_snapshot_row_position(
    *,
    snapshot: SignalSnapshot,
    bars: pd.DataFrame,
) -> int | None:
    ordered_index = pd.DatetimeIndex(bars.index)
    if ordered_index.empty:
        return None
    target_date = snapshot.decision_time.date()
    matching = [position for position, item in enumerate(ordered_index.date) if item <= target_date]
    if not matching:
        return None
    return matching[-1]


def _feature_vector_from_row(row: pd.Series) -> dict[str, float]:
    return {str(key): float(value) for key, value in row.to_dict().items()}


def _estimate_data_quality_score(row: pd.Series) -> float:
    total = max(len(row.index), 1)
    non_zero = 0
    for value in row.to_list():
        try:
            if float(value) != 0.0:
                non_zero += 1
        except (TypeError, ValueError):
            continue
    return round(non_zero / total, 6)


def _merge_label_maturity_status(
    *,
    current_status: MaturityStatus,
    current_outcome: OutcomeRecord | None,
) -> MaturityStatus:
    if current_status == MaturityStatus.FULLY_MATURED:
        return MaturityStatus.FULLY_MATURED
    if current_status == MaturityStatus.RECONCILED:
        if (
            current_outcome is not None
            and current_outcome.execution_fill_ratio is not None
            and str(current_outcome.reconcile_status).strip()
        ):
            return MaturityStatus.FULLY_MATURED
        return MaturityStatus.RECONCILED
    return MaturityStatus.LABEL_MATURED


def _default_fidelity_tier_for_snapshot(snapshot: SignalSnapshot) -> BackfillFidelityTier:
    if snapshot.feature_capture_mode == FeatureCaptureMode.HYBRID:
        return BackfillFidelityTier.SILVER
    if snapshot.feature_capture_mode == FeatureCaptureMode.REPLAYED_RECOMPUTE:
        return BackfillFidelityTier.BRONZE
    return BackfillFidelityTier.GOLD


def _normalize_trainable_manifest_fidelity_filter(
    fidelity_filter: Sequence[BackfillFidelityTier] | None,
) -> list[BackfillFidelityTier]:
    if fidelity_filter is None:
        return [BackfillFidelityTier.GOLD, BackfillFidelityTier.SILVER]
    normalized: list[BackfillFidelityTier] = []
    seen: set[BackfillFidelityTier] = set()
    for item in fidelity_filter:
        tier = item if isinstance(item, BackfillFidelityTier) else BackfillFidelityTier(str(item))
        if tier in seen:
            continue
        seen.add(tier)
        normalized.append(tier)
    return normalized


def _normalize_trainable_manifest_maturity_statuses(
    maturity_statuses: Sequence[MaturityStatus] | None,
) -> list[MaturityStatus]:
    default_statuses = [
        MaturityStatus.LABEL_MATURED,
        MaturityStatus.RECONCILED,
        MaturityStatus.FULLY_MATURED,
    ]
    if maturity_statuses is None:
        return default_statuses
    normalized: list[MaturityStatus] = []
    seen: set[MaturityStatus] = set()
    for item in maturity_statuses:
        status = item if isinstance(item, MaturityStatus) else MaturityStatus(str(item))
        if status in seen:
            continue
        seen.add(status)
        normalized.append(status)
    return normalized or default_statuses


def _snapshot_matches_trainable_contract(
    *,
    snapshot: SignalSnapshot,
    outcome: OutcomeRecord | None,
    allowed_feature_schemas: dict[str, str],
    label_policy: LabelPolicyRecord,
    allowed_fidelity: set[BackfillFidelityTier],
    allowed_maturity: set[MaturityStatus],
) -> bool:
    expected_hash = allowed_feature_schemas.get(snapshot.feature_schema_id)
    if expected_hash is None or snapshot.feature_schema_hash != expected_hash:
        return False
    if snapshot.label_policy_id != label_policy.label_policy_id:
        return False
    if snapshot.label_policy_hash != label_policy.label_policy_hash:
        return False
    if outcome is None or outcome.maturity_status not in allowed_maturity:
        return False
    if not allowed_fidelity:
        return True
    return outcome.backfill_fidelity_tier in allowed_fidelity


def _is_better_trainable_manifest_candidate(
    *,
    candidate: _TrainableManifestCandidate,
    current_best: _TrainableManifestCandidate | None,
) -> bool:
    if current_best is None:
        return True
    if candidate.row_count != current_best.row_count:
        return candidate.row_count > current_best.row_count
    if candidate.feature_count != current_best.feature_count:
        return candidate.feature_count > current_best.feature_count
    if candidate.schema_created_at != current_best.schema_created_at:
        return candidate.schema_created_at > current_best.schema_created_at
    if candidate.label_policy_created_at != current_best.label_policy_created_at:
        return candidate.label_policy_created_at > current_best.label_policy_created_at
    return candidate.latest_decision_time > current_best.latest_decision_time


def _resolve_manifest_selection_mode(
    *,
    requested_feature_schema: FeatureSchemaRecord | None,
    requested_label_policy: LabelPolicyRecord | None,
) -> str:
    if requested_feature_schema is None and requested_label_policy is None:
        return "auto"
    if requested_feature_schema is not None and requested_label_policy is not None:
        return "explicit"
    return "filtered"


def _build_trainable_manifest_selection_rule(
    *,
    selection_mode: str,
    symbols: Sequence[str],
    fidelity_filter: Sequence[BackfillFidelityTier],
    maturity_statuses: Sequence[MaturityStatus],
    feature_schema_id: str,
    label_policy_id: str,
    time_window_start: datetime | None,
    time_window_end: datetime | None,
) -> str:
    fidelity_part = (
        "fidelity=" + ",".join(item.value for item in fidelity_filter)
        if fidelity_filter
        else "fidelity=*"
    )
    parts = [
        f"selection_mode={selection_mode.strip().lower() or 'auto'}",
        "symbols=" + (",".join(symbols) if symbols else "*"),
        fidelity_part,
        "maturity=" + ",".join(item.value for item in maturity_statuses),
        f"feature_schema_id={feature_schema_id.strip()}",
        f"label_policy_id={label_policy_id.strip()}",
    ]
    if time_window_start is not None:
        parts.append(f"time_window_start={time_window_start.isoformat()}")
    if time_window_end is not None:
        parts.append(f"time_window_end={time_window_end.isoformat()}")
    return "learning_backfill_trainable_v1:" + ";".join(parts)


def _build_backfill_snapshot_id(
    *,
    symbol: str,
    strategy: str,
    decision_time: datetime,
    feature_schema_id: str,
    label_policy_id: str,
    source: str,
) -> str:
    payload = "|".join(
        [
            symbol.strip().upper(),
            strategy.strip().lower(),
            decision_time.astimezone(UTC).isoformat(),
            feature_schema_id.strip(),
            label_policy_id.strip(),
            source.strip().lower(),
        ]
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]
    return f"backfill_snap_{digest}"


def _group_snapshots_by_symbol(
    snapshots: Sequence[SignalSnapshot],
) -> dict[str, list[SignalSnapshot]]:
    grouped: dict[str, list[SignalSnapshot]] = {}
    for snapshot in snapshots:
        symbol = _normalize_symbol(snapshot.symbol)
        if not symbol:
            continue
        grouped.setdefault(symbol, []).append(snapshot)
    return grouped


def _normalize_symbols(symbols: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in symbols:
        symbol = _normalize_symbol(item)
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        normalized.append(symbol)
    return normalized


def _normalize_symbol(value: object) -> str:
    text = str(value).strip().upper()
    if not text:
        return ""
    primary = text.split(".", maxsplit=1)[0]
    if len(primary) == 6 and primary.isdigit():
        return primary
    return text


def _decision_time_from_index(value: object) -> datetime:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return datetime.combine(timestamp.date(), _BUSINESS_CLOSE_UTC)
    return timestamp.to_pydatetime().astimezone(UTC)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _ensure_optional_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _ensure_utc(value)


def _coerce_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _merge_mapping(
    current: Mapping[str, object],
    updates: Mapping[str, object],
) -> dict[str, object]:
    merged = {str(key): value for key, value in current.items()}
    for key, value in updates.items():
        merged[str(key)] = value
    return merged


def _extract_archive_snapshot_id(source: object) -> str:
    payload = _coerce_mapping(source)
    direct_snapshot_id = str(payload.get("snapshot_id", "")).strip()
    if direct_snapshot_id:
        return direct_snapshot_id
    decision_trace = _coerce_mapping(payload.get("decision_trace"))
    protocol = _coerce_mapping(decision_trace.get("learning_protocol"))
    return str(protocol.get("snapshot_id", "")).strip()


def _runtime_archive_snapshot_id_for_trade(
    *,
    trade: Mapping[str, object],
    snapshot_ids_by_symbol: Mapping[str, str],
    latest_signal_by_symbol: Mapping[str, Mapping[str, object]],
) -> str:
    snapshot_id = _extract_archive_snapshot_id(trade)
    if snapshot_id:
        return snapshot_id
    recommendation_id = str(trade.get("recommendation_id", "")).strip().upper()
    if recommendation_id:
        raw_signals = [
            item
            for item in latest_signal_by_symbol.values()
            if str(item.get("recommendation_id", "")).strip().upper() == recommendation_id
        ]
        for signal in raw_signals:
            snapshot_id = _extract_archive_snapshot_id(signal)
            if snapshot_id:
                return snapshot_id
    symbol = _normalize_symbol(trade.get("symbol"))
    if not symbol:
        return ""
    latest_signal = latest_signal_by_symbol.get(symbol, {})
    snapshot_id = _extract_archive_snapshot_id(latest_signal)
    if snapshot_id:
        return snapshot_id
    return str(snapshot_ids_by_symbol.get(symbol, "")).strip()


def _runtime_archive_reference_price_for_trade(
    *,
    trade: Mapping[str, object],
    latest_signal: Mapping[str, object],
) -> float:
    candidates = (
        trade.get("reference_price"),
        trade.get("recommended_price"),
        trade.get("signal_price"),
    )
    for item in candidates:
        value = _as_float(item, default=0.0)
        if value > 0:
            return value
    signal_trade_plan = _coerce_mapping(latest_signal.get("trade_plan"))
    value = _as_float(signal_trade_plan.get("reference_price"), default=0.0)
    if value > 0:
        return value
    return _as_float(latest_signal.get("reference_price"), default=0.0)


def _parse_optional_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return _ensure_utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def _as_float(value: object, *, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return float(text)
        except ValueError:
            return default
    return default


def _runtime_archive_strategy_position_map(
    payload: Mapping[str, object],
) -> dict[str, float]:
    runtime_state = _coerce_mapping(payload.get("runtime_state"))
    portfolio = _coerce_mapping(runtime_state.get("portfolio"))
    raw_positions = portfolio.get("positions")
    if not isinstance(raw_positions, list):
        raw_portfolio = _coerce_mapping(payload.get("portfolio"))
        raw_positions = raw_portfolio.get("positions")
    position_map: dict[str, float] = {}
    if not isinstance(raw_positions, list):
        return position_map
    for item in raw_positions:
        row = _coerce_mapping(item)
        symbol = _normalize_symbol(row.get("symbol"))
        if not symbol:
            continue
        position_map[symbol] = _as_float(row.get("target_position"), default=0.0)
    return position_map


def _runtime_archive_broker_position_map(
    payload: Mapping[str, object],
) -> dict[str, float]:
    runtime_state = _coerce_mapping(payload.get("runtime_state"))
    raw_broker_positions = runtime_state.get("broker_positions")
    if not isinstance(raw_broker_positions, Mapping):
        return {}
    return {
        symbol: _as_float(value, default=0.0)
        for key, value in raw_broker_positions.items()
        if (symbol := _normalize_symbol(key))
    }


def _backfill_fidelity_rank(tier: BackfillFidelityTier | None) -> int:
    if tier == BackfillFidelityTier.GOLD:
        return 3
    if tier == BackfillFidelityTier.SILVER:
        return 2
    if tier == BackfillFidelityTier.BRONZE:
        return 1
    return 0


def _prefer_backfill_fidelity_tier(
    *,
    current: BackfillFidelityTier | None,
    candidate: BackfillFidelityTier,
) -> BackfillFidelityTier:
    if _backfill_fidelity_rank(candidate) >= _backfill_fidelity_rank(current):
        return candidate
    return current or candidate


def _calculate_archive_execution_slippage_bp(
    *,
    side: str,
    execution_price: float,
    reference_price: float,
) -> float | None:
    if execution_price <= 0 or reference_price <= 0:
        return None
    normalized_side = side.strip().lower()
    if normalized_side == "sell":
        delta = reference_price - execution_price
    else:
        delta = execution_price - reference_price
    return round((delta / reference_price) * 10000.0, 4)


def _resolve_runtime_history_archive_paths(
    *,
    archive_dir: str | Path,
    archive_paths: Sequence[str | Path] | None,
) -> list[Path]:
    resolved: list[Path] = []
    seen: set[str] = set()
    for item in archive_paths or []:
        path = Path(str(item)).expanduser()
        token = str(path.resolve(strict=False)).lower()
        if token in seen:
            continue
        seen.add(token)
        if path.is_file():
            resolved.append(path)
    if resolved:
        return sorted(resolved, key=lambda item: item.name)

    if not str(archive_dir).strip():
        return []
    directory = Path(str(archive_dir)).expanduser()
    if not directory.exists() or not directory.is_dir():
        return []
    for path in sorted(directory.glob("runtime_history_*.json"), key=lambda item: item.name):
        token = str(path.resolve(strict=False)).lower()
        if token in seen:
            continue
        seen.add(token)
        if path.is_file():
            resolved.append(path)
    return resolved


def _stable_config_hash(config: StockAnalyzerConfig) -> str:
    payload = config.model_dump(mode="json")
    serialized = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
