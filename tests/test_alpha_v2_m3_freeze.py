"""M3 Validation Freeze Manifest 测试（冻结清单完整性 / 哈希完整性 / 回填纪律）。"""

from __future__ import annotations

import pytest

from stock_analyzer.alpha_v2.validation.freeze import (
    FREEZE_MANIFEST_SCHEMA,
    REQUIRED_TOP_LEVEL_FIELDS,
    FreezeIncompleteError,
    assert_freeze_complete,
    build_validation_freeze,
    freeze_manifest_hash,
    load_validation_freeze,
    seal_freeze_manifest,
    verify_freeze_against_runtime,
    verify_freeze_integrity,
    write_validation_freeze,
)


def _manifest(**overrides):
    base = dict(
        validation_epoch_id="alpha_v2_epoch_001",
        code_commit="55c7592fe88ac3b3ab5b233df422b0f528141074",
        git_branch="feat/alpha-v2-m1-0917",
        config_hash="c" * 64,
        config_hash_scope="effective_config_with_env_overrides",
        model={
            "model_id": "alpha_v2_shadow_epoch_001",
            "artifact_hash": "a" * 64,
            "artifact_created_at": "2026-09-18T12:00:00+08:00",
            "artifact_path": "artifacts/alpha_v2/validation/model/alpha_v2_shadow_epoch_001",
            "status": "frozen",
        },
        feature_columns=["ret_1d", "ma5"],
        feature_group_ids=["price_volume_technical"],
        selection_contract={
            "selection_contract_id": "night_alpha_v2_v1",
            "quality_target": 300,
            "light_target": 100,
            "deep_target": 50,
            "final_cap": 5,
        },
        execution_price_mode="raw",
        feature_price_mode="qfq",
        created_at="2026-09-18T22:00:00+08:00",
    )
    base.update(overrides)
    return build_validation_freeze(**base)


def test_manifest_has_all_required_fields_and_stable_hash():
    manifest = _manifest()
    for field in REQUIRED_TOP_LEVEL_FIELDS:
        assert field in manifest
    assert manifest["schema"] == FREEZE_MANIFEST_SCHEMA
    assert manifest["freeze_manifest_hash"] == freeze_manifest_hash(manifest)
    assert verify_freeze_integrity(manifest)
    # 主/确认 horizon 的硬编码是 M3 §5 的业务口径
    assert manifest["primary_business_horizon"] == 5
    assert manifest["confirmation_horizon"] == 3
    assert manifest["horizons"] == [3, 5, 10, 15]
    # 样本门原样冻结（20/60/120/250）
    assert manifest["sample_gates"] == {
        "failure_alert": 20,
        "direction_review": 60,
        "advisory_discussion": 120,
        "auto_governance": 250,
    }
    assert manifest["benchmarks"]["primary_layer"] == "quality_pool_ew"


def test_write_and_load_roundtrip(tmp_path):
    manifest = _manifest()
    path = write_validation_freeze(manifest, root=tmp_path)
    loaded = load_validation_freeze(tmp_path)
    assert loaded is not None
    assert loaded["freeze_manifest_hash"] == manifest["freeze_manifest_hash"]
    assert path.name == "validation_freeze_manifest.json"


def test_tampered_manifest_fails_integrity(tmp_path):
    manifest = _manifest()
    write_validation_freeze(manifest, root=tmp_path)
    tampered = dict(manifest)
    tampered["code_commit"] = "deadbeef"
    assert not verify_freeze_integrity(tampered)
    # 带着被改的内容与旧 hash 再落盘：直接拒绝（不允许"改内容保留旧哈希"）。
    with pytest.raises(FreezeIncompleteError):
        write_validation_freeze(tampered, root=tmp_path)


def test_missing_field_raises():
    manifest = _manifest()
    manifest.pop("config_hash")
    with pytest.raises(FreezeIncompleteError):
        assert_freeze_complete(manifest)


def test_pending_model_is_explicit_not_silent():
    manifest = _manifest(model=None)
    assert manifest["model"]["status"] == "pending_freeze"
    # pending 不等于 frozen：对运行核验给出模型哈希违例
    violations = verify_freeze_against_runtime(manifest, model_artifact_hash="real")
    assert any(item.startswith("model_artifact_hash") for item in violations)


def test_verify_against_runtime_detects_drift():
    manifest = _manifest()
    assert verify_freeze_against_runtime(
        manifest,
        code_commit=manifest["code_commit"],
        config_hash=manifest["config_hash"],
        model_artifact_hash=manifest["model"]["artifact_hash"],
    ) == []
    violations = verify_freeze_against_runtime(manifest, code_commit="other")
    assert any(item.startswith("code_commit") for item in violations)


def test_seal_validation_start_date_only_once():
    manifest = _manifest()
    assert manifest["validation_start_date"] is None
    sealed = seal_freeze_manifest(
        manifest, validation_start_date="2026-09-21", sealed_at="2026-09-18T23:00:00+08:00"
    )
    assert sealed["validation_start_date"] == "2026-09-21"
    assert sealed["freeze_manifest_hash"] != manifest["freeze_manifest_hash"]
    with pytest.raises(ValueError):
        seal_freeze_manifest(
            sealed, validation_start_date="2026-09-22", sealed_at="2026-09-22"
        )
