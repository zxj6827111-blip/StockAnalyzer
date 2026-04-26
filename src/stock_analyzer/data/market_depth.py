"""Realtime five-level market depth providers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from time import perf_counter
from typing import Any, Protocol, cast

import pandas as pd


class MarketDepthError(RuntimeError):
    """Raised when market depth data cannot be fetched."""


class MarketDepthProvider(Protocol):
    """Provider contract for batched five-level snapshots."""

    def fetch_snapshots(
        self,
        symbols: list[str],
        *,
        force_refresh: bool = False,
    ) -> dict[str, dict[str, object]]:
        """Return normalized five-level snapshots keyed by symbol."""

    def status(self) -> dict[str, object]:
        """Return provider status for runtime diagnostics."""


class EasyQuotationClient(Protocol):
    def real(
        self,
        symbols: list[str],
        *,
        prefix: bool = False,
    ) -> object:
        """Fetch raw market depth snapshots."""


class MootdxQuotesClient(Protocol):
    def quotes(self, *, symbol: list[str]) -> object:
        """Fetch raw mootdx quotes dataframe."""


@dataclass(slots=True)
class EmptyMarketDepthProvider:
    """No-op provider used when market depth is disabled."""

    def fetch_snapshots(
        self,
        symbols: list[str],
        *,
        force_refresh: bool = False,
    ) -> dict[str, dict[str, object]]:
        _ = symbols, force_refresh
        return {}

    def status(self) -> dict[str, object]:
        return {
            "enabled": False,
            "provider": "disabled",
        }


@dataclass(slots=True)
class CachedMarketDepthProvider:
    """Small TTL cache to reduce repetitive depth requests."""

    inner: MarketDepthProvider
    ttl_sec: float = 5.0
    _cache: dict[str, tuple[float, dict[str, object]]] = field(default_factory=dict, init=False)
    cache_hits: int = 0
    cache_misses: int = 0

    def fetch_snapshots(
        self,
        symbols: list[str],
        *,
        force_refresh: bool = False,
    ) -> dict[str, dict[str, object]]:
        clean_symbols = _dedupe_symbols(symbols)
        if not clean_symbols:
            return {}
        now_perf = perf_counter()
        results: dict[str, dict[str, object]] = {}
        pending: list[str] = []
        for symbol in clean_symbols:
            cached = self._cache.get(symbol)
            if (
                not force_refresh
                and cached is not None
                and now_perf - cached[0] <= max(0.0, float(self.ttl_sec))
            ):
                self.cache_hits += 1
                results[symbol] = dict(cached[1])
                continue
            self.cache_misses += 1
            pending.append(symbol)
        if pending:
            fetched = self.inner.fetch_snapshots(pending, force_refresh=force_refresh)
            for symbol in pending:
                payload = dict(fetched.get(symbol, _empty_snapshot(symbol)))
                self._cache[symbol] = (now_perf, payload)
                results[symbol] = dict(payload)
        return results

    def clear(self, symbols: list[str] | None = None) -> None:
        if not symbols:
            self._cache.clear()
            return
        for symbol in _dedupe_symbols(symbols):
            self._cache.pop(symbol, None)

    def status(self) -> dict[str, object]:
        inner_status = self.inner.status()
        return {
            **inner_status,
            "cache_ttl_sec": float(self.ttl_sec),
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
        }


@dataclass(slots=True)
class FallbackMarketDepthProvider:
    """Use backup provider for failures or symbol-level gaps."""

    primary: MarketDepthProvider
    backup: MarketDepthProvider | None = None
    last_error: str = ""
    last_source: str = ""

    def fetch_snapshots(
        self,
        symbols: list[str],
        *,
        force_refresh: bool = False,
    ) -> dict[str, dict[str, object]]:
        clean_symbols = _dedupe_symbols(symbols)
        if not clean_symbols:
            return {}
        primary_result: dict[str, dict[str, object]] = {}
        try:
            primary_result = self.primary.fetch_snapshots(
                clean_symbols,
                force_refresh=force_refresh,
            )
            self.last_error = ""
            self.last_source = "primary"
        except Exception as exc:
            self.last_error = str(exc)
            if self.backup is None:
                raise
            primary_result = {}
        missing = [
            symbol
            for symbol in clean_symbols
            if not bool(primary_result.get(symbol, {}).get("available", False))
        ]
        if missing and self.backup is not None:
            try:
                backup_result = self.backup.fetch_snapshots(missing, force_refresh=force_refresh)
            except Exception as exc:
                if not primary_result:
                    raise MarketDepthError(f"primary and backup failed: {exc}") from exc
                self.last_error = str(exc)
                backup_result = {}
            else:
                if backup_result:
                    self.last_source = "mixed" if primary_result else "backup"
            merged = dict(primary_result)
            for symbol in missing:
                if bool(backup_result.get(symbol, {}).get("available", False)):
                    merged[symbol] = backup_result[symbol]
            primary_result = merged
        return primary_result

    def status(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "provider": "fallback",
            "last_error": self.last_error,
            "last_source": self.last_source,
            "primary": self.primary.status(),
        }
        if self.backup is not None:
            payload["backup"] = self.backup.status()
        return payload


@dataclass(slots=True)
class EasyQuotationMarketDepthProvider:
    """Five-level snapshot provider backed by easyquotation + Sina."""

    source_name: str = "easyquotation_sina"
    _client: EasyQuotationClient | None = field(default=None, init=False)
    _quotation_factory: Callable[[], EasyQuotationClient] | None = None
    last_error: str = ""

    def fetch_snapshots(
        self,
        symbols: list[str],
        *,
        force_refresh: bool = False,
    ) -> dict[str, dict[str, object]]:
        _ = force_refresh
        clean_symbols = _dedupe_symbols(symbols)
        if not clean_symbols:
            return {}
        client = self._resolve_client()
        prefixed_symbols = [_to_prefixed_symbol(symbol) for symbol in clean_symbols]
        try:
            raw = client.real(prefixed_symbols, prefix=True)
        except Exception as exc:  # pragma: no cover - depends on provider runtime.
            self.last_error = str(exc)
            raise MarketDepthError(f"easyquotation request failed: {exc}") from exc
        self.last_error = ""
        result: dict[str, dict[str, object]] = {}
        for symbol, prefixed in zip(clean_symbols, prefixed_symbols, strict=False):
            payload = raw.get(prefixed) if isinstance(raw, dict) else None
            if isinstance(payload, dict):
                result[symbol] = _normalize_depth_payload(
                    symbol=symbol,
                    payload=payload,
                    source=self.source_name,
                    price_keys=("now", "price"),
                    prev_close_keys=("close", "last_close", "yesterday_close"),
                    open_keys=("open",),
                    bid_price_keys=[(f"bid{level}",) for level in range(1, 6)],
                    bid_volume_keys=[
                        (f"bid{level}_volume", f"bid_vol{level}")
                        for level in range(1, 6)
                    ],
                    ask_price_keys=[(f"ask{level}",) for level in range(1, 6)],
                    ask_volume_keys=[
                        (f"ask{level}_volume", f"ask_vol{level}")
                        for level in range(1, 6)
                    ],
                    name_keys=("name",),
                    timestamp_keys=("datetime", "timestamp"),
                    date_key="date",
                    time_key="time",
                )
        return result

    def status(self) -> dict[str, object]:
        return {
            "enabled": True,
            "provider": self.source_name,
            "last_error": self.last_error,
        }

    def _resolve_client(self) -> EasyQuotationClient:
        if self._client is not None:
            return self._client
        if self._quotation_factory is not None:
            self._client = self._quotation_factory()
            return self._client
        try:
            import easyquotation  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - depends on local env.
            raise MarketDepthError("easyquotation is not installed") from exc
        self._client = cast(EasyQuotationClient, easyquotation.use("sina"))
        return self._client


@dataclass(slots=True)
class MootdxMarketDepthProvider:
    """Five-level snapshot provider backed by mootdx."""

    source_name: str = "mootdx"
    timeout_sec: int = 5
    _client: MootdxQuotesClient | None = field(default=None, init=False)
    _quotes_factory: Callable[[], MootdxQuotesClient] | None = None
    last_error: str = ""

    def fetch_snapshots(
        self,
        symbols: list[str],
        *,
        force_refresh: bool = False,
    ) -> dict[str, dict[str, object]]:
        _ = force_refresh
        clean_symbols = _dedupe_symbols(symbols)
        if not clean_symbols:
            return {}
        client = self._resolve_client()
        try:
            frame = client.quotes(symbol=clean_symbols)
        except Exception as exc:  # pragma: no cover - depends on provider runtime.
            self.last_error = str(exc)
            raise MarketDepthError(f"mootdx request failed: {exc}") from exc
        self.last_error = ""
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return {}
        result: dict[str, dict[str, object]] = {}
        for record in frame.to_dict(orient="records"):
            if not isinstance(record, dict):
                continue
            symbol = _normalize_symbol(
                record.get("code")
                or record.get("symbol")
                or record.get("stock_code")
                or record.get("stock")
            )
            if not symbol:
                continue
            normalized_record = {str(key): value for key, value in record.items()}
            result[symbol] = _normalize_depth_payload(
                symbol=symbol,
                payload=normalized_record,
                source=self.source_name,
                price_keys=("price", "now", "last_price"),
                prev_close_keys=("last_close", "close", "prev_close"),
                open_keys=("open",),
                bid_price_keys=[
                    (f"bid{level}", f"buy{level}", f"bid_price{level}")
                    for level in range(1, 6)
                ],
                bid_volume_keys=[
                    (f"bid_vol{level}", f"bid{level}_volume", f"buy{level}_vol", f"buy_vol{level}")
                    for level in range(1, 6)
                ],
                ask_price_keys=[
                    (f"ask{level}", f"sell{level}", f"ask_price{level}")
                    for level in range(1, 6)
                ],
                ask_volume_keys=[
                    (
                        f"ask_vol{level}",
                        f"ask{level}_volume",
                        f"sell{level}_vol",
                        f"sell_vol{level}",
                    )
                    for level in range(1, 6)
                ],
                name_keys=("name",),
                timestamp_keys=("datetime", "servertime", "time", "timestamp"),
            )
        return result

    def status(self) -> dict[str, object]:
        return {
            "enabled": True,
            "provider": self.source_name,
            "timeout_sec": int(self.timeout_sec),
            "last_error": self.last_error,
        }

    def _resolve_client(self) -> MootdxQuotesClient:
        if self._client is not None:
            return self._client
        if self._quotes_factory is not None:
            self._client = self._quotes_factory()
            return self._client
        try:
            from mootdx.quotes import Quotes  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - depends on local env.
            raise MarketDepthError("mootdx is not installed") from exc
        self._client = cast(
            MootdxQuotesClient,
            Quotes.factory(market="std", timeout=max(1, int(self.timeout_sec))),
        )
        return self._client


def _normalize_depth_payload(
    *,
    symbol: str,
    payload: dict[str, object],
    source: str,
    price_keys: tuple[str, ...],
    prev_close_keys: tuple[str, ...],
    open_keys: tuple[str, ...],
    bid_price_keys: list[tuple[str, ...]],
    bid_volume_keys: list[tuple[str, ...]],
    ask_price_keys: list[tuple[str, ...]],
    ask_volume_keys: list[tuple[str, ...]],
    name_keys: tuple[str, ...] = ("name",),
    timestamp_keys: tuple[str, ...] = (),
    date_key: str = "",
    time_key: str = "",
) -> dict[str, object]:
    bid_levels = _extract_levels(payload, "bid", bid_price_keys, bid_volume_keys)
    ask_levels = _extract_levels(payload, "ask", ask_price_keys, ask_volume_keys)
    bid_total = sum(_safe_float(level.get("volume")) for level in bid_levels)
    ask_total = sum(_safe_float(level.get("volume")) for level in ask_levels)
    best_bid = _safe_float(bid_levels[0].get("price")) if bid_levels else 0.0
    best_ask = _safe_float(ask_levels[0].get("price")) if ask_levels else 0.0
    spread = best_ask - best_bid if best_bid > 0 and best_ask > 0 else 0.0
    mid = (best_bid + best_ask) / 2 if best_bid > 0 and best_ask > 0 else 0.0
    imbalance = (
        (bid_total - ask_total) / (bid_total + ask_total)
        if bid_total + ask_total > 0
        else 0.0
    )
    last_price = _pick_float(payload, price_keys)
    prev_close = _pick_float(payload, prev_close_keys)
    open_price = _pick_float(payload, open_keys)
    timestamp = _pick_timestamp(
        payload=payload,
        timestamp_keys=timestamp_keys,
        date_key=date_key,
        time_key=time_key,
    )
    return {
        "symbol": symbol,
        "name": _pick_text(payload, name_keys),
        "available": bool(bid_levels or ask_levels),
        "source": source,
        "timestamp": timestamp,
        "last_price": round(last_price, 4),
        "prev_close": round(prev_close, 4),
        "open_price": round(open_price, 4),
        "bid_levels": bid_levels,
        "ask_levels": ask_levels,
        "spread": round(spread, 4),
        "spread_pct": round(spread / mid, 6) if mid > 0 else 0.0,
        "imbalance": round(imbalance, 6),
        "bid_total_volume": round(bid_total, 2),
        "ask_total_volume": round(ask_total, 2),
    }


def _extract_levels(
    payload: dict[str, object],
    side: str,
    price_keys: list[tuple[str, ...]],
    volume_keys: list[tuple[str, ...]],
) -> list[dict[str, object]]:
    levels: list[dict[str, object]] = []
    for index, (price_options, volume_options) in enumerate(
        zip(price_keys, volume_keys, strict=False),
        start=1,
    ):
        price = _pick_float(payload, price_options)
        volume = _pick_float(payload, volume_options)
        if price <= 0 and volume <= 0:
            continue
        levels.append(
            {
                "side": side,
                "level": index,
                "price": round(price, 4),
                "volume": round(volume, 2),
            }
        )
    return levels


def _pick_timestamp(
    *,
    payload: dict[str, object],
    timestamp_keys: tuple[str, ...],
    date_key: str,
    time_key: str,
) -> str:
    direct = _pick_text(payload, timestamp_keys)
    if direct:
        return direct
    if date_key and time_key:
        date_value = str(payload.get(date_key, "")).strip()
        time_value = str(payload.get(time_key, "")).strip()
        if date_value and time_value:
            return f"{date_value} {time_value}".strip()
    return datetime.now().isoformat(timespec="seconds")


def _pick_text(payload: dict[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _pick_float(payload: dict[str, object], keys: tuple[str, ...]) -> float:
    for key in keys:
        if key not in payload:
            continue
        value = _safe_float(payload.get(key))
        if value != 0.0:
            return value
    return 0.0


def _safe_float(value: object) -> float:
    try:
        if value is None or value == "":
            return 0.0
        if isinstance(value, bool):
            return float(int(value))
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            return float(value.strip())
        return float(cast(Any, value))
    except (TypeError, ValueError):
        return 0.0


def _empty_snapshot(symbol: str) -> dict[str, object]:
    return {
        "symbol": symbol,
        "name": "",
        "available": False,
        "source": "",
        "timestamp": "",
        "last_price": 0.0,
        "prev_close": 0.0,
        "open_price": 0.0,
        "bid_levels": [],
        "ask_levels": [],
        "spread": 0.0,
        "spread_pct": 0.0,
        "imbalance": 0.0,
        "bid_total_volume": 0.0,
        "ask_total_volume": 0.0,
    }


def _dedupe_symbols(symbols: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in symbols:
        symbol = _normalize_symbol(raw)
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        ordered.append(symbol)
    return ordered


def _normalize_symbol(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    upper = text.upper()
    for prefix in ("SH", "SZ", "BJ"):
        if upper.startswith(prefix) and len(text) > 2:
            return text[2:]
    if "." in text:
        return text.split(".", 1)[0]
    return text


def _to_prefixed_symbol(symbol: str) -> str:
    normalized = _normalize_symbol(symbol)
    if normalized.startswith(("4", "8")):
        return f"bj{normalized}"
    if normalized.startswith(("5", "6", "9", "7")):
        return f"sh{normalized}"
    return f"sz{normalized}"
