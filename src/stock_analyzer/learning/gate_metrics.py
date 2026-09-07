"""批量版：一次查询取全窗口所需全部 (symbol, date) 的 OHLC，再本地算 ma5/atr14。"""
import time
from datetime import date, timedelta

import duckdb
import pandas as pd


def gate_metrics_batch(
    market_db: str,
    symbols: list[str],
    days: list[date],
    lookback_days: int = 20,
) -> dict[str, dict[str, float]]:
    """返回 f"{symbol}|{date.isoformat()}" -> {bias_ma5, atr_distance}。

    一次拉取 [min(days)-30d, max(days)] 全部日线，按 symbol 分组本地计算，
    对每个 (symbol, as_of=day) 取截至 day 的最近 lookback 根。
    """
    if not symbols or not days:
        return {}
    start = min(days) - timedelta(days=45)
    end = max(days)
    con = duckdb.connect(market_db, read_only=True)
    try:
        rows = con.execute(
            """
            SELECT symbol, date, close, high, low
            FROM daily_bars
            WHERE symbol IN (SELECT UNNEST(?))
              AND date >= CAST(? AS DATE) AND date <= CAST(? AS DATE)
            ORDER BY symbol, date
            """,
            [list(symbols), start.isoformat(), end.isoformat()],
        ).fetchall()
    finally:
        con.close()

    series: dict[str, list[tuple[date, float, float, float]]] = {}
    for symbol, d, close, high, low in rows:
        d2 = d if isinstance(d, date) else date.fromisoformat(str(d))
        series.setdefault(str(symbol), []).append((d2, float(high), float(low), float(close)))

    day_set = set(days)
    metrics: dict[str, dict[str, float]] = {}
    for symbol, points in series.items():
        n = len(points)
        for idx, (as_of, _h, _l, _c) in enumerate(points):
            if as_of not in day_set:
                continue
            window = points[max(0, idx - lookback_days + 1) : idx + 1]
            if len(window) < 6:
                continue
            closes = [p[3] for p in window]
            ma5 = sum(closes[-5:]) / 5.0
            trs: list[float] = []
            for i in range(1, len(window)):
                prev_close = window[i - 1][3]
                high_i, low_i = window[i][1], window[i][2]
                trs.append(max(high_i - low_i, abs(high_i - prev_close), abs(low_i - prev_close)))
            recent = trs[-14:]
            atr14 = sum(recent) / len(recent)
            close = closes[-1]
            bias = abs(close / ma5 - 1.0) if ma5 > 0 else 0.0
            atr_distance = abs(close - ma5) / atr14 if atr14 > 0 else 0.0
            metrics[f"{symbol}|{as_of.isoformat()}"] = {
                "bias_ma5": bias,
                "atr_distance": atr_distance,
            }
    return metrics
