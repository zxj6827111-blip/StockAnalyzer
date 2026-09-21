"""Alpha V2 M4-L：真实生产漏斗快照契约（Production Funnel Snapshot）。

**为什么有这个模块**：M3 起 shadow capture 一直把"Alpha 自己打分选出的 Top50"
伪标成 ``in_quality_pool/in_light_pool/in_deep_pool=True``，``deep_rank`` 写的是
Alpha 名次——那不是生产漏斗。M4-L 把成员资格的唯一权威来源切到生产系统当天
**真实执行完成**的夜扫结果（Week5SelectionEngine 的 Quality300 → Light100 →
Deep50），本模块定义这条 evidence 链的文件契约、写入纪律与校验硬门。

工件位置（``funnel_root``，默认 ``artifacts/runtime/production_funnel``）::

    <funnel_root>/<trade_date>/funnel_snapshot.json

写入纪律（单向、防篡改）：

1. **先证后链**：夜扫拿到终态结果（status ∈ {ok, empty} 且引擎跑了
   ``snapshot_funnel``）时先落"未链接"版本（``night_scan_report_id=""``）；
   晚报正式报告发布后由 ``link_funnel_to_report`` 把 ``report_id`` 与正式报告
   文件的 sha256 补进同一份工件并重算 ``funnel_snapshot_hash``。
2. **linked 工件不可变**：已链接的 funnel 再被改写（成员、rank、报告指向任一
   不同）抛 :class:`FunnelTamperError`——生产 funnel 当日只能有一份权威证据。
3. **不写 ≠ 静默**：夜扫 blocked / 降级 / 非 snapshot_funnel 时**不产出** funnel
   工件；capture 在生产模式找不到当天工件即 fail-closed。

捕获侧硬门（:func:`verify_funnel_for_capture`）任一不满足即拒捕：

- schema / source / 日期 / 契约 id 与目标值逐项对账；
- ``selector_mode ∈ {quality, quality_all_eligible}``（snapshot_fallback /
  degraded_fallback 都不是"当天真实生产选择"）；
- ``funnel_snapshot_hash`` 复算一致；链接态必填 report_id 且正式报告文件
  sha256 一致（要求 hash 锚定，不给"改了报告还说是真的"留口子）；
- ``Deep ⊆ Light ⊆ Quality``（pinned override 单独成列、不算成员）；
- 计数与成员列表自洽。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

from stock_analyzer.alpha_v2.artifacts import write_json_atomic

FUNNEL_SCHEMA = "alpha_v2_production_funnel.v1"
FUNNEL_FILENAME = "funnel_snapshot.json"
FUNNEL_SOURCE = "production_selection_engine"
# 质量选择器的"真实当天选择"模式（snapshot_fallback=读旧快照、degraded=配额抽样，
# 都不是当天真实生产结果；出现即留痕但 capture 必须拒绝）。
AUTHORITATIVE_SELECTOR_MODES: tuple[str, ...] = ("quality", "quality_all_eligible")
# 成员 rank 无法从生产链证明时的显式标记（契约禁止伪造 rank）。
RANK_NOT_AVAILABLE = "not_available"

DEFAULT_FUNNEL_ROOT = "artifacts/runtime/production_funnel"

_CAPTURE_REQUIRED_MEMBER_FIELDS: tuple[str, ...] = ("symbol", "rank", "rank_source")


class FunnelError(RuntimeError):
    """生产漏斗工件读写/校验失败基类。"""


class FunnelNotFoundError(FunnelError):
    """当天 funnel 工件不存在（生产模式 = fail-closed）。"""


class FunnelVerificationError(FunnelError):
    """funnel 工件内容与声明不符（日期/契约/哈希/嵌套关系任一项）。"""


class FunnelTamperError(FunnelError):
    """同一天出现两份**不同**的 funnel 证据，或 linked 工件被改写。"""


# ---------------------------------------------------------------------------
# 路径与哈希
# ---------------------------------------------------------------------------


def funnel_snapshot_path(funnel_root: str | Path, trade_date: date | str) -> Path:
    """``<root>/<YYYY-MM-DD>/funnel_snapshot.json``（按日目录，天然防串天）。"""
    day = trade_date.isoformat() if isinstance(trade_date, date) else str(trade_date).strip()
    return Path(funnel_root) / day / FUNNEL_FILENAME


def funnel_snapshot_hash(payload: Mapping[str, object]) -> str:
    """除 ``funnel_snapshot_hash`` 自身外的 canonical JSON sha256。"""
    body = {key: value for key, value in payload.items() if key != "funnel_snapshot_hash"}
    serialized = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 从生产夜扫结果提取（生产侧，写单方用）
# ---------------------------------------------------------------------------


def _ordered_members(items: object, *, score_keys: Sequence[str]) -> list[dict[str, object]]:
    """把报告里的"按顺序=名次"的成员列表转成契约成员行（rank 从 1 起）。

    成员来源列表不存在/为空 → 返回 ``[]``（计数零，交由校验侧按场景判）。
    任一成员缺 symbol → 跳过该行但保持其余名次连续语义：rank 按原始顺序位，
    不做二次压缩——让缺口在审计里可见。
    """
    members: list[dict[str, object]] = []
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return members
    position = 0
    for item in items:
        position += 1
        if not isinstance(item, Mapping):
            continue
        symbol = str(item.get("symbol", "") or "").strip()
        if not symbol:
            continue
        member: dict[str, object] = {
            "symbol": symbol,
            "rank": position,
            "rank_source": "stage_order",
        }
        score = _first_present(item, score_keys)
        if score is not None:
            member["score"] = score
        members.append(member)
    return members


def _first_present(item: Mapping[str, object], keys: Sequence[str]) -> object | None:
    for key in keys:
        value = item.get(key)
        if value is None:
            continue
        if isinstance(value, float) and (value != value):  # NaN 不进工件
            continue
        return value
    return None


def extract_funnel_from_scan_report(
    *,
    source_report: Mapping[str, object],
    trace_id: str,
    scan_status: str,
    created_at: str,
) -> dict[str, object]:
    """从 Week5SelectionEngine 完整报告提取 M4-L 漏斗快照载荷（纯函数）。

    只读取、不修改生产报告；成员一律按生产报告中的顺序名次落账。
    pinned 名单单独成列（生产语义 = 绕过漏斗直达 final 的注入票），
    **绝不**并入任何一级成员列表。
    """
    prefilter = _mapping(source_report.get("prefilter"))
    funnel_block = _mapping(source_report.get("funnel"))
    contract = _mapping(funnel_block.get("selection_contract"))
    quality_report = _mapping(prefilter.get("universe_quality_selection"))
    deep_report = _mapping(prefilter.get("deep_stage"))

    quality_members = _ordered_members(
        quality_report.get("selected"), score_keys=("score",)
    )
    light_members = _ordered_members(
        prefilter.get("shortlisted"), score_keys=("baseline_score",)
    )
    deep_members = _ordered_members(
        deep_report.get("selected"), score_keys=("funnel_score", "model_score")
    )

    pinned_symbols = [
        str(symbol).strip()
        for symbol in (prefilter.get("pinned_symbols") or [])
        if str(symbol).strip()
    ]

    payload: dict[str, object] = {
        "schema": FUNNEL_SCHEMA,
        "signal_date": "",  # 由调用方按夜扫 trade_date 填（extract 不猜日期）
        "trade_date": "",
        "created_at": str(created_at),
        "source": FUNNEL_SOURCE,
        "selection_contract_id": str(contract.get("selection_contract_id", "") or ""),
        "quality_target": _int_or_none(contract.get("quality_target")),
        "light_target": _int_or_none(contract.get("light_target")),
        "deep_target": _int_or_none(contract.get("deep_target")),
        "night_scan_report_id": "",
        "night_scan_trace_id": str(trace_id),
        "scan_status": str(scan_status),
        "funnel_policy": str(funnel_block.get("policy", "") or ""),
        "selector_mode": str(quality_report.get("selector_mode", "") or ""),
        "degraded": {
            "selector_mode": str(quality_report.get("selector_mode", "") or ""),
            "fallback_source": str(quality_report.get("fallback_source", "") or ""),
            "deep_stage_ran": bool(funnel_block.get("deep_stage_ran", False)),
            "deep_empty_reason": str(funnel_block.get("deep_empty_reason", "") or ""),
            "intraday_degraded": bool(prefilter.get("intraday_degraded", False)),
            "fresh_frame_used": deep_report.get("fresh_frame_used"),
            "model_prediction_degraded": bool(
                deep_report.get("model_prediction_degraded", False)
            ),
        },
        "quality_members": quality_members,
        "light_members": light_members,
        "deep_members": deep_members,
        "quality_count": len(quality_members),
        "light_count": len(light_members),
        "deep_count": len(deep_members),
        # pinned：与 funnel 成员资格严格分离；生产引擎里它们绕过 deep stage。
        "pinned_override_members": [{"symbol": symbol} for symbol in pinned_symbols],
        "pinned_added_count": len(pinned_symbols),
        "source_artifact_path": "",
        "source_artifact_sha256": "",
    }
    payload["funnel_snapshot_hash"] = funnel_snapshot_hash(payload)
    return payload


# ---------------------------------------------------------------------------
# 写入（生产侧）
# ---------------------------------------------------------------------------


def emit_funnel_snapshot(
    *,
    funnel_root: str | Path,
    payload: Mapping[str, object],
) -> Path:
    """落盘当日 funnel 工件（夜扫终态调用；幂等 + 防双版本）。

    - 文件不存在 → 写入；
    - 已存在且已链接（``night_scan_report_id`` 非空）→ 拒绝任何改写
      （:class:`FunnelTamperError`）：linked 工件 = 当日唯一权威；
    - 已存在未链接且内容一致（除时间戳/trace 外逐键相同）→ 幂等返回；
    - 已存在未链接但成员/契约等不同 → :class:`FunnelTamperError`
      （同一天两份互斥的"真实漏斗"是事故，不是新证据）。
    """
    path = funnel_snapshot_path(funnel_root, str(payload.get("trade_date", "")))
    existing = _load_json(path)
    if existing is None:
        return write_json_atomic(path, payload)
    if str(existing.get("night_scan_report_id", "") or "").strip():
        raise FunnelTamperError(
            f"{path} 已链接正式报告 {existing.get('night_scan_report_id')}，"
            "当日 funnel 已封版，拒绝重写；如需修正请走事故流程（关 epoch、留档）"
        )
    comparable_difference = _semantic_diff(existing, payload)
    if comparable_difference:
        raise FunnelTamperError(
            f"{path} 已存在且实质内容不同（{comparable_difference[:5]}）；"
            "同一天不允许出现两份不同的生产 funnel 证据"
        )
    return path


def link_funnel_to_report(
    *,
    funnel_root: str | Path,
    trade_date: str,
    report_id: str,
    report_path: str | Path,
) -> Path:
    """把正式晚报的身份（report_id + 文件 sha256）链进 funnel 工件并重算哈希。

    只允许从"未链接"过渡到"链接"；link 对象不同 = 同日出两份报告指向同一
    funnel，按 tamper 拒绝。返回更新后的文件路径。
    """
    path = funnel_snapshot_path(funnel_root, trade_date)
    existing = _load_json(path)
    if existing is None:
        raise FunnelNotFoundError(
            f"funnel 工件不存在，无法链接报告: {path}（夜扫未产出 funnel？）"
        )
    existing_report = str(existing.get("night_scan_report_id", "") or "").strip()
    if existing_report:
        if existing_report != str(report_id):
            raise FunnelTamperError(
                f"{path} 已链接 {existing_report}，不能再链接 {report_id}"
            )
        return path
    updated = dict(existing)
    updated["night_scan_report_id"] = str(report_id)
    report_file = Path(report_path)
    updated["source_artifact_path"] = str(report_file)
    updated["source_artifact_sha256"] = _file_sha256(report_file)
    updated["funnel_snapshot_hash"] = funnel_snapshot_hash(updated)
    return write_json_atomic(path, updated)


# ---------------------------------------------------------------------------
# 读取 + 捕获侧硬门
# ---------------------------------------------------------------------------


def load_funnel_snapshot(path: str | Path) -> dict[str, object]:
    """读工件并做 schema/哈希自检（读侧统一入口，调用方再做业务硬门）。"""
    target = Path(path)
    payload = _load_json(target)
    if payload is None:
        raise FunnelNotFoundError(f"funnel 工件不存在或不可读: {target}")
    if payload.get("schema") != FUNNEL_SCHEMA:
        raise FunnelVerificationError(
            f"funnel schema 不符: {payload.get('schema')!r}（期望 {FUNNEL_SCHEMA}）"
        )
    recorded = str(payload.get("funnel_snapshot_hash", "") or "")
    if not recorded or funnel_snapshot_hash(payload) != recorded:
        raise FunnelVerificationError(
            f"funnel_snapshot_hash 校验失败（{target}）：内容被事后修改或写坏"
        )
    return payload


def verify_funnel_for_capture(
    funnel: Mapping[str, object],
    *,
    signal_date: date,
    selection_contract_id: str,
    require_linked_report: bool,
    report_root: str | Path | None = None,
) -> dict[str, object]:
    """生产模式捕获前的硬门；通过返回归一化 cohort 视图，失败抛错。

    ``require_linked_report=True``（生产模式恒真）：funnel 必须已链接正式
    晚报，且报告文件的 sha256 与工件记录一致——链子断了当天不能算
    production-equivalent。
    """
    day = signal_date.isoformat()
    signal = str(funnel.get("signal_date", "") or "").strip()
    trade = str(funnel.get("trade_date", "") or "").strip()
    if signal != day or trade != day:
        raise FunnelVerificationError(
            f"funnel 日期不符: signal_date={signal!r} trade_date={trade!r}，期望 {day}"
            "（用昨天的漏斗冒充今天 = Attack B，必拒）"
        )
    contract = str(funnel.get("selection_contract_id", "") or "").strip()
    if contract != str(selection_contract_id):
        raise FunnelVerificationError(
            f"selection_contract_id 不符: funnel={contract!r} freeze={selection_contract_id!r}"
        )
    if str(funnel.get("source", "") or "") != FUNNEL_SOURCE:
        raise FunnelVerificationError(
            f"funnel.source 必须是 {FUNNEL_SOURCE}: 收到 {funnel.get('source')!r}"
        )
    selector_mode = str(funnel.get("selector_mode", "") or "").strip()
    if selector_mode not in AUTHORITATIVE_SELECTOR_MODES:
        raise FunnelVerificationError(
            f"selector_mode={selector_mode!r} 不是当天真实生产选择"
            f"（仅接受 {list(AUTHORITATIVE_SELECTOR_MODES)}；fallback/degraded 不算 clean OOS）"
        )
    if require_linked_report:
        report_id = str(funnel.get("night_scan_report_id", "") or "").strip()
        if not report_id:
            raise FunnelVerificationError(
                "funnel 未链接正式晚报（night_scan_report_id 为空）："
                "生产 clean OOS 要求 funnel 已锚定不可变的正式报告"
            )
        expected_hash = str(funnel.get("source_artifact_sha256", "") or "").strip()
        source_path = str(funnel.get("source_artifact_path", "") or "").strip()
        resolved = _resolve_report_file(
            report_id=report_id,
            trade_date=day,
            recorded_path=source_path,
            report_root=report_root,
        )
        if resolved is None:
            raise FunnelVerificationError(
                f"正式报告文件找不到: report_id={report_id}（预期路径 {source_path!r}）"
            )
        if not expected_hash or _file_sha256(resolved) != expected_hash:
            raise FunnelVerificationError(
                f"正式报告 {resolved} 的 sha256 与 funnel 记录不一致——报告被改写"
            )

    quality = _members_of(funnel, "quality_members")
    light = _members_of(funnel, "light_members")
    deep = _members_of(funnel, "deep_members")
    for name, members in (("quality", quality), ("light", light), ("deep", deep)):
        declared = _int_or_none(funnel.get(f"{name}_count"))
        if declared is None or declared != len(members):
            raise FunnelVerificationError(
                f"{name}_count={declared!r} 与 {name}_members 实际长度 {len(members)} 不符"
            )
        for member in members:
            missing = [key for key in _CAPTURE_REQUIRED_MEMBER_FIELDS if key not in member]
            if missing:
                raise FunnelVerificationError(
                    f"{name}_members 存在缺字段成员（缺 {missing}）：{member!r}"
                )
    deep_symbols = {member["symbol"] for member in deep}
    light_symbols = {member["symbol"] for member in light}
    quality_symbols = {member["symbol"] for member in quality}
    if not deep_symbols:
        raise FunnelVerificationError(
            "deep_members 为空：当天没有真实 Deep50 cohort（空 funnel 不得伪造影子行；"
            "应改记 missing day / audit）"
        )
    if not deep_symbols <= light_symbols or not light_symbols <= quality_symbols:
        raise FunnelVerificationError(
            "成员嵌套关系被破坏（要求 Deep ⊆ Light ⊆ Quality）；"
            "pinned/异常票必须从 pinned_override_members 单独表达"
        )
    rank_map_deep = {member["symbol"]: member for member in deep}
    return {
        "quality_members": quality,
        "light_members": light,
        "deep_members": deep,
        "deep_rank_by_symbol": rank_map_deep,
    }


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _members_of(funnel: Mapping[str, object], key: str) -> list[dict[str, object]]:
    rows = funnel.get(key)
    if not isinstance(rows, list):
        raise FunnelVerificationError(f"funnel.{key} 缺失或不是列表")
    return [dict(item) for item in rows if isinstance(item, Mapping)]


def _int_or_none(value: object) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _load_json(path: Path) -> dict[str, object] | None:
    try:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _resolve_report_file(
    *,
    report_id: str,
    trade_date: str,
    recorded_path: str,
    report_root: str | Path | None,
) -> Path | None:
    """按"记录的路径 → 约定路径"顺序定位正式报告文件。"""
    candidates: list[Path] = []
    if recorded_path:
        candidates.append(Path(recorded_path))
    if report_root is not None:
        candidates.append(Path(report_root) / trade_date / f"{report_id}.json")
    for candidate in candidates:
        try:
            if candidate.exists() and candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


_IGNORED_DIFF_KEYS = {"created_at", "funnel_snapshot_hash", "night_scan_trace_id"}


def _semantic_diff(
    existing: Mapping[str, object], incoming: Mapping[str, object]
) -> list[str]:
    """两次 emit 之间的实质差异键清单（忽略时间戳与 trace_id 等运行时噪声）。"""
    diffs: list[str] = []
    for key in sorted(set(existing) | set(incoming)):
        if key in _IGNORED_DIFF_KEYS:
            continue
        if existing.get(key) != incoming.get(key):
            diffs.append(str(key))
    return diffs


__all__ = [
    "AUTHORITATIVE_SELECTOR_MODES",
    "DEFAULT_FUNNEL_ROOT",
    "FUNNEL_FILENAME",
    "FUNNEL_SCHEMA",
    "FUNNEL_SOURCE",
    "RANK_NOT_AVAILABLE",
    "FunnelError",
    "FunnelNotFoundError",
    "FunnelTamperError",
    "FunnelVerificationError",
    "emit_funnel_snapshot",
    "extract_funnel_from_scan_report",
    "funnel_snapshot_hash",
    "funnel_snapshot_path",
    "link_funnel_to_report",
    "load_funnel_snapshot",
    "verify_funnel_for_capture",
]
