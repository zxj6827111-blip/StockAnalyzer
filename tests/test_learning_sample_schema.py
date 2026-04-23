from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    DatasetManifest,
    DatasetSplitPlanEntry,
    FeatureCaptureMode,
    MaturityStatus,
    OutcomeRecord,
    SignalSnapshot,
)


def test_signal_snapshot_validates_core_contract_fields() -> None:
    snapshot = SignalSnapshot(
        snapshot_id="snap-001",
        code_version="git:abc123",
        symbol="600000.SH",
        strategy="trend",
        decision_time=datetime(2026, 3, 25, 14, 50, tzinfo=UTC),
        feature_vector={"ret_1d": 0.01, "atr14": 0.4},
        feature_schema_id="feature_schema_v1_abc",
        feature_schema_hash="abc",
        feature_capture_mode=FeatureCaptureMode.OBSERVED_SNAPSHOT,
        runtime_config_hash="runtime_hash_1",
        label_policy_id="label_policy_v1_abc",
        label_policy_hash="label_hash_1",
        data_quality_score=0.92,
        sample_weight=1.5,
    )

    assert snapshot.feature_capture_mode == FeatureCaptureMode.OBSERVED_SNAPSHOT
    assert snapshot.feature_vector["ret_1d"] == 0.01
    assert snapshot.sample_weight == 1.5


def test_signal_snapshot_rejects_invalid_scores_and_empty_feature_vector() -> None:
    with pytest.raises(ValueError, match="feature_vector must not be empty"):
        SignalSnapshot(
            snapshot_id="snap-001",
            code_version="git:abc123",
            symbol="600000.SH",
            strategy="trend",
            decision_time=datetime(2026, 3, 25, 14, 50, tzinfo=UTC),
            feature_vector={},
            feature_schema_id="feature_schema_v1_abc",
            feature_schema_hash="abc",
            runtime_config_hash="runtime_hash_1",
            label_policy_id="label_policy_v1_abc",
            label_policy_hash="label_hash_1",
        )

    with pytest.raises(ValueError, match="data_quality_score must be between 0 and 1"):
        SignalSnapshot(
            snapshot_id="snap-002",
            code_version="git:abc123",
            symbol="600000.SH",
            strategy="trend",
            decision_time=datetime(2026, 3, 25, 14, 50, tzinfo=UTC),
            feature_vector={"ret_1d": 0.01},
            feature_schema_id="feature_schema_v1_abc",
            feature_schema_hash="abc",
            runtime_config_hash="runtime_hash_1",
            label_policy_id="label_policy_v1_abc",
            label_policy_hash="label_hash_1",
            data_quality_score=1.2,
        )


def test_outcome_record_supports_maturity_and_fidelity_contract() -> None:
    outcome = OutcomeRecord(
        snapshot_id="snap-001",
        maturity_status=MaturityStatus.RECONCILED,
        execution_fill_ratio=0.85,
        backfill_fidelity_tier=BackfillFidelityTier.SILVER,
        backfill_source="week7_sim_broker",
    )

    assert outcome.maturity_status == MaturityStatus.RECONCILED
    assert outcome.backfill_fidelity_tier == BackfillFidelityTier.SILVER
    assert outcome.execution_fill_ratio == 0.85


def test_dataset_manifest_carries_split_plan_and_fidelity_filter() -> None:
    manifest = DatasetManifest(
        dataset_manifest_id="manifest-001",
        source_store_version="learning_store_v1",
        feature_schema_id="feature_schema_v1_abc",
        feature_schema_hash="feature_hash_1",
        label_policy_id="label_policy_v1_abc",
        label_policy_hash="label_hash_1",
        sample_selection_rule="maturity_status = fully_matured",
        fidelity_filter=[BackfillFidelityTier.GOLD, BackfillFidelityTier.SILVER],
        included_snapshot_count=120,
        included_outcome_count=118,
        split_plan=[
            DatasetSplitPlanEntry(
                split_name="train",
                selector="date < 2026-03-01",
                row_count=80,
            ),
            DatasetSplitPlanEntry(
                split_name="test",
                selector="date >= 2026-03-01",
                row_count=40,
            ),
        ],
    )

    assert manifest.fidelity_filter == [
        BackfillFidelityTier.GOLD,
        BackfillFidelityTier.SILVER,
    ]
    assert manifest.split_plan[0].split_name == "train"
    assert manifest.included_snapshot_count == 120


def test_sample_schema_models_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        DatasetManifest(
            dataset_manifest_id="manifest-001",
            source_store_version="learning_store_v1",
            feature_schema_id="feature_schema_v1_abc",
            feature_schema_hash="feature_hash_1",
            label_policy_id="label_policy_v1_abc",
            label_policy_hash="label_hash_1",
            sample_selection_rule="all",
            unknown_field="boom",
        )
