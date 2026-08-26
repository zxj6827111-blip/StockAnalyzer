"""Simple daily scheduler for local runtime."""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4

from stock_analyzer.config import SchedulerConfig
from stock_analyzer.ops.file_lock import DistributedFileLock

JobCallback = Callable[[], dict[str, object]]
DatePredicate = Callable[[date], bool]
_SCHEDULER_RAN_KEY = "_scheduler_ran"
_SCHEDULER_SUCCESS_KEY = "_scheduler_success"
_SCHEDULER_DETAIL_KEY = "_scheduler_detail"


@dataclass(slots=True)
class ScheduledTaskResult:
    job: str
    ran: bool
    success: bool
    detail: str
    payload: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class _ScheduledJob:
    name: str
    trigger_time: time
    latest_time: time | None
    callback: JobCallback
    weekdays: frozenset[int] | None = None
    date_predicate: DatePredicate | None = None


@dataclass(slots=True)
class _IntervalJob:
    name: str
    window_start: time
    window_end: time
    interval_minutes: int
    callback: JobCallback
    weekdays: frozenset[int] | None = None
    date_predicate: DatePredicate | None = None


class DailyScheduler:
    """Run jobs once per day when current time crosses configured trigger."""

    def __init__(self, config: SchedulerConfig) -> None:
        self._config = config
        self._jobs: dict[str, _ScheduledJob] = {}
        self._interval_jobs: dict[str, _IntervalJob] = {}
        self._last_run: dict[str, date] = {}
        self._last_interval_slot: dict[str, tuple[date, int]] = {}
        self._job_runtime: dict[str, dict[str, object]] = {}

    def register(
        self,
        name: str,
        trigger_hhmm: str,
        callback: JobCallback,
        latest_hhmm: str = "",
        weekdays: Collection[int] | None = None,
        date_predicate: DatePredicate | None = None,
    ) -> None:
        self._jobs[name] = _ScheduledJob(
            name=name,
            trigger_time=_parse_hhmm(trigger_hhmm),
            latest_time=_parse_hhmm(latest_hhmm) if latest_hhmm.strip() else None,
            callback=callback,
            weekdays=_normalize_weekdays(weekdays),
            date_predicate=date_predicate,
        )

    def register_interval(
        self,
        name: str,
        window_start_hhmm: str,
        window_end_hhmm: str,
        interval_minutes: int,
        callback: JobCallback,
        weekdays: Collection[int] | None = None,
        date_predicate: DatePredicate | None = None,
    ) -> None:
        if interval_minutes <= 0:
            raise ValueError("interval_minutes must be > 0")
        window_start = _parse_hhmm(window_start_hhmm)
        window_end = _parse_hhmm(window_end_hhmm)
        if _to_minutes(window_end) < _to_minutes(window_start):
            raise ValueError("window_end must be >= window_start")
        self._interval_jobs[name] = _IntervalJob(
            name=name,
            window_start=window_start,
            window_end=window_end,
            interval_minutes=interval_minutes,
            callback=callback,
            weekdays=_normalize_weekdays(weekdays),
            date_predicate=date_predicate,
        )

    def registered_job_names(self) -> list[str]:
        return sorted([*self._jobs.keys(), *self._interval_jobs.keys()])

    def due_job_names(
        self,
        now: datetime | None = None,
        only_jobs: Collection[str] | None = None,
    ) -> list[str]:
        """Inspect due jobs without invoking callbacks or mutating scheduler state."""
        if not self._config.enabled:
            return []
        current = now or datetime.now()
        selectors = _normalize_job_selectors(only_jobs)
        matched_selectors: set[str] = set()
        due: list[str] = []
        for name, job in sorted(self._jobs.items(), key=lambda item: item[1].trigger_time):
            if selectors and not _job_matches_selectors(name, selectors, matched_selectors):
                continue
            if not _date_matches(
                current.date(),
                current_weekday=current.weekday(),
                weekdays=job.weekdays,
                date_predicate=job.date_predicate,
            ):
                continue
            if self._last_run.get(name) == current.date():
                continue
            next_due_raw = str(self._job_runtime.get(name, {}).get("next_due_at", "")).strip()
            if next_due_raw:
                try:
                    if current < datetime.fromisoformat(next_due_raw):
                        continue
                except ValueError:
                    pass
            if current.time() >= job.trigger_time:
                due.append(name)
        for name, interval_job in self._interval_jobs.items():
            if selectors and not _job_matches_selectors(name, selectors, matched_selectors):
                continue
            if not _date_matches(
                current.date(),
                current_weekday=current.weekday(),
                weekdays=interval_job.weekdays,
                date_predicate=interval_job.date_predicate,
            ):
                continue
            slot = _due_interval_slot(job=interval_job, current=current.time())
            if slot is None or self._last_interval_slot.get(name) == (current.date(), slot):
                continue
            next_due_raw = str(self._job_runtime.get(name, {}).get("next_due_at", "")).strip()
            if next_due_raw:
                try:
                    if current < datetime.fromisoformat(next_due_raw):
                        continue
                except ValueError:
                    pass
            due.append(name)
        return due

    def run_due(
        self,
        now: datetime | None = None,
        only_jobs: Collection[str] | None = None,
    ) -> list[ScheduledTaskResult]:
        if not self._config.enabled:
            return []

        current = now or datetime.now()
        current_weekday = current.weekday()
        results: list[ScheduledTaskResult] = []
        selectors = _normalize_job_selectors(only_jobs)
        matched_selectors: set[str] = set()
        ordered_jobs = sorted(self._jobs.items(), key=lambda item: item[1].trigger_time)
        for name, job in ordered_jobs:
            if selectors and not _job_matches_selectors(name, selectors, matched_selectors):
                continue
            if not _date_matches(
                current.date(),
                current_weekday=current_weekday,
                weekdays=job.weekdays,
                date_predicate=job.date_predicate,
            ):
                results.append(
                    ScheduledTaskResult(
                        job=name,
                        ran=False,
                        success=True,
                        detail="not_scheduled_today",
                        payload={},
                    )
                )
                continue
            today = current.date()
            already_ran = self._last_run.get(name) == today
            next_due_raw = str(self._job_runtime.get(name, {}).get("next_due_at", "")).strip()
            if next_due_raw:
                try:
                    next_due = datetime.fromisoformat(next_due_raw)
                except ValueError:
                    next_due = None
                if next_due is not None and current < next_due:
                    # 失败退避窗口内不执行（next_due 由 _record_job_result
                    # 按连续失败次数指数推后），避免失败任务的无限立即重试。
                    results.append(
                        ScheduledTaskResult(
                            job=name,
                            ran=False,
                            success=True,
                            detail="backoff",
                            payload={},
                        )
                    )
                    continue
            if job.latest_time is not None and current.time() > job.latest_time and not already_ran:
                self._last_run[name] = today
                self._record_job_result(
                    name=name,
                    attempted_at=current,
                    ran=False,
                    success=False,
                    detail="expired",
                    next_due_at=datetime.combine(today + timedelta(days=1), job.trigger_time),
                )
                results.append(
                    ScheduledTaskResult(
                        job=name,
                        ran=False,
                        success=True,
                        detail="expired",
                        payload={},
                    )
                )
                continue
            should_run = current.time() >= job.trigger_time and not already_ran
            if not should_run:
                results.append(
                    ScheduledTaskResult(
                        job=name,
                        ran=False,
                        success=True,
                        detail="not_due",
                        payload={},
                    )
                )
                continue

            try:
                ran, success, detail, payload = _execute_job_callback(
                    config=self._config,
                    name=name,
                    callback=job.callback,
                )
                if ran and success:
                    self._last_run[name] = today
                self._record_job_result(
                    name=name,
                    attempted_at=current,
                    ran=ran,
                    success=success,
                    detail=detail,
                    next_due_at=datetime.combine(
                        today + timedelta(days=1) if ran and success else today,
                        job.trigger_time,
                    ),
                )
                results.append(
                    ScheduledTaskResult(
                        job=name,
                        ran=ran,
                        success=success,
                        detail=detail,
                        payload=payload,
                    )
                )
            except Exception as exc:
                self._record_job_result(
                    name=name,
                    attempted_at=current,
                    ran=True,
                    success=False,
                    detail=str(exc),
                    next_due_at=current,
                )
                results.append(
                    ScheduledTaskResult(
                        job=name,
                        ran=True,
                        success=False,
                        detail=str(exc),
                        payload={},
                    )
                )

        for name, interval_job in self._interval_jobs.items():
            if selectors and not _job_matches_selectors(name, selectors, matched_selectors):
                continue
            if not _date_matches(
                current.date(),
                current_weekday=current_weekday,
                weekdays=interval_job.weekdays,
                date_predicate=interval_job.date_predicate,
            ):
                results.append(
                    ScheduledTaskResult(
                        job=name,
                        ran=False,
                        success=True,
                        detail="not_scheduled_today",
                        payload={},
                    )
                )
                continue
            slot = _due_interval_slot(job=interval_job, current=current.time())
            if slot is None:
                results.append(
                    ScheduledTaskResult(
                        job=name,
                        ran=False,
                        success=True,
                        detail="not_due",
                        payload={},
                    )
                )
                continue

            slot_marker = (current.date(), slot)
            if self._last_interval_slot.get(name) == slot_marker:
                results.append(
                    ScheduledTaskResult(
                        job=name,
                        ran=False,
                        success=True,
                        detail="not_due",
                        payload={},
                    )
                )
                continue

            try:
                ran, success, detail, payload = _execute_job_callback(
                    config=self._config,
                    name=name,
                    callback=interval_job.callback,
                )
                if ran and success:
                    self._last_interval_slot[name] = slot_marker
                self._record_job_result(
                    name=name,
                    attempted_at=current,
                    ran=ran,
                    success=success,
                    detail=detail,
                    next_due_at=_next_interval_due(
                        interval_job,
                        current=current,
                        completed_slot=slot if ran and success else None,
                    ),
                )
                results.append(
                    ScheduledTaskResult(
                        job=name,
                        ran=ran,
                        success=success,
                        detail=detail,
                        payload=payload,
                    )
                )
            except Exception as exc:
                self._record_job_result(
                    name=name,
                    attempted_at=current,
                    ran=True,
                    success=False,
                    detail=str(exc),
                    next_due_at=current,
                )
                results.append(
                    ScheduledTaskResult(
                        job=name,
                        ran=True,
                        success=False,
                        detail=str(exc),
                        payload={},
                    )
                )
        for selector in selectors:
            if selector not in matched_selectors:
                results.append(
                    ScheduledTaskResult(
                        job=selector,
                        ran=False,
                        success=False,
                        detail="unknown_job",
                        payload={},
                    )
                )
        return results

    def export_state(self) -> dict[str, object]:
        return {
            "last_run": {
                name: value.isoformat()
                for name, value in sorted(self._last_run.items(), key=lambda item: item[0])
            },
            "last_interval_slot": {
                name: {"date": value[0].isoformat(), "slot": value[1]}
                for name, value in sorted(
                    self._last_interval_slot.items(), key=lambda item: item[0]
                )
            },
            "jobs": {name: dict(value) for name, value in sorted(self._job_runtime.items())},
        }

    def import_state(self, raw: object) -> None:
        if not isinstance(raw, dict):
            return

        loaded_last_run: dict[str, date] = {}
        raw_last_run = raw.get("last_run")
        if isinstance(raw_last_run, dict):
            for name, value in raw_last_run.items():
                try:
                    loaded_last_run[str(name).strip()] = date.fromisoformat(str(value))
                except ValueError:
                    continue

        loaded_interval_slot: dict[str, tuple[date, int]] = {}
        raw_interval_slot = raw.get("last_interval_slot")
        if isinstance(raw_interval_slot, dict):
            for name, value in raw_interval_slot.items():
                if not isinstance(value, dict):
                    continue
                try:
                    slot_date = date.fromisoformat(str(value.get("date", "")))
                except ValueError:
                    continue
                slot = value.get("slot")
                if not isinstance(slot, int):
                    continue
                loaded_interval_slot[str(name).strip()] = (slot_date, slot)

        self._last_run = loaded_last_run
        self._last_interval_slot = loaded_interval_slot
        raw_jobs = raw.get("jobs")
        self._job_runtime = {
            str(name): dict(value)
            for name, value in raw_jobs.items()
            if isinstance(value, dict)
        } if isinstance(raw_jobs, dict) else {}

    def _record_job_result(
        self,
        *,
        name: str,
        attempted_at: datetime,
        ran: bool,
        success: bool,
        detail: str,
        next_due_at: datetime,
    ) -> None:
        previous = self._job_runtime.get(name, {})
        raw_failures = previous.get("consecutive_failures", 0)
        try:
            failures = int(raw_failures) if isinstance(raw_failures, (int, float, str)) else 0
        except ValueError:
            failures = 0
        if ran and success:
            failures = 0
        elif ran or detail == "expired":
            failures += 1
        if ran and not success:
            # 失败退避：失败的每日/间隔任务不能立即重试（旧实现把 next_due
            # 设为当天触发点/当前时刻，触发点已过时下一次 poll 立即再跑，
            # 单点失败会形成无节制的重试风暴持续烧 CPU）。退避按连续失败
            # 次数指数递增、封顶 30 分钟；传入了更晚的 next_due（如次日
            # 触发点）时尊重原计划。
            backoff_minutes = min(30, 2 ** min(max(failures - 1, 0), 5))
            backoff_due = attempted_at + timedelta(minutes=backoff_minutes)
            if backoff_due > next_due_at:
                next_due_at = backoff_due
        attempt_iso = (
            attempted_at.isoformat()
            if ran or detail == "expired"
            else str(previous.get("last_attempt_at", ""))
        )
        success_iso = (
            attempted_at.isoformat()
            if ran and success
            else str(previous.get("last_success_at", ""))
        )
        failure_detail = (
            ""
            if ran and success
            else detail
            if ran or detail == "expired"
            else str(previous.get("last_failure", ""))
        )
        expired_iso = (
            attempted_at.isoformat()
            if detail == "expired"
            else str(previous.get("last_expired", ""))
        )
        status = (
            "success"
            if ran and success
            else "expired"
            if detail == "expired"
            else "failed"
            if ran
            else str(previous.get("status", "idle"))
        )
        payload = {
            "last_attempt_at": attempt_iso,
            "last_success_at": success_iso,
            "last_attempt": attempt_iso,
            "last_success": success_iso,
            "last_failure": failure_detail,
            "last_expired": expired_iso,
            "running_since": "",
            "heartbeat_at": attempted_at.isoformat(),
            "run_id": uuid4().hex if ran or detail == "expired" else str(
                previous.get("run_id", "")
            ),
            "status": status,
            "consecutive_failures": failures,
            "next_due_at": next_due_at.isoformat(),
        }
        self._job_runtime[name] = payload


def _parse_hhmm(raw: str) -> time:
    parts = raw.split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid hh:mm format: {raw}")
    hour = int(parts[0])
    minute = int(parts[1])
    return time(hour=hour, minute=minute)


def _normalize_weekdays(weekdays: Collection[int] | None) -> frozenset[int] | None:
    if weekdays is None:
        return None
    normalized = frozenset(int(value) for value in weekdays)
    if not normalized:
        raise ValueError("weekdays must not be empty")
    invalid = sorted(value for value in normalized if value < 0 or value > 6)
    if invalid:
        raise ValueError(f"weekdays must be between 0 and 6: {invalid}")
    return normalized


def _normalize_job_selectors(selectors: Collection[str] | None) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in selectors or []:
        value = str(item).strip()
        if not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return tuple(normalized)


def _job_matches_selectors(
    job_name: str,
    selectors: tuple[str, ...],
    matched_selectors: set[str],
) -> bool:
    matched = False
    for selector in selectors:
        if job_name == selector or _job_family_matches_selector(
            job_name=job_name,
            selector=selector,
        ):
            matched_selectors.add(selector)
            matched = True
    return matched


def _job_family_matches_selector(*, job_name: str, selector: str) -> bool:
    family = {
        "live_runtime": "week5_live_runtime",
        "week5_live_runtime": "week5_live_runtime",
        "week5_automation_live_runtime": "week5_automation_live_runtime",
        "week5_first_board": "week5_first_board",
        "week5_market_radar": "week5_market_radar",
        "week5_automation_market_radar": "week5_automation_market_radar",
    }.get(selector)
    return bool(family and job_name.startswith(f"{family}_"))


def _weekday_matches(current_weekday: int, weekdays: frozenset[int] | None) -> bool:
    return weekdays is None or current_weekday in weekdays


def _date_matches(
    current_date: date,
    *,
    current_weekday: int,
    weekdays: frozenset[int] | None,
    date_predicate: DatePredicate | None,
) -> bool:
    if not _weekday_matches(current_weekday, weekdays):
        return False
    return date_predicate is None or bool(date_predicate(current_date))


def scheduler_job_lock_path(config: SchedulerConfig, name: str) -> Path:
    safe_name = "".join(
        character if character.isalnum() or character in {"-", "_", "."} else "_"
        for character in str(name).strip()
    )
    return Path(config.job_lock_dir) / f"{safe_name or 'unnamed'}.lock"


def _execute_job_callback(
    *,
    config: SchedulerConfig,
    name: str,
    callback: JobCallback,
) -> tuple[bool, bool, str, dict[str, object]]:
    lock: DistributedFileLock | None = None
    if config.job_lock_enabled:
        lock_path = scheduler_job_lock_path(config, name)
        lock = DistributedFileLock(
            lock_path,
            stale_after_sec=max(1, int(config.job_lock_stale_after_sec)),
        )
        if not lock.acquire():
            return False, True, "already_running", {"lock_path": str(lock_path)}
    try:
        return _normalize_callback_result(callback())
    finally:
        if lock is not None:
            lock.release()


def _normalize_callback_result(
    payload: dict[str, object] | None,
) -> tuple[bool, bool, str, dict[str, object]]:
    normalized = dict(payload or {})
    raw_ran = normalized.pop(_SCHEDULER_RAN_KEY, True)
    raw_success = normalized.pop(_SCHEDULER_SUCCESS_KEY, True)
    raw_detail = normalized.pop(_SCHEDULER_DETAIL_KEY, "ok")
    ran = bool(raw_ran)
    success = bool(raw_success)
    detail = str(raw_detail).strip() or ("ok" if ran else "deferred")
    return ran, success, detail, normalized


def _to_minutes(clock: time) -> int:
    return clock.hour * 60 + clock.minute


def _due_interval_slot(job: _IntervalJob, current: time) -> int | None:
    current_min = _to_minutes(current)
    start_min = _to_minutes(job.window_start)
    end_min = _to_minutes(job.window_end)
    if current_min < start_min or current_min > end_min:
        return None
    return start_min + ((current_min - start_min) // job.interval_minutes) * job.interval_minutes


def _next_interval_due(
    job: _IntervalJob,
    *,
    current: datetime,
    completed_slot: int | None,
) -> datetime:
    start_min = _to_minutes(job.window_start)
    end_min = _to_minutes(job.window_end)
    if completed_slot is None:
        next_min = max(start_min, _to_minutes(current.time()))
    else:
        next_min = completed_slot + job.interval_minutes
    next_day = current.date()
    if next_min > end_min:
        next_day += timedelta(days=1)
        next_min = start_min
    return datetime.combine(next_day, time(hour=next_min // 60, minute=next_min % 60))
