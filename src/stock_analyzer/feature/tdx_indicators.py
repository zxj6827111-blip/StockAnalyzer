"""TDX-formula style primitives and the "先跌后涨5日外" screening signal.

Implements the Tongdaxin (通达信) formula translated to pandas:

    {先跌后涨新版5日外}
    MA5X28 := BARSLAST(CROSS(MA5,MA28)) > 5;
    MA7X35 := BARSLAST(CROSS(MA7,MA35)) > 5;
    M60_DTG: 60-min MACD death cross followed by golden cross (both within
             the last 20 daily bars, golden more recent), where the 60-min
             DIF/DEA are aligned to each daily bar via the value of the last
             completed 60-min bar of that day (TDX #MIN60 semantics).
    MTM_DTG: MTM(12) crossed below 0 then back above 0, both within 14 bars.
    HIST_TURN: daily MACD histogram turns positive on the current bar.
    ABOVE_MA: close above MA28 or MA35.
    XG := all of the above.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "cross",
    "bars_last",
    "count_true",
    "tdx_ema",
    "macd_lines",
    "resample_session_60m",
    "compute_fall_then_rise",
    "compute_fall_then_rise_within5",
    "compute_ma735_trend",
    "FALL_THEN_RISE_COLUMNS",
]

FALL_THEN_RISE_COLUMNS = (
    "ftr_ma5x28",
    "ftr_ma7x35",
    "ftr_m60_dtg",
    "ftr_mtm_dtg",
    "ftr_hist_turn",
    "ftr_above_ma",
    "ftr_signal_daily",
    "ftr_signal",
)


def cross(fast: pd.Series, slow: pd.Series | float) -> pd.Series:
    """TDX CROSS(A,B): A>B now and A<=B on the previous bar."""
    slow_series = (
        slow if isinstance(slow, pd.Series) else pd.Series(float(slow), index=fast.index)
    )
    above = fast > slow_series
    below_or_equal_prev = (fast <= slow_series).shift(1, fill_value=False)
    return (above & below_or_equal_prev).fillna(False)


def bars_last(condition: pd.Series) -> pd.Series:
    """TDX BARSLAST(X): bars since X was last true (0 when true now).

    Bars before the first occurrence get NaN, matching TDX null; any
    comparison against NaN evaluates False, same as TDX.
    """
    cond = condition.fillna(False).to_numpy(dtype=bool)
    idx = np.arange(len(cond), dtype=float)
    last_true = pd.Series(
        np.where(cond, idx, np.nan), index=condition.index
    ).ffill()
    return pd.Series(idx, index=condition.index) - last_true


def count_true(condition: pd.Series, window: int) -> pd.Series:
    """TDX COUNT(X,N): number of true values in the last N bars (inclusive)."""
    return (
        condition.fillna(False)
        .astype(float)
        .rolling(window=window, min_periods=1)
        .sum()
    )


def tdx_ema(series: pd.Series, span: int) -> pd.Series:
    """TDX EMA(X,N) == pandas ewm(span=N, adjust=False)."""
    return series.ewm(span=span, adjust=False).mean()


def macd_lines(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Standard TDX MACD: DIF=EMA12-EMA26, DEA=EMA(DIF,9), hist=2*(DIF-DEA)."""
    dif = tdx_ema(close, 12) - tdx_ema(close, 26)
    dea = tdx_ema(dif, 9)
    hist = 2.0 * (dif - dea)
    return dif, dea, hist


_SESSION_60M_EDGES = ((10, 30), (11, 30), (14, 0), (15, 0))


def resample_session_60m(minute_bars: pd.DataFrame) -> pd.DataFrame:
    """Resample A-share intraday bars (1m/5m, end-of-bar timestamps) to the
    four session-aligned 60-minute bars (10:30, 11:30, 14:00, 15:00)."""
    if minute_bars.empty:
        return pd.DataFrame()
    frame = minute_bars.sort_index()
    stamps = pd.DatetimeIndex(frame.index)
    minutes_of_day = stamps.hour * 60 + stamps.minute
    edges_min = [h * 60 + m for h, m in _SESSION_60M_EDGES]
    bucket = np.searchsorted(edges_min, minutes_of_day, side="left")
    valid = bucket < len(edges_min)
    frame = frame.loc[valid]
    if frame.empty:
        return pd.DataFrame()
    bucket = bucket[valid]
    bucket_minutes = np.asarray(edges_min)[bucket]
    bar_end = pd.DatetimeIndex(frame.index).normalize() + pd.to_timedelta(
        bucket_minutes, unit="m"
    )
    agg: dict[str, str] = {"close": "last"}
    for column, how in (
        ("open", "first"),
        ("high", "max"),
        ("low", "min"),
        ("volume", "sum"),
        ("amount", "sum"),
    ):
        if column in frame.columns:
            agg[column] = how
    resampled = pd.DataFrame(frame.groupby(bar_end).agg(agg))
    resampled.index.name = "datetime"
    return resampled


def _min60_daily_aligned_macd(
    min60_close: pd.Series, daily_index: pd.Index
) -> tuple[pd.Series, pd.Series] | None:
    """Align 60-min DIF/DEA to daily bars via the last 60m bar of each day
    (TDX "MACD.DIF#MIN60" semantics)."""
    if min60_close is None or min60_close.empty:
        return None
    closes = min60_close.dropna().sort_index()
    if closes.empty:
        return None
    dif, dea, _ = macd_lines(closes)
    day = pd.to_datetime(closes.index).normalize()
    dif_daily = dif.groupby(day).last()
    dea_daily = dea.groupby(day).last()
    daily_dates = pd.to_datetime(pd.Index(daily_index)).normalize()
    dif_aligned = pd.Series(
        dif_daily.reindex(daily_dates).to_numpy(), index=daily_index
    )
    dea_aligned = pd.Series(
        dea_daily.reindex(daily_dates).to_numpy(), index=daily_index
    )
    if dif_aligned.notna().sum() < 2:
        return None
    return dif_aligned, dea_aligned


def compute_fall_then_rise_within5(
    daily: pd.DataFrame,
    min60_close: pd.Series | None = None,
    *,
    min60_missing_policy: str = "fail",
) -> pd.DataFrame:
    """先跌后涨5日内筛选指标_完整版 (the "within 5 bars" variant).

    Differences from :func:`compute_fall_then_rise`: MA golden crosses must be
    WITHIN the last 5 bars, MTM window widened to 35, close must be above both
    MA28 and MA35, and a bullish MA alignment (MA5>MA7>MA28>MA35) is required.
    Emits the same ``ftr_signal`` / ``ftr_signal_daily`` contract columns so
    the evaluation tooling can be reused.
    """
    if min60_missing_policy not in {"fail", "pass"}:
        raise ValueError(f"invalid min60_missing_policy: {min60_missing_policy}")
    close = pd.to_numeric(daily["close"], errors="coerce")

    ma5 = close.rolling(5).mean()
    ma7 = close.rolling(7).mean()
    ma28 = close.rolling(28).mean()
    ma35 = close.rolling(35).mean()
    ma5x28 = bars_last(cross(ma5, ma28)) <= 5
    ma7x35 = bars_last(cross(ma7, ma35)) <= 5

    _, _, hist = macd_lines(close)
    hist_turn = (hist.shift(1) <= 0) & (hist > 0)

    mtm = close - close.shift(12)
    mtm_s = bars_last(cross(pd.Series(0.0, index=close.index), mtm))
    mtm_g = bars_last(cross(mtm, pd.Series(0.0, index=close.index)))
    mtm_dtg = (mtm_s <= 35) & (mtm_g <= 35) & (mtm_g < mtm_s)

    above_ma = (close > ma28) & (close > ma35)
    bull_ma = (ma5 > ma7) & (ma7 > ma28) & (ma28 > ma35)

    aligned = None
    if min60_close is not None:
        aligned = _min60_daily_aligned_macd(min60_close, daily.index)
    if aligned is not None:
        dif60, dea60 = aligned
        m60_ds = count_true(cross(dea60, dif60), 20) >= 1
        m60_gs = count_true(cross(dif60, dea60), 20) >= 1
        m60_dtg = (
            m60_ds
            & m60_gs
            & (bars_last(cross(dif60, dea60)) < bars_last(cross(dea60, dif60)))
            & dif60.notna()
        )
    else:
        m60_dtg = pd.Series(min60_missing_policy == "pass", index=daily.index)

    signal_daily = ma5x28 & ma7x35 & mtm_dtg & hist_turn & above_ma & bull_ma
    signal = signal_daily & m60_dtg

    result = pd.DataFrame(
        {
            "ftr_ma5x28": ma5x28,
            "ftr_ma7x35": ma7x35,
            "ftr_m60_dtg": m60_dtg,
            "ftr_mtm_dtg": mtm_dtg,
            "ftr_hist_turn": hist_turn,
            "ftr_above_ma": above_ma,
            "ftr_bull_ma": bull_ma,
            "ftr_signal_daily": signal_daily,
            "ftr_signal": signal,
        },
        index=daily.index,
    )
    return result.fillna(False).astype(bool)


def compute_ma735_trend(daily: pd.DataFrame) -> pd.DataFrame:
    """735金叉及趋势 TDX signal.

    TJ1: MA7 golden-crosses MA35 with both MAs rising.
    TJ2: MA7 above MA35, both rising, deviation (MA7-MA35)/MA35 <= 2%.
    XG:  TJ1 or TJ2.
    """
    close = pd.to_numeric(daily["close"], errors="coerce")
    ma7 = close.rolling(7).mean()
    ma35 = close.rolling(35).mean()
    dev = (ma7 - ma35) / ma35 * 100.0
    up7 = ma7 > ma7.shift(1)
    up35 = ma35 > ma35.shift(1)
    tj1 = cross(ma7, ma35) & up7 & up35
    tj2 = (ma7 > ma35) & up7 & up35 & (dev <= 2.0)
    result = pd.DataFrame(
        {
            "m735_tj1": tj1,
            "m735_tj2": tj2,
            "m735_signal": tj1 | tj2,
        },
        index=daily.index,
    )
    return result.fillna(False).astype(bool)


def compute_fall_then_rise(
    daily: pd.DataFrame,
    min60_close: pd.Series | None = None,
    *,
    min60_missing_policy: str = "fail",
) -> pd.DataFrame:
    """Compute the 先跌后涨5日外 signal on daily bars.

    Args:
        daily: daily bars indexed by date with at least a ``close`` column.
        min60_close: 60-minute close series (DatetimeIndex). When ``None`` the
            60-min condition follows ``min60_missing_policy``.
        min60_missing_policy: ``"fail"`` marks the 60-min condition False when
            no 60-min data is available; ``"pass"`` waives it (daily-only).

    Returns:
        DataFrame indexed like ``daily`` with the boolean columns listed in
        ``FALL_THEN_RISE_COLUMNS``. ``ftr_signal_daily`` is the signal without
        the 60-min condition; ``ftr_signal`` is the full formula.
    """
    if min60_missing_policy not in {"fail", "pass"}:
        raise ValueError(f"invalid min60_missing_policy: {min60_missing_policy}")
    close = pd.to_numeric(daily["close"], errors="coerce")

    ma5 = close.rolling(5).mean()
    ma7 = close.rolling(7).mean()
    ma28 = close.rolling(28).mean()
    ma35 = close.rolling(35).mean()
    # BARSLAST(CROSS(...)) > 5, made robust to windowed data: a cross that
    # happened before the loaded window is by definition more than 5 bars
    # ago, so "no cross within the last 5 bars" is the equivalent test
    # (strict TDX null semantics would fail the condition when no cross
    # exists in the loaded history).
    ma5x28 = ~(bars_last(cross(ma5, ma28)) <= 5)
    ma7x35 = ~(bars_last(cross(ma7, ma35)) <= 5)

    _, _, hist = macd_lines(close)
    hist_turn = (hist.shift(1) <= 0) & (hist > 0)

    mtm = close - close.shift(12)
    mtm_s = bars_last(cross(pd.Series(0.0, index=close.index), mtm))
    mtm_g = bars_last(cross(mtm, pd.Series(0.0, index=close.index)))
    mtm_dtg = (mtm_s <= 14) & (mtm_g <= 14) & (mtm_g < mtm_s)

    above_ma = (close > ma28) | (close > ma35)

    aligned = None
    if min60_close is not None:
        aligned = _min60_daily_aligned_macd(min60_close, daily.index)
    if aligned is not None:
        dif60, dea60 = aligned
        m60_ds = count_true(cross(dea60, dif60), 20) >= 1
        m60_gs = count_true(cross(dif60, dea60), 20) >= 1
        gold_last = bars_last(cross(dif60, dea60))
        dead_last = bars_last(cross(dea60, dif60))
        m60_dtg = m60_ds & m60_gs & (gold_last < dead_last)
        # Daily bars with no 60m data that day (holes in coverage) stay False.
        m60_dtg = m60_dtg & dif60.notna()
    else:
        fill = min60_missing_policy == "pass"
        m60_dtg = pd.Series(fill, index=daily.index)

    signal_daily = ma5x28 & ma7x35 & mtm_dtg & hist_turn & above_ma
    signal = signal_daily & m60_dtg

    result = pd.DataFrame(
        {
            "ftr_ma5x28": ma5x28,
            "ftr_ma7x35": ma7x35,
            "ftr_m60_dtg": m60_dtg,
            "ftr_mtm_dtg": mtm_dtg,
            "ftr_hist_turn": hist_turn,
            "ftr_above_ma": above_ma,
            "ftr_signal_daily": signal_daily,
            "ftr_signal": signal,
        },
        index=daily.index,
    )
    return result.fillna(False).astype(bool)
