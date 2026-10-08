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
from stock_analyzer.research.funnel_trace import (
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


def build_night_scan_funnel_trace(
    *,
    report: Mapping[str, Any],
    trade_date: date | datetime | str,
    contract: TrendStrategyContract,
    model_identity: ModelIdentity | Mapping[str, Any] | None = None,
    features_used: Sequence[str] = (),
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
    return build_funnel_trace(trade_date=day, stages=stages, contract=contract)


__all__ = [
    "LAYER_FIELDS",
    "NIGHT_TRACE_SUFFIX",
    "NIGHT_UNATTRIBUTED_DROP",
    "build_night_scan_funnel_trace",
]
