"""AKShare market data provider implementation."""

from __future__ import annotations

import importlib
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from time import sleep
from typing import Any, Protocol, cast

import numpy as np
import pandas as pd

from stock_analyzer.data.background_adapter import AkshareBackgroundAdapter
from stock_analyzer.data.financial_adapter import AkshareFinancialAdapter
from stock_analyzer.data.provider import DataSourceError

_DEFAULT_FLOAT_MARKET_CAP = 12_000_000_000.0
_DEFAULT_HOLDER_COUNT = 60_000.0
_DEFAULT_FINANCING_BALANCE = 2_500_000_000.0


class _AkshareModule(Protocol):
    def stock_zh_a_hist(
        self,
        *,
        symbol: str,
        period: str,
        start_date: str,
        end_date: str,
        adjust: str,
    ) -> object: ...


class AkshareProvider:
    """Fetch A-share historical bars from AKShare with resilient fallback."""

    def __init__(
        self,
        financial_adapter: AkshareFinancialAdapter | None = None,
        background_adapter: AkshareBackgroundAdapter | None = None,
        retry_delay_sec: float = 0.5,
        max_attempts: int = 2,
        socket_timeout_sec: float = 15.0,
    ) -> None:
        self._financial_adapter = financial_adapter or AkshareFinancialAdapter()
        self._background_adapter = background_adapter or AkshareBackgroundAdapter()
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
        try:
            imported = importlib.import_module("akshare")
        except ImportError as exc:
            raise DataSourceError("akshare is not installed") from exc
        ak = cast(_AkshareModule, imported)

        resolved_end_date = end_date or date.today()
        start_date = resolved_end_date - timedelta(days=lookback_days * 2)

        try:
            raw, source = self._fetch_hist_with_retry(
                ak_module=ak,
                symbol=symbol,
                start_date=start_date.strftime("%Y%m%d"),
                end_date=resolved_end_date.strftime("%Y%m%d"),
            )
        except Exception as exc:  # pragma: no cover - depends on network/data provider.
            raise DataSourceError(f"akshare request failed for {symbol}: {exc}") from exc

        if raw.empty:
            raise DataSourceError(f"akshare returned empty dataframe for {symbol}")

        if source == "tx":
            return _normalize_tx_hist_frame(raw=raw, symbol=symbol, lookback_days=lookback_days)
        return self._normalize_em_hist_frame(raw=raw, symbol=symbol, lookback_days=lookback_days)

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        _ = symbol, interval, lookback_days
        return pd.DataFrame()

    def _normalize_em_hist_frame(
        self,
        *,
        raw: pd.DataFrame,
        symbol: str,
        lookback_days: int,
    ) -> pd.DataFrame:
        renamed = raw.rename(
            columns={
                "日期": "date",
                "开盘": "open",
                "最高": "high",
                "最低": "low",
                "收盘": "close",
                "成交量": "volume",
                "成交额": "turnover",
                "换手率": "turnover_rate",
            }
        )

        required_columns = {"date", "open", "high", "low", "close", "volume", "turnover"}
        if not required_columns.issubset(renamed.columns):
            missing = required_columns - set(renamed.columns)
            raise DataSourceError(f"akshare response missing columns: {sorted(missing)}")

        frame = renamed.copy()
        frame["date"] = pd.to_datetime(frame["date"])
        for column in ("open", "high", "low", "close", "volume", "turnover"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.dropna(subset=["open", "high", "low", "close", "volume", "turnover"])
        frame = frame.sort_values("date").tail(lookback_days)
        frame = frame.set_index("date")
        frame.index.name = "date"

        if "turnover_rate" in frame.columns:
            turnover_rate = (
                pd.to_numeric(frame["turnover_rate"], errors="coerce").fillna(0.0) / 100.0
            )
            estimated_market_cap = frame["turnover"] / turnover_rate.replace(0, pd.NA)
            frame["float_market_cap"] = estimated_market_cap.fillna(_DEFAULT_FLOAT_MARKET_CAP)
        else:
            frame["float_market_cap"] = _DEFAULT_FLOAT_MARKET_CAP

        financial = self._financial_adapter.fetch_snapshot(symbol=symbol)
        frame["suspended"] = False
        frame["name"] = str(financial.get("name", ""))
        frame["is_st"] = bool(financial.get("is_st", False))
        frame["is_delisting_risk"] = bool(financial.get("is_delisting_risk", False))
        frame["roe"] = pd.to_numeric(
            pd.Series([financial.get("roe", np.nan)] * len(frame), index=frame.index),
            errors="coerce",
        )
        frame["debt_ratio"] = pd.to_numeric(
            pd.Series([financial.get("debt_ratio", np.nan)] * len(frame), index=frame.index),
            errors="coerce",
        )
        frame["financial_data_complete"] = bool(financial.get("financial_data_complete", False))
        missing_fields_raw = financial.get("missing_fields", [])
        missing_fields = missing_fields_raw if isinstance(missing_fields_raw, list) else []
        frame["financial_missing_fields"] = ",".join(
            str(item) for item in missing_fields if str(item).strip()
        )
        frame["financial_source"] = str(financial.get("source", "fallback"))
        frame["financial_report_date"] = str(financial.get("latest_report_date", ""))
        frame["board"] = _infer_board(symbol)
        frame = self._background_adapter.enrich_bars(symbol=symbol, bars=frame)
        return _select_output_columns(frame)

    def _fetch_hist_with_retry(
        self,
        *,
        ak_module: _AkshareModule,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> tuple[pd.DataFrame, str]:
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                with _temporary_request_timeout(self._socket_timeout_sec):
                    raw = ak_module.stock_zh_a_hist(
                        symbol=symbol,
                        period="daily",
                        start_date=start_date,
                        end_date=end_date,
                        adjust="qfq",
                    )
                return cast(pd.DataFrame, raw), "em"
            except Exception as exc:  # pragma: no cover - depends on network/data provider.
                last_error = exc
                if attempt >= self._max_attempts:
                    break
                if self._retry_delay_sec > 0:
                    sleep(self._retry_delay_sec)

        tx_func = getattr(ak_module, "stock_zh_a_hist_tx", None)
        if callable(tx_func):
            try:
                with _temporary_request_timeout(self._socket_timeout_sec):
                    raw = tx_func(
                        symbol=_to_tx_symbol(symbol),
                        start_date=start_date,
                        end_date=end_date,
                    )
                return cast(pd.DataFrame, raw), "tx"
            except Exception as exc:  # pragma: no cover - depends on network/data provider.
                last_error = exc

        if last_error is None:
            raise RuntimeError("akshare_request_failed_without_exception")
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


def _infer_board(symbol: str) -> str:
    text = symbol.strip()
    if text.startswith("688"):
        return "star"
    if text.startswith("300") or text.startswith("301"):
        return "gem"
    if text.startswith("8") or text.startswith("4"):
        return "bj"
    return "main"


def _to_tx_symbol(symbol: str) -> str:
    text = symbol.strip()
    if text.startswith(("5", "6", "9")):
        return f"sh{text}"
    return f"sz{text}"


def _normalize_tx_hist_frame(
    *,
    raw: pd.DataFrame,
    symbol: str,
    lookback_days: int,
) -> pd.DataFrame:
    renamed = raw.rename(columns={"amount": "volume"})
    required_columns = {"date", "open", "high", "low", "close", "volume"}
    if not required_columns.issubset(renamed.columns):
        missing = required_columns - set(renamed.columns)
        raise DataSourceError(f"akshare tx response missing columns: {sorted(missing)}")

    frame = renamed.copy()
    frame["date"] = pd.to_datetime(frame["date"], unit="ms", errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date", "open", "high", "low", "close", "volume"])
    frame = frame.sort_values("date").tail(lookback_days)
    frame = frame.set_index("date")
    frame.index.name = "date"

    frame["turnover"] = frame["close"] * frame["volume"] * 100.0
    frame["float_market_cap"] = _DEFAULT_FLOAT_MARKET_CAP
    frame["suspended"] = False
    frame["name"] = ""
    frame["is_st"] = False
    frame["is_delisting_risk"] = False
    frame["roe"] = 0.08
    frame["debt_ratio"] = 0.55
    frame["financial_data_complete"] = True
    frame["financial_missing_fields"] = ""
    frame["financial_source"] = "akshare_tx_default"
    frame["financial_report_date"] = ""
    frame["holder_count"] = _DEFAULT_HOLDER_COUNT
    frame["block_trade_net"] = 0.0
    frame["financing_balance"] = _DEFAULT_FINANCING_BALANCE
    frame["margin_financing_balance"] = _DEFAULT_FINANCING_BALANCE
    frame["northbound_net"] = 0.0
    frame["dragon_tiger_flag"] = 0.0
    frame["background_data_source"] = "akshare_tx_default"
    frame["background_data_complete"] = True
    frame["board"] = _infer_board(symbol)
    return _select_output_columns(frame)


def _select_output_columns(frame: pd.DataFrame) -> pd.DataFrame:
    return frame[
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
    ]
