"""Hybrid runtime provider for daytime live monitoring over offline history."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as dt_time
from time import perf_counter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

from stock_analyzer.data.intraday_summary import (
    fetch_sina_minute_bars,
    summarize_minute_bars,
)
from stock_analyzer.data.provider import MarketDataProvider

_LIVE_SUPPORTED_INTERVALS = {"1m", "5m"}
_LIVE_SESSION_START = dt_time(hour=9, minute=15)
_LIVE_SESSION_END = dt_time(hour=15, minute=5)


@dataclass(slots=True)
class HybridRuntimeProvider:
    """Overlay live minute bars onto offline history during market hours."""

    base_provider: MarketDataProvider
    market_timezone: str = "Asia/Shanghai"
    live_enabled: bool = True
    live_session_only: bool = True
    live_interval_priority: tuple[str, ...] = ("1m", "5m")
    live_timeout_sec: int = 5
    live_cache_ttl_sec: float = 8.0
    now_provider: Callable[[], datetime] | None = None
    minute_fetcher: Callable[..., pd.DataFrame] = fetch_sina_minute_bars
    _live_minute_cache: dict[tuple[str, str], tuple[float, pd.DataFrame]] = field(
        default_factory=dict,
        init=False,
    )

    def fetch_daily_bars(
        self,
        symbol: str,
        lookback_days: int = 120,
        *,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        base_frame = _fetch_daily_bars_compat(
            provider=self.base_provider,
            symbol=symbol,
            lookback_days=max(1, int(lookback_days)),
            end_date=end_date,
        )
        if base_frame.empty or not self._should_use_live_overlay():
            return base_frame.tail(max(1, int(lookback_days))).copy()

        live_frame = self._load_live_minute_frame(
            symbol=symbol,
            intervals=self.live_interval_priority,
            require_today=True,
        )
        if live_frame.empty:
            return base_frame.tail(max(1, int(lookback_days))).copy()

        overlay = self._build_live_daily_overlay(base_frame=base_frame, live_frame=live_frame)
        merged = _merge_by_trade_date(base_frame, overlay)
        return merged.tail(max(1, int(lookback_days))).copy()

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        try:
            base_frame = self.base_provider.fetch_intraday_summary(
                symbol=symbol,
                interval=interval,
                lookback_days=max(1, int(lookback_days)),
            )
        except Exception:
            base_frame = pd.DataFrame()
        if not self._should_use_live_overlay():
            return base_frame.tail(max(1, int(lookback_days))).copy()

        normalized_interval = interval.strip().lower()
        if normalized_interval not in _LIVE_SUPPORTED_INTERVALS:
            return base_frame.tail(max(1, int(lookback_days))).copy()

        live_frame = self._load_live_minute_frame(
            symbol=symbol,
            intervals=self._live_overlay_intervals_for_summary(
                symbol=symbol,
                interval=normalized_interval,
            ),
            require_today=True,
        )
        if live_frame.empty:
            return base_frame.tail(max(1, int(lookback_days))).copy()

        overlay = summarize_minute_bars(live_frame, interval=normalized_interval)
        if overlay.empty:
            return base_frame.tail(max(1, int(lookback_days))).copy()

        merged = _merge_by_trade_date(base_frame, overlay)
        return merged.tail(max(1, int(lookback_days))).copy()

    def status(self) -> dict[str, object]:
        status_method = getattr(self.base_provider, "status", None)
        base_status: dict[str, object] = {}
        if callable(status_method):
            payload = status_method()
            if isinstance(payload, dict):
                base_status = payload
        return {
            **base_status,
            "live_overlay_enabled": self.live_enabled,
            "live_overlay_session_only": self.live_session_only,
            "live_overlay_intervals": list(self.live_interval_priority),
            "live_overlay_cache_ttl_sec": float(self.live_cache_ttl_sec),
            "live_overlay_timezone": self.market_timezone,
        }

    def _build_live_daily_overlay(
        self,
        *,
        base_frame: pd.DataFrame,
        live_frame: pd.DataFrame,
    ) -> pd.DataFrame:
        ordered = _normalize_intraday_frame(live_frame)
        ordered_index = pd.DatetimeIndex(ordered.index)
        latest_trade_date = ordered_index.max().normalize()
        session = ordered.loc[ordered_index.normalize() == latest_trade_date].copy()
        if session.empty:
            return pd.DataFrame()

        reference = _resolve_overlay_reference(base_frame=base_frame, trade_date=latest_trade_date)
        open_price = _as_float(session["open"].iloc[0])
        high_price = float(pd.to_numeric(session["high"], errors="coerce").max())
        low_price = float(pd.to_numeric(session["low"], errors="coerce").min())
        close_price = _as_float(session["close"].iloc[-1])
        volume = float(pd.to_numeric(session["volume"], errors="coerce").fillna(0.0).sum())
        amount_series = session.get("amount", pd.Series(dtype=float))
        turnover = float(pd.to_numeric(amount_series, errors="coerce").fillna(0.0).sum())

        payload = reference.to_dict() if reference is not None else {}
        payload["open"] = open_price
        payload["high"] = high_price
        payload["low"] = low_price
        payload["close"] = close_price
        payload["volume"] = volume
        payload["turnover"] = turnover
        payload["suspended"] = False

        if reference is not None:
            ref_close = _as_float(reference.get("close"))
            ref_float_cap = _as_float(reference.get("float_market_cap"))
            if ref_close > 0 and ref_float_cap > 0 and close_price > 0:
                payload["float_market_cap"] = ref_float_cap * close_price / ref_close
            else:
                payload["float_market_cap"] = ref_float_cap

            base_source = str(reference.get("background_data_source", "")).strip() or "offline"
            payload["background_data_source"] = f"{base_source}+live"
            financial_source = str(reference.get("financial_source", "")).strip() or "offline"
            payload["financial_source"] = financial_source

        overlay = pd.DataFrame([payload], index=[latest_trade_date])
        overlay.index.name = base_frame.index.name or "date"
        return overlay

    def _live_overlay_intervals_for_summary(
        self,
        *,
        symbol: str,
        interval: str,
    ) -> tuple[str, ...]:
        normalized_interval = interval.strip().lower()
        if normalized_interval != "5m":
            return (normalized_interval,)
        # When a 1m frame was just loaded for the same symbol, reuse it and
        # derive the 5m summary locally to avoid a second live round-trip.
        if (symbol.strip(), "1m") in self._live_minute_cache:
            return ("1m", "5m")
        return ("5m", "1m")

    def _load_live_minute_frame(
        self,
        *,
        symbol: str,
        intervals: Sequence[str],
        require_today: bool,
    ) -> pd.DataFrame:
        today = self._now_local().date()
        for interval in intervals:
            normalized_interval = interval.strip().lower()
            if normalized_interval not in _LIVE_SUPPORTED_INTERVALS:
                continue
            frame = self._fetch_cached_live_minute_frame(
                symbol=symbol,
                interval=normalized_interval,
            )
            if frame.empty:
                continue
            latest_date = frame.index.max().date()
            if require_today and latest_date != today:
                continue
            return frame
        return pd.DataFrame()

    def _fetch_cached_live_minute_frame(
        self,
        *,
        symbol: str,
        interval: str,
    ) -> pd.DataFrame:
        cache_key = (symbol.strip(), interval)
        now_perf = perf_counter()
        cached = self._live_minute_cache.get(cache_key)
        if cached is not None:
            cached_at, cached_frame = cached
            if now_perf - cached_at <= max(0.0, float(self.live_cache_ttl_sec)):
                return cached_frame.copy()
        try:
            frame = self.minute_fetcher(
                symbol=symbol,
                interval=interval,
                timeout_sec=max(1, int(self.live_timeout_sec)),
            )
        except Exception:
            frame = pd.DataFrame()
        normalized = _normalize_intraday_frame(frame)
        self._live_minute_cache[cache_key] = (now_perf, normalized)
        return normalized.copy()

    def _should_use_live_overlay(self) -> bool:
        if not self.live_enabled:
            return False
        if not self.live_session_only:
            return True
        now = self._now_local()
        if now.weekday() >= 5:
            return False
        now_time = now.time().replace(tzinfo=None)
        return _LIVE_SESSION_START <= now_time <= _LIVE_SESSION_END

    def _now_local(self) -> datetime:
        zone = _resolve_zoneinfo(self.market_timezone)
        current = self.now_provider() if self.now_provider is not None else datetime.now(zone)
        if zone is None:
            return current
        if current.tzinfo is None:
            return current.replace(tzinfo=zone)
        return current.astimezone(zone)


def _normalize_intraday_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    normalized = frame.copy()
    if not isinstance(normalized.index, pd.DatetimeIndex):
        normalized.index = pd.to_datetime(normalized.index, errors="coerce")
    normalized = normalized[normalized.index.notna()].sort_index()
    normalized = normalized[~normalized.index.duplicated(keep="last")]
    return normalized


def _merge_by_trade_date(base_frame: pd.DataFrame, overlay: pd.DataFrame) -> pd.DataFrame:
    if overlay.empty:
        return base_frame.copy()
    if base_frame.empty:
        return overlay.sort_index().copy()
    normalized_base = base_frame.copy().sort_index()
    target_dates = {timestamp.normalize() for timestamp in pd.DatetimeIndex(overlay.index)}
    keep_mask = [timestamp.normalize() not in target_dates for timestamp in normalized_base.index]
    kept = normalized_base.loc[keep_mask]
    merged = pd.concat([kept, overlay], axis=0, sort=False)
    merged = merged.sort_index()
    merged = merged[~merged.index.duplicated(keep="last")]
    return merged


def _resolve_overlay_reference(
    *,
    base_frame: pd.DataFrame,
    trade_date: pd.Timestamp,
) -> pd.Series | None:
    if base_frame.empty:
        return None
    normalized = base_frame.sort_index()
    normalized_index = pd.DatetimeIndex(normalized.index)
    historical = normalized.loc[normalized_index.normalize() < trade_date]
    if not historical.empty:
        return pd.Series(historical.iloc[-1], copy=False)
    return pd.Series(normalized.iloc[-1], copy=False)


def _resolve_zoneinfo(value: str) -> ZoneInfo | None:
    name = value.strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return None


def _as_float(value: object) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


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
