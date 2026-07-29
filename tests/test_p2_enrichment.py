from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stock_analyzer.data.market_warehouse import MarketWarehouse
from stock_analyzer.data.tushare_provider import TushareProvider


class _FakeProP2:
    def __init__(
        self,
        margin: pd.DataFrame | None = None,
        moneyflow: pd.DataFrame | None = None,
        hk_hold: pd.DataFrame | None = None,
    ) -> None:
        self._margin = margin if margin is not None else pd.DataFrame()
        self._moneyflow = moneyflow if moneyflow is not None else pd.DataFrame()
        self._hk_hold = hk_hold if hk_hold is not None else pd.DataFrame()

    def margin_detail(self, **kwargs: object) -> object:
        return self._margin

    def moneyflow(self, **kwargs: object) -> object:
        return self._moneyflow

    def hk_hold(self, **kwargs: object) -> object:
        return self._hk_hold

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


def test_fetch_margin_detail_normalizes_fields() -> None:
    raw = pd.DataFrame({
        "ts_code": ["600000.SH"],
        "trade_date": ["20240419"],
        "rzye": [1_000_000_000.0],
        "rzmre": [50_000_000.0],
        "rqye": [200_000_000.0],
        "rqyl": [100_000.0],
        "rqmcl": [5000.0],
    })
    pro = _FakeProP2(margin=raw)
    provider = TushareProvider(pro_api=pro)  # type: ignore[arg-type]
    out = provider.fetch_margin_detail("600000", end_date=date(2024, 4, 19))
    assert len(out) == 1
    row = out.iloc[0]
    assert row["financing_balance"] == pytest.approx(1e9)
    assert row["financing_buy_amount"] == pytest.approx(5e7)
    assert row["securities_lending_balance"] == pytest.approx(2e8)
    assert row["securities_lending_volume"] == pytest.approx(1e5)
    assert row["securities_lending_sell_volume"] == pytest.approx(5000.0)
    assert row["source"] == "tushare_margin_detail"


def test_fetch_moneyflow_derives_net_mf() -> None:
    raw = pd.DataFrame({
        "ts_code": ["600000.SH"],
        "trade_date": ["20240419"],
        "buy_sm_amount": [100.0],
        "sell_sm_amount": [80.0],
        "buy_md_amount": [200.0],
        "sell_md_amount": [150.0],
        "buy_lg_amount": [500.0],
        "sell_lg_amount": [300.0],
        "buy_elg_amount": [1000.0],
        "sell_elg_amount": [600.0],
    })
    pro = _FakeProP2(moneyflow=raw)
    provider = TushareProvider(pro_api=pro)  # type: ignore[arg-type]
    out = provider.fetch_moneyflow("600000", end_date=date(2024, 4, 19))
    assert len(out) == 1
    # net_mf = buy_lg + buy_elg - sell_lg - sell_elg = 500 + 1000 - 300 - 600 = 600
    assert out.iloc[0]["net_mf_amount"] == pytest.approx(600.0)


def test_moneyflow_nan_when_component_missing() -> None:
    raw = pd.DataFrame({
        "ts_code": ["600000.SH"],
        "trade_date": ["20240419"],
        "buy_lg_amount": [500.0],
        "sell_lg_amount": [np.nan],
        "buy_elg_amount": [1000.0],
        "sell_elg_amount": [600.0],
    })
    pro = _FakeProP2(moneyflow=raw)
    provider = TushareProvider(pro_api=pro)  # type: ignore[arg-type]
    out = provider.fetch_moneyflow("600000", end_date=date(2024, 4, 19))
    assert len(out) == 1
    assert np.isnan(out.iloc[0]["net_mf_amount"])


def test_fetch_hk_hold_normalizes() -> None:
    raw = pd.DataFrame({
        "ts_code": ["600000.SH"],
        "trade_date": ["20240419"],
        "vol": [50_000_000.0],
        "ratio": [0.05],
        "market_cap": [5e9],
    })
    pro = _FakeProP2(hk_hold=raw)
    provider = TushareProvider(pro_api=pro)  # type: ignore[arg-type]
    out = provider.fetch_hk_hold("600000", end_date=date(2024, 4, 19))
    assert len(out) == 1
    assert out.iloc[0]["hold_vol"] == pytest.approx(5e7)
    assert out.iloc[0]["hold_ratio"] == pytest.approx(0.05)
    assert out.iloc[0]["hold_market_cap"] == pytest.approx(5e9)


def test_hk_hold_empty_means_no_coverage() -> None:
    pro = _FakeProP2(hk_hold=pd.DataFrame())
    provider = TushareProvider(pro_api=pro)  # type: ignore[arg-type]
    out = provider.fetch_hk_hold("600000")
    assert out.empty


def test_warehouse_p2_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "wh.duckdb"
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    wh = MarketWarehouse(db_path=db, package_root=pkg)
    wh.ensure_schema()

    margin = pd.DataFrame({
        "symbol": ["600000"],
        "trade_date": pd.to_datetime(["2024-04-19"]),
        "financing_balance": [1e9],
        "financing_buy_amount": [5e7],
        "securities_lending_balance": [2e8],
        "securities_lending_volume": [1e5],
        "securities_lending_sell_volume": [5000.0],
        "source": ["tushare_margin_detail"],
        "as_of": ["2024-04-19"],
        "coverage_complete": [True],
    })
    n1 = wh.upsert_margin_detail(symbol="600000", frame=margin)
    n2 = wh.upsert_margin_detail(symbol="600000", frame=margin)
    assert n1 == 1
    assert n2 == 1
    stored = wh.fetch_margin_detail(symbol="600000")
    assert len(stored) == 1
    assert stored.iloc[0]["financing_balance"] == pytest.approx(1e9)

    mf = pd.DataFrame({
        "symbol": ["600000"],
        "trade_date": pd.to_datetime(["2024-04-19"]),
        "buy_lg_amount": [500.0],
        "sell_lg_amount": [300.0],
        "buy_elg_amount": [1000.0],
        "sell_elg_amount": [600.0],
        "net_mf_amount": [600.0],
        "source": ["tushare_moneyflow"],
        "as_of": ["2024-04-19"],
        "coverage_complete": [True],
    })
    wh.upsert_moneyflow(symbol="600000", frame=mf)
    stored_mf = wh.fetch_moneyflow(symbol="600000")
    assert len(stored_mf) == 1
    assert stored_mf.iloc[0]["net_mf_amount"] == pytest.approx(600.0)

    hk = pd.DataFrame({
        "symbol": ["600000"],
        "trade_date": pd.to_datetime(["2024-04-19"]),
        "hold_vol": [5e7],
        "hold_ratio": [0.05],
        "hold_market_cap": [5e9],
        "source": ["tushare_hk_hold"],
        "as_of": ["2024-04-19"],
        "coverage_complete": [True],
    })
    wh.upsert_hk_hold(symbol="600000", frame=hk)
    stored_hk = wh.fetch_hk_hold(symbol="600000")
    assert len(stored_hk) == 1
    assert stored_hk.iloc[0]["hold_ratio"] == pytest.approx(0.05)
