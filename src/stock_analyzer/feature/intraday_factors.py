"""Reusable intraday-factor extraction from minute panels."""

from __future__ import annotations

import math
from typing import SupportsFloat

import pandas as pd


def summarize_intraday_factors(
    frame: pd.DataFrame,
    *,
    interval: str | int,
) -> pd.DataFrame:
    """Convert minute bars into daily intraday summary factors.

    The output stays at daily frequency so runtime and training can keep
    consuming pre-computed summaries instead of raw minute panels.
    """
    prepared = _prepare_minute_frame(frame)
    if prepared.empty:
        return pd.DataFrame()

    interval_minutes = _parse_interval_minutes(interval)
    lookback_bars = max(1, round(30 / interval_minutes))
    summaries: list[dict[str, object]] = []

    trade_dates = pd.DatetimeIndex(prepared.index).normalize()
    for trade_date, group in prepared.groupby(trade_dates):
        session = group.sort_index().copy()
        if session.empty:
            continue

        open_price = float(session["open"].iloc[0])
        close_price = float(session["close"].iloc[-1])
        high_price = float(session["high"].max())
        low_price = float(session["low"].min())
        volume = float(session["volume"].sum())
        amount = float(session["amount"].sum())
        vwap = amount / volume if volume > 0 else close_price

        close_series = pd.to_numeric(session["close"], errors="coerce").astype(float)
        open_series = pd.to_numeric(session["open"], errors="coerce").astype(float)
        minute_returns = close_series.pct_change().dropna()
        realized_vol = float(minute_returns.std(ddof=0) * math.sqrt(max(1, len(minute_returns))))
        session_return = close_price / open_price - 1.0 if open_price > 0 else 0.0
        session_range_pct = (high_price - low_price) / open_price if open_price > 0 else 0.0
        vwap_gap = close_price / vwap - 1.0 if vwap > 0 else 0.0
        positive_bar_ratio = float((close_series >= open_series).mean())
        close_position = (
            (close_price - low_price) / (high_price - low_price)
            if high_price > low_price
            else 0.5
        )

        morning_group = session.head(lookback_bars)
        tail_group = session.tail(lookback_bars)
        midday_split = max(1, len(session) // 2)
        am_group = session.iloc[:midday_split]

        am_close = float(am_group["close"].iloc[-1]) if not am_group.empty else close_price
        am_return = am_close / open_price - 1.0 if open_price > 0 else 0.0
        pm_return = close_price / am_close - 1.0 if am_close > 0 else 0.0
        am_pm_diff = pm_return - am_return
        am_pm_reversal_strength = abs(am_pm_diff)

        first_tail_close = (
            float(tail_group["close"].iloc[0]) if not tail_group.empty else close_price
        )
        last30_return = close_price / first_tail_close - 1.0 if first_tail_close > 0 else 0.0

        morning_volume_share = (
            float(morning_group["volume"].sum()) / volume
            if volume > 0 and not morning_group.empty
            else 0.0
        )
        tail_volume_share = (
            float(tail_group["volume"].sum()) / volume
            if volume > 0 and not tail_group.empty
            else 0.0
        )

        above_vwap_ratio = float((close_series >= vwap).mean()) if not close_series.empty else 0.0
        price_efficiency = _price_efficiency(
            close_series,
            open_price=open_price,
            close_price=close_price,
        )
        tail_returns = pd.to_numeric(tail_group["close"], errors="coerce").pct_change().dropna()
        tail_realized_vol = (
            float(tail_returns.std(ddof=0) * math.sqrt(max(1, len(tail_returns))))
            if not tail_returns.empty
            else 0.0
        )
        tail_volatility_ratio = tail_realized_vol / realized_vol if realized_vol > 0 else 0.0
        close_vwap_stability = _close_vwap_stability(
            close_price=close_price,
            vwap=vwap,
            high_price=high_price,
            low_price=low_price,
        )
        intraday_pullback_ratio = _intraday_pullback_ratio(
            open_price=open_price,
            high_price=high_price,
            close_price=close_price,
        )

        summaries.append(
            {
                "date": (
                    trade_date
                    if isinstance(trade_date, pd.Timestamp)
                    else pd.Timestamp(str(trade_date))
                ),
                "minute_count": int(len(session)),
                "session_return": session_return,
                "session_range_pct": session_range_pct,
                "realized_vol": realized_vol,
                "vwap_gap": vwap_gap,
                "am_return": am_return,
                "pm_return": pm_return,
                "am_pm_diff": am_pm_diff,
                "last30_return": last30_return,
                "last30_volume_share": tail_volume_share,
                "tail30_volume_share": tail_volume_share,
                "morning30_volume_share": morning_volume_share,
                "positive_bar_ratio": positive_bar_ratio,
                "close_position": close_position,
                "above_vwap_ratio": above_vwap_ratio,
                "price_efficiency": price_efficiency,
                "am_pm_reversal_strength": am_pm_reversal_strength,
                "tail_volatility_ratio": tail_volatility_ratio,
                "close_vwap_stability": close_vwap_stability,
                "intraday_pullback_ratio": intraday_pullback_ratio,
            }
        )

    if not summaries:
        return pd.DataFrame()
    summary = pd.DataFrame(summaries).set_index("date").sort_index()
    summary.index.name = "date"
    return summary


def _prepare_minute_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    prepared = frame.copy()
    if not isinstance(prepared.index, pd.DatetimeIndex):
        datetime_col = None
        for candidate in ("datetime", "date", "time"):
            if candidate in prepared.columns:
                datetime_col = candidate
                break
        if datetime_col is None:
            return pd.DataFrame()
        prepared[datetime_col] = pd.to_datetime(prepared[datetime_col], errors="coerce")
        prepared = prepared.dropna(subset=[datetime_col]).set_index(datetime_col)

    required_columns = ("open", "high", "low", "close")
    for column in required_columns:
        if column not in prepared.columns:
            return pd.DataFrame()
        prepared[column] = pd.to_numeric(prepared[column], errors="coerce")
    if "volume" not in prepared.columns:
        prepared["volume"] = 0.0
    if "amount" not in prepared.columns:
        prepared["amount"] = 0.0
    prepared["volume"] = pd.to_numeric(prepared["volume"], errors="coerce").fillna(0.0)
    prepared["amount"] = pd.to_numeric(prepared["amount"], errors="coerce").fillna(0.0)
    prepared = prepared.dropna(subset=["open", "high", "low", "close"]).sort_index()
    return prepared


def _parse_interval_minutes(value: str | int | SupportsFloat) -> int:
    if isinstance(value, int):
        return max(1, value)
    if isinstance(value, float):
        return max(1, int(value))
    text = str(value).strip().lower()
    if text.endswith("m"):
        text = text[:-1]
    try:
        return max(1, int(float(text)))
    except ValueError:
        return 1


def _price_efficiency(close_series: pd.Series, *, open_price: float, close_price: float) -> float:
    path_length = float(close_series.diff().abs().dropna().sum())
    if path_length <= 0:
        return 0.0
    return abs(close_price - open_price) / path_length


def _close_vwap_stability(
    *,
    close_price: float,
    vwap: float,
    high_price: float,
    low_price: float,
) -> float:
    session_range = max(high_price - low_price, abs(close_price), 1e-6)
    return max(0.0, 1.0 - abs(close_price - vwap) / session_range)


def _intraday_pullback_ratio(
    *,
    open_price: float,
    high_price: float,
    close_price: float,
) -> float:
    if high_price <= max(open_price, close_price):
        return 0.0
    denominator = max(high_price - open_price, 1e-6)
    return max(0.0, min(1.0, (high_price - close_price) / denominator))
