"""Model Registry 与磁盘工件的对账（S05 / 原 P0-02）。

**只读、只报告、不改历史记录**（阶段施工提示词 S05 明确要求）：

- 历史坏记录不得伪造修复，只能分类标注；
- 分类：（见 :data:`RECONCILIATION_KINDS`）

```text
ok                          artifact_uri 是真模型工件，内容哈希可重算且与登记一致
hash_mismatch               内容哈希可重算但与登记值不同（换件/篡改/登错）
legacy_manifest_pointer     artifact_uri 指向数据集清单等非模型 JSON
legacy_empty_hash           登记的内容哈希为空（无法验证）
legacy_alias_pointer        artifact_uri 指向可变别名文件（不是 immutable bundle）
unrecoverable_artifact      artifact_uri 在磁盘上不存在，且归档里也没有同哈希 bundle
```

对账结论供 S06 的 HistoricalModelResolver 与 S05 的治理动作消费：只有 ``ok``
（以及 ``hash_mismatch`` 明确排除后）才允许被当作可信身份。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from stock_analyzer.models.artifact import ModelArtifact
from stock_analyzer.models.bundle import ARTIFACT_FILENAME, compute_artifact_identity_hash

KIND_OK = "ok"
KIND_HASH_MISMATCH = "hash_mismatch"
KIND_LEGACY_MANIFEST_POINTER = "legacy_manifest_pointer"
KIND_LEGACY_EMPTY_HASH = "legacy_empty_hash"
KIND_LEGACY_ALIAS_POINTER = "legacy_alias_pointer"
KIND_UNRECOVERABLE_ARTIFACT = "unrecoverable_artifact"

RECONCILIATION_KINDS = (
    KIND_OK,
    KIND_HASH_MISMATCH,
    KIND_LEGACY_MANIFEST_POINTER,
    KIND_LEGACY_EMPTY_HASH,
    KIND_LEGACY_ALIAS_POINTER,
    KIND_UNRECOVERABLE_ARTIFACT,
)

# 数据集清单（manifest）与模型工件的判别字段：模型工件必须有模型载荷与特征列。
_MODEL_PAYLOAD_KEYS = ("lgbm_model", "xgb_model", "feature_columns")


def _text(value: object) -> str:
    return str(value or "").strip()


def is_model_artifact_payload(payload: object) -> bool:
    """JSON payload 是否是**模型工件**（而不是数据集清单/其它 JSON）。

    判据是"必须携带模型载荷"：数据集清单没有 ``lgbm_model``/``feature_columns``，
    这条判据正是 S05"新写入 registry 的 URI 必须真指向模型工件"的可执行形式。
    """
    if not isinstance(payload, Mapping):
        return False
    if not all(key in payload for key in _MODEL_PAYLOAD_KEYS):
        return False
    feature_columns = payload.get("feature_columns")
    return isinstance(feature_columns, list) and bool(feature_columns)


@dataclass(frozen=True, slots=True)
class ReconciliationEntry:
    """单条 registry 记录的对账结果（事实 + 分类，不含任何修复动作）。"""

    model_id: str
    artifact_uri: str
    kind: str
    detail: str
    registered_content_hash: str = ""
    actual_content_hash: str = ""
    artifact_created_at: str = ""
    lifecycle_state: str = ""

    def to_payload(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "artifact_uri": self.artifact_uri,
            "kind": self.kind,
            "detail": self.detail,
            "registered_content_hash": self.registered_content_hash,
            "actual_content_hash": self.actual_content_hash,
            "artifact_created_at": self.artifact_created_at,
            "lifecycle_state": self.lifecycle_state,
        }


def classify_record(
    record: Mapping[str, object],
    *,
    alias_paths: Sequence[str] = (),
    bundle_hash_index: Mapping[str, str] | None = None,
) -> ReconciliationEntry:
    """把一条 registry 记录分类（只读磁盘，不写任何东西）。"""
    model_id = _text(record.get("model_id"))
    uri = _text(record.get("artifact_uri"))
    registered_hash = _text(record.get("artifact_content_hash"))
    lifecycle_state = _text(record.get("lifecycle_state"))
    normalized_alias_paths = {_text(item) for item in alias_paths if _text(item)}

    if not uri:
        return ReconciliationEntry(
            model_id=model_id,
            artifact_uri="",
            kind=KIND_UNRECOVERABLE_ARTIFACT,
            detail="registry 记录没有 artifact_uri",
            registered_content_hash=registered_hash,
            lifecycle_state=lifecycle_state,
        )

    path = Path(uri).expanduser()
    if not path.exists():
        # 磁盘找不到：若归档里存在同哈希 bundle，仍可恢复（这里只标注，不搬文件）。
        index = bundle_hash_index or {}
        if registered_hash and registered_hash.lower() in {
            value.lower() for value in index.values()
        }:
            return ReconciliationEntry(
                model_id=model_id,
                artifact_uri=uri,
                kind=KIND_LEGACY_ALIAS_POINTER,
                detail="artifact_uri 不存在，但归档内有同哈希 bundle（可变别名/路径漂移）",
                registered_content_hash=registered_hash,
                lifecycle_state=lifecycle_state,
            )
        return ReconciliationEntry(
            model_id=model_id,
            artifact_uri=uri,
            kind=KIND_UNRECOVERABLE_ARTIFACT,
            detail="artifact_uri 在磁盘上不存在，归档内也没有同哈希 bundle",
            registered_content_hash=registered_hash,
            lifecycle_state=lifecycle_state,
        )

    try:
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 对账不得抛出
        return ReconciliationEntry(
            model_id=model_id,
            artifact_uri=uri,
            kind=KIND_UNRECOVERABLE_ARTIFACT,
            detail=f"artifact 不可读/不可解析: {type(exc).__name__}",
            registered_content_hash=registered_hash,
            lifecycle_state=lifecycle_state,
        )

    if not is_model_artifact_payload(payload):
        return ReconciliationEntry(
            model_id=model_id,
            artifact_uri=uri,
            kind=KIND_LEGACY_MANIFEST_POINTER,
            detail="artifact_uri 指向的不是模型工件（疑似数据集清单/其它 JSON）",
            registered_content_hash=registered_hash,
            lifecycle_state=lifecycle_state,
        )

    artifact_created_at = ""
    try:
        artifact_created_at = _text(ModelArtifact.load(path).created_at)
    except Exception:  # noqa: BLE001 - created_at 只是附加信息
        artifact_created_at = ""

    actual_hash = ""
    try:
        actual_hash = _text(compute_artifact_identity_hash(path))
    except Exception as exc:  # noqa: BLE001 - 哈希算不出来属不可验证
        return ReconciliationEntry(
            model_id=model_id,
            artifact_uri=uri,
            kind=KIND_UNRECOVERABLE_ARTIFACT,
            detail=f"内容哈希无法重算: {type(exc).__name__}",
            registered_content_hash=registered_hash,
            artifact_created_at=artifact_created_at,
            lifecycle_state=lifecycle_state,
        )

    if not registered_hash:
        return ReconciliationEntry(
            model_id=model_id,
            artifact_uri=uri,
            kind=KIND_LEGACY_EMPTY_HASH,
            detail="登记的内容哈希为空（无法与实算哈希对账）",
            actual_content_hash=actual_hash,
            artifact_created_at=artifact_created_at,
            lifecycle_state=lifecycle_state,
        )
    if uri in normalized_alias_paths:
        return ReconciliationEntry(
            model_id=model_id,
            artifact_uri=uri,
            kind=KIND_LEGACY_ALIAS_POINTER,
            detail="artifact_uri 指向可变别名文件（不是 immutable bundle 路径）",
            registered_content_hash=registered_hash,
            actual_content_hash=actual_hash,
            artifact_created_at=artifact_created_at,
            lifecycle_state=lifecycle_state,
        )
    if registered_hash.lower() != actual_hash.lower():
        return ReconciliationEntry(
            model_id=model_id,
            artifact_uri=uri,
            kind=KIND_HASH_MISMATCH,
            detail=(
                "登记哈希与实算哈希不同（换件/篡改/登记错误）："
                f"registered={registered_hash[:12]}… actual={actual_hash[:12]}…"
            ),
            registered_content_hash=registered_hash,
            actual_content_hash=actual_hash,
            artifact_created_at=artifact_created_at,
            lifecycle_state=lifecycle_state,
        )
    return ReconciliationEntry(
        model_id=model_id,
        artifact_uri=uri,
        kind=KIND_OK,
        detail="artifact_uri 是真模型工件，实算哈希与登记一致",
        registered_content_hash=registered_hash,
        actual_content_hash=actual_hash,
        artifact_created_at=artifact_created_at,
        lifecycle_state=lifecycle_state,
    )


def build_reconciliation_report(
    records: Iterable[Mapping[str, object]],
    *,
    alias_paths: Sequence[str] = (),
    archive_root: str | Path | None = None,
) -> dict[str, object]:
    """对账报告（含分类计数 + 归档清单）；**不做任何修改**。"""
    bundle_hash_index: dict[str, str] = {}
    bundle_ids: list[str] = []
    if archive_root is not None:
        root = Path(archive_root).expanduser()
        if root.is_dir():
            for item in sorted(root.glob("model_v2_*")):
                artifact_file = item / ARTIFACT_FILENAME
                bundle_ids.append(item.name)
                try:
                    bundle_hash_index[item.name] = compute_artifact_identity_hash(artifact_file)
                except Exception:  # noqa: BLE001 - 算不出来就不进索引
                    continue

    entries = [
        classify_record(
            record,
            alias_paths=alias_paths,
            bundle_hash_index=bundle_hash_index,
        )
        for record in records
    ]
    counts: dict[str, int] = {kind: 0 for kind in RECONCILIATION_KINDS}
    for entry in entries:
        counts[entry.kind] = counts.get(entry.kind, 0) + 1
    return {
        "record_count": len(entries),
        "kind_counts": counts,
        "archive_bundle_count": len(bundle_ids),
        "archive_bundles": sorted(bundle_ids),
        "entries": [entry.to_payload() for entry in entries],
        "authority_note": (
            "本报告只读：历史坏记录一律标注，不伪造修复。"
            "在服身份以 artifacts/model_serving_manifest.json 的内容哈希为准；"
            "仓库内受跟踪的开发工件不构成在服身份（Codex N5）。"
        ),
    }


__all__ = [
    "KIND_HASH_MISMATCH",
    "KIND_LEGACY_ALIAS_POINTER",
    "KIND_LEGACY_EMPTY_HASH",
    "KIND_LEGACY_MANIFEST_POINTER",
    "KIND_OK",
    "KIND_UNRECOVERABLE_ARTIFACT",
    "RECONCILIATION_KINDS",
    "ReconciliationEntry",
    "build_reconciliation_report",
    "classify_record",
    "is_model_artifact_payload",
]
