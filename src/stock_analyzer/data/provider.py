"""Market data provider interfaces and local synthetic fallback."""

from __future__ import annotations

import zlib
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, cast

import numpy as np
import pandas as pd


class DataSourceError(RuntimeError):
    """Raised when market data cannot be fetched."""


class FutureDataLeakError(DataSourceError):
    """Raised by the as-of backtest path when a fetched frame contains rows
    dated after the requested ``as_of`` cutoff.

    This is the core correctness guard for historical backtesting: every
    provider in the chain (vendor ZIP overlay, market warehouse, cached/
    resilient wrappers) is expected to honor ``end_date`` and truncate its
    result accordingly. If a bug or a misconfigured provider ever returns
    rows beyond the cutoff, this exception must fire immediately rather than
    silently letting future information leak into an as-of scan -- a
    dedicated unit test asserts this by injecting a provider that returns
    future rows on purpose.
    """

    def __init__(
        self,
        message: str,
        *,
        symbol: str = "",
        as_of: date | None = None,
        actual_max_date: date | None = None,
    ) -> None:
        super().__init__(message)
        self.symbol = symbol
        self.as_of = as_of
        self.actual_max_date = actual_max_date


class RequiredIntradayDataError(DataSourceError):
    """Raised when required pre-aggregated intraday data is unavailable."""

    def __init__(
        self,
        message: str,
        *,
        missing_symbols: Collection[str] | None = None,
        partial_frames: dict[str, pd.DataFrame] | None = None,
    ) -> None:
        super().__init__(message)
        self.missing_symbols = tuple(
            sorted(
                {str(symbol).strip() for symbol in (missing_symbols or ()) if str(symbol).strip()}
            )
        )
        self.partial_frames = {
            str(symbol).strip(): frame.copy()
            for symbol, frame in (partial_frames or {}).items()
            if str(symbol).strip() and isinstance(frame, pd.DataFrame)
        }


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

    def fetch_intraday_summaries(
        self,
        symbols: list[str],
        interval: str,
        lookback_days: int = 120,
    ) -> dict[str, pd.DataFrame]:
        """Return summaries for a symbol batch using one backend query."""


def fetch_intraday_summaries_compat(
    provider: MarketDataProvider,
    *,
    symbols: list[str],
    interval: str,
    lookback_days: int = 120,
) -> dict[str, pd.DataFrame]:
    """Use the provider batch API when available, otherwise preserve legacy behavior."""
    batch_method = getattr(provider, "fetch_intraday_summaries", None)
    if callable(batch_method):
        payload = batch_method(
            symbols=symbols,
            interval=interval,
            lookback_days=lookback_days,
        )
        if isinstance(payload, dict):
            return cast(dict[str, pd.DataFrame], payload)
    return {
        symbol: provider.fetch_intraday_summary(
            symbol=symbol,
            interval=interval,
            lookback_days=lookback_days,
        )
        for symbol in symbols
    }


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
        seed = _stable_synthetic_seed(symbol=symbol, seed_offset=self.seed_offset)
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
                "roe": np.nan,
                "debt_ratio": np.nan,
                "financial_data_complete": False,
                "financial_missing_fields": "roe,debt_ratio",
                "financial_source": "synthetic",
                "financial_report_date": "",
                "financial_trust_level": "synthetic",
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

    def fetch_intraday_summaries(
        self,
        symbols: list[str],
        interval: str,
        lookback_days: int = 120,
    ) -> dict[str, pd.DataFrame]:
        _ = interval, lookback_days
        return {str(symbol): pd.DataFrame() for symbol in symbols}


def _stable_synthetic_seed(*, symbol: str, seed_offset: int) -> int:
    symbol_hash = zlib.crc32(symbol.encode("utf-8"))
    return (symbol_hash + seed_offset) % (2**32)


def _infer_board(symbol: str) -> str:
    text = symbol.strip()
    if text.startswith("688"):
        return "科创板"
    if text.startswith("300") or text.startswith("301"):
        return "创业板"
    if text.startswith("8") or text.startswith("4"):
        return "北交所"
    return "主板"
