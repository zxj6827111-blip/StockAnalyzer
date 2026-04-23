from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from stock_analyzer.learning.feedback_features import merge_feedback_feature_vector
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    DatasetManifest,
    DatasetSplitPlanEntry,
    FeatureCaptureMode,
    MaturityStatus,
    OutcomeRecord,
    SignalSnapshot,
)
from stock_analyzer.learning.sample_store import SampleStore


def test_sample_store_round_trips_snapshot_outcome_and_manifest(tmp_path: Path) -> None:
    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    snapshot = SignalSnapshot(
        snapshot_id="snap-001",
        code_version="git:abc123",
        symbol="600000.SH",
        strategy="trend",
        decision_time=datetime(2026, 3, 25, 14, 50, tzinfo=UTC),
        feature_vector={"ret_1d": 0.01, "atr14": 0.4},
        feature_schema_id="feature_schema_v1_abc",
        feature_schema_hash="feature_hash_1",
        feature_capture_mode=FeatureCaptureMode.OBSERVED_SNAPSHOT,
        runtime_config_hash="runtime_hash_1",
        label_policy_id="label_policy_v1_abc",
        label_policy_hash="label_hash_1",
        data_quality_score=0.92,
    )
    outcome = OutcomeRecord(
        snapshot_id="snap-001",
        maturity_status=MaturityStatus.LABEL_MATURED,
        label_mature_time=datetime(2026, 3, 31, 15, 0, tzinfo=UTC),
        realized_return=0.07,
        backfill_fidelity_tier=BackfillFidelityTier.GOLD,
        backfill_source="runtime_observed",
    )
    manifest = DatasetManifest(
        dataset_manifest_id="manifest-001",
        source_store_version="learning_store_v1",
        feature_schema_id="feature_schema_v1_abc",
        feature_schema_hash="feature_hash_1",
        label_policy_id="label_policy_v1_abc",
        label_policy_hash="label_hash_1",
        sample_selection_rule="maturity_status in ('label_matured','reconciled','fully_matured')",
        fidelity_filter=[BackfillFidelityTier.GOLD],
        included_snapshot_count=1,
        included_outcome_count=1,
        split_plan=[DatasetSplitPlanEntry(split_name="train", row_count=1)],
    )

    store.write_snapshot(snapshot)
    store.upsert_outcome(outcome)
    store.write_manifest(manifest)

    loaded_snapshot = store.get_snapshot("snap-001")
    loaded_outcome = store.get_outcome("snap-001")
    loaded_manifest = store.get_manifest("manifest-001")

    assert loaded_snapshot is not None
    assert loaded_outcome is not None
    assert loaded_manifest is not None
    assert loaded_snapshot.model_dump() == snapshot.model_dump()
    assert loaded_outcome.model_dump() == outcome.model_dump()
    assert loaded_manifest.model_dump() == manifest.model_dump()
    assert store.counts() == {
        "signal_snapshots": 1,
        "outcome_records": 1,
        "dataset_manifests": 1,
    }


def test_sample_store_rejects_duplicate_snapshot_ids(tmp_path: Path) -> None:
    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    snapshot = SignalSnapshot(
        snapshot_id="snap-dup",
        code_version="git:abc123",
        symbol="600000.SH",
        strategy="trend",
        decision_time=datetime(2026, 3, 25, 14, 50, tzinfo=UTC),
        feature_vector={"ret_1d": 0.01},
        feature_schema_id="feature_schema_v1_abc",
        feature_schema_hash="feature_hash_1",
        runtime_config_hash="runtime_hash_1",
        label_policy_id="label_policy_v1_abc",
        label_policy_hash="label_hash_1",
    )

    store.write_snapshot(snapshot)
    with pytest.raises(ValueError, match="snapshot_id already exists"):
        store.write_snapshot(snapshot)


def test_sample_store_upsert_outcome_replaces_previous_state(tmp_path: Path) -> None:
    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    first = OutcomeRecord(
        snapshot_id="snap-001",
        maturity_status=MaturityStatus.PENDING,
    )
    second = OutcomeRecord(
        snapshot_id="snap-001",
        maturity_status=MaturityStatus.FULLY_MATURED,
        execution_fill_ratio=1.0,
        backfill_fidelity_tier=BackfillFidelityTier.SILVER,
    )

    store.upsert_outcome(first)
    store.upsert_outcome(second)

    loaded = store.get_outcome("snap-001")
    assert loaded is not None
    assert loaded.maturity_status == MaturityStatus.FULLY_MATURED
    assert loaded.execution_fill_ratio == 1.0
    assert store.counts()["outcome_records"] == 1


def test_sample_store_rejects_duplicate_manifest_ids(tmp_path: Path) -> None:
    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    manifest = DatasetManifest(
        dataset_manifest_id="manifest-dup",
        source_store_version="learning_store_v1",
        feature_schema_id="feature_schema_v1_abc",
        feature_schema_hash="feature_hash_1",
        label_policy_id="label_policy_v1_abc",
        label_policy_hash="label_hash_1",
        sample_selection_rule="all",
    )

    store.write_manifest(manifest)
    with pytest.raises(ValueError, match="dataset_manifest_id already exists"):
        store.write_manifest(manifest)


def test_sample_store_enriches_snapshot_contexts_without_overwriting_existing_values(
    tmp_path: Path,
) -> None:
    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    snapshot = SignalSnapshot(
        snapshot_id="snap-enrich",
        code_version="git:abc123",
        symbol="600000.SH",
        strategy="trend",
        decision_time=datetime(2026, 3, 25, 14, 50, tzinfo=UTC),
        feature_vector=merge_feedback_feature_vector(
            {"ret_1d": 0.01},
            add_missing_columns=True,
        ),
        feature_schema_id="feature_schema_v1_abc",
        feature_schema_hash="feature_hash_1",
        runtime_config_hash="runtime_hash_1",
        label_policy_id="label_policy_v1_abc",
        label_policy_hash="label_hash_1",
        risk_context={"can_open_new_position": True},
        news_context={"news_component": 0.62},
        regime_context={"runtime_regime": "neutral"},
    )
    store.write_snapshot(snapshot)

    updated = store.enrich_snapshot_contexts(
        "snap-enrich",
        risk_context={
            "m1_negative_case_bucket": "severe",
            "m1_negative_case_similarity": 0.84,
        },
        news_context={"m7_effectiveness_score": 0.73},
        regime_context={"m3_match_score": 0.91},
    )

    assert updated is not None
    assert updated.risk_context["can_open_new_position"] is True
    assert updated.risk_context["m1_negative_case_bucket"] == "severe"
    assert updated.news_context["news_component"] == 0.62
    assert updated.news_context["m7_effectiveness_score"] == 0.73
    assert updated.regime_context["runtime_regime"] == "neutral"
    assert updated.regime_context["m3_match_score"] == 0.91
    assert updated.feature_vector["lp_m1_negative_case_bucket_severe"] == 1.0
    assert updated.feature_vector["lp_m1_negative_case_similarity"] == 0.84
    assert updated.feature_vector["lp_m3_match_score"] == 0.91
    assert updated.feature_vector["lp_m7_effectiveness_score"] == 0.73

    reloaded = store.get_snapshot("snap-enrich")
    assert reloaded is not None
    assert reloaded.model_dump() == updated.model_dump()
