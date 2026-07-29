from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stock_analyzer.data.market_warehouse import MarketWarehouse
from stock_analyzer.data.tushare_provider import TushareProvider


class _FakeProTradeStatus:
    def __init__(
        self,
        stk_limit: pd.DataFrame | None = None,
        suspend_d: pd.DataFrame | None = None,
    ) -> None:
        self._stk_limit = stk_limit if stk_limit is not None else pd.DataFrame()
        self._suspend_d = suspend_d if suspend_d is not None else pd.DataFrame()
        self.stk_calls = 0
        self.suspend_calls = 0

    def stk_limit(self, **kwargs: object) -> object:
        self.stk_calls += 1
        return self._stk_limit

    def suspend_d(self, **kwargs: object) -> object:
        self.suspend_calls += 1
        return self._suspend_d

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


def test_fetch_trade_status_merges_limit_and_suspend() -> None:
    limit_df = pd.DataFrame({
        "ts_code": ["600000.SH", "600000.SH"],
        "trade_date": ["20240419", "20240422"],
        "up_limit": [11.0, 11.5],
        "down_limit": [9.0, 9.5],
    })
    suspend_df = pd.DataFrame({
        "ts_code": ["600000.SH"],
        "trade_date": ["20240419"],
        "suspend_type": ["重大事项"],
    })
    pro = _FakeProTradeStatus(stk_limit=limit_df, suspend_d=suspend_df)
    provider = TushareProvider(pro_api=pro)  # type: ignore[arg-type]
    out = provider.fetch_trade_status(
        "600000",
        start_date=date(2024, 4, 19),
        end_date=date(2024, 4, 22),
    )
    assert len(out) == 2
    row_19 = out[out["trade_date"] == pd.Timestamp("2024-04-19")].iloc[0]
    assert row_19["up_limit"] == pytest.approx(11.0)
    assert row_19["down_limit"] == pytest.approx(9.0)
    assert bool(row_19["suspended"]) is True
    assert row_19["suspend_type"] == "重大事项"
    row_22 = out[out["trade_date"] == pd.Timestamp("2024-04-22")].iloc[0]
    assert bool(row_22["suspended"]) is False


def test_trade_status_empty_when_no_data() -> None:
    pro = _FakeProTradeStatus()
    provider = TushareProvider(pro_api=pro)  # type: ignore[arg-type]
    out = provider.fetch_trade_status("600000")
    assert out.empty


def test_warehouse_trade_status_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "wh.duckdb"
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    wh = MarketWarehouse(db_path=db, package_root=pkg)
    wh.ensure_schema()

    status = pd.DataFrame({
        "symbol": ["600000", "600000"],
        "trade_date": pd.to_datetime(["2024-04-19", "2024-04-22"]),
        "up_limit": [11.0, 11.5],
        "down_limit": [9.0, 9.5],
        "suspended": [True, False],
        "suspend_type": ["重大事项", ""],
        "source": ["tushare_stk_limit+suspend_d", "tushare_stk_limit"],
        "as_of": ["2024-04-19", "2024-04-22"],
        "coverage_complete": [True, True],
    })
    n1 = wh.upsert_trade_status(symbol="600000", frame=status)
    n2 = wh.upsert_trade_status(symbol="600000", frame=status)
    assert n1 == 2
    assert n2 == 2
    stored = wh.fetch_trade_status(symbol="600000")
    assert len(stored) == 2


def test_apply_trade_status_to_daily(tmp_path: Path) -> None:
    db = tmp_path / "wh.duckdb"
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    wh = MarketWarehouse(db_path=db, package_root=pkg)
    wh.ensure_schema()

    daily = pd.DataFrame({
        "date": pd.to_datetime(["2024-04-19", "2024-04-22"]),
        "open": [10.0, 10.5],
        "high": [10.5, 11.0],
        "low": [9.5, 10.0],
        "close": [10.2, 10.8],
        "volume": [1e6, 1e6],
        "turnover": [1e7, 1e7],
        "float_market_cap": [1e10, 1e10],
        "suspended": [False, False],
        "name": ["测试", "测试"],
        "is_st": [False, False],
        "is_delisting_risk": [False, False],
        "roe": [np.nan, np.nan],
        "debt_ratio": [np.nan, np.nan],
        "financial_data_complete": [False, False],
        "financial_missing_fields": ["roe,debt_ratio", "roe,debt_ratio"],
        "financial_source": ["tushare_pending", "tushare_pending"],
        "financial_report_date": ["", ""],
        "financial_as_of": ["", ""],
        "financial_trust_level": ["missing", "missing"],
        "financial_completeness": [0.0, 0.0],
        "holder_count": [np.nan, np.nan],
        "block_trade_net": [np.nan, np.nan],
        "financing_balance": [np.nan, np.nan],
        "margin_financing_balance": [np.nan, np.nan],
        "northbound_net": [np.nan, np.nan],
        "dragon_tiger_flag": [np.nan, np.nan],
        "background_data_source": ["tushare_pro_qfq", "tushare_pro_qfq"],
        "background_data_complete": [False, False],
        "background_missing_fields": ["", ""],
        "background_as_of": ["", ""],
        "price_series_mode": ["qfq", "qfq"],
        "adjustment_source": ["tushare_adj_factor", "tushare_adj_factor"],
        "adjustment_anchor_date": ["2024-04-22", "2024-04-22"],
        "adjustment_anchor_factor": [1.0, 1.0],
        "board": ["main", "main"],
    }).set_index("date")
    wh.replace_daily_bars(symbol="600000", frame=daily)

    status = pd.DataFrame({
        "symbol": ["600000", "600000"],
        "trade_date": pd.to_datetime(["2024-04-19", "2024-04-22"]),
        "up_limit": [11.0, 11.5],
        "down_limit": [9.0, 9.5],
        "suspended": [True, False],
        "suspend_type": ["重大事项", ""],
        "source": ["tushare_stk_limit+suspend_d", "tushare_stk_limit"],
        "as_of": ["2024-04-19", "2024-04-22"],
        "coverage_complete": [True, True],
    })
    wh.upsert_trade_status(symbol="600000", frame=status)
    enriched = wh.apply_trade_status_to_daily(symbol="600000")
    assert bool(enriched.loc[pd.Timestamp("2024-04-19"), "suspended"]) is True
    assert bool(enriched.loc[pd.Timestamp("2024-04-22"), "suspended"]) is False
    assert float(enriched.loc[pd.Timestamp("2024-04-19"), "up_limit"]) == pytest.approx(11.0)
    assert float(enriched.loc[pd.Timestamp("2024-04-22"), "down_limit"]) == pytest.approx(9.5)
