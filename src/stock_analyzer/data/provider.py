"""Market data provider interfaces and local synthetic fallback."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

import numpy as np
import pandas as pd


class DataSourceError(RuntimeError):
    """Raised when market data cannot be fetched."""


class MarketDataProvider(Protocol):
    """Unified provider contract used by the pipeline."""

    def fetch_daily_bars(
        self,
        symbol: str,
        lookback_days: int = 120,
        *,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        """Return OHLCV dataframe indexed by trading date."""

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        """Return daily intraday summary factors indexed by trading date."""


@dataclass(slots=True)
class SyntheticProvider:
    """Deterministic random-walk provider used in tests and fallback mode."""

    seed_offset: int = 0

    def fetch_daily_bars(
        self,
        symbol: str,
        lookback_days: int = 120,
        *,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        seed = (abs(hash(symbol)) + self.seed_offset) % (2**32)
        rng = np.random.default_rng(seed)
        dates = pd.bdate_range(end=end_date or datetime.now().date(), periods=lookback_days)
        record_count = len(dates)

        close = np.cumprod(1 + rng.normal(0.0012, 0.02, size=record_count)) * 10
        open_price = close * (1 + rng.normal(0, 0.003, size=record_count))
        high = np.maximum(open_price, close) * (1 + rng.uniform(0, 0.02, size=record_count))
        low = np.minimum(open_price, close) * (1 - rng.uniform(0, 0.02, size=record_count))

        volume = rng.integers(2_000_000, 12_000_000, size=record_count).astype(float)
        turnover = volume * close
        float_market_cap = np.full(record_count, 12_000_000_000.0)

        frame = pd.DataFrame(
            {
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "turnover": turnover,
                "float_market_cap": float_market_cap,
                "suspended": False,
                "name": "",
                "is_st": False,
                "is_delisting_risk": False,
                "roe": 0.08,
                "debt_ratio": 0.55,
                "financial_data_complete": True,
                "financial_missing_fields": "",
                "financial_source": "synthetic",
                "financial_report_date": "",
                "holder_count": np.full(record_count, 60_000.0),
                "block_trade_net": np.zeros(record_count, dtype=float),
                "financing_balance": np.full(record_count, 2_500_000_000.0),
                "margin_financing_balance": np.full(record_count, 2_500_000_000.0),
                "northbound_net": np.zeros(record_count, dtype=float),
                "dragon_tiger_flag": np.zeros(record_count, dtype=float),
                "background_data_source": "synthetic",
                "background_data_complete": True,
                "board": _infer_board(symbol),
            },
            index=dates,
        )
        frame.index.name = "date"
        return frame

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        _ = symbol, interval, lookback_days
        return pd.DataFrame()


def _infer_board(symbol: str) -> str:
    text = symbol.strip()
    if text.startswith("688"):
        return "科创板"
    if text.startswith("300") or text.startswith("301"):
        return "创业板"
    if text.startswith("8") or text.startswith("4"):
        return "北交所"
    return "主板"
