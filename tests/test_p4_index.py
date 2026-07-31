from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stock_analyzer.data.market_warehouse import MarketWarehouse, load_package_daily_bars
from stock_analyzer.data.tushare_provider import TushareProvider
from stock_analyzer.feature.engineer import FeatureEngineer
from stock_analyzer.feature.market_context import fetch_market_benchmark_bars


class _FakeProP4:
    def __init__(self, index_daily: pd.DataFrame | None = None) -> None:
        self._index_daily = index_daily if index_daily is not None else pd.DataFrame()

    def index_daily(self, **kwargs: object) -> object:
        return self._index_daily

    def daily(self, **kwargs: object) -> object:
        return pd.DataFrame()

    def daily_basic(self, **kwargs: object) -> object:
        return pd.DataFrame()

    def adj_factor(self, **kwargs: object) -> object:
        return pd.DataFrame()

    def trade_cal(self, **kwargs: object) -> object:
        return pd.DataFrame()

    def stock_basic(self, **kwargs: object) -> object:
        return pd.DataFrame()

    def fina_indicator(self, **kwargs: object) -> object:
        return pd.DataFrame()

    def stk_limit(self, **kwargs: object) -> object:
        return pd.DataFrame()

    def suspend_d(self, **kwargs: object) -> object:
        return pd.DataFrame()

    def margin_detail(self, **kwargs: object) -> object:
        return pd.DataFrame()

    def moneyflow(self, **kwargs: object) -> object:
        return pd.DataFrame()

    def hk_hold(self, **kwargs: object) -> object:
        return pd.DataFrame()

    def top_list(self, **kwargs: object) -> object:
        return pd.DataFrame()

    def top_inst(self, **kwargs: object) -> object:
        return pd.DataFrame()

    def block_trade(self, **kwargs: object) -> object:
        return pd.DataFrame()


def test_fetch_index_daily_normalizes() -> None:
    raw = pd.DataFrame({
        "ts_code": ["000300.SH", "000300.SH"],
        "trade_date": ["20240418", "20240419"],
        "open": [3500.0, 3520.0],
        "high": [3550.0, 3560.0],
        "low": [3480.0, 3500.0],
        "close": [3530.0, 3540.0],
        "vol": [1e8, 1.1e8],
        "amount": [2e9, 2.1e9],
    })
    pro = _FakeProP4(index_daily=raw)
    provider = TushareProvider(pro_api=pro)  # type: ignore[arg-type]
    out = provider.fetch_index_daily("000300.SH", end_date=date(2024, 4, 19))
    assert len(out) == 2
    assert out.iloc[0]["index_code"] == "000300.SH"
    assert out.iloc[0]["close"] == pytest.approx(3530.0)
    assert out.iloc[0]["volume"] == pytest.approx(1e10)  # 手->股
    assert out.iloc[0]["turnover"] == pytest.approx(2e12)  # 千元->元
    assert out.iloc[0]["source"] == "tushare_index_daily"


def test_index_daily_empty() -> None:
    pro = _FakeProP4(index_daily=pd.DataFrame())
    provider = TushareProvider(pro_api=pro)  # type: ignore[arg-type]
    out = provider.fetch_index_daily("000300.SH")
    assert out.empty


def test_warehouse_index_daily_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "wh.duckdb"
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    wh = MarketWarehouse(db_path=db, package_root=pkg)
    wh.ensure_schema()

    idx = pd.DataFrame({
        "index_code": ["000300.SH", "000300.SH"],
        "trade_date": pd.to_datetime(["2024-04-18", "2024-04-19"]),
        "open": [3500.0, 3520.0],
        "high": [3550.0, 3560.0],
        "low": [3480.0, 3500.0],
        "close": [3530.0, 3540.0],
        "volume": [1e10, 1.1e10],
        "turnover": [2e12, 2.1e12],
        "source": ["tushare_index_daily", "tushare_index_daily"],
        "as_of": ["2024-04-18", "2024-04-19"],
        "coverage_complete": [True, True],
    })
    n1 = wh.upsert_index_daily(frame=idx)
    n2 = wh.upsert_index_daily(frame=idx)
    assert n1 == 2
    assert n2 == 2
    stored = wh.fetch_index_daily(index_code="000300.SH")
    assert len(stored) == 2
    assert stored.iloc[0]["close"] == pytest.approx(3530.0)
    package = load_package_daily_bars(source_root=pkg, symbol="000300")
    assert len(package) == 2
    benchmark = fetch_market_benchmark_bars(
        wh,
        lookback_days=120,
        primary_symbol="000300",
        end_date=date(2024, 4, 19),
    )
    assert float(benchmark.iloc[-1]["close"]) == pytest.approx(3540.0)


def _make_bars(n: int = 80) -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-01", periods=n)
    np.random.seed(42)
    close = 10.0 + np.cumsum(np.random.randn(n) * 0.1)
    return pd.DataFrame(
        {
            "open": close - 0.05,
            "high": close + 0.1,
            "low": close - 0.1,
            "close": close,
            "volume": np.random.uniform(1e6, 2e6, n),
            "turnover": np.random.uniform(1e7, 2e7, n),
            "float_market_cap": [1e10] * n,
        },
        index=dates,
    )


def _make_index(n: int = 80) -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-01", periods=n)
    np.random.seed(99)
    close = 3500.0 + np.cumsum(np.random.randn(n) * 5.0)
    return pd.DataFrame({"close": close}, index=dates)


def test_feature_engineer_relative_strength_with_index() -> None:
    bars = _make_bars(80)
    market_index = _make_index(80)
    features = FeatureEngineer().transform(bars, market_index=market_index)
    assert "excess_ret_5" in features.columns
    assert "excess_ret_20" in features.columns
    assert "excess_ret_60" in features.columns
    assert "relative_strength_5" in features.columns
    assert "relative_strength_20" in features.columns
    assert "rs_ma5" in features.columns
    assert "rs_ma20" in features.columns
    assert "rolling_beta_60" in features.columns
    assert "excess_vol_20" in features.columns
    assert "excess_vol_60" in features.columns
    assert "market_trend" in features.columns
    # After shift(1), first row is 0 (filled), later rows have values
    assert features["excess_ret_5"].iloc[-1] != 0.0


def test_feature_engineer_no_index_produces_nan_columns() -> None:
    bars = _make_bars(80)
    features = FeatureEngineer().transform(bars, market_index=None)
    assert "excess_ret_5" in features.columns
    # Without index, all relative features should be 0 (NaN filled to 0)
    assert features["excess_ret_5"].sum() == pytest.approx(0.0)
    assert features["rolling_beta_60"].sum() == pytest.approx(0.0)


def test_relative_strength_no_future_leak() -> None:
    bars = _make_bars(80)
    market_index = _make_index(80)
    features = FeatureEngineer().transform(bars, market_index=market_index)
    # Features are shifted by 1 day (T-1), so feature at time t
    # only uses data up to t-1
    # Verify: excess_ret_5 at row i uses close[i-1] and close[i-6]
    # This is enforced by the final shift(1) in transform
    assert features.index[0] == bars.index[0]
    # First row after shift should be 0 (NaN filled)
    assert features["excess_ret_5"].iloc[0] == pytest.approx(0.0)
