"""Shared market-relative feature helpers."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from stock_analyzer.config import MarketRelativeFeatureConfig
from stock_analyzer.data.provider import MarketDataProvider


def build_market_relative_frame(
    provider: MarketDataProvider,
    *,
    bars: pd.DataFrame,
    config: MarketRelativeFeatureConfig,
) -> pd.DataFrame:
    ordered_bars = _normalize_price_frame(bars)
    if ordered_bars.empty or not bool(config.enabled):
        return pd.DataFrame(index=ordered_bars.index)

    end_date = ordered_bars.index.max().date()
    benchmark_bars = fetch_market_benchmark_bars(
        provider,
        lookback_days=max(120, len(ordered_bars) + 5),
        primary_symbol=str(config.benchmark_symbol).strip() or "000300",
        fallback_symbol=str(config.fallback_symbol).strip() or "399001",
        end_date=end_date,
    )
    return build_market_index_frame(bars=ordered_bars, benchmark_bars=benchmark_bars)


def fetch_market_benchmark_bars(
    provider: MarketDataProvider,
    *,
    lookback_days: int,
    primary_symbol: str = "000300",
    fallback_symbol: str = "399001",
    end_date: date | None = None,
) -> pd.DataFrame:
    candidates: list[str] = []
    for raw_symbol in (primary_symbol, fallback_symbol):
        symbol = str(raw_symbol).strip()
        if symbol and symbol not in candidates:
            candidates.append(symbol)

    errors: list[str] = []
    for symbol in candidates:
        try:
            bars = _fetch_daily_bars_compat(
                provider,
                symbol=symbol,
                lookback_days=max(120, int(lookback_days)),
                end_date=end_date,
            )
        except Exception as exc:
            errors.append(f"{symbol}:{exc.__class__.__name__}:{exc}")
            continue
        if bars.empty:
            errors.append(f"{symbol}:empty")
            continue
        ordered = _normalize_price_frame(bars)
        if ordered.empty or "close" not in ordered.columns:
            errors.append(f"{symbol}:close_missing")
            continue
        ordered.attrs["benchmark_symbol"] = symbol
        return ordered

    raise RuntimeError(
        "market_benchmark_unavailable: " + ";".join(errors or ["no_candidate_symbols"])
    )


def build_market_index_frame(
    *,
    bars: pd.DataFrame,
    benchmark_bars: pd.DataFrame,
) -> pd.DataFrame:
    ordered_bars = _normalize_price_frame(bars)
    ordered_benchmark = _normalize_price_frame(benchmark_bars)
    if ordered_bars.empty or ordered_benchmark.empty:
        return pd.DataFrame(index=ordered_bars.index)

    bar_close = pd.to_numeric(ordered_bars["close"], errors="coerce").astype(float)
    benchmark_close = pd.to_numeric(ordered_benchmark["close"], errors="coerce").astype(float)

    benchmark_frame = pd.DataFrame(index=ordered_benchmark.index)
    benchmark_frame["benchmark_ret_1d"] = benchmark_close.pct_change()
    benchmark_frame["benchmark_ret_5d"] = benchmark_close.pct_change(5)
    benchmark_frame["benchmark_ret_20d"] = benchmark_close.pct_change(20)
    benchmark_ma20 = benchmark_close.rolling(20, min_periods=1).mean()
    benchmark_frame["benchmark_above_ma20"] = (benchmark_close > benchmark_ma20).astype(float)

    aligned = benchmark_frame.reindex(ordered_bars.index).ffill()
    stock_ret_1d = bar_close.pct_change()
    stock_ret_5d = bar_close.pct_change(5)
    aligned["excess_ret_1d"] = stock_ret_1d - aligned["benchmark_ret_1d"]
    aligned["excess_ret_5d"] = stock_ret_5d - aligned["benchmark_ret_5d"]

    benchmark_ret_1d = aligned["benchmark_ret_1d"]
    beta_20d = stock_ret_1d.rolling(20, min_periods=20).cov(benchmark_ret_1d)
    beta_20d = beta_20d / benchmark_ret_1d.rolling(20, min_periods=20).var(ddof=0).replace(
        0.0, np.nan
    )
    beta_60d = stock_ret_1d.rolling(60, min_periods=60).cov(benchmark_ret_1d)
    beta_60d = beta_60d / benchmark_ret_1d.rolling(60, min_periods=60).var(ddof=0).replace(
        0.0, np.nan
    )
    aligned["beta_20d"] = beta_20d
    aligned["beta_60d"] = beta_60d

    columns = [
        "benchmark_ret_1d",
        "benchmark_ret_5d",
        "benchmark_ret_20d",
        "excess_ret_1d",
        "excess_ret_5d",
        "beta_20d",
        "beta_60d",
        "benchmark_above_ma20",
    ]
    result = aligned.reindex(columns=columns)
    result.attrs["benchmark_symbol"] = str(
        ordered_benchmark.attrs.get("benchmark_symbol", "")
    ).strip()
    return result


def _normalize_price_frame(frame: pd.DataFrame) -> pd.DataFrame:
    prepared = frame.copy()
    if not isinstance(prepared.index, pd.DatetimeIndex):
        if "trade_date" in prepared.columns:
            prepared.index = pd.to_datetime(prepared["trade_date"], errors="coerce")
        elif "date" in prepared.columns:
            prepared.index = pd.to_datetime(prepared["date"], errors="coerce")
        else:
            prepared.index = pd.to_datetime(prepared.index, errors="coerce")
    prepared = prepared[prepared.index.notna()].sort_index()
    if prepared.empty:
        return prepared
    return prepared.loc[~prepared.index.duplicated(keep="last")]


def _fetch_daily_bars_compat(
    provider: MarketDataProvider,
    *,
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
    except TypeError:
        return provider.fetch_daily_bars(symbol=symbol, lookback_days=lookback_days)
