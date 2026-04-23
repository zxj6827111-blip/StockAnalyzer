from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from stock_analyzer.config import load_config
from stock_analyzer.evolution.shadow_dataset_builder import ShadowDatasetBuilder
from stock_analyzer.learning.dataset_manifest import DatasetManifestBuilder
from stock_analyzer.learning.feature_schema_registry import FeatureSchemaRegistry
from stock_analyzer.learning.label_policy_registry import LabelPolicyRegistry
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    MaturityStatus,
    OutcomeRecord,
    SignalSnapshot,
)
from stock_analyzer.learning.sample_store import SampleStore
from stock_analyzer.models.registry import ModelRegistry
from stock_analyzer.models.trainer import ModelTrainer


def test_shadow_dataset_builder_builds_scored_test_split_rows(tmp_path: Path) -> None:
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
    artifact_path = tmp_path / "shadow_dataset_artifact.json"
    result.artifact.save(artifact_path)

    registry = ModelRegistry(db_path=tmp_path / "model_registry.duckdb")
    record = registry.register_artifact(
        artifact=result.artifact,
        artifact_uri=str(artifact_path.resolve()),
    )

    builder = ShadowDatasetBuilder(
        store=store,
        model_registry=registry,
        feature_schema_registry=feature_registry,
        label_policy_registry=label_registry,
    )
    dataset = builder.build_for_model(
        model_id=record.model_id,
        split_names=["test"],
    )
    test_items = [
        item
        for item in store.list_manifest_items(manifest.dataset_manifest_id)
        if item.split_name == "test"
    ]

    assert dataset.model_id == record.model_id
    assert dataset.dataset_manifest_id == manifest.dataset_manifest_id
    assert dataset.feature_schema_id == feature_record.feature_schema_id
    assert dataset.label_policy_id == label_record.label_policy_id
    assert dataset.requested_split_names == ["test"]
    assert dataset.row_count == len(test_items)
    assert dataset.split_counts == {"test": len(test_items)}
    assert dataset.schema_feature_columns == ["feature_b", "feature_a"]
    assert "__random_baseline__" in dataset.model_feature_columns
    assert dataset.predictor_mode["predictor_mode"] == "artifact_loaded"

    first = dataset.rows[0]
    assert first.ordinal == test_items[0].ordinal
    assert first.split_name == "test"
    assert first.label in {0.0, 1.0}
    assert first.feature_vector["__random_baseline__"] == 0.0
    assert 0.0 <= first.baseline_scores["p_lgbm"] <= 1.0
    assert 0.0 <= first.baseline_scores["p_xgb"] <= 1.0
    assert 0.0 <= first.baseline_scores["p_meta"] <= 1.0


def _build_learning_protocol_fixture(
    tmp_path: Path,
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
        feature_names=["feature_b", "feature_a"],
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
            feature_vector={
                "feature_b": float(index % 7),
                "feature_a": float((index % 3) / 3.0),
            },
            feature_schema_id=feature_record.feature_schema_id,
            feature_schema_hash=feature_record.feature_schema_hash,
            runtime_config_hash="runtime_hash_1",
            label_policy_id=label_record.label_policy_id,
            label_policy_hash=label_record.label_policy_hash,
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
