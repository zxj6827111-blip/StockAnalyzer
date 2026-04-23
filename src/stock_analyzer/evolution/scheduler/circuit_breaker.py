"""Three-level circuit breaker for evolution scheduler."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum


class BreakerScope(StrEnum):
    """Circuit breaker scope."""

    NONE = "none"
    GLOBAL = "global"
    MODULE = "module"
    BLACKOUT_DAY = "blackout_day"


@dataclass(frozen=True, slots=True)
class BreakerStatus:
    """Circuit breaker status for one module."""

    blocked: bool
    scope: BreakerScope
    reason: str


class CircuitBreaker:
    """Track global/module/blackout breaker states."""

    def __init__(
        self,
        global_m9_failure_threshold: int = 3,
        module_failure_day_threshold: int = 3,
    ) -> None:
        self._global_m9_failure_threshold = global_m9_failure_threshold
        self._module_failure_day_threshold = module_failure_day_threshold
        self._m9_failure_count = 0
        self._module_failure_streak: dict[str, int] = {}
        self._module_last_failure_day: dict[str, date] = {}
        self._blackout_days: set[date] = set()

    def set_blackout_day(self, day: date, enabled: bool = True) -> None:
        """Set or clear a market-wide blackout day."""
        if enabled:
            self._blackout_days.add(day)
            return
        self._blackout_days.discard(day)

    def record_result(self, module: str, success: bool, day: date | None = None) -> None:
        """Record one module run result."""
        effective_day = day or date.today()
        normalized = module.strip().upper()
        if not normalized:
            raise ValueError("module must not be empty")

        if normalized == "M9":
            if success:
                self._m9_failure_count = 0
            else:
                self._m9_failure_count += 1

        if success:
            self._module_failure_streak[normalized] = 0
            return

        previous_day = self._module_last_failure_day.get(normalized)
        if previous_day is not None and effective_day - previous_day == timedelta(days=1):
            self._module_failure_streak[normalized] = (
                self._module_failure_streak.get(normalized, 0) + 1
            )
        else:
            self._module_failure_streak[normalized] = 1
        self._module_last_failure_day[normalized] = effective_day

    def status(self, module: str, day: date | None = None) -> BreakerStatus:
        """Return current breaker status for a module."""
        effective_day = day or date.today()
        normalized = module.strip().upper()
        if not normalized:
            raise ValueError("module must not be empty")

        if effective_day in self._blackout_days:
            return BreakerStatus(
                blocked=True,
                scope=BreakerScope.BLACKOUT_DAY,
                reason="blackout_day",
            )

        if self._m9_failure_count >= self._global_m9_failure_threshold:
            return BreakerStatus(
                blocked=True,
                scope=BreakerScope.GLOBAL,
                reason="global_m9_failures",
            )

        streak = self._module_failure_streak.get(normalized, 0)
        if streak >= self._module_failure_day_threshold:
            return BreakerStatus(
                blocked=True,
                scope=BreakerScope.MODULE,
                reason="module_failure_streak",
            )

        return BreakerStatus(blocked=False, scope=BreakerScope.NONE, reason="ok")

    def can_run(self, module: str, day: date | None = None) -> bool:
        """Return whether one module is allowed to run."""
        return not self.status(module=module, day=day).blocked
