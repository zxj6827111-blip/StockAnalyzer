"""EFinance market data provider implementation."""

from __future__ import annotations

import importlib
import socket
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, timedelta
from time import sleep
from typing import Any, Protocol, cast

import numpy as np
import pandas as pd

from stock_analyzer.data.provider import DataSourceError

_DEFAULT_FLOAT_MARKET_CAP = 12_000_000_000.0
_DEFAULT_HOLDER_COUNT = 60_000.0
_DEFAULT_FINANCING_BALANCE = 2_500_000_000.0


class _EfinanceStockApi(Protocol):
    def get_quote_history(
        self,
        symbol: str,
        *,
        beg: str,
        end: str,
        klt: int,
        fqt: int,
        suppress_error: bool,
    ) -> object: ...


class _EfinanceModule(Protocol):
    stock: _EfinanceStockApi


class EfinanceProvider:
    """Fetch A-share daily bars from EFinance with resilient column normalization."""

    def __init__(
        self,
        ef_module: _EfinanceModule | None = None,
        retry_delay_sec: float = 0.5,
        max_attempts: int = 2,
        socket_timeout_sec: float = 15.0,
    ) -> None:
        self._ef_module = ef_module
        self._retry_delay_sec = max(0.0, retry_delay_sec)
        self._max_attempts = max(1, max_attempts)
        self._socket_timeout_sec = max(0.1, socket_timeout_sec)

    def fetch_daily_bars(
        self,
        symbol: str,
        lookback_days: int = 120,
        *,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        ef = self._resolve_ef_module()
        resolved_end_date = end_date or date.today()
        start_date = resolved_end_date - timedelta(days=max(1, int(lookback_days)) * 2)

        try:
            raw = self._fetch_quote_history_with_retry(
                ef_module=ef,
                symbol=symbol,
                beg=start_date.strftime("%Y%m%d"),
                end=resolved_end_date.strftime("%Y%m%d"),
            )
        except Exception as exc:  # pragma: no cover - depends on network/data provider.
            raise DataSourceError(f"efinance request failed for {symbol}: {exc}") from exc

        source_frame = _coerce_quote_frame(raw)
        if source_frame.empty:
            raise DataSourceError(f"efinance returned empty dataframe for {symbol}")

        frame = _normalize_quote_frame(source_frame)
        if frame.empty:
            raise DataSourceError(f"efinance normalized dataframe is empty for {symbol}")

        trimmed = frame.tail(max(1, int(lookback_days))).copy()
        name = _extract_symbol_name(source_frame)
        is_st = _contains_st(name)
        is_delisting_risk = _contains_delisting_risk(name)

        # EFinance daily bars do not include complete factor fields in one call.
        trimmed["suspended"] = False
        trimmed["name"] = name
        trimmed["is_st"] = is_st
        trimmed["is_delisting_risk"] = is_delisting_risk
        trimmed["roe"] = 0.08
        trimmed["debt_ratio"] = 0.55
        trimmed["financial_data_complete"] = True
        trimmed["financial_missing_fields"] = ""
        trimmed["financial_source"] = "efinance_default"
        trimmed["financial_report_date"] = ""
        trimmed["holder_count"] = _DEFAULT_HOLDER_COUNT
        trimmed["block_trade_net"] = 0.0
        trimmed["financing_balance"] = _DEFAULT_FINANCING_BALANCE
        trimmed["margin_financing_balance"] = _DEFAULT_FINANCING_BALANCE
        trimmed["northbound_net"] = 0.0
        trimmed["dragon_tiger_flag"] = 0.0
        trimmed["background_data_source"] = "efinance_default"
        trimmed["background_data_complete"] = True
        trimmed["board"] = _infer_board(symbol)
        selected = trimmed[
            [
                "open",
                "high",
                "low",
                "close",
                "volume",
                "turnover",
                "float_market_cap",
                "suspended",
                "name",
                "is_st",
                "is_delisting_risk",
                "roe",
                "debt_ratio",
                "financial_data_complete",
                "financial_missing_fields",
                "financial_source",
                "financial_report_date",
                "holder_count",
                "block_trade_net",
                "financing_balance",
                "margin_financing_balance",
                "northbound_net",
                "dragon_tiger_flag",
                "background_data_source",
                "background_data_complete",
                "board",
            ]
        ].copy()
        selected.index.name = "date"
        return selected

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        _ = symbol, interval, lookback_days
        return pd.DataFrame()

    def _resolve_ef_module(self) -> _EfinanceModule:
        if self._ef_module is not None:
            return self._ef_module
        try:
            imported = importlib.import_module("efinance")
        except ImportError as exc:
            raise DataSourceError("efinance is not installed") from exc
        ef = cast(_EfinanceModule, imported)
        self._ef_module = ef
        return ef

    def _fetch_quote_history_with_retry(
        self,
        *,
        ef_module: _EfinanceModule,
        symbol: str,
        beg: str,
        end: str,
    ) -> object:
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                with _temporary_request_timeout(self._socket_timeout_sec):
                    return ef_module.stock.get_quote_history(
                        symbol,
                        beg=beg,
                        end=end,
                        klt=101,
                        fqt=1,
                        suppress_error=True,
                    )
            except Exception as exc:  # pragma: no cover - depends on network/data provider.
                last_error = exc
                if attempt >= self._max_attempts:
                    break
                if self._retry_delay_sec > 0:
                    sleep(self._retry_delay_sec)
        if last_error is None:
            raise RuntimeError("efinance_request_failed_without_exception")
        raise last_error


@contextmanager
def _temporary_request_timeout(timeout_sec: float) -> Iterator[None]:
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout_sec)
    requests_module: Any | None = None
    original_request: Any | None = None
    try:
        try:
            requests_import = importlib.import_module("requests")
        except ImportError:
            requests_module = None
        else:
            requests_module = requests_import
        if requests_module is not None:
            original_request = requests_module.sessions.Session.request

            def _patched_request(
                session_self: object,
                method: str,
                url: str,
                *args: object,
                **kwargs: object,
            ) -> object:
                request_kwargs = dict(kwargs)
                request_kwargs.setdefault("timeout", timeout_sec)
                return original_request(session_self, method, url, *args, **request_kwargs)

            requests_module.sessions.Session.request = _patched_request
        yield
    finally:
        if requests_module is not None and original_request is not None:
            requests_module.sessions.Session.request = original_request
        socket.setdefaulttimeout(previous)


def _coerce_quote_frame(raw: object) -> pd.DataFrame:
    if isinstance(raw, pd.DataFrame):
        return raw
    if isinstance(raw, dict):
        for value in raw.values():
            if isinstance(value, pd.DataFrame) and not value.empty:
                return value
    return pd.DataFrame()


def _normalize_quote_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()

    columns = list(frame.columns)
    selected = frame.copy()

    rename_map: dict[object, str] = {}
    date_col = _resolve_column(
        columns,
        aliases=("date", "datetime", "trade_date"),
        fallback_index=2,
    )
    open_col = _resolve_column(columns, aliases=("open",), fallback_index=3)
    close_col = _resolve_column(columns, aliases=("close",), fallback_index=4)
    high_col = _resolve_column(columns, aliases=("high",), fallback_index=5)
    low_col = _resolve_column(columns, aliases=("low",), fallback_index=6)
    volume_col = _resolve_column(columns, aliases=("volume", "vol"), fallback_index=7)
    turnover_col = _resolve_column(
        columns,
        aliases=("turnover", "amount", "trade_amount"),
        fallback_index=8,
    )
    turnover_rate_col = _resolve_column(
        columns,
        aliases=("turnover_rate", "turnoverrate"),
        fallback_index=12,
    )
    for source, target in (
        (date_col, "date"),
        (open_col, "open"),
        (high_col, "high"),
        (low_col, "low"),
        (close_col, "close"),
        (volume_col, "volume"),
        (turnover_col, "turnover"),
    ):
        if source is not None:
            rename_map[source] = target
    if turnover_rate_col is not None:
        rename_map[turnover_rate_col] = "turnover_rate"

    normalized = selected.rename(columns=rename_map)
    if "date" not in normalized.columns:
        normalized["date"] = pd.to_datetime(normalized.index, errors="coerce")
    else:
        normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    for col in ("open", "high", "low", "close", "volume", "turnover"):
        if col in normalized.columns:
            normalized[col] = pd.to_numeric(normalized[col], errors="coerce")

    required = {"date", "open", "high", "low", "close", "volume", "turnover"}
    if not required.issubset(normalized.columns):
        missing = sorted(required - set(normalized.columns))
        raise DataSourceError(f"efinance response missing columns: {missing}")

    cleaned = normalized.dropna(
        subset=["date", "open", "high", "low", "close", "volume", "turnover"]
    )
    if cleaned.empty:
        return cleaned
    cleaned = cleaned.sort_values("date")
    cleaned = cleaned.drop_duplicates(subset=["date"], keep="last")

    cleaned = cleaned.set_index("date")
    cleaned.index.name = "date"
    turnover_rate = pd.Series(np.nan, index=cleaned.index, dtype=float)
    if "turnover_rate" in cleaned.columns:
        turnover_rate = pd.to_numeric(cleaned["turnover_rate"], errors="coerce")
        turnover_rate = turnover_rate.where(turnover_rate <= 1.0, turnover_rate / 100.0)
        turnover_rate = turnover_rate.replace(0.0, np.nan)
    estimated_market_cap = cleaned["turnover"] / turnover_rate
    cleaned["float_market_cap"] = estimated_market_cap.replace([np.inf, -np.inf], np.nan).fillna(
        _DEFAULT_FLOAT_MARKET_CAP
    )
    return cleaned


def _resolve_column(
    columns: Sequence[object],
    *,
    aliases: tuple[str, ...],
    fallback_index: int,
) -> object | None:
    normalized_aliases = {_normalize_token(alias) for alias in aliases}
    for column in columns:
        token = _normalize_token(column)
        if token and token in normalized_aliases:
            return column
    if 0 <= fallback_index < len(columns):
        return columns[fallback_index]
    return None


def _normalize_token(value: object) -> str:
    return str(value).strip().lower().replace(" ", "").replace("_", "")


def _extract_symbol_name(frame: pd.DataFrame) -> str:
    if frame.empty:
        return ""
    columns = list(frame.columns)
    name_col = _resolve_column(
        columns,
        aliases=("name", "stockname", "securityname", "symbolname"),
        fallback_index=0,
    )
    if name_col is None or name_col not in frame.columns:
        return ""
    series = frame[name_col]
    if series.empty:
        return ""
    for value in reversed(series.astype(str).tolist()):
        text = value.strip()
        if not text:
            continue
        if _is_numeric_text(text):
            continue
        return text
    return ""


def _contains_st(name: str) -> bool:
    text = name.strip().upper()
    return text.startswith("ST") or "*ST" in text


def _contains_delisting_risk(name: str) -> bool:
    return "\u9000" in name


def _infer_board(symbol: str) -> str:
    text = symbol.strip()
    if text.startswith("688"):
        return "star"
    if text.startswith("300") or text.startswith("301"):
        return "gem"
    if text.startswith("8") or text.startswith("4"):
        return "bj"
    return "main"


def _is_numeric_text(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True

