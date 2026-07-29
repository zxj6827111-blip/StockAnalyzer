"""Tushare Pro market data provider for A-share daily bars."""

from __future__ import annotations

import importlib
import os
import re
from datetime import date, timedelta
from time import sleep
from typing import Any, Protocol

import numpy as np
import pandas as pd

from stock_analyzer.data.financial_pit import normalize_fina_indicator_rows
from stock_analyzer.data.provider import DataSourceError

_DEFAULT_FLOAT_MARKET_CAP = 12_000_000_000.0
_SYMBOL_RE = re.compile(r"(\d{6})")


class _TushareProApi(Protocol):
    def daily(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object: ...

    def daily_basic(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
        fields: str = "",
    ) -> object: ...

    def adj_factor(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object: ...

    def trade_cal(
        self,
        *,
        exchange: str = "",
        start_date: str = "",
        end_date: str = "",
        is_open: str = "",
    ) -> object: ...

    def stock_basic(
        self,
        *,
        ts_code: str = "",
        fields: str = "",
    ) -> object: ...

    def fina_indicator(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
        fields: str = "",
    ) -> object: ...


class TushareProvider:
    """Fetch A-share daily bars from Tushare Pro (`pro.daily` + `adj_factor` → qfq)."""

    def __init__(
        self,
        *,
        token: str = "",
        pro_api: _TushareProApi | None = None,
        retry_delay_sec: float = 0.35,
        max_attempts: int = 2,
        socket_timeout_sec: float = 15.0,
        price_series_mode: str = "qfq",
    ) -> None:
        self._token = str(token or "").strip() or _resolve_tushare_token()
        self._pro_api = pro_api
        self._retry_delay_sec = max(0.0, float(retry_delay_sec))
        self._max_attempts = max(1, int(max_attempts))
        self._socket_timeout_sec = max(0.1, float(socket_timeout_sec))
        self._price_series_mode = str(price_series_mode or "qfq").strip().lower() or "qfq"
        self._name_cache: dict[str, str] = {}
        self._trade_cal_cache: dict[str, list[date]] = {}

    def fetch_daily_bars(
        self,
        symbol: str,
        lookback_days: int = 120,
        *,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        pro = self._resolve_pro_api()
        code6 = _normalize_symbol(symbol)
        if not code6:
            raise DataSourceError(f"invalid symbol for tushare: {symbol}")
        ts_code = _to_ts_code(code6)
        resolved_end = end_date or date.today()
        start = resolved_end - timedelta(days=max(1, int(lookback_days)) * 2 + 10)
        start_s = start.strftime("%Y%m%d")
        end_s = resolved_end.strftime("%Y%m%d")

        try:
            raw = self._call_with_retry(
                lambda: pro.daily(ts_code=ts_code, start_date=start_s, end_date=end_s)
            )
        except Exception as exc:  # pragma: no cover
            raise DataSourceError(f"tushare daily failed for {ts_code}: {exc}") from exc

        daily = _coerce_frame(raw)
        if daily.empty:
            raise DataSourceError(f"tushare daily empty for {ts_code}")

        basic = pd.DataFrame()
        try:
            basic_raw = self._call_with_retry(
                lambda: pro.daily_basic(
                    ts_code=ts_code,
                    start_date=start_s,
                    end_date=end_s,
                    fields="ts_code,trade_date,turnover_rate,circ_mv,total_mv",
                )
            )
            basic = _coerce_frame(basic_raw)
        except Exception:
            basic = pd.DataFrame()

        adj = pd.DataFrame()
        if self._price_series_mode in {"qfq", "hfq"}:
            try:
                adj_start = (start - timedelta(days=400)).strftime("%Y%m%d")
                adj_raw = self._call_with_retry(
                    lambda: pro.adj_factor(
                        ts_code=ts_code,
                        start_date=adj_start,
                        end_date=end_s,
                    )
                )
                adj = _coerce_frame(adj_raw)
            except Exception as exc:
                raise DataSourceError(
                    f"tushare adj_factor failed for {ts_code} "
                    f"(required for price_series_mode={self._price_series_mode}): {exc}"
                ) from exc
            if adj.empty:
                raise DataSourceError(
                    f"tushare adj_factor empty for {ts_code} "
                    f"(required for price_series_mode={self._price_series_mode})"
                )

        name = self._lookup_name(pro=pro, ts_code=ts_code, code6=code6)
        frame = _normalize_tushare_daily(
            daily=daily,
            basic=basic,
            adj=adj,
            symbol=code6,
            name=name,
            lookback_days=lookback_days,
            price_series_mode=self._price_series_mode,
        )
        if frame.empty:
            raise DataSourceError(f"tushare normalized empty for {ts_code}")
        return frame

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        _ = symbol, interval, lookback_days
        return pd.DataFrame()

    def list_open_trade_dates(
        self,
        *,
        start_date: date,
        end_date: date,
        exchange: str = "SSE",
    ) -> list[date]:
        """Return open exchange session dates in [start_date, end_date]."""
        if end_date < start_date:
            return []
        pro = self._resolve_pro_api()
        start_s = start_date.strftime("%Y%m%d")
        end_s = end_date.strftime("%Y%m%d")
        cache_key = f"{exchange}:{start_s}:{end_s}"
        cached = self._trade_cal_cache.get(cache_key)
        if cached is not None:
            return list(cached)
        try:
            raw = self._call_with_retry(
                lambda: pro.trade_cal(
                    exchange=exchange,
                    start_date=start_s,
                    end_date=end_s,
                    is_open="1",
                )
            )
        except Exception as exc:  # pragma: no cover
            raise DataSourceError(f"tushare trade_cal failed: {exc}") from exc
        frame = _coerce_frame(raw)
        if frame.empty or "cal_date" not in frame.columns:
            self._trade_cal_cache[cache_key] = []
            return []
        parsed = pd.to_datetime(
            frame["cal_date"].astype(str), format="%Y%m%d", errors="coerce"
        ).dropna()
        dates = sorted(
            {
                item
                for item in parsed.dt.date.tolist()
                if isinstance(item, date) and start_date <= item <= end_date
            }
        )
        self._trade_cal_cache[cache_key] = dates
        return list(dates)

    def resolve_target_trade_date(
        self,
        *,
        now: date | None = None,
        after_close: bool = True,
    ) -> date:
        """Resolve latest open session date for warehouse sync targeting."""
        current = now or date.today()
        start = current - timedelta(days=20)
        open_dates = self.list_open_trade_dates(start_date=start, end_date=current)
        if not open_dates:
            cursor = current if after_close else current - timedelta(days=1)
            while cursor.weekday() >= 5:
                cursor -= timedelta(days=1)
            return cursor
        if after_close:
            return open_dates[-1]
        if open_dates[-1] == current and len(open_dates) >= 2:
            return open_dates[-2]
        if open_dates[-1] < current:
            return open_dates[-1]
        if len(open_dates) >= 2:
            return open_dates[-2]
        return open_dates[-1]


    def fetch_fina_indicator(
        self,
        symbol: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        """Fetch fina_indicator rows and normalize to PIT financial snapshots.

        Returns empty DataFrame on successful empty query.
        Raises DataSourceError on API failure so callers preserve prior snapshots.
        """
        pro = self._resolve_pro_api()
        code6 = _normalize_symbol(symbol)
        if not code6:
            raise DataSourceError(f"invalid symbol for tushare fina_indicator: {symbol}")
        ts_code = _to_ts_code(code6)
        resolved_end = end_date or date.today()
        resolved_start = start_date or (resolved_end - timedelta(days=365 * 5 + 30))
        start_s = resolved_start.strftime("%Y%m%d")
        end_s = resolved_end.strftime("%Y%m%d")
        fields = "ts_code,ann_date,end_date,roe,debt_to_assets,update_flag"
        try:
            raw = self._call_with_retry(
                lambda: pro.fina_indicator(
                    ts_code=ts_code,
                    start_date=start_s,
                    end_date=end_s,
                    fields=fields,
                )
            )
        except Exception as exc:
            raise DataSourceError(
                f"tushare fina_indicator failed for {ts_code}: {exc}"
            ) from exc
        frame = _coerce_frame(raw)
        return normalize_fina_indicator_rows(frame, symbol=code6)

    def _resolve_pro_api(self) -> _TushareProApi:
        if self._pro_api is not None:
            return self._pro_api
        if not self._token:
            raise DataSourceError(
                "tushare token missing; set market_warehouse.tushare_token or "
                "SA__MARKET_WAREHOUSE__TUSHARE_TOKEN / TUSHARE_TOKEN"
            )
        try:
            ts = importlib.import_module("tushare")
        except ImportError as exc:
            raise DataSourceError("tushare is not installed") from exc
        set_token = getattr(ts, "set_token", None)
        if callable(set_token):
            set_token(self._token)
        pro_api = getattr(ts, "pro_api", None)
        if not callable(pro_api):
            raise DataSourceError("tushare.pro_api unavailable")
        self._pro_api = pro_api(self._token)
        return self._pro_api

    def _call_with_retry(self, fn: Any) -> object:
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                return fn()
            except Exception as exc:  # pragma: no cover
                last_error = exc
                if attempt >= self._max_attempts:
                    break
                if self._retry_delay_sec > 0:
                    sleep(self._retry_delay_sec * attempt)
        assert last_error is not None
        raise last_error

    def _lookup_name(self, *, pro: _TushareProApi, ts_code: str, code6: str) -> str:
        cached = self._name_cache.get(code6)
        if cached is not None:
            return cached
        name = ""
        try:
            raw = self._call_with_retry(
                lambda: pro.stock_basic(ts_code=ts_code, fields="ts_code,name")
            )
            frame = _coerce_frame(raw)
            if not frame.empty and "name" in frame.columns:
                name = str(frame["name"].iloc[0] or "").strip()
        except Exception:
            name = ""
        self._name_cache[code6] = name
        return name


def _resolve_tushare_token() -> str:
    for key in (
        "SA__MARKET_WAREHOUSE__TUSHARE_TOKEN",
        "TUSHARE_TOKEN",
        "TS_TOKEN",
    ):
        value = str(os.environ.get(key, "") or "").strip()
        if value:
            return value
    return ""


def _normalize_symbol(symbol: str) -> str:
    text = str(symbol or "").strip().upper()
    match = _SYMBOL_RE.search(text)
    return match.group(1) if match else ""


def _to_ts_code(code6: str) -> str:
    if code6.startswith(("5", "6", "9")):
        return f"{code6}.SH"
    if code6.startswith(("4", "8")):
        return f"{code6}.BJ"
    return f"{code6}.SZ"


def _coerce_frame(raw: object) -> pd.DataFrame:
    if isinstance(raw, pd.DataFrame):
        return raw.copy()
    return pd.DataFrame()


def _apply_price_adjust(
    frame: pd.DataFrame,
    *,
    adj: pd.DataFrame,
    price_series_mode: str,
) -> tuple[pd.DataFrame, dict[str, object]]:
    """Apply Tushare adj_factor to match system price_series_mode (default qfq).

    Tushare ``daily`` is unadjusted. Forward-adjusted (qfq) uses:
        price_qfq = price_raw * adj_factor / adj_factor_latest

    Volume/turnover contract (aligned with AKShare qfq runtime path):
    - volume stays actual traded share count (Tushare vol*100), not reverse-scaled.
    - turnover stays actual traded amount (Tushare amount*1000).
    Do not invent volume continuity via price*volume; actual shares are the runtime unit.
    """
    mode = str(price_series_mode or "raw").strip().lower()
    if mode in {"", "raw"}:
        out = frame.copy()
        meta: dict[str, object] = {
            "price_series_mode": "raw",
            "adjustment_source": "none",
            "adjustment_anchor_date": "",
            "adjustment_anchor_factor": float("nan"),
        }
        return out, meta
    if mode not in {"qfq", "hfq"}:
        raise DataSourceError(f"unsupported tushare price_series_mode: {price_series_mode}")
    if adj.empty or "trade_date" not in adj.columns or "adj_factor" not in adj.columns:
        raise DataSourceError("tushare adj_factor required for qfq/hfq but missing columns")

    adj2 = adj.copy()
    adj2["date"] = pd.to_datetime(adj2["trade_date"].astype(str), format="%Y%m%d", errors="coerce")
    adj2["adj_factor"] = pd.to_numeric(adj2["adj_factor"], errors="coerce")
    adj2 = adj2.dropna(subset=["date", "adj_factor"]).drop_duplicates(subset=["date"], keep="last")
    if adj2.empty:
        raise DataSourceError("tushare adj_factor empty after normalize")

    out = frame.merge(adj2[["date", "adj_factor"]], on="date", how="left")
    if out["adj_factor"].isna().any():
        out["adj_factor"] = out["adj_factor"].ffill().bfill()
    if out["adj_factor"].isna().any():
        raise DataSourceError("tushare adj_factor could not be aligned to daily bars")

    if mode == "qfq":
        anchor_factor = float(out["adj_factor"].iloc[-1])
        anchor_date = pd.Timestamp(out["date"].iloc[-1]).date().isoformat()
        if anchor_factor == 0:
            raise DataSourceError("tushare adj_factor latest is zero")
        scale = out["adj_factor"] / anchor_factor
    else:
        anchor_factor = float(out["adj_factor"].iloc[0])
        anchor_date = pd.Timestamp(out["date"].iloc[0]).date().isoformat()
        if anchor_factor == 0:
            raise DataSourceError("tushare adj_factor first is zero")
        scale = out["adj_factor"] / anchor_factor

    for col in ("open", "high", "low", "close"):
        out[col] = pd.to_numeric(out[col], errors="coerce") * scale
    # Keep actual share volume; do not reverse-scale for synthetic price*volume continuity.
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce")
    out["turnover"] = pd.to_numeric(out["turnover"], errors="coerce")
    out = out.drop(columns=["adj_factor"])
    meta = {
        "price_series_mode": mode,
        "adjustment_source": "tushare_adj_factor",
        "adjustment_anchor_date": anchor_date,
        "adjustment_anchor_factor": float(anchor_factor),
    }
    return out, meta


def _normalize_tushare_daily(
    *,
    daily: pd.DataFrame,
    basic: pd.DataFrame,
    adj: pd.DataFrame,
    symbol: str,
    name: str,
    lookback_days: int,
    price_series_mode: str = "qfq",
) -> pd.DataFrame:
    frame = daily.copy()
    rename = {
        "trade_date": "date",
        "vol": "volume",
        "amount": "turnover",
    }
    frame = frame.rename(columns={k: v for k, v in rename.items() if k in frame.columns})
    if "date" not in frame.columns:
        raise DataSourceError("tushare daily missing trade_date")
    frame["date"] = pd.to_datetime(frame["date"].astype(str), format="%Y%m%d", errors="coerce")
    for col in ("open", "high", "low", "close", "volume", "turnover"):
        if col not in frame.columns:
            raise DataSourceError(f"tushare daily missing column: {col}")
        frame[col] = pd.to_numeric(frame[col], errors="coerce")

    # Tushare vol is 手 (=100 shares); amount is 千元.
    frame["volume"] = frame["volume"] * 100.0
    frame["turnover"] = frame["turnover"] * 1000.0

    if not basic.empty and "trade_date" in basic.columns:
        basic2 = basic.copy()
        basic2["date"] = pd.to_datetime(
            basic2["trade_date"].astype(str), format="%Y%m%d", errors="coerce"
        )
        keep = [c for c in ("date", "turnover_rate", "circ_mv", "total_mv") if c in basic2.columns]
        basic2 = basic2[keep].drop_duplicates(subset=["date"], keep="last")
        frame = frame.merge(basic2, on="date", how="left")

    frame = frame.dropna(subset=["date", "open", "high", "low", "close", "volume"])
    if frame.empty:
        return frame
    frame = frame.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    frame, adjust_meta = _apply_price_adjust(frame, adj=adj, price_series_mode=price_series_mode)

    if "circ_mv" in frame.columns:
        circ = pd.to_numeric(frame["circ_mv"], errors="coerce") * 10000.0
        frame["float_market_cap"] = circ.fillna(_DEFAULT_FLOAT_MARKET_CAP)
    elif "turnover_rate" in frame.columns:
        rate = pd.to_numeric(frame["turnover_rate"], errors="coerce").replace(0, np.nan) / 100.0
        frame["float_market_cap"] = (frame["turnover"] / rate).fillna(_DEFAULT_FLOAT_MARKET_CAP)
    else:
        frame["float_market_cap"] = _DEFAULT_FLOAT_MARKET_CAP

    is_st = "ST" in name.upper() if name else False
    is_delisting = any(token in name for token in ("退", "*ST")) if name else False

    frame["suspended"] = False
    frame["name"] = name
    frame["is_st"] = is_st
    frame["is_delisting_risk"] = is_delisting
    # Do NOT invent financial completeness. Real values need ann_date-aware P1 enrichment.
    frame["roe"] = np.nan
    frame["debt_ratio"] = np.nan
    frame["financial_data_complete"] = False
    frame["financial_missing_fields"] = "roe,debt_ratio"
    frame["financial_source"] = "tushare_pending"
    frame["financial_report_date"] = ""
    frame["financial_as_of"] = ""
    frame["financial_trust_level"] = "missing"
    frame["financial_completeness"] = 0.0
    # Unknown / not queried => NaN. Confirmed zero-event fields stay NaN until real feeds exist.
    frame["holder_count"] = np.nan
    frame["block_trade_net"] = np.nan
    frame["financing_balance"] = np.nan
    frame["margin_financing_balance"] = np.nan
    frame["northbound_net"] = np.nan
    frame["dragon_tiger_flag"] = np.nan
    mode = (
        str(adjust_meta.get("price_series_mode") or price_series_mode or "qfq")
        .strip()
        .lower()
        or "qfq"
    )
    frame["background_data_source"] = f"tushare_pro_{mode}"
    frame["background_data_complete"] = False
    frame["background_missing_fields"] = (
        "holder_count,block_trade_net,financing_balance,"
        "margin_financing_balance,northbound_net,dragon_tiger_flag"
    )
    frame["background_as_of"] = ""
    frame["price_series_mode"] = mode
    frame["adjustment_source"] = str(adjust_meta.get("adjustment_source") or "")
    frame["adjustment_anchor_date"] = str(adjust_meta.get("adjustment_anchor_date") or "")
    anchor_factor: Any = adjust_meta.get("adjustment_anchor_factor")
    frame["adjustment_anchor_factor"] = float(anchor_factor or float("nan"))
    frame["board"] = _infer_board(symbol)

    selected = frame[
        [
            "date",
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
            "financial_as_of",
            "financial_trust_level",
            "financial_completeness",
            "holder_count",
            "block_trade_net",
            "financing_balance",
            "margin_financing_balance",
            "northbound_net",
            "dragon_tiger_flag",
            "background_data_source",
            "background_data_complete",
            "background_missing_fields",
            "background_as_of",
            "price_series_mode",
            "adjustment_source",
            "adjustment_anchor_date",
            "adjustment_anchor_factor",
            "board",
        ]
    ].copy()
    selected = selected.tail(max(1, int(lookback_days)))
    selected = selected.set_index("date")
    selected.index.name = "date"
    selected.attrs["price_series_meta"] = dict(adjust_meta)
    return selected


def _infer_board(symbol: str) -> str:
    code = _normalize_symbol(symbol)
    if code.startswith("688"):
        return "star"
    if code.startswith(("300", "301")):
        return "chinext"
    if code.startswith(("8", "4")):
        return "bse"
    return "main"
