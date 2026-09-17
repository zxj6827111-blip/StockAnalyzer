"""历史模型解析器（S06 / 原 P0-03）：as_of 之前**真实存在且可用**的模型。

**为什么必须有它**（蓝图 §2.9 / DF-S01-002）：在 S06 之前，历史回测直接加载
``config.training.artifact_path`` 指向的**当前在服**工件——只要 serving alias 更新过，
"回测 2026-09 用的却是 2026-10 训练的模型"就会静默发生。S01 只让身份可见，S06 才是
真正的时间闸门。

两种模式，语义严格区分（阶段施工提示词 S06）：

- ``strict_production_replay``：回答"当天真正生产激活的模型是谁"。
  需要**历史激活证据**（``promoted_at``/``activated_at`` ≤ 决策时刻）；没有证据就
  ``unscorable``——**禁止猜**。
- ``pit_research``：回答"as_of 之前真实存在、且训练数据时间合法的最近研究模型是谁"。
  以工件 ``created_at`` ≤ 决策时刻为准。

合法性条件（缺一即拒，全部记录原因）：
artifact 存在 / 实算哈希与登记一致 / created_at ≤ 决策时刻 / feature schema 与
label policy 可解析 / dataset manifest 存在 / manifest 无 blocking 质量标记 /
训练 outcome 已成熟 / 无未来特征可用性。

绝对禁止：``no eligible PIT model -> fallback current serving model``。
正确结果是 ``status=unscorable``、``reason=no_eligible_pit_model``。

时间语义：比较一律在 timezone-aware 域内进行；naive 时间戳按调用方给出的时区
假定（默认 Asia/Shanghai）解释并标注 ``assumed_local_timezone``；若调用方禁止假定，
则该候选以 ``time_semantics_unverified`` 被拒（fail-closed，而不是当它合法）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

MODE_PIT_RESEARCH = "pit_research"
MODE_STRICT_PRODUCTION_REPLAY = "strict_production_replay"
MODES = (MODE_PIT_RESEARCH, MODE_STRICT_PRODUCTION_REPLAY)

STATUS_RESOLVED = "resolved"
STATUS_UNSCORABLE = "unscorable"

# unscorable 原因（稳定契约）
REASON_NO_CANDIDATES = "no_candidates"
REASON_NO_ELIGIBLE_PIT_MODEL = "no_eligible_pit_model"
REASON_NO_ACTIVATION_EVIDENCE = "no_activation_evidence"
REASON_TIME_SEMANTICS_UNVERIFIED = "time_semantics_unverified"
REASON_MODE_UNSUPPORTED = "mode_unsupported"

# 单候选拒绝原因（前缀契约，便于聚合统计）
REJECT_NOT_YET_CREATED = "not_yet_created"
REJECT_CREATED_AFTER_DECISION = "created_after_decision"
REJECT_NOT_ACTIVATED_YET = "not_activated_yet"
REJECT_ARTIFACT_MISSING = "artifact_missing"
REJECT_HASH_UNVERIFIED = "hash_unverified"
REJECT_HASH_MISMATCH = "hash_mismatch"
REJECT_SCHEMA_UNKNOWN = "feature_schema_unknown"
REJECT_LABEL_UNKNOWN = "label_policy_unknown"
REJECT_MANIFEST_MISSING = "dataset_manifest_missing"
REJECT_MANIFEST_BLOCKING = "dataset_manifest_blocking"
REJECT_OUTCOMES_IMMATURE = "training_outcomes_immature"
REJECT_FUTURE_FEATURES = "future_feature_availability"
REJECT_TIME_SEMANTICS = "time_semantics_unverified"
REJECT_LIFECYCLE = "lifecycle_not_eligible"


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """解析结果（resolved 或 unscorable，两者都带完整证据）。"""

    status: str
    mode: str
    as_of: str
    decision_time: str
    reason: str = ""
    model_id: str = ""
    artifact_uri: str = ""
    artifact_content_hash: str = ""
    artifact_created_at: str = ""
    feature_schema_id: str = ""
    feature_schema_hash: str = ""
    label_policy_id: str = ""
    label_policy_hash: str = ""
    dataset_manifest_id: str = ""
    activated_at: str = ""
    time_semantics: str = ""
    candidates_considered: int = 0
    rejected: dict[str, str] = field(default_factory=dict)
    fallback_used: bool = False

    @property
    def scorable(self) -> bool:
        return self.status == STATUS_RESOLVED

    def to_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "mode": self.mode,
            "as_of": self.as_of,
            "decision_time": self.decision_time,
            "reason": self.reason,
            "model_id": self.model_id,
            "artifact_uri": self.artifact_uri,
            "artifact_content_hash": self.artifact_content_hash,
            "artifact_created_at": self.artifact_created_at,
            "feature_schema_id": self.feature_schema_id,
            "feature_schema_hash": self.feature_schema_hash,
            "label_policy_id": self.label_policy_id,
            "label_policy_hash": self.label_policy_hash,
            "dataset_manifest_id": self.dataset_manifest_id,
            "activated_at": self.activated_at,
            "time_semantics": self.time_semantics,
            "candidates_considered": self.candidates_considered,
            "rejected": dict(self.rejected),
            "fallback_used": self.fallback_used,
        }


def resolve_historical_model(
    *,
    as_of: datetime,
    candidates: Iterable[Mapping[str, object]],
    mode: str = MODE_PIT_RESEARCH,
    allowed_feature_schema_ids: Sequence[str] = (),
    allowed_label_policy_ids: Sequence[str] = (),
    assume_local_timezone: bool = True,
    local_timezone: str = "Asia/Shanghai",
) -> ResolvedModel:
    """按 as_of 解析可用历史模型；找不到就 ``unscorable``（绝不回退在服模型）。"""
    decision_time = _as_aware(as_of, assume_local_timezone=assume_local_timezone, tz=local_timezone)
    time_semantics = "aware" if _is_aware(as_of) else (
        "assumed_local_timezone" if assume_local_timezone else "unverified"
    )
    base = {
        "mode": str(mode),
        "as_of": _iso(as_of),
        "decision_time": _iso(decision_time) if decision_time is not None else "",
        "time_semantics": time_semantics,
    }
    normalized_mode = str(mode or "").strip()
    if normalized_mode not in MODES:
        return ResolvedModel(status=STATUS_UNSCORABLE, reason=REASON_MODE_UNSUPPORTED, **base)
    if decision_time is None:
        # 禁止假定时 naive 时间戳一律判不了 → 直接 unscorable（fail-closed）。
        return ResolvedModel(
            status=STATUS_UNSCORABLE, reason=REASON_TIME_SEMANTICS_UNVERIFIED, **base
        )

    allowed_schemas = {
        str(item).strip() for item in allowed_feature_schema_ids if str(item).strip()
    }
    allowed_labels = {
        str(item).strip() for item in allowed_label_policy_ids if str(item).strip()
    }

    rows = list(candidates)
    if not rows:
        return ResolvedModel(status=STATUS_UNSCORABLE, reason=REASON_NO_CANDIDATES, **base)

    eligible: list[tuple[datetime, Mapping[str, object], str]] = []
    rejected: dict[str, str] = {}
    for row in rows:
        model_id = _text(row.get("model_id")) or "(unknown)"
        reason = _reject_reason(
            row,
            decision_time=decision_time,
            mode=normalized_mode,
            allowed_schemas=allowed_schemas,
            allowed_labels=allowed_labels,
            assume_local_timezone=assume_local_timezone,
            local_timezone=local_timezone,
        )
        if reason:
            rejected[model_id] = reason
            continue
        anchor = _anchor_time(
            row,
            mode=normalized_mode,
            assume_local_timezone=assume_local_timezone,
            local_timezone=local_timezone,
        )
        if anchor is None:  # pragma: no cover - 已在 _reject_reason 里拦住
            rejected[model_id] = REJECT_TIME_SEMANTICS
            continue
        eligible.append((anchor, row, model_id))

    if not eligible:
        # 严格生产重放模式：全部候选都因"缺/未到激活证据"被拒 → 归 no_activation_evidence
        # （回答"没有历史激活证据"，而不是笼统的"没有可用模型"）。
        rejected_values = list(rejected.values())
        all_missing_evidence = bool(rejected_values) and all(
            value == REJECT_NOT_ACTIVATED_YET for value in rejected_values
        )
        reason = (
            REASON_NO_ACTIVATION_EVIDENCE
            if normalized_mode == MODE_STRICT_PRODUCTION_REPLAY and all_missing_evidence
            else REASON_NO_ELIGIBLE_PIT_MODEL
        )
        return ResolvedModel(
            status=STATUS_UNSCORABLE,
            reason=reason,
            candidates_considered=len(rows),
            rejected=rejected,
            **base,
        )

    eligible.sort(key=lambda item: (item[0], str(item[2])), reverse=True)
    anchor, chosen, model_id = eligible[0]
    if not _raw_timestamp_aware(chosen, mode=normalized_mode):
        # 决策时刻是 aware、但被选中的候选时间戳是 naive：身份时间语义只能靠
        # "假定本地时区"成立，必须如实标注（蓝图要求 time_semantics 可见）。
        base["time_semantics"] = "assumed_local_timezone"
    return ResolvedModel(
        status=STATUS_RESOLVED,
        model_id=model_id,
        artifact_uri=_text(chosen.get("artifact_uri")),
        artifact_content_hash=_text(chosen.get("artifact_content_hash")),
        artifact_created_at=_text(chosen.get("artifact_created_at")),
        feature_schema_id=_text(chosen.get("feature_schema_id")),
        feature_schema_hash=_text(chosen.get("feature_schema_hash")),
        label_policy_id=_text(chosen.get("label_policy_id")),
        label_policy_hash=_text(chosen.get("label_policy_hash")),
        dataset_manifest_id=_text(chosen.get("dataset_manifest_id")),
        activated_at=_text(chosen.get("activated_at") or chosen.get("promoted_at")),
        candidates_considered=len(rows),
        rejected=rejected,
        **base,
    )


def _raw_timestamp_aware(row: Mapping[str, object], *, mode: str) -> bool:
    """被选中候选用于排序的时间戳是否本身带时区（不做任何假定）。"""
    keys = ("activated_at", "promoted_at") if mode == MODE_STRICT_PRODUCTION_REPLAY else (
        "artifact_created_at",
    )
    for key in keys:
        value = row.get(key)
        if isinstance(value, datetime):
            return _is_aware(value)
        text = _text(value)
        if not text:
            continue
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).tzinfo is not None
        except ValueError:
            return False
    return False


def _reject_reason(
    row: Mapping[str, object],
    *,
    decision_time: datetime,
    mode: str,
    allowed_schemas: set[str],
    allowed_labels: set[str],
    assume_local_timezone: bool,
    local_timezone: str,
) -> str:
    """单候选的拒绝原因；返回空串表示通过。顺序即优先级（硬条件在前）。"""
    # N2：工件缺失与 registry 状态无关，恒为硬拒绝（先判，避免被状态掩盖）。
    exists = row.get("artifact_exists")
    if exists is not None and not bool(exists):
        return REJECT_ARTIFACT_MISSING
    if exists is None and not _text(row.get("artifact_uri")):
        return REJECT_ARTIFACT_MISSING

    hash_ok = row.get("content_hash_verified")
    identity_status = _text(row.get("identity_status"))
    if hash_ok is False or identity_status in {"mismatch", "loaded_hash_missing"}:
        return REJECT_HASH_MISMATCH if hash_ok is False or identity_status == "mismatch" else (
            REJECT_HASH_UNVERIFIED
        )
    if hash_ok is None and not _text(row.get("artifact_content_hash")):
        return REJECT_HASH_UNVERIFIED

    lifecycle = _text(row.get("lifecycle_state")).lower()
    if lifecycle in {"revoked", "blocked", "quarantined", "retired"}:
        return REJECT_LIFECYCLE

    if allowed_schemas:
        schema_id = _text(row.get("feature_schema_id"))
        if not schema_id or schema_id not in allowed_schemas:
            return REJECT_SCHEMA_UNKNOWN
    if allowed_labels:
        label_id = _text(row.get("label_policy_id"))
        if not label_id or label_id not in allowed_labels:
            return REJECT_LABEL_UNKNOWN

    if row.get("dataset_manifest_exists") is False:
        return REJECT_MANIFEST_MISSING
    if row.get("dataset_manifest_blocking"):
        return REJECT_MANIFEST_BLOCKING
    if row.get("training_outcomes_mature") is False:
        return REJECT_OUTCOMES_IMMATURE
    if row.get("future_feature_availability"):
        return REJECT_FUTURE_FEATURES

    if mode == MODE_STRICT_PRODUCTION_REPLAY:
        activated = _parse_dt(
            row.get("activated_at") or row.get("promoted_at"),
            assume_local_timezone=assume_local_timezone,
            tz=local_timezone,
        )
        if activated is None:
            return REJECT_NOT_ACTIVATED_YET
        if activated > decision_time:
            return REJECT_NOT_ACTIVATED_YET

    created = _parse_dt(
        row.get("artifact_created_at"),
        assume_local_timezone=assume_local_timezone,
        tz=local_timezone,
    )
    if created is None:
        return REJECT_TIME_SEMANTICS
    if created > decision_time:
        # 核心不变量：不得加载 as_of 之后创建/激活的模型。
        return REJECT_CREATED_AFTER_DECISION
    return ""


def _anchor_time(
    row: Mapping[str, object],
    *,
    mode: str,
    assume_local_timezone: bool,
    local_timezone: str,
) -> datetime | None:
    if mode == MODE_STRICT_PRODUCTION_REPLAY:
        return _parse_dt(
            row.get("activated_at") or row.get("promoted_at"),
            assume_local_timezone=assume_local_timezone,
            tz=local_timezone,
        )
    return _parse_dt(
        row.get("artifact_created_at"),
        assume_local_timezone=assume_local_timezone,
        tz=local_timezone,
    )


def _text(value: object) -> str:
    return str(value or "").strip()


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return _text(value)


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _as_aware(
    value: datetime, *, assume_local_timezone: bool, tz: str
) -> datetime | None:
    if _is_aware(value):
        return value
    if not assume_local_timezone:
        return None
    try:
        zone = ZoneInfo(tz)
    except Exception:  # noqa: BLE001 - 时区库不可用时退化为"判不了"
        return None
    return value.replace(tzinfo=zone)


def _parse_dt(
    value: object, *, assume_local_timezone: bool, tz: str
) -> datetime | None:
    """解析 ISO 字符串/日期为 timezone-aware datetime（失败返回 None）。"""
    if isinstance(value, datetime):
        return _as_aware(value, assume_local_timezone=assume_local_timezone, tz=tz)
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_aware(parsed, assume_local_timezone=assume_local_timezone, tz=tz)


__all__ = [
    "MODE_PIT_RESEARCH",
    "MODE_STRICT_PRODUCTION_REPLAY",
    "MODES",
    "REASON_MODE_UNSUPPORTED",
    "REASON_NO_ACTIVATION_EVIDENCE",
    "REASON_NO_CANDIDATES",
    "REASON_NO_ELIGIBLE_PIT_MODEL",
    "REASON_TIME_SEMANTICS_UNVERIFIED",
    "STATUS_RESOLVED",
    "STATUS_UNSCORABLE",
    "ResolvedModel",
    "resolve_historical_model",
]


# ---------------------------------------------------------------------------
# registry / 磁盘适配器
# ---------------------------------------------------------------------------


def load_registry_candidates(
    registry: object | None,
    *,
    limit: int = 200,
    verify_disk: bool = True,
) -> list[dict[str, object]]:
    """把 registry 记录 + 磁盘事实拼成解析器输入（只读）。

    ``verify_disk=True`` 时逐条核对工件是否存在、实算哈希是否与登记一致——
    这正是 S06 合法性条件里的 "artifact exists / actual hash == registry hash"。
    工件缺失 **不因 registry 状态而放行**（Codex N2）。
    """
    records: list[object] = []
    if registry is not None:
        try:
            records = list(
                registry.list_records(  # type: ignore[attr-defined]
                    limit=limit, suppress_read_errors=True
                )
            )
        except Exception:  # noqa: BLE001 - 注册表读不到 → 无候选（后续判 unscorable）
            records = []
    candidates: list[dict[str, object]] = []
    for record in records:
        row: dict[str, object] = {
            "model_id": getattr(record, "model_id", ""),
            "artifact_uri": getattr(record, "artifact_uri", ""),
            "artifact_created_at": _iso(getattr(record, "artifact_created_at", None)),
            "artifact_content_hash": getattr(record, "artifact_content_hash", ""),
            "feature_schema_id": getattr(record, "feature_schema_id", ""),
            "feature_schema_hash": getattr(record, "feature_schema_hash", ""),
            "label_policy_id": getattr(record, "label_policy_id", ""),
            "label_policy_hash": getattr(record, "label_policy_hash", ""),
            "dataset_manifest_id": getattr(record, "dataset_manifest_id", ""),
            "lifecycle_state": str(getattr(record, "lifecycle_state", "")),
            "promoted_at": _iso(getattr(record, "promoted_at", None)),
        }
        if verify_disk:
            _attach_disk_facts(row)
        candidates.append(row)
    return candidates


def _attach_disk_facts(row: dict[str, object]) -> None:
    """补 artifact_exists / content_hash_verified / dataset_manifest_exists。"""
    from pathlib import Path

    from stock_analyzer.models.bundle import compute_artifact_identity_hash

    uri = _text(row.get("artifact_uri"))
    registered_hash = _text(row.get("artifact_content_hash"))
    if not uri:
        row["artifact_exists"] = False
        return
    path = Path(uri).expanduser()
    row["artifact_exists"] = path.exists()
    if not path.exists():
        row["content_hash_verified"] = False
        return
    try:
        actual = compute_artifact_identity_hash(path)
    except Exception:  # noqa: BLE001 - 算不出来 → 未验证（不是"已验证一致"）
        row["content_hash_verified"] = None
        return
    if not registered_hash:
        row["content_hash_verified"] = None
        return
    row["content_hash_verified"] = registered_hash.lower() == actual.lower()
