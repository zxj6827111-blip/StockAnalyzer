"""Alpha V2 Validation Epoch 注册表（M3 §4）。

**核心不变量**：真实 OOS 数据必须按 epoch 分桶报告。一个 epoch 冻结一份
validation freeze manifest；模型/代码/配置/基准有实质变化时**不得**把新数据
并入旧 epoch，而是：

```text
关闭 epoch_N（记录结束原因） -> 修复/重新验收 -> 开启 epoch_N+1
```

注册表文件 ``artifacts/alpha_v2/validation/epochs.json`` 是**追加式账本**：
记录只增不改（关闭 = 在原记录上补 closed_* 字段，这本身也是一次新的原子写，
旧哈希留在 ``history`` 里）。``days_summary`` 由 KPI/成熟任务更新。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from stock_analyzer.alpha_v2.artifacts import write_json_atomic
from stock_analyzer.alpha_v2.validation.freeze import VALIDATION_DIRNAME

EPOCH_REGISTRY_SCHEMA = "alpha_v2_validation_epochs.v1"
EPOCH_REGISTRY_FILENAME = "epochs.json"
EPOCH_ID_PATTERN = re.compile(r"^alpha_v2_epoch_\d{3,}$")

EPOCH_OPEN = "open"
EPOCH_CLOSED = "closed"

# epoch 级冻结对象的键：任何一项变化都必须关旧开新。
FROZEN_IDENTITY_KEYS: tuple[str, ...] = (
    "code_commit",
    "config_hash",
    "model_id",
    "model_artifact_hash",
    "feature_schema_hash",
    "label_policy_hash",
    "selection_contract_id",
    "execution_price_mode",
)

# 快照行自带的身份子集（快照/成熟写入侧只能校验这些；全量 8 键在 CLI 层校验）。
ROW_IDENTITY_KEYS: tuple[str, ...] = (
    "code_commit",
    "config_hash",
    "model_id",
    "model_artifact_hash",
    "selection_contract_id",
)


class EpochRegistryError(RuntimeError):
    """epoch 规则违例（双开/重复 id/关已关 epoch 等）的显式失败。"""


@dataclass(frozen=True, slots=True)
class EpochRecord:
    epoch_id: str
    status: str
    opened_at: str
    opened_on_date: str  # 首个 shadow 交易日（validation_start_date）
    freeze_manifest_hash: str
    identity: dict[str, object] = field(default_factory=dict)
    closed_at: str = ""
    close_reason: str = ""
    days: dict[str, object] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "epoch_id": self.epoch_id,
            "status": self.status,
            "opened_at": self.opened_at,
            "opened_on_date": self.opened_on_date,
            "freeze_manifest_hash": self.freeze_manifest_hash,
            "identity": dict(self.identity),
            "closed_at": self.closed_at,
            "close_reason": self.close_reason,
            "days": dict(self.days),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> EpochRecord:
        return cls(
            epoch_id=str(payload.get("epoch_id", "")),
            status=str(payload.get("status", "")),
            opened_at=str(payload.get("opened_at", "")),
            opened_on_date=str(payload.get("opened_on_date", "")),
            freeze_manifest_hash=str(payload.get("freeze_manifest_hash", "")),
            identity=dict(payload.get("identity", {}) or {}),  # type: ignore[arg-type]
            closed_at=str(payload.get("closed_at", "")),
            close_reason=str(payload.get("close_reason", "")),
            days=dict(payload.get("days", {}) or {}),  # type: ignore[arg-type]
        )


def _registry_path(root: str | Path) -> Path:
    return Path(root) / VALIDATION_DIRNAME / EPOCH_REGISTRY_FILENAME


def epoch_dir(root: str | Path, epoch_id: str) -> Path:
    """``validation/<epoch_id>/``（各 epoch 的 shadow/outcomes/reports 都在其下）。"""
    return Path(root) / VALIDATION_DIRNAME / epoch_id


def epoch_subdirs(root: str | Path, epoch_id: str) -> dict[str, Path]:
    base = epoch_dir(root, epoch_id)
    return {
        "shadow": base / "shadow",
        "outcomes": base / "outcomes",
        "reports": base / "reports",
        "manifests": base / "manifests",
        "missing": base / "missing",
    }


def load_epoch_registry(
    root: str | Path,
) -> tuple[dict[str, EpochRecord], list[dict[str, object]]]:
    """读取注册表；返回 ``(epochs, history)``，文件不存在 => 两个都是空结构。"""
    path = _registry_path(root)
    epochs: dict[str, EpochRecord] = {}
    history: list[dict[str, object]] = []
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise EpochRegistryError(f"epoch 注册表损坏: {path}: {exc}") from exc
        for raw in payload.get("epochs", []):
            record = EpochRecord.from_payload(raw)
            epochs[record.epoch_id] = record
        history = [dict(item) for item in payload.get("history", [])]
    return epochs, history


def _write_registry(
    root: str | Path,
    epochs: Mapping[str, EpochRecord],
    history: list[dict[str, object]],
) -> Path:
    payload = {
        "schema": EPOCH_REGISTRY_SCHEMA,
        "updated_at": datetime.now().astimezone().isoformat(),
        "epochs": [epochs[key].to_payload() for key in sorted(epochs)],
        "history": [
            _history_item(item) for item in history
        ],
    }
    path = _registry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_json_atomic(path, payload)


def _history_item(entry: Mapping[str, object]) -> dict[str, object]:
    item = dict(entry)
    item.setdefault("history_hash", _record_hash(item))
    return item


def _record_hash(payload: Mapping[str, object]) -> str:
    body = {k: v for k, v in payload.items() if k != "history_hash"}
    return hashlib.sha256(
        json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]


def open_epoch(
    *,
    root: str | Path,
    epoch_id: str,
    freeze_manifest_hash: str,
    identity: Mapping[str, object],
    opened_on_date: str,
    opened_at: str | None = None,
) -> EpochRecord:
    """开启新 epoch。**同时刻只允许一个 open epoch**（双开 = 口径混淆）。"""
    if not EPOCH_ID_PATTERN.match(str(epoch_id)):
        raise EpochRegistryError(
            f"epoch_id 必须匹配 {EPOCH_ID_PATTERN.pattern}，收到 {epoch_id!r}"
        )
    if not str(freeze_manifest_hash).strip():
        raise EpochRegistryError(
                "open_epoch 需要 freeze_manifest_hash（epoch 必须锚定一份冻结清单）"
            )
    if not str(opened_on_date).strip():
        raise EpochRegistryError("open_epoch 需要 opened_on_date（validation_start_date）")
    epochs, history = load_epoch_registry(root)
    active = [record for record in epochs.values() if record.status == EPOCH_OPEN]
    if active:
        raise EpochRegistryError(
            f"已有开放 epoch（{', '.join(r.epoch_id for r in active)}）；"
            "先关闭它（带原因），再开新 epoch"
        )
    existing = epochs.get(epoch_id)
    if existing is not None:
        raise EpochRegistryError(f"epoch_id 已存在（{epoch_id}）；不允许复用同一名称重开")
    record = EpochRecord(
        epoch_id=epoch_id,
        status=EPOCH_OPEN,
        opened_at=str(opened_at or datetime.now().astimezone().isoformat()),
        opened_on_date=str(opened_on_date),
        freeze_manifest_hash=str(freeze_manifest_hash),
        identity=dict(identity),
    )
    epochs[epoch_id] = record
    history.append(
        {
            "event": "epoch_opened",
            "epoch_id": epoch_id,
            "at": record.opened_at,
            "freeze_manifest_hash": record.freeze_manifest_hash,
        }
    )
    _write_registry(root, epochs, history)
    return record


def close_epoch(
    *,
    root: str | Path,
    epoch_id: str,
    reason: str,
    closed_at: str | None = None,
    days: Mapping[str, object] | None = None,
) -> EpochRecord:
    """关闭 epoch（**必须给原因**）：关闭后再向它写数据会被 run 侧守卫拒绝。"""
    epochs, history = load_epoch_registry(root)
    record = epochs.get(epoch_id)
    if record is None:
        raise EpochRegistryError(f"epoch 不存在: {epoch_id}")
    if record.status != EPOCH_OPEN:
        raise EpochRegistryError(f"epoch {epoch_id} 已关闭（{record.closed_at}），不能重复关闭")
    reason_text = str(reason).strip()
    if not reason_text:
        raise EpochRegistryError("close_epoch 必须给出原因（比如 blocking bug id / 新配置哈希）")
    stamp = str(closed_at or datetime.now().astimezone().isoformat())
    closed = EpochRecord(
        epoch_id=record.epoch_id,
        status=EPOCH_CLOSED,
        opened_at=record.opened_at,
        opened_on_date=record.opened_on_date,
        freeze_manifest_hash=record.freeze_manifest_hash,
        identity=record.identity,
        closed_at=stamp,
        close_reason=reason_text,
        days=dict(days or record.days),
    )
    epochs[epoch_id] = closed
    history.append(
        {
            "event": "epoch_closed",
            "epoch_id": epoch_id,
            "at": stamp,
            "reason": reason_text,
        }
    )
    _write_registry(root, epochs, history)
    return closed


def active_epoch(root: str | Path) -> EpochRecord | None:
    epochs, _ = load_epoch_registry(root)
    for record in epochs.values():
        if record.status == EPOCH_OPEN:
            return record
    return None


def get_epoch(root: str | Path, epoch_id: str) -> EpochRecord | None:
    epochs, _ = load_epoch_registry(root)
    return epochs.get(epoch_id)


def assert_epoch_open(record: EpochRecord | None, *, epoch_id: str = "") -> EpochRecord:
    """shadow 写入/成熟任务的统一前置闸门。"""
    if record is None:
        raise EpochRegistryError(f"epoch 不存在或注册表不可读: {epoch_id or '<未指定>'}")
    if record.status != EPOCH_OPEN:
        raise EpochRegistryError(
            f"epoch {record.epoch_id} 已关闭（{record.closed_at or '?'}，原因："
            f"{record.close_reason or '未记录'}）；已关闭 epoch 不接受新数据"
        )
    return record


def update_epoch_days(
    *, root: str | Path, epoch_id: str, days: Mapping[str, object]
) -> EpochRecord:
    """由成熟/KPI 任务回写 days 汇总（样本量记账属于 epoch，不是新口径）。"""
    epochs, history = load_epoch_registry(root)
    record = epochs.get(epoch_id)
    if record is None:
        raise EpochRegistryError(f"epoch 不存在: {epoch_id}")
    updated = EpochRecord(
        epoch_id=record.epoch_id,
        status=record.status,
        opened_at=record.opened_at,
        opened_on_date=record.opened_on_date,
        freeze_manifest_hash=record.freeze_manifest_hash,
        identity=record.identity,
        closed_at=record.closed_at,
        close_reason=record.close_reason,
        days=dict(days),
    )
    epochs[epoch_id] = updated
    _write_registry(root, epochs, history)
    return updated


def require_open_epoch(root: str | Path, epoch_id: str) -> EpochRecord:
    """以**注册表**为准的打开态核验（不信任调用方持有的可能是过期的内存对象）。

    写入方（快照/成熟/清单）必须用本函数，而不是 ``assert_epoch_open(内存对象)``：
    进程内对象先拿到 record 再关 epoch 的场景（长驻调度器）下，只有重读注册表
    才能挡住"向已关闭 epoch 写数据"。
    """
    record = get_epoch(root, epoch_id)
    return assert_epoch_open(record, epoch_id=epoch_id)


def epoch_identity_matches(
    record: EpochRecord,
    identity: Mapping[str, object],
    *,
    keys: tuple[str, ...] | None = None,
) -> list[str]:
    """epoch 记录的冻结身份 vs 运行侧身份的对账（**严格**：任一侧缺失即违例）。

    修复轮为对抗"epoch identity 可漂移"而把语义收紧为 fail-closed：
    expected 缺失 / actual 缺失 / 两边不一致，都算是违例——不允许
    "只有两边都有值才比较"的 fail-open 行为。``keys`` 可按调用方实际
    拥有的子集收窄（如行级身份只能给出 :data:`ROW_IDENTITY_KEYS`）。
    """
    check_keys: tuple[str, ...] = tuple(keys) if keys else FROZEN_IDENTITY_KEYS
    violations: list[str] = []
    for key in check_keys:
        expected = str(record.identity.get(key, "") or "").strip()
        actual = str(identity.get(key, "") or "").strip()
        if not expected:
            violations.append(f"{key}:epoch_identity_missing")
            continue
        if not actual:
            violations.append(f"{key}:runtime_identity_missing")
            continue
        if expected != actual:
            violations.append(f"{key}:{expected[:12]}!={actual[:12]}")
    return violations


def require_epoch_identity_match(
    *,
    root: str | Path,
    epoch_id: str,
    identity: Mapping[str, object] | None = None,
    keys: tuple[str, ...] | None = None,
    require_manifest_anchor: bool = True,
) -> EpochRecord:
    """所有 validation epoch 写入路径的统一身份闸门（**注册表为准**）。

    三道都过才放行，任一不过抛 :class:`EpochRegistryError`：

    1. epoch 存在且处于 open（重读注册表，不信内存对象）；
    2. **磁盘冻结清单仍是 epoch 锚定的那份**——``freeze_manifest_hash``
       逐位一致且清单内容自洽（被替换/被篡改都拦下）；``require_manifest_anchor``
       为 False 时跳过（多见于纯测试的矩阵装配场景，但生产路径必须 True）；
    3. ``identity`` 提供的身份键与 epoch 冻结身份逐项一致
       （缺失 = 违例，不是跳过）。
    """
    record = require_open_epoch(root, epoch_id)
    if require_manifest_anchor:
        from stock_analyzer.alpha_v2.validation.freeze import (  # 惰性导入避免循环
            load_validation_freeze,
            verify_freeze_integrity,
        )

        manifest = load_validation_freeze(root)
        if manifest is None:
            raise EpochRegistryError(
                f"validation freeze manifest 缺失（epoch {epoch_id} 无法证明锚定）"
            )
        recorded_hash = str(manifest.get("freeze_manifest_hash", "") or "")
        if not recorded_hash:
            raise EpochRegistryError("磁盘冻结清单缺少 freeze_manifest_hash")
        if recorded_hash != record.freeze_manifest_hash:
            raise EpochRegistryError(
                "磁盘冻结清单已被替换（"
                f"{recorded_hash[:12]}… != epoch 锚定 {record.freeze_manifest_hash[:12]}…）；"
                "冻结对象的正确动作是关闭当前 epoch 并开新 epoch，不是替换清单"
            )
        if not verify_freeze_integrity(manifest):
            raise EpochRegistryError("磁盘冻结清单内容与 freeze_manifest_hash 不一致（疑似篡改）")
    if identity is not None:
        violations = epoch_identity_matches(record, identity, keys=keys)
        if violations:
            raise EpochRegistryError(
                f"epoch {epoch_id} 的运行身份与冻结身份不符: " + "; ".join(violations)
            )
    return record


__all__ = [
    "EPOCH_CLOSED",
    "EPOCH_ID_PATTERN",
    "EPOCH_OPEN",
    "EPOCH_REGISTRY_FILENAME",
    "EPOCH_REGISTRY_SCHEMA",
    "FROZEN_IDENTITY_KEYS",
    "ROW_IDENTITY_KEYS",
    "EpochRecord",
    "EpochRegistryError",
    "active_epoch",
    "assert_epoch_open",
    "close_epoch",
    "epoch_dir",
    "epoch_identity_matches",
    "epoch_subdirs",
    "get_epoch",
    "load_epoch_registry",
    "open_epoch",
    "require_epoch_identity_match",
    "require_open_epoch",
    "update_epoch_days",
]
