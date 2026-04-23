from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from stock_analyzer.learning.execution_risk_labels import (
    ExecutionRiskLabelBuilder,
    ExecutionRiskTarget,
)
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    MaturityStatus,
    OutcomeRecord,
    SignalSnapshot,
)
from stock_analyzer.learning.sample_store import SampleStore


def test_execution_risk_label_builder_derives_targets_and_flattens_features(
    tmp_path: Path,
) -> None:
    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    base_time = datetime(2026, 3, 1, 14, 30, tzinfo=UTC)

    _write_snapshot_and_outcome(
        store=store,
        snapshot=SignalSnapshot(
            snapshot_id="snap-001",
            code_version="git:test",
            symbol="600000.SH",
            strategy="trend",
            decision_time=base_time,
            feature_vector={"turnover_ratio": 1.5, "volume_log": 0.4},
            feature_schema_id="feature_schema_v1",
            feature_schema_hash="feature_hash_v1",
            model_outputs={"p_meta": 0.62, "p_lgbm": 0.58},
            score_breakdown={"news": 0.55},
            risk_context={"can_open_new_position": True, "degraded_mode": False},
            news_context={"news_component": 0.7},
            regime_context={"volatility_bucket": 2},
            runtime_config_hash="runtime_hash_v1",
            label_policy_id="label_policy_v1",
            label_policy_hash="label_hash_v1",
            data_quality_score=0.96,
            sample_weight=1.1,
        ),
        outcome=OutcomeRecord(
            snapshot_id="snap-001",
            maturity_status=MaturityStatus.FULLY_MATURED,
            label_mature_time=base_time + timedelta(days=5),
            execution_fill_ratio=1.0,
            realized_slippage_bp=8.0,
            reconcile_status="ok",
            sim_vs_broker_diff=0.005,
            backfill_fidelity_tier=BackfillFidelityTier.GOLD,
            backfill_source="runtime_observed",
        ),
    )
    _write_snapshot_and_outcome(
        store=store,
        snapshot=SignalSnapshot(
            snapshot_id="snap-002",
            code_version="git:test",
            symbol="000001.SZ",
            strategy="trend",
            decision_time=base_time + timedelta(days=1),
            feature_vector={"turnover_ratio": 0.9, "volume_log": 0.2},
            feature_schema_id="feature_schema_v1",
            feature_schema_hash="feature_hash_v1",
            model_outputs={"p_meta": 0.48, "p_lgbm": 0.44},
            score_breakdown={"news": 0.41},
            risk_context={"can_open_new_position": False, "degraded_mode": True},
            news_context={"news_component": 0.3},
            regime_context={"volatility_bucket": 3},
            runtime_config_hash="runtime_hash_v1",
            label_policy_id="label_policy_v1",
            label_policy_hash="label_hash_v1",
            data_quality_score=0.9,
            sample_weight=0.95,
        ),
        outcome=OutcomeRecord(
            snapshot_id="snap-002",
            maturity_status=MaturityStatus.FULLY_MATURED,
            label_mature_time=base_time + timedelta(days=6),
            execution_fill_ratio=0.82,
            realized_slippage_bp=18.0,
            reconcile_status="mismatch",
            sim_vs_broker_diff=0.03,
            backfill_fidelity_tier=BackfillFidelityTier.SILVER,
            backfill_source="repair_backfill",
        ),
    )

    dataset = ExecutionRiskLabelBuilder(store=store).build_dataset()

    assert dataset.row_count == 2
    assert dataset.source_snapshot_count == 2
    assert dataset.target_coverage == {
        ExecutionRiskTarget.CAN_FILL.value: 2,
        ExecutionRiskTarget.LIKELY_SLIPPAGE_HIGH.value: 2,
        ExecutionRiskTarget.RECONCILE_MISMATCH_RISK.value: 2,
        ExecutionRiskTarget.SIM_BROKER_DIVERGENCE_RISK.value: 2,
    }
    assert "model_output__p_meta" in dataset.feature_names
    assert "score__news" in dataset.feature_names
    assert "risk__can_open_new_position" in dataset.feature_names

    first_row = dataset.rows[0]
    second_row = dataset.rows[1]

    assert first_row.targets == {
        ExecutionRiskTarget.CAN_FILL.value: 1.0,
        ExecutionRiskTarget.LIKELY_SLIPPAGE_HIGH.value: 0.0,
        ExecutionRiskTarget.RECONCILE_MISMATCH_RISK.value: 0.0,
        ExecutionRiskTarget.SIM_BROKER_DIVERGENCE_RISK.value: 0.0,
    }
    assert first_row.feature_vector["model_output__p_meta"] == 0.62
    assert first_row.feature_vector["score__news"] == 0.55
    assert first_row.feature_vector["risk__can_open_new_position"] == 1.0
    assert first_row.feature_vector["meta__decision_weekday"] == float(base_time.weekday())

    assert second_row.targets == {
        ExecutionRiskTarget.CAN_FILL.value: 0.0,
        ExecutionRiskTarget.LIKELY_SLIPPAGE_HIGH.value: 1.0,
        ExecutionRiskTarget.RECONCILE_MISMATCH_RISK.value: 1.0,
        ExecutionRiskTarget.SIM_BROKER_DIVERGENCE_RISK.value: 1.0,
    }
    assert second_row.backfill_fidelity_tier == BackfillFidelityTier.SILVER.value


def test_execution_risk_label_builder_skips_rows_without_required_maturity_or_targets(
    tmp_path: Path,
) -> None:
    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    base_time = datetime(2026, 3, 1, 14, 30, tzinfo=UTC)

    _write_snapshot_and_outcome(
        store=store,
        snapshot=_make_snapshot(snapshot_id="snap-101", decision_time=base_time),
        outcome=OutcomeRecord(
            snapshot_id="snap-101",
            maturity_status=MaturityStatus.RECONCILED,
            execution_fill_ratio=1.0,
            realized_slippage_bp=9.0,
            reconcile_status="ok",
            sim_vs_broker_diff=0.0,
        ),
    )
    _write_snapshot_and_outcome(
        store=store,
        snapshot=_make_snapshot(
            snapshot_id="snap-102",
            decision_time=base_time + timedelta(days=1),
        ),
        outcome=OutcomeRecord(
            snapshot_id="snap-102",
            maturity_status=MaturityStatus.FULLY_MATURED,
            backfill_fidelity_tier=BackfillFidelityTier.GOLD,
            backfill_source="runtime_observed",
        ),
    )

    builder = ExecutionRiskLabelBuilder(store=store)
    dataset = builder.build_dataset()

    assert dataset.row_count == 0
    assert dataset.skipped_by_maturity == 1
    assert dataset.skipped_missing_targets == 1

    reconciled_dataset = builder.build_dataset(
        maturity_statuses=[MaturityStatus.RECONCILED],
    )
    assert reconciled_dataset.row_count == 1
    assert reconciled_dataset.rows_for_target(ExecutionRiskTarget.CAN_FILL)
    assert reconciled_dataset.rows[0].targets[ExecutionRiskTarget.CAN_FILL.value] == 1.0


def _write_snapshot_and_outcome(
    *,
    store: SampleStore,
    snapshot: SignalSnapshot,
    outcome: OutcomeRecord,
) -> None:
    store.write_snapshot(snapshot)
    store.upsert_outcome(outcome)


def _make_snapshot(*, snapshot_id: str, decision_time: datetime) -> SignalSnapshot:
    return SignalSnapshot(
        snapshot_id=snapshot_id,
        code_version="git:test",
        symbol="600000.SH",
        strategy="trend",
        decision_time=decision_time,
        feature_vector={"turnover_ratio": 1.2, "volume_log": 0.3},
        feature_schema_id="feature_schema_v1",
        feature_schema_hash="feature_hash_v1",
        model_outputs={"p_meta": 0.61},
        score_breakdown={"news": 0.52},
        risk_context={"can_open_new_position": True},
        news_context={"news_component": 0.65},
        regime_context={"volatility_bucket": 2},
        runtime_config_hash="runtime_hash_v1",
        label_policy_id="label_policy_v1",
        label_policy_hash="label_hash_v1",
        data_quality_score=0.94,
        sample_weight=1.0,
    )
