"""Execution matcher with T+1 and A-share tradability constraints.

Thin shell over the shared ``stock_analyzer.execution.ExecutionEngine``.
All rule atoms (tradability, slippage, tick, rounding, costs) live in the
shared engine; this module keeps the backtest-only exit sequence simulation
(``simulate_exit``) which scans future bars.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime

from stock_analyzer.config import BacktestMatcherConfig, LimitRuleConfig
from stock_analyzer.execution.engine import ExecutionEngine, MatchDecision, OrderPlan

__all__ = [
    "EntrySimulation",
    "ExecutionMatcher",
    "ExitSimulation",
    "MatchDecision",
    "OrderPlan",
]


@dataclass(slots=True)
class EntrySimulation:
    """入场模拟结果（S02：盘后信号 → 次日真实可成交）。

    字段语义（稳定契约，供 outcome / 回测报告消费）：

    - ``executed``：是否真的成交；False 时 ``no_fill_reason`` 必有值；
    - ``signal_date``：产生信号的交易日（T，收盘后决策）；
    - ``entry_date``：实际成交日（主口径必须 > signal_date）；未成交时为 None；
    - ``entry_price_raw``：raw 成交价（未计成本前的开盘价，含滑点前）；
    - ``reference_open_raw``：该成交日 raw 开盘价（审计用，等于滑点前价格）；
    - ``slippage``：滑点后的成交价（含滑点，未含手续费）；
    - ``cost``：按成交价与数量估算的买入成本（手续费等）；
    - ``net_entry_price``：计入滑点后的成交价（成本另计，便于 outcome 计算）；
    - ``no_fill_reason``：suspended / limit_up_open / no_valid_price_data /
      no_future_bars / beyond_delay_window；
    - ``entry_delay_days``：成交日相对 signal_date 的**交易日**延迟（1 = T+1）。
    """

    executed: bool
    signal_date: datetime
    entry_price_raw: float
    reference_open_raw: float
    slippage: float
    cost: float
    entry_date: datetime | None = None
    net_entry_price: float = 0.0
    no_fill_reason: str = ""
    entry_delay_days: int = 0
    deferred_sessions: int = 0
    details: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class ExitSimulation:
    executed: bool
    exit_price: float
    exit_date: datetime
    reason: str
    deferred_days: int = 0
    exit_no_fill: bool = False
    forced_exit: bool = False
    forced_exit_close_date: datetime | None = None
    forced_exit_close_price: float = 0.0


class ExecutionMatcher:
    """Shared engine + backtest-only exit simulation."""

    def __init__(
        self,
        config: BacktestMatcherConfig,
        limit_rule: LimitRuleConfig | None = None,
    ) -> None:
        self._engine = ExecutionEngine(config, limit_rule=limit_rule)

    def can_buy(self, bar: Mapping[str, object]) -> MatchDecision:
        return self._engine.can_buy(bar)

    def can_sell(
        self,
        bar: Mapping[str, object],
        last_buy_date: datetime | None,
        current_date: datetime,
    ) -> MatchDecision:
        return self._engine.can_sell(
            bar=bar,
            last_buy_date=last_buy_date,
            current_date=current_date,
        )

    def dynamic_slippage_ratio(
        self,
        strategy: str,
        atr14: float,
        close: float,
        volume_ratio: float,
    ) -> float:
        return self._engine.dynamic_slippage_ratio(
            strategy=strategy,
            atr14=atr14,
            close=close,
            volume_ratio=volume_ratio,
        )

    def should_downgrade_by_slippage(self, slippage_ratio: float) -> bool:
        return self._engine.should_downgrade_by_slippage(slippage_ratio)

    @property
    def max_exit_carry_days(self) -> int:
        return self._engine.max_exit_carry_days

    def apply_slippage(self, price: float, side: str, slippage_ratio: float) -> float:
        return self._engine.apply_slippage(
            price=price,
            side=side,
            slippage_ratio=slippage_ratio,
        )

    def plan_order(self, *, side: str, price: float, requested_quantity: int) -> OrderPlan:
        return self._engine.plan_order(
            side=side,
            price=price,
            requested_quantity=requested_quantity,
        )

    def simulate_entry(
        self,
        *,
        signal_date: datetime,
        future_bars: list[tuple[datetime, dict[str, float | bool]]],
        slippage_ratio: float = 0.0,
        max_entry_sessions: int = 1,
        quantity: int = 0,
    ) -> EntrySimulation:
        """盘后信号 → 次日（或延迟窗口内）**真实可成交**入场模拟（S02）。

        主口径（``max_entry_sessions=1``）：只能在 T+1 交易日开盘成交，不可成交
        即 ``no_fill``，不得回退到 T 日收盘价（那是不可实现的成交——蓝图 §2.12）。

        敏感性口径（``max_entry_sessions>1``）：允许顺延到窗口内下一可成交开盘；
        调用方必须把它与主口径分开报告，不得混成一个主结果。

        不可成交判据（全部 fail-closed，不猜测）：
        - 该日停牌（``suspended``）；
        - **开盘**价达到/超过涨停价（``limit_up_open``）：开盘即在涨停队列里，
          不假设能成交；开盘=最高=最低=涨停时另标 ``one_price_limit_up``；
        - 无有效价格/涨跌停数据（``no_valid_price_data``，含 IPO 无涨跌幅基准期）。

        判据用**开盘价**而不是收盘价：引擎的 ``can_buy`` 按收盘价拒绝涨停买入
        （对 T 日收盘买入口径正确），但本模拟买的是 T+1 开盘——"开盘在涨停以下、
        盘中封板收盘涨停"这种 bar 是**可以**成交的（S02 测试用例明确区分
        "一字涨停不可成交"与"普通涨停但可成交"）。

        成交价 = raw 开盘价 + 滑点（``apply_slippage``，side="buy"），并按 ``quantity``
        估算买入成本；``entry_date`` 恒晚于 ``signal_date``（交易日维度）。
        """
        sessions = max(1, int(max_entry_sessions))
        bars = list(future_bars)
        deferred = 0
        last_reason = "no_future_bars"
        last_details: dict[str, object] = {}
        for position, (bar_date, bar) in enumerate(bars, start=1):
            if position > sessions:
                break
            decision = self.can_buy(bar)
            up_limit = _optional_numeric(
                dict(decision.details).get("up_limit"), default=None
            )
            open_price = _price(bar, key="open", fallback_key="close")
            high_price = _price(bar, key="high", fallback_key="close")
            low_price = _price(bar, key="low", fallback_key="close")
            if not decision.executable and str(decision.reason) in {
                "suspended",
                "no_valid_price_data",
            }:
                last_reason = str(decision.reason)
                last_details = dict(decision.details)
                deferred = position
                continue
            if open_price <= 0 or up_limit is None:
                last_reason = "no_valid_price_data"
                last_details = {"open": open_price, "up_limit": up_limit}
                deferred = position
                continue
            if open_price >= up_limit:
                last_reason = "limit_up_open"
                last_details = {
                    "open": open_price,
                    "up_limit": up_limit,
                    "one_price_limit_up": bool(
                        low_price >= up_limit and high_price >= up_limit
                    ),
                }
                deferred = position
                continue
            slipped = self._engine.apply_slippage(
                price=open_price, side="buy", slippage_ratio=max(0.0, float(slippage_ratio))
            )
            net_price = self._engine.apply_price_tick(slipped, side="buy")
            cost = (
                self.estimate_cost("buy", net_price, int(quantity), trade_date=bar_date)
                if quantity > 0
                else 0.0
            )
            return EntrySimulation(
                executed=True,
                signal_date=signal_date,
                entry_date=bar_date,
                entry_price_raw=open_price,
                reference_open_raw=open_price,
                slippage=net_price - open_price,
                cost=cost,
                net_entry_price=net_price,
                entry_delay_days=position,
                deferred_sessions=deferred,
                details={
                    "buy_reason": str(decision.reason),
                    "close_at_limit_up": bool(
                        up_limit is not None
                        and _price(bar, key="close", fallback_key="open") >= up_limit
                    ),
                },
            )
        if not bars:
            last_reason = "no_future_bars"
        elif deferred >= sessions and last_reason == "no_future_bars":
            # 窗口用尽仍不可成交：区分"窗口内一直没有可成交日"与"这就是最后一天"。
            last_reason = "beyond_delay_window"
        return EntrySimulation(
            executed=False,
            signal_date=signal_date,
            entry_price_raw=0.0,
            reference_open_raw=0.0,
            slippage=0.0,
            cost=0.0,
            no_fill_reason=last_reason,
            deferred_sessions=deferred,
            details=last_details,
        )

    def simulate_exit(
        self,
        entry_price: float,
        entry_date: datetime,
        future_bars: list[tuple[datetime, dict[str, float | bool]]],
        take_profit_pct: float,
        stop_loss_pct: float,
        horizon_days: int | None = None,
    ) -> ExitSimulation:
        if entry_price <= 0:
            raise ValueError("entry_price must be > 0")

        take_profit_level = entry_price * (1.0 + max(0.0, take_profit_pct))
        stop_loss_level = entry_price * (1.0 - max(0.0, stop_loss_pct))
        evaluation_horizon = len(future_bars) if horizon_days is None else max(0, int(horizon_days))
        max_exit_carry_days = self.max_exit_carry_days
        forced_discount = self._engine.forced_liquidation_discount

        pending_exit = False
        pending_exit_reason = ""
        deferred_days = 0
        last_bar_date: datetime | None = None
        last_bar_close = entry_price

        for offset, (current_date, bar) in enumerate(future_bars, start=1):
            open_price = _price(bar, key="open", fallback_key="close")
            high_price = _price(bar, key="high", fallback_key="close")
            low_price = _price(bar, key="low", fallback_key="close")
            close_price = _price(bar, key="close", fallback_key="open")

            last_bar_date = current_date
            last_bar_close = close_price

            decision = self.can_sell(
                bar=bar,
                last_buy_date=entry_date,
                current_date=current_date,
            )
            if pending_exit:
                if decision.executable:
                    return ExitSimulation(
                        executed=True,
                        exit_price=self._engine.apply_price_tick(open_price, side="sell"),
                        exit_date=current_date,
                        reason=f"{pending_exit_reason}_deferred_fill",
                        deferred_days=deferred_days,
                        exit_no_fill=True,
                    )
                deferred_days += 1
                if deferred_days > max_exit_carry_days:
                    forced_price = max(0.0, close_price * (1.0 - forced_discount))
                    return ExitSimulation(
                        executed=True,
                        exit_price=self._engine.apply_price_tick(forced_price, side="sell"),
                        exit_date=current_date,
                        reason="forced_liquidation_max_carry",
                        deferred_days=deferred_days,
                        exit_no_fill=True,
                        forced_exit=True,
                        forced_exit_close_date=current_date,
                        forced_exit_close_price=close_price,
                    )
                continue

            within_horizon = offset <= evaluation_horizon
            if not within_horizon:
                continue

            stop_triggered = open_price <= stop_loss_level or low_price <= stop_loss_level
            take_profit_triggered = (
                open_price >= take_profit_level or high_price >= take_profit_level
            )
            if stop_triggered:
                if decision.executable:
                    if open_price <= stop_loss_level:
                        return ExitSimulation(
                            executed=True,
                            exit_price=self._engine.apply_price_tick(open_price, side="sell"),
                            exit_date=current_date,
                            reason="stop_loss_gap_open",
                            deferred_days=deferred_days,
                        )
                    return ExitSimulation(
                        executed=True,
                        exit_price=self._engine.apply_price_tick(stop_loss_level, side="sell"),
                        exit_date=current_date,
                        reason="stop_loss_intraday",
                        deferred_days=deferred_days,
                    )
                pending_exit = True
                pending_exit_reason = "stop_loss"
                deferred_days = 1
                continue

            if take_profit_triggered:
                if decision.executable:
                    if open_price >= take_profit_level:
                        return ExitSimulation(
                            executed=True,
                            exit_price=self._engine.apply_price_tick(open_price, side="sell"),
                            exit_date=current_date,
                            reason="take_profit_gap_open",
                        )
                    return ExitSimulation(
                        executed=True,
                        exit_price=self._engine.apply_price_tick(take_profit_level, side="sell"),
                        exit_date=current_date,
                        reason="take_profit_intraday",
                    )
                pending_exit = True
                pending_exit_reason = "take_profit"
                deferred_days = 1
                continue

            if offset == evaluation_horizon:
                if decision.executable:
                    return ExitSimulation(
                        executed=True,
                        exit_price=self._engine.apply_price_tick(close_price, side="sell"),
                        exit_date=current_date,
                        reason="max_hold_exit",
                    )
                pending_exit = True
                pending_exit_reason = "max_hold"
                deferred_days = 1

        if pending_exit and last_bar_date is not None:
            forced_price = max(0.0, last_bar_close * (1.0 - forced_discount))
            return ExitSimulation(
                executed=True,
                exit_price=self._engine.apply_price_tick(forced_price, side="sell"),
                exit_date=last_bar_date,
                reason="forced_liquidation_data_end",
                deferred_days=deferred_days,
                exit_no_fill=True,
                forced_exit=True,
                forced_exit_close_date=last_bar_date,
                forced_exit_close_price=last_bar_close,
            )

        if last_bar_date is not None and evaluation_horizon > 0:
            return ExitSimulation(
                executed=True,
                exit_price=self._engine.apply_price_tick(last_bar_close, side="sell"),
                exit_date=last_bar_date,
                reason="max_hold_exit",
                deferred_days=deferred_days,
            )

        return ExitSimulation(
            executed=False,
            exit_price=entry_price,
            exit_date=entry_date,
            reason="no_future_bars",
            deferred_days=deferred_days,
        )

    def estimate_cost(
        self,
        side: str,
        price: float,
        quantity: int,
        trade_date: datetime | date | None = None,
    ) -> float:
        return self._engine.estimate_cost(
            side=side,
            price=price,
            quantity=quantity,
            trade_date=trade_date,
        )


def _price(
    bar: Mapping[str, object],
    key: str,
    fallback_key: str,
) -> float:
    return _optional_numeric(bar.get(key), default=_optional_numeric(bar.get(fallback_key), 0.0))


def _optional_numeric(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default
