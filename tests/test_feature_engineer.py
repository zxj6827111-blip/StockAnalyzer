from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stock_analyzer.feature.engineer import FeatureEngineer


def _bars() -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-01", periods=30)
    close = np.arange(10.0, 40.0)
    volume = np.linspace(1_000_000, 2_000_000, num=30)
    frame = pd.DataFrame(
        {
            "open": close * 0.99,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "volume": volume,
            "turnover": close * volume,
            "float_market_cap": 10_000_000_000.0,
            "is_st": [False] * 30,
            "is_delisting_risk": [False] * 30,
            "roe": np.linspace(0.06, 0.16, num=30),
            "debt_ratio": np.linspace(0.35, 0.55, num=30),
            "holder_count": np.linspace(60000, 52000, num=30),
            "block_trade_net": np.linspace(-1_000_000, 2_000_000, num=30),
            "margin_financing_balance": np.linspace(1_000_000_000, 1_200_000_000, num=30),
            "northbound_net": np.linspace(-50_000_000, 80_000_000, num=30),
            "dragon_tiger_flag": [1 if i % 7 == 0 else 0 for i in range(30)],
            "board": ["main"] * 30,
        },
        index=dates,
    )
    frame.index.name = "date"
    return frame


def test_feature_engineer_uses_t_minus_1_values() -> None:
    bars = _bars()
    features = FeatureEngineer().transform(bars)

    idx = features.index[10]
    prior_close = float(bars.iloc[9]["close"])
    assert float(features.loc[idx, "close_t1"]) == prior_close

    expected_ma5 = float(bars["close"].iloc[5:10].mean())
    assert abs(float(features.loc[idx, "ma5"]) - expected_ma5) < 1e-9
    assert float(features.loc[idx, "bg_roe"]) == pytest.approx(float(bars.iloc[9]["roe"]))
    assert float(features.loc[idx, "bg_board_code"]) == 0.0
    assert features.shape[1] >= 60
    assert "__not_exists__" not in features.columns
    assert np.isfinite(features.to_numpy(dtype=float)).all()


def test_feature_engineer_merges_intraday_summaries_with_t_minus_1_shift() -> None:
    bars = _bars()
    intraday_index = bars.index.copy()
    intraday_1m = pd.DataFrame(
        {
            "session_return": np.linspace(0.01, 0.30, num=len(intraday_index)),
            "realized_vol": np.linspace(0.02, 0.10, num=len(intraday_index)),
        },
        index=intraday_index,
    )
    intraday_5m = pd.DataFrame(
        {
            "am_pm_diff": np.linspace(-0.05, 0.05, num=len(intraday_index)),
            "close_position": np.linspace(0.10, 0.90, num=len(intraday_index)),
        },
        index=intraday_index,
    )

    features = FeatureEngineer().transform(
        bars,
        intraday_1m=intraday_1m,
        intraday_5m=intraday_5m,
    )

    idx = features.index[10]
    assert float(features.loc[idx, "i1m_session_return"]) == pytest.approx(
        float(intraday_1m.iloc[9]["session_return"])
    )
    assert float(features.loc[idx, "i1m_realized_vol"]) == pytest.approx(
        float(intraday_1m.iloc[9]["realized_vol"])
    )
    assert float(features.loc[idx, "i5m_am_pm_diff"]) == pytest.approx(
        float(intraday_5m.iloc[9]["am_pm_diff"])
    )
    assert float(features.loc[idx, "i5m_close_position"]) == pytest.approx(
        float(intraday_5m.iloc[9]["close_position"])
    )


def test_feature_engineer_merges_market_relative_features_with_t_minus_1_shift() -> None:
    bars = _bars()
    market_index = pd.DataFrame(
        {
            "benchmark_ret_1d": np.linspace(-0.03, 0.03, num=len(bars.index)),
            "benchmark_ret_5d": np.linspace(-0.05, 0.05, num=len(bars.index)),
            "benchmark_ret_20d": np.linspace(-0.08, 0.08, num=len(bars.index)),
            "excess_ret_1d": np.linspace(-0.02, 0.02, num=len(bars.index)),
            "excess_ret_5d": np.linspace(-0.04, 0.04, num=len(bars.index)),
            "beta_20d": np.linspace(0.8, 1.2, num=len(bars.index)),
            "beta_60d": np.linspace(0.7, 1.1, num=len(bars.index)),
            "benchmark_above_ma20": np.where(np.arange(len(bars.index)) >= 10, 1.0, 0.0),
        },
        index=bars.index.copy(),
    )

    features = FeatureEngineer().transform(bars, market_index=market_index)

    idx = features.index[10]
    assert float(features.loc[idx, "benchmark_ret_1d"]) == pytest.approx(
        float(market_index.iloc[9]["benchmark_ret_1d"])
    )
    assert float(features.loc[idx, "excess_ret_5d"]) == pytest.approx(
        float(market_index.iloc[9]["excess_ret_5d"])
    )
    assert float(features.loc[idx, "beta_20d"]) == pytest.approx(
        float(market_index.iloc[9]["beta_20d"])
    )
    assert float(features.loc[idx, "benchmark_above_ma20"]) == pytest.approx(
        float(market_index.iloc[9]["benchmark_above_ma20"])
    )
