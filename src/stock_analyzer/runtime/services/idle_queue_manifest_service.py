"""Idle queue window-context and manifest definition workflows."""

# mypy: disable-error-code=redundant-cast

from __future__ import annotations

from datetime import date, datetime, timedelta
from datetime import time as dt_time
from functools import lru_cache
from importlib import import_module
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService


class RuntimeIdleQueueManifestService:
    """Build idle queue window context and task manifests."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service

    def build_idle_context(self, now: datetime) -> dict[str, object]:
        service = self._service
        idle_cfg = service._config.idle_queue
        workday_start = _parse_hhmm_time(idle_cfg.workday_start_time)
        weekend_start = _parse_hhmm_time(idle_cfg.weekend_start_time)
        base_hard_stop = _parse_hhmm_time(idle_cfg.base_hard_stop)
        premarket = _parse_hhmm_time(service._config.scheduler.premarket_time)
        effective_hard_stop = _min_clock(base_hard_stop, premarket)
        effective_soft_stop = _clock_shift_minutes(
            effective_hard_stop, -max(0, idle_cfg.soft_stop_lead_minutes)
        )
        effective_report_deadline = _clock_shift_minutes(
            effective_hard_stop, -max(0, idle_cfg.report_deadline_lead_minutes)
        )
        report_budget_start = _clock_shift_minutes(
            effective_report_deadline, -max(0, idle_cfg.report_min_budget_minutes)
        )
        default_trigger = _parse_hhmm_time(idle_cfg.report_default_trigger_time)
        trigger_time = _min_clock(default_trigger, report_budget_start)

        window = "off"
        trade_date_raw = ""
        window_key = ""
        weekday = now.weekday()
        clock = now.time()
        clock_sec = _clock_to_seconds(clock)
        workday_start_sec = _clock_to_seconds(workday_start)
        weekend_start_sec = _clock_to_seconds(weekend_start)
        hard_plus_sec = min(
            _clock_to_seconds(effective_hard_stop) + max(0, idle_cfg.hard_kill_grace_seconds),
            24 * 3600 - 1,
        )

        if weekday in {0, 1, 2, 3} and clock_sec >= workday_start_sec:
            window = "workday"
            trade_date_raw = now.strftime("%Y%m%d")
            window_key = f"WD-{trade_date_raw}"
        elif weekday in {1, 2, 3, 4} and clock_sec <= hard_plus_sec:
            window = "workday"
            prev_day = now.date() - timedelta(days=1)
            trade_date_raw = prev_day.strftime("%Y%m%d")
            window_key = f"WD-{trade_date_raw}"
        elif weekday == 5 and clock_sec >= weekend_start_sec:
            window = "weekend"
            trade_date_raw = _last_friday(now.date()).strftime("%Y%m%d")
            window_key = f"WE-{trade_date_raw}"
        elif weekday == 6:
            window = "weekend"
            trade_date_raw = _last_friday(now.date()).strftime("%Y%m%d")
            window_key = f"WE-{trade_date_raw}"

        if window == "off":
            service._idle_trade_date_frozen = ""
            service._idle_window_key = ""
            trade_date = ""
        else:
            if service._idle_window_key != window_key or not service._idle_trade_date_frozen:
                service._idle_window_key = window_key
                service._idle_trade_date_frozen = trade_date_raw
            trade_date = service._idle_trade_date_frozen

        return {
            "window": window,
            "window_key": window_key,
            "trade_date": trade_date,
            "effective_hard_stop": effective_hard_stop.strftime("%H:%M:%S"),
            "effective_soft_stop": effective_soft_stop.strftime("%H:%M:%S"),
            "effective_report_deadline": effective_report_deadline.strftime("%H:%M:%S"),
            "trigger_time": trigger_time.strftime("%H:%M:%S"),
            "now": now.isoformat(),
        }

    def build_idle_task_manifests(self) -> dict[str, dict[str, object]]:
        return {
            "WD-P0-01": {
                "task_id": "WD-P0-01",
                "priority": "P0",
                "schedule": "workday",
                "phase": 1,
                "max_wall_time_minutes": 30,
                "task_output_subdir": "data_quality",
                "write_whitelist": [],
            },
            "WD-P0-02": {
                "task_id": "WD-P0-02",
                "priority": "P0",
                "schedule": "workday",
                "phase": 2,
                "max_wall_time_minutes": 45,
                "task_output_subdir": "failure_analysis",
                "write_whitelist": [],
            },
            "WD-P0-03": {
                "task_id": "WD-P0-03",
                "priority": "P0",
                "schedule": "workday",
                "phase": 2,
                "max_wall_time_minutes": 20,
                "task_output_subdir": "psi_monitor",
                "write_whitelist": [],
            },
            "WD-P0-04": {
                "task_id": "WD-P0-04",
                "priority": "P0",
                "schedule": "workday",
                "phase": 2,
                "max_wall_time_minutes": 25,
                "task_output_subdir": "exposure_scan",
                "write_whitelist": [],
            },
            "WD-P1-05": {
                "task_id": "WD-P1-05",
                "priority": "P1",
                "schedule": "workday",
                "phase": 2,
                "depends_on": ["WD-P0-01"],
                "gate_conditions": {"WD-P0-01.status": ["ok", "degraded", "fallback"]},
                "max_wall_time_minutes": 90,
                "task_output_subdir": "precompute",
                "write_whitelist": [],
            },
            "WD-P1-06": {
                "task_id": "WD-P1-06",
                "priority": "P1",
                "schedule": "workday",
                "phase": 3,
                "depends_on": ["WD-P0-01"],
                "gate_conditions": {"WD-P0-01.status": ["ok", "degraded", "fallback"]},
                "max_wall_time_minutes": 60,
                "task_output_subdir": "monte_carlo",
                "write_whitelist": [],
            },
            "WD-P1-07": {
                "task_id": "WD-P1-07",
                "priority": "P1",
                "schedule": "workday",
                "phase": 2,
                "depends_on": ["WD-P0-01"],
                "gate_conditions": {"WD-P0-01.status": ["ok", "degraded", "fallback"]},
                "max_wall_time_minutes": 30,
                "task_output_subdir": "sector_radar",
                "write_whitelist": [],
            },
            "WD-REPORT": {
                "task_id": "WD-REPORT",
                "priority": "P0",
                "schedule": "workday",
                "phase": 1,
                "max_wall_time_minutes": 5,
                "task_output_subdir": "morning_brief",
                "write_whitelist": [],
            },
            "WE-P0-01": {
                "task_id": "WE-P0-01",
                "priority": "P0",
                "schedule": "weekend",
                "phase": 3,
                "must_run": True,
                "defer_policy": "none",
                "rotating_priority": 0,
                "max_defer_runs": 0,
                "force_run_on_disk_usage_pct": 100.0,
                "max_wall_time_minutes": 360,
                "task_output_subdir": "soak_test",
                "write_whitelist": [],
            },
            "WE-P0-02": {
                "task_id": "WE-P0-02",
                "priority": "P0",
                "schedule": "weekend",
                "phase": 2,
                "must_run": True,
                "defer_policy": "none",
                "rotating_priority": 0,
                "max_defer_runs": 0,
                "force_run_on_disk_usage_pct": 100.0,
                "max_wall_time_minutes": 180,
                "task_output_subdir": "reproducibility",
                "write_whitelist": [],
            },
            "WE-P1-03": {
                "task_id": "WE-P1-03",
                "priority": "P1",
                "schedule": "weekend",
                "phase": 1,
                "must_run": False,
                "defer_policy": "next_weekend",
                "rotating_priority": 0,
                "max_defer_runs": 2,
                "force_run_on_disk_usage_pct": 100.0,
                "max_wall_time_minutes": 360,
                "symbol_cap": 120,
                "task_output_subdir": "rolling_backtest",
                "write_whitelist": [],
            },
            "WE-LEARN-01": {
                "task_id": "WE-LEARN-01",
                "priority": "P1",
                "schedule": "weekend",
                "phase": 2,
                "must_run": False,
                "defer_policy": "next_weekend",
                "rotating_priority": 0,
                "max_defer_runs": 2,
                "force_run_on_disk_usage_pct": 100.0,
                "max_wall_time_minutes": 240,
                "min_remaining_minutes": 270,
                "symbol_cap": 80,
                "task_output_subdir": "model_learning",
                "write_whitelist": [],
                "min_interval_days": 7,
            },
            "WE-P1-04": {
                "task_id": "WE-P1-04",
                "priority": "P1",
                "schedule": "weekend",
                "phase": 3,
                "must_run": False,
                "defer_policy": "next_weekend",
                "rotating_priority": 0,
                "max_defer_runs": 2,
                "force_run_on_disk_usage_pct": 100.0,
                "max_wall_time_minutes": 240,
                "symbol_cap": 80,
                "task_output_subdir": "counterfactual",
                "write_whitelist": [],
            },
            "WE-P1-05": {
                "task_id": "WE-P1-05",
                "priority": "P1",
                "schedule": "weekend",
                "phase": 3,
                "must_run": False,
                "defer_policy": "next_weekend",
                "rotating_priority": 0,
                "max_defer_runs": 2,
                "force_run_on_disk_usage_pct": 100.0,
                "max_wall_time_minutes": 240,
                "symbol_cap": 180,
                "task_output_subdir": "multi_seed",
                "write_whitelist": [],
            },
            "WE-P1-06": {
                "task_id": "WE-P1-06",
                "priority": "P1",
                "schedule": "weekend",
                "phase": 3,
                "must_run": False,
                "defer_policy": "next_weekend",
                "rotating_priority": 0,
                "max_defer_runs": 2,
                "force_run_on_disk_usage_pct": 100.0,
                "max_wall_time_minutes": 180,
                "task_output_subdir": "cost_sensitivity",
                "write_whitelist": [],
            },
            "WE-P1-07": {
                "task_id": "WE-P1-07",
                "priority": "P1",
                "schedule": "weekend",
                "phase": 3,
                "must_run": False,
                "defer_policy": "next_weekend",
                "rotating_priority": 0,
                "max_defer_runs": 2,
                "force_run_on_disk_usage_pct": 100.0,
                "max_wall_time_minutes": 120,
                "task_output_subdir": "disaster_recovery",
                "write_whitelist": [],
                "min_interval_days": 14,
            },
            "WE-P2-08": {
                "task_id": "WE-P2-08",
                "priority": "P2",
                "schedule": "weekend",
                "phase": 1,
                "must_run": False,
                "defer_policy": "next_weekend",
                "rotating_priority": 0,
                "max_defer_runs": 2,
                "force_run_on_disk_usage_pct": 70.0,
                "max_wall_time_minutes": 180,
                "task_output_subdir": "storage_maintenance",
                "write_whitelist": [
                    {
                        "task": "WE-P2-08",
                        "paths": ["artifacts/faiss_snapshots/", "artifacts/shadow_logs/"],
                        "actions": ["compress", "delete_via_queue"],
                    }
                ],
            },
        }


@lru_cache(maxsize=1)
def _runtime_service_module() -> Any:
    return import_module("stock_analyzer.runtime.service")


def _clock_shift_minutes(clock: dt_time, minutes: int) -> dt_time:
    return cast(dt_time, _runtime_service_module()._clock_shift_minutes(clock, minutes))


def _clock_to_seconds(clock: dt_time) -> int:
    return cast(int, _runtime_service_module()._clock_to_seconds(clock))


def _last_friday(current: date) -> date:
    return cast(date, _runtime_service_module()._last_friday(current))


def _min_clock(left: dt_time, right: dt_time) -> dt_time:
    return cast(dt_time, _runtime_service_module()._min_clock(left, right))


def _parse_hhmm_time(raw: str) -> dt_time:
    return cast(dt_time, _runtime_service_module()._parse_hhmm_time(raw))
