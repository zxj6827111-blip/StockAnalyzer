from __future__ import annotations

import sys
from types import ModuleType
from typing import Any, cast

import pandas as pd

from stock_analyzer.data.akshare_provider import AkshareProvider
from stock_analyzer.data.background_adapter import AkshareBackgroundAdapter
from stock_analyzer.data.financial_adapter import AkshareFinancialAdapter


class _FakeFinancialAdapter:
    def fetch_snapshot(self, symbol: str) -> dict[str, object]:
        _ = symbol
        return {
            "name": "Demo",
            "is_st": False,
            "is_delisting_risk": False,
            "roe": 0.12,
            "debt_ratio": 0.34,
            "financial_data_complete": True,
            "missing_fields": [],
            "source": "fake",
            "latest_report_date": "2025-12-31",
        }


class _FakeBackgroundAdapter:
    def enrich_bars(self, symbol: str, bars: pd.DataFrame) -> pd.DataFrame:
        _ = symbol
        enriched = bars.copy()
        enriched["holder_count"] = 1000.0
        enriched["block_trade_net"] = 0.0
        enriched["financing_balance"] = 1_000_000.0
        enriched["margin_financing_balance"] = 1_000_000.0
        enriched["northbound_net"] = 0.0
        enriched["dragon_tiger_flag"] = 0.0
        enriched["background_data_source"] = "fake"
        enriched["background_data_complete"] = True
        return enriched


class _FakeAkshare:
    def stock_zh_a_hist(
        self,
        *,
        symbol: str,
        period: str,
        start_date: str,
        end_date: str,
        adjust: str,
    ) -> pd.DataFrame:
        _ = (symbol, period, start_date, end_date, adjust)
        raise RuntimeError("eastmoney blocked")

    def stock_zh_a_hist_tx(
        self,
        *,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        _ = (symbol, start_date, end_date)
        return pd.DataFrame(
            {
                "date": [1767571200000, 1767657600000, 1767744000000],
                "open": [10.0, 10.2, 10.4],
                "close": [10.1, 10.3, 10.5],
                "high": [10.2, 10.4, 10.6],
                "low": [9.9, 10.1, 10.3],
                "amount": [100_000.0, 120_000.0, 150_000.0],
            }
        )


def test_akshare_provider_falls_back_to_tx_history() -> None:
    provider = AkshareProvider(
        financial_adapter=cast(AkshareFinancialAdapter, _FakeFinancialAdapter()),
        background_adapter=cast(AkshareBackgroundAdapter, _FakeBackgroundAdapter()),
        max_attempts=1,
    )
    fake_ak = _FakeAkshare()
    fake_module = ModuleType("akshare")
    cast(Any, fake_module).stock_zh_a_hist = fake_ak.stock_zh_a_hist
    cast(Any, fake_module).stock_zh_a_hist_tx = fake_ak.stock_zh_a_hist_tx
    original_module = sys.modules.get("akshare")
    sys.modules["akshare"] = fake_module
    try:
        bars = provider.fetch_daily_bars(symbol="000001", lookback_days=2)
    finally:
        if original_module is None:
            sys.modules.pop("akshare", None)
        else:
            sys.modules["akshare"] = original_module

    assert len(bars) == 2
    assert bars.index.name == "date"
    assert bars["turnover"].iloc[-1] == 10.5 * 150_000.0 * 100.0
    assert bars["financial_source"].iloc[-1] == "akshare_tx_default"
    assert bars["background_data_source"].iloc[-1] == "akshare_tx_default"
    assert bars["board"].iloc[-1] == "main"
