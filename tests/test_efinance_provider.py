from __future__ import annotations

import pandas as pd
import pytest

from stock_analyzer.data.efinance_provider import EfinanceProvider, _EfinanceStockApi
from stock_analyzer.data.provider import DataSourceError


class _FakeStockApi:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def get_quote_history(
        self,
        symbol: str,
        *,
        beg: str,
        end: str,
        klt: int,
        fqt: int,
        suppress_error: bool,
    ) -> object:
        _ = (symbol, beg, end, klt, fqt, suppress_error)
        return self._payload


class _FakeEfinance:
    def __init__(self, payload: object) -> None:
        self.stock: _EfinanceStockApi = _FakeStockApi(payload)


def test_efinance_provider_normalizes_daily_bars() -> None:
    raw = pd.DataFrame(
        {
            "name": ["*ST Demo", "*ST Demo", "*ST Demo"],
            "code": ["600000", "600000", "600000"],
            "date": ["2026-02-27", "2026-02-28", "2026-03-02"],
            "open": [10.0, 10.2, 10.3],
            "close": [10.1, 10.3, 10.4],
            "high": [10.2, 10.4, 10.5],
            "low": [9.9, 10.1, 10.2],
            "volume": [1_000_000, 1_100_000, 1_200_000],
            "turnover": [10_000_000.0, 11_300_000.0, 12_480_000.0],
            "amp": [1.0, 1.1, 1.2],
            "chg_pct": [0.2, 0.3, 0.4],
            "chg_amt": [0.1, 0.1, 0.1],
            "turnover_rate": [10.0, 10.0, 10.0],
        }
    )
    provider = EfinanceProvider(ef_module=_FakeEfinance({"600000": raw}))
    bars = provider.fetch_daily_bars(symbol="600000", lookback_days=2)

    assert len(bars) == 2
    assert bars.index.name == "date"
    assert "float_market_cap" in bars.columns
    assert bars["float_market_cap"].iloc[-1] == pytest.approx(124_800_000.0)
    assert bool(bars["is_st"].iloc[-1]) is True
    assert bars["board"].iloc[-1] == "main"


def test_efinance_provider_raises_when_required_columns_missing() -> None:
    raw = pd.DataFrame({"col_a": [1, 2], "col_b": [3, 4]})
    provider = EfinanceProvider(ef_module=_FakeEfinance(raw))
    with pytest.raises(DataSourceError):
        provider.fetch_daily_bars(symbol="600000", lookback_days=30)


def test_efinance_provider_handles_duplicate_dates_with_turnover_rate() -> None:
    raw = pd.DataFrame(
        {
            "name": ["Demo", "Demo", "Demo"],
            "code": ["600000", "600000", "600000"],
            "date": ["2026-03-01", "2026-03-01", "2026-03-02"],
            "open": [10.0, 10.1, 10.2],
            "close": [10.1, 10.2, 10.3],
            "high": [10.2, 10.3, 10.4],
            "low": [9.9, 10.0, 10.1],
            "volume": [1_000_000, 1_050_000, 1_100_000],
            "turnover": [10_000_000.0, 10_500_000.0, 11_330_000.0],
            "turnover_rate": [10.0, 10.5, 10.0],
        }
    )
    provider = EfinanceProvider(ef_module=_FakeEfinance({"600000": raw}))

    bars = provider.fetch_daily_bars(symbol="600000", lookback_days=5)

    assert len(bars) == 2
    assert bars.index.name == "date"
    assert bars["float_market_cap"].iloc[-1] == pytest.approx(113_300_000.0)
