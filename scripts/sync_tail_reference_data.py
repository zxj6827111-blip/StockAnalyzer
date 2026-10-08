"""把 §3.1 要求的五类参考数据从生产仓库**只读**复制进独立研究库。

```bash
python scripts/sync_tail_reference_data.py \\
    --warehouse data/market.duckdb \\
    --out artifacts/research/tail_minute_bars.duckdb \\
    --start 2025-01-02 --end 2026-09-30 \\
    --symbols-file artifacts/research/tail_symbols.txt
```

研究库默认与分钟库同文件：尾盘标签与历史重建要同时吃分钟 bar 和日级参考数据。
生产仓库只以 ``read_only`` 打开，不会被这个命令改动。

退出码是**真实退出码**：

- ``0`` 五类来源（日历 / RAW 日线 / 精确涨跌停 / 停复牌 / 证券历史状态）都落到研究库
- ``3`` 有来源缺失（仓库里没这张表，或该表在窗口内没有行）→ 如实记为不足、继续采集；
  不得用比例近似或填 0 冒充已补齐
- ``5`` 输入不可用或口径不成立（仓库文件不存在、日线不是 raw 口径）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.research.tail_reference_store import (  # noqa: E402
    REFERENCE_DB_DEFAULT,
    TailReferenceError,
    TailReferenceStore,
    read_warehouse_reference_frames,
    sync_reference_from_warehouse,
)

RC_OK = 0
RC_INSUFFICIENT = 3
RC_ERROR = 5


def _load_symbols(args: argparse.Namespace) -> list[str] | None:
    if args.symbols:
        return [item.strip() for item in args.symbols.split(",") if item.strip()]
    if args.symbols_file:
        lines = Path(args.symbols_file).read_text(encoding="utf-8").splitlines()
        return [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warehouse", required=True, help="生产仓库 market.duckdb（只读）")
    parser.add_argument("--out", default=REFERENCE_DB_DEFAULT)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--symbols", default="")
    parser.add_argument("--symbols-file", default="")
    parser.add_argument("--exchange", default="SSE")
    parser.add_argument("--report", default="")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    try:
        start = date.fromisoformat(str(args.start)[:10])
        end = date.fromisoformat(str(args.end)[:10])
    except ValueError as exc:
        print(f"--start/--end 必须是 ISO 日期: {exc}", file=sys.stderr)
        return RC_ERROR
    if start > end:
        print("--start 晚于 --end", file=sys.stderr)
        return RC_ERROR

    try:
        symbols = _load_symbols(args)
        reading = read_warehouse_reference_frames(
            args.warehouse, start=start, end=end, symbols=symbols,
        )
        with TailReferenceStore(args.out) as store:
            result = sync_reference_from_warehouse(store, reading, exchange=args.exchange)
    except (TailReferenceError, OSError, ValueError) as exc:
        print(f"参考数据不可用: {type(exc).__name__}: {exc}", file=sys.stderr)
        return RC_ERROR

    result["warehouse"] = str(args.warehouse)
    result["out"] = str(args.out)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
        )
    if not args.quiet:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    if result["missing_sources"]:
        print(f"来源不足: {result['missing_sources']}")
        print("研究库只补到了部分参考数据；缺失的来源继续采集，不得用近似值顶替")
        return RC_INSUFFICIENT
    return RC_OK


if __name__ == "__main__":
    raise SystemExit(main())
