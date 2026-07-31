"""Integration tests for the P1-P4 review fixes (#1-#7)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stock_analyzer.config import DataSourceConfig
from stock_analyzer.data.financial_pit import percent_to_ratio
from stock_analyzer.data.market_warehouse import MarketWarehouse, load_package_daily_bars
from stock_analyzer.data.provider import DataSourceError
from stock_analyzer.data.resilient_provider import ResilientProvider
from stock_analyzer.data.tushare_provider import TushareProvider
from stock_analyzer.feature.engineer import FeatureEngineer
from stock_analyzer.feature.market_context import build_market_index_frame
from stock_analyzer.runtime.services.market_sync_service import RuntimeMarketSyncService


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
# Fix #1b: enrichment APIs are resolved through ResilientProvider wrappers
# --------------------------------------------------------------------------- #
class _WrappedEnrichmentProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch_daily_bars(
        self,
        symbol: str,
        lookback_days: int = 120,
        *,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        _ = symbol, lookback_days, end_date
        return pd.DataFrame()

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        _ = symbol, interval, lookback_days
        return pd.DataFrame()

    def fetch_index_daily(self, index_code: str, **kwargs: object) -> pd.DataFrame:
        self.calls.append("index_daily")
        _ = index_code, kwargs
        return pd.DataFrame(
            {
                "index_code": ["000300.SH"],
                "trade_date": pd.to_datetime(["2024-01-03"]),
                "open": [3000.0],
                "high": [3010.0],
                "low": [2990.0],
                "close": [3005.0],
                "volume": [1.0e8],
                "turnover": [2.0e11],
                "source": ["tushare_index_daily"],
                "as_of": ["2024-01-03"],
                "coverage_complete": [True],
            }
        )

    def fetch_fina_indicator(self, **kwargs: object) -> pd.DataFrame:
        self.calls.append("financial")
        _ = kwargs
        return pd.DataFrame(
            {
                "symbol": ["600000"],
                "end_date": pd.to_datetime(["2023-12-31"]),
                "ann_date": pd.to_datetime(["2024-01-02"]),
                "roe": [0.12],
                "debt_ratio": [0.35],
                "update_flag": [0],
                "financial_report_date": ["2023-12-31"],
                "financial_as_of": ["2024-01-02"],
                "financial_source": ["tushare_fina_indicator"],
                "financial_trust_level": ["reported"],
                "financial_missing_fields": [""],
                "financial_data_complete": [True],
                "financial_completeness": [1.0],
                "coverage_complete": [True],
                "as_of": ["2024-01-02"],
                "source": ["tushare_fina_indicator"],
            }
        )

    def fetch_trade_status(self, **kwargs: object) -> pd.DataFrame:
        self.calls.append("trade_status")
        _ = kwargs
        return pd.DataFrame(
            {
                "symbol": ["600000"],
                "trade_date": pd.to_datetime(["2024-01-03"]),
                "up_limit": [11.0],
                "down_limit": [9.0],
                "suspended": [False],
                "source": ["tushare_stk_limit+suspend_d"],
                "as_of": ["2024-01-03"],
                "coverage_complete": [True],
            }
        )

    def fetch_margin_detail(self, **kwargs: object) -> pd.DataFrame:
        self.calls.append("margin")
        _ = kwargs
        return pd.DataFrame(
            {
                "symbol": ["600000"],
                "trade_date": pd.to_datetime(["2024-01-03"]),
                "financing_balance": [1.23e8],
                "source": ["tushare_margin_detail"],
                "as_of": ["2024-01-03"],
                "coverage_complete": [True],
            }
        )

    def fetch_moneyflow(self, **kwargs: object) -> pd.DataFrame:
        self.calls.append("moneyflow")
        _ = kwargs
        return pd.DataFrame(
            {
                "symbol": ["600000"],
                "trade_date": pd.to_datetime(["2024-01-03"]),
                "net_mf_amount": [2.5e6],
                "source": ["tushare_moneyflow"],
                "as_of": ["2024-01-03"],
                "coverage_complete": [True],
            }
        )

    def fetch_hk_hold(self, **kwargs: object) -> pd.DataFrame:
        self.calls.append("hk_hold")
        _ = kwargs
        return pd.DataFrame(
            {
                "symbol": ["600000"],
                "trade_date": pd.to_datetime(["2024-01-03"]),
                "hold_vol": [1.0e6],
                "hold_ratio": [1.2],
                "source": ["tushare_hk_hold"],
                "as_of": ["2024-01-03"],
                "coverage_complete": [True],
            }
        )

    def fetch_top_list(self, **kwargs: object) -> pd.DataFrame:
        self.calls.append("top_list")
        _ = kwargs
        return pd.DataFrame(
            {
                "symbol": ["600000"],
                "trade_date": pd.to_datetime(["2024-01-03"]),
                "dragon_tiger_flag": [1.0],
                "source": ["tushare_top_list"],
                "as_of": ["2024-01-03"],
                "coverage_complete": [True],
            }
        )

    def fetch_top_inst(self, **kwargs: object) -> pd.DataFrame:
        self.calls.append("top_inst")
        _ = kwargs
        return pd.DataFrame(
            {
                "symbol": ["600000"],
                "trade_date": pd.to_datetime(["2024-01-03"]),
                "institution_name": ["A"],
                "inst_buy_amount": [3.0e6],
                "inst_sell_amount": [1.0e6],
                "inst_net_amount": [2.0e6],
                "source": ["tushare_top_inst"],
                "as_of": ["2024-01-03"],
                "coverage_complete": [True],
            }
        )

    def fetch_block_trade(self, **kwargs: object) -> pd.DataFrame:
        self.calls.append("block_trade")
        _ = kwargs
        return pd.DataFrame(
            {
                "symbol": ["600000"],
                "trade_date": pd.to_datetime(["2024-01-03"]),
                "block_trade_amount": [5.1e6],
                "block_trade_premium_discount": [0.02],
                "source": ["tushare_block_trade"],
                "as_of": ["2024-01-03"],
                "coverage_complete": [True],
            }
        )


def test_enrichment_resolves_resilient_provider_primary(tmp_path: Path) -> None:
    wh = MarketWarehouse(db_path=tmp_path / "wh.duckdb", package_root=tmp_path / "pkg")
    wh.ensure_schema()
    _write_daily(wh, "600000", suspended=False)
    primary = _WrappedEnrichmentProvider()
    provider = ResilientProvider(
        primary=primary,
        backup=None,
        config=DataSourceConfig(primary="tushare"),
    )
    sync = RuntimeMarketSyncService(object())

    index_result = sync._enrich_market_warehouse_index_daily(
        warehouse=wh,
        online_provider=provider,
        target_end_date=date(2024, 1, 3),
        force=True,
    )
    symbol_result = sync._enrich_market_warehouse_symbol(
        warehouse=wh,
        online_provider=provider,
        symbol="600000",
        target_end_date=date(2024, 1, 3),
        force=True,
    )

    assert index_result["status"] == "ok"
    assert symbol_result["status"] == "ok"
    assert set(primary.calls) == {
        "index_daily",
        "financial",
        "trade_status",
        "margin",
        "moneyflow",
        "hk_hold",
        "top_list",
        "top_inst",
        "block_trade",
    }
    assert len(wh.fetch_index_daily(index_code="000300.SH")) == 1
    assert len(wh.fetch_financial_snapshots(symbol="600000")) == 1

    pkg = load_package_daily_bars(source_root=wh.package_root, symbol="600000")
    day = pd.Timestamp("2024-01-03")
    assert float(pkg.loc[day, "roe"]) == pytest.approx(0.12)
    assert float(pkg.loc[day, "debt_ratio"]) == pytest.approx(0.35)
    assert float(pkg.loc[day, "up_limit"]) == pytest.approx(11.0)
    assert float(pkg.loc[day, "financing_balance"]) == pytest.approx(1.23e8)
    assert float(pkg.loc[day, "moneyflow_net_amount"]) == pytest.approx(2.5e6)
    assert float(pkg.loc[day, "dragon_tiger_flag"]) == pytest.approx(1.0)

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

    moneyflow = pd.DataFrame(
        {
            "symbol": ["600000"],
            "trade_date": pd.to_datetime(["2024-01-02"]),
            "net_mf_amount": [2.5e6],
            "source": ["tushare_moneyflow"],
            "as_of": ["2024-01-02"],
            "coverage_complete": [True],
        }
    )
    wh.upsert_moneyflow(symbol="600000", frame=moneyflow)

    hk_hold = pd.DataFrame(
        {
            "symbol": ["600000", "600000"],
            "trade_date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "hold_vol": [1.0e6, 1.2e6],
            "hold_ratio": [1.0, 1.2],
            "source": ["tushare_hk_hold", "tushare_hk_hold"],
            "as_of": ["2024-01-02", "2024-01-03"],
            "coverage_complete": [True, True],
        }
    )
    wh.upsert_hk_hold(symbol="600000", frame=hk_hold)

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

    top_inst = pd.DataFrame(
        {
            "symbol": ["600000", "600000"],
            "trade_date": pd.to_datetime(["2024-01-03", "2024-01-03"]),
            "institution_name": ["A", "B"],
            "inst_buy_amount": [3.0e6, 2.0e6],
            "inst_sell_amount": [1.0e6, 2.5e6],
            "inst_net_amount": [2.0e6, -0.5e6],
            "source": ["tushare_top_inst", "tushare_top_inst"],
            "as_of": ["2024-01-03", "2024-01-03"],
            "coverage_complete": [True, True],
        }
    )
    wh.upsert_top_inst_events(symbol="600000", frame=top_inst)

    block_trade = pd.DataFrame(
        {
            "symbol": ["600000"],
            "trade_date": pd.to_datetime(["2024-01-03"]),
            "block_price": [10.2],
            "block_trade_volume": [5.0e5],
            "block_trade_amount": [5.1e6],
            "block_trade_premium_discount": [0.02],
            "block_trade_net": [np.nan],
            "source": ["tushare_block_trade"],
            "as_of": ["2024-01-03"],
            "coverage_complete": [True],
        }
    )
    wh.upsert_block_trade_events(symbol="600000", frame=block_trade)

    wh.apply_p2_p3_to_daily(symbol="600000")

    pkg = load_package_daily_bars(source_root=wh.package_root, symbol="600000")
    assert float(pkg.loc[pd.Timestamp("2024-01-02"), "financing_balance"]) == pytest.approx(1.23e8)
    assert float(
        pkg.loc[pd.Timestamp("2024-01-02"), "margin_financing_balance"]
    ) == pytest.approx(1.23e8)
    assert float(pkg.loc[pd.Timestamp("2024-01-03"), "dragon_tiger_flag"]) == pytest.approx(1.0)
    assert float(pkg.loc[pd.Timestamp("2024-01-02"), "moneyflow_net_amount"]) == pytest.approx(
        2.5e6
    )
    assert float(pkg.loc[pd.Timestamp("2024-01-03"), "hk_hold_ratio"]) == pytest.approx(1.2)
    assert float(pkg.loc[pd.Timestamp("2024-01-03"), "hk_hold_change"]) == pytest.approx(2.0e5)
    assert float(pkg.loc[pd.Timestamp("2024-01-03"), "inst_net_amount"]) == pytest.approx(1.5e6)
    assert float(pkg.loc[pd.Timestamp("2024-01-03"), "block_trade_amount"]) == pytest.approx(
        5.1e6
    )
    assert float(
        pkg.loc[pd.Timestamp("2024-01-03"), "block_trade_premium_discount"]
    ) == pytest.approx(0.02)
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
    provider = TushareProvider(  # type: ignore[arg-type]
        pro_api=_PartialPro(), token="x", min_request_interval_sec=0
    )
    frame = provider.fetch_trade_status("600000")
    assert not frame.empty
    assert float(frame.iloc[0]["up_limit"]) == pytest.approx(11.0)
    assert frame.iloc[0]["suspended"] is None
    assert bool(frame.iloc[0]["coverage_complete"]) is False
    assert frame.attrs["coverage_complete"] is False
    assert frame.attrs["failed_components"] == ["suspend_d"]


def test_partial_trade_status_roundtrip_preserves_unknown_suspension(tmp_path: Path) -> None:
    wh = MarketWarehouse(db_path=tmp_path / "wh.duckdb", package_root=tmp_path / "pkg")
    wh.ensure_schema()
    _write_daily(wh, "600000", suspended=True)
    provider = TushareProvider(  # type: ignore[arg-type]
        pro_api=_PartialPro(), token="x", min_request_interval_sec=0
    )

    result = RuntimeMarketSyncService(object())._enrich_market_warehouse_trade_status(
        warehouse=wh,
        online_provider=provider,
        symbol="600000",
        target_end_date=date(2024, 1, 2),
        start_date=date(2024, 1, 2),
    )

    assert result["status"] == "partial"
    daily = wh.fetch_all_daily_bars(symbol="600000")
    assert bool(daily.loc[pd.Timestamp("2024-01-02"), "suspended"]) is True
    assert float(daily.loc[pd.Timestamp("2024-01-02"), "up_limit"]) == pytest.approx(11.0)


def test_enrichment_checkpoint_skips_current_and_uses_overlap(tmp_path: Path) -> None:
    wh = MarketWarehouse(db_path=tmp_path / "wh.duckdb", package_root=tmp_path / "pkg")
    wh.write_symbol_meta(
        "600000",
        {"enrichment_checkpoints": {"financial": "2026-07-29"}},
    )
    sync = RuntimeMarketSyncService(object())

    current = sync._resolve_enrichment_start_date(
        warehouse=wh,
        symbol="600000",
        phase="financial",
        target_end_date=date(2026, 7, 29),
        default_lookback_days=365 * 5,
        overlap_days=7,
    )
    incremental = sync._resolve_enrichment_start_date(
        warehouse=wh,
        symbol="600000",
        phase="financial",
        target_end_date=date(2026, 7, 30),
        default_lookback_days=365 * 5,
        overlap_days=7,
    )

    assert current is None
    assert incremental == date(2026, 7, 22)
