"""把 vendor 分钟 ZIP 里的**带时刻** bar 落到独立研究库（改进计划 §3.1 / §5）。

只在本地跑，不碰生产仓库、不进 NAS 调度：

```bash
python scripts/sync_tail_minute_bars.py \\
    --root /path/to/tdx_offline_package \\
    --out artifacts/research/tail_minute_bars.duckdb \\
    --price-basis raw --bar-time-semantics bar_end \\
    --symbols-file artifacts/research/tail_symbols.txt
```

``--price-basis`` 与 ``--bar-time-semantics`` 故意没有默认值：分钟价的复权口径和
bar 时刻语义决定了尾盘成交模拟对不对，猜不得。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.data.intraday_summary_builder import archive_paths  # noqa: E402
from stock_analyzer.research.minute_bar_store import (  # noqa: E402
    MINUTE_TABLES,
    MinuteBarStore,
    MinuteStoreError,
    coverage_report,
    read_vendor_zip_minutes,
)


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(str(value)) if value else None


def _load_symbols(args: argparse.Namespace) -> list[str] | None:
    if args.symbols:
        return [item.strip() for item in args.symbols.split(",") if item.strip()]
    if args.symbols_file:
        lines = Path(args.symbols_file).expanduser().read_text(encoding="utf-8").splitlines()
        return [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    return None


def run(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(str(args.root)).expanduser()
    if not root.is_dir():
        raise MinuteStoreError(f"vendor root does not exist: {root}")
    cutoff = _parse_date(args.start) or date.min
    archives = archive_paths(root, interval=args.interval, cutoff=cutoff)
    if args.archive:
        archives = [Path(str(args.archive)).expanduser()]
    if not archives:
        raise MinuteStoreError(
            f"no {args.interval} minute archives under {root} covering {cutoff}; "
            "研究库不写空报告 —— 先确认源包路径与命名"
        )
    symbols = _load_symbols(args)
    if symbols and args.limit_symbols:
        symbols = symbols[: int(args.limit_symbols)]

    store = MinuteBarStore(args.out)
    per_archive: list[dict[str, Any]] = []
    total_rows = 0
    try:
        for path in archives:
            frame = read_vendor_zip_minutes(
                path,
                symbols=symbols,
                volume_multiplier=args.volume_multiplier,
                amount_multiplier=args.amount_multiplier,
                start=_parse_date(args.start),
                end=_parse_date(args.end),
            )
            written = store.upsert_frame(
                frame,
                interval=args.interval,
                price_basis=args.price_basis,
                bar_time_semantics=args.bar_time_semantics,
                source="vendor_zip",
            )
            total_rows += written
            per_archive.append({
                "archive": path.name,
                "rows_read": int(len(frame)),
                "rows_written": written,
                "symbols": sorted({str(item) for item in frame["symbol"]})[:5]
                if not frame.empty else [],
            })
        report = {
            "ok": total_rows > 0,
            "root": str(root),
            "out": str(Path(str(args.out)).expanduser()),
            "table": MINUTE_TABLES[args.interval],
            "interval": args.interval,
            "price_basis": args.price_basis,
            "bar_time_semantics": args.bar_time_semantics,
            "archives": len(archives),
            "rows_written": total_rows,
            "per_archive": per_archive[-10:],
            "coverage": coverage_report(
                store,
                interval=args.interval,
                start=_parse_date(args.start),
                end=_parse_date(args.end),
            ),
        }
    finally:
        store.close()
    if total_rows <= 0:
        report["error"] = "no minute rows ingested; 源 ZIP 里读不到带 datetime 的 bar"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="vendor 离线包根目录")
    parser.add_argument("--out", default="artifacts/research/tail_minute_bars.duckdb")
    parser.add_argument("--interval", default="1m", choices=sorted(MINUTE_TABLES))
    parser.add_argument("--archive", default="", help="只处理单个 ZIP（调试用）")
    parser.add_argument("--start", default="", help="YYYY-MM-DD")
    parser.add_argument("--end", default="", help="YYYY-MM-DD")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--symbols-file", default="")
    parser.add_argument("--limit-symbols", type=int, default=0)
    parser.add_argument("--price-basis", required=True, choices=("raw", "qfq"))
    parser.add_argument(
        "--bar-time-semantics", required=True, choices=("bar_end", "bar_start")
    )
    parser.add_argument("--volume-multiplier", type=float, default=100.0)
    parser.add_argument("--amount-multiplier", type=float, default=1.0)
    parser.add_argument("--report", default="artifacts/research/tail_minute_bars_sync.json")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    try:
        report = run(args)
    except MinuteStoreError as exc:
        print(f"sync_tail_minute_bars blocked: {exc}", file=sys.stderr)
        return 5

    if args.report:
        path = Path(str(args.report)).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if not args.quiet:
        tail = report["coverage"]["tail_window"]
        print(json.dumps({
            "rows_written": report["rows_written"],
            "distinct_days": report["coverage"]["distinct_days"],
            "first_day": report["coverage"]["first_day"],
            "last_day": report["coverage"]["last_day"],
            "tail_window_status": tail["status"],
            "complete_symbol_days": tail["complete_symbol_days"],
            "symbol_days": tail["symbol_days"],
        }, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 5


if __name__ == "__main__":
    raise SystemExit(main())
