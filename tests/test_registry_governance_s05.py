"""S05 Registry / Archive 治理：在服清单、容量化归档、历史坏记录对账。

对应蓝图 §5 P0-02 / 阶段施工提示词 S05 的 Done When：

```text
新训练模型能从 model_id 定位 immutable artifact，并重算 hash 一致
```

以及三条硬要求：

1. 新写入 registry 的 URI 必须真指向**模型工件**（不是数据集清单）；
2. dataset manifest 单独引用；serving manifest 独立
   （``artifacts/model_serving_manifest.json``）；
3. 历史坏记录只做 reconciliation 标注（legacy_manifest_pointer / legacy_empty_hash /
   legacy_alias_pointer / unrecoverable_artifact），**不得伪造修复**。
"""

from __future__ import annotations

import json
from pathlib import Path

from stock_analyzer.models.registry_reconciliation import (
    KIND_HASH_MISMATCH,
    KIND_LEGACY_ALIAS_POINTER,
    KIND_LEGACY_EMPTY_HASH,
    KIND_LEGACY_MANIFEST_POINTER,
    KIND_OK,
    KIND_UNRECOVERABLE_ARTIFACT,
    build_reconciliation_report,
    classify_record,
    is_model_artifact_payload,
)
from stock_analyzer.models.serving_manifest import (
    DEFAULT_SERVING_MANIFEST_PATH,
    build_serving_manifest,
    read_serving_manifest,
    write_serving_manifest,
)

_ROOT = Path(__file__).resolve().parents[1]

_ARTIFACT_CREATED_AT = "2026-08-16T10:00:00"


def _write_model_artifact(path: Path, *, created_at: str = _ARTIFACT_CREATED_AT) -> Path:
    payload = {
        "version": "v2",
        "created_at": created_at,
        "feature_schema_id": "fs_v1",
        "feature_schema_hash": "schema-hash-1",
        "label_policy_id": "label_policy_v1_e2afc1135a3f",
        "label_policy_hash": "label-hash-1",
        "dataset_manifest_id": "dataset_manifest_1",
        "feature_columns": ["close", "ret_5d"],
        "lgbm_model": {},
        "xgb_model": {},
        "lgbm_calibrator": {},
        "xgb_calibrator": {},
        "training_metrics": {"auc": 0.5},
        "metadata": {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _write_dataset_manifest(path: Path) -> Path:
    """数据集清单：没有模型载荷（正是历史坏记录指向的那类文件）。"""
    path.write_text(
        json.dumps(
            {
                "manifest_id": "dataset_manifest_1",
                "rows": 1000,
                "split": {"train": 800, "test": 200},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


class _RecordStub:
    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


class _RegistryStub:
    def __init__(self, record: object | None = None) -> None:
        self._record = record

    def active_champion(self, *, suppress_read_errors: bool = False) -> object | None:
        _ = suppress_read_errors
        return self._record

    def list_records(
        self, *, limit: int | None = None, suppress_read_errors: bool = False
    ) -> list[object]:
        _ = (limit, suppress_read_errors)
        return [self._record] if self._record is not None else []


# ---------------------------------------------------------------------------
# 1. 新写入：URI 必须是模型工件
# ---------------------------------------------------------------------------


def test_model_artifact_payload_detection() -> None:
    assert is_model_artifact_payload(
        {"lgbm_model": {}, "xgb_model": {}, "feature_columns": ["close"]}
    )
    # 数据集清单 / 没有特征列的 JSON 一律不算模型工件
    assert not is_model_artifact_payload({"manifest_id": "d1", "rows": 10})
    assert not is_model_artifact_payload({"lgbm_model": {}, "xgb_model": {}, "feature_columns": []})
    assert not is_model_artifact_payload("not-a-dict")


def test_dataset_manifest_pointer_is_flagged_not_accepted(tmp_path: Path) -> None:
    manifest = _write_dataset_manifest(tmp_path / "dataset_manifest.json")
    entry = classify_record(
        {
            "model_id": "m_legacy",
            "artifact_uri": str(manifest),
            "artifact_content_hash": "a" * 64,
            "lifecycle_state": "trained",
        }
    )
    assert entry.kind == KIND_LEGACY_MANIFEST_POINTER
    assert "数据集清单" in entry.detail


def test_ok_record_hash_recomputes(tmp_path: Path) -> None:
    from stock_analyzer.models.bundle import compute_artifact_identity_hash

    artifact = _write_model_artifact(tmp_path / "model_v2_abc" / "model_v1.json")
    digest = compute_artifact_identity_hash(artifact)
    entry = classify_record(
        {
            "model_id": "m_ok",
            "artifact_uri": str(artifact),
            "artifact_content_hash": digest,
            "lifecycle_state": "champion",
        }
    )
    assert entry.kind == KIND_OK
    assert entry.actual_content_hash == digest
    assert entry.artifact_created_at == _ARTIFACT_CREATED_AT


def test_dataset_manifest_id_is_referenced_separately(tmp_path: Path) -> None:
    """数据集清单只以 id 引用，不进 artifact_uri（工件与服务端身份分离）。"""
    artifact = _write_model_artifact(tmp_path / "model.json")
    manifest = build_serving_manifest(artifact_path=artifact, registry=_RegistryStub())
    assert manifest["serving"]["dataset_manifest_id"] == "dataset_manifest_1"
    assert manifest["serving"]["artifact_path"] == str(artifact)
    assert "dataset_manifest" not in Path(str(manifest["serving"]["artifact_path"])).name


# ---------------------------------------------------------------------------
# 2. 历史坏记录：只标注，不伪造
# ---------------------------------------------------------------------------


def test_legacy_empty_hash_marked(tmp_path: Path) -> None:
    artifact = _write_model_artifact(tmp_path / "model.json")
    entry = classify_record(
        {"model_id": "m_empty", "artifact_uri": str(artifact), "artifact_content_hash": ""}
    )
    assert entry.kind == KIND_LEGACY_EMPTY_HASH
    assert entry.actual_content_hash != ""


def test_legacy_alias_pointer_marked(tmp_path: Path) -> None:
    alias = _write_model_artifact(tmp_path / "model_v1.json")
    from stock_analyzer.models.bundle import compute_artifact_identity_hash

    entry = classify_record(
        {
            "model_id": "m_alias",
            "artifact_uri": str(alias),
            "artifact_content_hash": compute_artifact_identity_hash(alias),
        },
        alias_paths=[str(alias)],
    )
    assert entry.kind == KIND_LEGACY_ALIAS_POINTER
    assert "可变别名" in entry.detail


def test_unrecoverable_artifact_marked(tmp_path: Path) -> None:
    entry = classify_record(
        {
            "model_id": "m_gone",
            "artifact_uri": str(tmp_path / "gone.json"),
            "artifact_content_hash": "b" * 64,
        }
    )
    assert entry.kind == KIND_UNRECOVERABLE_ARTIFACT


def test_hash_mismatch_is_not_silently_accepted(tmp_path: Path) -> None:
    artifact = _write_model_artifact(tmp_path / "model.json")
    entry = classify_record(
        {"model_id": "m_bad", "artifact_uri": str(artifact), "artifact_content_hash": "c" * 64}
    )
    assert entry.kind == KIND_HASH_MISMATCH
    assert "不同" in entry.detail


def test_reconciliation_report_counts_and_never_writes(tmp_path: Path) -> None:
    artifact = _write_model_artifact(tmp_path / "model.json")
    manifest = _write_dataset_manifest(tmp_path / "manifest.json")
    before = sorted(item.name for item in tmp_path.iterdir())
    report = build_reconciliation_report(
        [
            {"model_id": "ok", "artifact_uri": str(artifact), "artifact_content_hash": ""},
            {
                "model_id": "legacy",
                "artifact_uri": str(manifest),
                "artifact_content_hash": "d" * 64,
            },
            {"model_id": "gone", "artifact_uri": str(tmp_path / "nope.json")},
        ]
    )
    assert report["record_count"] == 3
    assert report["kind_counts"][KIND_LEGACY_EMPTY_HASH] == 1
    assert report["kind_counts"][KIND_LEGACY_MANIFEST_POINTER] == 1
    assert report["kind_counts"][KIND_UNRECOVERABLE_ARTIFACT] == 1
    # 只读：目录内容一字未改
    assert sorted(item.name for item in tmp_path.iterdir()) == before
    assert "只读" in str(report["authority_note"])


# ---------------------------------------------------------------------------
# 3. Serving manifest
# ---------------------------------------------------------------------------


def test_serving_manifest_records_facts_and_authority(tmp_path: Path) -> None:
    from stock_analyzer.models.bundle import compute_artifact_identity_hash

    artifact = _write_model_artifact(tmp_path / "model_v1.json")
    digest = compute_artifact_identity_hash(artifact)
    manifest = build_serving_manifest(
        artifact_path=artifact,
        registry=_RegistryStub(
            _RecordStub(model_id="m1", artifact_content_hash=digest, lifecycle_state="champion")
        ),
        alias_path=artifact,
        source="unit_test",
        generated_at="2026-09-18T00:00:00+08:00",
    )
    assert manifest["schema"] == "model_serving_manifest.v1"
    assert manifest["serving"]["artifact_content_hash"] == digest
    assert manifest["serving"]["artifact_created_at"] == _ARTIFACT_CREATED_AT
    assert manifest["registry"]["model_id"] == "m1"
    assert manifest["registry"]["identity_verified"] is True
    # N5：权威口径必须显式——alias 不是身份真相源
    assert manifest["authority"]["alias_is_identity_source"] is False
    assert manifest["authority"]["authoritative_content_hash"] == digest


def test_serving_manifest_write_read_round_trip(tmp_path: Path) -> None:
    artifact = _write_model_artifact(tmp_path / "model_v1.json")
    target = tmp_path / "model_serving_manifest.json"
    result = write_serving_manifest(
        artifact_path=artifact,
        registry=_RegistryStub(),
        manifest_path=target,
        generated_at="2026-09-18T00:00:00+08:00",
    )
    assert result["written"] is True
    loaded = read_serving_manifest(target)
    assert loaded is not None
    assert loaded["generated_at"] == "2026-09-18T00:00:00+08:00"


def test_serving_manifest_missing_artifact_is_reported_not_raised(tmp_path: Path) -> None:
    target = tmp_path / "model_serving_manifest.json"
    result = write_serving_manifest(
        artifact_path=tmp_path / "missing.json", registry=_RegistryStub(), manifest_path=target
    )
    # 工件缺失仍要写出清单（身份缺失必须可见）
    assert result["written"] is True
    payload = result["payload"]
    assert payload["serving"]["artifact_exists"] is False
    assert payload["research_fail_closed"] is True


def test_serving_manifest_default_path_matches_spec() -> None:
    assert DEFAULT_SERVING_MANIFEST_PATH == "artifacts/model_serving_manifest.json"


def test_serving_manifest_write_failure_is_best_effort(tmp_path: Path, monkeypatch) -> None:
    """写盘失败不得抛出（发布流程不能被身份清单写失败打死），但要如实返回错误。"""
    artifact = _write_model_artifact(tmp_path / "model_v1.json")

    import stock_analyzer.models.serving_manifest as module

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(module, "write_json_atomic", _boom)
    result = module.write_serving_manifest(
        artifact_path=artifact, registry=_RegistryStub(), manifest_path=tmp_path / "m.json"
    )
    assert result["written"] is False
    assert "write_failed" in str(result["error"])


# ---------------------------------------------------------------------------
# 4. 归档：容量管理（保留下限 + 字节预算）
# ---------------------------------------------------------------------------


def _make_bundle(root: Path, name: str, *, size_bytes: int, mtime: float) -> Path:
    import os

    from stock_analyzer.models.bundle import ARTIFACT_FILENAME

    bundle = root / name
    bundle.mkdir(parents=True)
    payload = {
        "version": "v2",
        "created_at": _ARTIFACT_CREATED_AT,
        "feature_columns": ["close"],
        "lgbm_model": {},
        "xgb_model": {},
        "lgbm_calibrator": {},
        "xgb_calibrator": {},
        "training_metrics": {},
    }
    (bundle / ARTIFACT_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    padding = bundle / "sidecar.bin"
    padding.write_bytes(b"\0" * size_bytes)
    os.utime(bundle, (mtime, mtime))
    os.utime(padding, (mtime, mtime))
    return bundle


def test_prune_keeps_floor_even_when_over_budget(tmp_path: Path) -> None:
    from stock_analyzer.models.bundle import prune_model_bundle_archive

    for index in range(4):
        _make_bundle(tmp_path, f"model_v2_{index}", size_bytes=2000, mtime=1000 + index)
    removed = prune_model_bundle_archive(
        tmp_path, retention_count=2, max_total_bytes=1  # 预算极小：只保留下限
    )
    remaining = sorted(item.name for item in tmp_path.glob("model_v2_*"))
    assert len(remaining) == 2  # 保留下限 2 个最新（index 3, 2）
    assert len(removed) == 2
    assert all(name in remaining for name in ("model_v2_3", "model_v2_2"))


def test_prune_protected_bundles_survive_budget_pressure(tmp_path: Path) -> None:
    from stock_analyzer.models.bundle import prune_model_bundle_archive

    for index in range(3):
        _make_bundle(tmp_path, f"model_v2_{index}", size_bytes=2000, mtime=1000 + index)
    removed = prune_model_bundle_archive(
        tmp_path,
        retention_count=1,
        protected_bundle_ids={"model_v2_0"},  # 最旧，但在 protected 里
        max_total_bytes=1,
    )
    remaining = sorted(item.name for item in tmp_path.glob("model_v2_*"))
    assert "model_v2_0" in remaining
    assert "model_v2_2" in remaining
    assert "model_v2_1" in removed


def test_prune_without_budget_keeps_legacy_count_semantics(tmp_path: Path) -> None:
    """不传预算时行为与既有 count 语义一致（Legacy 回归保护）。"""
    from stock_analyzer.models.bundle import prune_model_bundle_archive

    for index in range(4):
        _make_bundle(tmp_path, f"model_v2_{index}", size_bytes=10, mtime=1000 + index)
    removed = prune_model_bundle_archive(tmp_path, retention_count=2)
    assert len(removed) == 2
    assert len(list(tmp_path.glob("model_v2_*"))) == 2
