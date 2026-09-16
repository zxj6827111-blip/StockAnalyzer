"""批量版：一次查询取全窗口所需全部 (symbol, date) 的 OHLC，再本地算 ma5/atr14。

公式**不在本文件**——统一走 `risk.overextension.overextension_inputs_from_ohlc`，
生产快照路径调的是同一份。2026-09-16 的事故正是"同一个 evaluator、两套输入"：
生产路径喂的 bar 没有 ma5/atr14，evaluator 取占位常量把闸门变成无条件否决，
而这里（harness 口径）算的是真值、显示"过热只拒 0.2~1.9%"——两边结论相反。
"""

from datetime import date, timedelta

import duckdb

from stock_analyzer.risk.overextension import overextension_inputs_from_ohlc


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
        for idx, (as_of, _h, _l, _c) in enumerate(points):
            if as_of not in day_set:
                continue
            window = points[max(0, idx - lookback_days + 1) : idx + 1]
            # 公式只此一份：与生产快照路径共用 overextension_inputs_from_ohlc。
            # 日线查询里没有 open；open 只参与 gap_pct，而本函数不返回它，
            # 故用 close 占位，不影响 bias_ma5 / atr_distance。
            inputs = overextension_inputs_from_ohlc(
                [[c, h, low, c] for _d, h, low, c in window],
                lookback_bars=lookback_days,
            )
            if inputs is None:
                continue
            metrics[f"{symbol}|{as_of.isoformat()}"] = {
                "bias_ma5": inputs.bias_ma5,
                "atr_distance": inputs.atr_distance,
            }
    return metrics
