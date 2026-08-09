"""Monthly factor IC decay report (PRD acceptance #28).

Computes per-factor IC time series bucketed by month and derives decay
metrics: recent IC mean, linear IC slope over time, IC-time correlation,
relative decay rate and a health verdict.

Inputs are either raw factor records (alphalens-style rows, reusing the
rank-IC helper from alphalens_sidecar) or a precomputed daily IC series,
or pre-bucketed monthly IC values (e.g. from factor lifecycle history).
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from stock_analyzer.research.alphalens_sidecar import _infer_factor_columns, _rank_ic

_DEFAULT_RECENT_WINDOW_MONTHS = 3
_MIN_HORIZON = 1


def compute_factor_ic_series(
    *,
    records: Sequence[Mapping[str, object]],
    factor_columns: Sequence[str] | None = None,
    horizon: int = 5,
    date_col: str = "trade_date",
    symbol_col: str = "symbol",
    close_col: str = "close",
) -> pd.DataFrame:
    """Compute one rank-IC value per trading date per factor.

    Returns a DataFrame indexed by trading date with factor columns holding
    the cross-sectional rank IC between the factor and the forward ``horizon``
    day return. Empty when no usable data is present.
    """
    frame = pd.DataFrame(list(records))
    if frame.empty:
        return pd.DataFrame()
    if date_col not in frame.columns or symbol_col not in frame.columns:
        return pd.DataFrame()
    working = frame.copy()
    working[date_col] = pd.to_datetime(working[date_col], errors="coerce")
    working[close_col] = pd.to_numeric(working[close_col], errors="coerce")
    working[symbol_col] = working[symbol_col].astype(str).str.strip()
    working = working.dropna(subset=[date_col, close_col])
    working = working[working[symbol_col] != ""]
    if working.empty:
        return pd.DataFrame()

    factor_names = (
        list(factor_columns) if factor_columns is not None else _infer_factor_columns(working)
    )
    if not factor_names:
        return pd.DataFrame()
    cleaned_horizon = max(_MIN_HORIZON, int(horizon))
    working[f"_ret_{cleaned_horizon}"] = (
        working.groupby(symbol_col)[close_col].shift(-cleaned_horizon)
        / working[close_col]
        - 1.0
    )
    rows = working.dropna(subset=[f"_ret_{cleaned_horizon}"])
    if rows.empty:
        return pd.DataFrame()

    series_by_factor: dict[str, pd.Series] = {}
    for factor_name in factor_names:
        if factor_name not in working.columns or factor_name in {
            date_col,
            symbol_col,
            close_col,
            f"_ret_{cleaned_horizon}",
        }:
            continue
        factor_series = pd.to_numeric(working[factor_name], errors="coerce")
        usable = rows.assign(_factor=factor_series).dropna(subset=["_factor"])
        if usable.empty:
            continue
        daily_ics: list[tuple[pd.Timestamp, float]] = []
        for day, group in usable.groupby(date_col):
            if len(group) < 2:
                continue
            daily_ics.append(
                (
                    pd.Timestamp(str(day)),
                    _rank_ic(group["_factor"], group[f"_ret_{cleaned_horizon}"]),
                )
            )
        if daily_ics:
            series_by_factor[factor_name] = pd.Series(
                {day: value for day, value in daily_ics},
                dtype=float,
            )
    if not series_by_factor:
        return pd.DataFrame()
    result = pd.DataFrame(series_by_factor)
    result.index = pd.to_datetime(result.index)
    return result.sort_index()


def bucket_ic_series_by_month(
    ic_series: pd.DataFrame,
    lookback_months: int = 12,
) -> pd.DataFrame:
    """Bucket a daily IC series into monthly mean IC per factor.

    Returns a DataFrame indexed by ``YYYY-MM`` month labels with factor
    columns holding the mean IC of the month. Only the trailing
    ``lookback_months`` months are kept (all months when <= 0).
    """
    if ic_series is None or ic_series.empty:
        return pd.DataFrame()
    frame = ic_series.copy()
    frame.index = pd.to_datetime(frame.index)
    frame["_month"] = frame.index.strftime("%Y-%m")
    monthly = frame.groupby("_month", sort=True).mean(numeric_only=True)
    if lookback_months and lookback_months > 0 and len(monthly) > lookback_months:
        monthly = monthly.iloc[-lookback_months:]
    return monthly


def _month_days_wide(ic_series: pd.DataFrame) -> pd.DataFrame:
    frame = ic_series.copy()
    frame.index = pd.to_datetime(frame.index)
    frame["_month"] = frame.index.strftime("%Y-%m")
    return frame.groupby("_month", sort=True).count()


def compute_ic_decay_report(
    *,
    ic_series: pd.DataFrame | None = None,
    records: Sequence[Mapping[str, object]] | None = None,
    factor_columns: Sequence[str] | None = None,
    horizon: int = 5,
    lookback_months: int = 12,
    min_months: int = 3,
    healthy_threshold: float = 0.03,
    slope_threshold: float = -0.005,
) -> dict[str, object]:
    """Compute the monthly IC decay report.

    ``ic_series`` (date-indexed daily IC per factor) takes precedence; when
    missing, ``records`` are converted through :func:`compute_factor_ic_series`.
    """
    if ic_series is None:
        if not records:
            return _empty_report(
                status="invalid_input",
                horizon=horizon,
                lookback_months=lookback_months,
                min_months=min_months,
                healthy_threshold=healthy_threshold,
                slope_threshold=slope_threshold,
            )
        ic_series = compute_factor_ic_series(
            records=records,
            factor_columns=factor_columns,
            horizon=horizon,
        )
    if ic_series is None or ic_series.empty:
        return _empty_report(
            status="empty",
            horizon=horizon,
            lookback_months=lookback_months,
            min_months=min_months,
            healthy_threshold=healthy_threshold,
            slope_threshold=slope_threshold,
        )
    monthly = bucket_ic_series_by_month(ic_series, lookback_months=lookback_months)
    if monthly.empty:
        return _empty_report(
            status="empty",
            horizon=horizon,
            lookback_months=lookback_months,
            min_months=min_months,
            healthy_threshold=healthy_threshold,
            slope_threshold=slope_threshold,
        )
    days_wide = _month_days_wide(ic_series).reindex(index=monthly.index)
    factors: list[dict[str, object]] = []
    for factor_name in monthly.columns:
        months: list[str] = []
        ics: list[float] = []
        days: list[int] = []
        for month_label in monthly.index:
            raw_ic = monthly.at[month_label, factor_name]
            if raw_ic != raw_ic:
                continue
            months.append(str(month_label))
            ics.append(_as_float(raw_ic))
            raw_days = days_wide.at[month_label, factor_name]
            raw_days_value = _as_float(raw_days)
            days.append(int(raw_days_value) if raw_days_value == raw_days_value else 0)
        if not months:
            continue
        factors.append(
            _build_factor_item(
                factor_name,
                months=months,
                ics=ics,
                days=days,
                min_months=min_months,
                healthy_threshold=healthy_threshold,
                slope_threshold=slope_threshold,
            )
        )
    factors.sort(
        key=lambda item: (
            item.get("health") == "decaying",
            -abs(_as_float(item.get("recent_ic_mean"))),
            str(item.get("factor", "")),
        )
    )
    healthy = sum(1 for item in factors if item.get("health") == "healthy")
    decaying = sum(1 for item in factors if item.get("health") == "decaying")
    insufficient = sum(1 for item in factors if item.get("health") == "insufficient_data")
    report = _empty_report(
        status="ok" if healthy + decaying > 0 else "insufficient_data",
        horizon=horizon,
        lookback_months=lookback_months,
        min_months=min_months,
        healthy_threshold=healthy_threshold,
        slope_threshold=slope_threshold,
    )
    report["months"] = [str(month_label) for month_label in monthly.index]
    report["factor_count"] = len(factors)
    report["factors"] = factors
    report["summary"] = {
        "factors_total": len(factors),
        "factors_healthy": healthy,
        "factors_decaying": decaying,
        "factors_insufficient": insufficient,
        "decaying_factors": [
            str(item["factor"]) for item in factors if item.get("health") == "decaying"
        ],
    }
    return report


def compute_ic_decay_from_monthly_ics(
    *,
    monthly_ics: Mapping[str, Sequence[Mapping[str, object]]],
    lookback_months: int = 12,
    min_months: int = 3,
    healthy_threshold: float = 0.03,
    slope_threshold: float = -0.005,
) -> dict[str, object]:
    """Compute the monthly IC decay report from pre-bucketed monthly IC values.

    ``monthly_ics`` maps a factor name to records like
    ``[{"month": "2026-01", "ic": 0.04}, ...]``. Entries without a valid
    month or IC value are skipped.
    """
    factors: list[dict[str, object]] = []
    for factor_name, raw_points in monthly_ics.items():
        name = str(factor_name).strip()
        if not name:
            continue
        points: dict[str, float] = {}
        for raw_point in raw_points:
            if not isinstance(raw_point, Mapping):
                continue
            month_label = _normalize_month(str(raw_point.get("month", "")))
            if not month_label:
                continue
            raw_ic = raw_point.get("ic")
            if raw_ic is None:
                continue
            ic = _as_float(raw_ic)
            if ic != ic:
                continue
            points[month_label] = ic
        if not points:
            continue
        ordered = sorted(points.items(), key=lambda item: item[0])
        months = [month_label for month_label, _ in ordered]
        ics = [ic for _, ic in ordered]
        if lookback_months and lookback_months > 0 and len(months) > lookback_months:
            months = months[-lookback_months:]
            ics = ics[-lookback_months:]
        factors.append(
            _build_factor_item(
                name,
                months=months,
                ics=ics,
                days=None,
                min_months=min_months,
                healthy_threshold=healthy_threshold,
                slope_threshold=slope_threshold,
            )
        )
    if not factors:
        return _empty_report(
            status="empty",
            horizon=0,
            lookback_months=lookback_months,
            min_months=min_months,
            healthy_threshold=healthy_threshold,
            slope_threshold=slope_threshold,
        )
    factors.sort(
        key=lambda item: (
            item.get("health") == "decaying",
            -abs(_as_float(item.get("recent_ic_mean"))),
            str(item.get("factor", "")),
        )
    )
    healthy = sum(1 for item in factors if item.get("health") == "healthy")
    decaying = sum(1 for item in factors if item.get("health") == "decaying")
    insufficient = sum(1 for item in factors if item.get("health") == "insufficient_data")
    all_months: set[str] = set()
    for item in factors:
        factor_monthly_points = item.get("monthly_ics")
        if not isinstance(factor_monthly_points, list):
            continue
        for point in factor_monthly_points:
            if not isinstance(point, Mapping):
                continue
            month_label = str(point.get("month", "")).strip()
            if month_label:
                all_months.add(month_label)
    report = _empty_report(
        status="ok" if healthy + decaying > 0 else "insufficient_data",
        horizon=0,
        lookback_months=lookback_months,
        min_months=min_months,
        healthy_threshold=healthy_threshold,
        slope_threshold=slope_threshold,
    )
    report["months"] = sorted(all_months)
    report["factor_count"] = len(factors)
    report["factors"] = factors
    report["summary"] = {
        "factors_total": len(factors),
        "factors_healthy": healthy,
        "factors_decaying": decaying,
        "factors_insufficient": insufficient,
        "decaying_factors": [
            str(item["factor"]) for item in factors if item.get("health") == "decaying"
        ],
    }
    return report


def persist_ic_decay_report(*, report: Mapping[str, object], output_path: str | Path) -> str:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _build_factor_item(
    factor_name: str,
    *,
    months: list[str],
    ics: list[float],
    days: list[int] | None,
    min_months: int,
    healthy_threshold: float,
    slope_threshold: float,
) -> dict[str, object]:
    month_points: list[dict[str, object]] = []
    for index, month_label in enumerate(months):
        point: dict[str, object] = {"month": month_label, "ic": round(ics[index], 6)}
        if days is not None:
            point["days"] = days[index]
        month_points.append(point)
    item: dict[str, object] = {
        "factor": factor_name,
        "months_used": len(months),
        "monthly_ics": month_points,
    }
    n = len(ics)
    if n == 0:
        item["health"] = "insufficient_data"
        return item
    if n < max(1, int(min_months)):
        item["health"] = "insufficient_data"
        return item
    recent_window = min(_DEFAULT_RECENT_WINDOW_MONTHS, n)
    earliest_window = min(_DEFAULT_RECENT_WINDOW_MONTHS, n)
    recent_ic_mean = sum(ics[-recent_window:]) / recent_window
    earliest_ic_mean = sum(ics[:earliest_window]) / earliest_window
    slope = _linear_slope(ics)
    time_corr = _pearson_corr(ics)
    baseline = max(abs(earliest_ic_mean), 1e-9)
    decay_rate = (recent_ic_mean - earliest_ic_mean) / baseline
    ic_mean_breach = recent_ic_mean < healthy_threshold
    slope_breach = slope < slope_threshold
    health = "decaying" if (ic_mean_breach or slope_breach) else "healthy"
    if ic_mean_breach and slope_breach:
        reason = "recent_ic_mean_below_threshold_and_slope_below_threshold"
    elif ic_mean_breach:
        reason = "recent_ic_mean_below_threshold"
    elif slope_breach:
        reason = "slope_below_threshold"
    else:
        reason = ""
    item.update(
        {
            "recent_ic_mean": round(recent_ic_mean, 6),
            "recent_n": recent_window,
            "earliest_ic_mean": round(earliest_ic_mean, 6),
            "ic_slope": round(slope, 6),
            "ic_time_corr": round(time_corr, 6),
            "decay_rate": round(decay_rate, 6),
            "health": health,
            "ic_mean_breach": ic_mean_breach,
            "slope_breach": slope_breach,
            "reason": reason,
        }
    )
    return item


def _linear_slope(values: Sequence[float]) -> float:
    finite = [float(value) for value in values if value == value]
    if len(finite) < 2:
        return 0.0
    try:
        coeffs = np.polyfit(np.arange(len(finite), dtype=float), np.asarray(finite, dtype=float), 1)
        slope = float(coeffs[0])
    except (TypeError, ValueError, np.linalg.LinAlgError):
        return 0.0
    return slope if slope == slope else 0.0


def _pearson_corr(values: Sequence[float]) -> float:
    finite = [float(value) for value in values if value == value]
    if len(finite) < 2:
        return 0.0
    left = pd.Series(finite, dtype=float)
    right = pd.Series(np.arange(len(finite), dtype=float), dtype=float)
    if left.std(ddof=0) == 0 or right.std(ddof=0) == 0:
        return 0.0
    corr = left.corr(right)
    return float(corr) if corr == corr else 0.0


def _normalize_month(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    for fmt in ("%Y-%m", "%Y-%m-%d", "%Y%m"):
        try:
            return datetime.strptime(text[:10], fmt).strftime("%Y-%m")
        except ValueError:
            continue
    return ""


def _empty_report(
    *,
    status: str,
    horizon: int,
    lookback_months: int,
    min_months: int,
    healthy_threshold: float,
    slope_threshold: float,
) -> dict[str, object]:
    return {
        "status": status,
        "engine": "ic_decay_report",
        "horizon": horizon,
        "lookback_months": lookback_months,
        "min_months": min_months,
        "healthy_threshold": healthy_threshold,
        "slope_threshold": slope_threshold,
        "months": [],
        "factor_count": 0,
        "factors": [],
        "summary": {
            "factors_total": 0,
            "factors_healthy": 0,
            "factors_decaying": 0,
            "factors_insufficient": 0,
            "decaying_factors": [],
        },
    }


def _as_float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return math.nan
        try:
            return float(text)
        except ValueError:
            return math.nan
    return math.nan
