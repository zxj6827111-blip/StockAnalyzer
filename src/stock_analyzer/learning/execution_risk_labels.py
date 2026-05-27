"""Execution-risk label extraction from the learning protocol sample store."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from stock_analyzer.learning.sample_schema import MaturityStatus, OutcomeRecord, SignalSnapshot
from stock_analyzer.learning.sample_store import SampleStore


class ExecutionRiskTarget(StrEnum):
    CAN_FILL = "can_fill"
    LIKELY_SLIPPAGE_HIGH = "likely_slippage_high"
    SIM_BROKER_DIVERGENCE_RISK = "sim_broker_divergence_risk"
    RECONCILE_MISMATCH_RISK = "reconcile_mismatch_risk"


@dataclass(frozen=True, slots=True)
class ExecutionRiskLabelingConfig:
    """Thresholds used to derive execution-risk targets."""

    fill_ratio_threshold: float = 0.9
    slippage_bp_threshold: float = 12.0
    sim_broker_diff_threshold: float = 0.02
    reconcile_mismatch_statuses: tuple[str, ...] = (
        "mismatch",
        "manual_override",
        "manual_adjusted",
        "diverged",
        "failed",
    )


@dataclass(slots=True)
class ExecutionRiskLabeledRow:
    """One execution-risk training row with ex-ante features and ex-post targets."""

    snapshot_id: str
    symbol: str
    strategy: str
    decision_time: datetime
    maturity_status: str
    backfill_fidelity_tier: str
    feature_vector: dict[str, float] = field(default_factory=dict)
    targets: dict[str, float] = field(default_factory=dict)
    outcome_context: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "symbol": self.symbol,
            "strategy": self.strategy,
            "decision_time": self.decision_time.isoformat(),
            "maturity_status": self.maturity_status,
            "backfill_fidelity_tier": self.backfill_fidelity_tier,
            "feature_vector": dict(self.feature_vector),
            "targets": dict(self.targets),
            "outcome_context": dict(self.outcome_context),
        }

    def preview_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "symbol": self.symbol,
            "decision_time": self.decision_time.isoformat(),
            "targets": dict(self.targets),
            "outcome_context": {
                "execution_fill_ratio": self.outcome_context.get("execution_fill_ratio"),
                "realized_slippage_bp": self.outcome_context.get("realized_slippage_bp"),
                "reconcile_status": self.outcome_context.get("reconcile_status"),
                "sim_vs_broker_diff": self.outcome_context.get("sim_vs_broker_diff"),
            },
        }


@dataclass(slots=True)
class ExecutionRiskDataset:
    """Collection of labeled execution-risk rows and audit metadata."""

    dataset_id: str
    generated_at: str
    requested_maturity_statuses: list[str]
    feature_names: list[str]
    row_count: int
    source_snapshot_count: int
    skipped_missing_outcome: int
    skipped_by_maturity: int
    skipped_missing_targets: int
    target_coverage: dict[str, int] = field(default_factory=dict)
    rows: list[ExecutionRiskLabeledRow] = field(default_factory=list)

    def rows_for_target(self, target: str | ExecutionRiskTarget) -> list[ExecutionRiskLabeledRow]:
        normalized_target = _normalize_target_name(target)
        return [row for row in self.rows if normalized_target in row.targets]

    def to_dict(self, *, include_rows: bool = True, preview_limit: int = 5) -> dict[str, object]:
        payload = {
            "dataset_id": self.dataset_id,
            "generated_at": self.generated_at,
            "requested_maturity_statuses": list(self.requested_maturity_statuses),
            "feature_names": list(self.feature_names),
            "row_count": self.row_count,
            "source_snapshot_count": self.source_snapshot_count,
            "skipped_missing_outcome": self.skipped_missing_outcome,
            "skipped_by_maturity": self.skipped_by_maturity,
            "skipped_missing_targets": self.skipped_missing_targets,
            "target_coverage": dict(self.target_coverage),
            "preview": [row.preview_dict() for row in self.rows[: max(1, int(preview_limit))]],
        }
        if include_rows:
            payload["rows"] = [row.to_dict() for row in self.rows]
        return payload


class ExecutionRiskLabelBuilder:
    """Resolve execution-risk labels from persisted snapshots and outcomes."""

    def __init__(
        self,
        *,
        store: SampleStore,
        labeling: ExecutionRiskLabelingConfig | None = None,
    ) -> None:
        self._store = store
        self._labeling = labeling or ExecutionRiskLabelingConfig()

    def build_dataset(
        self,
        *,
        snapshot_ids: Sequence[str] | None = None,
        maturity_statuses: Sequence[MaturityStatus | str] | None = None,
        max_rows: int | None = None,
        now: datetime | None = None,
    ) -> ExecutionRiskDataset:
        normalized_statuses = _normalize_maturity_statuses(maturity_statuses)
        snapshots = self._store.list_snapshots(snapshot_ids=snapshot_ids)
        outcome_map = {
            outcome.snapshot_id: outcome
            for outcome in self._store.list_outcomes(
                snapshot_ids=[snapshot.snapshot_id for snapshot in snapshots]
            )
        }
        ordered_snapshots = sorted(
            snapshots,
            key=lambda snapshot: (snapshot.decision_time, snapshot.symbol, snapshot.snapshot_id),
        )

        skipped_missing_outcome = 0
        skipped_by_maturity = 0
        skipped_missing_targets = 0
        rows: list[ExecutionRiskLabeledRow] = []
        feature_names: set[str] = set()
        target_coverage: dict[str, int] = {}

        for snapshot in ordered_snapshots:
            outcome = outcome_map.get(snapshot.snapshot_id)
            if outcome is None:
                skipped_missing_outcome += 1
                continue
            if outcome.maturity_status not in normalized_statuses:
                skipped_by_maturity += 1
                continue

            targets = self._resolve_targets(outcome=outcome)
            if not targets:
                skipped_missing_targets += 1
                continue

            features = self._build_feature_vector(snapshot=snapshot)
            if not features:
                skipped_missing_targets += 1
                continue
            feature_names.update(features.keys())
            for target_name in targets:
                target_coverage[target_name] = target_coverage.get(target_name, 0) + 1
            rows.append(
                ExecutionRiskLabeledRow(
                    snapshot_id=snapshot.snapshot_id,
                    symbol=snapshot.symbol,
                    strategy=snapshot.strategy,
                    decision_time=snapshot.decision_time,
                    maturity_status=outcome.maturity_status.value,
                    backfill_fidelity_tier=(
                        outcome.backfill_fidelity_tier.value
                        if outcome.backfill_fidelity_tier is not None
                        else ""
                    ),
                    feature_vector=features,
                    targets=targets,
                    outcome_context=_build_outcome_context(outcome),
                )
            )

        resolved_max_rows = max(0, int(max_rows or 0))
        if resolved_max_rows > 0 and len(rows) > resolved_max_rows:
            rows = rows[-resolved_max_rows:]
            target_coverage = {}
            feature_names = set()
            for row in rows:
                feature_names.update(row.feature_vector.keys())
                for target_name in row.targets:
                    target_coverage[target_name] = target_coverage.get(target_name, 0) + 1

        run_now = _as_utc_datetime(now or datetime.now(UTC))
        dataset_id = _build_dataset_id(
            rows=rows,
            generated_at=run_now,
            maturity_statuses=normalized_statuses,
            labeling=self._labeling,
        )
        return ExecutionRiskDataset(
            dataset_id=dataset_id,
            generated_at=run_now.isoformat(),
            requested_maturity_statuses=[status.value for status in normalized_statuses],
            feature_names=sorted(feature_names),
            row_count=len(rows),
            source_snapshot_count=len(ordered_snapshots),
            skipped_missing_outcome=skipped_missing_outcome,
            skipped_by_maturity=skipped_by_maturity,
            skipped_missing_targets=skipped_missing_targets,
            target_coverage=target_coverage,
            rows=rows,
        )

    def _build_feature_vector(self, *, snapshot: SignalSnapshot) -> dict[str, float]:
        return build_execution_risk_feature_vector(snapshot=snapshot)

    def _resolve_targets(self, *, outcome: OutcomeRecord) -> dict[str, float]:
        return resolve_execution_risk_targets(outcome=outcome, labeling=self._labeling)


def resolve_execution_risk_targets(
    *,
    outcome: OutcomeRecord,
    labeling: ExecutionRiskLabelingConfig | None = None,
) -> dict[str, float]:
    """Resolve execution-risk targets without applying maturity filters."""

    resolved_labeling = labeling or ExecutionRiskLabelingConfig()
    targets: dict[str, float] = {}

    fill_ratio = outcome.execution_fill_ratio
    if fill_ratio is not None:
        targets[ExecutionRiskTarget.CAN_FILL.value] = float(
            float(fill_ratio) >= float(resolved_labeling.fill_ratio_threshold)
        )

    slippage_bp = outcome.realized_slippage_bp
    if slippage_bp is not None:
        targets[ExecutionRiskTarget.LIKELY_SLIPPAGE_HIGH.value] = float(
            float(slippage_bp) >= float(resolved_labeling.slippage_bp_threshold)
        )

    reconcile_status = str(outcome.reconcile_status or "").strip().lower()
    sim_vs_broker_diff = outcome.sim_vs_broker_diff
    is_reconcile_mismatch = (
        reconcile_status in resolved_labeling.reconcile_mismatch_statuses
        if reconcile_status
        else False
    )
    if reconcile_status:
        targets[ExecutionRiskTarget.RECONCILE_MISMATCH_RISK.value] = float(
            is_reconcile_mismatch
        )
    if reconcile_status or sim_vs_broker_diff is not None:
        divergence_risk = is_reconcile_mismatch
        if sim_vs_broker_diff is not None:
            divergence_risk = divergence_risk or (
                abs(float(sim_vs_broker_diff))
                >= float(resolved_labeling.sim_broker_diff_threshold)
            )
        targets[ExecutionRiskTarget.SIM_BROKER_DIVERGENCE_RISK.value] = float(divergence_risk)
    return targets


def _build_outcome_context(outcome: OutcomeRecord) -> dict[str, object]:
    return {
        "execution_fill_ratio": outcome.execution_fill_ratio,
        "realized_slippage_bp": outcome.realized_slippage_bp,
        "sim_vs_broker_diff": outcome.sim_vs_broker_diff,
        "reconcile_status": outcome.reconcile_status,
        "realized_return": outcome.realized_return,
        "max_favorable_excursion": outcome.max_favorable_excursion,
        "max_adverse_excursion": outcome.max_adverse_excursion,
    }


def build_execution_risk_feature_vector(
    *,
    snapshot: SignalSnapshot,
    model_outputs: Mapping[str, object] | None = None,
) -> dict[str, float]:
    """Build the flattened execution-risk feature vector for one snapshot."""

    features = {str(key): float(value) for key, value in snapshot.feature_vector.items()}
    features["meta__data_quality_score"] = float(snapshot.data_quality_score or 0.0)
    features["meta__sample_weight"] = float(snapshot.sample_weight)
    features["meta__decision_weekday"] = float(snapshot.decision_time.weekday())
    features["meta__decision_month"] = float(snapshot.decision_time.month)
    features["meta__decision_hour"] = float(snapshot.decision_time.hour)
    features.update(
        _flatten_numeric_mapping(
            model_outputs if model_outputs is not None else snapshot.model_outputs,
            prefix="model_output__",
        )
    )
    features.update(_flatten_numeric_mapping(snapshot.score_breakdown, prefix="score__"))
    features.update(_flatten_numeric_mapping(snapshot.risk_context, prefix="risk__"))
    features.update(_flatten_numeric_mapping(snapshot.news_context, prefix="news__"))
    features.update(_flatten_numeric_mapping(snapshot.regime_context, prefix="regime__"))
    return {
        key: value
        for key, value in features.items()
        if key.strip() and math.isfinite(float(value))
    }


def _flatten_numeric_mapping(
    payload: Mapping[str, object],
    *,
    prefix: str,
) -> dict[str, float]:
    flattened: dict[str, float] = {}

    def visit(value: Mapping[str, object], head: str) -> None:
        for raw_key, item in value.items():
            key = str(raw_key).strip()
            if not key:
                continue
            feature_name = f"{head}{key}"
            if isinstance(item, Mapping):
                visit(item, f"{feature_name}__")
                continue
            parsed = _as_feature_float(item)
            if parsed is None:
                continue
            flattened[feature_name] = parsed

    visit(payload, prefix)
    return flattened


def _as_feature_float(value: object) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = float(text)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def _normalize_maturity_statuses(
    statuses: Sequence[MaturityStatus | str] | None,
) -> tuple[MaturityStatus, ...]:
    if not statuses:
        return (MaturityStatus.FULLY_MATURED,)
    normalized: list[MaturityStatus] = []
    for item in statuses:
        if isinstance(item, MaturityStatus):
            normalized.append(item)
            continue
        text = str(item).strip().lower()
        if not text:
            continue
        normalized.append(MaturityStatus(text))
    deduped: list[MaturityStatus] = []
    seen: set[MaturityStatus] = set()
    for item in normalized:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return tuple(deduped) or (MaturityStatus.FULLY_MATURED,)


def _normalize_target_name(target: str | ExecutionRiskTarget) -> str:
    return target.value if isinstance(target, ExecutionRiskTarget) else str(target).strip().lower()


def _build_dataset_id(
    *,
    rows: Sequence[ExecutionRiskLabeledRow],
    generated_at: datetime,
    maturity_statuses: Sequence[MaturityStatus],
    labeling: ExecutionRiskLabelingConfig,
) -> str:
    material = {
        "generated_at": generated_at.isoformat(),
        "maturity_statuses": [status.value for status in maturity_statuses],
        "labeling": {
            "fill_ratio_threshold": labeling.fill_ratio_threshold,
            "slippage_bp_threshold": labeling.slippage_bp_threshold,
            "sim_broker_diff_threshold": labeling.sim_broker_diff_threshold,
            "reconcile_mismatch_statuses": list(labeling.reconcile_mismatch_statuses),
        },
        "snapshot_ids": [row.snapshot_id for row in rows],
    }
    digest = hashlib.sha256(
        json.dumps(material, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"execution_risk_dataset_v1_{digest[:12]}"


def _as_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
