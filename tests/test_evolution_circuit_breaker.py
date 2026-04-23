from __future__ import annotations

from datetime import date, timedelta

from stock_analyzer.evolution.scheduler.circuit_breaker import BreakerScope, CircuitBreaker


def test_global_breaker_opens_after_three_m9_failures() -> None:
    breaker = CircuitBreaker()
    day = date(2026, 3, 1)
    breaker.record_result("M9", success=False, day=day)
    breaker.record_result("M9", success=False, day=day)
    breaker.record_result("M9", success=False, day=day)
    status = breaker.status("M4", day=day)
    assert status.blocked is True
    assert status.scope == BreakerScope.GLOBAL


def test_module_breaker_opens_after_three_consecutive_days() -> None:
    breaker = CircuitBreaker()
    start = date(2026, 3, 1)
    breaker.record_result("M4", success=False, day=start)
    breaker.record_result("M4", success=False, day=start + timedelta(days=1))
    breaker.record_result("M4", success=False, day=start + timedelta(days=2))
    status = breaker.status("M4", day=start + timedelta(days=2))
    assert status.blocked is True
    assert status.scope == BreakerScope.MODULE


def test_blackout_day_blocks_all_modules() -> None:
    breaker = CircuitBreaker()
    blackout = date(2026, 3, 2)
    breaker.set_blackout_day(blackout, enabled=True)
    status = breaker.status("M2", day=blackout)
    assert status.blocked is True
    assert status.scope == BreakerScope.BLACKOUT_DAY
