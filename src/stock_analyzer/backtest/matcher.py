"""Execution matcher with T+1 and A-share tradability constraints.

Thin shell over the shared ``stock_analyzer.execution.ExecutionEngine``.
All rule atoms (tradability, slippage, tick, rounding, costs) live in the
shared engine; this module keeps the backtest-only exit sequence simulation
(``simulate_exit``) which scans future bars.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime

from stock_analyzer.config import BacktestMatcherConfig, LimitRuleConfig
from stock_analyzer.execution.engine import ExecutionEngine, MatchDecision, OrderPlan

__all__ = [
    "ExecutionMatcher",
    "ExitSimulation",
    "MatchDecision",
    "OrderPlan",
]


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
