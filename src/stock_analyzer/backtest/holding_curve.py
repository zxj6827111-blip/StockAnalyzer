"""持有期走势分析（holding curve），PLAN Task 4。

给定股票清单与买入日，按**交易日**（非自然日）逐日推进，计算每日收益率、期间
最高/最低、最优退出日及其收益、最大回撤，并用 :class:`ExecutionMatcher`
（复用 ``backtest/matcher.py``，与 ``walk_forward.py`` 同一套涨跌停/T+1/滑点/
成本规则）模拟真实可成交的退出，避免把无法成交的理想收益当成回测结果。

两套「收益」故意分开呈现，不互相覆盖：

- ``daily_returns``：忽略涨跌停约束的纯价格收益曲线（回答「如果不考虑成交约束，
  第几天卖最赚」），供前端画逐日走势曲线。
- ``matched_exit``：``ExecutionMatcher.simulate_exit`` 给出的真实可成交退出
  （涨跌停不可成交时延迟、超期强制平仓、跳空止损等），是本模块「收益统计」
  的口径依据。

交易日推进优先直接使用 ``bars.index``（真实数据，行情本身自带交易日历），不
依赖 ``data/trading_calendar.py`` 的静态节假日表去猜测下一个交易日——遇到停牌
或数据缺口时，直接按 bars 里实际存在的下一根记录推进，与真实回测/实盘的数据
可用性完全一致。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, cast

import pandas as pd

from stock_analyzer.backtest.matcher import ExecutionMatcher, ExitSimulation

# ExecutionMatcher.simulate_exit 的止盈止损默认值对齐 config.py LabelConfig
# 默认值（take_profit_pct=0.08 / stop_loss_pct=0.05 / horizon_days=10），
# 保证「不特别指定参数」时的回测口径与生产训练标签口径一致。
_DEFAULT_TAKE_PROFIT_PCT = 0.08
_DEFAULT_STOP_LOSS_PCT = 0.05
_DEFAULT_HORIZON_DAYS = 10


@dataclass(slots=True)
class HoldingDayReturn:
    """单个持有交易日（T+N）的快照。"""

    offset: int  # T+N 的 N（从 1 开始）
    trade_date: date
    close: float
    return_pct: float  # (close - entry_price) / entry_price
    high_return_pct: float  # 当日最高价对应的收益率（期间内瞬时最优）
    low_return_pct: float  # 当日最低价对应的收益率（期间内瞬时最差）


@dataclass(slots=True)
class SymbolHoldingResult:
    """单只标的的完整持有期分析结果。"""

    symbol: str
    entry_date: date
    entry_price: float
    status: str  # "ok" | "no_fill" | "insufficient_data" | "error"
    error: str = ""
    horizon_days: int = 0
    available_trading_days: int = 0
    daily_returns: list[HoldingDayReturn] = field(default_factory=list)
    best_exit_offset: int = 0  # 纯价格口径下收益最高的 T+N（0 表示未找到）
    best_exit_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0  # 期间相对入场价的最大回撤（负值或 0）
    take_profit_triggered: bool = False
    stop_loss_triggered: bool = False
    matched_exit: ExitSimulation | None = None
    matched_net_return_pct: float = 0.0  # 计入成本后的真实净收益（仅 executed 时有意义）
    # --- S02：T+1 入场契约 ---
    signal_date: date | None = None  # 产生信号的交易日（T，收盘后决策）
    entry_delay_days: int = 0  # 成交日相对信号日的交易日延迟（主口径恒为 1）
    entry_price_raw: float = 0.0  # raw 开盘价（滑点前）
    entry_slippage: float = 0.0  # 滑点后的成交价 - raw 开盘价
    entry_cost: float = 0.0  # 买入成本（按 quantity=1000 估算）
    no_fill_reason: str = ""  # 未成交原因（status == "no_fill" 时必有值）
    entry_mode: str = "next_session_open"  # next_session_open | next_tradable_open


@dataclass(slots=True)
class HoldingCurveSummary:
    """多只标的汇总统计。"""

    symbol_count: int = 0
    ok_count: int = 0
    win_count: int = 0  # matched_net_return_pct > 0 的标的数
    loss_count: int = 0
    win_rate: float = 0.0
    avg_best_exit_offset: float = 0.0  # 平均最优持有天数（纯价格口径）
    profit_loss_ratio: float = 0.0  # 平均盈利 / 平均亏损（绝对值），无亏损时为 0
    # 各持有天数（T+1..T+N）的平均收益分布：直接回答「第几天卖最赚」。
    avg_return_by_offset: dict[int, float] = field(default_factory=dict)
    # S02：买入未成交（不可成交）的标的数及其原因分布——这些标的**不进入**
    # 收益统计，但必须可审计（可成交率是 V2 的核心指标之一）。
    no_fill_count: int = 0
    no_fill_reason_counts: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class HoldingCurveReport:
    entry_date: date
    horizon_days: int
    results: list[SymbolHoldingResult] = field(default_factory=list)
    summary: HoldingCurveSummary = field(default_factory=HoldingCurveSummary)


def _bar_snapshot(row: pd.Series) -> dict[str, float | bool]:
    """行情行 -> ExecutionMatcher 期望的 bar dict（与 walk_forward._bar_snapshot 同构）。

    与 ``backtest/walk_forward.py::_bar_snapshot`` 保持逐字段一致，因为两者都
    喂给同一个 ``ExecutionEngine.can_buy``/``can_sell``（经 ExecutionMatcher
    转发），字段缺一都会导致涨跌停判定退化为不可靠的猜测。
    """
    close = float(row.get("close", 0.0))
    open_price = float(row.get("open", close))
    high_price = float(row.get("high", max(open_price, close)))
    low_price = float(row.get("low", min(open_price, close)))
    # S07（DF-S02-001）：**不再注入** close*1.1/0.9 的估算涨跌停。
    # 估算值会被 ExecutionEngine 的 use_source_first 当成权威 source 值，从而掩盖
    # 真实板块涨跌幅（ST 5% / 创业板科创板 20% / IPO 无限制）——实测一字涨停在该
    # 路径下会被判成"可成交"。这里只透传真实存在的列；缺列时引擎按 pre_close/board
    # 解析，仍解析不出就 fail-closed（no_valid_price_data），不猜测。
    snapshot: dict[str, float | bool] = {
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close,
        "suspended": bool(row.get("suspended", False)),
    }
    optional_keys = ("up_limit", "down_limit", "pre_close", "pct_change", "is_st", "name", "board")
    for optional_key in optional_keys:
        if optional_key not in row:
            continue
        value = row.get(optional_key)
        if value is None:
            continue
        if isinstance(value, float) and not math.isfinite(value):
            continue  # NaN/Inf：视为缺失（与 limit_rule 同口径）
        snapshot[optional_key] = value
    return snapshot


def _future_bars(
    bars: pd.DataFrame,
    anchor_pos: int,
    horizon_days: int,
) -> list[tuple[datetime, dict[str, float | bool]]]:
    """从入场位置之后按交易日切片（复用 walk_forward._future_bars 的切片语义）。

    与 ``ExecutionMatcher.max_exit_carry_days`` 配合：调用方应传入
    ``horizon_days + max_exit_carry_days + 1`` 之类的缓冲窗口，让延迟成交/强制
    平仓有足够的未来 bar 可以推进，而不是卡在 horizon_days 正好截断。
    """
    if horizon_days <= 0:
        return []
    start = anchor_pos + 1
    end = min(len(bars), start + horizon_days)
    result: list[tuple[datetime, dict[str, float | bool]]] = []
    for pos in range(start, end):
        ts = bars.index[pos]
        date_value = (
            ts.to_pydatetime() if isinstance(ts, pd.Timestamp) else pd.Timestamp(ts).to_pydatetime()
        )
        result.append((date_value, _bar_snapshot(bars.iloc[pos])))
    return result


def _resolve_entry_position(bars: pd.DataFrame, entry_date: date) -> int | None:
    """在 bars.index 中定位 entry_date（或之后最近一个交易日）对应的整数位置。

    真实持仓的买入日未必恰好等于请求的 as_of 日期（例如该日停牌、非交易日）；
    这里直接按 bars 里实际存在的记录定位，不使用静态节假日表猜测——数据本身
    就是最可靠的交易日历。找不到匹配或更晚的记录时返回 None。
    """
    if bars.empty:
        return None
    index = bars.index
    if not isinstance(index, pd.DatetimeIndex):
        index = pd.DatetimeIndex(pd.to_datetime(index, errors="coerce"))
    entry_ts = pd.Timestamp(entry_date)
    positions = index.searchsorted(entry_ts, side="left")
    if positions >= len(index):
        return None
    return int(positions)


def _bar_datetime(bar_date: object) -> datetime:
    """bar 的 index 值 → 该交易日的 datetime（当日 00:00）。"""
    if isinstance(bar_date, pd.Timestamp):
        return bar_date.to_pydatetime()
    if isinstance(bar_date, datetime):
        return bar_date
    return pd.Timestamp(cast(Any, bar_date)).to_pydatetime()


def analyze_symbol_holding(
    *,
    symbol: str,
    bars: pd.DataFrame,
    entry_date: date,
    matcher: ExecutionMatcher,
    horizon_days: int = _DEFAULT_HORIZON_DAYS,
    take_profit_pct: float = _DEFAULT_TAKE_PROFIT_PCT,
    stop_loss_pct: float = _DEFAULT_STOP_LOSS_PCT,
    slippage_ratio: float = 0.0,
    max_entry_sessions: int = 1,
) -> SymbolHoldingResult:
    """单只标的的持有期走势分析（S02：入场为 T+1 真实可成交开盘价）。

    ``entry_date`` 的语义在 S02 起是**信号日 T**（盘后决策），不是买入日：
    买入发生在 T 之后第一个可成交交易日的开盘，成交价 = raw 开盘价 + 滑点；
    T 日收盘价**不再**被当作入场价（那是不可实现的成交，蓝图 §2.12）。

    Args:
        symbol: 标的代码。
        bars: 该标的的日线 DataFrame（index 为 DatetimeIndex，至少含
            open/high/low/close；可选 up_limit/down_limit/suspended）。
        entry_date: 信号日 T（收盘后决策时点所属交易日）。
        matcher: 复用的 ExecutionMatcher 实例（涨跌停/T+1/滑点/成本规则）。
        horizon_days: 目标持有交易日数（默认 10，对齐 config.py labels 默认值）。
        take_profit_pct/stop_loss_pct: 止盈止损百分比（默认对齐 labels 配置）。
        slippage_ratio: 买入滑点比例（默认 0，调用方按策略/波动给定）。
        max_entry_sessions: 入场延迟窗口（交易日）。1 = 主口径（只能 T+1）；
            >1 = sensitivity（允许顺延到窗口内下一可成交开盘）。窗口内的成交日
            在 ``entry_delay_days`` 如实标注，两者不得混成一个主结果。
    """
    if bars.empty:
        return SymbolHoldingResult(
            symbol=symbol,
            entry_date=entry_date,
            entry_price=0.0,
            status="insufficient_data",
            error="empty_bars",
        )

    normalized_bars = bars if isinstance(bars.index, pd.DatetimeIndex) else bars.copy()
    if not isinstance(normalized_bars.index, pd.DatetimeIndex):
        normalized_bars.index = pd.DatetimeIndex(
            pd.to_datetime(normalized_bars.index, errors="coerce")
        )
        normalized_bars = normalized_bars[normalized_bars.index.notna()].sort_index()

    anchor_pos = _resolve_entry_position(normalized_bars, entry_date)
    if anchor_pos is None:
        return SymbolHoldingResult(
            symbol=symbol,
            entry_date=entry_date,
            entry_price=0.0,
            status="insufficient_data",
            error="entry_date_not_found_in_bars",
        )

    signal_row_date = _bar_datetime(normalized_bars.index[anchor_pos])
    # 入场候选 = 信号日**之后**的 bar（T+1 起）。信号日当天的 bar 只用于定位，
    # 绝不作为成交价来源。
    entry_window = _future_bars(
        normalized_bars,
        anchor_pos=anchor_pos,
        horizon_days=max(1, int(max_entry_sessions)),
    )
    entry = matcher.simulate_entry(
        signal_date=signal_row_date,
        future_bars=entry_window,
        slippage_ratio=slippage_ratio,
        max_entry_sessions=max(1, int(max_entry_sessions)),
    )
    if not entry.executed or entry.entry_date is None:
        return SymbolHoldingResult(
            symbol=symbol,
            entry_date=entry_date,
            entry_price=0.0,
            status="no_fill",
            error=entry.no_fill_reason,
            signal_date=signal_row_date.date(),
            entry_delay_days=0,
            entry_price_raw=0.0,
            entry_slippage=0.0,
            entry_cost=0.0,
            no_fill_reason=entry.no_fill_reason,
            entry_mode=(
                "next_session_open" if max_entry_sessions <= 1 else "next_tradable_open"
            ),
            horizon_days=horizon_days,
        )

    entry_price = float(entry.net_entry_price)
    actual_entry_date_value = entry.entry_date.date()
    if entry_price <= 0:
        return SymbolHoldingResult(
            symbol=symbol,
            entry_date=actual_entry_date_value,
            entry_price=entry_price,
            status="error",
            error="non_positive_entry_price",
            signal_date=signal_row_date.date(),
            entry_delay_days=entry.entry_delay_days,
            no_fill_reason="",
        )

    # 入场后的 bar 位置：entry_delay_days 是交易日延迟（1 = T+1）。
    entry_pos = anchor_pos + entry.entry_delay_days
    # 缓冲窗口对齐 walk_forward.py 的做法：多留 max_exit_carry_days + 1 根，
    # 让延迟成交/强制平仓有足够未来 bar 推进，不被 horizon_days 正好截断。
    buffer_horizon = horizon_days + matcher.max_exit_carry_days + 1
    future = _future_bars(normalized_bars, anchor_pos=entry_pos, horizon_days=buffer_horizon)
    available_trading_days = min(len(future), horizon_days)

    daily_returns: list[HoldingDayReturn] = []
    max_drawdown_pct = 0.0
    take_profit_triggered = False
    stop_loss_triggered = False
    take_profit_level = entry_price * (1.0 + max(0.0, take_profit_pct))
    stop_loss_level = entry_price * (1.0 - max(0.0, stop_loss_pct))

    for offset, (trade_dt, bar) in enumerate(future[:horizon_days], start=1):
        close = float(bar.get("close", 0.0))
        high = float(bar.get("high", close))
        low = float(bar.get("low", close))
        return_pct = (close - entry_price) / entry_price
        high_return_pct = (high - entry_price) / entry_price
        low_return_pct = (low - entry_price) / entry_price
        daily_returns.append(
            HoldingDayReturn(
                offset=offset,
                trade_date=trade_dt.date(),
                close=close,
                return_pct=return_pct,
                high_return_pct=high_return_pct,
                low_return_pct=low_return_pct,
            )
        )
        max_drawdown_pct = min(max_drawdown_pct, low_return_pct)
        if high >= take_profit_level:
            take_profit_triggered = True
        if low <= stop_loss_level:
            stop_loss_triggered = True

    best_exit_offset = 0
    best_exit_return_pct = 0.0
    if daily_returns:
        best_day = max(daily_returns, key=lambda item: item.return_pct)
        best_exit_offset = best_day.offset
        best_exit_return_pct = best_day.return_pct

    matched_exit = matcher.simulate_exit(
        entry_price=entry_price,
        entry_date=datetime.combine(actual_entry_date_value, datetime.min.time()),
        future_bars=future,
        take_profit_pct=take_profit_pct,
        stop_loss_pct=stop_loss_pct,
        horizon_days=horizon_days,
    )
    matched_net_return_pct = 0.0
    if matched_exit.executed:
        gross_return = (matched_exit.exit_price - entry_price) / entry_price
        round_trip_cost = _estimate_round_trip_cost(
            matcher=matcher,
            buy_price=entry_price,
            sell_price=matched_exit.exit_price,
        )
        matched_net_return_pct = gross_return - round_trip_cost

    return SymbolHoldingResult(
        symbol=symbol,
        entry_date=actual_entry_date_value,
        entry_price=entry_price,
        status="ok",
        horizon_days=horizon_days,
        available_trading_days=available_trading_days,
        daily_returns=daily_returns,
        best_exit_offset=best_exit_offset,
        best_exit_return_pct=best_exit_return_pct,
        max_drawdown_pct=max_drawdown_pct,
        take_profit_triggered=take_profit_triggered,
        stop_loss_triggered=stop_loss_triggered,
        matched_exit=matched_exit,
        matched_net_return_pct=matched_net_return_pct,
        signal_date=signal_row_date.date(),
        entry_delay_days=entry.entry_delay_days,
        entry_price_raw=entry.entry_price_raw,
        entry_slippage=entry.slippage,
        entry_cost=entry.cost,
        entry_mode=(
            "next_session_open" if max_entry_sessions <= 1 else "next_tradable_open"
        ),
    )


def _estimate_round_trip_cost(
    matcher: ExecutionMatcher,
    buy_price: float,
    sell_price: float,
    quantity: int = 1000,
) -> float:
    """买卖双边成本占比（与 walk_forward._estimate_round_trip_cost 同构）。"""
    amount = buy_price * quantity
    if amount <= 0:
        return 0.0
    buy_cost = float(matcher.estimate_cost("buy", price=buy_price, quantity=quantity))
    sell_cost = float(matcher.estimate_cost("sell", price=sell_price, quantity=quantity))
    return (buy_cost + sell_cost) / amount


def analyze_holding_curve(
    *,
    bars_by_symbol: Mapping[str, pd.DataFrame],
    entry_date: date,
    matcher: ExecutionMatcher,
    horizon_days: int = _DEFAULT_HORIZON_DAYS,
    take_profit_pct: float = _DEFAULT_TAKE_PROFIT_PCT,
    stop_loss_pct: float = _DEFAULT_STOP_LOSS_PCT,
    symbols: Sequence[str] | None = None,
    slippage_ratio: float = 0.0,
    max_entry_sessions: int = 1,
) -> HoldingCurveReport:
    """对一批标的跑持有期走势分析，并产出汇总统计。

    Args:
        bars_by_symbol: symbol -> 日线 DataFrame 的映射（调用方负责提供，
            通常是 as-of 扫描结果里 buy 候选对应的完整历史行情，覆盖到
            entry_date 之后 horizon_days 根记录或截止今日）。
        entry_date: 统一买入日（S02 起语义为**信号日 T**）。
        matcher: 复用的 ExecutionMatcher 实例。
        symbols: 可选的标的子集/顺序（None 时使用 bars_by_symbol 的全部键，
            按输入顺序）。
        slippage_ratio: 买入滑点比例（S07/DF-S02-003：调用方应传策略静态滑点，
            不再默认 0）。**半批审 B2 修复**：此前本函数没有该参数，服务层却按
            关键字传入 → TypeError，导致"有候选的 as-of 回测"必崩、滑点修复空转。
        max_entry_sessions: 入场延迟窗口（1 = 主口径 T+1；>1 = sensitivity）。
    """
    ordered_symbols = list(symbols) if symbols is not None else list(bars_by_symbol.keys())
    results: list[SymbolHoldingResult] = []
    for symbol in ordered_symbols:
        bars = bars_by_symbol.get(symbol, pd.DataFrame())
        try:
            result = analyze_symbol_holding(
                symbol=symbol,
                bars=bars,
                entry_date=entry_date,
                matcher=matcher,
                horizon_days=horizon_days,
                take_profit_pct=take_profit_pct,
                stop_loss_pct=stop_loss_pct,
                slippage_ratio=slippage_ratio,
                max_entry_sessions=max_entry_sessions,
            )
        except Exception as exc:  # noqa: BLE001 - 单只票的意外异常不能打断整批
            result = SymbolHoldingResult(
                symbol=symbol,
                entry_date=entry_date,
                entry_price=0.0,
                status="error",
                error=f"{type(exc).__name__}: {exc}",
            )
        results.append(result)

    summary = _summarize(results, horizon_days=horizon_days)
    return HoldingCurveReport(
        entry_date=entry_date,
        horizon_days=horizon_days,
        results=results,
        summary=summary,
    )


def _summarize(results: list[SymbolHoldingResult], *, horizon_days: int) -> HoldingCurveSummary:
    ok_results = [item for item in results if item.status == "ok"]
    # S02：未成交（no_fill）必须**计数可见**：这类票不进收益统计，但"有多少票
    # 根本买不到"本身就是研究结论（蓝图 §1.2 的 no-fill / 可成交率指标）。
    no_fill_results = [item for item in results if item.status == "no_fill"]
    no_fill_counts: dict[str, int] = {}
    for item in no_fill_results:
        reason = item.no_fill_reason or "unknown"
        no_fill_counts[reason] = no_fill_counts.get(reason, 0) + 1
    if not ok_results:
        return HoldingCurveSummary(
            symbol_count=len(results),
            ok_count=0,
            no_fill_count=len(no_fill_results),
            no_fill_reason_counts=no_fill_counts,
        )

    wins = [item for item in ok_results if item.matched_net_return_pct > 0]
    losses = [item for item in ok_results if item.matched_net_return_pct <= 0]
    win_rate = len(wins) / len(ok_results) if ok_results else 0.0
    avg_win = (
        sum(item.matched_net_return_pct for item in wins) / len(wins) if wins else 0.0
    )
    avg_loss = (
        sum(abs(item.matched_net_return_pct) for item in losses) / len(losses) if losses else 0.0
    )
    profit_loss_ratio = (avg_win / avg_loss) if avg_loss > 0 else 0.0
    avg_best_exit_offset = (
        sum(item.best_exit_offset for item in ok_results) / len(ok_results)
    )

    avg_return_by_offset: dict[int, float] = {}
    for offset in range(1, horizon_days + 1):
        offset_returns = [
            day.return_pct
            for item in ok_results
            for day in item.daily_returns
            if day.offset == offset
        ]
        if offset_returns:
            avg_return_by_offset[offset] = sum(offset_returns) / len(offset_returns)

    return HoldingCurveSummary(
        symbol_count=len(results),
        ok_count=len(ok_results),
        win_count=len(wins),
        loss_count=len(losses),
        win_rate=win_rate,
        avg_best_exit_offset=avg_best_exit_offset,
        profit_loss_ratio=profit_loss_ratio,
        avg_return_by_offset=avg_return_by_offset,
        no_fill_count=len(no_fill_results),
        no_fill_reason_counts=no_fill_counts,
    )
