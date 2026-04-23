"""Time semantics and invariant validation helpers."""

from __future__ import annotations

from datetime import datetime

import pandas as pd


def apply_time_invariants_to_frame(
    frame: pd.DataFrame,
    *,
    decision_time: datetime,
    timezone: str = "Asia/Shanghai",
    holding_horizon_days: int = 5,
    settlement_lag_days: int = 1,
    require_mature_label: bool = False,
) -> tuple[pd.DataFrame, dict[str, object]]:
    """Filter rows that violate four-timestamp invariants.

    Invariants:
    1. ``available_time >= event_time``
    2. ``decision_time > available_time``
    3. ``label_mature_time >= label_anchor_time + holding_horizon + settlement_lag``
    4. If ``require_mature_label`` is true, ``decision_time >= label_mature_time``
    """
    if frame.empty:
        return frame.copy(), {
            "decision_time": _to_tz_timestamp(decision_time, timezone=timezone).isoformat(),
            "total_rows": 0,
            "kept_rows": 0,
            "dropped_rows": 0,
            "violations": {
                "invalid_event_available_order": 0,
                "invalid_decision_order": 0,
                "invalid_label_maturity_formula": 0,
                "label_not_matured": 0,
                "missing_time_fields": 0,
            },
        }

    event_time = _column_or_index_time(frame=frame, column="event_time", timezone=timezone)
    available_time = _column_or_fallback_time(
        frame=frame,
        column="available_time",
        fallback=event_time,
        timezone=timezone,
    )
    label_anchor_time = _column_or_fallback_time(
        frame=frame,
        column="label_anchor_time",
        fallback=event_time,
        timezone=timezone,
    )
    maturity_offset_days = max(0, int(holding_horizon_days)) + max(0, int(settlement_lag_days))
    expected_label_mature = label_anchor_time + pd.to_timedelta(maturity_offset_days, unit="D")
    label_mature_time = _column_or_fallback_time(
        frame=frame,
        column="label_mature_time",
        fallback=expected_label_mature,
        timezone=timezone,
    )

    decision_ts = _to_tz_timestamp(decision_time, timezone=timezone)
    missing_time_fields = (
        event_time.isna()
        | available_time.isna()
        | label_anchor_time.isna()
        | label_mature_time.isna()
    )
    invalid_event_available_order = available_time < event_time
    invalid_decision_order = decision_ts <= available_time
    invalid_label_maturity_formula = label_mature_time < expected_label_mature
    label_not_matured = decision_ts < label_mature_time if require_mature_label else pd.Series(
        [False] * len(frame), index=frame.index
    )
    valid_mask = ~(
        missing_time_fields
        | invalid_event_available_order
        | invalid_decision_order
        | invalid_label_maturity_formula
        | label_not_matured
    )

    filtered = frame.loc[valid_mask].copy()
    return filtered, {
        "decision_time": decision_ts.isoformat(),
        "total_rows": int(len(frame)),
        "kept_rows": int(valid_mask.sum()),
        "dropped_rows": int((~valid_mask).sum()),
        "violations": {
            "invalid_event_available_order": int(invalid_event_available_order.sum()),
            "invalid_decision_order": int(invalid_decision_order.sum()),
            "invalid_label_maturity_formula": int(invalid_label_maturity_formula.sum()),
            "label_not_matured": int(label_not_matured.sum()),
            "missing_time_fields": int(missing_time_fields.sum()),
        },
    }


def _column_or_index_time(frame: pd.DataFrame, column: str, *, timezone: str) -> pd.Series:
    if column in frame.columns:
        return _normalize_time_series(
            pd.to_datetime(frame[column], errors="coerce"),
            timezone=timezone,
        )
    if isinstance(frame.index, pd.MultiIndex):
        level_name = _multi_index_time_level(frame.index)
        index_values = frame.index.get_level_values(level_name)
        index_series = pd.Series(pd.to_datetime(index_values, errors="coerce"), index=frame.index)
        return _normalize_time_series(index_series, timezone=timezone)
    index_series = pd.Series(pd.to_datetime(frame.index, errors="coerce"), index=frame.index)
    return _normalize_time_series(index_series, timezone=timezone)


def _column_or_fallback_time(
    frame: pd.DataFrame,
    column: str,
    fallback: pd.Series,
    *,
    timezone: str,
) -> pd.Series:
    if column in frame.columns:
        parsed = pd.to_datetime(frame[column], errors="coerce")
        normalized = _normalize_time_series(parsed, timezone=timezone)
        return normalized.fillna(fallback)
    return fallback.copy()


def _normalize_time_series(series: pd.Series, *, timezone: str) -> pd.Series:
    if not isinstance(series, pd.Series):
        series = pd.Series(series)
    if not isinstance(series.index, pd.Index):
        series.index = pd.RangeIndex(start=0, stop=len(series))
    if series.dt.tz is None:
        localized = series.dt.tz_localize(timezone, ambiguous="NaT", nonexistent="shift_forward")
        return pd.Series(localized, index=series.index)
    converted = series.dt.tz_convert(timezone)
    return pd.Series(converted, index=series.index)


def _to_tz_timestamp(value: datetime, *, timezone: str) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize(timezone)
    return ts.tz_convert(timezone)


def _multi_index_time_level(index: pd.MultiIndex) -> int | str:
    preferred_names = {"date", "datetime", "trade_date", "event_time"}
    for name in index.names:
        if isinstance(name, str) and name.lower() in preferred_names:
            return name
    return len(index.levels) - 1
