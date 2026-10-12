"""离线公司行动采集器的逐日覆盖与截断边界。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "collect_corporate_actions_history.py"
_SPEC = importlib.util.spec_from_file_location("collect_corporate_actions_history", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
collector = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(collector)


class _Pro:
    def __init__(self, frames):
        self.frames = frames

    def dividend(self, *, ex_date: str, fields: str):
        value = self.frames[ex_date]
        if isinstance(value, Exception):
            raise value
        return value


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=collector.FIELDS.split(","))


def _event_frame() -> pd.DataFrame:
    return pd.DataFrame([{
        "ts_code": "600000.SH", "end_date": "20251231", "ann_date": "20260301",
        "div_proc": "实施", "stk_div": 0.0, "stk_bo_rate": 0.0,
        "stk_co_rate": 0.0, "cash_div": 0.5, "cash_div_tax": 0.6,
        "record_date": "20260302", "ex_date": "20260303",
        "pay_date": "20260306", "div_listdate": "", "imp_ann_date": "20260301",
        "base_date": "", "base_share": None,
    }], columns=collector.FIELDS.split(","))


def test_zero_event_dates_still_have_complete_coverage_rows() -> None:
    events, coverage, summary = collector.collect(
        _Pro({"20260302": _empty(), "20260303": _event_frame()}),
        ["20260302", "20260303"], sleep=0, progress=False,
    )
    assert len(events) == 1
    assert events[0]["source"] == collector.SOURCE
    assert [row["ex_date"] for row in coverage] == ["20260302", "20260303"]
    assert all(row["coverage_complete"] for row in coverage)
    assert coverage[0]["rows_returned"] == 0
    assert summary["coverage_complete"] is True


def test_row_limit_and_api_failure_are_not_misreported_as_empty_complete_days() -> None:
    capped = pd.concat([_event_frame()] * collector.API_ROW_LIMIT, ignore_index=True)
    events, coverage, summary = collector.collect(
        _Pro({"20260302": capped,
              "20260303": RuntimeError("request failed with token=must-not-leak")}),
        ["20260302", "20260303"], sleep=0, progress=False,
    )
    assert len(events) == collector.API_ROW_LIMIT
    assert coverage[0]["truncated"] is True
    assert coverage[0]["coverage_complete"] is False
    assert coverage[1]["coverage_complete"] is False
    assert coverage[1]["error"] == "RuntimeError"
    assert "must-not-leak" not in str(coverage)
    assert summary["coverage_complete"] is False
    assert summary["completed_days"] == 0
