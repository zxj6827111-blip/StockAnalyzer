#!/usr/bin/env python3
"""产生**可复现的**漏斗第一层输入：从仓库日线导出全市场符号清单 + 来源留痕。

为什么要有这个脚本（计划 §2 / 质量报告 §3e、§3f）：归档 `universe` 层此前喂的是一份
研究侧手工清单（900 只），仓库里没有产生它的代码，所以"全市场 → 硬性资格检查"这层
无法追溯，而实测全市场过了所有硬门的 symbol-day 里有 **76.4% 根本不在那份清单里**。
本脚本把第一层的输入变成"由仓库导出、可用 SQL 与摘要复现"的工件，让重放与留档能真的
从全市场起算。

导出口径要写清，不可当成品用：
- 这里只做**符号枚举**，不做资格判定（硬门在 `replay_tail_candidate_pool.py` 里按契约判）；
- "当天有 bar"是数据可用性事实，不等于"当天可交易"（缺 bar 不当停牌，也不当退市）；
- 清单里含历史不足 120 根的新股：它们会在硬门层被 `insufficient_history_at_asof` 归位，
  不在这里预先削掉 —— 否则第一层的输入又变成一次未留档的前置筛选。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb

_RC_OK = 0
_RC_ERROR = 5

#: 与 scripts/replay_tail_candidate_pool.py 的 warm-up 口径一致：往回取 400 个日历日，
#: 让决策窗口里的每只票都有机会凑满滚动窗口所需的历史。
WARM_UP_CALENDAR_DAYS = 400

_EXPORT_SQL = """
SELECT DISTINCT substr(CAST(symbol AS VARCHAR), 1, 6) AS symbol
FROM daily_bars
WHERE date BETWEEN ? AND ?
ORDER BY symbol
"""


def export_symbols(warehouse: Path, start: date, end: date) -> tuple[list[str], int]:
    """返回（符号清单, 窗口内出现过的 bar 行数）。表不存在就直接失败，不返回空清单。"""
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        tables = {str(row[0]) for row in con.execute("SHOW TABLES").fetchall()}
        if "daily_bars" not in tables:
            raise SystemExit("daily_bars table missing — 全市场清单不能凭空产生")
        symbols = [str(row[0]) for row in con.execute(
            _EXPORT_SQL, [start - timedelta(days=WARM_UP_CALENDAR_DAYS), end]
        ).fetchall()]
        rows = int(con.execute(
            "SELECT count(*) FROM daily_bars WHERE date BETWEEN ? AND ?",
            [start - timedelta(days=WARM_UP_CALENDAR_DAYS), end],
        ).fetchone()[0])
    finally:
        con.close()
    if not symbols:
        raise SystemExit("no symbols in the requested window")
    return symbols, rows


def run(args: argparse.Namespace) -> dict[str, Any]:
    warehouse = Path(str(args.warehouse)).expanduser()
    if not warehouse.exists():
        raise SystemExit(f"warehouse not found: {warehouse}")
    start, end = date.fromisoformat(str(args.start)[:10]), date.fromisoformat(str(args.end)[:10])
    symbols, bar_rows = export_symbols(warehouse, start, end)
    out = Path(str(args.out)).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(symbols) + "\n"
    out.write_text(body, encoding="utf-8")
    provenance = {
        "ok": True,
        "producer": str(Path(__file__).name),
        "warehouse": str(warehouse),
        "window": [start.isoformat(), end.isoformat()],
        "warm_up_calendar_days": WARM_UP_CALENDAR_DAYS,
        "query": " ".join(_EXPORT_SQL.split()),
        "distinct_symbols": len(symbols),
        "daily_bar_rows_in_window": bar_rows,
        "symbols_file": str(out),
        "symbols_file_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "semantics": "符号枚举，不含资格判定；缺 bar 不当停牌也不当退市；历史不足交给硬门归位",
    }
    if args.provenance:
        target = Path(str(args.provenance)).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    return provenance


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warehouse", required=True)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--out", required=True, help="导出的符号清单文件（逐行 6 位代码）")
    parser.add_argument("--provenance", default="", help="来源留痕 JSON；不给就只打到 stdout")
    args = parser.parse_args(argv)
    try:
        provenance = run(args)
    except SystemExit as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return _RC_ERROR
    if not args.provenance:
        print(json.dumps(provenance, ensure_ascii=False, indent=2))
    return _RC_OK


if __name__ == "__main__":
    raise SystemExit(main())
