from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

import pandas as pd

from stock_analyzer.time_semantics import apply_time_invariants_to_frame


def _as_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def test_time_invariants_filter_invalid_rows() -> None:
    index = pd.bdate_range("2026-03-01", periods=5)
    frame = pd.DataFrame(
        {
            "event_time": [
                "2026-03-01T09:30:00+08:00",
                "2026-03-02T09:30:00+08:00",
                "2026-03-03T09:30:00+08:00",
                "2026-03-04T09:30:00+08:00",
                "2026-03-05T09:30:00+08:00",
            ],
            "available_time": [
                "2026-03-01T10:00:00+08:00",
                "2026-03-02T09:00:00+08:00",
                "2030-03-03T09:30:00+08:00",
                "2026-03-04T10:00:00+08:00",
                "2026-03-05T10:00:00+08:00",
            ],
            "label_anchor_time": [
                "2026-03-01T09:30:00+08:00",
                "2026-03-02T09:30:00+08:00",
                "2026-03-03T09:30:00+08:00",
                "2026-03-04T09:30:00+08:00",
                "2026-03-05T09:30:00+08:00",
            ],
            "label_mature_time": [
                "2026-03-07T09:30:00+08:00",
                "2026-03-08T09:30:00+08:00",
                "2026-03-09T09:30:00+08:00",
                "2026-03-04T09:31:00+08:00",
                "2026-04-01T09:30:00+08:00",
            ],
            "x": [1, 2, 3, 4, 5],
        },
        index=index,
    )
    filtered, info = apply_time_invariants_to_frame(
        frame,
        decision_time=datetime.fromisoformat("2026-03-20T10:00:00"),
        holding_horizon_days=5,
        settlement_lag_days=1,
        require_mature_label=True,
    )
    assert filtered["x"].tolist() == [1]
    assert info["total_rows"] == 5
    assert info["kept_rows"] == 1
    assert info["dropped_rows"] == 4
    violations = _as_mapping(info["violations"])
    assert violations["invalid_event_available_order"] == 1
    assert violations["invalid_decision_order"] == 1
    assert violations["invalid_label_maturity_formula"] == 1
    assert violations["label_not_matured"] == 1


def test_time_invariants_can_skip_label_maturity_check() -> None:
    index = pd.bdate_range("2026-03-01", periods=2)
    frame = pd.DataFrame(
        {
            "event_time": [
                "2026-03-01T09:30:00+08:00",
                "2026-03-02T09:30:00+08:00",
            ],
            "available_time": [
                "2026-03-01T10:00:00+08:00",
                "2026-03-02T10:00:00+08:00",
            ],
            "label_anchor_time": [
                "2026-03-01T09:30:00+08:00",
                "2026-03-02T09:30:00+08:00",
            ],
            "label_mature_time": [
                "2030-01-01T09:30:00+08:00",
                "2030-01-02T09:30:00+08:00",
            ],
            "x": [10, 11],
        },
        index=index,
    )
    filtered, info = apply_time_invariants_to_frame(
        frame,
        decision_time=datetime.fromisoformat("2026-03-20T10:00:00"),
        holding_horizon_days=5,
        settlement_lag_days=1,
        require_mature_label=False,
    )
    assert filtered["x"].tolist() == [10, 11]
    violations = _as_mapping(info["violations"])
    assert violations["label_not_matured"] == 0


def test_time_invariants_use_date_level_from_multiindex() -> None:
    symbols = ["600000", "600000", "000001", "000001"]
    dates = pd.to_datetime(["2026-03-03", "2026-03-04", "2026-03-03", "2026-03-04"])
    index = pd.MultiIndex.from_arrays([symbols, dates], names=["symbol", "date"])
    frame = pd.DataFrame(
        {
            "x": [1, 2, 3, 4],
        },
        index=index,
    )
    filtered, info = apply_time_invariants_to_frame(
        frame,
        decision_time=datetime.fromisoformat("2026-03-20T10:00:00"),
        holding_horizon_days=5,
        settlement_lag_days=1,
        require_mature_label=True,
    )
    assert filtered["x"].tolist() == [1, 2, 3, 4]
    assert info["kept_rows"] == 4
    violations = _as_mapping(info["violations"])
    assert violations["missing_time_fields"] == 0
