"""夜扫半段的漏斗留档生产者：把 Quality300 / Light100 / Deep50 落成 ``funnel_trace``。

改进计划 §2 要九层都能追溯"输入、晋级、拒绝原因"。这三层此前只落在
``production_funnel`` 快照里（成员 + 计数），格式与尾盘半段不同构，于是"前置筛选是否
过早淘汰了适合短期上涨的股票"这类问题跨两种工件拼不起来，也没法按层做预测性规则消融。

只记**报告里真的存在**的东西，三条不糊弄的规矩：

1. 逐只拒绝原因夜扫确实没记（NOTE-002 D11），被淘汰的股票统一挂在
   ``night_truncation_reason_not_recorded`` 下。这不是补齐，是把缺口以可读形式留在证据里；
   视图据此仍判这层"原因不可用"，不会假装能回答原因分布。
2. 下一层成员不是上一层子集时（板块配额 pinned 注入会造成），不谎称上层规模：
   ``inputs`` 取**两层成员并集**，``notes`` 里点名越界符号。计数恒等式
   ``inputs == advanced + dropped`` 因此仍成立，且每个数都有明确定义。
3. 三层截断都由旧综合分/等级驱动，全部标 ``predictive``——§2 的消融只动这类，而
   生产安全、交易资格与数据完整性硬门不在这里，也不得被消融误删。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from stock_analyzer.contracts.trend_strategy import (
    ModelIdentity,
    TrendStrategyContract,
)
from stock_analyzer.feature.trend_candidate_contract import HARD_GATE_ATTRIBUTION_ORDER
from stock_analyzer.research.funnel_trace import (
    KIND_HARD_GATE,
    KIND_PREDICTIVE,
    FunnelTrace,
    build_funnel_trace,
    record_stage,
)

#: 夜扫半段唯一的"拒绝原因"：原因本身没被记下来（D11 的现存事实）。
NIGHT_UNATTRIBUTED_DROP = "night_truncation_reason_not_recorded"

#: 层名 → 在 ``report["prefilter"]`` 里取成员的路径。
LAYER_FIELDS = (
    ("quality_300", ("universe_quality_selection", "selected")),
    ("light_100", ("shortlisted",)),
    ("deep_50", ("deep_stage", "selected")),
)
NIGHT_TRACE_SUFFIX = "night"


def _symbols(value: Any) -> list[str]:
    out: list[str] = []
    for row in value or ():
        symbol = ""
        if isinstance(row, Mapping):
            symbol = str(row.get("symbol", "")).strip()
        elif isinstance(row, (str, bytes)):
            symbol = str(row).strip()
        if symbol and symbol not in out:
            out.append(symbol)
    return out


def _layer_symbols(prefilter: Mapping[str, Any], layer: str) -> list[str]:
    path = dict(LAYER_FIELDS)[layer]
    value: Any = prefilter
    for key in path:
        if not isinstance(value, Mapping):
            return []
        value = value.get(key)
    if isinstance(value, (str, bytes)) or not isinstance(value, (Mapping, Sequence)):
        return []
    return _symbols(value)


def _as_day(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _universe_facts(universe: Any) -> dict[str, Any]:
    """把 PIT 股票池快照读成符号级事实；缺任何一份符号清单就返回空（不编造）。"""
    if universe is None:
        return {}
    # 快照是 slots dataclass（没有 __dict__），Mapping 与对象两种形状都要能吃。
    def fact(key: str) -> Any:
        if isinstance(universe, Mapping):
            return universe.get(key)
        return getattr(universe, key, None)

    eligible = _symbols(fact("eligible_symbols"))
    active = _symbols(fact("expected_active_symbols"))
    reasons: dict[str, list[str]] = {}
    raw_reasons = fact("excluded_reasons") or {}
    if isinstance(raw_reasons, Mapping):
        for symbol, reason in raw_reasons.items():
            reasons.setdefault(str(reason), []).append(str(symbol))
    # 当天的硬门输入列没有判别力（常数填充）⇒ 这两层不落档：留档声称"硬性资格检查判过"
    # 就成了假话，而那正是 2026-10-08 float_market_cap=1.2e10 事故的样子。
    broken = [str(rule) for rule in (fact("non_evaluable_gates") or ())]
    if not eligible or not active:
        # 只有计数（旧 payload 的形状）时不落这两层：用计数冒充成员会让留档说谎。
        return {}
    return {
        "snapshot_id": str(fact("universe_snapshot_id") or ""),
        "membership_source_path": str(fact("membership_source_path") or ""),
        "as_of": str(fact("as_of") or ""),
        "eligible": eligible,
        "expected_active": active,
        "suspended": _symbols(fact("known_suspended_symbols")),
        "reasons": reasons,
        "coverage": str(fact("survivorship_coverage") or ""),
        "delisting_verified": bool(fact("delisting_coverage_verified")),
        "non_evaluable_gates": broken,
        # §2 要每层记"使用的特征"：资格层记的是判定输入列与算它的契约版本。
        # 旧 payload 没这两个键时留空，不替它编。
        "gate_input_columns": tuple(sorted({str(c) for c in (fact("gate_input_columns") or ())})),
        "feature_contract_version": str(fact("feature_contract_version") or ""),
        "float_cap_interpretation_version": str(
            fact("float_cap_interpretation_version") or ""
        ),
    }


#: 硬门成员清单在**生产夜扫报告**里的候选路径，按声明顺序尝试，命中的那条要写进留档。
#:
#: 2026-10-09 生产第一份夜扫留档只有 `quality_300/light_100/deep_50` 三层，`universe` 与
#: ``hard_eligibility`` 缺席。事后取证：成员其实正常产出了，但放在
#: ``source_report/prefilter/universe_quality_selection/hard_gate_membership``
#: （5,478 考虑 / 3,676 晋级），而消费端只认 ``prefilter.hard_gate_membership`` 与顶层那一条 ——
#: 于是读到空、这两层被跳过。本地 fixture 是按消费端假定的形状造的，所以测试全绿、生产读空。
#: 这类"fixture 不是生产形状"的偏差由本清单 + 命中路径留痕来堵，而不是再靠猜。
MEMBERSHIP_PATHS: tuple[tuple[str, ...], ...] = (
    ("prefilter", "universe_quality_selection", "hard_gate_membership"),
    ("prefilter", "hard_gate_membership"),
    ("source_report", "prefilter", "universe_quality_selection", "hard_gate_membership"),
    ("source_report", "prefilter", "hard_gate_membership"),
    ("hard_gate_membership",),
)


def locate_hard_gate_membership(
    report: Mapping[str, Any],
) -> tuple[Mapping[str, Any], str]:
    """返回 ``(成员清单, 命中的路径)``；一条路径都没命中时返回 ``({}, "")``——不猜也不填。"""
    for path in MEMBERSHIP_PATHS:
        node: Any = report
        for key in path:
            if not isinstance(node, Mapping):
                node = None
                break
            node = node.get(key)
        if isinstance(node, Mapping) and node:
            return node, ".".join(path)
    return {}, ""


def universe_membership_status(report: Mapping[str, Any]) -> dict[str, Any]:
    """这两层到底落没落、为什么没落 —— 必须是一条读得到的事实。

    改进计划 §2 要的是"哪一层没有留档"本身；只让 ``build_universe_stage_traces`` 返回空元组
    等于把答案留给别人反推，那是这里明确禁止的静默。
    """
    membership, source_path = locate_hard_gate_membership(report)
    if not membership:
        return {
            "emitted": False,
            "reason": "membership_path_not_found",
            "tried_paths": [".".join(path) for path in MEMBERSHIP_PATHS],
        }
    considered = list(membership.get("considered") or ())
    advanced = list(membership.get("advanced") or ())
    gates = [str(item) for item in (membership.get("non_evaluable_gates") or ())]
    if not considered or not advanced:
        return {
            "emitted": False, "reason": "membership_has_no_symbol_lists",
            "source_path": source_path,
            "considered": len(considered), "advanced": len(advanced),
        }
    if gates:
        return {
            "emitted": False, "reason": "non_evaluable_gate_inputs",
            "source_path": source_path, "non_evaluable_gates": gates,
        }
    return {
        "emitted": True, "reason": "", "source_path": source_path,
        "considered": len(considered), "advanced": len(advanced),
    }


def live_universe_facts(report: Mapping[str, Any]) -> dict[str, Any]:
    """把夜扫报告里的硬门**成员**读成留档层要的符号级事实。

    线上这两层的输入不是"全市场"，而是这次批处理真的取到过指标的那批票；
    幸存者偏差覆盖从来没证明过，所以 ``delisting_coverage_verified`` 恒为假、
    覆盖率写成 ``incomplete_or_unknown`` —— 不能因为规模像 5,000 只就当它是全集。
    报告里只有计数（旧形状）时返回空，这两层就不落档，与重放侧同一条规矩。
    """
    membership, source_path = locate_hard_gate_membership(report)
    prefilter = report.get("prefilter")
    prefilter = prefilter if isinstance(prefilter, Mapping) else {}
    considered = [str(symbol) for symbol in (membership.get("considered") or ())]
    advanced = [str(symbol) for symbol in (membership.get("advanced") or ())]
    if not considered or not advanced:
        return {}
    reasons = {
        str(symbol): str(reason)
        for reason, symbols in (membership.get("rejected_symbols") or {}).items()
        for symbol in symbols
    }
    return {
        "universe_snapshot_id": str(
            prefilter.get("selection_snapshot_id")
            or membership.get("selection_snapshot_id")
            or "night_scan_universe_selection"
        ),
        # 命中路径本身也是证据：读的是报告里哪一个键，将来形状再变时能一眼看出是哪条断了。
        "membership_source_path": source_path,
        "as_of": str(report.get("data_as_of") or report.get("generated_at") or ""),
        "eligible_symbols": considered,
        "expected_active_symbols": advanced,
        "excluded_reasons": reasons,
        "known_suspended_symbols": [],
        "survivorship_coverage": str(
            membership.get("survivorship_coverage") or "incomplete_or_unknown"
        ),
        "delisting_coverage_verified": False,
        "non_evaluable_gates": list(membership.get("non_evaluable_gates") or ()),
        "gate_input_columns": tuple(membership.get("gate_input_columns") or ()),
        "feature_contract_version": str(membership.get("feature_contract_version") or ""),
        "float_cap_interpretation_version": str(
            membership.get("float_cap_interpretation_version") or ""
        ),
    }



def build_universe_stage_traces(
    *,
    universe: Any,
    data_as_of: str,
    contract: TrendStrategyContract,
    model_identity: ModelIdentity | Mapping[str, Any] | None = None,
) -> tuple[Any, ...]:
    """全市场 → 硬性资格检查两层的留档：只用股票池快照的**符号级**事实构造。

    - ``universe`` 这一层不淘汰任何股票：它记的是"这次考虑过的候选全集"，并写出快照 id
      与幸存者偏差口径（``delisting_coverage_verified`` 为假时，覆盖率仍是
      ``incomplete_or_unknown``，这个事实必须留在证据里而不是被 5000 只的数字掩盖）。
    - ``hard_eligibility`` 的晋级是"as_of 时点确实可能存在成交"的 ``expected_active``；
      被淘汰的按快照里的**真实逐只原因**分组（未来上市 / 窗口内历史不足 / 停牌或停更）。
      其中 ``known_suspended`` 的含义是"eligible 但最近窗口内没有任何 bar"，按契约它
      **不等于**证明停牌——原因名保持与快照一致，不替它编造解释。
    - 拿不到符号级清单（例如只有计数的旧 payload）时返回空元组：宁可不落这两层，
      也不拿计数冒充成员。
    """
    facts = _universe_facts(universe)
    if not facts:
        return ()
    if facts["non_evaluable_gates"]:
        # 有硬门当天的输入列没有判别力：这一层的"晋级/淘汰"不能声称是判出来的。
        return ()
    considered = sorted(set(facts["eligible"]) | {
        symbol for symbols in facts["reasons"].values() for symbol in symbols
    })
    active = sorted(set(facts["expected_active"]))
    rejected = {reason: sorted(set(symbols)) for reason, symbols in facts["reasons"].items()}
    suspended = sorted(set(facts["suspended"]))
    if suspended:
        rejected["known_suspended"] = suspended
    kept = [symbol for symbol in active if symbol in set(considered)]
    if considered and len(kept) + sum(len(v) for v in rejected.values()) != len(considered):
        # 计数对不上就说明快照的清单不完整（例如 expected_active 落在 considered 之外）：
        # 与其落成一条自相矛盾的留档，不如不落。
        return ()
    universe_stage = record_stage(
        stage="universe",
        kind=KIND_HARD_GATE,
        input_symbols=considered,
        advanced_symbols=considered,
        data_as_of=data_as_of,
        contract=contract,
        model_identity=model_identity,
        notes=(f"universe_snapshot_id={facts['snapshot_id']} "
               f"survivorship_coverage={facts['coverage']} "
               f"delisting_coverage_verified={facts['delisting_verified']}"
               + (f" membership_source_path={facts['membership_source_path']}"
                  if facts["membership_source_path"] else "")),
    )
    eligibility_stage = record_stage(
        stage="hard_eligibility",
        kind=KIND_HARD_GATE,
        input_symbols=considered,
        advanced_symbols=kept,
        rejected=rejected,
        # §2 要这一层记下它读了什么：判定输入列（由生产者按当天真的跑过的规则给），
        # 不是模型特征清单，所以只在这份清单存在时才填，旧 payload 留空。
        features_used=facts["gate_input_columns"],
        data_as_of=data_as_of,
        contract=contract,
        model_identity=model_identity,
        notes=(
            "known_suspended 是 eligible 但窗口内无 bar，按契约不等于证明停牌；"
            "一只票同时踩中多条硬门时只记第一条，归因顺序来自契约："
            f"{'|'.join(HARD_GATE_ATTRIBUTION_ORDER)}"
            + (f"；feature_compute_version={facts['feature_contract_version']}"
               if facts["feature_contract_version"] else "")
            + (f"；float_cap_interpretation={facts['float_cap_interpretation_version']}"
               if facts["float_cap_interpretation_version"] else "")
        ),
    )
    return (universe_stage, eligibility_stage)


def build_night_scan_funnel_trace(
    *,
    report: Mapping[str, Any],
    trade_date: date | datetime | str,
    contract: TrendStrategyContract,
    model_identity: ModelIdentity | Mapping[str, Any] | None = None,
    features_used: Sequence[str] = (),
    universe: Any = None,
) -> FunnelTrace | None:
    """从夜扫报告构造夜扫半段留档；报告里没有成员时返回 ``None``（不编造）。"""
    prefilter = report.get("prefilter")
    if not isinstance(prefilter, Mapping):
        return None
    members = {layer: _layer_symbols(prefilter, layer) for layer, _ in LAYER_FIELDS}
    if not any(members.values()):
        return None

    day = _as_day(trade_date)
    data_as_of = str(report.get("data_snapshot_id") or report.get("timestamp")
                     or report.get("scan_completed_at") or day.isoformat())
    features = tuple(str(name) for name in features_used)

    stages = []
    previous: list[str] = []
    for layer, _ in LAYER_FIELDS:
        current = members[layer]
        current_set = set(current)
        dropped = [symbol for symbol in previous if symbol not in current_set]
        outside = sorted(symbol for symbol in current if symbol not in set(previous))
        # 第一层没有上一层可比，inputs 就是本层成员（没有淘汰可记）。
        pool = sorted(set(previous) | set(current)) if previous else current
        notes = ""
        if previous and outside:
            notes = (
                f"membership_not_nested: {','.join(outside)[:400]} 不在上一层成员里"
                "（板块配额/pinned 注入）；inputs 已按两层成员并集计"
            )
        stages.append(record_stage(
            stage=layer,
            kind=KIND_PREDICTIVE,
            input_symbols=pool,
            advanced_symbols=current,
            rejected={NIGHT_UNATTRIBUTED_DROP: dropped} if dropped else {},
            features_used=features,
            data_as_of=data_as_of,
            contract=contract,
            model_identity=model_identity,
            notes=notes,
        ))
        previous = current
    universe_stages = build_universe_stage_traces(
        universe=universe, data_as_of=data_as_of, contract=contract,
        model_identity=model_identity,
    )
    return build_funnel_trace(
        trade_date=day, stages=[*universe_stages, *stages], contract=contract
    )


__all__ = [
    "LAYER_FIELDS",
    "MEMBERSHIP_PATHS",
    "build_universe_stage_traces",
    "NIGHT_TRACE_SUFFIX",
    "NIGHT_UNATTRIBUTED_DROP",
    "build_night_scan_funnel_trace",
    "live_universe_facts",
    "locate_hard_gate_membership",
    "universe_membership_status",
]
