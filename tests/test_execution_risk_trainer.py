from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from stock_analyzer.learning.execution_risk_labels import ExecutionRiskTarget
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    MaturityStatus,
    OutcomeRecord,
    SignalSnapshot,
)
from stock_analyzer.learning.sample_store import SampleStore
from stock_analyzer.models.execution_risk_predictor import ExecutionRiskPredictor
from stock_analyzer.models.execution_risk_trainer import (
    ExecutionRiskTrainer,
    ExecutionRiskTrainingConfig,
    diagnose_execution_risk_dataset,
)


def test_execution_risk_trainer_trains_multi_target_artifact_from_sample_store(
    tmp_path: Path,
) -> None:
    store = _build_execution_risk_store(tmp_path)
    trainer = ExecutionRiskTrainer(
        config=ExecutionRiskTrainingConfig(
            min_samples_per_target=20,
            calibration_ratio=0.2,
            test_ratio=0.2,
            epochs=180,
            seed=7,
        )
    )

    result = trainer.train_from_sample_store(store=store)

    assert set(result.trained_targets) >= {
        ExecutionRiskTarget.CAN_FILL.value,
        ExecutionRiskTarget.LIKELY_SLIPPAGE_HIGH.value,
        ExecutionRiskTarget.SIM_BROKER_DIVERGENCE_RISK.value,
    }
    assert result.target_row_counts[ExecutionRiskTarget.CAN_FILL.value] == 72
    assert result.target_metrics[ExecutionRiskTarget.CAN_FILL.value]["samples_total"] == 72.0
    assert result.artifact.dataset_id.startswith("execution_risk_dataset_v1_")
    assert result.artifact.trained_targets == result.trained_targets


def test_execution_risk_dataset_diagnostics_explain_single_class_targets(
    tmp_path: Path,
) -> None:
    store = SampleStore(db_path=tmp_path / "single_class.duckdb")
    base_time = datetime(2026, 1, 1, 14, 30, tzinfo=UTC)
    for index in range(30):
        snapshot = SignalSnapshot(
            snapshot_id=f"snap-single-{index:03d}",
            code_version="git:test",
            symbol="600000.SH",
            strategy="trend",
            decision_time=base_time + timedelta(days=index),
            feature_vector={"liquidity_score": 0.9},
            feature_schema_id="feature_schema_exec_v1",
            feature_schema_hash="feature_hash_exec_v1",
            runtime_config_hash="runtime_hash_exec_v1",
            label_policy_id="label_policy_exec_v1",
            label_policy_hash="label_hash_exec_v1",
        )
        store.write_snapshot(snapshot)
        store.upsert_outcome(
            OutcomeRecord(
                snapshot_id=snapshot.snapshot_id,
                maturity_status=MaturityStatus.RECONCILED,
                reconcile_status="ok",
                sim_vs_broker_diff=0.0,
            )
        )

    trainer = ExecutionRiskTrainer(config=ExecutionRiskTrainingConfig(min_samples_per_target=24))
    dataset = trainer.build_dataset_from_sample_store(
        store=store,
        maturity_statuses=["reconciled"],
    )
    diagnostics = diagnose_execution_risk_dataset(
        dataset=dataset,
        config=trainer.config,
        outcomes=store.list_outcomes(),
        labeling=trainer.labeling,
    )
    outcome_coverage = diagnostics["outcome_coverage"]

    assert diagnostics["can_train"] is False
    assert diagnostics["target_row_counts"]["reconcile_mismatch_risk"] == 30
    assert diagnostics["target_class_counts"]["reconcile_mismatch_risk"] == {
        "negative": 30,
        "positive": 0,
    }
    assert diagnostics["skipped_targets"]["reconcile_mismatch_risk"] == "single_class_target"
    assert diagnostics["skipped_targets"]["sim_broker_divergence_risk"] == "single_class_target"
    trainability = diagnostics["target_trainability"]
    assert trainability["reconcile_mismatch_risk"]["positive_deficit"] == 3
    assert trainability["reconcile_mismatch_risk"]["negative_deficit"] == 0
    assert outcome_coverage["maturity_counts"]["reconciled"] == 30
    assert outcome_coverage["requested_field_coverage"]["reconcile_status"] == 30
    assert outcome_coverage["requested_target_coverage"]["reconcile_mismatch_risk"] == 30


def test_execution_risk_dataset_diagnostics_explain_minority_class_shortage(
    tmp_path: Path,
) -> None:
    store = SampleStore(db_path=tmp_path / "minority_class.duckdb")
    base_time = datetime(2026, 1, 1, 14, 30, tzinfo=UTC)
    for index in range(29):
        snapshot = SignalSnapshot(
            snapshot_id=f"snap-minority-{index:03d}",
            code_version="git:test",
            symbol="600000.SH",
            strategy="trend",
            decision_time=base_time + timedelta(days=index),
            feature_vector={"liquidity_score": 0.9},
            feature_schema_id="feature_schema_exec_v1",
            feature_schema_hash="feature_hash_exec_v1",
            runtime_config_hash="runtime_hash_exec_v1",
            label_policy_id="label_policy_exec_v1",
            label_policy_hash="label_hash_exec_v1",
        )
        store.write_snapshot(snapshot)
        store.upsert_outcome(
            OutcomeRecord(
                snapshot_id=snapshot.snapshot_id,
                maturity_status=MaturityStatus.RECONCILED,
                realized_slippage_bp=25.0 if index < 2 else 5.0,
            )
        )

    trainer = ExecutionRiskTrainer(
        config=ExecutionRiskTrainingConfig(
            min_samples_per_target=24,
            min_class_samples_per_target=3,
        )
    )
    dataset = trainer.build_dataset_from_sample_store(
        store=store,
        maturity_statuses=["reconciled"],
    )
    diagnostics = diagnose_execution_risk_dataset(
        dataset=dataset,
        config=trainer.config,
        outcomes=store.list_outcomes(),
        labeling=trainer.labeling,
    )
    trainability = diagnostics["target_trainability"]["likely_slippage_high"]

    assert diagnostics["can_train"] is False
    assert diagnostics["target_class_counts"]["likely_slippage_high"] == {
        "negative": 27,
        "positive": 2,
    }
    assert diagnostics["skipped_targets"]["likely_slippage_high"] == "minority_class_too_small"
    assert trainability["minority_count"] == 2
    assert trainability["minority_deficit"] == 1
    assert trainability["positive_deficit"] == 1
    assert diagnostics["min_class_samples_per_target"] == 3


def test_execution_risk_trainer_stratifies_split_when_minority_lands_in_holdout(
    tmp_path: Path,
) -> None:
    store = SampleStore(db_path=tmp_path / "minority_holdout.duckdb")
    base_time = datetime(2026, 1, 1, 14, 30, tzinfo=UTC)
    for index in range(30):
        is_late_positive = index >= 27
        snapshot = SignalSnapshot(
            snapshot_id=f"snap-holdout-{index:03d}",
            code_version="git:test",
            symbol="600000.SH",
            strategy="trend",
            decision_time=base_time + timedelta(days=index),
            feature_vector={
                "liquidity_score": 0.15 if is_late_positive else 0.9,
                "volatility_score": 0.86 if is_late_positive else 0.12,
            },
            feature_schema_id="feature_schema_exec_v1",
            feature_schema_hash="feature_hash_exec_v1",
            runtime_config_hash="runtime_hash_exec_v1",
            label_policy_id="label_policy_exec_v1",
            label_policy_hash="label_hash_exec_v1",
        )
        store.write_snapshot(snapshot)
        store.upsert_outcome(
            OutcomeRecord(
                snapshot_id=snapshot.snapshot_id,
                maturity_status=MaturityStatus.RECONCILED,
                realized_slippage_bp=25.0 if is_late_positive else 5.0,
                backfill_fidelity_tier=BackfillFidelityTier.GOLD,
                backfill_source="runtime_observed",
            )
        )

    trainer = ExecutionRiskTrainer(
        config=ExecutionRiskTrainingConfig(
            min_samples_per_target=24,
            min_class_samples_per_target=3,
            calibration_ratio=0.2,
            test_ratio=0.2,
            epochs=80,
            seed=17,
        )
    )
    dataset = trainer.build_dataset_from_sample_store(
        store=store,
        maturity_statuses=["reconciled"],
    )
    diagnostics = diagnose_execution_risk_dataset(
        dataset=dataset,
        config=trainer.config,
        outcomes=store.list_outcomes(),
        labeling=trainer.labeling,
    )
    trainability = diagnostics["target_trainability"]["likely_slippage_high"]

    assert diagnostics["can_train"] is True
    assert diagnostics["skipped_targets"] == {}
    assert trainability["train_positive"] == 1
    assert trainability["train_negative"] == 17
    assert trainability["calibration_positive"] == 1
    assert trainability["calibration_negative"] == 5
    assert trainability["test_positive"] == 1
    assert trainability["test_negative"] == 5
    assert trainability["split_strategy"] == "stratified_class_balanced"

    result = trainer.train(dataset=dataset)

    assert result.trained_targets == ["likely_slippage_high"]
    assert result.target_split_strategies["likely_slippage_high"] == "stratified_class_balanced"
    assert result.target_metrics["likely_slippage_high"]["samples_train"] == 18.0


def test_execution_risk_trainer_keeps_calibration_binary_after_stratified_rescue(
    tmp_path: Path,
) -> None:
    store = SampleStore(db_path=tmp_path / "minority_calibration.duckdb")
    base_time = datetime(2026, 1, 1, 14, 30, tzinfo=UTC)
    positive_indices = {23, 28, 29}
    for index in range(30):
        is_positive = index in positive_indices
        snapshot = SignalSnapshot(
            snapshot_id=f"snap-calibration-{index:03d}",
            code_version="git:test",
            symbol="600000.SH",
            strategy="trend",
            decision_time=base_time + timedelta(days=index),
            feature_vector={
                "liquidity_score": 0.15 if is_positive else 0.9,
                "volatility_score": 0.86 if is_positive else 0.12,
            },
            feature_schema_id="feature_schema_exec_v1",
            feature_schema_hash="feature_hash_exec_v1",
            runtime_config_hash="runtime_hash_exec_v1",
            label_policy_id="label_policy_exec_v1",
            label_policy_hash="label_hash_exec_v1",
        )
        store.write_snapshot(snapshot)
        store.upsert_outcome(
            OutcomeRecord(
                snapshot_id=snapshot.snapshot_id,
                maturity_status=MaturityStatus.RECONCILED,
                realized_slippage_bp=25.0 if is_positive else 5.0,
                backfill_fidelity_tier=BackfillFidelityTier.GOLD,
                backfill_source="runtime_observed",
            )
        )

    trainer = ExecutionRiskTrainer(
        config=ExecutionRiskTrainingConfig(
            min_samples_per_target=24,
            min_class_samples_per_target=3,
            calibration_ratio=0.2,
            test_ratio=0.2,
            epochs=80,
            seed=19,
        )
    )
    dataset = trainer.build_dataset_from_sample_store(
        store=store,
        maturity_statuses=["reconciled"],
    )
    diagnostics = diagnose_execution_risk_dataset(
        dataset=dataset,
        config=trainer.config,
        outcomes=store.list_outcomes(),
        labeling=trainer.labeling,
    )
    trainability = diagnostics["target_trainability"]["likely_slippage_high"]
    split_class_counts = trainability["split_class_counts"]

    assert diagnostics["can_train"] is True
    assert diagnostics["skipped_targets"] == {}
    assert trainability["split_strategy"] == "stratified_class_balanced"
    assert split_class_counts == {
        "train": {"negative": 17, "positive": 1},
        "calibration": {"negative": 5, "positive": 1},
        "test": {"negative": 5, "positive": 1},
    }

    result = trainer.train(dataset=dataset)

    assert result.trained_targets == ["likely_slippage_high"]
    assert result.target_split_strategies["likely_slippage_high"] == "stratified_class_balanced"


def test_execution_risk_predictor_roundtrips_saved_artifact_and_scores_rows(
    tmp_path: Path,
) -> None:
    store = _build_execution_risk_store(tmp_path)
    result = ExecutionRiskTrainer(
        config=ExecutionRiskTrainingConfig(
            min_samples_per_target=20,
            calibration_ratio=0.2,
            test_ratio=0.2,
            epochs=180,
            seed=11,
        )
    ).train_from_sample_store(store=store)

    artifact_path = tmp_path / "execution_risk_artifact.json"
    result.artifact.save(artifact_path)
    predictor = ExecutionRiskPredictor.load(artifact_path)

    low_risk_features = {
        "liquidity_score": 0.95,
        "volatility_score": 0.15,
        "model_output__p_meta": 0.72,
        "risk__degraded_mode": 0.0,
        "meta__data_quality_score": 0.96,
        "meta__sample_weight": 1.0,
        "meta__decision_weekday": 2.0,
        "meta__decision_month": 3.0,
        "meta__decision_hour": 14.0,
    }
    stressed_features = {
        "liquidity_score": 0.15,
        "volatility_score": 0.92,
        "model_output__p_meta": 0.35,
        "risk__degraded_mode": 1.0,
        "meta__data_quality_score": 0.82,
        "meta__sample_weight": 1.0,
        "meta__decision_weekday": 2.0,
        "meta__decision_month": 3.0,
        "meta__decision_hour": 14.0,
    }

    low_risk = predictor.predict_features(low_risk_features)
    stressed = predictor.predict_features(stressed_features)

    assert set(low_risk.keys()) == set(result.trained_targets)
    assert all(0.0 <= value <= 1.0 for value in low_risk.values())
    assert all(0.0 <= value <= 1.0 for value in stressed.values())
    assert (
        low_risk[ExecutionRiskTarget.CAN_FILL.value]
        > stressed[ExecutionRiskTarget.CAN_FILL.value]
    )
    assert (
        low_risk[ExecutionRiskTarget.LIKELY_SLIPPAGE_HIGH.value]
        < stressed[ExecutionRiskTarget.LIKELY_SLIPPAGE_HIGH.value]
    )
    assert (
        low_risk[ExecutionRiskTarget.SIM_BROKER_DIVERGENCE_RISK.value]
        < stressed[ExecutionRiskTarget.SIM_BROKER_DIVERGENCE_RISK.value]
    )


def _build_execution_risk_store(tmp_path: Path) -> SampleStore:
    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    base_time = datetime(2026, 1, 1, 14, 30, tzinfo=UTC)

    for index in range(72):
        decision_time = base_time + timedelta(days=index)
        high_liquidity = index % 2 == 0
        stressed_market = index % 3 == 0
        liquidity_score = 0.92 if high_liquidity else 0.18
        volatility_score = 0.88 if stressed_market else 0.14
        p_meta = 0.74 if high_liquidity else 0.34
        snapshot = SignalSnapshot(
            snapshot_id=f"snap-{index:03d}",
            code_version="git:test",
            symbol="600000.SH" if high_liquidity else "000001.SZ",
            strategy="trend",
            decision_time=decision_time,
            feature_vector={
                "liquidity_score": liquidity_score,
                "volatility_score": volatility_score,
            },
            feature_schema_id="feature_schema_exec_v1",
            feature_schema_hash="feature_hash_exec_v1",
            model_outputs={"p_meta": p_meta},
            risk_context={"degraded_mode": stressed_market},
            runtime_config_hash="runtime_hash_exec_v1",
            label_policy_id="label_policy_exec_v1",
            label_policy_hash="label_hash_exec_v1",
            data_quality_score=0.96 if high_liquidity else 0.84,
            sample_weight=1.0,
        )
        outcome = OutcomeRecord(
            snapshot_id=snapshot.snapshot_id,
            maturity_status=MaturityStatus.FULLY_MATURED,
            label_mature_time=decision_time + timedelta(days=5),
            execution_fill_ratio=0.98 if high_liquidity else 0.74,
            realized_slippage_bp=17.0 if stressed_market else 6.0,
            reconcile_status=(
                "mismatch"
                if stressed_market and not high_liquidity
                else "ok"
            ),
            sim_vs_broker_diff=0.032 if stressed_market and not high_liquidity else 0.004,
            backfill_fidelity_tier=BackfillFidelityTier.GOLD,
            backfill_source="runtime_observed",
        )
        store.write_snapshot(snapshot)
        store.upsert_outcome(outcome)
    return store
