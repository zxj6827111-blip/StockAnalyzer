"""CLI：trend 尾盘链路的数据就绪审计（只读）。

用途：在训练/回测之前先回答"这套数据能不能支撑 14:30-14:50 尾盘契约的成交模拟"。
只读探测，不改任何库；退出码 0=ready / 3=insufficient / 5=blocked（真实退出码）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

from stock_analyzer.research.trend_data_readiness import (
    audit_trend_data_readiness,
    format_blocking_gaps,
    readiness_exit_code,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="market.duckdb 路径（只读打开）")
    parser.add_argument(
        "--minute-db", default="",
        help="带时刻的分钟研究库（scripts/sync_tail_minute_bars.py 产物，只读）",
    )
    parser.add_argument("--out", default="artifacts/research/trend_data_readiness.json")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"数据库不存在: {db_path}", file=sys.stderr)
        return 5

    connection = duckdb.connect(str(db_path), read_only=True)
    minute_connection = (
        duckdb.connect(str(Path(args.minute_db)), read_only=True)
        if args.minute_db and Path(args.minute_db).exists() else None
    )
    try:
        report = audit_trend_data_readiness(
            connection=connection, minute_connection=minute_connection
        )
    finally:
        connection.close()
        if minute_connection is not None:
            minute_connection.close()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    if not args.quiet:
        print(f"readiness = {report['readiness']}")
        gaps = format_blocking_gaps(report)
        print(gaps if gaps else "所有就绪项均达标")
        print(f"报告已写入 {out_path}")
    return readiness_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
