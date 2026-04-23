from __future__ import annotations

from stock_analyzer.config import DataHealthConfig
from stock_analyzer.monitor.health import DataHealthMonitor


def test_health_monitor_marks_degraded_on_low_success_rate() -> None:
    monitor = DataHealthMonitor(
        DataHealthConfig(window_size=4, min_success_rate=0.95, max_latency_sec=120)
    )
    monitor.record(success=True, latency_sec=1.0)
    monitor.record(success=False, latency_sec=1.0)
    snapshot = monitor.snapshot()
    assert snapshot["degraded_mode"] is True
    assert snapshot["degrade_reason"] == "low_success_rate"
