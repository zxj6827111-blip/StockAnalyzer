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
            return _deserialize_frame(cached_raw, legacy_index_name="date")

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
                return _deserialize_frame(fallback_raw, legacy_index_name="date")
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

    def latest_daily_dates(
        self,
        *,
        symbols: list[str] | None = None,
    ) -> dict[str, date] | None:
        """Transparent pass-through so the incremental snapshot layer can drive
        date-based dirtiness through the cache wrapper.

        Returns ``None`` when the inner chain has NO date interface: callers
        must then treat the wrapper as date-incapable (fall back to probing)
        instead of mistaking an empty dict for "no symbol has a record",
        which would mark every candidate dirty.
        """
        inner = getattr(self.inner, "latest_daily_dates", None)
        if not callable(inner):
            return None
        result = inner(symbols=symbols)
        return result if isinstance(result, dict) else None


def _serialize_frame(frame: pd.DataFrame) -> str:
    # JSON split 格式不携带 index name；显式保存以便反序列化恢复
    # （下游依赖 index.name == "date"，例如 snapshot 增量窗口拼接）。
    payload = {
        "frame": frame.to_json(date_format="iso", orient="split"),
        "index_name": str(frame.index.name or ""),
    }
    return json.dumps(payload, ensure_ascii=False)


def _deserialize_frame(
    raw: str,
    *,
    legacy_index_name: str | None = None,
) -> pd.DataFrame:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("invalid cached frame payload")
    frame_json = payload.get("frame")
    legacy_payload = False
    if isinstance(frame_json, str):
        # Current/HEAD format: {"frame": split_json, ["index_name"]}.
        # HEAD entries without index_name are named by the typed caller.
        frame = pd.read_json(StringIO(frame_json), orient="split")
        index_name = payload.get("index_name")
        legacy_payload = "index_name" not in payload
    else:
        # Legacy format: the cache value is the raw orient="split" JSON.
        if "columns" not in payload and "data" not in payload:
            raise ValueError("invalid cached frame payload")
        frame = pd.read_json(StringIO(raw), orient="split")
        index_name = None
        legacy_payload = True
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index)
    if isinstance(index_name, str) and index_name:
        frame.index.name = index_name
    elif legacy_payload and legacy_index_name:
        frame.index.name = legacy_index_name
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
