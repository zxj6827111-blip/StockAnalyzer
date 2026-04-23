from __future__ import annotations

import pandas as pd

from stock_analyzer.data.intraday_summary import summarize_minute_bars
from stock_analyzer.feature.intraday_factors import summarize_intraday_factors


def _minute_frame() -> pd.DataFrame:
    index = pd.date_range("2026-03-10 09:30:00", periods=12, freq="5min")
    return pd.DataFrame(
        {
            "open": [
                10.0,
                10.05,
                10.10,
                10.12,
                10.14,
                10.16,
                10.18,
                10.20,
                10.22,
                10.19,
                10.17,
                10.16,
            ],
            "high": [
                10.06,
                10.11,
                10.14,
                10.16,
                10.18,
                10.21,
                10.23,
                10.25,
                10.24,
                10.21,
                10.18,
                10.18,
            ],
            "low": [
                9.99,
                10.02,
                10.08,
                10.10,
                10.12,
                10.15,
                10.17,
                10.18,
                10.17,
                10.15,
                10.13,
                10.12,
            ],
            "close": [
                10.05,
                10.10,
                10.12,
                10.15,
                10.17,
                10.19,
                10.21,
                10.22,
                10.19,
                10.17,
                10.16,
                10.17,
            ],
            "volume": [1000, 1200, 1100, 1300, 1250, 1400, 1500, 1600, 1550, 1500, 1480, 1520],
            "amount": [
                10050,
                12120,
                11132,
                13195,
                12712,
                14266,
                15315,
                16352,
                15795,
                15255,
                15037,
                15458,
            ],
        },
        index=index,
    )


def test_intraday_factor_summary_exposes_new_daily_columns() -> None:
    summary = summarize_intraday_factors(_minute_frame(), interval="5m")
    row = summary.iloc[0]

    assert "tail30_volume_share" in summary.columns
    assert "morning30_volume_share" in summary.columns
    assert "above_vwap_ratio" in summary.columns
    assert "price_efficiency" in summary.columns
    assert "am_pm_reversal_strength" in summary.columns
    assert "tail_volatility_ratio" in summary.columns
    assert "close_vwap_stability" in summary.columns
    assert "intraday_pullback_ratio" in summary.columns
    assert 0.0 <= float(row["tail30_volume_share"]) <= 1.0
    assert 0.0 <= float(row["morning30_volume_share"]) <= 1.0


def test_intraday_summary_builder_reuses_shared_factor_module() -> None:
    summary = summarize_minute_bars(_minute_frame(), interval="5m")
    assert "tail30_volume_share" in summary.columns
    assert "close_vwap_stability" in summary.columns
