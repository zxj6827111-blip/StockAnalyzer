"""Cache wrapper for market data providers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from io import StringIO

import pandas as pd

from stock_analyzer.data.provider import DataSourceError, MarketDataProvider
from stock_analyzer.infra.cache import CacheStore


@dataclass(slots=True)
class CachedProvider:
    """Provider decorator with cache-first fetch strategy."""

    inner: MarketDataProvider
    cache: CacheStore
    ttl_sec: int = 60
    key_prefix: str = "provider"
    cache_hits: int = 0
    cache_misses: int = 0
    fallback_hits: int = 0
    last_error: str = ""

    def fetch_daily_bars(
        self,
        symbol: str,
        lookback_days: int = 120,
        *,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        end_date_key = end_date.isoformat() if end_date is not None else "latest"
        cache_key = f"{self.key_prefix}:bars:{symbol}:{lookback_days}:{end_date_key}"
        cached_raw = self.cache.get(cache_key)
        if cached_raw is not None:
            self.cache_hits += 1
            return _deserialize_frame(cached_raw)

        self.cache_misses += 1
        try:
            frame = _fetch_daily_bars_compat(
                provider=self.inner,
                symbol=symbol,
                lookback_days=lookback_days,
                end_date=end_date,
            )
        except Exception as exc:
            self.last_error = str(exc)
            # Retry reading once to support race where another worker populated cache.
            fallback_raw = self.cache.get(cache_key)
            if fallback_raw is not None:
                self.fallback_hits += 1
                return _deserialize_frame(fallback_raw)
            raise DataSourceError(f"cached provider inner failed: {exc}") from exc

        self.cache.set(cache_key, _serialize_frame(frame), ttl_sec=self.ttl_sec)
        self.last_error = ""
        return frame

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        interval_key = interval.strip().lower()
        cache_key = f"{self.key_prefix}:intraday:{interval_key}:{symbol}:{lookback_days}"
        cached_raw = self.cache.get(cache_key)
        if cached_raw is not None:
            self.cache_hits += 1
            return _deserialize_frame(cached_raw)

        self.cache_misses += 1
        try:
            frame = self.inner.fetch_intraday_summary(
                symbol=symbol,
                interval=interval_key,
                lookback_days=lookback_days,
            )
        except Exception as exc:
            self.last_error = str(exc)
            fallback_raw = self.cache.get(cache_key)
            if fallback_raw is not None:
                self.fallback_hits += 1
                return _deserialize_frame(fallback_raw)
            raise DataSourceError(f"cached provider inner failed: {exc}") from exc

        self.cache.set(cache_key, _serialize_frame(frame), ttl_sec=self.ttl_sec)
        self.last_error = ""
        return frame

    def status(self) -> dict[str, object]:
        inner_status: dict[str, object] = {}
        status_method = getattr(self.inner, "status", None)
        if callable(status_method):
            payload = status_method()
            if isinstance(payload, dict):
                inner_status = payload

        return {
            **inner_status,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "cache_fallback_hits": self.fallback_hits,
            "cache_last_error": self.last_error,
        }


def _serialize_frame(frame: pd.DataFrame) -> str:
    payload = {"frame": frame.to_json(date_format="iso", orient="split")}
    return json.dumps(payload, ensure_ascii=False)


def _deserialize_frame(raw: str) -> pd.DataFrame:
    payload = json.loads(raw)
    frame_json = payload.get("frame")
    if not isinstance(frame_json, str):
        raise ValueError("invalid cached frame payload")
    frame = pd.read_json(StringIO(frame_json), orient="split")
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index)
    return frame.sort_index()


def _fetch_daily_bars_compat(
    *,
    provider: MarketDataProvider,
    symbol: str,
    lookback_days: int,
    end_date: date | None,
) -> pd.DataFrame:
    if end_date is None:
        return provider.fetch_daily_bars(symbol=symbol, lookback_days=lookback_days)
    try:
        return provider.fetch_daily_bars(
            symbol=symbol,
            lookback_days=lookback_days,
            end_date=end_date,
        )
    except TypeError as exc:
        if "end_date" not in str(exc):
            raise
        return provider.fetch_daily_bars(symbol=symbol, lookback_days=lookback_days)
