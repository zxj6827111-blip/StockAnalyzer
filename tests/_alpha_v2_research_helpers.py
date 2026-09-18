"""Alpha V2 研究链测试共用夹具（S11–S23）。

放在 tests/ 下的普通模块（非 conftest），因为 pytest 已把 tests/ 目录加入
``sys.path``（见 tests/conftest.py），多个测试文件可以直接 import 而不会触发
conftest 的模块注册冲突。

设计要点：

- 构造的 bar 必须**自带 ``prev_close_raw`` / ``pre_close`` / ``board``**，否则
  涨跌停价无法推导，引擎会 fail-closed —— 那样测的就不是我们要测的东西；
- ``open`` 默认取"上一根收盘"，让"T+1 开盘"与"决策日收盘"在数值上可区分，
  便于断言"入场价不是决策日收盘价"。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from stock_analyzer.alpha_v2.research.panel import DailyPanel
from stock_analyzer.backtest.matcher import ExecutionMatcher
from stock_analyzer.config import BacktestMatcherConfig, LimitRuleConfig


# 连续交易日（跳过周末），便于手算 T+1 与第 h 个持有日。
# 用生成器而不是硬编码：多因子/滚动窗口（如 MA20 的 5 日斜率需要 25 根）需要更长的
# 序列，硬编码列表很容易在扩容时漏改，导致"因子静默变 NaN"这类难查的测试假象。
def _trading_days(start: date, count: int) -> list[date]:
    days: list[date] = []
    current = start
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


DAYS: list[date] = _trading_days(date(2026, 1, 5), 45)


def bar(
    symbol: str,
    day: date,
    *,
    open_: float,
    high: float,
    low: float,
    close: float,
    prev_close: float | None,
    board: str = "主板",
    is_st: bool = False,
    suspended: bool = False,
    up_limit: float | None = None,
    down_limit: float | None = None,
    price_series_mode: str | None = "raw",
    pre_close: float | None = None,
    pre_close_source: str = "source",
    turnover: float | None = None,
    float_market_cap: float = 5.0e9,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "trade_date": pd.Timestamp(day),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1_000_000.0,
        "turnover": close * 1_000_000.0 if turnover is None else turnover,
        "float_market_cap": float_market_cap,
        "board": board,
        "is_st": is_st,
        "is_delisting_risk": False,
        "suspended": suspended,
        "up_limit": up_limit,
        "down_limit": down_limit,
        "price_series_mode": price_series_mode,
        "prev_close_raw": prev_close,
        "pre_close": prev_close if pre_close is None else pre_close,
        "pre_close_source": pre_close_source,
        "listing_days_lower_bound": 500,
    }


def panel(bars: list[dict[str, Any]], *, calendar: list[date] | None = None) -> DailyPanel:
    frame = pd.DataFrame(bars)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame = frame.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    days = calendar or sorted({ts.date() for ts in frame["trade_date"]})
    return DailyPanel(
        bars=frame,
        calendar=tuple(days),
        symbols=tuple(sorted(frame["symbol"].unique())),
        source="unit_test",
        window_start=min(days),
        window_end=max(days),
    )


def matcher() -> ExecutionMatcher:
    return ExecutionMatcher(BacktestMatcherConfig(), limit_rule=LimitRuleConfig())


def walk(
    symbol: str,
    closes: list[float],
    *,
    start: float = 10.0,
    board: str = "主板",
    float_market_cap: float = 5.0e9,
    turnover_base: float = 1.0e8,
) -> list[dict[str, Any]]:
    """构造一条单调、合法（不触碰涨跌停）的 bar 序列。"""
    bars: list[dict[str, Any]] = []
    prev = start
    for day, close in zip(DAYS, closes, strict=False):
        bars.append(
            bar(
                symbol,
                day,
                open_=prev,
                high=max(prev, close) * 1.001,
                low=min(prev, close) * 0.999,
                close=close,
                prev_close=prev,
                board=board,
                float_market_cap=float_market_cap,
                turnover=turnover_base,
            )
        )
        prev = close
    return bars


def flat_panel(
    symbols: list[str],
    *,
    closes: dict[str, list[float]] | None = None,
    **kwargs: Any,
) -> DailyPanel:
    """多票面板：默认所有票走同一条温和上行的序列。"""
    default = [10.0, 10.05, 10.10, 10.15, 10.20, 10.25, 10.30]
    bars: list[dict[str, Any]] = []
    for symbol in symbols:
        series = (closes or {}).get(symbol, default)
        bars.extend(walk(symbol, series, **kwargs))
    return panel(bars)


def local_market_db() -> Path:
    """本机 market.duckdb（不存在时由调用方 skip）。"""
    return Path(__file__).resolve().parents[1] / "artifacts" / "warehouse" / "market.duckdb"


__all__ = [
    "DAYS",
    "bar",
    "flat_panel",
    "local_market_db",
    "matcher",
    "panel",
    "walk",
]
