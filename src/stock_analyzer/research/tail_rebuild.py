"""历史重建的**消费侧**：把研究库里的参考数据接进尾盘标签权威（改进计划 §3.1/§3.3）。

前面几层各自解决了一半问题：``TailReferenceStore`` 知道怎么落五类日级参考数据，
``MinuteBarStore`` 知道怎么按分钟吐出带时刻的 raw bar，``build_tail_net_profit_label``
是线上与历史共用的唯一判定入口。这一模块只做**接线**，不新增任何判定规则：

* ``daily_bar_series()`` 出 ``[(日期, raw 日线)]``，逐日带上精确涨跌停与状态声明；
* ``day_limits()`` 出分钟 bar 缺的那几列，让"涨停锁死""停牌"在纯研究库路径上可判；
* 判定仍然只有 ``build_tail_net_profit_label`` 一个出口 —— 这里不重算净收益。

为什么要单独拦一层"数据不足"：``evaluate_tail_entry`` 在缺少 ``up_limit`` 时判
``no_valid_price_data``，这在**线上**是对的（那一刻确实买不进），但在**重建**里
它是"我们的参考数据不足以判断"，不是"这只股票买不进"。两者若混在同一个
``not_filled`` 计数里，成交率就会被数据缺口污染，§4 的对比基线也就不可信。
所以这里把参考数据缺口单独记成 ``insufficient_reference_data``，既不进样本流，
也不冒充亏损或未成交。

三条写死的边界：

1. 缺分钟 bar 就是 ``insufficient``，**绝不回落到开盘价**（§5：开盘回测不能代替尾盘）。
2. 日历里应开市、日线上却没有的日子（且落在已观测序列之内）→ 第 5 个交易日会算错，
   直接 ``insufficient``，不"跳过这天继续"。
3. 本模块只产 ``replayed_recompute`` 样本；真实观察样本由线上留档走另一条路，
   两者永远分开报告（§3.3）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    TrendStrategyContract,
)
from stock_analyzer.labels.tail_net_profit import (
    CAPTURE_REPLAYED,
    LABEL_PROFIT,
    TailLabelRecord,
    build_tail_net_profit_label,
)
from stock_analyzer.research.minute_bar_store import MinuteBarStore, MinuteStoreError
from stock_analyzer.research.tail_reference_store import TailReferenceStore

#: 参考数据不足以判定，与"判定结果是未成交/亏损"严格区分。
STATUS_INSUFFICIENT = "insufficient_reference_data"


class TailRebuildError(RuntimeError):
    """重建请求本身不合法（入场日不晚于决策日这类结构性错误）。"""


@dataclass(frozen=True)
class RebuildRequest:
    """一个待重建样本：夜扫决策日 + 次日的尾盘入场日。"""

    symbol: str
    decision_date: date
    entry_date: date
    #: 夜扫时冻结的特征快照；重建不能现场重算它（那会把 as-of 语义打穿）。
    overnight_features: Mapping[str, Any] = field(default_factory=dict)
    model_probabilities: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.symbol).strip():
            raise TailRebuildError("rebuild request needs a symbol")
        if self.entry_date <= self.decision_date:
            raise TailRebuildError(
                f"entry_date {self.entry_date} must be after decision_date "
                f"{self.decision_date}: 确认与成交只能在夜扫次日的尾盘窗口"
            )


@dataclass(frozen=True)
class TailRebuildOutcome:
    """一个请求的重建结果：要么是一条标签记录，要么是一份数据缺口清单。"""

    request: RebuildRequest
    sufficient: bool
    missing: tuple[str, ...]
    record: TailLabelRecord | None
    minute_bar_count: int
    session_days: int
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def reason(self) -> str:
        if self.sufficient and self.record is not None:
            return self.record.reason or self.record.status
        return STATUS_INSUFFICIENT

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "symbol": self.request.symbol,
            "decision_date": self.request.decision_date.isoformat(),
            "entry_date": self.request.entry_date.isoformat(),
            "sufficient": self.sufficient,
            "missing_reference_inputs": list(self.missing),
            "minute_bar_count": self.minute_bar_count,
            "session_days": self.session_days,
            "capture_mode": CAPTURE_REPLAYED,
        }
        if self.record is not None:
            payload["label"] = self.record.to_dict()
        if self.detail:
            payload["detail"] = dict(self.detail)
        return payload


def exit_window_end(entry_date: date, contract: TrendStrategyContract) -> date:
    """把"再要若干个交易日"换算成一个**保守**的日历上界。

    取数时还不知道入场日之后哪些日子是交易日，所以按周末加长假放宽：
    N 个交易日必定落在 ``2N+4`` 个日历日内。真正的序列完整性由
    ``daily_bar_series()`` 对照权威日历判定，这里只管多取。
    """
    sessions = contract.holding_days + contract.exit_defer_max_sessions + 1
    return entry_date + timedelta(days=2 * sessions + 4)


def _entry_minute_bars(
    minutes: MinuteBarStore,
    request: RebuildRequest,
    *,
    interval: str,
    day_limits: Mapping[str, Any],
) -> tuple[list[Any], list[str], dict[str, Any]]:
    """读入场日分钟 bar；口径不纯直接判缺，不把异常抛给整批重建。"""
    try:
        bars = minutes.bars_for(
            request.symbol, request.entry_date, interval=interval, day_limits=day_limits
        )
    except MinuteStoreError as exc:
        # ADR-002：混着复权价的分钟序列根本不能模拟成交，原因如实写进 detail。
        return [], ["minute_bars_price_basis_not_raw"], {"minute_store_error": str(exc)}
    if not bars:
        # 缺 bar 就是缺 bar：不回落到开盘价，也不把它当成停牌。
        return [], ["entry_minute_bars"], {}
    return bars, [], {}


def rebuild_tail_label(
    *,
    reference: TailReferenceStore,
    minutes: MinuteBarStore,
    request: RebuildRequest,
    confirmation: Callable[..., tuple[bool, str]],
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
    interval: str = "1m",
    cost_estimator: Callable[[str, float, int, Any], float] | None = None,
    price_ticker: Callable[[float, str], float] | None = None,
    slippage_ratio: float = 0.0,
    model_version: str = "",
    market_state: str = "",
) -> TailRebuildOutcome:
    """用研究库的参考数据重建一个 ``p_net_profit_5d_tail`` 样本。

    ``confirmation`` 必须由调用方传入**与线上同一个**谓词：§4 要求相同输入下线上与
    历史产生一致的筛选判定，这一点靠"两边共用同一个函数"来保证，不靠两边各写一份。
    """
    day_limits = reference.day_limits(request.symbol, request.entry_date)
    series = reference.daily_bar_series(
        request.symbol, request.entry_date, exit_window_end(request.entry_date, contract)
    )
    session_days = [day for day, _bar in series["sessions"]]
    has_entry_bar = request.entry_date in session_days

    missing: list[str] = []
    detail: dict[str, Any] = {}
    if not has_entry_bar:
        missing.append("entry_daily_bar")
    if not series["calendar_declared"]:
        # 没有权威日历就无法校验序列完整性，也就无法保证"第 5 日"落在正确的交易日。
        missing.append("trade_calendar_missing")
    if not day_limits.get("up_limit") or not day_limits.get("down_limit"):
        missing.append("entry_day_limit_prices")

    bars, minute_missing, minute_detail = _entry_minute_bars(
        minutes, request, interval=interval, day_limits=day_limits
    )
    missing.extend(minute_missing)
    detail.update(minute_detail)

    # 已观测序列**内部**的空洞才改写"第 N 日"；序列末尾之后的空缺交给契约判
    # insufficient_data_at_series_end（那是样本未成熟，不是数据缺失）。
    holes: list[str] = []
    if session_days:
        last_seen = max(session_days)
        holes = [
            day for day in series["missing_session_days"]
            if date.fromisoformat(day) < last_seen
        ]
        if holes:
            missing.append("daily_bar_session_holes")

    missing = list(dict.fromkeys(missing))
    if missing:
        detail.update({
            "missing_session_days": list(series["missing_session_days"]),
            "session_holes_inside_window": holes,
            "missing_limit_price_days": list(series["missing_limit_price_days"]),
            "undeclared_trade_status_days": list(series["undeclared_status_days"]),
        })
        return TailRebuildOutcome(
            request=request, sufficient=False, missing=tuple(missing), record=None,
            minute_bar_count=len(bars), session_days=len(session_days), detail=detail,
        )

    record = build_tail_net_profit_label(
        symbol=request.symbol,
        decision_date=request.decision_date,
        entry_date=request.entry_date,
        minute_bars=bars,
        daily_bars=series["sessions"],
        confirmation=confirmation,
        contract=contract,
        cost_estimator=cost_estimator,
        price_ticker=price_ticker,
        slippage_ratio=slippage_ratio,
        model_probabilities=request.model_probabilities,
        overnight_features=request.overnight_features,
        capture_mode=CAPTURE_REPLAYED,
        model_version=model_version,
        market_state=market_state,
    )
    detail.update({
        "undeclared_trade_status_days": list(series["undeclared_status_days"]),
        "session_days_observed": len(session_days),
        # 状态未声明不会让重建失败，但它必然让这一天出不来已实现盈亏；
        # 报告要能说明"标签为空"是数据来源缺口，而不是策略不行。
        "trade_status_source_gap": bool(series["undeclared_status_days"]),
    })
    return TailRebuildOutcome(
        request=request, sufficient=True, missing=(), record=record,
        minute_bar_count=len(bars), session_days=len(session_days), detail=detail,
    )


def rebuild_tail_labels(
    *,
    reference: TailReferenceStore,
    minutes: MinuteBarStore,
    requests: Sequence[RebuildRequest],
    confirmation: Callable[..., tuple[bool, str]],
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
    interval: str = "1m",
    cost_estimator: Callable[[str, float, int, Any], float] | None = None,
    price_ticker: Callable[[float, str], float] | None = None,
    slippage_ratio: float = 0.0,
    model_version: str = "",
    market_state: str = "",
) -> list[TailRebuildOutcome]:
    """批量重建。单条请求的参考数据缺口不中断整批（缺口计数见 ``summarize``）。"""
    return [
        rebuild_tail_label(
            reference=reference, minutes=minutes, request=request,
            confirmation=confirmation, contract=contract, interval=interval,
            cost_estimator=cost_estimator, price_ticker=price_ticker,
            slippage_ratio=slippage_ratio, model_version=model_version,
            market_state=market_state,
        )
        for request in requests
    ]


def summarize_rebuild(outcomes: Sequence[TailRebuildOutcome]) -> dict[str, Any]:
    """成交率、标签成熟度与数据缺口**分开**报（§3.3：未成交不计盈亏，缺口不计未成交）。"""
    insufficient = [item for item in outcomes if not item.sufficient]
    records = [item.record for item in outcomes if item.record is not None]
    missing_by_input: dict[str, int] = {}
    for item in insufficient:
        for name in item.missing:
            missing_by_input[name] = missing_by_input.get(name, 0) + 1
    filled = sum(1 for record in records if record.filled)
    trainable = sum(1 for record in records if record.trainable)
    status_counts: dict[str, int] = {}
    for record in records:
        key = f"{record.status}:{record.reason}" if record.reason else record.status
        status_counts[key] = status_counts.get(key, 0) + 1
    return {
        "requests": len(outcomes),
        "insufficient": len(insufficient),
        "rebuild_blocked_on_reference_data": bool(insufficient),
        "missing_reference_inputs": dict(sorted(missing_by_input.items())),
        "label_records": len(records),
        "filled": filled,
        #: 成交率分母只算参考数据足够、真的跑过判定的样本。
        "fill_rate": (filled / len(records)) if records else None,
        "trainable": trainable,
        "net_profits": sum(
            1 for record in records if record.trainable and record.label == LABEL_PROFIT
        ),
        "status_reason_counts": dict(sorted(status_counts.items())),
        "trade_status_source_gap_symbols": sum(
            1 for item in outcomes if item.detail.get("trade_status_source_gap")
        ),
        "capture_mode": CAPTURE_REPLAYED,
    }


__all__ = [
    "RebuildRequest",
    "STATUS_INSUFFICIENT",
    "TailRebuildError",
    "TailRebuildOutcome",
    "exit_window_end",
    "rebuild_tail_label",
    "rebuild_tail_labels",
    "summarize_rebuild",
]
