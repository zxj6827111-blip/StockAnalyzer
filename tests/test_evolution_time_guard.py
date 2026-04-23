from __future__ import annotations

from datetime import datetime

from stock_analyzer.evolution.scheduler.time_guard import TimeGuard, TimeGuardMode


def test_time_guard_hard_stop_window() -> None:
    guard = TimeGuard()
    decision = guard.evaluate(datetime(2026, 3, 2, 9, 0))
    assert decision.mode == TimeGuardMode.HARD_STOP


def test_time_guard_soft_yield_window() -> None:
    guard = TimeGuard()
    decision = guard.evaluate(datetime(2026, 3, 2, 10, 0))
    assert decision.mode == TimeGuardMode.SOFT_YIELD


def test_time_guard_transition_window() -> None:
    guard = TimeGuard()
    decision = guard.evaluate(datetime(2026, 3, 2, 12, 0))
    assert decision.mode == TimeGuardMode.TRANSITION


def test_time_guard_full_power_offhours_and_weekend() -> None:
    guard = TimeGuard()
    offhours = guard.evaluate(datetime(2026, 3, 2, 16, 0))
    weekend = guard.evaluate(datetime(2026, 3, 1, 10, 0))
    assert offhours.mode == TimeGuardMode.FULL_POWER
    assert weekend.mode == TimeGuardMode.FULL_POWER
