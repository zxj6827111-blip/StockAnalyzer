from __future__ import annotations

from datetime import date

import pandas as pd

from scripts.collect_tail_trade_references import collect, read_explicit_windows


class _Pro:
    def __init__(self, *, daily: pd.DataFrame, limits: pd.DataFrame, suspensions: pd.DataFrame):
        self.daily_frame = daily
        self.limit_frame = limits
        self.suspension_frame = suspensions
        self.calendar_frame = pd.DataFrame(
            {
                "cal_date": ["20260105", "20260106", "20260107"],
                "is_open": [1, 1, 1],
            }
        )
        self.calendar_kwargs = None
        self.daily_kwargs = []

    def trade_cal(self, **kwargs):
        self.calendar_kwargs = kwargs
        return self.calendar_frame

    def daily(self, **kwargs):
        self.daily_kwargs.append(kwargs)
        trade_date = kwargs.get("trade_date")
        if trade_date:
            return self.daily_frame.loc[
                self.daily_frame["trade_date"].eq(trade_date)
            ].reset_index(drop=True)
        return self.daily_frame

    def stk_limit(self, **_kwargs):
        return self.limit_frame

    def suspend_d(self, **_kwargs):
        return self.suspension_frame


def _pro(*, include_suspended_daily: bool = False, conflict: bool = False) -> _Pro:
    days = ["20260105", "20260106", "20260107"]
    daily_days = [days[0], days[2]]
    volumes = [1000, 2000]
    if include_suspended_daily:
        daily_days.insert(1, days[1])
        volumes.insert(1, 0)
    if conflict:
        daily_days.insert(1, days[1])
        volumes.insert(1, 100)
    daily = pd.DataFrame(
        {
            "ts_code": ["600000.SH"] * len(daily_days),
            "trade_date": daily_days,
            "open": [10.0] * len(daily_days),
            "high": [10.2] * len(daily_days),
            "low": [9.8] * len(daily_days),
            "close": [10.1] * len(daily_days),
            "vol": volumes,
        }
    )
    limits = pd.DataFrame(
        {
            "ts_code": ["600000.SH"] * len(days),
            "trade_date": days,
            "up_limit": [11.0] * len(days),
            "down_limit": [9.0] * len(days),
        }
    )
    suspensions = pd.DataFrame(
        {
            "ts_code": ["600000.SH"],
            "trade_date": [days[1]],
            "suspend_type": ["S"],
            "suspend_timing": [float("nan")],
        }
    )
    return _Pro(daily=daily, limits=limits, suspensions=suspensions)


def test_collection_accepts_positive_volume_trading_and_explicit_suspension_only() -> None:
    pro = _pro()
    daily, limits, statuses, report = collect(
        pro,
        {"600000": (date(2026, 1, 5), date(2026, 1, 7))},
        delay=0,
        progress=False,
    )

    assert [row["date"] for row in daily] == ["2026-01-05", "2026-01-07"]
    assert len(limits) == 3
    assert [(row["trade_date"], row["trade_status"]) for row in statuses] == [
        ("2026-01-05", "trading"),
        ("2026-01-06", "suspended"),
        ("2026-01-07", "trading"),
    ]
    assert report["coverage_complete"] is True
    assert "is_open" not in pro.calendar_kwargs
    assert pro.daily_kwargs == [
        {"trade_date": "20260105"},
        {"trade_date": "20260106"},
        {"trade_date": "20260107"},
    ]
    assert report["daily_query_mode"] == "market_wide_by_trade_date"
    assert report["trade_calendar"]["calendar_days"] == 3


def test_full_day_suspension_is_not_overridden_by_zero_volume_daily_bar() -> None:
    daily, _limits, statuses, report = collect(
        _pro(include_suspended_daily=True),
        {"600000": (date(2026, 1, 5), date(2026, 1, 7))},
        delay=0,
        progress=False,
    )

    assert len(daily) == 3
    assert [row["trade_date"] for row in statuses] == ["2026-01-05", "2026-01-06", "2026-01-07"]
    assert statuses[1]["trade_status"] == "suspended"
    assert statuses[1]["suspend_events_json"] == '[{"suspend_timing":null,"suspend_type":"S"}]'
    # The full-day suspension event proves the status; a zero-volume daily row does not
    # prove trading and does not conflict with that event.
    assert report["symbols"][0]["status_conflict_days"] == []
    assert report["coverage_complete"] is True


def test_collection_conflict_blocks_status_and_complete_coverage() -> None:
    _daily, _limits, statuses, report = collect(
        _pro(conflict=True),
        {"600000": (date(2026, 1, 5), date(2026, 1, 7))},
        delay=0,
        progress=False,
    )

    assert {row["trade_date"] for row in statuses} == {"2026-01-05", "2026-01-07"}
    assert report["symbols"][0]["status_conflict_days"] == ["2026-01-06"]
    assert report["coverage_complete"] is False


def test_resume_event_without_positive_volume_daily_bar_remains_unknown() -> None:
    pro = _pro()
    pro.suspension_frame.loc[:, "suspend_type"] = "R"
    _daily, _limits, statuses, report = collect(
        pro,
        {"600000": (date(2026, 1, 5), date(2026, 1, 7))},
        delay=0,
        progress=False,
    )

    assert {row["trade_date"] for row in statuses} == {"2026-01-05", "2026-01-07"}
    assert report["symbols"][0]["unknown_status_days"] == ["2026-01-06"]
    assert report["coverage_complete"] is False


def test_intraday_suspension_with_positive_volume_proves_trading() -> None:
    pro = _pro()
    pro.daily_frame.loc[len(pro.daily_frame)] = {
        "ts_code": "600000.SH",
        "trade_date": "20260106",
        "open": 10.0,
        "high": 10.2,
        "low": 9.8,
        "close": 10.1,
        "vol": 100,
    }
    pro.suspension_frame["suspend_timing"] = ["09:30-10:00"]
    _daily, _limits, statuses, report = collect(
        pro,
        {"600000": (date(2026, 1, 5), date(2026, 1, 7))},
        delay=0,
        progress=False,
    )

    status = next(row for row in statuses if row["trade_date"] == "2026-01-06")
    assert status["trade_status"] == "trading"
    assert status["suspend_events_json"] == '[{"suspend_timing":"09:30-10:00","suspend_type":"S"}]'
    assert report["coverage_complete"] is True


def test_mixed_suspend_and_resume_events_without_daily_trade_remain_unknown() -> None:
    pro = _pro()
    pro.suspension_frame = pd.DataFrame(
        {
            "ts_code": ["600000.SH", "600000.SH"],
            "trade_date": ["20260106", "20260106"],
            "suspend_type": ["S", "R"],
            "suspend_timing": [None, None],
        }
    )
    _daily, _limits, statuses, report = collect(
        pro,
        {"600000": (date(2026, 1, 5), date(2026, 1, 7))},
        delay=0,
        progress=False,
    )

    assert {row["trade_date"] for row in statuses} == {"2026-01-05", "2026-01-07"}
    assert report["symbols"][0]["unknown_status_days"] == ["2026-01-06"]
    assert report["coverage_complete"] is False


def test_market_wide_daily_ignores_other_symbols_and_does_not_imply_target_trading() -> None:
    pro = _pro()
    pro.daily_frame.loc[0, "ts_code"] = "000001.SZ"
    _daily, _limits, _statuses, report = collect(
        pro,
        {"600000": (date(2026, 1, 5), date(2026, 1, 7))},
        delay=0,
        progress=False,
    )

    symbol_report = report["symbols"][0]
    assert symbol_report["unknown_status_days"] == ["2026-01-05"]
    assert report["coverage_complete"] is False


def test_collection_rejects_calendar_response_with_missing_calendar_dates() -> None:
    pro = _pro()
    pro.calendar_frame = pro.calendar_frame.iloc[[0, 2]].reset_index(drop=True)

    try:
        collect(
            pro,
            {"600000": (date(2026, 1, 5), date(2026, 1, 7))},
            delay=0,
            progress=False,
        )
    except RuntimeError as exc:
        assert str(exc) == "calendar_empty_or_malformed"
    else:
        raise AssertionError("incomplete calendar must fail closed")


def test_collection_uses_only_open_days_from_full_calendar_response() -> None:
    pro = _pro()
    pro.calendar_frame.loc[1, "is_open"] = 0
    pro.daily_frame = pro.daily_frame[pro.daily_frame["trade_date"] != "20260106"]
    pro.limit_frame = pro.limit_frame[pro.limit_frame["trade_date"] != "20260106"]
    pro.suspension_frame = pro.suspension_frame.iloc[0:0]

    daily, limits, statuses, report = collect(
        pro,
        {"600000": (date(2026, 1, 5), date(2026, 1, 7))},
        delay=0,
        progress=False,
    )

    assert [row["date"] for row in daily] == ["2026-01-05", "2026-01-07"]
    assert len(limits) == 2
    assert [row["trade_date"] for row in statuses] == ["2026-01-05", "2026-01-07"]
    assert report["coverage_complete"] is True


def test_explicit_windows_preserve_locally_fixed_dates_and_leading_zeroes(tmp_path) -> None:
    path = tmp_path / "windows.jsonl"
    path.write_text(
        '{"symbol":"000001","start":"2026-01-05","end":"2026-01-07"}\n',
        encoding="utf-8",
    )

    windows, sha = read_explicit_windows(path)

    assert windows == {"000001": (date(2026, 1, 5), date(2026, 1, 7))}
    assert len(sha) == 64
