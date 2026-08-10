"""Tushare Pro market data provider for A-share daily bars."""

from __future__ import annotations

import importlib
import json
import os
import re
import urllib.error
import urllib.request
from datetime import date, timedelta
from threading import Lock
from time import monotonic, sleep
from typing import Any, Protocol

import numpy as np
import pandas as pd

from stock_analyzer.data.financial_pit import normalize_fina_indicator_rows
from stock_analyzer.data.provider import DataSourceError

_DEFAULT_FLOAT_MARKET_CAP = 12_000_000_000.0
_SYMBOL_RE = re.compile(r"(\d{6})")

# Client errors that never warrant retry: bad parameters, auth/permission
# problems and missing resources must surface immediately.
_NON_RETRYABLE_HTTP_CODES = frozenset({400, 401, 403, 404, 405, 406, 409, 410, 413, 422})
# Boundedly retryable client codes: throttling / service-busy signals.
_TRANSIENT_HTTP_CODES = frozenset({408, 425, 429})
# Any other 4xx is treated as non-transient; all 5xx are transient.
_DEFAULT_MAX_BACKOFF_SEC = 32.0


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
        list_status: str = "",
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

    def stk_limit(
        self,
        *,
        ts_code: str = "",
        trade_date: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object: ...

    def suspend_d(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object: ...

    def margin_detail(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object: ...

    def moneyflow(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object: ...

    def hk_hold(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object: ...

    def top_list(
        self,
        *,
        ts_code: str = "",
        trade_date: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object: ...

    def top_inst(
        self,
        *,
        ts_code: str = "",
        trade_date: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object: ...

    def block_trade(
        self,
        *,
        ts_code: str = "",
        trade_date: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object: ...

    def index_daily(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object: ...


class _HttpTushareProApi:
    """Minimal Tushare Pro HTTP client used when the optional SDK is absent."""

    _API_URL = "http://api.tushare.pro"

    def __init__(self, *, token: str, timeout_sec: float) -> None:
        self._token = str(token).strip()
        self._timeout_sec = max(0.1, float(timeout_sec))

    def _call(self, api_name: str, **kwargs: object) -> pd.DataFrame:
        fields = str(kwargs.pop("fields", "") or "")
        params = {str(key): value for key, value in kwargs.items() if value is not None}
        payload = {
            "api_name": str(api_name).strip(),
            "token": self._token,
            "params": params,
            "fields": fields,
        }
        request = urllib.request.Request(
            self._API_URL,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._timeout_sec) as response:
            parsed = json.loads(response.read().decode("utf-8"))
        if not isinstance(parsed, dict):
            raise DataSourceError(f"tushare {api_name} returned invalid response")
        if parsed.get("code") != 0:
            msg = str(parsed.get("msg", "") or "").strip()
            raise DataSourceError(f"tushare {api_name} failed: code={parsed.get('code')} msg={msg}")
        data = parsed.get("data", {})
        if not isinstance(data, dict):
            return pd.DataFrame()
        response_fields = data.get("fields", [])
        items = data.get("items", [])
        if not isinstance(response_fields, list) or not isinstance(items, list):
            return pd.DataFrame()
        return pd.DataFrame(items, columns=[str(item) for item in response_fields])

    def daily(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object:
        return self._call("daily", ts_code=ts_code, start_date=start_date, end_date=end_date)

    def daily_basic(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
        fields: str = "",
    ) -> object:
        return self._call(
            "daily_basic",
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
            fields=fields,
        )

    def adj_factor(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object:
        return self._call(
            "adj_factor",
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
        )

    def trade_cal(
        self,
        *,
        exchange: str = "",
        start_date: str = "",
        end_date: str = "",
        is_open: str = "",
    ) -> object:
        return self._call(
            "trade_cal",
            exchange=exchange,
            start_date=start_date,
            end_date=end_date,
            is_open=is_open,
        )

    def stock_basic(
        self,
        *,
        ts_code: str = "",
        list_status: str = "",
        fields: str = "",
    ) -> object:
        return self._call(
            "stock_basic",
            ts_code=ts_code,
            list_status=list_status,
            fields=fields,
        )

    def fina_indicator(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
        fields: str = "",
    ) -> object:
        return self._call(
            "fina_indicator",
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
            fields=fields,
        )

    def stk_limit(
        self,
        *,
        ts_code: str = "",
        trade_date: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object:
        return self._call(
            "stk_limit",
            ts_code=ts_code,
            trade_date=trade_date,
            start_date=start_date,
            end_date=end_date,
        )

    def suspend_d(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object:
        return self._call(
            "suspend_d",
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
        )

    def margin_detail(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object:
        return self._call(
            "margin_detail",
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
        )

    def moneyflow(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object:
        return self._call(
            "moneyflow",
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
        )

    def hk_hold(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object:
        return self._call(
            "hk_hold",
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
        )

    def top_list(
        self,
        *,
        ts_code: str = "",
        trade_date: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object:
        return self._call(
            "top_list",
            ts_code=ts_code,
            trade_date=trade_date,
            start_date=start_date,
            end_date=end_date,
        )

    def top_inst(
        self,
        *,
        ts_code: str = "",
        trade_date: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object:
        return self._call(
            "top_inst",
            ts_code=ts_code,
            trade_date=trade_date,
            start_date=start_date,
            end_date=end_date,
        )

    def block_trade(
        self,
        *,
        ts_code: str = "",
        trade_date: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object:
        return self._call(
            "block_trade",
            ts_code=ts_code,
            trade_date=trade_date,
            start_date=start_date,
            end_date=end_date,
        )

    def index_daily(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object:
        return self._call(
            "index_daily",
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
        )


class TushareProvider:
    """Fetch A-share daily bars from Tushare Pro (`pro.daily` + `adj_factor` → qfq).

    Retry policy for :meth:`_call_with_retry`:
    - HTTP 4xx client errors (400/401/403/404/405/406/409/410/413/422 and any
      other 4xx) are non-transient: parameters, permissions and missing
      resources must surface immediately, never be retried;
    - HTTP 408/425/429 are transient and get a bounded retry;
    - HTTP 5xx are transient and get a bounded retry;
    - plain ``URLError``, ``TimeoutError`` (incl. the historical
      ``socket.timeout`` alias), ``ConnectionError`` (reset/refused/aborted),
      ``socket.gaierror``/``socket.herror`` (DNS) and ``BrokenPipeError`` are
      transient and get a bounded retry; generic ``OSError`` subtypes such as
      ``FileNotFoundError`` or ``PermissionError`` are NOT retried;
    - the primary tushare SDK path raises ``requests.exceptions.*``:
      ``Timeout``/``ConnectionError`` are transient, ``HTTPError`` follows
      the same status-code policy as urllib (4xx non-transient except
      408/425/429, 5xx transient), and any other ``RequestException`` is
      non-transient;
    - Tushare business errors (``DataSourceError`` from a JSON body
      ``code != 0``) are non-transient: a bad parameter or permission issue
      must not be replayed by text-matching the message.
    Backoff is ``retry_delay_sec * attempt`` capped at ``max_backoff_sec``
    (an absolute upper bound, default 32 s) - never an unbounded
    "32x base" growth.
    """

    def __init__(
        self,
        *,
        token: str = "",
        pro_api: _TushareProApi | None = None,
        retry_delay_sec: float = 0.35,
        max_attempts: int = 2,
        socket_timeout_sec: float = 15.0,
        price_series_mode: str = "qfq",
        min_request_interval_sec: float | None = None,
        max_backoff_sec: float = _DEFAULT_MAX_BACKOFF_SEC,
    ) -> None:
        self._token = str(token or "").strip() or _resolve_tushare_token()
        self._pro_api = pro_api
        self._retry_delay_sec = max(0.0, float(retry_delay_sec))
        self._max_attempts = max(1, int(max_attempts))
        self._socket_timeout_sec = max(0.1, float(socket_timeout_sec))
        self._price_series_mode = str(price_series_mode or "qfq").strip().lower() or "qfq"
        self._max_backoff_sec = max(0.0, float(max_backoff_sec))
        self._min_request_interval_sec = max(
            0.0,
            float(
                retry_delay_sec if min_request_interval_sec is None else min_request_interval_sec
            ),
        )
        self._last_request_time: float = 0.0
        self._request_lock = Lock()
        self._name_cache: dict[str, str] = {}
        self._trade_cal_cache: dict[str, list[date]] = {}
        self._top_list_by_trade_date_cache: dict[str, pd.DataFrame] = {}
        self._top_inst_by_trade_date_cache: dict[str, pd.DataFrame] = {}

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
            raise DataSourceError(f"tushare fina_indicator failed for {ts_code}: {exc}") from exc
        frame = _coerce_frame(raw)
        return normalize_fina_indicator_rows(frame, symbol=code6)

    def fetch_trade_status(
        self,
        symbol: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        """Fetch stk_limit + suspend_d and merge into daily_trade_status rows.

        Returns DataFrame with columns:
        symbol, trade_date, up_limit, down_limit, suspended, suspend_type,
        source, as_of, coverage_complete
        """
        pro = self._resolve_pro_api()
        code6 = _normalize_symbol(symbol)
        if not code6:
            raise DataSourceError(f"invalid symbol for trade_status: {symbol}")
        ts_code = _to_ts_code(code6)
        resolved_end = end_date or date.today()
        resolved_start = start_date or (resolved_end - timedelta(days=365))
        start_s = resolved_start.strftime("%Y%m%d")
        end_s = resolved_end.strftime("%Y%m%d")

        limit_frame = pd.DataFrame()
        limit_error: Exception | None = None
        try:
            raw_limit = self._call_with_retry(
                lambda: pro.stk_limit(
                    ts_code=ts_code,
                    start_date=start_s,
                    end_date=end_s,
                )
            )
            limit_frame = _coerce_frame(raw_limit)
        except Exception as exc:
            limit_error = exc

        suspend_frame = pd.DataFrame()
        suspend_error: Exception | None = None
        try:
            raw_suspend = self._call_with_retry(
                lambda: pro.suspend_d(
                    ts_code=ts_code,
                    start_date=start_s,
                    end_date=end_s,
                )
            )
            suspend_frame = _coerce_frame(raw_suspend)
        except Exception as exc:
            suspend_error = exc

        # If BOTH interfaces fail we must NOT report success-as-empty; raise so the
        # caller records a failed phase and preserves prior trusted data. A single
        # interface failure still yields a partial (but real) result.
        if limit_error is not None and suspend_error is not None:
            raise DataSourceError(
                f"tushare trade_status failed for {ts_code}: "
                f"stk_limit={limit_error}; suspend_d={suspend_error}"
            )

        result = _normalize_trade_status(
            limit_frame=limit_frame,
            suspend_frame=suspend_frame,
            symbol=code6,
            start_date=resolved_start,
            end_date=resolved_end,
            limit_available=limit_error is None,
            suspend_available=suspend_error is None,
        )
        failed_components: list[str] = []
        if limit_error is not None:
            failed_components.append("stk_limit")
        if suspend_error is not None:
            failed_components.append("suspend_d")
        result.attrs["coverage_complete"] = not failed_components
        result.attrs["failed_components"] = failed_components
        return result

    def fetch_margin_detail(
        self,
        symbol: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        """Fetch margin_detail (融资融券) for a single stock."""
        pro = self._resolve_pro_api()
        code6 = _normalize_symbol(symbol)
        if not code6:
            raise DataSourceError(f"invalid symbol for margin_detail: {symbol}")
        ts_code = _to_ts_code(code6)
        resolved_end = end_date or date.today()
        resolved_start = start_date or (resolved_end - timedelta(days=365))
        start_s = resolved_start.strftime("%Y%m%d")
        end_s = resolved_end.strftime("%Y%m%d")
        try:
            raw = self._call_with_retry(
                lambda: pro.margin_detail(
                    ts_code=ts_code,
                    start_date=start_s,
                    end_date=end_s,
                )
            )
        except Exception as exc:
            raise DataSourceError(f"tushare margin_detail failed for {ts_code}: {exc}") from exc
        return _normalize_margin_detail(_coerce_frame(raw), symbol=code6)

    def fetch_moneyflow(
        self,
        symbol: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        """Fetch moneyflow (个股资金流) for a single stock."""
        pro = self._resolve_pro_api()
        code6 = _normalize_symbol(symbol)
        if not code6:
            raise DataSourceError(f"invalid symbol for moneyflow: {symbol}")
        ts_code = _to_ts_code(code6)
        resolved_end = end_date or date.today()
        resolved_start = start_date or (resolved_end - timedelta(days=365))
        start_s = resolved_start.strftime("%Y%m%d")
        end_s = resolved_end.strftime("%Y%m%d")
        try:
            raw = self._call_with_retry(
                lambda: pro.moneyflow(
                    ts_code=ts_code,
                    start_date=start_s,
                    end_date=end_s,
                )
            )
        except Exception as exc:
            raise DataSourceError(f"tushare moneyflow failed for {ts_code}: {exc}") from exc
        return _normalize_moneyflow(_coerce_frame(raw), symbol=code6)

    def fetch_hk_hold(
        self,
        symbol: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        """Fetch hk_hold (北向持股) for a single stock."""
        pro = self._resolve_pro_api()
        code6 = _normalize_symbol(symbol)
        if not code6:
            raise DataSourceError(f"invalid symbol for hk_hold: {symbol}")
        ts_code = _to_ts_code(code6)
        resolved_end = end_date or date.today()
        resolved_start = start_date or (resolved_end - timedelta(days=365))
        start_s = resolved_start.strftime("%Y%m%d")
        end_s = resolved_end.strftime("%Y%m%d")
        try:
            raw = self._call_with_retry(
                lambda: pro.hk_hold(
                    ts_code=ts_code,
                    start_date=start_s,
                    end_date=end_s,
                )
            )
        except Exception as exc:
            raise DataSourceError(f"tushare hk_hold failed for {ts_code}: {exc}") from exc
        return _normalize_hk_hold(_coerce_frame(raw), symbol=code6)

    def _resolve_event_trade_dates(
        self,
        *,
        start_date: date,
        end_date: date,
    ) -> list[date]:
        """Resolve event-ledger dates; fallback keeps unit tests and degraded smoke cheap."""
        try:
            dates = self.list_open_trade_dates(start_date=start_date, end_date=end_date)
        except Exception:
            dates = []
        if dates:
            return dates
        return [end_date]

    def _fetch_top_list_raw_range(
        self,
        *,
        pro: _TushareProApi,
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for trade_day in self._resolve_event_trade_dates(
            start_date=start_date,
            end_date=end_date,
        ):
            trade_date_s = trade_day.strftime("%Y%m%d")
            cached = self._top_list_by_trade_date_cache.get(trade_date_s)
            if cached is None:
                raw = self._call_with_retry(
                    lambda trade_date_s=trade_date_s: pro.top_list(
                        trade_date=trade_date_s,
                    )
                )
                cached = _coerce_frame(raw)
                self._top_list_by_trade_date_cache[trade_date_s] = cached
            if not cached.empty:
                frames.append(cached.copy())
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, axis=0, ignore_index=True)

    def _fetch_top_inst_raw_range(
        self,
        *,
        pro: _TushareProApi,
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for trade_day in self._resolve_event_trade_dates(
            start_date=start_date,
            end_date=end_date,
        ):
            trade_date_s = trade_day.strftime("%Y%m%d")
            cached = self._top_inst_by_trade_date_cache.get(trade_date_s)
            if cached is None:
                raw = self._call_with_retry(
                    lambda trade_date_s=trade_date_s: pro.top_inst(
                        trade_date=trade_date_s,
                    )
                )
                cached = _coerce_frame(raw)
                self._top_inst_by_trade_date_cache[trade_date_s] = cached
            if not cached.empty:
                frames.append(cached.copy())
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, axis=0, ignore_index=True)

    def fetch_top_list(
        self,
        symbol: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        """Fetch top_list (龙虎榜) events for a single stock.

        Tushare top_list requires a single trade_date and returns all stocks for
        that date, so fetch by trade day and filter by ts_code locally. Per-date
        caches prevent a full-universe sync from refetching the same event ledger
        for every symbol.
        """
        pro = self._resolve_pro_api()
        code6 = _normalize_symbol(symbol)
        if not code6:
            raise DataSourceError(f"invalid symbol for top_list: {symbol}")
        ts_code = _to_ts_code(code6)
        resolved_end = end_date or date.today()
        resolved_start = start_date or resolved_end
        try:
            raw = self._fetch_top_list_raw_range(
                pro=pro,
                start_date=resolved_start,
                end_date=resolved_end,
            )
        except Exception as exc:
            raise DataSourceError(f"tushare top_list failed for {ts_code}: {exc}") from exc
        return _normalize_top_list(_coerce_frame(raw), symbol=code6)

    def fetch_top_inst(
        self,
        symbol: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        """Fetch top_inst (龙虎榜机构明细) for a single stock.

        Tushare top_inst follows the same trade_date event-ledger contract as
        top_list; fetch by day and filter by current symbol before normalization.
        """
        pro = self._resolve_pro_api()
        code6 = _normalize_symbol(symbol)
        if not code6:
            raise DataSourceError(f"invalid symbol for top_inst: {symbol}")
        ts_code = _to_ts_code(code6)
        resolved_end = end_date or date.today()
        resolved_start = start_date or resolved_end
        try:
            raw = self._fetch_top_inst_raw_range(
                pro=pro,
                start_date=resolved_start,
                end_date=resolved_end,
            )
        except Exception as exc:
            raise DataSourceError(f"tushare top_inst failed for {ts_code}: {exc}") from exc
        return _normalize_top_inst(_coerce_frame(raw), symbol=code6)

    def fetch_block_trade(
        self,
        symbol: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        """Fetch block_trade (大宗交易) events for a single stock."""
        pro = self._resolve_pro_api()
        code6 = _normalize_symbol(symbol)
        if not code6:
            raise DataSourceError(f"invalid symbol for block_trade: {symbol}")
        ts_code = _to_ts_code(code6)
        resolved_end = end_date or date.today()
        resolved_start = start_date or (resolved_end - timedelta(days=365))
        start_s = resolved_start.strftime("%Y%m%d")
        end_s = resolved_end.strftime("%Y%m%d")
        try:
            raw = self._call_with_retry(
                lambda: pro.block_trade(
                    ts_code=ts_code,
                    start_date=start_s,
                    end_date=end_s,
                )
            )
        except Exception as exc:
            raise DataSourceError(f"tushare block_trade failed for {ts_code}: {exc}") from exc
        return _normalize_block_trade(_coerce_frame(raw), symbol=code6)

    def fetch_index_daily(
        self,
        index_code: str = "000300.SH",
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        """Fetch index_daily for market-level index (default: CSI 300).

        Returns DataFrame with: trade_date, open, high, low, close, vol, amount
        """
        pro = self._resolve_pro_api()
        resolved_end = end_date or date.today()
        resolved_start = start_date or (resolved_end - timedelta(days=365 * 2))
        start_s = resolved_start.strftime("%Y%m%d")
        end_s = resolved_end.strftime("%Y%m%d")
        try:
            raw = self._call_with_retry(
                lambda: pro.index_daily(
                    ts_code=index_code,
                    start_date=start_s,
                    end_date=end_s,
                )
            )
        except Exception as exc:
            raise DataSourceError(f"tushare index_daily failed for {index_code}: {exc}") from exc
        return _normalize_index_daily(_coerce_frame(raw), index_code=index_code)

    def fetch_delisted_stock_basic(self) -> pd.DataFrame:
        """Fetch ``stock_basic(list_status='D')`` — delisted A-share symbols.

        Raises :class:`DataSourceError` when no tushare token is configured so
        callers can fall back to a local delisted-symbol list file.
        """
        pro = self._resolve_pro_api()
        raw = self._call_with_retry(
            lambda: pro.stock_basic(list_status="D", fields="ts_code,name,delist_date")
        )
        return _coerce_frame(raw)

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
        except ImportError:
            self._pro_api = _HttpTushareProApi(
                token=self._token,
                timeout_sec=self._socket_timeout_sec,
            )
            return self._pro_api
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
            # Apply the same cadence to initial calls and retries. The lock prevents
            # hard-timeout worker threads from issuing a burst through one provider.
            with self._request_lock:
                elapsed = monotonic() - self._last_request_time
                if self._last_request_time > 0 and elapsed < self._min_request_interval_sec:
                    sleep(self._min_request_interval_sec - elapsed)
                self._last_request_time = monotonic()
            try:
                return fn()
            except Exception as exc:  # pragma: no cover
                last_error = exc
                if attempt >= self._max_attempts:
                    break
                if not self._is_retryable_error(exc):
                    break
                if self._retry_delay_sec > 0:
                    backoff = min(
                        self._retry_delay_sec * attempt,
                        self._max_backoff_sec,
                    )
                    sleep(backoff)
        assert last_error is not None
        raise last_error

    def _is_retryable_error(self, exc: Exception) -> bool:
        """Classify exceptions for bounded retry.

        ``HTTPError`` is a subclass of ``URLError`` so it must be checked
        first: HTTP 4xx client errors (bad parameters, auth/permission
        problems, missing resources) are non-transient, HTTP 408/425/429 and
        all 5xx are transient, while plain ``URLError`` (DNS, timeout,
        connection reset/refused) remains transient. Tushare business errors
        already wrapped in ``DataSourceError`` are never retried. Only
        explicit network-layer error types are transient: generic ``OSError``
        (e.g. ``FileNotFoundError``, ``PermissionError``) is not.

        Both transport stacks are covered: the urllib fallback
        (``_HttpTushareProApi``) raises ``urllib.error.*``/builtin socket
        errors, while the primary tushare SDK path raises
        ``requests.exceptions.*`` (``RequestException`` is an ``OSError``
        subclass, not the builtin ``ConnectionError``, so it must be matched
        explicitly).
        """
        import socket as _socket

        if isinstance(exc, urllib.error.HTTPError):
            code = int(exc.code)
            if code in _TRANSIENT_HTTP_CODES or code >= 500:
                return True
            if code in _NON_RETRYABLE_HTTP_CODES or 400 <= code < 500:
                return False
            return False
        if isinstance(exc, DataSourceError):
            return False
        if isinstance(exc, urllib.error.URLError):
            return True
        if isinstance(exc, TimeoutError):
            return True
        if isinstance(exc, ConnectionError):
            return True
        if isinstance(exc, (_socket.gaierror, _socket.herror, BrokenPipeError)):
            return True
        try:
            import requests  # type: ignore[import-untyped]
        except ImportError:
            return False
        if isinstance(exc, requests.exceptions.HTTPError):
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            if isinstance(status, int):
                if status in _TRANSIENT_HTTP_CODES or status >= 500:
                    return True
                if status in _NON_RETRYABLE_HTTP_CODES or 400 <= status < 500:
                    return False
            return False
        if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
            return True
        if isinstance(exc, requests.exceptions.RequestException):
            return False
        return False

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
    if code6.startswith("920"):
        return f"{code6}.BJ"
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
        str(adjust_meta.get("price_series_mode") or price_series_mode or "qfq").strip().lower()
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


def _normalize_trade_status(
    *,
    limit_frame: pd.DataFrame,
    suspend_frame: pd.DataFrame,
    symbol: str,
    start_date: date,
    end_date: date,
    limit_available: bool = True,
    suspend_available: bool = True,
) -> pd.DataFrame:
    """Merge stk_limit and suspend_d into unified trade status rows.

    When suspend_available=False (suspend_d failed), suspended is set to None
    (unknown) and coverage_complete=False. This prevents the execution layer
    from treating unknown suspension status as "not suspended".
    """
    rows: dict[str, dict[str, object]] = {}

    if not limit_frame.empty and "trade_date" in limit_frame.columns:
        lf = limit_frame.copy()
        lf["trade_date"] = pd.to_datetime(
            lf["trade_date"].astype(str), format="%Y%m%d", errors="coerce"
        )
        lf = lf.dropna(subset=["trade_date"])
        for _, row in lf.iterrows():
            td = row["trade_date"]
            key = td.strftime("%Y-%m-%d")
            if key not in rows:
                rows[key] = {
                    "symbol": symbol,
                    "trade_date": td,
                    "up_limit": float("nan"),
                    "down_limit": float("nan"),
                    "suspended": None if not suspend_available else False,
                    "suspend_type": "",
                    "source": "tushare_stk_limit",
                    "as_of": key,
                    "coverage_complete": limit_available and suspend_available,
                }
            up = pd.to_numeric([row.get("up_limit")], errors="coerce")[0]
            down = pd.to_numeric([row.get("down_limit")], errors="coerce")[0]
            if not pd.isna(up):
                rows[key]["up_limit"] = float(up)
            if not pd.isna(down):
                rows[key]["down_limit"] = float(down)

    if not suspend_frame.empty and "trade_date" in suspend_frame.columns:
        sf = suspend_frame.copy()
        sf["trade_date"] = pd.to_datetime(
            sf["trade_date"].astype(str), format="%Y%m%d", errors="coerce"
        )
        sf = sf.dropna(subset=["trade_date"])
        for _, row in sf.iterrows():
            td = row["trade_date"]
            key = td.strftime("%Y-%m-%d")
            if key not in rows:
                rows[key] = {
                    "symbol": symbol,
                    "trade_date": td,
                    "up_limit": float("nan"),
                    "down_limit": float("nan"),
                    "suspended": False,
                    "suspend_type": "",
                    "source": "tushare_suspend_d",
                    "as_of": key,
                    "coverage_complete": True,
                }
            rows[key]["suspended"] = True
            rows[key]["coverage_complete"] = limit_available and suspend_available
            stype = str(row.get("suspend_type", "") or row.get("reason", "") or "").strip()
            if stype:
                rows[key]["suspend_type"] = stype
            prior_source = str(rows[key].get("source", ""))
            rows[key]["source"] = (
                "tushare_stk_limit+suspend_d"
                if "stk_limit" in prior_source
                else "tushare_suspend_d"
            )

    if not rows:
        return pd.DataFrame()

    result = pd.DataFrame(list(rows.values()))
    result["trade_date"] = pd.to_datetime(result["trade_date"])
    result = result.sort_values("trade_date").reset_index(drop=True)
    return result


def _normalize_margin_detail(frame: pd.DataFrame, *, symbol: str) -> pd.DataFrame:
    """Normalize margin_detail rows.

    Fields preserved:
    - rzye -> financing_balance (融资余额, yuan)
    - rzmre -> financing_buy_amount (融资买入额, yuan)
    - rqye -> securities_lending_balance (融券余额, yuan)
    - rqyl -> securities_lending_volume (融券余量, shares)
    - rqmcl -> securities_lending_sell_volume (融券卖出量, shares)
    """
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        return pd.DataFrame()
    out = _filter_symbol_event_rows(frame, symbol=symbol)
    if out.empty:
        return pd.DataFrame()
    out["symbol"] = str(symbol).zfill(6)[-6:]
    out["trade_date"] = pd.to_datetime(
        out["trade_date"].astype(str), format="%Y%m%d", errors="coerce"
    )
    out = out.dropna(subset=["trade_date"])
    if out.empty:
        return pd.DataFrame()
    rename = {
        "rzye": "financing_balance",
        "rzmre": "financing_buy_amount",
        "rqye": "securities_lending_balance",
        "rqyl": "securities_lending_volume",
        "rqmcl": "securities_lending_sell_volume",
    }
    for old, new in rename.items():
        if old in out.columns:
            out[new] = pd.to_numeric(out[old], errors="coerce")
    keep = ["symbol", "trade_date"] + [v for v in rename.values() if v in out.columns]
    out = out[keep].sort_values("trade_date").reset_index(drop=True)
    out["source"] = "tushare_margin_detail"
    out["as_of"] = out["trade_date"].dt.strftime("%Y-%m-%d")
    out["coverage_complete"] = True
    return out


def _normalize_moneyflow(frame: pd.DataFrame, *, symbol: str) -> pd.DataFrame:
    """Normalize moneyflow rows.

    Derived field:
    - net_mf_amount = buy_lg + buy_elg - sell_lg - sell_elg (主力净流入, 万元)

    Original fields preserved (万元):
    - buy_sm_amount, sell_sm_amount (小单)
    - buy_md_amount, sell_md_amount (中单)
    - buy_lg_amount, sell_lg_amount (大单)
    - buy_elg_amount, sell_elg_amount (超大单)
    """
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        return pd.DataFrame()
    out = _filter_symbol_event_rows(frame, symbol=symbol)
    if out.empty:
        return pd.DataFrame()
    out["symbol"] = str(symbol).zfill(6)[-6:]
    out["trade_date"] = pd.to_datetime(
        out["trade_date"].astype(str), format="%Y%m%d", errors="coerce"
    )
    out = out.dropna(subset=["trade_date"])
    if out.empty:
        return pd.DataFrame()

    amount_cols = [
        "buy_sm_amount",
        "sell_sm_amount",
        "buy_md_amount",
        "sell_md_amount",
        "buy_lg_amount",
        "sell_lg_amount",
        "buy_elg_amount",
        "sell_elg_amount",
    ]
    for col in amount_cols:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    buy_lg = out.get("buy_lg_amount", pd.Series(0.0, index=out.index))
    buy_elg = out.get("buy_elg_amount", pd.Series(0.0, index=out.index))
    sell_lg = out.get("sell_lg_amount", pd.Series(0.0, index=out.index))
    sell_elg = out.get("sell_elg_amount", pd.Series(0.0, index=out.index))
    out["net_mf_amount"] = (
        buy_lg.fillna(0) + buy_elg.fillna(0) - sell_lg.fillna(0) - sell_elg.fillna(0)
    )
    mask = buy_lg.isna() | buy_elg.isna() | sell_lg.isna() | sell_elg.isna()
    out.loc[mask, "net_mf_amount"] = float("nan")

    keep = ["symbol", "trade_date"] + [c for c in amount_cols if c in out.columns]
    keep.append("net_mf_amount")
    out = out[keep].sort_values("trade_date").reset_index(drop=True)
    out["source"] = "tushare_moneyflow"
    out["as_of"] = out["trade_date"].dt.strftime("%Y-%m-%d")
    out["coverage_complete"] = True
    return out


def _normalize_hk_hold(frame: pd.DataFrame, *, symbol: str) -> pd.DataFrame:
    """Normalize hk_hold rows.

    Fields:
    - hold_vol: 北向持股数量 (股)
    - hold_ratio: 北向持股比例 (小数)
    - hold_market_cap: 北向持股市值 (元)

    0 rows from API = no coverage/unknown, NOT real zero.
    """
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        return pd.DataFrame()
    out = frame.copy()
    out["symbol"] = str(symbol).zfill(6)[-6:]
    out["trade_date"] = pd.to_datetime(
        out["trade_date"].astype(str), format="%Y%m%d", errors="coerce"
    )
    out = out.dropna(subset=["trade_date"])
    if out.empty:
        return pd.DataFrame()

    if "vol" in out.columns:
        out["hold_vol"] = pd.to_numeric(out["vol"], errors="coerce")
    elif "hold_vol" in out.columns:
        out["hold_vol"] = pd.to_numeric(out["hold_vol"], errors="coerce")
    if "ratio" in out.columns:
        out["hold_ratio"] = pd.to_numeric(out["ratio"], errors="coerce")
    elif "hold_ratio" in out.columns:
        out["hold_ratio"] = pd.to_numeric(out["hold_ratio"], errors="coerce")
    if "market_cap" in out.columns:
        out["hold_market_cap"] = pd.to_numeric(out["market_cap"], errors="coerce")
    elif "hold_market_cap" in out.columns:
        out["hold_market_cap"] = pd.to_numeric(out["hold_market_cap"], errors="coerce")

    keep_cols = ["symbol", "trade_date"]
    for c in ("hold_vol", "hold_ratio", "hold_market_cap"):
        if c in out.columns:
            keep_cols.append(c)
    out = out[keep_cols].sort_values("trade_date").reset_index(drop=True)
    out["source"] = "tushare_hk_hold"
    out["as_of"] = out["trade_date"].dt.strftime("%Y-%m-%d")
    out["coverage_complete"] = True
    return out


def _filter_symbol_event_rows(frame: pd.DataFrame, *, symbol: str) -> pd.DataFrame:
    """Filter date-level Tushare event rows down to one 6-digit symbol."""
    if frame is None or frame.empty:
        return pd.DataFrame()
    code6 = str(symbol).zfill(6)[-6:]
    out = frame.copy()
    if "ts_code" in out.columns:
        raw_codes = out["ts_code"].astype(str).str.upper()
        return out.loc[raw_codes.str.startswith(code6)].copy()
    if "symbol" in out.columns:
        normalized = out["symbol"].astype(str).map(_normalize_symbol)
        return out.loc[normalized == code6].copy()
    return pd.DataFrame()


def _normalize_top_list(frame: pd.DataFrame, *, symbol: str) -> pd.DataFrame:
    """Normalize top_list (龙虎榜) rows.

    dragon_tiger_flag = 1 on event days, 0 on confirmed no-event days.
    NaN means not queried / request failed.
    """
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        return pd.DataFrame()
    out = _filter_symbol_event_rows(frame, symbol=symbol)
    if out.empty:
        return pd.DataFrame()
    out["symbol"] = str(symbol).zfill(6)[-6:]
    out["trade_date"] = pd.to_datetime(
        out["trade_date"].astype(str), format="%Y%m%d", errors="coerce"
    )
    out = out.dropna(subset=["trade_date"])
    if out.empty:
        return pd.DataFrame()

    # Aggregate per trade_date (multiple reasons possible)
    agg = out.groupby("trade_date", as_index=False).agg(
        reason_count=("trade_date", "size"),
    )
    if "reason" in out.columns:
        reasons = (
            out.groupby("trade_date")["reason"]
            .apply(lambda x: "|".join(sorted(set(str(v) for v in x if v))))
            .reset_index()
        )
        reasons.columns = ["trade_date", "reasons"]
        agg = agg.merge(reasons, on="trade_date", how="left")
    else:
        agg["reasons"] = ""

    if "buy" in out.columns:
        agg["buy_amount"] = out.groupby("trade_date")["buy"].sum().values
    if "sell" in out.columns:
        agg["sell_amount"] = out.groupby("trade_date")["sell"].sum().values
    if "amount" in out.columns:
        agg["turnover"] = out.groupby("trade_date")["amount"].sum().values

    agg["dragon_tiger_flag"] = 1.0
    agg["source"] = "tushare_top_list"
    agg["as_of"] = agg["trade_date"].dt.strftime("%Y-%m-%d")
    agg["coverage_complete"] = True
    return agg.sort_values("trade_date").reset_index(drop=True)


def _normalize_top_inst(frame: pd.DataFrame, *, symbol: str) -> pd.DataFrame:
    """Normalize top_inst (龙虎榜机构明细) rows."""
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        return pd.DataFrame()
    out = _filter_symbol_event_rows(frame, symbol=symbol)
    if out.empty:
        return pd.DataFrame()
    out["symbol"] = str(symbol).zfill(6)[-6:]
    out["trade_date"] = pd.to_datetime(
        out["trade_date"].astype(str), format="%Y%m%d", errors="coerce"
    )
    out = out.dropna(subset=["trade_date"])
    if out.empty:
        return pd.DataFrame()

    rename = {
        "exalter": "institution_name",
        "buy": "inst_buy_amount",
        "sell": "inst_sell_amount",
        "net_buy": "inst_net_amount",
    }
    for old, new in rename.items():
        if old in out.columns:
            out[new] = out[old] if old == "exalter" else pd.to_numeric(out[old], errors="coerce")

    keep = ["symbol", "trade_date"]
    for c in ("institution_name", "inst_buy_amount", "inst_sell_amount", "inst_net_amount"):
        if c in out.columns:
            keep.append(c)
    out = out[keep].sort_values("trade_date").reset_index(drop=True)
    out["source"] = "tushare_top_inst"
    out["as_of"] = out["trade_date"].dt.strftime("%Y-%m-%d")
    out["coverage_complete"] = True
    return out


def _normalize_block_trade(frame: pd.DataFrame, *, symbol: str) -> pd.DataFrame:
    """Normalize block_trade (大宗交易) rows.

    Derived fields:
    - block_trade_amount: 成交金额 (元)
    - block_trade_volume: 成交量 (股)
    - block_trade_premium_discount: (price - close) / close, NaN if close unknown
    - block_trade_net: NaN (no reliable direction from block_trade API)

    Note: block_trade_net stays NaN because Tushare block_trade does not
    provide reliable buy/sell direction. Do NOT fill with amount.
    """
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        return pd.DataFrame()
    out = frame.copy()
    out["symbol"] = str(symbol).zfill(6)[-6:]
    out["trade_date"] = pd.to_datetime(
        out["trade_date"].astype(str), format="%Y%m%d", errors="coerce"
    )
    out = out.dropna(subset=["trade_date"])
    if out.empty:
        return pd.DataFrame()

    if "price" in out.columns:
        out["block_price"] = pd.to_numeric(out["price"], errors="coerce")
    if "vol" in out.columns:
        out["block_trade_volume"] = pd.to_numeric(out["vol"], errors="coerce") * 100.0
    if "amount" in out.columns:
        out["block_trade_amount"] = pd.to_numeric(out["amount"], errors="coerce")
    if "close" in out.columns:
        close = pd.to_numeric(out["close"], errors="coerce")
        price = out.get("block_price", pd.Series(float("nan"), index=out.index))
        out["block_trade_premium_discount"] = (price - close) / close.replace(0, float("nan"))
    else:
        out["block_trade_premium_discount"] = float("nan")

    # block_trade_net: NaN (no reliable direction)
    out["block_trade_net"] = float("nan")

    keep = ["symbol", "trade_date"]
    for c in (
        "block_price",
        "block_trade_volume",
        "block_trade_amount",
        "block_trade_premium_discount",
        "block_trade_net",
    ):
        if c in out.columns:
            keep.append(c)
    if "buyer" in out.columns:
        keep.append("buyer")
        out["buyer"] = out["buyer"].astype(str)
    if "seller" in out.columns:
        keep.append("seller")
        out["seller"] = out["seller"].astype(str)

    out = out[keep].sort_values("trade_date").reset_index(drop=True)
    out["source"] = "tushare_block_trade"
    out["as_of"] = out["trade_date"].dt.strftime("%Y-%m-%d")
    out["coverage_complete"] = True
    return out


def _normalize_index_daily(frame: pd.DataFrame, *, index_code: str) -> pd.DataFrame:
    """Normalize index_daily rows."""
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        return pd.DataFrame()
    out = frame.copy()
    out["index_code"] = index_code
    out["trade_date"] = pd.to_datetime(
        out["trade_date"].astype(str), format="%Y%m%d", errors="coerce"
    )
    out = out.dropna(subset=["trade_date"])
    if out.empty:
        return pd.DataFrame()
    for col in ("open", "high", "low", "close", "vol", "amount"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    if "vol" in out.columns:
        out["volume"] = out["vol"] * 100.0
    if "amount" in out.columns:
        out["turnover"] = out["amount"] * 1000.0
    keep = ["index_code", "trade_date", "open", "high", "low", "close"]
    if "volume" in out.columns:
        keep.append("volume")
    if "turnover" in out.columns:
        keep.append("turnover")
    out = out[keep].sort_values("trade_date").reset_index(drop=True)
    out["source"] = "tushare_index_daily"
    out["as_of"] = out["trade_date"].dt.strftime("%Y-%m-%d")
    out["coverage_complete"] = True
    return out
