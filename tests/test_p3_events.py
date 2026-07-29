from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stock_analyzer.data.market_warehouse import MarketWarehouse
from stock_analyzer.data.tushare_provider import TushareProvider


class _FakeProP3:
    def __init__(
        self,
        top_list: pd.DataFrame | None = None,
        top_inst: pd.DataFrame | None = None,
        block_trade: pd.DataFrame | None = None,
    ) -> None:
        self._top_list = top_list if top_list is not None else pd.DataFrame()
        self._top_inst = top_inst if top_inst is not None else pd.DataFrame()
        self._block_trade = block_trade if block_trade is not None else pd.DataFrame()

    def top_list(self, **kwargs: object) -> object:
        return self._top_list

    def top_inst(self, **kwargs: object) -> object:
        return self._top_inst

    def block_trade(self, **kwargs: object) -> object:
        return self._block_trade

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


def test_fetch_top_list_event_day() -> None:
    raw = pd.DataFrame({
        "ts_code": ["600000.SH", "600000.SH"],
        "trade_date": ["20240419", "20240419"],
        "reason": ["涨幅偏离7%", "换手率达20%"],
        "buy": [1e8, 5e7],
        "sell": [3e7, 2e7],
        "amount": [2e9, 1.5e9],
    })
    pro = _FakeProP3(top_list=raw)
    provider = TushareProvider(pro_api=pro)  # type: ignore[arg-type]
    out = provider.fetch_top_list("600000", end_date=date(2024, 4, 19))
    assert len(out) == 1
    row = out.iloc[0]
    assert row["dragon_tiger_flag"] == 1.0
    assert row["reason_count"] == 2
    assert "涨幅偏离7%" in row["reasons"]
    assert row["buy_amount"] == pytest.approx(1.5e8)
    assert row["sell_amount"] == pytest.approx(5e7)


def test_top_list_empty_means_no_event() -> None:
    pro = _FakeProP3(top_list=pd.DataFrame())
    provider = TushareProvider(pro_api=pro)  # type: ignore[arg-type]
    out = provider.fetch_top_list("600000")
    assert out.empty


def test_fetch_top_inst() -> None:
    raw = pd.DataFrame({
        "ts_code": ["600000.SH"],
        "trade_date": ["20240419"],
        "exalter": ["机构专用"],
        "buy": [5e7],
        "sell": [1e7],
        "net_buy": [4e7],
    })
    pro = _FakeProP3(top_inst=raw)
    provider = TushareProvider(pro_api=pro)  # type: ignore[arg-type]
    out = provider.fetch_top_inst("600000", end_date=date(2024, 4, 19))
    assert len(out) == 1
    assert out.iloc[0]["institution_name"] == "机构专用"
    assert out.iloc[0]["inst_net_amount"] == pytest.approx(4e7)


def test_fetch_block_trade_premium_discount() -> None:
    raw = pd.DataFrame({
        "ts_code": ["600000.SH"],
        "trade_date": ["20240419"],
        "price": [10.5],
        "vol": [1000.0],  # 手
        "amount": [1.05e7],
        "close": [10.0],
        "buyer": ["买方A"],
        "seller": ["卖方B"],
    })
    pro = _FakeProP3(block_trade=raw)
    provider = TushareProvider(pro_api=pro)  # type: ignore[arg-type]
    out = provider.fetch_block_trade("600000", end_date=date(2024, 4, 19))
    assert len(out) == 1
    row = out.iloc[0]
    assert row["block_trade_volume"] == pytest.approx(100000.0)  # 手->股
    assert row["block_trade_amount"] == pytest.approx(1.05e7)
    assert row["block_trade_premium_discount"] == pytest.approx(0.05)
    assert np.isnan(row["block_trade_net"])  # no reliable direction
    assert row["buyer"] == "买方A"
    assert row["seller"] == "卖方B"


def test_block_trade_net_stays_nan_without_direction() -> None:
    raw = pd.DataFrame({
        "ts_code": ["600000.SH"],
        "trade_date": ["20240419"],
        "price": [10.5],
        "vol": [1000.0],
        "amount": [1.05e7],
    })
    pro = _FakeProP3(block_trade=raw)
    provider = TushareProvider(pro_api=pro)  # type: ignore[arg-type]
    out = provider.fetch_block_trade("600000", end_date=date(2024, 4, 19))
    assert len(out) == 1
    assert np.isnan(out.iloc[0]["block_trade_net"])


def test_warehouse_p3_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "wh.duckdb"
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    wh = MarketWarehouse(db_path=db, package_root=pkg)
    wh.ensure_schema()

    tl = pd.DataFrame({
        "symbol": ["600000"],
        "trade_date": pd.to_datetime(["2024-04-19"]),
        "dragon_tiger_flag": [1.0],
        "reason_count": [2],
        "reasons": ["涨幅偏离7%|换手率达20%"],
        "buy_amount": [1.5e8],
        "sell_amount": [5e7],
        "turnover": [3.5e9],
        "source": ["tushare_top_list"],
        "as_of": ["2024-04-19"],
        "coverage_complete": [True],
    })
    n1 = wh.upsert_top_list_events(symbol="600000", frame=tl)
    n2 = wh.upsert_top_list_events(symbol="600000", frame=tl)
    assert n1 == 1
    assert n2 == 1
    stored = wh.fetch_top_list_events(symbol="600000")
    assert len(stored) == 1
    assert stored.iloc[0]["dragon_tiger_flag"] == 1.0

    bt = pd.DataFrame({
        "symbol": ["600000"],
        "trade_date": pd.to_datetime(["2024-04-19"]),
        "block_price": [10.5],
        "block_trade_volume": [100000.0],
        "block_trade_amount": [1.05e7],
        "block_trade_premium_discount": [0.05],
        "block_trade_net": [np.nan],
        "buyer": ["买方A"],
        "seller": ["卖方B"],
        "source": ["tushare_block_trade"],
        "as_of": ["2024-04-19"],
        "coverage_complete": [True],
    })
    wh.upsert_block_trade_events(symbol="600000", frame=bt)
    stored_bt = wh.fetch_block_trade_events(symbol="600000")
    assert len(stored_bt) == 1
    assert stored_bt.iloc[0]["block_trade_premium_discount"] == pytest.approx(0.05)
    assert np.isnan(stored_bt.iloc[0]["block_trade_net"])
