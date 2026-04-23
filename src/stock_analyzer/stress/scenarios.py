"""Stress test scenarios for portfolio risk resilience."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from stock_analyzer.config import StockAnalyzerConfig
from stock_analyzer.risk.controls import CapitalCurveGuard, CircuitBreaker


@dataclass(slots=True)
class StressScenario:
    name: str
    daily_returns: list[float]
    freeze_within_days: int | None = 3
    max_drawdown_limit_pct: float = 25.0


@dataclass(slots=True)
class StressScenarioResult:
    name: str
    days: int
    final_equity: float
    max_drawdown_pct: float
    freeze_day: int | None
    reduce_day: int | None
    pass_drawdown: bool
    pass_freeze_timing: bool
    passed: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def run_default_stress_suite(config: StockAnalyzerConfig) -> dict[str, object]:
    scenarios = _default_scenarios()
    results: list[StressScenarioResult] = []
    for scenario in scenarios:
        results.append(_run_scenario(config=config, scenario=scenario))

    passed = [item for item in results if item.passed]
    failed = [item.name for item in results if not item.passed]
    return {
        "timestamp": datetime.now().isoformat(),
        "summary": {
            "scenario_count": len(results),
            "passed_count": len(passed),
            "failed_count": len(results) - len(passed),
            "failed_scenarios": failed,
        },
        "scenarios": [item.to_dict() for item in results],
    }


def _run_scenario(config: StockAnalyzerConfig, scenario: StressScenario) -> StressScenarioResult:
    capital_guard = CapitalCurveGuard(config=config.capital_curve)
    breaker = CircuitBreaker(config=config.circuit_breaker)

    equity = 1.0
    peak_equity = 1.0
    max_drawdown_pct = 0.0
    freeze_day: int | None = None
    reduce_day: int | None = None
    trailing_returns: list[float] = []

    for idx, daily_return in enumerate(scenario.daily_returns, start=1):
        equity *= max(0.01, 1.0 + daily_return)
        peak_equity = max(peak_equity, equity)
        drawdown_pct = max(0.0, (peak_equity - equity) / peak_equity * 100.0)
        max_drawdown_pct = max(max_drawdown_pct, drawdown_pct)

        breaker.register_trade(pnl_pct=daily_return * 100.0)
        trailing_returns.append(daily_return)
        if len(trailing_returns) > 5:
            trailing_returns = trailing_returns[-5:]
        weekly_drawdown = max(0.0, -sum(trailing_returns) * 100.0)
        breaker.update_portfolio_drawdown(
            daily_drawdown=max(0.0, -daily_return * 100.0),
            weekly_drawdown=weekly_drawdown,
        )

        capital_action, _ = capital_guard.evaluate(current_equity=equity)
        if reduce_day is None and (
            capital_action in {"reduce", "alert"} or breaker.should_reduce()
        ):
            reduce_day = idx
        if freeze_day is None and (capital_action == "freeze" or breaker.should_pause()):
            freeze_day = idx

    pass_drawdown = max_drawdown_pct < scenario.max_drawdown_limit_pct
    pass_freeze_timing = _freeze_timing_pass(
        freeze_day=freeze_day,
        freeze_within_days=scenario.freeze_within_days,
    )
    return StressScenarioResult(
        name=scenario.name,
        days=len(scenario.daily_returns),
        final_equity=round(equity, 6),
        max_drawdown_pct=round(max_drawdown_pct, 4),
        freeze_day=freeze_day,
        reduce_day=reduce_day,
        pass_drawdown=pass_drawdown,
        pass_freeze_timing=pass_freeze_timing,
        passed=pass_drawdown and pass_freeze_timing,
    )


def _freeze_timing_pass(freeze_day: int | None, freeze_within_days: int | None) -> bool:
    if freeze_within_days is None:
        return freeze_day is None
    if freeze_day is None:
        return False
    return freeze_day <= freeze_within_days


def _default_scenarios() -> list[StressScenario]:
    return [
        StressScenario(
            name="2015_crash",
            daily_returns=[-0.06, -0.04, -0.03, 0.01, -0.02, 0.015, -0.01],
        ),
        StressScenario(
            name="2016_circuit_break",
            daily_returns=[-0.055, -0.045, 0.02, -0.03, 0.01, -0.015],
        ),
        StressScenario(
            name="2018_bear",
            daily_returns=[-0.04, -0.03, -0.02, -0.015, -0.01, 0.01, -0.01, -0.005],
        ),
        StressScenario(
            name="2020_pandemic",
            daily_returns=[-0.05, -0.035, -0.025, 0.015, -0.02, 0.01],
        ),
        StressScenario(
            name="2023_grind_down",
            daily_returns=[
                -0.035,
                -0.02,
                -0.015,
                -0.01,
                -0.01,
                -0.008,
                -0.007,
                -0.006,
                -0.005,
                0.005,
            ],
        ),
        StressScenario(
            name="2024_quant_shock",
            daily_returns=[-0.045, -0.03, -0.022, -0.015, 0.012, -0.01],
        ),
    ]
