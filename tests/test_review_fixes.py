"""Integration tests for the P1-P4 review fixes (#1-#7)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stock_analyzer.data.financial_pit import percent_to_ratio
from stock_analyzer.data.market_warehouse import MarketWarehouse, load_package_daily_bars
from stock_analyzer.data.provider import DataSourceError
from stock_analyzer.data.tushare_provider import TushareProvider
from stock_analyzer.feature.engineer import FeatureEngineer
from stock_analyzer.feature.market_context import build_market_index_frame


# --------------------------------------------------------------------------- #
# Fix #2: percent_to_ratio always divides by 100
# --------------------------------------------------------------------------- #
def test_percent_to_ratio_always_divides_by_100() -> None:
    assert percent_to_ratio(1.2) == pytest.approx(0.012)
    assert percent_to_ratio(12.5) == pytest.approx(0.125)
    assert percent_to_ratio(0.5) == pytest.approx(0.005)
    assert percent_to_ratio(100.0) == pytest.approx(1.0)
    assert np.isnan(percent_to_ratio(None))
    assert np.isnan(percent_to_ratio("n/a"))


# --------------------------------------------------------------------------- #
# Fix #3: apply_trade_status_to_daily also writes the runtime package
# --------------------------------------------------------------------------- #
def _write_daily(wh: MarketWarehouse, symbol: str, suspended: bool) -> None:
    idx = pd.date_range("2024-01-02", periods=3, freq="B")
    frame = pd.DataFrame(
        {
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.5,
            "volume": 1_000_000.0,
            "turnover": 1.0e7,
            "float_market_cap": 1.0e10,
            "suspended": suspended,
        },
        index=idx,
    )
    frame.index.name = "date"
    wh.replace_daily_bars(symbol=symbol, frame=frame)
    from stock_analyzer.data.market_warehouse import write_package_daily_bars

    write_package_daily_bars(package_root=wh.package_root, symbol=symbol, frame=frame)


def test_apply_trade_status_writes_package(tmp_path: Path) -> None:
    wh = MarketWarehouse(
        db_path=tmp_path / "wh.duckdb",
        package_root=tmp_path / "pkg",
    )
    wh.ensure_schema()
    _write_daily(wh, "600000", suspended=False)

    status = pd.DataFrame(
        {
            "symbol": "600000",
            "trade_date": pd.to_datetime(["2024-01-02"]),
            "up_limit": [11.0],
            "down_limit": [9.0],
            "suspended": [True],
            "source": ["tushare_stk_limit+suspend_d"],
            "as_of": ["2024-01-02"],
            "coverage_complete": [True],
        }
    )
    wh.upsert_trade_status(symbol="600000", frame=status)
    wh.apply_trade_status_to_daily(symbol="600000")

    pkg = load_package_daily_bars(source_root=wh.package_root, symbol="600000")
    assert bool(pkg.loc[pd.Timestamp("2024-01-02"), "suspended"]) is True
    assert float(pkg.loc[pd.Timestamp("2024-01-02"), "up_limit"]) == pytest.approx(11.0)


# --------------------------------------------------------------------------- #
# Fix #4: P2/P3 projected back into daily_bars + package
# --------------------------------------------------------------------------- #
def test_apply_p2_p3_projects_to_daily_and_package(tmp_path: Path) -> None:
    wh = MarketWarehouse(db_path=tmp_path / "wh.duckdb", package_root=tmp_path / "pkg")
    wh.ensure_schema()
    _write_daily(wh, "600000", suspended=False)

    margin = pd.DataFrame(
        {
            "symbol": "600000",
            "trade_date": pd.to_datetime(["2024-01-02"]),
            "financing_balance": [1.23e8],
            "source": ["tushare_margin_detail"],
            "as_of": ["2024-01-02"],
            "coverage_complete": [True],
        }
    )
    wh.upsert_margin_detail(symbol="600000", frame=margin)

    top_list = pd.DataFrame(
        {
            "symbol": "600000",
            "trade_date": pd.to_datetime(["2024-01-03"]),
            "dragon_tiger_flag": [1.0],
            "source": ["tushare_top_list"],
            "as_of": ["2024-01-03"],
            "coverage_complete": [True],
        }
    )
    wh.upsert_top_list_events(symbol="600000", frame=top_list)

    wh.apply_p2_p3_to_daily(symbol="600000")

    pkg = load_package_daily_bars(source_root=wh.package_root, symbol="600000")
    assert float(pkg.loc[pd.Timestamp("2024-01-02"), "financing_balance"]) == pytest.approx(1.23e8)
    assert float(
        pkg.loc[pd.Timestamp("2024-01-02"), "margin_financing_balance"]
    ) == pytest.approx(1.23e8)
    assert float(pkg.loc[pd.Timestamp("2024-01-03"), "dragon_tiger_flag"]) == pytest.approx(1.0)
    # northbound_net and block_trade_net must stay NaN (no fabricated values)
    assert pd.isna(pkg.loc[pd.Timestamp("2024-01-02"), "northbound_net"])
    assert pd.isna(pkg.loc[pd.Timestamp("2024-01-02"), "block_trade_net"])


# --------------------------------------------------------------------------- #
# Fix #5: benchmark_close flows through the real pipeline market frame
# --------------------------------------------------------------------------- #
def test_benchmark_close_reaches_relative_strength_features() -> None:
    idx = pd.date_range("2024-01-02", periods=80, freq="B")
    bars = pd.DataFrame(
        {
            "open": np.linspace(10, 12, 80),
            "high": np.linspace(10.5, 12.5, 80),
            "low": np.linspace(9.5, 11.5, 80),
            "close": np.linspace(10, 12, 80),
            "volume": np.full(80, 1.0e6),
            "turnover": np.full(80, 1.0e7),
            "float_market_cap": np.full(80, 1.0e10),
        },
        index=idx,
    )
    bars.index.name = "date"
    benchmark = pd.DataFrame({"close": np.linspace(3000, 3300, 80)}, index=idx)
    benchmark.index.name = "date"

    market_frame = build_market_index_frame(bars=bars, benchmark_bars=benchmark)
    assert "benchmark_close" in market_frame.columns

    features = FeatureEngineer().transform(bars, market_index=market_frame)
    # After T-1 shift, the tail should carry non-zero relative-strength signal.
    assert features["relative_strength_20"].abs().sum() > 0
    assert features["rolling_beta_60"].abs().sum() > 0
    assert features["market_trend"].abs().sum() > 0


# --------------------------------------------------------------------------- #
# Fix #7: fetch_trade_status raises when BOTH interfaces fail
# --------------------------------------------------------------------------- #
class _FailingPro:
    def stk_limit(self, **kwargs: object) -> object:
        raise RuntimeError("stk_limit down")

    def suspend_d(self, **kwargs: object) -> object:
        raise RuntimeError("suspend_d down")


def test_fetch_trade_status_raises_on_total_failure() -> None:
    provider = TushareProvider(pro_api=_FailingPro(), token="x")  # type: ignore[arg-type]
    with pytest.raises(DataSourceError):
        provider.fetch_trade_status("600000")


class _PartialPro:
    def stk_limit(self, **kwargs: object) -> object:
        return pd.DataFrame(
            {
                "ts_code": ["600000.SH"],
                "trade_date": ["20240102"],
                "up_limit": [11.0],
                "down_limit": [9.0],
            }
        )

    def suspend_d(self, **kwargs: object) -> object:
        raise RuntimeError("suspend_d down")


def test_fetch_trade_status_partial_failure_still_returns() -> None:
    provider = TushareProvider(pro_api=_PartialPro(), token="x")  # type: ignore[arg-type]
    frame = provider.fetch_trade_status("600000")
    assert not frame.empty
    assert float(frame.iloc[0]["up_limit"]) == pytest.approx(11.0)
