"""Idle queue due-task selection, run-state, and task dispatch workflows."""

# mypy: disable-error-code=redundant-cast

from __future__ import annotations

import shutil
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from functools import lru_cache
from importlib import import_module
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService


class RuntimeIdleQueueRegistryService:
    """Manage idle queue scheduling state and task dispatch."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service

    def idle_prepare_weekend_cycle_state(self, trade_date: str) -> None:
        service = self._service
        if not trade_date or trade_date == service._idle_last_weekend_trade_date:
            return
        previous_trade_date = service._idle_last_weekend_trade_date
        p1_tasks = [
            "WE-LEARN-01",
            "WE-P1-03",
            "WE-P1-04",
            "WE-P1-05",
            "WE-P1-06",
            "WE-P1-07",
        ]
        if previous_trade_date:
            for task_id in p1_tasks:
                if service._idle_already_ran(task_id=task_id, trade_date=previous_trade_date):
                    service._idle_weekend_rotation_scores[task_id] = 0
                else:
                    service._idle_weekend_rotation_scores[task_id] = (
                        service._idle_weekend_rotation_scores.get(task_id, 0) + 1
                    )
        service._idle_last_weekend_trade_date = trade_date

    def idle_weekend_due_tasks(self, trade_date: str, now_clock: datetime) -> list[str]:
        service = self._service
        service._idle_prepare_weekend_cycle_state(trade_date=trade_date)
        manifests = service._idle_task_manifests
        due: list[str] = []

        p0_tasks = ["WE-P0-01", "WE-P0-02"]
        p1_tasks = service._idle_weekend_sorted_p1_tasks()
        p2_tasks = ["WE-P2-08"]

        for task_id in p0_tasks:
            if service._idle_already_ran(task_id=task_id, trade_date=trade_date):
                continue
            due.append(task_id)
        if due:
            return due

        for task_id in p2_tasks:
            if service._idle_already_ran(task_id=task_id, trade_date=trade_date):
                continue
            if service._idle_should_force_weekend_task(task_id=task_id):
                due.append(task_id)
        if due:
            return due

        remaining_minutes = service._idle_weekend_remaining_minutes(now_clock)
        for task_id in p1_tasks:
            if service._idle_already_ran(task_id=task_id, trade_date=trade_date):
                continue
            manifest = manifests.get(task_id, {})
            min_interval_days = _as_int(manifest.get("min_interval_days"), default=0)
            if min_interval_days > 0:
                last_trade_date = service._idle_latest_trade_date_for_task(task_id=task_id)
                current_trade_date_dt = _parse_trade_date(trade_date)
                if last_trade_date and current_trade_date_dt is not None:
                    last_trade_date_dt = _parse_trade_date(last_trade_date)
                    if last_trade_date_dt is not None:
                        delta_days = (current_trade_date_dt - last_trade_date_dt).days
                        if delta_days < min_interval_days:
                            continue

            max_wall_time = _as_int(manifest.get("max_wall_time_minutes"), default=0)
            min_remaining_minutes = _as_int(manifest.get("min_remaining_minutes"), default=0)
            must_run = bool(manifest.get("must_run", False))
            required_minutes = max(max_wall_time, min_remaining_minutes)
            if not must_run and required_minutes > 0 and remaining_minutes < required_minutes:
                service._idle_record_weekend_defer(task_id=task_id, trade_date=trade_date)
                continue
            due.append(task_id)
        if due:
            return due

        for task_id in p2_tasks:
            if service._idle_already_ran(task_id=task_id, trade_date=trade_date):
                continue
            manifest = manifests.get(task_id, {})
            max_wall_time = _as_int(manifest.get("max_wall_time_minutes"), default=0)
            if remaining_minutes < max_wall_time and not service._idle_should_force_weekend_task(
                task_id
            ):
                service._idle_record_weekend_defer(task_id=task_id, trade_date=trade_date)
                continue
            due.append(task_id)
        return due

    def idle_weekend_sorted_p1_tasks(self) -> list[str]:
        service = self._service
        p1_tasks = [
            "WE-LEARN-01",
            "WE-P1-03",
            "WE-P1-04",
            "WE-P1-05",
            "WE-P1-06",
            "WE-P1-07",
        ]
        return sorted(
            p1_tasks,
            key=lambda task_id: (
                -(service._idle_weekend_rotation_scores.get(task_id, 0)),
                task_id,
            ),
        )

    def idle_weekend_remaining_minutes(self, now_clock: datetime) -> int:
        if now_clock.weekday() == 5:
            weekend_end = datetime.combine(now_clock.date() + timedelta(days=2), dt_time(0, 0, 0))
        elif now_clock.weekday() == 6:
            weekend_end = datetime.combine(now_clock.date() + timedelta(days=1), dt_time(0, 0, 0))
        else:
            weekend_end = now_clock
        remaining_seconds = max((weekend_end - now_clock).total_seconds(), 0.0)
        return int(remaining_seconds // 60)

    def idle_should_force_weekend_task(self, task_id: str) -> bool:
        service = self._service
        manifest = service._idle_task_manifests.get(task_id, {})
        max_defer_runs = _as_int(manifest.get("max_defer_runs"), default=0)
        defer_runs = service._idle_weekend_defer_runs.get(task_id, 0)
        if max_defer_runs > 0 and defer_runs >= max_defer_runs:
            return True

        force_threshold = _as_float(manifest.get("force_run_on_disk_usage_pct"), default=101.0)
        if force_threshold <= 0:
            return False
        output_root = service._resolve_evolution_path(service._config.idle_queue.output_root)
        try:
            usage = shutil.disk_usage(output_root)
        except OSError:
            return False
        total = max(int(usage.total), 1)
        usage_pct = float(usage.used / total * 100.0)
        return usage_pct >= force_threshold

    def idle_record_weekend_defer(self, task_id: str, trade_date: str) -> None:
        service = self._service
        key = f"{trade_date}:{task_id}"
        if (
            key in service._idle_task_status
            and service._idle_task_status.get(key, "") == "deferred"
        ):
            return
        service._idle_weekend_defer_runs[task_id] = (
            service._idle_weekend_defer_runs.get(task_id, 0) + 1
        )
        service._idle_set_task_status(task_id=task_id, trade_date=trade_date, status="deferred")

    def idle_latest_trade_date_for_task(self, task_id: str) -> str:
        service = self._service
        latest = ""
        for key in service._idle_task_run_keys.keys():
            parts = key.split(":", maxsplit=1)
            if len(parts) != 2:
                continue
            trade_date, found_task_id = parts
            if found_task_id != task_id:
                continue
            if trade_date > latest:
                latest = trade_date
        return latest

    def idle_due_tasks(self, context: dict[str, object]) -> list[str]:
        service = self._service
        window = str(context.get("window", "off"))
        if window == "off":
            return []

        trade_date = str(context.get("trade_date", ""))
        now_clock = _parse_iso_datetime(str(context.get("now", "")))
        if now_clock is None:
            now_clock = datetime.now()

        if window == "weekend":
            return cast(
                list[str],
                service._idle_weekend_due_tasks(
                    trade_date=trade_date,
                    now_clock=now_clock,
                ),
            )

        soft_stop = _parse_hhmmss_time(str(context.get("effective_soft_stop", "08:20:00")))
        report_deadline = _parse_hhmmss_time(
            str(context.get("effective_report_deadline", "08:23:00"))
        )
        trigger_time = _parse_hhmmss_time(str(context.get("trigger_time", "08:15:00")))
        hard_stop = _parse_hhmmss_time(str(context.get("effective_hard_stop", "08:30:00")))
        hard_plus_grace = _clock_shift_minutes(hard_stop, 1)

        due: list[str] = []
        wd_p0_done = service._idle_already_ran("WD-P0-01", trade_date)
        report_done = service._idle_already_ran("WD-REPORT", trade_date)
        in_tail_window = now_clock.time() <= hard_plus_grace
        ordered_workday_tasks = [
            "WD-P0-01",
            "WD-P0-02",
            "WD-P0-03",
            "WD-P0-04",
            "WD-P1-05",
            "WD-P1-06",
            "WD-P1-07",
        ]

        p0_01_status = service._idle_get_task_status("WD-P0-01", trade_date)
        p0_01_gate_ok = p0_01_status in {"ok", "degraded", "fallback"}

        if in_tail_window:
            if now_clock.time() >= report_deadline and not report_done:
                due.append("WD-REPORT")
            if now_clock.time() < soft_stop:
                for task_id in ordered_workday_tasks:
                    if service._idle_already_ran(task_id, trade_date):
                        continue
                    if task_id.startswith("WD-P1-") and not p0_01_gate_ok:
                        continue
                    due.append(task_id)
                    break
            if not report_done and (wd_p0_done or now_clock.time() >= trigger_time):
                due.append("WD-REPORT")
        else:
            for task_id in ordered_workday_tasks:
                if service._idle_already_ran(task_id, trade_date):
                    continue
                if task_id.startswith("WD-P1-") and not p0_01_gate_ok:
                    continue
                due.append(task_id)
                break
            if not due and not report_done:
                due.append("WD-REPORT")
        return _dedupe_preserve_order(due)

    def idle_already_ran(self, task_id: str, trade_date: str) -> bool:
        service = self._service
        return f"{trade_date}:{task_id}" in service._idle_task_run_keys

    def idle_set_task_status(self, task_id: str, trade_date: str, status: str) -> None:
        service = self._service
        key = f"{trade_date}:{task_id}"
        service._idle_task_status[key] = status.strip().lower()
        _trim_run_state_map(service._idle_task_status, limit=4000)

    def idle_get_task_status(self, task_id: str, trade_date: str) -> str:
        service = self._service
        return str(service._idle_task_status.get(f"{trade_date}:{task_id}", "")).strip().lower()

    def idle_mark_ran(self, task_id: str, trade_date: str) -> None:
        service = self._service
        key = f"{trade_date}:{task_id}"
        service._idle_task_run_keys[key] = datetime.now().isoformat()
        _trim_run_state_map(service._idle_task_run_keys, limit=4000)

    def run_idle_task(self, task_id: str, context: dict[str, object]) -> dict[str, object]:
        service = self._service
        if task_id == "WD-P0-01":
            return cast(dict[str, object], service._idle_task_wd_p0_01(context=context))
        if task_id == "WD-P0-02":
            return cast(dict[str, object], service._idle_task_wd_p0_02(context=context))
        if task_id == "WD-P0-03":
            return cast(dict[str, object], service._idle_task_wd_p0_03(context=context))
        if task_id == "WD-P0-04":
            return cast(dict[str, object], service._idle_task_wd_p0_04(context=context))
        if task_id == "WD-P1-05":
            return cast(dict[str, object], service._idle_task_wd_p1_05(context=context))
        if task_id == "WD-P1-06":
            return cast(dict[str, object], service._idle_task_wd_p1_06(context=context))
        if task_id == "WD-P1-07":
            return cast(dict[str, object], service._idle_task_wd_p1_07(context=context))
        if task_id == "WD-REPORT":
            return cast(dict[str, object], service._idle_task_wd_report(context=context))
        if task_id == "WE-P0-01":
            return cast(dict[str, object], service._idle_task_we_p0_01(context=context))
        if task_id == "WE-P0-02":
            return cast(dict[str, object], service._idle_task_we_p0_02(context=context))
        if task_id == "WE-LEARN-01":
            return cast(dict[str, object], service._idle_task_we_learn_01(context=context))
        if task_id == "WE-P1-03":
            return cast(dict[str, object], service._idle_task_we_p1_03(context=context))
        if task_id == "WE-P1-04":
            return cast(dict[str, object], service._idle_task_we_p1_04(context=context))
        if task_id == "WE-P1-05":
            return cast(dict[str, object], service._idle_task_we_p1_05(context=context))
        if task_id == "WE-P1-06":
            return cast(dict[str, object], service._idle_task_we_p1_06(context=context))
        if task_id == "WE-P1-07":
            return cast(dict[str, object], service._idle_task_we_p1_07(context=context))
        if task_id == "WE-P2-08":
            return cast(dict[str, object], service._idle_task_we_p2_08(context=context))
        return {
            "status": "skipped",
            "reason": f"task_not_implemented:{task_id}",
            "output_files": [],
        }

    def idle_task_ttl(self, task_id: str) -> int:
        service = self._service
        cfg = service._config.idle_queue
        if task_id == "WE-P1-07":
            return max(1, int(cfg.fallback_ttl_low_freq_runs))
        if task_id.startswith("WE-"):
            return max(1, int(cfg.fallback_ttl_weekend_runs))
        return max(1, int(cfg.fallback_ttl_workday_runs))


def _trim_run_state_map(mapping: dict[str, str], limit: int) -> None:
    if len(mapping) <= limit:
        return
    keys = sorted(mapping.keys())
    drop = len(mapping) - limit
    for stale_key in keys[:drop]:
        mapping.pop(stale_key, None)


@lru_cache(maxsize=1)
def _runtime_service_module() -> Any:
    return import_module("stock_analyzer.runtime.service")


def _as_float(value: object, default: float) -> float:
    return cast(float, _runtime_service_module()._as_float(value, default))


def _as_int(value: object, default: int) -> int:
    return cast(int, _runtime_service_module()._as_int(value, default))


def _clock_shift_minutes(clock: dt_time, minutes: int) -> dt_time:
    return cast(dt_time, _runtime_service_module()._clock_shift_minutes(clock, minutes))


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    return cast(list[str], _runtime_service_module()._dedupe_preserve_order(items))


def _parse_hhmmss_time(raw: str) -> dt_time:
    return cast(dt_time, _runtime_service_module()._parse_hhmmss_time(raw))


def _parse_iso_datetime(value: object) -> datetime | None:
    return cast(datetime | None, _runtime_service_module()._parse_iso_datetime(value))


def _parse_trade_date(value: str) -> date | None:
    return cast(date | None, _runtime_service_module()._parse_trade_date(value))
