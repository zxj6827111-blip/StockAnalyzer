"""Shared A-share execution rules, single source for backtest and live simulation.

Rule atoms extracted from the former backtest-only ``ExecutionMatcher``:
tradability checks (T+1, limit-up/down, suspended), dynamic slippage,
price tick, share rounding, min-notional, and cost estimation. The engine
holds no backtest-specific dependency (no bar sequence scanning here;
``simulate_exit`` stays in ``stock_analyzer.backtest.matcher``).
"""

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
class OrderPlan:
    executable: bool
    quantity: int
    requested_quantity: int
    residual_quantity: int
    trim_reason: str = ""
    residual_action: str = ""


class ExecutionEngine:
    """Validate tradability, shape orders and estimate transaction costs."""

    def __init__(
        self,
        config: BacktestMatcherConfig,
        limit_rule: LimitRuleConfig | None = None,
    ) -> None:
        _validate_matcher_config(config)
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

    def static_slippage_ratio(self, strategy: str) -> float:
        """Static per-strategy base slippage (online fallback without atr14/volume_ratio)."""
        return max(0.0, float(self._config.slippage_by_strategy.get(strategy, 0.0)))

    def should_downgrade_by_slippage(self, slippage_ratio: float) -> bool:
        return float(slippage_ratio) > float(self._config.max_dynamic_slippage_ratio)

    @property
    def max_exit_carry_days(self) -> int:
        return max(0, int(self._config.max_exit_carry_days))

    @property
    def forced_liquidation_discount(self) -> float:
        return max(0.0, float(self._config.forced_liquidation_discount_bp)) / 10000.0

    def apply_slippage(self, price: float, side: str, slippage_ratio: float) -> float:
        ratio = max(0.0, slippage_ratio)
        if side.lower() == "buy":
            adjusted = price * (1.0 + ratio)
        else:
            adjusted = max(0.0, price * (1.0 - ratio))
        return self.apply_price_tick(adjusted, side=side)

    def apply_price_tick(self, price: float, *, side: str) -> float:
        value = max(0.0, float(price))
        rule = str(self._config.price_tick_rule).strip().lower()
        if rule != "exchange_tick":
            return value
        tick = 0.01
        if side.lower() == "buy":
            return round(math.ceil(value / tick) * tick, 2)
        return round(math.floor(value / tick) * tick, 2)

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

        normalized_price = self.apply_price_tick(price, side=side)
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

    def share_rounding_unit(self) -> int:
        """Lot unit derived from the shared rounding rule (online order sizing)."""
        rule = str(self._config.share_rounding_rule).strip().lower()
        if rule == "lot_down_100":
            return 100
        return 1

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

    def _apply_share_rounding(self, quantity: int) -> int:
        rule = str(self._config.share_rounding_rule).strip().lower()
        qty = max(0, int(quantity))
        if rule == "lot_down_100":
            return (qty // 100) * 100
        return qty

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


def _validate_matcher_config(config: BacktestMatcherConfig) -> None:
    """Reject invalid matcher parameters early (same semantics as acceptance cost_model check)."""
    if config.commission_rate <= 0:
        raise ValueError("commission_rate must be > 0")
    if config.stamp_tax_rate < 0:
        raise ValueError("stamp_tax_rate must be >= 0")
    if config.transfer_fee_rate < 0:
        raise ValueError("transfer_fee_rate must be >= 0")
    if config.min_commission_per_order < 5.0:
        raise ValueError("min_commission_per_order must be >= 5.0")
    if str(config.stamp_tax_apply_on).strip().lower() != "sell_only":
        raise ValueError("stamp_tax_apply_on must be 'sell_only'")


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
