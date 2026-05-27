from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import cast

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.feature.engineer import FeatureEngineer
from stock_analyzer.learning import (
    DatasetManifestBuilder,
    FeatureSchemaRegistry,
    LabelPolicyRegistry,
    LearningBackfillEngine,
    SampleStore,
)
from stock_analyzer.learning.feedback_features import (
    LEARNING_PROTOCOL_FEEDBACK_FEATURE_COLUMNS,
)
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    FeatureCaptureMode,
    MaturityStatus,
    OutcomeRecord,
    SignalSnapshot,
)
from stock_analyzer.models.artifact import ModelArtifact
from stock_analyzer.runtime.service import StockAnalyzerService


def _load_test_config(tmp_path: Path) -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = False
    config.training.bootstrap_auto_seed_watchlist = False
    config.training.bootstrap_state_path = str(tmp_path / "bootstrap_state.json")
    config.training.artifact_path = str(tmp_path / "model.json")
    config.evolution.auto_run = False
    config.cloud_backup.enabled = False
    config.market_warehouse.auto_run = False
    config.tdx_sync.auto_run = False
    config.idle_queue.enabled = False
    config.idle_queue.auto_run = False
    offline_root = tmp_path / "offline"
    offline_root.mkdir(parents=True, exist_ok=True)
    config.data_source.local_data_root = str(offline_root)
    return config


def _build_engine_fixture(
    tmp_path: Path,
) -> tuple[
    StockAnalyzerConfig,
    SyntheticProvider,
    SampleStore,
    FeatureSchemaRegistry,
    LabelPolicyRegistry,
    LearningBackfillEngine,
]:
    config = _load_test_config(tmp_path)
    provider = SyntheticProvider(seed_offset=2401)
    db_path = tmp_path / "learning_protocol.duckdb"
    store = SampleStore(db_path=db_path)
    feature_registry = FeatureSchemaRegistry(db_path=db_path)
    label_registry = LabelPolicyRegistry(db_path=db_path)
    engine = LearningBackfillEngine(
        config=config,
        provider=provider,
        sample_store=store,
        feature_schema_registry=feature_registry,
        label_policy_registry=label_registry,
    )
    return config, provider, store, feature_registry, label_registry, engine


def _seed_observed_snapshot(
    *,
    config: StockAnalyzerConfig,
    provider: SyntheticProvider,
    store: SampleStore,
    feature_registry: FeatureSchemaRegistry,
    label_registry: LabelPolicyRegistry,
    symbol: str,
    end_date: date,
    row_offset: int,
    outcome: OutcomeRecord,
) -> SignalSnapshot:
    bars = provider.fetch_daily_bars(symbol=symbol, lookback_days=90, end_date=end_date)
    features = FeatureEngineer().transform(bars)
    feature_record = feature_registry.register_from_frame(
        features,
        feature_engineer_name="FeatureEngineer",
        feature_engineer_version="transform_t1_v1",
        code_version="git:test",
        fillna_policy="fill_zero_after_shift",
        normalization_hint="t1_shifted",
    )
    label_record = label_registry.register_from_config(config.labels)
    decision_index = max(20, row_offset)
    decision_time = datetime.combine(
        bars.index[decision_index].date(),
        time(15, 0, tzinfo=UTC),
    )
    snapshot = SignalSnapshot(
        snapshot_id=f"{symbol}-observed-{decision_index:03d}",
        code_version="git:test",
        symbol=symbol,
        strategy="trend",
        decision_time=decision_time,
        feature_vector={
            str(key): float(value) for key, value in features.iloc[decision_index].to_dict().items()
        },
        feature_schema_id=feature_record.feature_schema_id,
        feature_schema_hash=feature_record.feature_schema_hash,
        feature_capture_mode=FeatureCaptureMode.OBSERVED_SNAPSHOT,
        feature_observed_at=decision_time,
        runtime_config_hash="runtime_hash_test",
        label_policy_id=label_record.label_policy_id,
        label_policy_hash=label_record.label_policy_hash,
    )
    store.write_snapshot(snapshot)
    store.upsert_outcome(
        outcome.model_copy(update={"snapshot_id": snapshot.snapshot_id}, deep=True)
    )
    return snapshot


def _seed_projection_compatible_trainable_samples(
    *,
    config: StockAnalyzerConfig,
    store: SampleStore,
    feature_registry: FeatureSchemaRegistry,
    label_registry: LabelPolicyRegistry,
    symbol: str,
    legacy_rows: int = 16,
    current_rows: int = 8,
) -> tuple[object, object]:
    legacy_record = feature_registry.register_feature_names(
        feature_names=["feature_a", "feature_b"],
        feature_schema_id="feature_schema_legacy_trainable",
        feature_engineer_version="test",
        code_version="git:test",
    )
    current_record = feature_registry.register_feature_names(
        feature_names=["feature_a", "feature_b", "feature_c"],
        feature_schema_id="feature_schema_current_trainable",
        feature_engineer_version="test",
        code_version="git:test",
        projection_compatible_from=[legacy_record.feature_schema_id],
    )
    label_record = label_registry.register_from_config(
        config.labels,
        label_policy_id="label_policy_trainable_manifest",
    )
    base_time = datetime(2026, 1, 1, 15, 0, tzinfo=UTC)

    for index in range(legacy_rows):
        decision_time = base_time + timedelta(days=index)
        snapshot = SignalSnapshot(
            snapshot_id=f"{symbol}-legacy-trainable-{index:03d}",
            code_version="git:test",
            symbol=symbol,
            strategy="trend",
            decision_time=decision_time,
            feature_vector={
                "feature_a": float((index % 5) / 5.0),
                "feature_b": float((index % 7) - 3),
            },
            feature_schema_id=legacy_record.feature_schema_id,
            feature_schema_hash=legacy_record.feature_schema_hash,
            feature_capture_mode=FeatureCaptureMode.OBSERVED_SNAPSHOT,
            feature_observed_at=decision_time,
            runtime_config_hash="runtime_hash_trainable",
            label_policy_id=label_record.label_policy_id,
            label_policy_hash=label_record.label_policy_hash,
        )
        outcome = OutcomeRecord(
            snapshot_id=snapshot.snapshot_id,
            maturity_status=MaturityStatus.RECONCILED,
            label_mature_time=decision_time + timedelta(days=config.labels.horizon_days),
            realized_return=0.08 if index % 2 == 0 else -0.05,
            max_favorable_excursion=0.10 if index % 2 == 0 else 0.02,
            max_adverse_excursion=-0.02 if index % 2 == 0 else -0.07,
            backfill_fidelity_tier=BackfillFidelityTier.GOLD,
            backfill_source="runtime_observed",
        )
        store.write_snapshot(snapshot)
        store.upsert_outcome(outcome)

    for index in range(current_rows):
        decision_time = base_time + timedelta(days=legacy_rows + index)
        snapshot = SignalSnapshot(
            snapshot_id=f"{symbol}-current-trainable-{index:03d}",
            code_version="git:test",
            symbol=symbol,
            strategy="trend",
            decision_time=decision_time,
            feature_vector={
                "feature_a": float(((index + 1) % 5) / 5.0),
                "feature_b": float(((index + 2) % 7) - 3),
                "feature_c": float(index) / 10.0,
            },
            feature_schema_id=current_record.feature_schema_id,
            feature_schema_hash=current_record.feature_schema_hash,
            feature_capture_mode=FeatureCaptureMode.OBSERVED_SNAPSHOT,
            feature_observed_at=decision_time,
            runtime_config_hash="runtime_hash_trainable",
            label_policy_id=label_record.label_policy_id,
            label_policy_hash=label_record.label_policy_hash,
        )
        outcome = OutcomeRecord(
            snapshot_id=snapshot.snapshot_id,
            maturity_status=MaturityStatus.FULLY_MATURED,
            label_mature_time=decision_time + timedelta(days=config.labels.horizon_days),
            realized_return=0.09 if index % 2 == 0 else -0.04,
            max_favorable_excursion=0.11 if index % 2 == 0 else 0.02,
            max_adverse_excursion=-0.02 if index % 2 == 0 else -0.06,
            execution_fill_ratio=1.0,
            reconcile_status="ok",
            backfill_fidelity_tier=BackfillFidelityTier.GOLD,
            backfill_source="runtime_observed",
        )
        store.write_snapshot(snapshot)
        store.upsert_outcome(outcome)

    return current_record, label_record


def _as_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return cast(Mapping[str, object], value)


def _build_runtime_history_archive_payload(
    *,
    snapshot: SignalSnapshot,
    recommendation_id: str = "REC-LEARNING-ARCHIVE-1",
    entry_price: float = 10.25,
    reference_price: float = 10.0,
) -> dict[str, object]:
    return {
        "archive_version": 1,
        "generated_at": "2026-03-31T15:30:00+00:00",
        "day": "2026-03-31",
        "latest_signals": {
            "trace_id": "trace-learning-archive-1",
            "signals": [
                {
                    "symbol": snapshot.symbol,
                    "strategy": snapshot.strategy,
                    "recommendation_id": recommendation_id,
                    "snapshot_id": snapshot.snapshot_id,
                    "decision_trace": {
                        "runtime_feedback": {
                            "m1": {"execution_pressure": 0.2},
                            "m3": {"runtime_regime": "trend"},
                            "m7": {"m7_news_count": 3},
                        }
                    },
                }
            ],
        },
        "runtime_state": {
            "portfolio": {
                "positions": [
                    {
                        "symbol": snapshot.symbol,
                        "target_position": 0.2,
                    }
                ]
            },
            "broker_positions": {snapshot.symbol: 0.2},
        },
        "reconcile": {
            "latest": {
                "timestamp": "2026-03-31T15:30:00+00:00",
                "status": "ok",
                "matched_count": 1,
                "mismatch_count": 0,
                "missing_in_strategy": [],
                "missing_in_broker": [],
                "diffs": [],
                "strategy_positions": 1,
                "broker_positions": 1,
            }
        },
        "runtime": {
            "audit_events": [
                {
                    "timestamp": "2026-03-31T10:00:00+00:00",
                    "event_type": "command_accepted",
                    "payload": {
                        "action": "SET_POSITION",
                        "command_update": {
                            "action": "SET_POSITION",
                            "status": "opened",
                            "symbol": snapshot.symbol,
                            "target_position": 0.2,
                            "manual_fill": {
                                "entry_price": entry_price,
                                "quantity": 1000,
                            },
                            "recommendation_reference": {
                                "recommendation_id": recommendation_id,
                                "snapshot_id": snapshot.snapshot_id,
                                "reference_price": reference_price,
                                "target_position": 0.2,
                                "strategy": snapshot.strategy,
                            },
                        },
                    },
                }
            ]
        },
    }


def test_bootstrap_backfill_is_idempotent_and_manifest_filters_bronze(tmp_path: Path) -> None:
    _config, _provider, store, feature_registry, _label_registry, engine = _build_engine_fixture(
        tmp_path
    )

    first = engine.bootstrap_backfill(
        symbols=["600000"],
        lookback_days=90,
        end_date=date(2026, 3, 31),
        min_history_rows=35,
    )
    counts_after_first = store.counts()
    second = engine.bootstrap_backfill(
        symbols=["600000"],
        lookback_days=90,
        end_date=date(2026, 3, 31),
        min_history_rows=35,
    )
    counts_after_second = store.counts()

    assert first["ok"] is True
    assert counts_after_first["signal_snapshots"] > 0
    assert counts_after_first["signal_snapshots"] == counts_after_first["outcome_records"]
    assert first["snapshots_inserted"] == counts_after_first["signal_snapshots"]
    assert second["ok"] is True
    assert second["snapshots_inserted"] == 0
    assert second["snapshots_skipped_existing"] == counts_after_first["signal_snapshots"]
    assert counts_after_first == counts_after_second

    first_snapshot_id = store.list_snapshot_ids()[0]
    first_snapshot = store.get_snapshot(first_snapshot_id)
    assert first_snapshot is not None
    first_schema = feature_registry.get_by_id(first_snapshot.feature_schema_id)
    assert first_schema is not None
    assert first_schema.feature_names[-len(LEARNING_PROTOCOL_FEEDBACK_FEATURE_COLUMNS) :] == list(
        LEARNING_PROTOCOL_FEEDBACK_FEATURE_COLUMNS
    )
    assert first_snapshot.feature_vector["lp_m3_match_score"] == 0.0

    bronze_manifest = DatasetManifestBuilder(store=store).create_manifest(
        feature_schema_id=first_snapshot.feature_schema_id,
        feature_schema_hash=first_snapshot.feature_schema_hash,
        label_policy_id=first_snapshot.label_policy_id,
        label_policy_hash=first_snapshot.label_policy_hash,
        fidelity_filter=[BackfillFidelityTier.BRONZE],
    )
    gold_manifest = DatasetManifestBuilder(store=store).create_manifest(
        feature_schema_id=first_snapshot.feature_schema_id,
        feature_schema_hash=first_snapshot.feature_schema_hash,
        label_policy_id=first_snapshot.label_policy_id,
        label_policy_hash=first_snapshot.label_policy_hash,
        fidelity_filter=[BackfillFidelityTier.GOLD, BackfillFidelityTier.SILVER],
    )

    assert bronze_manifest.included_snapshot_count == counts_after_first["signal_snapshots"]
    assert bronze_manifest.fidelity_breakdown == {
        "bronze": counts_after_first["signal_snapshots"]
    }
    assert gold_manifest.included_snapshot_count == 0
    assert gold_manifest.dropped_reason_breakdown == {
        "fidelity_filtered:bronze": counts_after_first["signal_snapshots"]
    }

    trainable_manifest = engine.build_trainable_manifest(symbols=["600000"])

    assert trainable_manifest["ok"] is False
    assert trainable_manifest["errors"] == ["no_trainable_candidates"]


def test_build_trainable_manifest_auto_selects_projection_compatible_current_schema(
    tmp_path: Path,
) -> None:
    config, _provider, store, feature_registry, label_registry, engine = _build_engine_fixture(
        tmp_path
    )
    current_record, label_record = _seed_projection_compatible_trainable_samples(
        config=config,
        store=store,
        feature_registry=feature_registry,
        label_registry=label_registry,
        symbol="600000.SH",
    )

    payload = engine.build_trainable_manifest(symbols=["600000"])
    manifest = store.get_manifest(str(payload["dataset_manifest_id"]))
    manifest_snapshot_ids = store.list_manifest_snapshot_ids(str(payload["dataset_manifest_id"]))

    assert payload["ok"] is True
    assert payload["selection_mode"] == "auto"
    assert payload["feature_schema_id"] == current_record.feature_schema_id
    assert payload["feature_schema_hash"] == current_record.feature_schema_hash
    assert payload["label_policy_id"] == label_record.label_policy_id
    assert payload["included_snapshot_count"] == 24
    assert sum(int(value) for value in _as_mapping(payload["split_counts"]).values()) == 24
    assert payload["fidelity_breakdown"] == {"gold": 24}
    assert str(payload["dataset_manifest_id"]).startswith("dataset_manifest_v1_")
    assert manifest is not None
    assert manifest.feature_schema_id == current_record.feature_schema_id
    assert "600000.SH-legacy-trainable-000" in manifest_snapshot_ids
    assert "600000.SH-current-trainable-000" in manifest_snapshot_ids


def test_incremental_backfill_promotes_pending_observed_snapshot_to_label_matured(
    tmp_path: Path,
) -> None:
    config, provider, store, feature_registry, label_registry, engine = _build_engine_fixture(
        tmp_path
    )
    snapshot = _seed_observed_snapshot(
        config=config,
        provider=provider,
        store=store,
        feature_registry=feature_registry,
        label_registry=label_registry,
        symbol="600000",
        end_date=date(2026, 3, 31),
        row_offset=40,
        outcome=OutcomeRecord(
            snapshot_id="placeholder",
            maturity_status=MaturityStatus.PENDING,
            outcome_updated_at=datetime(2026, 3, 1, 15, 0, tzinfo=UTC),
        ),
    )

    result = engine.incremental_backfill(
        symbols=["600000"],
        as_of=datetime(2026, 3, 31, 15, 0, tzinfo=UTC),
    )
    outcome = store.get_outcome(snapshot.snapshot_id)

    assert result["ok"] is True
    assert result["promoted_label_matured"] == 1
    assert outcome is not None
    assert outcome.maturity_status == MaturityStatus.LABEL_MATURED
    assert outcome.label_mature_time is not None
    assert outcome.realized_return is not None
    assert outcome.backfill_fidelity_tier == BackfillFidelityTier.GOLD
    assert outcome.recomputed_feature_schema_id == ""


def test_repair_backfill_promotes_reconciled_sample_to_fully_matured(tmp_path: Path) -> None:
    config, provider, store, feature_registry, label_registry, engine = _build_engine_fixture(
        tmp_path
    )
    snapshot = _seed_observed_snapshot(
        config=config,
        provider=provider,
        store=store,
        feature_registry=feature_registry,
        label_registry=label_registry,
        symbol="000001",
        end_date=date(2026, 3, 31),
        row_offset=42,
        outcome=OutcomeRecord(
            snapshot_id="placeholder",
            maturity_status=MaturityStatus.RECONCILED,
            execution_fill_ratio=1.0,
            reconcile_status="ok",
            outcome_updated_at=datetime(2026, 3, 5, 15, 0, tzinfo=UTC),
        ),
    )

    result = engine.repair_backfill(
        snapshot_ids=[snapshot.snapshot_id],
        as_of=datetime(2026, 3, 31, 15, 0, tzinfo=UTC),
    )
    outcome = store.get_outcome(snapshot.snapshot_id)

    assert result["ok"] is True
    assert result["promoted_fully_matured"] == 1
    assert outcome is not None
    assert outcome.maturity_status == MaturityStatus.FULLY_MATURED
    assert outcome.backfill_fidelity_tier == BackfillFidelityTier.GOLD
    assert outcome.last_backfill_at is not None
    assert outcome.label_mature_time is not None


def test_backfill_from_runtime_history_archive_enriches_observed_snapshot_and_outcome(
    tmp_path: Path,
) -> None:
    config, provider, store, feature_registry, label_registry, engine = _build_engine_fixture(
        tmp_path
    )
    snapshot = _seed_observed_snapshot(
        config=config,
        provider=provider,
        store=store,
        feature_registry=feature_registry,
        label_registry=label_registry,
        symbol="600000",
        end_date=date(2026, 3, 31),
        row_offset=40,
        outcome=OutcomeRecord(
            snapshot_id="placeholder",
            maturity_status=MaturityStatus.LABEL_MATURED,
            label_mature_time=datetime(2026, 3, 20, 15, 0, tzinfo=UTC),
            outcome_updated_at=datetime(2026, 3, 20, 15, 0, tzinfo=UTC),
        ),
    )
    archive_payload = _build_runtime_history_archive_payload(snapshot=snapshot)

    result = engine.backfill_from_runtime_history_archive(archive_payload=archive_payload)
    enriched_snapshot = store.get_snapshot(snapshot.snapshot_id)
    outcome = store.get_outcome(snapshot.snapshot_id)

    assert result["ok"] is True
    assert result["contexts_enriched"] == 1
    assert result["execution_updates"] == 1
    assert result["command_events_linked"] == 1
    assert result["reconcile_updates"] == 1
    assert result["reconcile_promoted"] == 1
    assert result["missing_snapshot_ids"] == []
    assert enriched_snapshot is not None
    assert enriched_snapshot.risk_context["execution_pressure"] == 0.2
    assert enriched_snapshot.regime_context["runtime_regime"] == "trend"
    assert enriched_snapshot.news_context["m7_news_count"] == 3
    assert outcome is not None
    assert outcome.maturity_status == MaturityStatus.FULLY_MATURED
    assert outcome.execution_fill_ratio == 1.0
    assert outcome.realized_slippage_bp == 250.0
    assert outcome.reconcile_status == "ok"
    assert outcome.sim_vs_broker_diff == 0.0
    assert outcome.backfill_fidelity_tier == BackfillFidelityTier.GOLD
    assert outcome.backfill_source == "runtime_history_archive"


def test_backfill_from_runtime_history_archive_uses_portfolio_trades_when_command_event_missing(
    tmp_path: Path,
) -> None:
    config, provider, store, feature_registry, label_registry, engine = _build_engine_fixture(
        tmp_path
    )
    snapshot = _seed_observed_snapshot(
        config=config,
        provider=provider,
        store=store,
        feature_registry=feature_registry,
        label_registry=label_registry,
        symbol="600000",
        end_date=date(2026, 3, 31),
        row_offset=40,
        outcome=OutcomeRecord(
            snapshot_id="placeholder",
            maturity_status=MaturityStatus.LABEL_MATURED,
            label_mature_time=datetime(2026, 3, 20, 15, 0, tzinfo=UTC),
            outcome_updated_at=datetime(2026, 3, 20, 15, 0, tzinfo=UTC),
        ),
    )
    archive_payload = _build_runtime_history_archive_payload(
        snapshot=snapshot,
        entry_price=10.18,
        reference_price=10.0,
    )
    archive_payload["runtime"] = {"audit_events": []}
    archive_payload["portfolio"] = {
        "positions": [{"symbol": snapshot.symbol, "target_position": 0.2}],
        "trades": [
            {
                "trade_id": "TRD-ARCHIVE-1",
                "side": "buy",
                "symbol": snapshot.symbol,
                "strategy": snapshot.strategy,
                "target_position": 0.2,
                "timestamp": "2026-03-31T10:00:00+00:00",
                "entry_price": 10.18,
                "quantity": 1000,
            }
        ],
    }
    latest_signal = archive_payload["latest_signals"]["signals"][0]
    latest_signal["trade_plan"] = {
        "reference_price": 10.0,
    }

    result = engine.backfill_from_runtime_history_archive(archive_payload=archive_payload)
    outcome = store.get_outcome(snapshot.snapshot_id)

    assert result["ok"] is True
    assert result["execution_updates"] == 1
    assert result["command_events_linked"] == 0
    assert result["portfolio_trade_events_linked"] == 1
    assert result["reconcile_updates"] == 1
    assert outcome is not None
    assert outcome.execution_fill_ratio == 1.0
    assert outcome.realized_slippage_bp == 180.0
    assert outcome.reconcile_status == "ok"
    assert outcome.maturity_status == MaturityStatus.FULLY_MATURED


def test_backfill_from_runtime_history_archive_uses_pipeline_rejected_execution(
    tmp_path: Path,
) -> None:
    config, provider, store, feature_registry, label_registry, engine = _build_engine_fixture(
        tmp_path
    )
    snapshot = _seed_observed_snapshot(
        config=config,
        provider=provider,
        store=store,
        feature_registry=feature_registry,
        label_registry=label_registry,
        symbol="600000",
        end_date=date(2026, 3, 31),
        row_offset=40,
        outcome=OutcomeRecord(
            snapshot_id="placeholder",
            maturity_status=MaturityStatus.LABEL_MATURED,
            label_mature_time=datetime(2026, 3, 20, 15, 0, tzinfo=UTC),
            outcome_updated_at=datetime(2026, 3, 20, 15, 0, tzinfo=UTC),
        ),
    )
    archive_payload = _build_runtime_history_archive_payload(snapshot=snapshot)
    archive_payload["runtime"] = {
        "audit_events": [
            {
                "timestamp": "2026-03-31T10:00:00+00:00",
                "event_type": "pipeline_run",
                "payload": {
                    "portfolio_update": {
                        "executions": [
                            {
                                "trade_id": "SKIP-trace-600000-rejected_max_holdings",
                                "side": "buy",
                                "symbol": snapshot.symbol,
                                "strategy": snapshot.strategy,
                                "status": "rejected_max_holdings",
                                "target_position": 0.2,
                                "price": 10.18,
                                "quantity": 0,
                                "trade_time": "2026-03-31T10:00:00+00:00",
                            }
                        ]
                    }
                },
            }
        ]
    }
    archive_payload["portfolio"] = {"positions": [], "trades": []}

    result = engine.backfill_from_runtime_history_archive(archive_payload=archive_payload)
    outcome = store.get_outcome(snapshot.snapshot_id)

    assert result["ok"] is True
    assert result["execution_updates"] == 1
    assert result["command_events_linked"] == 0
    assert result["portfolio_trade_events_linked"] == 1
    assert outcome is not None
    assert outcome.execution_fill_ratio == 0.0
    assert outcome.realized_slippage_bp is None
    assert outcome.reconcile_status == "ok"
    assert outcome.maturity_status == MaturityStatus.FULLY_MATURED


def test_backfill_from_runtime_history_archive_ignores_pipeline_dry_run_execution(
    tmp_path: Path,
) -> None:
    config, provider, store, feature_registry, label_registry, engine = _build_engine_fixture(
        tmp_path
    )
    snapshot = _seed_observed_snapshot(
        config=config,
        provider=provider,
        store=store,
        feature_registry=feature_registry,
        label_registry=label_registry,
        symbol="600000",
        end_date=date(2026, 3, 31),
        row_offset=40,
        outcome=OutcomeRecord(
            snapshot_id="placeholder",
            maturity_status=MaturityStatus.LABEL_MATURED,
            label_mature_time=datetime(2026, 3, 20, 15, 0, tzinfo=UTC),
            outcome_updated_at=datetime(2026, 3, 20, 15, 0, tzinfo=UTC),
        ),
    )
    archive_payload = _build_runtime_history_archive_payload(snapshot=snapshot)
    archive_payload["runtime"] = {
        "audit_events": [
            {
                "timestamp": "2026-03-31T10:00:00+00:00",
                "event_type": "pipeline_run",
                "payload": {
                    "dry_run_execution": True,
                    "portfolio_update": {
                        "dry_run": True,
                        "executions": [
                            {
                                "trade_id": "SKIP-trace-600000-rejected_max_holdings",
                                "side": "buy",
                                "symbol": snapshot.symbol,
                                "strategy": snapshot.strategy,
                                "status": "rejected_max_holdings",
                                "target_position": 0.2,
                                "price": 10.18,
                                "quantity": 0,
                                "trade_time": "2026-03-31T10:00:00+00:00",
                            }
                        ],
                    },
                },
            }
        ]
    }
    archive_payload["portfolio"] = {"positions": [], "trades": []}

    result = engine.backfill_from_runtime_history_archive(archive_payload=archive_payload)
    outcome = store.get_outcome(snapshot.snapshot_id)

    assert result["ok"] is True
    assert result["execution_updates"] == 0
    assert result["portfolio_trade_events_linked"] == 0
    assert outcome is not None
    assert outcome.execution_fill_ratio is None


def test_backfill_from_runtime_history_archives_batches_directory_in_day_order(
    tmp_path: Path,
) -> None:
    config, provider, store, feature_registry, label_registry, engine = _build_engine_fixture(
        tmp_path
    )
    first_snapshot = _seed_observed_snapshot(
        config=config,
        provider=provider,
        store=store,
        feature_registry=feature_registry,
        label_registry=label_registry,
        symbol="600000",
        end_date=date(2026, 3, 31),
        row_offset=40,
        outcome=OutcomeRecord(
            snapshot_id="placeholder",
            maturity_status=MaturityStatus.LABEL_MATURED,
            label_mature_time=datetime(2026, 3, 20, 15, 0, tzinfo=UTC),
            outcome_updated_at=datetime(2026, 3, 20, 15, 0, tzinfo=UTC),
        ),
    )
    second_snapshot = _seed_observed_snapshot(
        config=config,
        provider=provider,
        store=store,
        feature_registry=feature_registry,
        label_registry=label_registry,
        symbol="000001",
        end_date=date(2026, 3, 31),
        row_offset=41,
        outcome=OutcomeRecord(
            snapshot_id="placeholder",
            maturity_status=MaturityStatus.LABEL_MATURED,
            label_mature_time=datetime(2026, 3, 21, 15, 0, tzinfo=UTC),
            outcome_updated_at=datetime(2026, 3, 21, 15, 0, tzinfo=UTC),
        ),
    )
    archive_dir = tmp_path / "runtime_history"
    archive_dir.mkdir(parents=True, exist_ok=True)
    first_archive = archive_dir / "runtime_history_20260330.json"
    second_archive = archive_dir / "runtime_history_20260331.json"
    first_payload = _build_runtime_history_archive_payload(
        snapshot=first_snapshot,
        recommendation_id="REC-BATCH-1",
        entry_price=10.1,
        reference_price=10.0,
    )
    first_payload["day"] = "2026-03-30"
    first_payload["generated_at"] = "2026-03-30T15:30:00+00:00"
    second_payload = _build_runtime_history_archive_payload(
        snapshot=second_snapshot,
        recommendation_id="REC-BATCH-2",
        entry_price=10.4,
        reference_price=10.0,
    )
    second_payload["day"] = "2026-03-31"
    second_payload["generated_at"] = "2026-03-31T15:30:00+00:00"
    first_archive.write_text(
        json.dumps(first_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    second_archive.write_text(
        json.dumps(second_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    result = engine.backfill_from_runtime_history_archives(archive_dir=archive_dir)
    first_outcome = store.get_outcome(first_snapshot.snapshot_id)
    second_outcome = store.get_outcome(second_snapshot.snapshot_id)

    assert result["ok"] is True
    assert result["processed_archives"] == 2
    assert result["processed_archive_days"] == ["2026-03-30", "2026-03-31"]
    assert result["contexts_enriched"] == 2
    assert result["execution_updates"] == 2
    assert result["reconcile_updates"] == 2
    assert result["reconcile_promoted"] == 2
    assert result["missing_snapshot_ids"] == []
    assert first_outcome is not None
    assert second_outcome is not None
    assert first_outcome.maturity_status == MaturityStatus.FULLY_MATURED
    assert second_outcome.maturity_status == MaturityStatus.FULLY_MATURED
    assert first_outcome.realized_slippage_bp == 100.0
    assert second_outcome.realized_slippage_bp == 400.0


def test_service_bootstrap_learning_backfill_records_audit_event(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)
    provider = SyntheticProvider(seed_offset=2602)
    object.__setattr__(service, "_provider", provider)

    payload = service.bootstrap_learning_backfill(
        symbols=["600000"],
        lookback_days=90,
        end_date=date(2026, 3, 31),
        min_history_rows=35,
    )
    events = _as_mapping(service.audit_events(limit=20, event_type="learning_backfill_bootstrap"))
    latest = _as_mapping(cast(list[object], events["events"])[-1])
    latest_payload = _as_mapping(latest["payload"])

    assert payload["ok"] is True
    assert int(events["records"]) >= 1
    assert latest["event_type"] == "learning_backfill_bootstrap"
    assert latest_payload["mode"] == "bootstrap_backfill"
    assert latest_payload["candidate_rows"] == payload["candidate_rows"]
    assert latest_payload["snapshots_inserted"] == payload["snapshots_inserted"]


def test_service_build_learning_trainable_manifest_records_audit_event(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)
    current_record, _label_record = _seed_projection_compatible_trainable_samples(
        config=config,
        store=service._sample_store,
        feature_registry=service._feature_schema_registry,
        label_registry=service._label_policy_registry,
        symbol="600000.SH",
    )

    payload = service.build_learning_trainable_manifest(symbols=["600000"])
    events = _as_mapping(service.audit_events(limit=20, event_type="learning_backfill_manifest"))
    latest = _as_mapping(cast(list[object], events["events"])[-1])
    latest_payload = _as_mapping(latest["payload"])

    assert payload["ok"] is True
    assert int(events["records"]) >= 1
    assert latest["event_type"] == "learning_backfill_manifest"
    assert latest_payload["mode"] == "build_trainable_manifest"
    assert latest_payload["dataset_manifest_id"] == payload["dataset_manifest_id"]
    assert latest_payload["feature_schema_id"] == current_record.feature_schema_id


def test_service_backfill_learning_runtime_history_archive_records_audit_event(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)
    provider = SyntheticProvider(seed_offset=2603)
    object.__setattr__(service, "_provider", provider)
    snapshot = _seed_observed_snapshot(
        config=config,
        provider=provider,
        store=service._sample_store,
        feature_registry=service._feature_schema_registry,
        label_registry=service._label_policy_registry,
        symbol="600000",
        end_date=date(2026, 3, 31),
        row_offset=40,
        outcome=OutcomeRecord(
            snapshot_id="placeholder",
            maturity_status=MaturityStatus.LABEL_MATURED,
            label_mature_time=datetime(2026, 3, 20, 15, 0, tzinfo=UTC),
            outcome_updated_at=datetime(2026, 3, 20, 15, 0, tzinfo=UTC),
        ),
    )
    archive_path = tmp_path / "runtime_history_20260331.json"
    archive_path.write_text(
        json.dumps(
            _build_runtime_history_archive_payload(snapshot=snapshot),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    payload = service.backfill_learning_runtime_history_archive(archive_path=str(archive_path))
    events = _as_mapping(
        service.audit_events(limit=20, event_type="learning_backfill_runtime_history")
    )
    latest = _as_mapping(cast(list[object], events["events"])[-1])
    latest_payload = _as_mapping(latest["payload"])
    outcome = service._sample_store.get_outcome(snapshot.snapshot_id)  # noqa: SLF001

    assert payload["ok"] is True
    assert int(events["records"]) >= 1
    assert latest["event_type"] == "learning_backfill_runtime_history"
    assert latest_payload["mode"] == "runtime_history_archive_backfill"
    assert latest_payload["archive_path"] == str(archive_path)
    assert outcome is not None
    assert outcome.maturity_status == MaturityStatus.FULLY_MATURED


def test_service_backfill_learning_runtime_history_archives_uses_configured_directory(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    archive_dir = tmp_path / "runtime_history"
    archive_dir.mkdir(parents=True, exist_ok=True)
    config.command_channel.history_archive_enabled = True
    config.command_channel.history_archive_dir = str(archive_dir)
    service = StockAnalyzerService(config=config)
    provider = SyntheticProvider(seed_offset=2604)
    object.__setattr__(service, "_provider", provider)
    snapshot = _seed_observed_snapshot(
        config=config,
        provider=provider,
        store=service._sample_store,
        feature_registry=service._feature_schema_registry,
        label_registry=service._label_policy_registry,
        symbol="600000",
        end_date=date(2026, 3, 31),
        row_offset=40,
        outcome=OutcomeRecord(
            snapshot_id="placeholder",
            maturity_status=MaturityStatus.LABEL_MATURED,
            label_mature_time=datetime(2026, 3, 20, 15, 0, tzinfo=UTC),
            outcome_updated_at=datetime(2026, 3, 20, 15, 0, tzinfo=UTC),
        ),
    )
    archive_path = archive_dir / "runtime_history_20260331.json"
    archive_path.write_text(
        json.dumps(
            _build_runtime_history_archive_payload(
                snapshot=snapshot,
                recommendation_id="REC-BATCH-SERVICE-1",
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    payload = service.backfill_learning_runtime_history_archives()
    events = _as_mapping(
        service.audit_events(limit=20, event_type="learning_backfill_runtime_history_batch")
    )
    latest = _as_mapping(cast(list[object], events["events"])[-1])
    latest_payload = _as_mapping(latest["payload"])
    outcome = service._sample_store.get_outcome(snapshot.snapshot_id)  # noqa: SLF001

    assert payload["ok"] is True
    assert payload["archive_dir"] == str(archive_dir)
    assert payload["processed_archives"] == 1
    assert int(events["records"]) >= 1
    assert latest["event_type"] == "learning_backfill_runtime_history_batch"
    assert latest_payload["mode"] == "runtime_history_archive_batch_backfill"
    assert latest_payload["archive_dir"] == str(archive_dir)
    assert outcome is not None
    assert outcome.maturity_status == MaturityStatus.FULLY_MATURED


def test_service_bootstrap_learning_from_runtime_history_builds_manifest(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    archive_dir = tmp_path / "runtime_history"
    archive_dir.mkdir(parents=True, exist_ok=True)
    config.command_channel.history_archive_enabled = True
    config.command_channel.history_archive_dir = str(archive_dir)
    service = StockAnalyzerService(config=config)
    provider = SyntheticProvider(seed_offset=2605)
    object.__setattr__(service, "_provider", provider)
    snapshot = _seed_observed_snapshot(
        config=config,
        provider=provider,
        store=service._sample_store,
        feature_registry=service._feature_schema_registry,
        label_registry=service._label_policy_registry,
        symbol="600000",
        end_date=date(2026, 3, 31),
        row_offset=40,
        outcome=OutcomeRecord(
            snapshot_id="placeholder",
            maturity_status=MaturityStatus.LABEL_MATURED,
            label_mature_time=datetime(2026, 3, 20, 15, 0, tzinfo=UTC),
            outcome_updated_at=datetime(2026, 3, 20, 15, 0, tzinfo=UTC),
            backfill_fidelity_tier=BackfillFidelityTier.GOLD,
            backfill_source="runtime_observed",
        ),
    )
    archive_path = archive_dir / "runtime_history_20260331.json"
    archive_path.write_text(
        json.dumps(
            _build_runtime_history_archive_payload(
                snapshot=snapshot,
                recommendation_id="REC-COLD-START-1",
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    payload = service.bootstrap_learning_from_runtime_history(symbols=["600000"])
    events = _as_mapping(
        service.audit_events(limit=20, event_type="learning_backfill_runtime_history_cold_start")
    )
    latest = _as_mapping(cast(list[object], events["events"])[-1])
    latest_payload = _as_mapping(latest["payload"])
    manifest = _as_mapping(payload["manifest"])

    assert payload["ok"] is True
    assert payload["processed_archives"] == 1
    assert payload["dataset_manifest_id"]
    assert int(events["records"]) >= 1
    assert latest["event_type"] == "learning_backfill_runtime_history_cold_start"
    assert latest_payload["mode"] == "learning_runtime_history_cold_start"
    assert latest_payload["dataset_manifest_id"] == payload["dataset_manifest_id"]
    assert manifest["ok"] is True


def test_service_learning_protocol_status_reports_counts_and_latest_manifest(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    archive_dir = tmp_path / "runtime_history"
    archive_dir.mkdir(parents=True, exist_ok=True)
    config.command_channel.history_archive_enabled = True
    config.command_channel.history_archive_dir = str(archive_dir)
    service = StockAnalyzerService(config=config)
    provider = SyntheticProvider(seed_offset=2606)
    object.__setattr__(service, "_provider", provider)
    snapshot = _seed_observed_snapshot(
        config=config,
        provider=provider,
        store=service._sample_store,
        feature_registry=service._feature_schema_registry,
        label_registry=service._label_policy_registry,
        symbol="600000",
        end_date=date(2026, 3, 31),
        row_offset=40,
        outcome=OutcomeRecord(
            snapshot_id="placeholder",
            maturity_status=MaturityStatus.LABEL_MATURED,
            label_mature_time=datetime(2026, 3, 20, 15, 0, tzinfo=UTC),
            outcome_updated_at=datetime(2026, 3, 20, 15, 0, tzinfo=UTC),
            backfill_fidelity_tier=BackfillFidelityTier.GOLD,
            backfill_source="runtime_observed",
        ),
    )
    archive_path = archive_dir / "runtime_history_20260331.json"
    archive_path.write_text(
        json.dumps(
            _build_runtime_history_archive_payload(
                snapshot=snapshot,
                recommendation_id="REC-STATUS-1",
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    cold_start = service.bootstrap_learning_from_runtime_history(symbols=["600000"])

    status = _as_mapping(service.learning_protocol_status(manifest_limit=3))
    sample_store = _as_mapping(status["sample_store"])
    outcomes = _as_mapping(status["outcomes"])
    manifests = _as_mapping(status["manifests"])
    latest_manifest = _as_mapping(manifests["latest"])
    latest_backfill = _as_mapping(status["latest_learning_backfill"])

    assert cold_start["ok"] is True
    assert sample_store["signal_snapshots"] >= 1
    assert sample_store["outcome_records"] >= 1
    assert sample_store["dataset_manifests"] >= 1
    assert outcomes["records"] >= 1
    assert _as_mapping(outcomes["maturity_breakdown"])["fully_matured"] >= 1
    assert manifests["records"] >= 1
    assert latest_manifest["dataset_manifest_id"] == cold_start["dataset_manifest_id"]
    assert latest_backfill["event_type"] == "learning_backfill_runtime_history_cold_start"
    assert status["cold_start_ready"] is True


def test_service_train_learning_manifest_uses_latest_manifest_without_registry_side_effects(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    config.training.min_samples = 20
    service = StockAnalyzerService(config=config)
    current_record, label_record = _seed_projection_compatible_trainable_samples(
        config=config,
        store=service._sample_store,
        feature_registry=service._feature_schema_registry,
        label_registry=service._label_policy_registry,
        symbol="600000.SH",
    )
    manifest = _as_mapping(service.build_learning_trainable_manifest(symbols=["600000"]))

    payload = _as_mapping(service.train_learning_manifest())
    audit_events = _as_mapping(
        service.audit_events(limit=20, event_type="learning_manifest_trained")
    )
    latest_event = _as_mapping(cast(list[object], audit_events["events"])[-1])
    latest_payload = _as_mapping(latest_event["payload"])
    artifact_path = Path(str(payload["artifact_path"]))
    artifact = ModelArtifact.load(artifact_path)
    model_registry = _as_mapping(service.model_registry_entries(limit=10))

    assert payload["ok"] is True
    assert payload["input_mode"] == "dataset_manifest"
    assert payload["manifest_source"] == "latest"
    assert payload["dataset_manifest_id"] == manifest["dataset_manifest_id"]
    assert payload["feature_schema_id"] == current_record.feature_schema_id
    assert payload["label_policy_id"] == label_record.label_policy_id
    assert _as_mapping(payload["model_registry"])["registered"] is False
    assert _as_mapping(payload["model_registry"])["reason"] == "registration_disabled"
    assert artifact_path.exists()
    assert artifact_path != Path(config.training.artifact_path)
    assert artifact_path.parent == tmp_path / "learning_manifest_artifacts"
    assert artifact.dataset_manifest_id == manifest["dataset_manifest_id"]
    assert artifact.feature_schema_id == current_record.feature_schema_id
    assert artifact.label_policy_id == label_record.label_policy_id
    assert int(audit_events["records"]) >= 1
    assert latest_event["event_type"] == "learning_manifest_trained"
    assert latest_payload["dataset_manifest_id"] == payload["dataset_manifest_id"]
    assert latest_payload["artifact_path"] == payload["artifact_path"]
    assert model_registry["records"] == 0
