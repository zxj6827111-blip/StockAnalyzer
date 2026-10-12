from __future__ import annotations

import pandas as pd
import pytest

from scripts.collect_adj_factor_history import collect


class _Pro:
    def __init__(
        self,
        frames: dict[str, pd.DataFrame],
        days: list[str],
        open_flags: list[int] | None = None,
        market_frames: dict[str, pd.DataFrame] | None = None,
    ) -> None:
        self.frames = frames
        self.days = days
        self.open_flags = open_flags or [1] * len(days)
        self.market_frames = market_frames or {}
        self.calendar_kwargs = None
        self.adj_factor_calls = 0
        self.market_calls = 0

    def trade_cal(self, **kwargs):
        self.calendar_kwargs = kwargs
        return pd.DataFrame({"cal_date": self.days, "is_open": self.open_flags})

    def adj_factor(
        self,
        *,
        ts_code: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        trade_date: str | None = None,
    ):
        if trade_date is not None:
            self.market_calls += 1
            return self.market_frames[trade_date]
        assert ts_code and start_date and end_date
        self.adj_factor_calls += 1
        return self.frames[ts_code]


def _market_frame(day: str, *, include_600000: bool) -> pd.DataFrame:
    codes = ["000001.SZ"]
    if include_600000:
        codes.append("600000.SH")
    filler_count = 3000 - len(codes)
    codes.extend(f"{value:06d}.SZ" for value in range(1000, 1000 + filler_count))
    return pd.DataFrame({
        "ts_code": codes,
        "trade_date": [day] * len(codes),
        "adj_factor": [1.0] * len(codes),
    })


def test_adj_factor_collection_tracks_complete_and_missing_symbol_sessions() -> None:
    days = ["20260105", "20260106"]
    pro = _Pro(
        {
            "600000.SH": pd.DataFrame(
                {
                    "ts_code": ["600000.SH", "600000.SH"],
                    "trade_date": days,
                    "adj_factor": [1.0, 1.01],
                }
            ),
            "000001.SZ": pd.DataFrame(
                {
                    "ts_code": ["000001.SZ"],
                    "trade_date": [days[0]],
                    "adj_factor": [1.0],
                }
            ),
        },
        days,
    )
    factors, coverage, report = collect(
        pro,
        "20260105",
        "20260106",
        ["000001", "600000"],
        request_interval_sec=0,
    )
    assert len(factors) == 3
    assert coverage["coverage_complete"].tolist() == [False, True]
    # Returned observations remain valid even when another session is absent.
    assert factors["coverage_complete"].tolist() == [True, True, True]
    assert report["requested_symbols"] == 2
    assert report["completed_symbols"] == 1
    assert coverage["ts_code"].tolist() == ["000001.SZ", "600000.SH"]
    assert report["incomplete_symbols"]["000001"] == ["20260106"]
    assert report["coverage_complete"] is False
    assert "is_open" not in pro.calendar_kwargs
    assert report["trade_calendar"]["calendar_days"] == 2


def test_adj_factor_collection_fails_closed_when_required_columns_are_missing() -> None:
    days = ["20260105"]
    pro = _Pro({"600000.SH": pd.DataFrame({"ts_code": ["600000.SH"]})}, days)
    factors, coverage, report = collect(
        pro,
        days[0],
        days[0],
        ["600000.SH"],
        request_interval_sec=0,
    )
    assert factors.empty
    assert not bool(coverage.iloc[0]["coverage_complete"])
    assert "response_missing_columns" in coverage.iloc[0]["error"]
    assert report["coverage_complete"] is False


def test_adj_factor_collection_rejects_invalid_values_and_wrong_source_identity() -> None:
    day = "20260105"
    pro = _Pro(
        {
            "000001.SZ": pd.DataFrame(
                {
                    "ts_code": ["600000.SH"],
                    "trade_date": [day],
                    "adj_factor": [1.0],
                }
            ),
            "600000.SH": pd.DataFrame(
                {
                    "ts_code": ["600000.SH"],
                    "trade_date": [day],
                    "adj_factor": [0.0],
                }
            ),
        },
        [day],
    )

    factors, _coverage, report = collect(
        pro,
        day,
        day,
        ["000001", "600000"],
        request_interval_sec=0,
    )

    assert factors.empty
    assert report["coverage_complete"] is False
    assert report["symbols_with_unexpected_source_codes"] == {"000001": ["600000.SH"]}
    assert report["symbols_with_invalid_factor_values"] == {"600000": [day]}


def test_adj_factor_collection_rejects_incomplete_calendar_range() -> None:
    pro = _Pro({}, ["20260105", "20260107"])

    with pytest.raises(RuntimeError, match="calendar_has_missing_dates"):
        collect(pro, "20260105", "20260107", ["600000"], request_interval_sec=0)


def test_adj_factor_calendar_filters_closed_days_and_keeps_open_sessions() -> None:
    days = ["20260105", "20260106", "20260107"]
    pro = _Pro(
        {
            "600000.SH": pd.DataFrame(
                {
                    "ts_code": ["600000.SH", "600000.SH"],
                    "trade_date": [days[0], days[2]],
                    "adj_factor": [1.0, 1.01],
                }
            )
        },
        days,
        [1, 0, 1],
    )

    factors, coverage, report = collect(
        pro,
        days[0],
        days[2],
        ["600000"],
        request_interval_sec=0,
    )

    assert len(factors) == 2
    assert bool(coverage.iloc[0]["coverage_complete"])
    assert report["requested_sessions"] == 2
    assert report["trade_calendar"]["calendar_days"] == 3


def test_trade_date_market_batch_keeps_exact_symbol_coverage_and_per_day_times() -> None:
    days = ["20260105", "20260106"]
    pro = _Pro(
        {},
        days,
        market_frames={
            days[0]: _market_frame(days[0], include_600000=True),
            days[1]: _market_frame(days[1], include_600000=False),
        },
    )
    factors, coverage, report = collect(
        pro,
        days[0],
        days[-1],
        ["000001", "600000"],
        request_interval_sec=0,
        query_mode="trade_date_market_batch",
    )

    assert pro.market_calls == 2
    assert pro.adj_factor_calls == 0
    assert len(factors) == 3
    assert coverage["coverage_complete"].tolist() == [True, False]
    assert factors["coverage_complete"].all()
    assert report["query_mode"] == "trade_date_market_batch"
    assert report["requested_sessions"] == 2
    assert report["completed_symbols"] == 1
    assert report["incomplete_symbols"] == {"600000": [days[1]]}
    assert set(report["date_responses"]) == set(days)
    assert all(len(value["response_sha256"]) == 64 for value in report["date_responses"].values())
    as_of_by_date = {day: item["as_of"] for day, item in report["date_responses"].items()}
    assert factors.apply(
        lambda row: row["as_of"] == as_of_by_date[row["trade_date"]], axis=1
    ).all()


def test_trade_date_market_batch_resumes_hash_bound_day_checkpoints(tmp_path) -> None:
    day = "20260105"
    checkpoint_dir = tmp_path / "daily_checkpoints"
    market_frame = _market_frame(day, include_600000=True)
    first_pro = _Pro({}, [day], market_frames={day: market_frame})
    first_factors, _coverage, first_report = collect(
        first_pro,
        day,
        day,
        ["000001", "600000"],
        request_interval_sec=0,
        checkpoint_dir=checkpoint_dir,
        query_mode="trade_date_market_batch",
    )
    assert first_pro.market_calls == 1
    assert len(first_factors) == 2

    resumed_pro = _Pro({}, [day], market_frames={})
    resumed_factors, _coverage, resumed_report = collect(
        resumed_pro,
        day,
        day,
        ["000001", "600000"],
        request_interval_sec=0,
        checkpoint_dir=checkpoint_dir,
        query_mode="trade_date_market_batch",
    )
    assert resumed_pro.market_calls == 0
    assert len(resumed_factors) == len(first_factors)
    assert resumed_report["checkpoint"]["resumed_days"] == 1
    assert resumed_report["date_responses"] == first_report["date_responses"]


def test_trade_date_market_batch_retry_requeries_only_days_missing_requested_symbols(
    tmp_path,
) -> None:
    days = ["20260105", "20260106"]
    checkpoint_dir = tmp_path / "daily_retry_checkpoints"
    initial_pro = _Pro(
        {},
        days,
        market_frames={
            days[0]: _market_frame(days[0], include_600000=False),
            days[1]: _market_frame(days[1], include_600000=True),
        },
    )
    _first_factors, _first_coverage, first_report = collect(
        initial_pro,
        days[0],
        days[-1],
        ["000001", "600000"],
        request_interval_sec=0,
        checkpoint_dir=checkpoint_dir,
        query_mode="trade_date_market_batch",
    )
    assert first_report["date_responses"][days[0]]["missing_requested_symbols"] == ["600000"]

    retry_pro = _Pro(
        {},
        days,
        market_frames={
            days[0]: _market_frame(days[0], include_600000=True),
        },
    )
    _retry_factors, _retry_coverage, retry_report = collect(
        retry_pro,
        days[0],
        days[-1],
        ["000001", "600000"],
        request_interval_sec=0,
        checkpoint_dir=checkpoint_dir,
        retry_incomplete=True,
        query_mode="trade_date_market_batch",
    )

    assert retry_pro.market_calls == 1
    assert retry_report["checkpoint"]["queried_days"] == 1
    assert retry_report["checkpoint"]["resumed_days"] == 1
    assert retry_report["coverage_complete"] is True


def test_adj_factor_collection_resumes_checkpoints_and_retries_only_incomplete_symbols(
    tmp_path,
) -> None:
    days = ["20260105", "20260106"]
    complete = pd.DataFrame(
        {
            "ts_code": ["600000.SH", "600000.SH"],
            "trade_date": days,
            "adj_factor": [1.0, 1.01],
        }
    )
    incomplete = pd.DataFrame(
        {"ts_code": ["000001.SZ"], "trade_date": [days[0]], "adj_factor": [1.0]}
    )
    checkpoint_dir = tmp_path / "checkpoints"
    initial_pro = _Pro(
        {"000001.SZ": incomplete, "600000.SH": complete},
        days,
    )
    first_factors, _first_coverage, first_report = collect(
        initial_pro,
        days[0],
        days[1],
        ["000001", "600000"],
        request_interval_sec=0,
        checkpoint_dir=checkpoint_dir,
    )
    assert len(first_factors) == 3
    assert initial_pro.adj_factor_calls == 2
    assert first_report["coverage_complete"] is False

    resume_pro = _Pro(
        {"000001.SZ": incomplete, "600000.SH": complete},
        days,
    )
    resumed_factors, _resumed_coverage, resumed_report = collect(
        resume_pro,
        days[0],
        days[1],
        ["000001", "600000"],
        request_interval_sec=0,
        checkpoint_dir=checkpoint_dir,
    )
    assert resume_pro.adj_factor_calls == 0
    assert len(resumed_factors) == len(first_factors)
    assert resumed_report["checkpoint"]["resumed_symbols"] == 2
    assert resumed_report["coverage_complete"] is False

    retry_pro = _Pro(
        {
            "000001.SZ": pd.DataFrame(
                {
                    "ts_code": ["000001.SZ", "000001.SZ"],
                    "trade_date": days,
                    "adj_factor": [1.0, 1.02],
                }
            ),
            "600000.SH": complete,
        },
        days,
    )
    factors, coverage, report = collect(
        retry_pro,
        days[0],
        days[1],
        ["000001", "600000"],
        request_interval_sec=0,
        checkpoint_dir=checkpoint_dir,
        retry_incomplete=True,
    )
    assert retry_pro.adj_factor_calls == 1
    assert report["checkpoint"] == {
        "enabled": True,
        "resumed_symbols": 1,
        "queried_symbols": 1,
    }
    assert report["coverage_complete"] is True
    assert coverage["coverage_complete"].all()
    assert len(factors) == 4


def test_adj_factor_checkpoint_refuses_a_different_symbol_scope(tmp_path) -> None:
    days = ["20260105"]
    frame = pd.DataFrame(
        {"ts_code": ["600000.SH"], "trade_date": days, "adj_factor": [1.0]}
    )
    checkpoint_dir = tmp_path / "checkpoints"
    first_pro = _Pro({"600000.SH": frame}, days)
    collect(
        first_pro,
        days[0],
        days[0],
        ["600000"],
        request_interval_sec=0,
        checkpoint_dir=checkpoint_dir,
    )

    second_pro = _Pro({"000001.SZ": frame}, days)
    with pytest.raises(RuntimeError, match="checkpoint_scope_mismatch"):
        collect(
            second_pro,
            days[0],
            days[0],
            ["000001"],
            request_interval_sec=0,
            checkpoint_dir=checkpoint_dir,
        )
    assert second_pro.adj_factor_calls == 0


def test_adj_factor_retry_incomplete_requires_checkpointing() -> None:
    day = "20260105"
    pro = _Pro({}, [day])
    with pytest.raises(ValueError, match="retry_incomplete_requires_checkpoint"):
        collect(
            pro,
            day,
            day,
            ["600000"],
            request_interval_sec=0,
            retry_incomplete=True,
        )
