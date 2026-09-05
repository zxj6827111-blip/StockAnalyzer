"""把 financial_snapshots 的 PIT 财务快照物化回 daily_bars 财务列。

背景：7/31 起 daily_bars 由裸行链路写入（见 docs/week5_datagate_root_cause_20260905.md），
财务三字段（financial_data_complete/roe/debt_ratio）全空。本驱动在
``backfill_financial_snapshots.py`` 重建 financial_snapshots 后执行：

    warehouse.apply_financial_snapshots_to_daily(symbol)

对每个有 PIT 快照的 symbol 重算全部日期的财务列（ann_date 语义，PIT 安全）。
幂等：可重复执行。只写 daily_bars 的财务列（整行 replace），不动其他表。

锁注意：与生产 cron（21:30/22:30 日线同步）互斥，请在无同步窗口运行；
对锁冲突做有限重试后跳过该 symbol（下轮重跑即可补齐）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_analyzer.config import load_config  # noqa: E402
from stock_analyzer.data.market_warehouse import MarketWarehouse  # noqa: E402


def _symbols_with_snapshots(db_path: str) -> list[str]:
    con = duckdb.connect(db_path, read_only=True)
    try:
        rows = con.execute(
            "SELECT DISTINCT symbol FROM financial_snapshots ORDER BY 1"
        ).fetchall()
    finally:
        con.close()
    return [str(r[0]) for r in rows]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/app/config/default.yaml")
    parser.add_argument("--market-db", default="")
    parser.add_argument("--limit", type=int, default=0, help="本次最多处理 symbol 数（0=不限）")
    parser.add_argument("--retry", type=int, default=3, help="锁冲突重试次数")
    parser.add_argument("--retry-sleep-sec", type=float, default=10.0)
    args = parser.parse_args()

    config = load_config(args.config)
    runtime_data_source = config.data_source
    db_path = args.market_db or str(runtime_data_source.warehouse_db_path)

    symbols = _symbols_with_snapshots(db_path)
    if args.limit > 0:
        symbols = symbols[: args.limit]
    print(f"[1] symbols with PIT snapshots: {len(symbols)}", flush=True)
    if not symbols:
        print("nothing to apply", flush=True)
        return 2

    warehouse = MarketWarehouse(
        db_path=db_path,
        package_root=str(Path(db_path).parent / "package"),
        package_writes_enabled=False,
    )

    ok = 0
    failed: list[dict[str, str]] = []
    started = time.time()
    for index, symbol in enumerate(symbols):
        applied = False
        for attempt in range(args.retry):
            try:
                warehouse.apply_financial_snapshots_to_daily(symbol=symbol)
                applied = True
                break
            except Exception as exc:  # noqa: BLE001
                text = f"{type(exc).__name__}: {exc}".lower()
                retriable = "lock" in text or "conflict" in text
                if not retriable or attempt >= args.retry - 1:
                    failed.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
                    print(f"[FAIL] {symbol} {type(exc).__name__}: {exc}", flush=True)
                    break
                time.sleep(args.retry_sleep_sec)
        if applied:
            ok += 1
        if (index + 1) % 50 == 0:
            print(
                f"[2] progress {index + 1}/{len(symbols)} ok={ok} failed={len(failed)} "
                f"elapsed={time.time() - started:.0f}s",
                flush=True,
            )

    summary = {
        "finished_at": datetime.now().isoformat(),
        "symbols_total": len(symbols),
        "ok": ok,
        "failed": len(failed),
        "failures": failed[:50],
        "total_seconds": round(time.time() - started, 1),
    }
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
