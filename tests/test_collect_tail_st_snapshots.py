from __future__ import annotations

import json

import pandas as pd
import pytest

from scripts.collect_tail_st_snapshots import CollectionError, collect


class _Pro:
    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.frames = frames
        self.calls: list[str] = []

    def stock_st(self, *, trade_date: str) -> pd.DataFrame:
        self.calls.append(trade_date)
        return self.frames[trade_date]


def _inputs(tmp_path, days: list[str]):
    requests = tmp_path / "requests.jsonl"
    requests.write_text(
        "".join(
            json.dumps({
                "symbol": "600000",
                "ts_code": "600000.SH",
                "decision_date": f"{day[:4]}-{day[4:6]}-{day[6:]}",
            }) + "\n"
            for day in days
        ),
        encoding="utf-8",
    )
    calendar = tmp_path / "calendar.csv"
    pd.DataFrame({"trade_date": days, "is_open": [1] * len(days)}).to_csv(
        calendar, index=False
    )
    return requests, calendar


def _frame(day: str, rows: list[tuple[str, str, str]]):
    return pd.DataFrame(
        [
            {
                "ts_code": code,
                "name": name,
                "trade_date": day,
                "type": kind,
                "type_name": "风险警示板",
            }
            for code, name, kind in rows
        ],
        columns=["ts_code", "name", "trade_date", "type", "type_name"],
    )


def test_collects_complete_daily_st_lists_and_hashes_each_source_response(tmp_path) -> None:
    days = ["20260302", "20260303"]
    requests, calendar = _inputs(tmp_path, days)
    pro = _Pro({
        days[0]: _frame(days[0], [("600000.SH", "ST浦发", "ST")]),
        days[1]: _frame(days[1], []),
    })
    out = tmp_path / "output"

    report = collect(
        pro,
        requests_path=requests,
        calendar_path=calendar,
        out_dir=out,
        request_interval_sec=0,
    )

    assert pro.calls == days
    assert report["coverage_complete"] is True
    assert report["complete_days"] == 2
    assert report["st_rows"] == 1
    assert report["date_responses"][days[0]]["rows_returned"] == 1
    assert len(report["date_responses"][days[0]]["response_sha256"]) == 64
    assert pd.read_csv(out / "st_snapshots.csv").iloc[0]["symbol"] == 600000


def test_rejects_decision_date_missing_from_open_calendar(tmp_path) -> None:
    requests, calendar = _inputs(tmp_path, ["20260302"])
    pd.DataFrame({"trade_date": ["20260302"], "is_open": [0]}).to_csv(
        calendar, index=False
    )

    with pytest.raises(CollectionError, match="decision_date_missing_from_open_calendar"):
        collect(
            _Pro({}),
            requests_path=requests,
            calendar_path=calendar,
            out_dir=tmp_path / "output",
            request_interval_sec=0,
        )


def test_accepts_iso_dates_in_calendar_csv(tmp_path) -> None:
    day = "20260302"
    requests, calendar = _inputs(tmp_path, [day])
    pd.DataFrame({"trade_date": ["2026-03-02"], "is_open": [True]}).to_csv(
        calendar, index=False
    )
    report = collect(
        _Pro({day: _frame(day, [])}),
        requests_path=requests,
        calendar_path=calendar,
        out_dir=tmp_path / "output",
        request_interval_sec=0,
    )

    assert report["coverage_complete"] is True
    assert report["decision_days"] == [day]


def test_duplicate_symbols_fail_the_specific_date_closed(tmp_path) -> None:
    day = "20260302"
    requests, calendar = _inputs(tmp_path, [day])
    pro = _Pro({day: _frame(day, [
        ("600000.SH", "ST浦发", "ST"),
        ("600000.SH", "ST浦发", "ST"),
    ])})

    report = collect(
        pro,
        requests_path=requests,
        calendar_path=calendar,
        out_dir=tmp_path / "output",
        request_interval_sec=0,
    )

    assert report["coverage_complete"] is False
    assert report["failed_days"] == {day: "response_contains_duplicate_symbol"}
    assert report["st_rows"] == 0


def test_checkpoint_resume_reuses_hash_verified_successful_day(tmp_path) -> None:
    day = "20260302"
    requests, calendar = _inputs(tmp_path, [day])
    checkpoints = tmp_path / "checkpoints"
    first_pro = _Pro({day: _frame(day, [("600000.SH", "ST浦发", "ST")])})
    first_report = collect(
        first_pro,
        requests_path=requests,
        calendar_path=calendar,
        out_dir=tmp_path / "first",
        request_interval_sec=0,
        checkpoint_dir=checkpoints,
    )
    resumed_pro = _Pro({})
    resumed_report = collect(
        resumed_pro,
        requests_path=requests,
        calendar_path=calendar,
        out_dir=tmp_path / "resumed",
        request_interval_sec=0,
        checkpoint_dir=checkpoints,
    )

    assert first_pro.calls == [day]
    assert resumed_pro.calls == []
    assert resumed_report["checkpoint"]["resumed_days"] == 1
    assert resumed_report["date_responses"] == first_report["date_responses"]
