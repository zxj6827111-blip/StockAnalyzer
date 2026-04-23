"""Data health monitor for degradation decisions."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from statistics import mean

from stock_analyzer.config import DataHealthConfig


@dataclass(slots=True)
class _FetchEvent:
    success: bool
    latency_sec: float


class DataHealthMonitor:
    """Track provider success/latency and produce degrade status."""

    def __init__(self, config: DataHealthConfig) -> None:
        self._config = config
        self._events: deque[_FetchEvent] = deque(maxlen=max(1, config.window_size))

    def record(self, success: bool, latency_sec: float) -> None:
        self._events.append(_FetchEvent(success=success, latency_sec=max(0.0, latency_sec)))

    def snapshot(self) -> dict[str, object]:
        if not self._events:
            return {
                "fetch_events": 0,
                "success_rate": 1.0,
                "avg_latency_sec": 0.0,
                "degraded_mode": False,
                "degrade_reason": "",
            }

        success_values = [1.0 if event.success else 0.0 for event in self._events]
        latency_values = [event.latency_sec for event in self._events]
        success_rate = mean(success_values)
        avg_latency = mean(latency_values)

        degraded = False
        reason = ""
        if success_rate < self._config.min_success_rate:
            degraded = True
            reason = "low_success_rate"
        elif avg_latency > self._config.max_latency_sec:
            degraded = True
            reason = "high_latency"

        return {
            "fetch_events": len(self._events),
            "success_rate": round(success_rate, 4),
            "avg_latency_sec": round(avg_latency, 4),
            "degraded_mode": degraded,
            "degrade_reason": reason,
        }
