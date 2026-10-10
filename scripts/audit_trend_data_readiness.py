"""CLI：trend 尾盘链路的数据就绪审计（只读）。

用途：在训练/回测之前先回答"这套数据能不能支撑 14:30-14:50 尾盘契约的成交模拟"。
只读探测，不改任何库；退出码 0=ready / 3=insufficient / 5=blocked（真实退出码）。

``--minute-db`` 指向 ``scripts/sync_tail_minute_bars.py`` 的产物：仓库里的分钟表只有
日级聚合，尾盘窗口能不能重建取决于这个研究库里有没有带时刻的 bar。

``--reference-db`` 指向同一研究库里的**日级参考数据副本**（默认就是分钟库那个文件，
五类参考表和分钟表同库共存）。生产仓库有数据 ≠ 能重建标签：成交与出场判定吃的是这份
副本，它没建、口径不是 raw、日历与行情互相矛盾、或没有显式交易状态声明，都会让重建
单独记 ``insufficient_reference_data`` / ``unknown_trade_status``。不传这一项时报告会留
一条 ``reference_copy_validated = insufficient``，免得把"没校验"当成"校验通过"。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.research.tail_reference_store import (  # noqa: E402
    REFERENCE_DB_DEFAULT,
)
from stock_analyzer.research.trend_data_readiness import (  # noqa: E402
    audit_trend_data_readiness,
    format_blocking_gaps,
    readiness_exit_code,
)


def _open_read_only(path: str):
    """存在才打开；不存在返回 None，由审计报告把"没校验"如实写成一条检查项。"""
    candidate = Path(path) if path else None
    if candidate is None or not candidate.exists():
        return None
    return duckdb.connect(str(candidate), read_only=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="market.duckdb 路径（只读打开）")
    parser.add_argument(
        "--minute-db", default="",
        help="带时刻的分钟研究库（scripts/sync_tail_minute_bars.py 产物，只读）",
    )
    parser.add_argument(
        "--reference-db", default=REFERENCE_DB_DEFAULT,
        help="研究库里的日级参考数据副本（默认与分钟库同文件，只读）",
    )
    parser.add_argument("--out", default="artifacts/research/trend_data_readiness.json")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"数据库不存在: {db_path}", file=sys.stderr)
        return 5

    connection = duckdb.connect(str(db_path), read_only=True)
    minute_connection = _open_read_only(args.minute_db)
    reference_connection = _open_read_only(args.reference_db)
    try:
        report = audit_trend_data_readiness(
            connection=connection, minute_connection=minute_connection,
            reference_connection=reference_connection,
        )
    finally:
        connection.close()
        for handle in (minute_connection, reference_connection):
            if handle is not None:
                handle.close()

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
