"""holder_count 回填（尾巴②）——tushare stk_holdernumber（披露日 ffill 语义）。

股东户数为季度披露数据（非逐日）：按 symbol 拉最近几期披露
（end_date/ann_date/holder_n），按 ``ann_date <= as_of`` 的最新一期值写回
daily_bars.holder_count（与旧链 akshare adapter 的 ffill 口径一致）。

范围：2024-09-01 起（Phase 2 数据窗口）；限频 walk（逐 symbol 一次调用，
0.35s 间隔）；幂等（重复执行只覆盖同列）。写 market.duckdb。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import duckdb  # noqa: E402
import pandas as pd  # noqa: E402

from stock_analyzer.data.tushare_provider import (  # noqa: E402
    _HttpTushareProApi,
    _resolve_tushare_token,
)

MARKET_DB = "/app/artifacts/warehouse/market.duckdb"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", default="2024-09-01")
    parser.add_argument("--market-db", default=MARKET_DB)
    parser.add_argument("--request-interval-sec", type=float, default=0.35)
    parser.add_argument("--limit", type=int, default=0, help="测试用：只处理前 N 只")
    args = parser.parse_args()

    token = _resolve_tushare_token()
    if not token:
        print("no tushare token", flush=True)
        return 2
    http = _HttpTushareProApi(token=token, timeout_sec=30.0)

    con = duckdb.connect(args.market_db)
    symbols = [
        str(r[0])
        for r in con.execute(
            "SELECT DISTINCT symbol FROM daily_bars WHERE date >= CAST(? AS DATE) ORDER BY 1",
            [args.start_date],
        ).fetchall()
    ]
    if args.limit > 0:
        symbols = symbols[: args.limit]
    print(f"[1] symbols: {len(symbols)}", flush=True)

    updated_symbols = 0
    failed = 0
    started = time.time()
    for index, symbol in enumerate(symbols):
        try:
            frame = http._call("stk_holdernumber", ts_code=f"{symbol}.SZ")  # noqa: SLF001
            if frame is None or frame.empty:
                frame = http._call("stk_holdernumber", ts_code=f"{symbol}.SH")  # noqa: SLF001
            if frame is None or frame.empty:
                failed += 1
                time.sleep(max(0.0, args.request_interval_sec))
                continue
            records = frame.to_dict("records")
            # ann_date 升序；写「ann_date <= as_of 的最新披露」。
            events = []
            for row in records:
                ann = str(row.get("ann_date") or "").strip()
                end = str(row.get("end_date") or "").strip()
                holders = row.get("holder_num")
                if len(ann) != 8 or holders is None:
                    continue
                ann_iso = f"{ann[:4]}-{ann[4:6]}-{ann[6:8]}"
                events.append((ann_iso, float(holders), end))
            if not events:
                failed += 1
                time.sleep(max(0.0, args.request_interval_sec))
                continue
            events.sort(key=lambda e: e[0])
            payload = pd.DataFrame(
                {
                    "ann_date": [e[0] for e in events],
                    "holder_count": [e[1] for e in events],
                }
            )
            con.register("hc_stage", payload)
            # 每个 trade_date 取 ann_date <= trade_date 的最新一期（ffill 语义）。
            con.execute(
                """
                UPDATE daily_bars SET holder_count = (
                    SELECT hc.holder_count FROM hc_stage hc
                    WHERE hc.ann_date <= CAST(daily_bars.date AS VARCHAR)
                    ORDER BY hc.ann_date DESC LIMIT 1
                )
                WHERE symbol = ? AND date >= CAST(? AS DATE)
                """,
                [symbol, args.start_date],
            )
            con.unregister("hc_stage")
            updated_symbols += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[warn] {symbol}: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(max(0.0, args.request_interval_sec))
        if (index + 1) % 500 == 0:
            print(
                f"[2] {index + 1}/{len(symbols)} ok={updated_symbols} failed={failed} "
                f"{time.time() - started:.0f}s",
                flush=True,
            )

    # 覆盖率核验
    coverage = con.execute(
        "SELECT COUNT(*) FILTER (WHERE holder_count IS NOT NULL)::DOUBLE / COUNT(*) "
        "FROM daily_bars WHERE date >= CAST(? AS DATE)",
        [args.start_date],
    ).fetchone()[0]
    con.close()
    print(
        json.dumps(
            {
                "updated_symbols": updated_symbols,
                "failed": failed,
                "holder_count_coverage_since_start": round(float(coverage), 4),
                "total_seconds": round(time.time() - started, 1),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
