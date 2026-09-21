"""Alpha V2 M4-L：真实生产漏斗快照契约（Production Funnel Snapshot）。

**为什么有这个模块**：M3 起 shadow capture 一直把"Alpha 自己打分选出的 Top50"
伪标成 ``in_quality_pool/in_light_pool/in_deep_pool=True``，``deep_rank`` 写的是
Alpha 名次——那不是生产漏斗。M4-L 把成员资格的唯一权威来源切到生产系统当天
**真实执行完成**的夜扫结果（Week5SelectionEngine 的 Quality300 → Light100 →
Deep50），本模块定义这条 evidence 链的文件契约、写入纪律与校验硬门。

工件位置（``funnel_root``，默认 ``artifacts/runtime/production_funnel``）::

    <funnel_root>/<trade_date>/night_scan_source_evidence.json   ← 成员来源（先写）
    <funnel_root>/<trade_date>/funnel_snapshot.json               ← 漏斗快照（由上一份抽取）

两条证据**名实分离**（R1 外部复核 BLOCKER 7：正式晚报只存 counts，不能拿它的
sha256 冒充"成员来源没被改"）：

- ``source_night_scan_artifact_path/sha256`` —— 指向 **night-scan source evidence**
  （含 Quality/Light/Deep 成员原文），funnel 的成员**从它抽取**，捕获时复算 hash
  并逐成员对账；
- ``published_report_id/path/sha256`` —— 指向晚报正式报告（不可变发布物），
  link 时除哈希外还做**语义校验**（report_id / trade_date / report_kind / scan_status）。

写入纪律（单向、防篡改）：

1. **先证后链**：夜扫拿到终态结果（status ∈ {ok, empty} 且引擎跑了
   ``snapshot_funnel``）时先落 source evidence（当日不可变），再从它抽取落
   funnel 快照（``published_report_id=""``）；晚报正式报告发布后由
   ``link_funnel_to_report`` 把报告身份与 sha256 补进同一份工件并重算哈希。
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

SOURCE_EVIDENCE_SCHEMA = "alpha_v2_night_scan_source_evidence.v1"
SOURCE_EVIDENCE_FILENAME = "night_scan_source_evidence.json"
# 允许链接的正式晚报 scan_status（funnel 只在扫描真的跑完时才会存在）。
LINKABLE_REPORT_SCAN_STATUSES: tuple[str, ...] = ("completed", "empty")

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


def source_evidence_path(funnel_root: str | Path, trade_date: date | str) -> Path:
    """``<root>/<YYYY-MM-DD>/night_scan_source_evidence.json``。"""
    day = trade_date.isoformat() if isinstance(trade_date, date) else str(trade_date).strip()
    return Path(funnel_root) / day / SOURCE_EVIDENCE_FILENAME


def source_evidence_hash(payload: Mapping[str, object]) -> str:
    """除 ``source_evidence_hash`` 自身外的 canonical JSON sha256。"""
    body = {key: value for key, value in payload.items() if key != "source_evidence_hash"}
    return hashlib.sha256(
        json.dumps(
            body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
    ).hexdigest()


def funnel_snapshot_hash(payload: Mapping[str, object]) -> str:
    """除 ``funnel_snapshot_hash`` 自身外的 canonical JSON sha256。"""
    body = {key: value for key, value in payload.items() if key != "funnel_snapshot_hash"}
    serialized = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def file_sha256(path: str | Path) -> str:
    """文件**字节**的 sha256（证据指针必须记这个，而不是字典规范化哈希）。"""
    return _file_sha256(Path(path))


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


def build_source_evidence(
    *,
    source_report: Mapping[str, object],
    trade_date: str,
    trace_id: str,
    created_at: str,
) -> dict[str, object]:
    """从夜扫报告抽出**成员来源原文**（funnel 唯一抽取入口，R1 BLOCKER 7）。

    这份工件回答"Quality300/Light100/Deep50 的成员是这次夜扫的哪份原文"——
    正式晚报只存 counts，不能替代它。落盘后**当日不可变**。
    """
    prefilter = _mapping(source_report.get("prefilter"))
    funnel_block = _mapping(source_report.get("funnel"))
    contract = _mapping(funnel_block.get("selection_contract"))
    quality_report = _mapping(prefilter.get("universe_quality_selection"))
    deep_report = _mapping(prefilter.get("deep_stage"))
    payload: dict[str, object] = {
        "schema": SOURCE_EVIDENCE_SCHEMA,
        "trade_date": str(trade_date),
        "created_at": str(created_at),
        "trace_id": str(trace_id),
        "selection_contract": contract,
        "selector_mode": str(quality_report.get("selector_mode", "") or ""),
        "funnel_policy": str(funnel_block.get("policy", "") or ""),
        "deep_stage_ran": bool(funnel_block.get("deep_stage_ran", False)),
        "quality_selected": list(quality_report.get("selected") or []),
        "light_shortlisted": list(prefilter.get("shortlisted") or []),
        "deep_selected": list(deep_report.get("selected") or []),
        "pinned_symbols": [
            str(symbol).strip()
            for symbol in (prefilter.get("pinned_symbols") or [])
            if str(symbol).strip()
        ],
        "scan_status": "night_scan_completed",
    }
    payload["source_evidence_hash"] = source_evidence_hash(payload)
    return payload


def write_source_evidence(
    *, funnel_root: str | Path, payload: Mapping[str, object]
) -> Path:
    """写当日源证据（幂等；同内容重复写 OK，内容不同抛 tamper）。"""
    path = source_evidence_path(funnel_root, str(payload.get("trade_date", "")))
    existing = _load_json(path)
    if existing is not None:
        # 幂等判定看**实质内容**：trace_id / created_at 是运行时噪声（同日重跑会变），
        # 成员、契约、selector_mode 变了才是"同一天两份互斥来源"。
        if _evidence_semantic_diff(existing, payload):
            raise FunnelTamperError(
                f"{path} 已存在且内容不同：同一天的夜扫成员来源只允许一份权威证据"
            )
        return path
    return write_json_atomic(path, payload)


def load_source_evidence(path: str | Path) -> dict[str, object]:
    """读源证据并自检 schema/hash（读侧统一入口）。"""
    payload = _load_json(Path(path))
    if payload is None:
        raise FunnelNotFoundError(f"夜扫源证据不存在或不可读: {path}")
    if payload.get("schema") != SOURCE_EVIDENCE_SCHEMA:
        raise FunnelVerificationError(
            f"源证据 schema 不符: {payload.get('schema')!r}（期望 {SOURCE_EVIDENCE_SCHEMA}）"
        )
    recorded = str(payload.get("source_evidence_hash", "") or "")
    if not recorded or source_evidence_hash(payload) != recorded:
        raise FunnelVerificationError(
            f"源证据 source_evidence_hash 校验失败（{path}）：成员原文被事后修改"
        )
    return payload


def extract_funnel_from_source_evidence(
    evidence: Mapping[str, object],
    *,
    source_artifact_path: str,
    source_artifact_sha256: str,
    signal_date: str = "",
    trade_date: str = "",
) -> dict[str, object]:
    """从源证据抽取 funnel 载荷（成员一律按证据里的顺序名次落账）。

    ``signal_date`` / ``trade_date`` 在这里**写入后**才计算哈希——调用方若在
    返回后再改这两个字段，哈希会与内容失配（读侧必拒）。
    """
    contract = _mapping(evidence.get("selection_contract"))
    quality_members = _ordered_members(
        evidence.get("quality_selected"), score_keys=("score",)
    )
    light_members = _ordered_members(
        evidence.get("light_shortlisted"), score_keys=("baseline_score",)
    )
    deep_members = _ordered_members(
        evidence.get("deep_selected"), score_keys=("funnel_score", "model_score")
    )
    pinned_symbols = [
        str(symbol).strip()
        for symbol in (evidence.get("pinned_symbols") or [])
        if str(symbol).strip()
    ]
    payload: dict[str, object] = {
        "schema": FUNNEL_SCHEMA,
        "signal_date": str(signal_date),
        "trade_date": str(trade_date),
        "created_at": str(evidence.get("created_at", "")),
        "source": FUNNEL_SOURCE,
        "selection_contract_id": str(contract.get("selection_contract_id", "") or ""),
        "quality_target": _int_or_none(contract.get("quality_target")),
        "light_target": _int_or_none(contract.get("light_target")),
        "deep_target": _int_or_none(contract.get("deep_target")),
        # 命名分离：报告身份（link 阶段补） vs 成员来源（此处即有）
        "published_report_id": "",
        "published_report_path": "",
        "published_report_sha256": "",
        "night_scan_trace_id": str(evidence.get("trace_id", "")),
        "source_night_scan_artifact_path": str(source_artifact_path),
        "source_night_scan_artifact_sha256": str(source_artifact_sha256),
        "scan_status": str(evidence.get("scan_status", "")),
        "funnel_policy": str(evidence.get("funnel_policy", "") or ""),
        "selector_mode": str(evidence.get("selector_mode", "") or ""),
        "degraded": {
            "selector_mode": str(evidence.get("selector_mode", "") or ""),
            "deep_stage_ran": bool(evidence.get("deep_stage_ran", False)),
        },
        "quality_members": quality_members,
        "light_members": light_members,
        "deep_members": deep_members,
        "quality_count": len(quality_members),
        "light_count": len(light_members),
        "deep_count": len(deep_members),
        "pinned_override_members": [{"symbol": symbol} for symbol in pinned_symbols],
        "pinned_added_count": len(pinned_symbols),
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
    prior_report = _published_report_id(existing)
    if prior_report:
        raise FunnelTamperError(
            f"{path} 已链接正式报告 {prior_report}，"
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
    """把正式晚报身份（id + 路径 + sha256）链进 funnel 工件并重算哈希。

    R1（BLOCKER 7.2）：**不只是 hash(file)**——先读正式报告并做语义校验：

    - ``report_id`` == 参数 report_id；
    - ``trade_date`` == funnel 的 trade_date；
    - ``report_kind`` == ``formal``（回放/notice 报告不得充当生产证据）；
    - ``scan_status`` ∈ {completed, empty}（funnel 只在扫描真的跑完时存在）。

    只允许从"未链接"过渡到"链接"；link 对象不同 = 拒绝。
    """
    path = funnel_snapshot_path(funnel_root, trade_date)
    existing = _load_json(path)
    if existing is None:
        raise FunnelNotFoundError(
            f"funnel 工件不存在，无法链接报告: {path}（夜扫未产出 funnel？）"
        )
    prior_id = _published_report_id(existing)
    if prior_id:
        if prior_id != str(report_id):
            raise FunnelTamperError(
                f"{path} 已链接 {prior_id}，不能再链接 {report_id}"
            )
        return path
    report_file = Path(report_path)
    report_payload = _load_json(report_file)
    if report_payload is None:
        raise FunnelVerificationError(f"正式报告不可读或不是 JSON 对象: {report_file}")
    violations: list[str] = []
    if str(report_payload.get("report_id", "") or "") != str(report_id):
        violations.append(
            f"report_id={report_payload.get('report_id')!r} != {report_id!r}"
        )
    if str(report_payload.get("trade_date", "") or "") != str(trade_date):
        violations.append(
            f"trade_date={report_payload.get('trade_date')!r} != {trade_date!r}"
        )
    report_kind = str(report_payload.get("report_kind", "") or "")
    if report_kind != "formal":
        violations.append(f"report_kind={report_kind!r} 不是 formal")
    scan_status = str(report_payload.get("scan_status", "") or "").lower()
    if scan_status not in LINKABLE_REPORT_SCAN_STATUSES:
        violations.append(
            f"scan_status={scan_status!r} 不是 {list(LINKABLE_REPORT_SCAN_STATUSES)}"
        )
    if violations:
        raise FunnelVerificationError(
            f"正式报告与链接参数语义不符（{report_file}）: " + "; ".join(violations)
        )
    updated = dict(existing)
    updated["published_report_id"] = str(report_id)
    updated["published_report_path"] = str(report_file)
    updated["published_report_sha256"] = _file_sha256(report_file)
    # 兼容旧工件字段名（读侧 _published_report_id 也认它）
    updated.pop("night_scan_report_id", None)
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


def _published_report_id(funnel: Mapping[str, object]) -> str:
    """报告指针（新字段优先；旧工件字段名兼容读）。"""
    current = str(funnel.get("published_report_id", "") or "").strip()
    if current:
        return current
    return str(funnel.get("night_scan_report_id", "") or "").strip()


def verify_funnel_for_capture(
    funnel: Mapping[str, object],
    *,
    signal_date: date,
    selection_contract_id: str,
    require_linked_report: bool,
    report_root: str | Path | None = None,
    funnel_root: str | Path | None = None,
) -> dict[str, object]:
    """生产模式捕获前的硬门；通过返回归一化 cohort 视图，失败抛错。

    对账项（R1 增强）：日期 / 契约 id / source / selector_mode / 计数 /
    ``Deep ⊆ Light ⊆ Quality`` / **成员唯一性 + rank 为正整数且与顺序自洽** /
    源证据（night-scan source evidence）文件 sha256 + 成员逐项复算 /
    （生产模式）正式报告 pointer 与文件 sha256。
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

    # ── 源证据：成员来源必须是可复算的不可变工件（R1 BLOCKER 7）──────────────
    source_path_text = str(funnel.get("source_night_scan_artifact_path", "") or "").strip()
    source_hash = str(funnel.get("source_night_scan_artifact_sha256", "") or "").strip()
    if not source_path_text or not source_hash:
        raise FunnelVerificationError(
            "funnel 缺源证据指针（source_night_scan_artifact_path/sha256）："
            "成员来源不可证——正式晚报只有 counts，不能代替成员证据"
        )
    source_path = Path(source_path_text)
    if not source_path.is_absolute() and funnel_root is not None:
        candidate = Path(funnel_root) / day / SOURCE_EVIDENCE_FILENAME
        if candidate.exists():
            source_path = candidate
    if not source_path.exists():
        raise FunnelVerificationError(f"源证据文件不存在: {source_path}")
    if _file_sha256(source_path) != source_hash:
        raise FunnelVerificationError(
            f"源证据 {source_path} 的 sha256 与 funnel 记录不一致——成员原文被改写"
        )
    evidence = load_source_evidence(source_path)
    if str(evidence.get("trade_date", "") or "") != day:
        raise FunnelVerificationError(
            f"源证据 trade_date={evidence.get('trade_date')!r} 与 signal_date={day} 不符"
        )
    evidence_contract = _mapping(evidence.get("selection_contract"))
    if str(evidence_contract.get("selection_contract_id", "") or "") != contract:
        raise FunnelVerificationError("源证据的 selection_contract_id 与 funnel 不符")
    if str(evidence.get("selector_mode", "") or "").strip() != selector_mode:
        raise FunnelVerificationError("源证据的 selector_mode 与 funnel 不符")
    derived = extract_funnel_from_source_evidence(
        evidence,
        source_artifact_path=str(source_path),
        source_artifact_sha256=source_hash,
    )
    for key in (
        "quality_members",
        "light_members",
        "deep_members",
        "pinned_override_members",
    ):
        if funnel.get(key) != derived.get(key):
            raise FunnelVerificationError(
                f"funnel.{key} 与源证据复算结果不一致——成员被事后改写"
            )

    if require_linked_report:
        report_id = _published_report_id(funnel)
        if not report_id:
            raise FunnelVerificationError(
                "funnel 未链接正式晚报（published_report_id 为空）："
                "生产 clean OOS 要求 funnel 已锚定不可变的正式报告"
            )
        expected_hash = str(funnel.get("published_report_sha256", "") or "").strip()
        source_report_path = str(funnel.get("published_report_path", "") or "").strip()
        resolved = _resolve_report_file(
            report_id=report_id,
            trade_date=day,
            recorded_path=source_report_path,
            report_root=report_root,
        )
        if resolved is None:
            raise FunnelVerificationError(
                f"正式报告文件找不到: report_id={report_id}（预期路径 {source_report_path!r}）"
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
        # 契约补强（R1 §8）：成员不得重复；rank 必须为正整数、唯一、与顺序自洽。
        seen: set[str] = set()
        ranks: list[int] = []
        for member in members:
            missing = [key for key in _CAPTURE_REQUIRED_MEMBER_FIELDS if key not in member]
            if missing:
                raise FunnelVerificationError(
                    f"{name}_members 存在缺字段成员（缺 {missing}）：{member!r}"
                )
            symbol = str(member["symbol"])
            if symbol in seen:
                raise FunnelVerificationError(f"{name}_members 出现重复 symbol: {symbol}")
            seen.add(symbol)
            rank = member["rank"]
            if not isinstance(rank, int) or rank <= 0:
                raise FunnelVerificationError(
                    f"{name}_members 的 rank 必须是正整数: {symbol} rank={rank!r}"
                )
            ranks.append(rank)
        if sorted(ranks) != list(range(1, len(members) + 1)):
            raise FunnelVerificationError(
                f"{name}_members 的 rank 必须唯一且与 stage order 自洽（1..{len(members)}）"
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
        "published_report_id": _published_report_id(funnel),
        "source_night_scan_artifact_path": str(source_path),
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
# 源证据里同样属于"运行时噪声"的键（同日重跑必然变化，不构成内容冲突）。
_EVIDENCE_IGNORED_DIFF_KEYS = {"created_at", "trace_id", "source_evidence_hash"}


def _evidence_semantic_diff(
    existing: Mapping[str, object], incoming: Mapping[str, object]
) -> list[str]:
    diffs: list[str] = []
    for key in sorted(set(existing) | set(incoming)):
        if key in _EVIDENCE_IGNORED_DIFF_KEYS:
            continue
        if existing.get(key) != incoming.get(key):
            diffs.append(str(key))
    return diffs


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
    "LINKABLE_REPORT_SCAN_STATUSES",
    "RANK_NOT_AVAILABLE",
    "SOURCE_EVIDENCE_FILENAME",
    "SOURCE_EVIDENCE_SCHEMA",
    "FunnelError",
    "FunnelNotFoundError",
    "FunnelTamperError",
    "FunnelVerificationError",
    "build_source_evidence",
    "emit_funnel_snapshot",
    "extract_funnel_from_source_evidence",
    "file_sha256",
    "funnel_snapshot_hash",
    "funnel_snapshot_path",
    "link_funnel_to_report",
    "load_funnel_snapshot",
    "load_source_evidence",
    "source_evidence_hash",
    "source_evidence_path",
    "verify_funnel_for_capture",
    "write_source_evidence",
]
