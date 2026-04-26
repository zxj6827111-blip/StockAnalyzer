"""AKShare background-factor adapter with resilient daily-series parsing."""

from __future__ import annotations

from datetime import date
from time import time
from typing import cast

import pandas as pd


class AkshareBackgroundAdapter:
    """Fetch optional background factors and align them to bar dates."""

    def __init__(self, cache_ttl_sec: int = 6 * 3600, ak_module: object | None = None) -> None:
        self._cache_ttl_sec = max(60, int(cache_ttl_sec))
        self._ak_module = ak_module
        self._cache: dict[tuple[str, int, str], tuple[float, pd.DataFrame]] = {}

    def enrich_bars(self, symbol: str, bars: pd.DataFrame) -> pd.DataFrame:
        if bars.empty:
            return bars
        normalized_symbol = symbol.strip()
        index = bars.index
        key = (
            normalized_symbol,
            len(index),
            str(index[-1].date()) if len(index) > 0 else "",
        )
        now = time()
        cached = self._cache.get(key)
        if cached is not None:
            ts, frame = cached
            if now - ts <= self._cache_ttl_sec:
                return bars.join(frame, how="left")

        ak = self._resolve_ak_module()
        if ak is None:
            enriched = bars.join(_empty_background(index=index), how="left")
            self._cache[key] = (now, enriched.drop(columns=list(bars.columns), errors="ignore"))
            return enriched

        holder_series, holder_source = self._fetch_holder_count(
            ak=ak,
            symbol=normalized_symbol,
            start=index[0].date(),
            end=index[-1].date(),
        )
        block_series, block_source = self._fetch_block_trade_net(
            ak=ak,
            symbol=normalized_symbol,
            start=index[0].date(),
            end=index[-1].date(),
        )
        financing_series, financing_source = self._fetch_financing_balance(
            ak=ak,
            symbol=normalized_symbol,
            start=index[0].date(),
            end=index[-1].date(),
        )
        northbound_series, northbound_source = self._fetch_northbound_net(
            ak=ak,
            symbol=normalized_symbol,
            start=index[0].date(),
            end=index[-1].date(),
        )
        dragon_series, dragon_source = self._fetch_dragon_tiger_flag(
            ak=ak,
            symbol=normalized_symbol,
            start=index[0].date(),
            end=index[-1].date(),
        )
        source_tokens = [
            token
            for token in (
                holder_source,
                block_source,
                financing_source,
                northbound_source,
                dragon_source,
            )
            if token
        ]
        source_text = ",".join(sorted(set(source_tokens)))
        background = pd.DataFrame(
            {
                "holder_count": holder_series.reindex(index).ffill(),
                "block_trade_net": block_series.reindex(index).fillna(0.0),
                "financing_balance": financing_series.reindex(index).ffill(),
                "margin_financing_balance": financing_series.reindex(index).ffill(),
                "northbound_net": northbound_series.reindex(index).fillna(0.0),
                "dragon_tiger_flag": dragon_series.reindex(index).fillna(0.0),
                "background_data_source": source_text,
                "background_data_complete": bool(source_tokens),
            },
            index=index,
        )
        enriched = bars.join(background, how="left")
        self._cache[key] = (now, background)
        return enriched

    def _resolve_ak_module(self) -> object | None:
        if self._ak_module is not None:
            return self._ak_module
        try:
            import akshare as ak  # type: ignore[import-untyped]
        except Exception:
            return None
        return cast(object, ak)

    def _fetch_holder_count(
        self,
        ak: object,
        symbol: str,
        start: date,
        end: date,
    ) -> tuple[pd.Series, str]:
        candidates = (
            "stock_zh_a_gdhs",
            "stock_holder_num_detail_em",
        )
        for func_name in candidates:
            frame = _call_dataframe_function(ak=ak, func_name=func_name, symbol=symbol)
            series = _extract_metric_series(
                frame=frame,
                symbol=symbol,
                value_keywords=("股东户数", "股东人数", "股东总户数", "holder"),
                start=start,
                end=end,
            )
            if series is not None and not series.empty:
                return series, func_name
        return _empty_numeric_series(), ""

    def _fetch_block_trade_net(
        self,
        ak: object,
        symbol: str,
        start: date,
        end: date,
    ) -> tuple[pd.Series, str]:
        candidates = (
            "stock_dzjy_mrtj",
            "stock_dzjy_detail",
            "stock_dzjy_hygtj",
        )
        for func_name in candidates:
            frame = _call_dataframe_function(ak=ak, func_name=func_name, symbol=symbol)
            series = _extract_block_trade_series(
                frame=frame,
                symbol=symbol,
                start=start,
                end=end,
            )
            if series is not None and not series.empty:
                return series, func_name
        return _empty_numeric_series(), ""

    def _fetch_financing_balance(
        self,
        ak: object,
        symbol: str,
        start: date,
        end: date,
    ) -> tuple[pd.Series, str]:
        candidates = (
            "stock_margin_detail_sse",
            "stock_margin_detail_szse",
            "stock_margin_sse",
            "stock_margin_szse",
        )
        for func_name in candidates:
            frame = _call_dataframe_function(ak=ak, func_name=func_name, symbol=symbol)
            series = _extract_metric_series(
                frame=frame,
                symbol=symbol,
                value_keywords=("融资余额", "融资融券余额", "financing"),
                start=start,
                end=end,
            )
            if series is not None and not series.empty:
                return series, func_name
        return _empty_numeric_series(), ""

    def _fetch_northbound_net(
        self,
        ak: object,
        symbol: str,
        start: date,
        end: date,
    ) -> tuple[pd.Series, str]:
        candidates = (
            "stock_hsgt_individual_em",
            "stock_hsgt_hold_stock_em",
            "stock_hsgt_north_net_flow_in_em",
        )
        for func_name in candidates:
            frame = _call_dataframe_function(ak=ak, func_name=func_name, symbol=symbol)
            series = _extract_metric_series(
                frame=frame,
                symbol=symbol,
                value_keywords=("净买", "净流入", "北向", "陆股通", "north", "net"),
                start=start,
                end=end,
            )
            if series is not None and not series.empty:
                return series, func_name
        return _empty_numeric_series(), ""

    def _fetch_dragon_tiger_flag(
        self,
        ak: object,
        symbol: str,
        start: date,
        end: date,
    ) -> tuple[pd.Series, str]:
        candidates = (
            "stock_lhb_detail_em",
            "stock_lhb_stock_statistic_em",
            "stock_lhb_jgmmtj_em",
        )
        for func_name in candidates:
            frame = _call_dataframe_function(ak=ak, func_name=func_name, symbol=symbol)
            series = _extract_flag_series(frame=frame, symbol=symbol, start=start, end=end)
            if series is not None and not series.empty:
                return series, func_name
        return _empty_numeric_series(), ""


def _empty_background(index: pd.Index) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "holder_count": pd.Series(index=index, dtype=float),
            "block_trade_net": 0.0,
            "financing_balance": pd.Series(index=index, dtype=float),
            "margin_financing_balance": pd.Series(index=index, dtype=float),
            "northbound_net": 0.0,
            "dragon_tiger_flag": 0.0,
            "background_data_source": "",
            "background_data_complete": False,
        },
        index=index,
    )


def _empty_numeric_series() -> pd.Series:
    return pd.Series(dtype=float)


def _call_dataframe_function(ak: object, func_name: str, symbol: str) -> pd.DataFrame | None:
    func = getattr(ak, func_name, None)
    if not callable(func):
        return None
    kwargs_candidates = (
        {"symbol": symbol},
        {"code": symbol},
        {"stock": symbol},
        {"ts_code": symbol},
    )
    for kwargs in kwargs_candidates:
        try:
            payload = func(**kwargs)
        except TypeError:
            continue
        except Exception:
            return None
        if isinstance(payload, pd.DataFrame):
            return payload
    try:
        payload = func(symbol)
    except Exception:
        return None
    if isinstance(payload, pd.DataFrame):
        return payload
    return None


def _extract_metric_series(
    frame: pd.DataFrame | None,
    symbol: str,
    value_keywords: tuple[str, ...],
    start: date,
    end: date,
) -> pd.Series | None:
    if frame is None or frame.empty:
        return None
    parsed = _filter_symbol_rows(frame=frame, symbol=symbol)
    date_col = _pick_column(parsed.columns, ("date", "日期", "交易日", "统计日期", "报告期"))
    value_col = _pick_column(parsed.columns, value_keywords)
    if date_col is None or value_col is None:
        return None
    dates = pd.to_datetime(parsed[date_col], errors="coerce")
    values = pd.to_numeric(parsed[value_col], errors="coerce")
    data = pd.DataFrame({"date": dates, "value": values}).dropna(subset=["date"])
    if data.empty:
        return None
    grouped = data.groupby(data["date"].dt.normalize(), as_index=True)["value"].last()
    grouped_index = pd.DatetimeIndex(grouped.index)
    mask = (grouped_index.date >= start) & (grouped_index.date <= end)
    return pd.Series(grouped.loc[mask], copy=False)


def _extract_block_trade_series(
    frame: pd.DataFrame | None,
    symbol: str,
    start: date,
    end: date,
) -> pd.Series | None:
    if frame is None or frame.empty:
        return None
    parsed = _filter_symbol_rows(frame=frame, symbol=symbol)
    date_col = _pick_column(parsed.columns, ("date", "日期", "交易日", "统计日期"))
    if date_col is None:
        return None
    dates = pd.to_datetime(parsed[date_col], errors="coerce")
    net_col = _pick_column(parsed.columns, ("净买", "净额", "net"))
    if net_col is not None:
        net = pd.to_numeric(parsed[net_col], errors="coerce")
    else:
        buy_col = _pick_column(parsed.columns, ("买入", "买方", "buy"))
        sell_col = _pick_column(parsed.columns, ("卖出", "卖方", "sell"))
        if buy_col is None or sell_col is None:
            return None
        buy = pd.to_numeric(parsed[buy_col], errors="coerce").fillna(0.0)
        sell = pd.to_numeric(parsed[sell_col], errors="coerce").fillna(0.0)
        net = buy - sell
    data = pd.DataFrame({"date": dates, "value": net}).dropna(subset=["date"])
    if data.empty:
        return None
    grouped = data.groupby(data["date"].dt.normalize(), as_index=True)["value"].sum()
    grouped_index = pd.DatetimeIndex(grouped.index)
    mask = (grouped_index.date >= start) & (grouped_index.date <= end)
    return pd.Series(grouped.loc[mask], copy=False)


def _extract_flag_series(
    frame: pd.DataFrame | None,
    symbol: str,
    start: date,
    end: date,
) -> pd.Series | None:
    if frame is None or frame.empty:
        return None
    parsed = _filter_symbol_rows(frame=frame, symbol=symbol)
    date_col = _pick_column(parsed.columns, ("date", "日期", "交易日", "上榜日期", "统计日期"))
    if date_col is None:
        return None
    dates = pd.to_datetime(parsed[date_col], errors="coerce")
    data = pd.DataFrame({"date": dates}).dropna(subset=["date"])
    if data.empty:
        return None
    data["value"] = 1.0
    grouped = data.groupby(data["date"].dt.normalize(), as_index=True)["value"].max()
    grouped_index = pd.DatetimeIndex(grouped.index)
    mask = (grouped_index.date >= start) & (grouped_index.date <= end)
    return pd.Series(grouped.loc[mask], copy=False)


def _filter_symbol_rows(frame: pd.DataFrame, symbol: str) -> pd.DataFrame:
    symbol_col = _pick_column(frame.columns, ("代码", "code", "symbol", "股票代码"))
    if symbol_col is None:
        return frame
    normalized = frame[symbol_col].astype(str).str.extract(r"(\d{6})", expand=False).fillna("")
    target = _normalize_symbol(symbol)
    filtered = frame.loc[normalized == target]
    if filtered.empty:
        return frame
    return filtered


def _pick_column(columns: pd.Index, keywords: tuple[str, ...]) -> str | None:
    for col in columns:
        text = str(col).strip().lower()
        for keyword in keywords:
            if keyword.lower() in text:
                return str(col)
    return None


def _normalize_symbol(symbol: str) -> str:
    text = symbol.strip()
    if "." in text:
        text = text.split(".", 1)[0]
    return text
