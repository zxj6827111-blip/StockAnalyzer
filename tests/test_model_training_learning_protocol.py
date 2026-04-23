from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from stock_analyzer.config import load_config
from stock_analyzer.learning.dataset_manifest import DatasetManifestBuilder
from stock_analyzer.learning.feedback_features import (
    LEARNING_PROTOCOL_FEEDBACK_FEATURE_COLUMNS,
    merge_feedback_feature_vector,
)
from stock_analyzer.learning.feature_schema_registry import FeatureSchemaRegistry
from stock_analyzer.learning.label_policy_registry import LabelPolicyRegistry
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    MaturityStatus,
    OutcomeRecord,
    SignalSnapshot,
)
from stock_analyzer.learning.sample_store import SampleStore
from stock_analyzer.models.trainer import ModelTrainer


def test_model_trainer_trains_directly_from_dataset_manifest(tmp_path: Path) -> None:
    (
        config,
        store,
        feature_registry,
        label_registry,
        feature_record,
        label_record,
    ) = _build_learning_protocol_fixture(tmp_path)
    manifest = DatasetManifestBuilder(store=store).create_manifest(
        feature_schema_id=feature_record.feature_schema_id,
        feature_schema_hash=feature_record.feature_schema_hash,
        label_policy_id=label_record.label_policy_id,
        label_policy_hash=label_record.label_policy_hash,
        fidelity_filter=[BackfillFidelityTier.GOLD],
        calibration_ratio=config.training.calibration_ratio,
        test_ratio=config.training.test_ratio,
    )
    trainer = ModelTrainer(
        training=config.training,
        labels=config.labels,
        models=config.models,
    )

    result = trainer.train_on_dataset_manifest(
        store=store,
        dataset_manifest=manifest,
        feature_schema_registry=feature_registry,
        label_policy_registry=label_registry,
    )
    artifact_path = tmp_path / "manifest_trained_artifact.json"
    result.artifact.save(artifact_path)
    reloaded = result.artifact.load(artifact_path)

    assert result.samples_total == 60
    assert result.samples_embargo == 0
    assert result.artifact.feature_columns[:2] == ["feature_b", "feature_a"]
    assert result.artifact.feature_columns[-1] == "__random_baseline__"
    assert result.artifact.feature_schema_id == feature_record.feature_schema_id
    assert result.artifact.label_policy_id == label_record.label_policy_id
    assert result.artifact.dataset_manifest_id == manifest.dataset_manifest_id
    assert result.artifact.metadata["dataset_split_strategy"] == "manifest"
    assert reloaded.dataset_manifest_id == manifest.dataset_manifest_id
    assert reloaded.feature_schema_hash == feature_record.feature_schema_hash


def test_model_trainer_trains_directly_from_sample_store(tmp_path: Path) -> None:
    (
        config,
        store,
        feature_registry,
        label_registry,
        feature_record,
        label_record,
    ) = _build_learning_protocol_fixture(tmp_path)
    trainer = ModelTrainer(
        training=config.training,
        labels=config.labels,
        models=config.models,
    )

    result = trainer.train_on_sample_store(
        store=store,
        feature_schema_id=feature_record.feature_schema_id,
        feature_schema_hash=feature_record.feature_schema_hash,
        label_policy_id=label_record.label_policy_id,
        label_policy_hash=label_record.label_policy_hash,
        fidelity_filter=[BackfillFidelityTier.GOLD],
        feature_schema_registry=feature_registry,
        label_policy_registry=label_registry,
    )

    assert result.samples_total == 60
    assert result.samples_train > result.samples_test
    assert result.artifact.feature_schema_id == feature_record.feature_schema_id
    assert result.artifact.label_policy_hash == label_record.label_policy_hash
    assert str(result.artifact.dataset_manifest_id).startswith("dataset_manifest_v1_")
    assert result.artifact.metadata["dataset_split_strategy"] == "manifest"


def test_model_trainer_records_feedback_weighting_metadata(tmp_path: Path) -> None:
    (
        config,
        store,
        feature_registry,
        label_registry,
        feature_record,
        label_record,
    ) = _build_learning_protocol_fixture(tmp_path, include_feedback_context=True)
    trainer = ModelTrainer(
        training=config.training,
        labels=config.labels,
        models=config.models,
    )

    result = trainer.train_on_sample_store(
        store=store,
        feature_schema_id=feature_record.feature_schema_id,
        feature_schema_hash=feature_record.feature_schema_hash,
        label_policy_id=label_record.label_policy_id,
        label_policy_hash=label_record.label_policy_hash,
        fidelity_filter=[BackfillFidelityTier.GOLD],
        feature_schema_registry=feature_registry,
        label_policy_registry=label_registry,
    )

    weighting = result.artifact.metadata["sample_weighting"]
    assert weighting["row_count"] == 60
    assert weighting["applied_rows"] > 0
    assert weighting["module_active_rows"]["M1"] > 0
    assert weighting["module_active_rows"]["M3"] > 0
    assert weighting["module_active_rows"]["M7"] > 0
    assert result.metrics["train_sample_weight_max"] > 1.0
    assert "lp_m3_match_score" in result.artifact.feature_columns
    assert "lp_m7_effectiveness_score" in result.artifact.feature_columns


def test_model_trainer_uses_projection_compatible_legacy_snapshots_from_sample_store(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.training.min_samples = 20
    config.training.validation_ratio = 0.2
    config.training.calibration_ratio = 0.1
    config.training.test_ratio = 0.1

    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    feature_registry = FeatureSchemaRegistry(db_path=tmp_path / "feature_schema.duckdb")
    label_registry = LabelPolicyRegistry(db_path=tmp_path / "label_policy.duckdb")
    legacy_record = feature_registry.register_feature_names(
        feature_names=["feature_b", "feature_a"],
        feature_schema_id="feature_schema_legacy",
        feature_engineer_version="test",
        code_version="git:test",
    )
    current_record = feature_registry.register_feature_names(
        feature_names=["feature_b", "feature_a", "feature_c"],
        feature_schema_id="feature_schema_current",
        feature_engineer_version="test",
        code_version="git:test",
        projection_compatible_from=[legacy_record.feature_schema_id],
    )
    label_record = label_registry.register_from_config(
        config.labels,
        label_policy_id="label_policy_v1_projection",
    )

    base_time = datetime(2026, 1, 1, 14, 30, tzinfo=UTC)
    for index in range(16):
        decision_time = base_time + timedelta(days=index)
        store.write_snapshot(
            SignalSnapshot(
                snapshot_id=f"legacy-snap-{index:03d}",
                code_version="git:test",
                symbol="600000.SH",
                strategy="trend",
                decision_time=decision_time,
                feature_vector={
                    "feature_b": float(index % 7),
                    "feature_a": float((index % 3) / 3.0),
                },
                feature_schema_id=legacy_record.feature_schema_id,
                feature_schema_hash=legacy_record.feature_schema_hash,
                runtime_config_hash="runtime_hash_1",
                label_policy_id=label_record.label_policy_id,
                label_policy_hash=label_record.label_policy_hash,
            )
        )
        store.upsert_outcome(
            OutcomeRecord(
                snapshot_id=f"legacy-snap-{index:03d}",
                maturity_status=MaturityStatus.RECONCILED,
                label_mature_time=decision_time + timedelta(days=7),
                realized_return=0.08 if index % 2 == 0 else -0.06,
                max_favorable_excursion=0.08 if index % 2 == 0 else 0.01,
                max_adverse_excursion=-0.01 if index % 2 == 0 else -0.08,
                backfill_fidelity_tier=BackfillFidelityTier.GOLD,
                backfill_source="runtime_observed",
            )
        )
    for index in range(8):
        decision_time = base_time + timedelta(days=30 + index)
        store.write_snapshot(
            SignalSnapshot(
                snapshot_id=f"current-snap-{index:03d}",
                code_version="git:test",
                symbol="600000.SH",
                strategy="trend",
                decision_time=decision_time,
                feature_vector={
                    "feature_b": float((index + 2) % 7),
                    "feature_a": float((index % 4) / 4.0),
                    "feature_c": float(index) / 10.0,
                },
                feature_schema_id=current_record.feature_schema_id,
                feature_schema_hash=current_record.feature_schema_hash,
                runtime_config_hash="runtime_hash_1",
                label_policy_id=label_record.label_policy_id,
                label_policy_hash=label_record.label_policy_hash,
            )
        )
        store.upsert_outcome(
            OutcomeRecord(
                snapshot_id=f"current-snap-{index:03d}",
                maturity_status=MaturityStatus.RECONCILED,
                label_mature_time=decision_time + timedelta(days=7),
                realized_return=0.09 if index % 2 == 0 else -0.04,
                max_favorable_excursion=0.10 if index % 2 == 0 else 0.02,
                max_adverse_excursion=-0.02 if index % 2 == 0 else -0.07,
                backfill_fidelity_tier=BackfillFidelityTier.GOLD,
                backfill_source="runtime_observed",
            )
        )

    trainer = ModelTrainer(
        training=config.training,
        labels=config.labels,
        models=config.models,
    )
    result = trainer.train_on_sample_store(
        store=store,
        feature_schema_id=current_record.feature_schema_id,
        feature_schema_hash=current_record.feature_schema_hash,
        label_policy_id=label_record.label_policy_id,
        label_policy_hash=label_record.label_policy_hash,
        fidelity_filter=[BackfillFidelityTier.GOLD],
        feature_schema_registry=feature_registry,
        label_policy_registry=label_registry,
    )

    manifest_snapshot_ids = store.list_manifest_snapshot_ids(result.artifact.dataset_manifest_id)

    assert result.samples_total == 24
    assert result.artifact.feature_schema_id == current_record.feature_schema_id
    assert result.artifact.feature_columns[:3] == ["feature_b", "feature_a", "feature_c"]
    assert "legacy-snap-000" in manifest_snapshot_ids
    assert "current-snap-000" in manifest_snapshot_ids


def _build_learning_protocol_fixture(
    tmp_path: Path,
    include_feedback_context: bool = False,
) -> tuple[
    object,
    SampleStore,
    FeatureSchemaRegistry,
    LabelPolicyRegistry,
    object,
    object,
]:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.training.min_samples = 20
    config.training.validation_ratio = 0.2
    config.training.calibration_ratio = 0.1
    config.training.test_ratio = 0.1

    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    feature_registry = FeatureSchemaRegistry(db_path=tmp_path / "feature_schema.duckdb")
    label_registry = LabelPolicyRegistry(db_path=tmp_path / "label_policy.duckdb")
    feature_record = feature_registry.register_feature_names(
        feature_names=[
            "feature_b",
            "feature_a",
            *LEARNING_PROTOCOL_FEEDBACK_FEATURE_COLUMNS,
        ],
        feature_schema_id="feature_schema_v1_111827cbef02",
        feature_engineer_version="test",
        code_version="git:test",
    )
    label_record = label_registry.register_from_config(
        config.labels,
        label_policy_id="label_policy_v1_6545f8448169",
    )

    base_time = datetime(2026, 1, 1, 14, 30, tzinfo=UTC)
    for index in range(60):
        snapshot = SignalSnapshot(
            snapshot_id=f"snap-{index:03d}",
            code_version="git:test",
            symbol="600000.SH",
            strategy="trend",
            decision_time=base_time + timedelta(days=index),
            feature_vector=merge_feedback_feature_vector(
                {
                    "feature_b": float(index % 7),
                    "feature_a": float((index % 3) / 3.0),
                },
                risk_context=(
                    {
                        "m1_negative_case_bucket": "severe" if index % 10 == 0 else "medium",
                        "m1_reason_codes": ["chase_high"] if index % 10 == 0 else [],
                    }
                    if include_feedback_context and index % 5 == 0
                    else {}
                ),
                news_context=(
                    {
                        "news_component": 0.9 if index % 2 == 0 else 0.1,
                        "m7_effectiveness_score": 0.8 if index % 4 == 0 else 0.6,
                        "m7_source_reliability": 0.75,
                    }
                    if include_feedback_context
                    else {}
                ),
                regime_context=(
                    {"m3_match_score": 0.7}
                    if include_feedback_context and index % 3 == 0
                    else {}
                ),
                add_missing_columns=True,
            ),
            feature_schema_id=feature_record.feature_schema_id,
            feature_schema_hash=feature_record.feature_schema_hash,
            runtime_config_hash="runtime_hash_1",
            label_policy_id=label_record.label_policy_id,
            label_policy_hash=label_record.label_policy_hash,
            risk_context=(
                {
                    "m1_negative_case_bucket": "severe" if index % 10 == 0 else "medium",
                    "m1_reason_codes": ["chase_high"] if index % 10 == 0 else [],
                }
                if include_feedback_context and index % 5 == 0
                else {}
            ),
            news_context=(
                {
                    "news_component": 0.9 if index % 2 == 0 else 0.1,
                    "m7_effectiveness_score": 0.8 if index % 4 == 0 else 0.6,
                    "m7_source_reliability": 0.75,
                }
                if include_feedback_context
                else {}
            ),
            regime_context=(
                {"m3_match_score": 0.7}
                if include_feedback_context and index % 3 == 0
                else {}
            ),
        )
        outcome = OutcomeRecord(
            snapshot_id=snapshot.snapshot_id,
            maturity_status=MaturityStatus.RECONCILED,
            label_mature_time=snapshot.decision_time + timedelta(days=7),
            realized_return=0.08 if index % 2 == 0 else -0.06,
            max_favorable_excursion=0.08 if index % 2 == 0 else 0.01,
            max_adverse_excursion=-0.01 if index % 2 == 0 else -0.08,
            backfill_fidelity_tier=BackfillFidelityTier.GOLD,
            backfill_source="runtime_observed",
        )
        store.write_snapshot(snapshot)
        store.upsert_outcome(outcome)

    return config, store, feature_registry, label_registry, feature_record, label_record
