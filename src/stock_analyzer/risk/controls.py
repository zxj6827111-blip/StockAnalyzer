"""Risk control modules for capital curve and circuit break rules."""

from __future__ import annotations

from dataclasses import dataclass, fields

from stock_analyzer.config import CapitalCurveConfig, CircuitBreakerConfig, StockAnalyzerConfig
from stock_analyzer.types import RiskStatus


def _validate_risk_status_contract() -> None:
    field_names = {item.name for item in fields(RiskStatus)}
    required = {
        "action",
        "drawdown_pct",
        "degraded_mode",
        "can_open_new_position",
        "reason",
        "hard_degraded_mode",
        "soft_degraded_mode",
    }
    missing = sorted(required - field_names)
    if missing:
        missing_text = ", ".join(missing)
        raise RuntimeError(
            "RiskStatus contract mismatch: missing fields "
            f"[{missing_text}]. This usually means the deployed source tree is inconsistent. "
            "Please redeploy the full project and rebuild both api and scheduler containers."
        )


_validate_risk_status_contract()


@dataclass(slots=True)
class CapitalCurveGuard:
    """Track peak equity and output drawdown-based action."""

    config: CapitalCurveConfig
    initial_equity: float = 1.0
    peak_equity: float = 1.0

    def evaluate(self, current_equity: float) -> tuple[str, float]:
        if current_equity <= 0:
            return "freeze", 100.0

        self.peak_equity = max(self.peak_equity, current_equity)
        drawdown_pct = max(0.0, (self.peak_equity - current_equity) / self.peak_equity * 100.0)

        if current_equity / self.initial_equity <= self.config.protect_line:
            return "freeze", drawdown_pct
        if drawdown_pct >= self.config.drawdown_freeze:
            return "freeze", drawdown_pct
        if drawdown_pct >= self.config.drawdown_reduce:
            return "reduce", drawdown_pct
        if drawdown_pct >= self.config.drawdown_alert:
            return "alert", drawdown_pct
        return "normal", drawdown_pct


@dataclass(slots=True)
class CircuitBreaker:
    """Track loss streak and portfolio drawdowns."""

    config: CircuitBreakerConfig
    consecutive_losses: int = 0
    daily_drawdown: float = 0.0
    weekly_drawdown: float = 0.0

    def register_trade(self, pnl_pct: float) -> None:
        if pnl_pct < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    def update_portfolio_drawdown(self, daily_drawdown: float, weekly_drawdown: float) -> None:
        self.daily_drawdown = max(0.0, daily_drawdown)
        self.weekly_drawdown = max(0.0, weekly_drawdown)

    def should_pause(self) -> bool:
        return (
            self.consecutive_losses >= self.config.consecutive_fail_pause
            or self.daily_drawdown >= self.config.portfolio_daily_drawdown_stop
        )

    def should_reduce(self) -> bool:
        return (
            self.consecutive_losses >= self.config.consecutive_fail_reduce
            or self.weekly_drawdown >= self.config.portfolio_weekly_drawdown_reduce
        )


class RiskController:
    """Aggregate risk states into operational gating outputs."""

    def __init__(self, config: StockAnalyzerConfig) -> None:
        self._config = config
        self._capital_guard = CapitalCurveGuard(config.capital_curve)
        self._circuit_breaker = CircuitBreaker(config.circuit_breaker)
        self._hard_degraded_mode = False
        self._soft_degraded_mode = False

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._circuit_breaker

    def update_degraded_mode(
        self,
        degraded_mode: bool | None = None,
        *,
        hard_degraded_mode: bool | None = None,
        soft_degraded_mode: bool | None = None,
    ) -> None:
        if hard_degraded_mode is None and soft_degraded_mode is None:
            self._hard_degraded_mode = bool(degraded_mode)
            self._soft_degraded_mode = False
            return

        self._hard_degraded_mode = bool(hard_degraded_mode)
        self._soft_degraded_mode = bool(soft_degraded_mode)

    def evaluate(self, current_equity: float) -> RiskStatus:
        capital_action, drawdown_pct = self._capital_guard.evaluate(current_equity)
        breaker_pause = self._circuit_breaker.should_pause()
        breaker_reduce = self._circuit_breaker.should_reduce()

        action = capital_action
        reason = f"capital_curve:{capital_action}"
        if breaker_pause:
            action = "freeze"
            reason = "circuit_breaker_pause"
        elif breaker_reduce and action == "normal":
            action = "reduce"
            reason = "circuit_breaker_reduce"

        hard_degraded = self._hard_degraded_mode
        soft_degraded = self._soft_degraded_mode
        degraded_mode = hard_degraded or soft_degraded
        can_open = action not in {"freeze", "pause"}
        if self._config.data_source.degrade_stops_new_buy and hard_degraded:
            can_open = False
            reason = "degraded_stop_new_buy"
            if action == "normal":
                action = "degraded"
        elif hard_degraded and action == "normal":
            action = "degraded"
            reason = "hard_degraded_monitoring"
        elif soft_degraded and action == "normal":
            action = "degraded"
            reason = "soft_degraded_monitoring"

        return RiskStatus(
            action=action,
            drawdown_pct=drawdown_pct,
            degraded_mode=degraded_mode,
            can_open_new_position=can_open,
            reason=reason,
            hard_degraded_mode=hard_degraded,
            soft_degraded_mode=soft_degraded,
        )
