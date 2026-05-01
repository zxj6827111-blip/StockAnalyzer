from __future__ import annotations

from datetime import datetime

from stock_analyzer.config import SchedulerConfig
from stock_analyzer.runtime.scheduler import DailyScheduler


def _record_run(runs: list[str]) -> dict[str, object]:
    runs.append("ok")
    payload: dict[str, object] = {"ok": True}
    return payload


def test_scheduler_runs_job_only_once_after_due_time() -> None:
    config = SchedulerConfig(
        enabled=True,
        premarket_time="08:30",
        auction_report_time="09:26",
        close_reconcile_time="15:30",
    )
    scheduler = DailyScheduler(config=config)
    runs: list[str] = []

    scheduler.register("test_job", "08:30", callback=lambda: _record_run(runs))

    before_due = datetime.fromisoformat("2026-03-01T08:29:00")
    first_due = datetime.fromisoformat("2026-03-01T08:30:00")
    second_same_day = datetime.fromisoformat("2026-03-01T09:00:00")

    result_before = scheduler.run_due(before_due)
    result_first = scheduler.run_due(first_due)
    result_second = scheduler.run_due(second_same_day)

    assert result_before[0].ran is False
    assert result_first[0].ran is True
    assert result_second[0].ran is False
    assert runs == ["ok"]


def test_scheduler_interval_job_runs_once_per_slot_within_window() -> None:
    config = SchedulerConfig(
        enabled=True,
        premarket_time="08:30",
        auction_report_time="09:26",
        close_reconcile_time="15:30",
    )
    scheduler = DailyScheduler(config=config)
    runs: list[str] = []

    scheduler.register_interval(
        name="interval_job",
        window_start_hhmm="09:30",
        window_end_hhmm="09:32",
        interval_minutes=1,
        callback=lambda: _record_run(runs),
    )

    before = scheduler.run_due(datetime.fromisoformat("2026-03-01T09:29:00"))
    first = scheduler.run_due(datetime.fromisoformat("2026-03-01T09:30:00"))
    duplicate = scheduler.run_due(datetime.fromisoformat("2026-03-01T09:30:30"))
    second = scheduler.run_due(datetime.fromisoformat("2026-03-01T09:31:00"))
    out_of_window = scheduler.run_due(datetime.fromisoformat("2026-03-01T09:33:00"))

    assert before[0].ran is False
    assert first[0].ran is True
    assert duplicate[0].ran is False
    assert second[0].ran is True
    assert out_of_window[0].ran is False
    assert runs == ["ok", "ok"]


def test_scheduler_retries_daily_job_when_callback_does_not_claim_execution_rights() -> None:
    config = SchedulerConfig(
        enabled=True,
        premarket_time="08:30",
        auction_report_time="09:26",
        close_reconcile_time="15:30",
    )
    scheduler = DailyScheduler(config=config)
    attempts: list[str] = []

    def _callback() -> dict[str, object]:
        attempts.append("called")
        if len(attempts) == 1:
            return {
                "status": "already_running",
                "_scheduler_ran": False,
                "_scheduler_detail": "already_running",
            }
        return {"status": "launched"}

    scheduler.register("test_job", "08:30", callback=_callback)

    first_due = scheduler.run_due(datetime.fromisoformat("2026-03-01T08:30:00"))
    state_after_first_due = scheduler.export_state()
    second_due = scheduler.run_due(datetime.fromisoformat("2026-03-01T08:31:00"))
    state_after_second_due = scheduler.export_state()

    assert first_due[0].ran is False
    assert first_due[0].detail == "already_running"
    assert state_after_first_due["last_run"] == {}
    assert second_due[0].ran is True
    assert second_due[0].detail == "ok"
    assert state_after_second_due["last_run"] == {"test_job": "2026-03-01"}
    assert attempts == ["called", "called"]


def test_scheduler_respects_weekday_filters_for_daily_and_interval_jobs() -> None:
    config = SchedulerConfig(
        enabled=True,
        premarket_time="08:30",
        auction_report_time="09:26",
        close_reconcile_time="15:30",
    )
    scheduler = DailyScheduler(config=config)
    daily_runs: list[str] = []
    interval_runs: list[str] = []

    scheduler.register(
        "weekday_daily",
        "08:30",
        callback=lambda: _record_run(daily_runs),
        weekdays=(0, 1, 2, 3, 4),
    )
    scheduler.register_interval(
        name="weekday_interval",
        window_start_hhmm="09:30",
        window_end_hhmm="09:32",
        interval_minutes=1,
        callback=lambda: _record_run(interval_runs),
        weekdays=(0, 1, 2, 3, 4),
    )

    weekend_daily = scheduler.run_due(datetime.fromisoformat("2026-03-01T08:30:00"))
    weekend_interval = scheduler.run_due(datetime.fromisoformat("2026-03-01T09:30:00"))
    weekday_daily = scheduler.run_due(datetime.fromisoformat("2026-03-02T08:30:00"))
    weekday_interval = scheduler.run_due(datetime.fromisoformat("2026-03-02T09:30:00"))

    assert weekend_daily[0].ran is False
    assert weekend_daily[0].detail == "not_scheduled_today"
    assert weekend_interval[1].ran is False
    assert weekend_interval[1].detail == "not_scheduled_today"
    assert weekday_daily[0].ran is True
    assert weekday_interval[1].ran is True
    assert daily_runs == ["ok"]
    assert interval_runs == ["ok"]


def test_scheduler_respects_date_predicate_for_daily_and_interval_jobs() -> None:
    config = SchedulerConfig(
        enabled=True,
        premarket_time="08:30",
        auction_report_time="09:26",
        close_reconcile_time="15:30",
    )
    scheduler = DailyScheduler(config=config)
    daily_runs: list[str] = []
    interval_runs: list[str] = []

    scheduler.register(
        "trading_daily",
        "08:30",
        callback=lambda: _record_run(daily_runs),
        weekdays=(0, 1, 2, 3, 4),
        date_predicate=lambda current: current.isoformat() != "2026-05-01",
    )
    scheduler.register_interval(
        name="trading_interval",
        window_start_hhmm="09:30",
        window_end_hhmm="09:32",
        interval_minutes=1,
        callback=lambda: _record_run(interval_runs),
        weekdays=(0, 1, 2, 3, 4),
        date_predicate=lambda current: current.isoformat() != "2026-05-01",
    )

    holiday_daily = scheduler.run_due(datetime.fromisoformat("2026-05-01T08:30:00"))
    holiday_interval = scheduler.run_due(datetime.fromisoformat("2026-05-01T09:30:00"))
    trading_daily = scheduler.run_due(datetime.fromisoformat("2026-05-06T08:30:00"))
    trading_interval = scheduler.run_due(datetime.fromisoformat("2026-05-06T09:30:00"))

    assert holiday_daily[0].ran is False
    assert holiday_daily[0].detail == "not_scheduled_today"
    assert holiday_interval[1].ran is False
    assert holiday_interval[1].detail == "not_scheduled_today"
    assert trading_daily[0].ran is True
    assert trading_interval[1].ran is True
    assert daily_runs == ["ok"]
    assert interval_runs == ["ok"]
