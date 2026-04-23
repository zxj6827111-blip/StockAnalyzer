from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from stock_analyzer.config import load_config
from stock_analyzer.evolution.champion_shadow_report import ChampionShadowReportBuilder
from stock_analyzer.learning.feature_schema_registry import FeatureSchemaRegistry
from stock_analyzer.learning.label_policy_registry import LabelPolicyRegistry
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    MaturityStatus,
    OutcomeRecord,
    SignalSnapshot,
)
from stock_analyzer.learning.sample_store import SampleStore
from stock_analyzer.models.registry import (
    ModelLifecycleState,
    ModelRegistry,
    ModelRole,
)
from stock_analyzer.models.trainer import ModelTrainer


def test_champion_shadow_report_builder_compares_shadow_manifest_against_active_champion(
    tmp_path: Path,
) -> None:
    (
        config,
        store,
        feature_registry,
        label_registry,
        feature_record,
        label_record,
        snapshot_ids,
    ) = _build_learning_protocol_fixture(tmp_path)
    trainer = ModelTrainer(
        training=config.training,
        labels=config.labels,
        models=config.models,
    )

    champion_result = trainer.train_on_sample_store(
        store=store,
        feature_schema_id=feature_record.feature_schema_id,
        feature_schema_hash=feature_record.feature_schema_hash,
        label_policy_id=label_record.label_policy_id,
        label_policy_hash=label_record.label_policy_hash,
        snapshot_ids=snapshot_ids[:40],
        feature_schema_registry=feature_registry,
        label_policy_registry=label_registry,
    )
    shadow_result = trainer.train_on_sample_store(
        store=store,
        feature_schema_id=feature_record.feature_schema_id,
        feature_schema_hash=feature_record.feature_schema_hash,
        label_policy_id=label_record.label_policy_id,
        label_policy_hash=label_record.label_policy_hash,
        snapshot_ids=snapshot_ids[10:],
        feature_schema_registry=feature_registry,
        label_policy_registry=label_registry,
    )

    champion_path = tmp_path / "champion_artifact.json"
    shadow_path = tmp_path / "shadow_artifact.json"
    champion_result.artifact.save(champion_path)
    shadow_result.artifact.save(shadow_path)

    registry = ModelRegistry(db_path=tmp_path / "model_registry.duckdb")
    champion_record = registry.register_artifact(
        artifact=champion_result.artifact,
        artifact_uri=str(champion_path.resolve()),
        role=ModelRole.CHAMPION,
        lifecycle_state=ModelLifecycleState.APPROVED,
    )
    shadow_record = registry.register_artifact(
        artifact=shadow_result.artifact,
        artifact_uri=str(shadow_path.resolve()),
        role=ModelRole.SHADOW,
        lifecycle_state=ModelLifecycleState.TRAINED,
        parent_model_id=champion_record.model_id,
    )

    report = ChampionShadowReportBuilder(
        store=store,
        model_registry=registry,
        feature_schema_registry=feature_registry,
        label_policy_registry=label_registry,
    ).build_report(
        shadow_model_id=shadow_record.model_id,
        split_names=["test"],
    )

    assert report.champion_model_id == champion_record.model_id
    assert report.shadow_model_id == shadow_record.model_id
    assert report.dataset_manifest_id == shadow_result.artifact.dataset_manifest_id
    assert report.row_count >= 1
    assert report.split_counts == {"test": report.row_count}
    assert report.m11_report["metrics"]["valid_samples"] == report.row_count
    assert "signal_divergence_ratio" in report.summary_metrics

    first_row = report.rows[0]
    assert first_row.split_name == "test"
    assert first_row.maturity_status == "reconciled"
    assert 0.0 <= first_row.champion_scores["p_meta"] <= 1.0
    assert 0.0 <= first_row.shadow_scores["p_meta"] <= 1.0
    assert first_row.champion_signal in {0, 1}
    assert first_row.shadow_signal in {0, 1}
    assert first_row.to_dict()["maturity_status"] == "reconciled"


def _build_learning_protocol_fixture(
    tmp_path: Path,
) -> tuple[
    object,
    SampleStore,
    FeatureSchemaRegistry,
    LabelPolicyRegistry,
    object,
    object,
    list[str],
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
    snapshot_ids: list[str] = []
    for index in range(60):
        snapshot_id = f"snap-{index:03d}"
        snapshot_ids.append(snapshot_id)
        snapshot = SignalSnapshot(
            snapshot_id=snapshot_id,
            code_version="git:test",
            symbol="600000.SH" if index % 2 == 0 else "000001.SZ",
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
            execution_fill_ratio=0.95 if index % 3 else 0.85,
            realized_slippage_bp=8.0 if index % 3 else 15.0,
            backfill_fidelity_tier=BackfillFidelityTier.GOLD,
            backfill_source="runtime_observed",
        )
        store.write_snapshot(snapshot)
        store.upsert_outcome(outcome)

    return (
        config,
        store,
        feature_registry,
        label_registry,
        feature_record,
        label_record,
        snapshot_ids,
    )
