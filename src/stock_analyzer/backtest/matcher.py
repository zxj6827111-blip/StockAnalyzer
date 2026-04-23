"""Execution matcher with T+1 and A-share tradability constraints."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime

from stock_analyzer.config import BacktestMatcherConfig, LimitRuleConfig
from stock_analyzer.data.limit_rule import build_price_limits, resolve_stamp_tax_rate


@dataclass(slots=True)
class MatchDecision:
    executable: bool
    reason: str


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


@dataclass(slots=True)
class OrderPlan:
    executable: bool
    quantity: int
    requested_quantity: int
    residual_quantity: int
    trim_reason: str = ""
    residual_action: str = ""


class ExecutionMatcher:
    """Validate tradability and estimate transaction costs."""

    def __init__(
        self,
        config: BacktestMatcherConfig,
        limit_rule: LimitRuleConfig | None = None,
    ) -> None:
        self._config = config
        self._limit_rule = limit_rule or LimitRuleConfig()

    def can_buy(self, bar: Mapping[str, object]) -> MatchDecision:
        if self._is_suspended(bar):
            return MatchDecision(executable=False, reason="suspended")
        if self._config.reject_limit_up_buy and self._is_limit_up(bar):
            return MatchDecision(executable=False, reason="limit_up_reject")
        return MatchDecision(executable=True, reason="ok")

    def can_sell(
        self,
        bar: Mapping[str, object],
        last_buy_date: datetime | None,
        current_date: datetime,
    ) -> MatchDecision:
        if self._is_suspended(bar):
            return MatchDecision(executable=False, reason="suspended")
        if (
            self._config.enforce_t_plus_1
            and last_buy_date is not None
            and current_date.date() <= last_buy_date.date()
        ):
            return MatchDecision(executable=False, reason="t_plus_1_block")
        if self._config.reject_limit_down_sell and self._is_limit_down(bar):
            return MatchDecision(executable=False, reason="limit_down_reject")
        return MatchDecision(executable=True, reason="ok")

    def dynamic_slippage_ratio(
        self,
        strategy: str,
        atr14: float,
        close: float,
        volume_ratio: float,
    ) -> float:
        base = float(self._config.slippage_by_strategy.get(strategy, 0.0))
        base = max(0.0, base)
        if not self._config.dynamic_slippage_enabled:
            return base
        if close <= 0:
            return base
        dynamic = (atr14 / close) * 0.35 + (volume_ratio - 1.0) * 0.001
        dynamic = max(0.0, dynamic)
        return max(base, dynamic)

    def should_downgrade_by_slippage(self, slippage_ratio: float) -> bool:
        return float(slippage_ratio) > float(self._config.max_dynamic_slippage_ratio)

    @property
    def max_exit_carry_days(self) -> int:
        return max(0, int(self._config.max_exit_carry_days))

    def apply_slippage(self, price: float, side: str, slippage_ratio: float) -> float:
        ratio = max(0.0, slippage_ratio)
        if side.lower() == "buy":
            adjusted = price * (1.0 + ratio)
        else:
            adjusted = max(0.0, price * (1.0 - ratio))
        return self._apply_price_tick(adjusted, side=side)

    def plan_order(self, *, side: str, price: float, requested_quantity: int) -> OrderPlan:
        quantity = max(0, int(requested_quantity))
        rounded_quantity = self._apply_share_rounding(quantity)
        residual_quantity = max(0, quantity - rounded_quantity)
        residual_action = ""
        if residual_quantity > 0 and self._config.residual_order_policy == "day_cancel_then_recalc":
            residual_action = "day_cancel_then_recalc"

        if rounded_quantity <= 0:
            return OrderPlan(
                executable=False,
                quantity=0,
                requested_quantity=quantity,
                residual_quantity=residual_quantity,
                trim_reason="rounding_zero",
                residual_action=residual_action,
            )

        normalized_price = self._apply_price_tick(price, side=side)
        notional = normalized_price * float(rounded_quantity)
        if notional < float(self._config.min_notional_per_order):
            return OrderPlan(
                executable=False,
                quantity=0,
                requested_quantity=quantity,
                residual_quantity=residual_quantity,
                trim_reason="min_notional",
                residual_action=residual_action,
            )

        return OrderPlan(
            executable=True,
            quantity=rounded_quantity,
            requested_quantity=quantity,
            residual_quantity=residual_quantity,
            residual_action=residual_action,
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
        forced_discount = max(0.0, float(self._config.forced_liquidation_discount_bp)) / 10000.0

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
                        exit_price=self._apply_price_tick(open_price, side="sell"),
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
                        exit_price=self._apply_price_tick(forced_price, side="sell"),
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
                            exit_price=self._apply_price_tick(open_price, side="sell"),
                            exit_date=current_date,
                            reason="stop_loss_gap_open",
                            deferred_days=deferred_days,
                        )
                    return ExitSimulation(
                        executed=True,
                        exit_price=self._apply_price_tick(stop_loss_level, side="sell"),
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
                            exit_price=self._apply_price_tick(open_price, side="sell"),
                            exit_date=current_date,
                            reason="take_profit_gap_open",
                        )
                    return ExitSimulation(
                        executed=True,
                        exit_price=self._apply_price_tick(take_profit_level, side="sell"),
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
                        exit_price=self._apply_price_tick(close_price, side="sell"),
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
                exit_price=self._apply_price_tick(forced_price, side="sell"),
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
                exit_price=self._apply_price_tick(last_bar_close, side="sell"),
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

    def _apply_share_rounding(self, quantity: int) -> int:
        rule = str(self._config.share_rounding_rule).strip().lower()
        qty = max(0, int(quantity))
        if rule == "lot_down_100":
            return (qty // 100) * 100
        return qty

    def _apply_price_tick(self, price: float, *, side: str) -> float:
        value = max(0.0, float(price))
        rule = str(self._config.price_tick_rule).strip().lower()
        if rule != "exchange_tick":
            return value
        tick = 0.01
        if side.lower() == "buy":
            return round(math.ceil(value / tick) * tick, 2)
        return round(math.floor(value / tick) * tick, 2)

    def estimate_cost(
        self,
        side: str,
        price: float,
        quantity: int,
        trade_date: datetime | date | None = None,
    ) -> float:
        amount = price * float(quantity)
        commission = max(
            self._config.min_commission_per_order,
            amount * self._config.commission_rate,
        )
        transfer_fee = amount * self._config.transfer_fee_rate
        stamp_tax = 0.0
        if side.lower() == "sell" and self._config.stamp_tax_apply_on == "sell_only":
            stamp_rate = resolve_stamp_tax_rate(
                config=self._limit_rule,
                trade_date=trade_date,
                default_rate=self._config.stamp_tax_rate,
            )
            stamp_tax = amount * stamp_rate
        return float(commission + transfer_fee + stamp_tax)

    def _is_suspended(self, bar: Mapping[str, object]) -> bool:
        return bool(bar.get("suspended", False))

    def _is_limit_up(self, bar: Mapping[str, object]) -> bool:
        close = _optional_numeric(bar.get("close"), default=0.0)
        limits = build_price_limits(
            bar=dict(bar),
            config=self._limit_rule,
        )
        up_limit = limits.up_limit
        if up_limit is None:
            up_limit = _optional_numeric(bar.get("up_limit"), default=close + 1.0)
        return bool(close >= up_limit)

    def _is_limit_down(self, bar: Mapping[str, object]) -> bool:
        close = _optional_numeric(bar.get("close"), default=0.0)
        limits = build_price_limits(
            bar=dict(bar),
            config=self._limit_rule,
        )
        down_limit = limits.down_limit
        if down_limit is None:
            down_limit = _optional_numeric(bar.get("down_limit"), default=close - 1.0)
        return bool(close <= down_limit)


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
