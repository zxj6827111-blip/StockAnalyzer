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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

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
    status: str  # "ok" | "insufficient_data" | "error"
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
    return {
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close,
        "up_limit": float(row.get("up_limit", close * 1.1)),
        "down_limit": float(row.get("down_limit", close * 0.9)),
        "suspended": bool(row.get("suspended", False)),
    }


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


def analyze_symbol_holding(
    *,
    symbol: str,
    bars: pd.DataFrame,
    entry_date: date,
    matcher: ExecutionMatcher,
    horizon_days: int = _DEFAULT_HORIZON_DAYS,
    take_profit_pct: float = _DEFAULT_TAKE_PROFIT_PCT,
    stop_loss_pct: float = _DEFAULT_STOP_LOSS_PCT,
) -> SymbolHoldingResult:
    """单只标的的持有期走势分析。

    Args:
        symbol: 标的代码。
        bars: 该标的的日线 DataFrame（index 为 DatetimeIndex，至少含
            open/high/low/close；可选 up_limit/down_limit/suspended）。
            应覆盖到「截止今日」或至少 entry_date 之后 horizon_days 根记录
            （数据不足时如实返回 available_trading_days 并降级）。
        entry_date: 买入日（用于定位入场 bar；实际入场价取该日或其后最近一根
            记录的收盘价）。
        matcher: 复用的 ExecutionMatcher 实例（涨跌停/T+1/滑点/成本规则）。
        horizon_days: 目标持有交易日数（默认 10，对齐 config.py labels 默认值）。
        take_profit_pct/stop_loss_pct: 止盈止损百分比（默认对齐 labels 配置）。
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

    entry_row = normalized_bars.iloc[anchor_pos]
    entry_price = float(entry_row.get("close", 0.0))
    actual_entry_date = normalized_bars.index[anchor_pos]
    actual_entry_date_value = (
        actual_entry_date.date()
        if isinstance(actual_entry_date, pd.Timestamp)
        else pd.Timestamp(actual_entry_date).date()
    )
    if entry_price <= 0:
        return SymbolHoldingResult(
            symbol=symbol,
            entry_date=actual_entry_date_value,
            entry_price=entry_price,
            status="error",
            error="non_positive_entry_price",
        )

    # 缓冲窗口对齐 walk_forward.py 的做法：多留 max_exit_carry_days + 1 根，
    # 让延迟成交/强制平仓有足够未来 bar 推进，不被 horizon_days 正好截断。
    buffer_horizon = horizon_days + matcher.max_exit_carry_days + 1
    future = _future_bars(normalized_bars, anchor_pos=anchor_pos, horizon_days=buffer_horizon)
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
) -> HoldingCurveReport:
    """对一批标的跑持有期走势分析，并产出汇总统计。

    Args:
        bars_by_symbol: symbol -> 日线 DataFrame 的映射（调用方负责提供，
            通常是 as-of 扫描结果里 buy 候选对应的完整历史行情，覆盖到
            entry_date 之后 horizon_days 根记录或截止今日）。
        entry_date: 统一买入日。
        matcher: 复用的 ExecutionMatcher 实例。
        symbols: 可选的标的子集/顺序（None 时使用 bars_by_symbol 的全部键，
            按输入顺序）。
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
    if not ok_results:
        return HoldingCurveSummary(symbol_count=len(results), ok_count=0)

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
    )
