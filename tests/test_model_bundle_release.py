"""Commit 2（P0-a）验收测试：内容寻址 bundle + 两阶段发布 CAS + fail-closed。"""

from __future__ import annotations

import json
import os
import struct
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest

import stock_analyzer.models.bundle as bundle_module
from stock_analyzer.models.artifact import ModelArtifact
from stock_analyzer.models.bundle import (
    ARTIFACT_FILENAME,
    BundleCollisionError,
    BundleIntegrityError,
    bundle_id_from_content_hash,
    compute_bundle_content_hash,
    prune_model_bundle_archive,
    publish_bundle_from_artifact_directory,
    publish_model_bundle,
    verify_artifact_integrity,
)
from stock_analyzer.models.registry import (
    ModelLifecycleState,
    ModelRegistry,
    ModelRegistryCASConflictError,
    ModelRole,
)
from tests.test_service_learning_governance import (
    _as_mapping,
    _load_test_config,
    _new_service,
    _seed_learning_protocol_samples,
)

# ---------------------------------------------------------------------------
# bundle 内容哈希与发布
# ---------------------------------------------------------------------------


def _write_file(root: Path, relative: str, content: bytes) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def test_bundle_hash_is_order_independent_and_deterministic(tmp_path: Path) -> None:
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    _write_file(dir_a, "zeta.bin", b"2222")
    _write_file(dir_a, "model.json", b"111")
    _write_file(dir_a, "sub/alpha.bin", b"33333333")
    # 同一文件集，不同创建顺序。
    _write_file(dir_b, "sub/alpha.bin", b"33333333")
    _write_file(dir_b, "model.json", b"111")
    _write_file(dir_b, "zeta.bin", b"2222")

    assert compute_bundle_content_hash(dir_a) == compute_bundle_content_hash(dir_b)
    assert len(compute_bundle_content_hash(dir_a)) == 64


def test_bundle_hash_encoding_is_unambiguous(tmp_path: Path) -> None:
    # 无长度前缀时 ("a" + b"bc") 与 ("ab" + b"") 无法区分；u64 前缀必须区分开。
    dir_a = tmp_path / "case_a"
    dir_b = tmp_path / "case_b"
    _write_file(dir_a, "a", b"bc")
    _write_file(dir_b, "ab", b"")

    digest_a = compute_bundle_content_hash(dir_a)
    digest_b = compute_bundle_content_hash(dir_b)
    assert digest_a != digest_b

    # 手工重放编码公式验证：排序后逐文件 u64_be(path_len)+path+u64_be(size)+bytes。
    expected = __import__("hashlib").sha256()
    for name, content in (("a", b"bc"),):
        encoded = name.encode("utf-8")
        expected.update(struct.pack(">Q", len(encoded)))
        expected.update(encoded)
        expected.update(struct.pack(">Q", len(content)))
        expected.update(content)
    assert digest_a == expected.hexdigest()


def test_publish_model_bundle_is_idempotent_for_identical_content(tmp_path: Path) -> None:
    artifact = ModelArtifact.create(
        feature_columns=["f"],
        lgbm_model={"kind": "fallback"},
        xgb_model={"kind": "fallback"},
        lgbm_calibrator={},
        xgb_calibrator={},
        training_metrics={"auc": 0.6},
    )
    archive = tmp_path / "archive"
    first = publish_model_bundle(artifact, archive_root=archive)
    second = publish_model_bundle(artifact, archive_root=archive)

    assert first.bundle_id == second.bundle_id
    assert first.root == second.root
    assert first.content_hash == second.content_hash
    assert first.bundle_id.startswith("model_v2_")
    assert len(first.bundle_id) == len("model_v2_") + 20
    # staging 目录不残留。
    assert not list(archive.glob(".staging_*"))


def test_prune_model_bundle_archive_keeps_recent_and_referenced_bundles(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive"
    publications = []
    for index in range(7):
        artifact = ModelArtifact.create(
            feature_columns=["f"],
            lgbm_model={"kind": "fallback", "index": index},
            xgb_model={},
            lgbm_calibrator={},
            xgb_calibrator={},
            training_metrics={"auc": 0.60 + index / 100.0},
        )
        publication = publish_model_bundle(artifact, archive_root=archive)
        os.utime(publication.root, (1_000 + index, 1_000 + index))
        publications.append(publication)

    removed = prune_model_bundle_archive(
        archive,
        retention_count=5,
        protected_bundle_ids={publications[0].bundle_id},
    )

    assert publications[0].root.is_dir()
    assert not publications[1].root.exists()
    assert len(removed) == 1
    assert len(list(archive.glob("model_v2_*"))) == 6


def test_publish_model_bundle_collision_fails_closed(tmp_path: Path, monkeypatch) -> None:
    artifact = ModelArtifact.create(
        feature_columns=["f"],
        lgbm_model={},
        xgb_model={},
        lgbm_calibrator={},
        xgb_calibrator={},
        training_metrics={},
    )
    archive = tmp_path / "archive"
    publication = publish_model_bundle(artifact, archive_root=archive)

    staged = archive / ".staging_forge"
    staged.mkdir()
    _write_file(staged, ARTIFACT_FILENAME, b'{"different": true}')

    hash_a = "a" * 64
    hash_b = "b" * 64
    # 预置一个“已存在”的最终目录，其目录名与 staging 伪造哈希的 bundle id 一致。
    (archive / bundle_id_from_content_hash(hash_a)).mkdir()
    calls = {"n": 0}

    def fake_compute(_root: Path) -> str:
        calls["n"] += 1
        return hash_a if calls["n"] == 1 else hash_b

    monkeypatch.setattr(bundle_module, "compute_bundle_content_hash", fake_compute)
    with pytest.raises(BundleCollisionError, match="collision"):
        bundle_module._finalize_staged_bundle(staging=staged, root=archive)
    # 冲突时既有不可变目录保持原样。
    assert (publication.root / ARTIFACT_FILENAME).is_file()


def test_seed_bundle_from_directory_preserves_sidecars_and_detects_tampering(
    tmp_path: Path,
) -> None:
    sidecar_bytes = b"native-binary-payload"
    source_dir = tmp_path / "seed"
    source_dir.mkdir()
    payload = {
        "version": "v2",
        "feature_columns": ["f"],
        "lgbm_model": {
            "sidecar_format": "bin",
            "sidecar_path": "model_v1_sidecars/lgbm_001.bin",
            "sidecar_sha256": __import__("hashlib").sha256(sidecar_bytes).hexdigest(),
        },
        "xgb_model": {},
        "lgbm_calibrator": {},
        "xgb_calibrator": {},
        "training_metrics": {},
    }
    (source_dir / "model_v1.json").write_text(json.dumps(payload), encoding="utf-8")
    _write_file(source_dir, "model_v1_sidecars/lgbm_001.bin", sidecar_bytes)

    publication = publish_bundle_from_artifact_directory(
        source_dir / "model_v1.json",
        archive_root=tmp_path / "archive",
    )
    bundled_sidecar = publication.root / "model_sidecars" / "lgbm_001.bin"
    assert bundled_sidecar.read_bytes() == sidecar_bytes
    verify_artifact_integrity(
        publication.artifact_path,
        expected_content_hash=publication.content_hash,
    )

    # 篡改 bundle 内 sidecar → 完整性校验 fail-closed。
    bundled_sidecar.write_bytes(b"tampered")
    with pytest.raises(BundleIntegrityError, match="sha256_mismatch|hash mismatch"):
        verify_artifact_integrity(
            publication.artifact_path,
            expected_content_hash=publication.content_hash,
        )


def test_verify_artifact_integrity_rejects_unsafe_sidecar_path(tmp_path: Path) -> None:
    source_dir = tmp_path / "seed"
    source_dir.mkdir()
    payload = {
        "version": "v2",
        "feature_columns": [],
        "lgbm_model": {"sidecar_path": "../escape.bin", "sidecar_sha256": ""},
        "xgb_model": {},
        "lgbm_calibrator": {},
        "xgb_calibrator": {},
        "training_metrics": {},
    }
    (source_dir / "model_v1.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BundleIntegrityError, match="unsafe_sidecar_path"):
        publish_bundle_from_artifact_directory(
            source_dir / "model_v1.json",
            archive_root=tmp_path / "archive",
        )


# ---------------------------------------------------------------------------
# registry 迁移 + CAS 晋级
# ---------------------------------------------------------------------------


_LEGACY_CREATE = (
    "CREATE TABLE model_registry ("
    "model_id VARCHAR PRIMARY KEY, "
    "schema_version VARCHAR NOT NULL, "
    "role VARCHAR NOT NULL, "
    "lifecycle_state VARCHAR NOT NULL, "
    "parent_model_id VARCHAR NOT NULL, "
    "artifact_uri VARCHAR NOT NULL, "
    "artifact_created_at VARCHAR, "
    "dataset_manifest_id VARCHAR NOT NULL, "
    "feature_schema_id VARCHAR NOT NULL, "
    "feature_schema_hash VARCHAR NOT NULL, "
    "label_policy_id VARCHAR NOT NULL, "
    "label_policy_hash VARCHAR NOT NULL, "
    "metrics_summary_json VARCHAR NOT NULL, "
    "blocked_reason VARCHAR NOT NULL, "
    "created_at VARCHAR NOT NULL, "
    "updated_at VARCHAR NOT NULL, "
    "promoted_at VARCHAR, "
    "revoked_at VARCHAR)"
)


def _approved_challenger_record(model_id: str, uri: str):
    """构建 APPROVED challenger 记录；uri 指向真实文件——champion 晋升会从该
    文件物化 content hash（Phase 0 §3.3 契约：champion 必须可校验）。"""

    from stock_analyzer.models.registry import build_model_registry_record_from_artifact

    artifact_path = Path(uri)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("{}", encoding="utf-8")
    artifact = ModelArtifact.create(
        feature_columns=["f"],
        lgbm_model={},
        xgb_model={},
        lgbm_calibrator={},
        xgb_calibrator={},
        training_metrics={},
        dataset_manifest_id="dm1",
        feature_schema_id="fs1",
        feature_schema_hash="fsh1",
        label_policy_id="lp1",
        label_policy_hash="lph1",
    )
    return build_model_registry_record_from_artifact(
        artifact=artifact,
        artifact_uri=uri,
        role=ModelRole.CHALLENGER,
        lifecycle_state=ModelLifecycleState.APPROVED,
        model_id=model_id,
    )


def test_registry_migration_is_idempotent_and_backfills_empty_hash(tmp_path: Path) -> None:
    import duckdb

    db_path = tmp_path / "registry.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_LEGACY_CREATE)
    conn.execute(
        "INSERT INTO model_registry (model_id, schema_version, role, lifecycle_state, "
        "parent_model_id, artifact_uri, dataset_manifest_id, feature_schema_id, "
        "feature_schema_hash, label_policy_id, label_policy_hash, metrics_summary_json, "
        "blocked_reason, created_at, updated_at) VALUES "
        "('legacy_model', '1', 'champion', 'approved', '', '/tmp/x.json', 'dm', 'fs', "
        "'fsh', 'lp', 'lph', '{}', '', '2026-01-01T00:00:00+00:00', "
        "'2026-01-01T00:00:00+00:00')"
    )
    conn.close()

    registry = ModelRegistry(db_path=db_path)
    registry.ensure_schema()
    registry.ensure_schema()  # 幂等

    record = registry.get_by_id("legacy_model")
    assert record is not None
    assert record.artifact_content_hash == ""

    conn = duckdb.connect(str(db_path))
    row = conn.execute(
        "SELECT artifact_content_hash FROM model_registry WHERE model_id = 'legacy_model'"
    ).fetchone()
    conn.close()
    assert row is not None and str(row[0]) == ""


def test_registry_read_path_works_on_unmigrated_legacy_table(tmp_path: Path) -> None:
    import duckdb

    db_path = tmp_path / "registry_legacy.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(_LEGACY_CREATE)
    conn.execute(
        "INSERT INTO model_registry (model_id, schema_version, role, lifecycle_state, "
        "parent_model_id, artifact_uri, dataset_manifest_id, feature_schema_id, "
        "feature_schema_hash, label_policy_id, label_policy_hash, metrics_summary_json, "
        "blocked_reason, created_at, updated_at) VALUES "
        "('legacy_read', '1', 'challenger', 'trained', '', '/tmp/y.json', 'dm', 'fs', "
        "'fsh', 'lp', 'lph', '{}', '', '2026-01-01T00:00:00+00:00', "
        "'2026-01-01T00:00:00+00:00')"
    )
    conn.close()

    registry = ModelRegistry(db_path=db_path)
    record = registry.get_by_id("legacy_read")
    assert record is not None
    assert record.role == ModelRole.CHALLENGER
    assert record.artifact_content_hash == ""


def test_registry_round_trips_artifact_content_hash(tmp_path: Path) -> None:
    registry = ModelRegistry(db_path=tmp_path / "registry.duckdb")
    record = _approved_challenger_record(
        "model_v2_abc", str(tmp_path / "bundle" / "model.json")
    )
    record = record.model_copy(update={"artifact_content_hash": "c" * 64})
    registry.register(record)
    loaded = registry.get_by_id("model_v2_abc")
    assert loaded is not None
    assert loaded.artifact_content_hash == "c" * 64


def test_promote_model_with_cas_success_and_conflict(tmp_path: Path) -> None:
    registry = ModelRegistry(db_path=tmp_path / "registry.duckdb")
    champion = _approved_challenger_record(
        "champ_old", str(tmp_path / "champ" / "model.json")
    )
    registry.register(champion)
    registry.update_role(model_id="champ_old", role=ModelRole.CHAMPION)
    challenger = _approved_challenger_record(
        "chall_new", str(tmp_path / "chall" / "model.json")
    )
    registry.register(challenger)

    promoted, demoted = registry.promote_model_with_cas(
        target_model_id="chall_new",
        expected_previous_champion_id="champ_old",
    )
    assert promoted.model_id == "chall_new"
    assert promoted.role == ModelRole.CHAMPION
    assert [item.model_id for item in demoted] == ["champ_old"]
    assert demoted[0].role == ModelRole.CHALLENGER
    champions = registry.list_records(role=ModelRole.CHAMPION)
    assert [item.model_id for item in champions] == ["chall_new"]

    # CAS 期望值不符 → 冲突，状态不变。
    other = _approved_challenger_record(
        "chall_other", str(tmp_path / "other" / "model.json")
    )
    registry.register(other)
    with pytest.raises(ModelRegistryCASConflictError):
        registry.promote_model_with_cas(
            target_model_id="chall_other",
            expected_previous_champion_id="chall_other_wrong",
        )
    assert registry.active_champion() is not None
    assert registry.active_champion().model_id == "chall_new"


# ---------------------------------------------------------------------------
# 两阶段发布（governance 全流程）
# ---------------------------------------------------------------------------


def _prepare_release_ready_service(tmp_path: Path):
    """构建到 ticket 已签发为止的 governance service 夹具。"""

    config = _load_test_config(tmp_path)
    config.training.model_archive_dir = str(tmp_path / "model_archive")
    service = _new_service(config)
    notifications: list[dict[str, object]] = []
    service.notify = lambda **kwargs: notifications.append(dict(kwargs))  # type: ignore[method-assign]
    _seed_learning_protocol_samples(service, symbols=["600000", "000001"], rows_per_symbol=30)

    champion_training = _as_mapping(
        service.train_models(
            full_market=True,
            lookback_days=240,
            preferred_symbols=["600000", "000001"],
            artifact_path=str(tmp_path / "champion_model.json"),
        )
    )
    champion_model_id = str(_as_mapping(champion_training["model_registry"])["model_id"])
    champion_record = service._model_registry.get_by_id(champion_model_id)
    assert champion_record is not None
    assert champion_record.artifact_content_hash, "training entry must register bundles"
    # 与既有 governance 夹具一致：champion 走完整生命周期后授角色。
    from datetime import UTC, datetime

    service.update_model_registry_lifecycle(
        model_id=champion_model_id,
        lifecycle_state="shadow_validated",
        timestamp=datetime(2026, 3, 31, 20, 0, tzinfo=UTC),
    )
    service.update_model_registry_lifecycle(
        model_id=champion_model_id,
        lifecycle_state="approved",
        timestamp=datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
    )
    service.update_model_registry_role(
        model_id=champion_model_id,
        role="champion",
        timestamp=datetime(2026, 4, 1, 9, 5, tzinfo=UTC),
    )

    manifest = _as_mapping(
        service.build_learning_trainable_manifest(symbols=["600000", "000001"])
    )
    shadow_validation = _as_mapping(
        service.run_learning_manifest_shadow_validation(
            dataset_manifest_id=str(manifest["dataset_manifest_id"]),
            split_names=["test"],
            min_samples=1,
            preview_limit=3,
            mark_shadow_validated=True,
        )
    )
    shadow_model_id = str(shadow_validation["shadow_model_id"])
    shadow_record = service._model_registry.get_by_id(shadow_model_id)
    assert shadow_record is not None
    assert shadow_record.artifact_content_hash, "shadow training must register bundles"

    gate_payload = dict(
        _as_mapping(
            service.evaluate_learning_model_promotion_gate(
                model_id=shadow_model_id,
                champion_model_id=champion_model_id,
                split_names=["test"],
                min_samples=1,
                preview_limit=3,
            )
        )
    )
    gate_payload.update(
        {
            "ok": True,
            "status": "pass",
            "accepted": True,
            "recommended_action": "approve",
            "reason_codes": ["promotion_gate_passed"],
            "blockers": [],
            "warnings": [],
            "errors": [],
        }
    )

    def _fake_run_shadow_gate(**kwargs: object) -> dict[str, object]:
        _ = kwargs
        return deepcopy(gate_payload)

    object.__setattr__(
        service,
        "run_learning_manifest_shadow_promotion_gate",
        _fake_run_shadow_gate,
    )
    workflow = _as_mapping(
        service.run_learning_manifest_shadow_proposal(
            dataset_manifest_id=str(manifest["dataset_manifest_id"]),
            split_names=["test"],
            min_samples=1,
            preview_limit=3,
            auto_approve=False,
        )
    )
    proposal = dict(_as_mapping(workflow["proposal"]))
    approval = _as_mapping(
        service.record_learning_model_proposal_approval(
            "risk_committee",
            True,
            proposal_id=str(proposal["proposal_id"]),
            note="gate passed",
        )
    )
    assert bool(approval["accepted"]) or bool(approval.get("approval", {}).get("approved"))
    ticket_payload = _as_mapping(
        service.issue_learning_model_release_ticket(
            "release_manager",
            proposal_id=str(proposal["proposal_id"]),
            note="manual release",
        )
    )
    return {
        "service": service,
        "notifications": notifications,
        "champion_model_id": champion_model_id,
        "shadow_model_id": shadow_model_id,
        "ticket": dict(_as_mapping(ticket_payload["ticket"])),
    }


def test_two_phase_release_happy_path_switches_alias_and_roles(tmp_path: Path) -> None:
    ctx = _prepare_release_ready_service(tmp_path)
    service = cast(object, ctx["service"])
    alias_path = Path(str(service._config.training.artifact_path)).expanduser()
    previous_alias_exists = alias_path.exists()

    execute = _as_mapping(
        service.execute_learning_model_release_ticket(
            "release_manager",
            ticket_id=str(ctx["ticket"]["ticket_id"]),
            note="release done",
        )
    )
    assert execute["accepted"] is True
    ticket = _as_mapping(execute["ticket"])

    shadow_record = service._model_registry.get_by_id(ctx["shadow_model_id"])
    champion_after = service._model_registry.active_champion()
    assert shadow_record is not None and shadow_record.role == ModelRole.CHAMPION
    assert champion_after is not None and champion_after.model_id == ctx["shadow_model_id"]
    old_record = service._model_registry.get_by_id(ctx["champion_model_id"])
    assert old_record is not None and old_record.role == ModelRole.CHALLENGER

    # 别名 JSON 存在且自描述字段与 registry/bundle 一致。
    assert alias_path.exists() or previous_alias_exists
    alias_payload = json.loads(alias_path.read_text(encoding="utf-8"))
    metadata = alias_payload.get("metadata", {})
    assert metadata.get("registry_model_id") == ctx["shadow_model_id"]
    assert metadata.get("bundle_content_hash") == shadow_record.artifact_content_hash

    release_flow = _as_mapping(ticket["release_flow"])
    assert release_flow["mode"] == "two_phase_bundle_cas"
    candidate_details = _as_mapping(release_flow["candidate_mode_details"])
    assert candidate_details.get("registry_model_id") == ctx["shadow_model_id"]

    # 别名可加载为 predictor 且 mode_details 暴露身份字段。
    from stock_analyzer.models.predictor import SignalPredictor

    predictor = SignalPredictor.load(alias_path)
    mode_details = predictor.mode_details()
    assert mode_details["registry_model_id"] == ctx["shadow_model_id"]
    assert mode_details["bundle_content_hash"] == shadow_record.artifact_content_hash


def test_two_phase_release_restores_all_when_registry_cas_fails(tmp_path: Path) -> None:
    ctx = _prepare_release_ready_service(tmp_path)
    service = cast(object, ctx["service"])
    alias_path = Path(str(service._config.training.artifact_path)).expanduser()
    alias_before = alias_path.read_bytes() if alias_path.exists() else None
    predictor_before = service._pipeline._predictor

    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("injected cas failure")

    object.__setattr__(
        service._model_registry,
        "promote_model_with_cas",
        _boom,
    )
    execute = _as_mapping(
        service.execute_learning_model_release_ticket(
            "release_manager",
            ticket_id=str(ctx["ticket"]["ticket_id"]),
        )
    )
    assert execute["accepted"] is False
    assert execute["code"] == "release_flow_failed"
    recovery = _as_mapping(execute["release_flow_recovery"])
    assert recovery["alias_restored"] is True
    assert recovery["predictor_restored"] is True

    if alias_before is None:
        assert not alias_path.exists()
    else:
        assert alias_path.read_bytes() == alias_before
    assert service._pipeline._predictor is predictor_before
    roles = {
        record.model_id: record.role
        for record in service._model_registry.list_records(limit=50)
    }
    assert roles[ctx["champion_model_id"]] == ModelRole.CHAMPION
    assert roles[ctx["shadow_model_id"]] != ModelRole.CHAMPION


def test_two_phase_release_compensates_when_ticket_persistence_fails(
    tmp_path: Path,
) -> None:
    ctx = _prepare_release_ready_service(tmp_path)
    service = cast(object, ctx["service"])
    governance = service._learning_governance_service
    alias_path = Path(str(service._config.training.artifact_path)).expanduser()
    alias_before = alias_path.read_bytes() if alias_path.exists() else None
    predictor_before = service._pipeline._predictor

    original_append_history = governance._append_history

    def _failing_append_history(*args: object, **kwargs: object) -> object:
        raise RuntimeError("injected ticket persistence failure")

    object.__setattr__(governance, "_append_history", _failing_append_history)
    try:
        execute = _as_mapping(
            service.execute_learning_model_release_ticket(
                "release_manager",
                ticket_id=str(ctx["ticket"]["ticket_id"]),
            )
        )
    finally:
        object.__setattr__(governance, "_append_history", original_append_history)

    assert execute["accepted"] is False
    assert execute["code"] == "ticket_persistence_failed"
    recovery = _as_mapping(execute["release_flow_recovery"])
    assert recovery["alias_restored"] is True
    assert recovery["predictor_restored"] is True
    assert recovery["registry_restored"] is True

    if alias_before is None:
        assert not alias_path.exists()
    else:
        assert alias_path.read_bytes() == alias_before
    assert service._pipeline._predictor is predictor_before
    roles = {
        record.model_id: record.role
        for record in service._model_registry.list_records(limit=50)
    }
    assert roles[ctx["champion_model_id"]] == ModelRole.CHAMPION
    assert roles[ctx["shadow_model_id"]] != ModelRole.CHAMPION


def test_two_phase_release_rejects_non_bundle_target_fail_closed(tmp_path: Path) -> None:
    ctx = _prepare_release_ready_service(tmp_path)
    service = cast(object, ctx["service"])
    # 把目标模型降级为“松散工件”记录（无内容哈希）→ 发布必须拒绝。
    record = service._model_registry.get_by_id(ctx["shadow_model_id"])
    assert record is not None
    stripped = record.model_copy(update={"artifact_content_hash": ""})
    service._model_registry.upsert_repair_record(stripped)

    execute = _as_mapping(
        service.execute_learning_model_release_ticket(
            "release_manager",
            ticket_id=str(ctx["ticket"]["ticket_id"]),
        )
    )
    assert execute["accepted"] is False
    assert execute["code"] == "target_bundle_not_content_addressed"
