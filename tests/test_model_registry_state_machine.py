from __future__ import annotations

from pathlib import Path

import pytest

from stock_analyzer.models.artifact import ModelArtifact
from stock_analyzer.models.registry import (
    ModelLifecycleState,
    ModelRegistry,
    ModelRole,
)


def _build_artifact(tmp_path: Path, *, protocol_bound: bool = True) -> tuple[Path, ModelArtifact]:
    artifact_path = tmp_path / ("bound_model.json" if protocol_bound else "legacy_model.json")
    artifact = ModelArtifact.create(
        feature_schema_id="feature_schema_v1_123456789abc" if protocol_bound else "",
        feature_schema_hash="feature_hash_1" if protocol_bound else "",
        label_policy_id="label_policy_v1_123456789abc" if protocol_bound else "",
        label_policy_hash="label_hash_1" if protocol_bound else "",
        dataset_manifest_id="dataset_manifest_v1_123456789abc" if protocol_bound else "",
        feature_columns=["feature_a", "feature_b"],
        lgbm_model={"backend": "fallback_logit", "payload": {"coef": [0.1, 0.2]}},
        xgb_model={"backend": "fallback_logit", "payload": {"coef": [0.1, 0.2]}},
        lgbm_calibrator={"kind": "identity"},
        xgb_calibrator={"kind": "identity"},
        training_metrics={"auc": 0.61, "brier": 0.21},
        metadata={"dataset_split_strategy": "manifest" if protocol_bound else "temporal"},
    )
    artifact.save(artifact_path)
    return artifact_path, artifact


def test_model_registry_registers_protocol_bound_artifact_idempotently(tmp_path: Path) -> None:
    artifact_path, artifact = _build_artifact(tmp_path, protocol_bound=True)
    registry = ModelRegistry(db_path=tmp_path / "registry.duckdb")

    first = registry.register_artifact(
        artifact=artifact,
        artifact_uri=str(artifact_path.resolve()),
        role=ModelRole.CHALLENGER,
        lifecycle_state=ModelLifecycleState.TRAINED,
        parent_model_id="champion_v7",
    )
    second = registry.register_artifact(
        artifact=artifact,
        artifact_uri=str(artifact_path.resolve()),
        role=ModelRole.CHALLENGER,
        lifecycle_state=ModelLifecycleState.TRAINED,
        parent_model_id="champion_v7",
    )

    assert first.model_id == second.model_id
    assert first.dataset_manifest_id == "dataset_manifest_v1_123456789abc"
    assert first.parent_model_id == "champion_v7"
    assert len(registry.list_records()) == 1


def test_model_registry_enforces_lifecycle_transitions_and_champion_role(tmp_path: Path) -> None:
    artifact_path, artifact = _build_artifact(tmp_path, protocol_bound=True)
    registry = ModelRegistry(db_path=tmp_path / "registry.duckdb")
    record = registry.register_artifact(
        artifact=artifact,
        artifact_uri=str(artifact_path.resolve()),
        role=ModelRole.CHALLENGER,
        lifecycle_state=ModelLifecycleState.TRAINED,
    )

    with pytest.raises(ValueError, match="illegal lifecycle transition"):
        registry.update_lifecycle(
            model_id=record.model_id,
            lifecycle_state=ModelLifecycleState.APPROVED,
        )

    with pytest.raises(ValueError, match="blocked transition requires blocked_reason"):
        registry.update_lifecycle(
            model_id=record.model_id,
            lifecycle_state=ModelLifecycleState.BLOCKED,
        )

    with pytest.raises(ValueError, match="champion role requires approved lifecycle state"):
        registry.update_role(
            model_id=record.model_id,
            role=ModelRole.CHAMPION,
        )

    blocked = registry.update_lifecycle(
        model_id=record.model_id,
        lifecycle_state=ModelLifecycleState.BLOCKED,
        blocked_reason="shadow_metrics_missing",
    )
    retrained = registry.update_lifecycle(
        model_id=record.model_id,
        lifecycle_state=ModelLifecycleState.TRAINED,
    )
    shadow_validated = registry.update_lifecycle(
        model_id=record.model_id,
        lifecycle_state=ModelLifecycleState.SHADOW_VALIDATED,
    )
    approved = registry.update_lifecycle(
        model_id=record.model_id,
        lifecycle_state=ModelLifecycleState.APPROVED,
    )
    champion = registry.update_role(
        model_id=record.model_id,
        role=ModelRole.CHAMPION,
    )

    assert blocked.lifecycle_state == ModelLifecycleState.BLOCKED
    assert blocked.blocked_reason == "shadow_metrics_missing"
    assert retrained.lifecycle_state == ModelLifecycleState.TRAINED
    assert retrained.blocked_reason == ""
    assert shadow_validated.lifecycle_state == ModelLifecycleState.SHADOW_VALIDATED
    assert shadow_validated.blocked_reason == ""
    assert approved.lifecycle_state == ModelLifecycleState.APPROVED
    assert approved.promoted_at is not None
    assert champion.role == ModelRole.CHAMPION
    assert registry.active_champion() is not None
    assert registry.active_champion().model_id == champion.model_id


def test_model_registry_rejects_approval_without_protocol_binding(tmp_path: Path) -> None:
    artifact_path, artifact = _build_artifact(tmp_path, protocol_bound=False)
    registry = ModelRegistry(db_path=tmp_path / "registry.duckdb")
    record = registry.register_artifact(
        artifact=artifact,
        artifact_uri=str(artifact_path.resolve()),
        role=ModelRole.SHADOW,
        lifecycle_state=ModelLifecycleState.TRAINED,
    )

    registry.update_lifecycle(
        model_id=record.model_id,
        lifecycle_state=ModelLifecycleState.SHADOW_VALIDATED,
    )
    with pytest.raises(ValueError, match="approved lifecycle requires protocol bindings"):
        registry.update_lifecycle(
            model_id=record.model_id,
            lifecycle_state=ModelLifecycleState.APPROVED,
        )


def test_model_registry_read_paths_retry_after_lock_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_path, artifact = _build_artifact(tmp_path, protocol_bound=True)
    registry = ModelRegistry(db_path=tmp_path / "registry.duckdb")
    registry.register_artifact(
        artifact=artifact,
        artifact_uri=str(artifact_path.resolve()),
        role=ModelRole.CHALLENGER,
        lifecycle_state=ModelLifecycleState.TRAINED,
    )

    original_open_connection = registry._open_connection
    attempts = 0

    def _flaky_open_connection(*, read_only: bool = False):
        nonlocal attempts
        if read_only:
            attempts += 1
            if attempts < 3:
                raise RuntimeError(
                    'Could not set lock on file "/app/artifacts/training/learning_protocol.duckdb"'
                )
        return original_open_connection(read_only=read_only)

    monkeypatch.setattr(registry, "_open_connection", _flaky_open_connection)

    records = registry.list_records()

    assert len(records) == 1
    assert attempts == 3


def test_model_registry_read_paths_fallback_on_duckdb_config_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_path, artifact = _build_artifact(tmp_path, protocol_bound=True)
    registry = ModelRegistry(db_path=tmp_path / "registry.duckdb")
    registry.register_artifact(
        artifact=artifact,
        artifact_uri=str(artifact_path.resolve()),
        role=ModelRole.CHALLENGER,
        lifecycle_state=ModelLifecycleState.TRAINED,
    )

    original_factory = registry._connection_factory

    def _conflicted_connection_factory(database: str, *, read_only: bool = False):
        if read_only:
            raise RuntimeError(
                "Connection Error: Can't open a connection to same database file "
                "with a different configuration than existing connections"
            )
        return original_factory(database)

    monkeypatch.setattr(registry, "_connection_factory", _conflicted_connection_factory)

    records = registry.list_records()

    assert len(records) == 1
