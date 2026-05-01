"""Simple daily scheduler for local runtime."""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import asdict, dataclass
from datetime import date, datetime, time

from stock_analyzer.config import SchedulerConfig

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

    def run_due(self, now: datetime | None = None) -> list[ScheduledTaskResult]:
        if not self._config.enabled:
            return []

        current = now or datetime.now()
        current_weekday = current.weekday()
        results: list[ScheduledTaskResult] = []
        ordered_jobs = sorted(self._jobs.items(), key=lambda item: item[1].trigger_time)
        for name, job in ordered_jobs:
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
            if job.latest_time is not None and current.time() > job.latest_time and not already_ran:
                self._last_run[name] = today
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
                ran, success, detail, payload = _normalize_callback_result(job.callback())
                if ran:
                    self._last_run[name] = today
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
                ran, success, detail, payload = _normalize_callback_result(
                    interval_job.callback()
                )
                if ran:
                    self._last_interval_slot[name] = slot_marker
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
                results.append(
                    ScheduledTaskResult(
                        job=name,
                        ran=True,
                        success=False,
                        detail=str(exc),
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
    if (current_min - start_min) % job.interval_minutes != 0:
        return None
    return current_min
