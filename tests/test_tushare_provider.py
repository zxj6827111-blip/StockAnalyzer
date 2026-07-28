from __future__ import annotations

import pandas as pd
import pytest

from stock_analyzer.data.provider import DataSourceError
from stock_analyzer.data.tushare_provider import TushareProvider, _to_ts_code


class _FakePro:
    def __init__(self, daily: pd.DataFrame, basic: pd.DataFrame | None = None) -> None:
        self._daily = daily
        self._basic = basic if basic is not None else pd.DataFrame()

    def daily(self, *, ts_code: str = "", start_date: str = "", end_date: str = "") -> object:
        _ = (ts_code, start_date, end_date)
        return self._daily

    def daily_basic(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
        fields: str = "",
    ) -> object:
        _ = (ts_code, start_date, end_date, fields)
        return self._basic

    def stock_basic(self, *, ts_code: str = "", fields: str = "") -> object:
        _ = (ts_code, fields)
        return pd.DataFrame({"ts_code": [ts_code], "name": ["浦发银行"]})


def test_to_ts_code_mapping() -> None:
    assert _to_ts_code("600000") == "600000.SH"
    assert _to_ts_code("000001") == "000001.SZ"
    assert _to_ts_code("430047") == "430047.BJ"


def test_tushare_provider_normalizes_daily_bars() -> None:
    daily = pd.DataFrame(
        {
            "ts_code": ["600000.SH", "600000.SH", "600000.SH"],
            "trade_date": ["20260710", "20260711", "20260714"],
            "open": [10.0, 10.1, 10.2],
            "high": [10.5, 10.6, 10.7],
            "low": [9.8, 9.9, 10.0],
            "close": [10.2, 10.3, 10.4],
            "vol": [1000.0, 1100.0, 1200.0],  # 手
            "amount": [1000.0, 1100.0, 1200.0],  # 千元
        }
    )
    basic = pd.DataFrame(
        {
            "ts_code": ["600000.SH", "600000.SH", "600000.SH"],
            "trade_date": ["20260710", "20260711", "20260714"],
            "turnover_rate": [1.0, 1.0, 1.0],
            "circ_mv": [1_000_000.0, 1_000_000.0, 1_200_000.0],  # 万元
        }
    )
    provider = TushareProvider(token="dummy", pro_api=_FakePro(daily, basic))
    bars = provider.fetch_daily_bars(symbol="600000", lookback_days=2)
    assert len(bars) == 2
    assert bars.index.name == "date"
    assert bars["volume"].iloc[-1] == pytest.approx(1200.0 * 100.0)
    assert bars["turnover"].iloc[-1] == pytest.approx(1200.0 * 1000.0)
    assert bars["float_market_cap"].iloc[-1] == pytest.approx(1_200_000.0 * 10000.0)
    assert bars["background_data_source"].iloc[-1] == "tushare_pro"
    assert bars["name"].iloc[-1] == "浦发银行"
    assert bars["board"].iloc[-1] == "main"


def test_tushare_provider_requires_token_without_injected_api() -> None:
    provider = TushareProvider(token="")
    provider._token = ""  # noqa: SLF001
    with pytest.raises(DataSourceError, match="token missing"):
        provider.fetch_daily_bars(symbol="600000", lookback_days=5)
