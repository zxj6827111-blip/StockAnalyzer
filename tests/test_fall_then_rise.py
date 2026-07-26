"""Tests for the 先跌后涨5日外 TDX indicator and pipeline filter."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stock_analyzer.config import FallThenRiseConfig
from stock_analyzer.feature.tdx_indicators import (
    bars_last,
    compute_fall_then_rise,
    compute_ma735_trend,
    count_true,
    cross,
    resample_session_60m,
)
from stock_analyzer.filter.fall_then_rise import FallThenRiseFilter


def test_cross_matches_tdx_semantics() -> None:
    fast = pd.Series([1.0, 2.0, 3.0, 2.0, 1.0, 2.0, 3.0])
    slow = pd.Series([2.0] * 7)
    assert cross(fast, slow).tolist() == [
        False, False, True, False, False, False, True,
    ]
    # Equality on the previous bar still counts as a cross.
    fast_eq = pd.Series([2.0, 3.0])
    assert cross(fast_eq, pd.Series([2.0, 2.0])).tolist() == [False, True]


def test_bars_last_counts_bars_since_condition() -> None:
    condition = pd.Series([False, True, False, False, True, False])
    values = bars_last(condition)
    assert np.isnan(values.iloc[0])
    assert values.iloc[1:].tolist() == [0.0, 1.0, 2.0, 0.0, 1.0]


def test_count_true_rolling_window() -> None:
    condition = pd.Series([True, False, True, True, False])
    assert count_true(condition, 3).tolist() == [1.0, 1.0, 2.0, 2.0, 2.0]


def test_resample_session_60m_buckets() -> None:
    stamps = pd.to_datetime(
        [
            "2026-01-05 09:35", "2026-01-05 10:30",  # first session hour
            "2026-01-05 10:35", "2026-01-05 11:30",  # second
            "2026-01-05 13:05", "2026-01-05 14:00",  # third
            "2026-01-05 14:05", "2026-01-05 15:00",  # fourth
        ]
    )
    frame = pd.DataFrame(
        {
            "open": range(8),
            "high": range(8),
            "low": range(8),
            "close": range(8),
            "volume": [1] * 8,
        },
        index=stamps,
    )
    result = resample_session_60m(frame)
    assert [ts.strftime("%H:%M") for ts in result.index] == [
        "10:30", "11:30", "14:00", "15:00",
    ]
    assert result["close"].tolist() == [1.0, 3.0, 5.0, 7.0]
    assert result["open"].tolist() == [0.0, 2.0, 4.0, 6.0]
    assert result["volume"].tolist() == [2.0, 2.0, 2.0, 2.0]


def _fall_then_rise_daily_bars() -> pd.DataFrame:
    """Build a price path that satisfies every daily condition on the last bar.

    Long uptrend (golden crosses far in the past, close above MA28/35),
    then a pullback that pushes MTM below zero, then a rebound so MTM and
    the MACD histogram both turn positive on the final bar.
    """
    prices: list[float] = list(np.linspace(80.0, 100.0, 60))  # slow climb
    prices += list(np.linspace(100.0, 130.0, 30))  # steep climb
    prices += list(np.linspace(130.0, 125.5, 14))  # shallow pullback
    prices += list(np.linspace(126.0, 133.0, 10))  # rebound
    index = pd.bdate_range("2025-06-02", periods=len(prices))
    return pd.DataFrame({"close": prices}, index=index)


def test_compute_fall_then_rise_daily_conditions_fire() -> None:
    daily = _fall_then_rise_daily_bars()
    flags = compute_fall_then_rise(daily, None, min60_missing_policy="pass")
    signal_days = flags.index[flags["ftr_signal_daily"]]
    assert len(signal_days) >= 1
    latest = flags.loc[signal_days[-1]]
    assert bool(latest["ftr_ma5x28"]) and bool(latest["ftr_ma7x35"])
    assert bool(latest["ftr_mtm_dtg"]) and bool(latest["ftr_hist_turn"])
    assert bool(latest["ftr_above_ma"])
    # With policy "pass" the 60-min condition is waived.
    assert bool(latest["ftr_signal"])


def test_compute_fall_then_rise_min60_policy_fail_blocks_signal() -> None:
    daily = _fall_then_rise_daily_bars()
    flags = compute_fall_then_rise(daily, None, min60_missing_policy="fail")
    assert not flags["ftr_signal"].any()
    assert flags["ftr_signal_daily"].sum() >= 1


def test_compute_fall_then_rise_min60_death_then_golden() -> None:
    daily = _fall_then_rise_daily_bars()
    # Per-bar geometric 60-min closes: climb, then a fall that dead-crosses
    # the 60m MACD inside the 20-bar window, then a rebound that golden-
    # crosses it more recently.
    climb = 100.0 * (1.001 ** np.arange(94 * 4))
    fall = climb[-1] * (0.99 ** np.arange(1, 8 * 4 + 1))
    rebound = fall[-1] * (1.012 ** np.arange(1, 12 * 4 + 1))
    values = np.concatenate([climb, fall, rebound])
    stamps = [
        pd.Timestamp(str(day.date()) + hour_minute)
        for day in daily.index
        for hour_minute in (" 10:30", " 11:30", " 14:00", " 15:00")
    ]
    assert len(values) == len(stamps)
    min60 = pd.Series(values, index=pd.DatetimeIndex(stamps))
    flags = compute_fall_then_rise(daily, min60, min60_missing_policy="fail")
    assert flags["ftr_signal"].sum() >= 1
    fired = flags.loc[flags["ftr_signal"]].iloc[-1]
    assert bool(fired["ftr_m60_dtg"])


def test_compute_ma735_trend_cross_and_pullback() -> None:
    # Established uptrend, shallow dip pulling MA7 under MA35, then resume:
    # MA7 re-crosses MA35 upward while both MAs are still rising.
    prices = list(np.linspace(100.0, 140.0, 100))
    prices += list(np.linspace(139.5, 133.0, 12))
    prices += list(np.linspace(133.5, 145.0, 25))
    daily = pd.DataFrame(
        {"close": prices}, index=pd.bdate_range("2025-01-01", periods=len(prices))
    )
    flags = compute_ma735_trend(daily)
    assert flags["m735_tj1"].sum() == 1  # exactly one fresh golden cross
    cross_day = flags.index[flags["m735_tj1"]][0]
    assert flags.at[cross_day, "m735_signal"]
    # TJ2 keeps firing right after the cross while deviation is small,
    # then stops once MA7 runs more than 2% above MA35.
    after = flags.loc[flags.index > cross_day, "m735_signal"]
    assert after.iloc[:3].all()
    assert not after.iloc[-1]


def test_invalid_min60_policy_raises() -> None:
    daily = _fall_then_rise_daily_bars()
    with pytest.raises(ValueError):
        compute_fall_then_rise(daily, None, min60_missing_policy="bogus")


def test_filter_disabled_returns_not_applied() -> None:
    config = FallThenRiseConfig(enabled=False)
    decision = FallThenRiseFilter(config).evaluate(
        symbol="600000", strategy="trend", bars=_fall_then_rise_daily_bars()
    )
    assert not decision.applied
    assert decision.allowed


def test_filter_strategy_scope() -> None:
    config = FallThenRiseConfig(enabled=True, mode="gate", apply_to=["monster"])
    decision = FallThenRiseFilter(config).evaluate(
        symbol="600000", strategy="trend", bars=_fall_then_rise_daily_bars()
    )
    assert not decision.applied


def test_filter_gate_and_bonus_modes() -> None:
    bars = _fall_then_rise_daily_bars()
    gate = FallThenRiseFilter(
        FallThenRiseConfig(enabled=True, mode="gate", min60_missing_policy="pass")
    ).evaluate(symbol="600000", strategy="trend", bars=bars)
    assert gate.applied
    assert gate.allowed == gate.signal

    bonus = FallThenRiseFilter(
        FallThenRiseConfig(
            enabled=True, mode="bonus", bonus_points=7.5, min60_missing_policy="pass"
        )
    ).evaluate(symbol="600000", strategy="trend", bars=bars)
    assert bonus.applied
    assert bonus.bonus_score == (7.5 if bonus.signal else 0.0)

    annotate = FallThenRiseFilter(
        FallThenRiseConfig(enabled=True, mode="annotate", min60_missing_policy="pass")
    ).evaluate(symbol="600000", strategy="trend", bars=bars)
    _assert_annotate(annotate)


def test_bonus_condition_ma_gates_fires_without_full_signal() -> None:
    bars = _fall_then_rise_daily_bars()
    # With min60_missing_policy="fail" (and no 60m data) the full signal is
    # impossible, but the MA gates still hold on the final bar.
    decision = FallThenRiseFilter(
        FallThenRiseConfig(
            enabled=True,
            mode="bonus",
            bonus_points=4.0,
            bonus_condition="ma_gates",
            min60_missing_policy="fail",
        )
    ).evaluate(symbol="600000", strategy="trend", bars=bars)
    assert decision.applied
    assert not decision.signal
    assert decision.flags["ftr_ma5x28"] and decision.flags["ftr_ma7x35"]
    assert decision.bonus_score == 4.0
    assert decision.bonus_condition == "ma_gates"
    assert decision.reason == "fall_then_rise_bonus:ma_gates"

    # Same bars, but bonus tied to the full signal -> no bonus.
    strict = FallThenRiseFilter(
        FallThenRiseConfig(
            enabled=True,
            mode="bonus",
            bonus_points=4.0,
            bonus_condition="signal",
            min60_missing_policy="fail",
        )
    ).evaluate(symbol="600000", strategy="trend", bars=bars)
    assert strict.bonus_score == 0.0
    assert strict.reason == "fall_then_rise_no_bonus"

    # signal_daily variant fires on the same bars (daily conditions all hold).
    daily_variant = FallThenRiseFilter(
        FallThenRiseConfig(
            enabled=True,
            mode="bonus",
            bonus_points=4.0,
            bonus_condition="signal_daily",
            min60_missing_policy="fail",
        )
    ).evaluate(symbol="600000", strategy="trend", bars=bars)
    assert daily_variant.bonus_score == 4.0


def test_invalid_mode_or_bonus_condition_raises() -> None:
    with pytest.raises(ValueError):
        FallThenRiseFilter(FallThenRiseConfig(enabled=True, mode="bogus"))
    with pytest.raises(ValueError):
        FallThenRiseFilter(
            FallThenRiseConfig(enabled=True, mode="bonus", bonus_condition="bogus")
        )


def _assert_annotate(annotate) -> None:
    assert annotate.applied
    assert annotate.allowed
    assert annotate.bonus_score == 0.0
    assert set(annotate.flags) == {
        "ftr_ma5x28", "ftr_ma7x35", "ftr_m60_dtg",
        "ftr_mtm_dtg", "ftr_hist_turn", "ftr_above_ma",
    }
    assert annotate.trace()["applied"] is True
