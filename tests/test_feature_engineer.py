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
            "moneyflow_net_amount": np.linspace(-2_000_000, 3_000_000, num=30),
            "hk_hold_ratio": np.linspace(1.0, 1.4, num=30),
            "hk_hold_change": np.linspace(-10_000, 20_000, num=30),
            "inst_net_amount": np.where(np.arange(30) % 7 == 0, 1_000_000.0, np.nan),
            "block_trade_amount": np.where(np.arange(30) % 5 == 0, 2_000_000.0, np.nan),
            "block_trade_volume": np.where(np.arange(30) % 5 == 0, 100_000.0, np.nan),
            "block_trade_premium_discount": np.where(
                np.arange(30) % 5 == 0, 0.01, np.nan
            ),
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
    assert "moneyflow_net_20" in features.columns
    assert "hk_hold_ratio_chg_5" in features.columns
    assert "inst_net_amount_20" in features.columns
    assert "block_trade_turnover_ratio_20" in features.columns
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


def test_feature_engineer_handles_nan_background_fields() -> None:
    bars = _bars()
    bars["holder_count"] = np.nan
    bars["northbound_net"] = np.nan
    bars["block_trade_net"] = np.nan
    bars["margin_financing_balance"] = np.nan
    features = FeatureEngineer().transform(bars)
    assert features.shape[0] > 0
    assert np.isfinite(features.to_numpy(dtype=float)).all()


_STANDARD_INTRADAY_COLUMNS = [
    "minute_count",
    "session_return",
    "session_range_pct",
    "realized_vol",
    "vwap_gap",
    "am_return",
    "pm_return",
    "am_pm_diff",
    "last30_return",
    "last30_volume_share",
    "tail30_volume_share",
    "morning30_volume_share",
    "positive_bar_ratio",
    "close_position",
    "above_vwap_ratio",
    "price_efficiency",
    "am_pm_reversal_strength",
    "tail_volatility_ratio",
    "close_vwap_stability",
    "intraday_pullback_ratio",
]


def test_transform_column_set_stable_without_intraday() -> None:
    bars = _bars()

    features_without = FeatureEngineer().transform(bars)

    expected_intraday_columns = [
        f"i1m_{column}" for column in _STANDARD_INTRADAY_COLUMNS
    ] + [f"i5m_{column}" for column in _STANDARD_INTRADAY_COLUMNS]
    for column in expected_intraday_columns:
        assert column in features_without.columns
    assert (features_without[expected_intraday_columns] == 0.0).all().all()

    intraday = pd.DataFrame(
        {
            "session_return": np.linspace(0.01, 0.30, num=len(bars.index)),
            "realized_vol": np.linspace(0.02, 0.10, num=len(bars.index)),
            "am_pm_diff": np.linspace(-0.05, 0.05, num=len(bars.index)),
        },
        index=bars.index.copy(),
    )
    features_with = FeatureEngineer().transform(
        bars,
        intraday_1m=intraday,
        intraday_5m=intraday,
    )

    assert set(features_without.columns) == set(features_with.columns)
    assert len(
        [c for c in features_with.columns if c.startswith("i1m_")]
    ) == len(_STANDARD_INTRADAY_COLUMNS)
    assert len(
        [c for c in features_with.columns if c.startswith("i5m_")]
    ) == len(_STANDARD_INTRADAY_COLUMNS)
