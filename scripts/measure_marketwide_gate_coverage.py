#!/usr/bin/env python3
"""全市场口径下重算硬性资格检查：量化"研究侧 900 只清单"遮蔽了多少合格候选。

为什么要单独测（计划 §2 的第一个诊断问题）：漏斗归档的 `universe` 层输入就是那份人工清单
（见质量报告 §3e），清单外的票从来没进过统计，所以"前置筛选是否过早淘汰了适合短期上涨的
股票"此前无法回答。这里不改任何生产判定，只用仓库日线按**同一套已登记为 HARD 的规则**
重跑一遍，输出"过了所有硬门但不在研究清单里"的 symbol-day 数量。

阈值口径必须写清：流动性与浮盈市值下限在生产里是**横截面分位**，全市场与池内算出的分位数
天然不同。本脚本按"每天在全市场上取同一分位"重算阈值，因此它回答的是
"这条规则在全市场上的形状"，**不能**拿来替换池内那组绝对阈值（两份数不可互相校验）。
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
import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.feature.trend_candidate_contract import (  # noqa: E402
    HARD_GATE_ATTRIBUTION_ORDER,
    unproven_float_market_cap_mask,
)
from stock_analyzer.research.float_cap_reference import apply_float_cap_reference  # noqa: E402

_RC_OK = 0
_RC_ERROR = 5

#: 与 scripts/replay_tail_candidate_pool.py 同源的两个数据完整性参数。
MIN_HISTORY_BARS = 120
MAX_BAR_GAP_CALENDAR_DAYS = 30
#: 与 replay 的 A_SHARE_CODE_PREFIXES 同一口径：沪主板/深主板/创业板/科创板/北交所。
SH_ELIGIBLE_PREFIXES = ("60", "00", "30", "68", "4", "8")


def load_market_frame(warehouse: Path, start: date, end: date) -> pd.DataFrame:
    """硬门要用的列 + 每只票的上一根 bar 日期（warm-up 往前推 400 天凑 120 根历史）。"""
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        frame = con.execute(
            """
            SELECT symbol, date, turnover, float_market_cap, is_st, is_delisting_risk,
                   suspended
            FROM daily_bars
            WHERE date BETWEEN ? AND ?
            ORDER BY symbol, date
            """,
            [start - timedelta(days=400), end],
        ).fetch_df()
    finally:
        con.close()
    if frame.empty:
        return frame
    frame["symbol"] = frame["symbol"].astype(str).str.split(".").str[0].str.zfill(6)
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    frame["prev_bar_date"] = frame.groupby("symbol")["date"].shift(1)
    # 每只票按日期排序后的序号：当天这根 bar 就是它的第 bar_index+1 根，
    # 于是"as_of 前有没有 120 根历史"可以逐行直接判，不必对全表重算 groupby。
    frame["bar_ordinal"] = frame.groupby("symbol").cumcount() + 1
    return frame


def _truthy(column: pd.Series) -> pd.Series:
    return pd.to_numeric(column, errors="coerce").fillna(0) > 0


def day_funnel(frame: pd.DataFrame, day: date, *, min_turnover: float,
               min_float_cap: float) -> dict[str, Any]:
    """一天的全市场硬门漏斗：输入 = 当天有 bar 的符号，淘汰按契约归因顺序取第一条。"""
    todays = frame.loc[frame["date"] == day]
    if todays.empty:
        return {"day": day.isoformat(), "inputs": 0, "advanced": 0, "rejected": {},
                "float_cap_gate_evaluable": True, "advanced_symbols": []}
    code = todays["symbol"]
    hits = {
        "board_eligibility": ~code.str.startswith(
            tuple(p for p in SH_ELIGIBLE_PREFIXES if len(p) == 2)
        ) & ~code.str.slice(0, 1).isin(("4", "8")),
        "is_st": _truthy(todays["is_st"]),
        "is_delisting_risk": _truthy(todays["is_delisting_risk"]),
        "suspended": _truthy(todays["suspended"]),
        "min_avg_turnover_20": todays["turnover"] < min_turnover,
        # 占位常量那一格不是观测值：市值门对它无从判定，单独记 unproven_float_market_cap
        # 出局。放它过去比较的话 ``value < threshold`` 恒假，等于白送一个"已通过"（§3g）。
        "min_float_market_cap": (
            ~unproven_float_market_cap_mask(todays["float_market_cap"])
        ) & (todays["float_market_cap"] < min_float_cap),
        "unproven_float_market_cap": unproven_float_market_cap_mask(
            todays["float_market_cap"]
        ),
        "stale_market_data": pd.to_datetime(todays["date"].astype(str)) - pd.to_datetime(
            todays["prev_bar_date"].astype(str)
        ) > pd.Timedelta(days=MAX_BAR_GAP_CALENDAR_DAYS),
    }
    claimed = pd.Series(False, index=todays.index)
    rejected: dict[str, int] = {}
    for rule in HARD_GATE_ATTRIBUTION_ORDER:
        if rule not in hits:
            continue
        newly = hits[rule].fillna(False) & ~claimed
        rejected[rule] = int(newly.sum())
        claimed |= newly
    history_ok = todays["bar_ordinal"] >= MIN_HISTORY_BARS
    # 历史不足的票按契约归到 insufficient_history_at_asof（它排在归因顺序末位）。
    rejected["insufficient_history_at_asof"] = int((~history_ok & ~claimed).sum())
    claimed |= ~history_ok
    keep = ~claimed
    advanced_symbols = sorted(todays.loc[keep, "symbol"].tolist())
    return {
        "day": day.isoformat(),
        "inputs": int(len(todays)),
        "advanced": int(len(advanced_symbols)),
        "rejected": rejected,
        "advanced_symbols": advanced_symbols,
        # 阈值 NaN = 当天**没有任何测过的市值**，这条门整日无从判定；必须留痕，
        # 不能让"门没淘汰任何票"被读成"所有票都过了市值门"。
        "float_cap_gate_evaluable": bool(pd.notna(min_float_cap)),
        "identity_ok": int(len(todays)) == len(advanced_symbols) + sum(rejected.values()),
    }


def summarize(funnels: list[dict[str, Any]], pool: set[str]) -> dict[str, Any]:
    totals: dict[str, int] = {}
    inputs = advanced = invisible_days = 0
    invisible: set[str] = set()
    broken_identity = 0
    cap_days_unevaluable = 0
    per_day = []
    for item in funnels:
        if not item.get("inputs"):
            continue
        inputs += item["inputs"]
        advanced += item["advanced"]
        broken_identity += 0 if item.get("identity_ok") else 1
        cap_days_unevaluable += 0 if item.get("float_cap_gate_evaluable", True) else 1
        for rule, count in item["rejected"].items():
            totals[rule] = totals.get(rule, 0) + count
        outside = [symbol for symbol in item["advanced_symbols"] if symbol not in pool]
        invisible_days += len(outside)
        invisible.update(outside)
        per_day.append({"day": item["day"], "inputs": item["inputs"],
                        "advanced": item["advanced"], "advanced_outside_pool": len(outside)})
    return {
        "decision_days": len(per_day),
        "counting_broken_days": broken_identity,
        "float_cap_gate_days_unevaluable": cap_days_unevaluable,
        "marketwide_symbol_days_input": inputs,
        "marketwide_symbol_days_advanced": advanced,
        "marketwide_rejection_by_first_reason": dict(sorted(totals.items())),
        "eligible_symbol_days_missing_from_research_list": invisible_days,
        "distinct_symbols_eligible_but_missing_from_research_list": len(invisible),
        "research_list_size": len(pool),
        "per_day_head": per_day[:5],
    }


def _day(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def run(args: argparse.Namespace) -> dict[str, Any]:
    warehouse = Path(str(args.warehouse)).expanduser()
    if not warehouse.exists():
        raise SystemExit(f"warehouse not found: {warehouse}")
    pool_file = Path(str(args.symbols_file)).expanduser()
    pool = {
        line.strip().split(".")[0].zfill(6)
        for line in pool_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    start, end = _day(args.start), _day(args.end)
    frame = load_market_frame(warehouse, start, end)
    if frame.empty:
        raise SystemExit("no daily bars in the requested window")
    cap_reference: dict[str, Any] = {}
    if args.float_cap_ref_db:
        frame, cap_reference = apply_float_cap_reference(
            frame, Path(str(args.float_cap_ref_db)).expanduser()
        )
    days = [d for d in sorted(frame["date"].unique()) if start <= d <= end]
    funnels = []
    for day in days:
        todays = frame.loc[frame["date"] == day]
        # 分位阈值只在**测过**的市值上推：占位常量进池时（2026-06 全月只有这一个取值）
        # quantile 就等于它本身，这条门那天对全市场一个都不淘汰（§3g）。
        cap_measured = todays["float_market_cap"].loc[
            ~unproven_float_market_cap_mask(todays["float_market_cap"])
        ]
        funnels.append(day_funnel(
            frame, day,
            min_turnover=float(todays["turnover"].quantile(args.turnover_quantile)),
            min_float_cap=(
                float(cap_measured.quantile(args.float_cap_quantile))
                if len(cap_measured) else float("nan")
            ),
        ))
    report = summarize(funnels, pool)
    report.update({
        "ok": True,
        "warehouse": str(warehouse),
        "symbols_file": str(pool_file),
        "symbols_file_sha256": hashlib.sha256(pool_file.read_bytes()).hexdigest(),
        "window": [start.isoformat(), end.isoformat()],
        "threshold_basis": "marketwide_cross_sectional_quantile_per_day",
        "turnover_quantile": args.turnover_quantile,
        "float_cap_quantile": args.float_cap_quantile,
        "min_history_bars": MIN_HISTORY_BARS,
        "max_bar_gap_calendar_days": MAX_BAR_GAP_CALENDAR_DAYS,
        "float_cap_reference": cap_reference,
    })
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warehouse", required=True)
    parser.add_argument("--symbols-file", required=True,
                        help="研究侧清单，只用来算清单外的合格候选，不是筛选输入")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--turnover-quantile", type=float, default=0.30)
    parser.add_argument("--float-cap-quantile", type=float, default=0.10)
    parser.add_argument(
        "--float-cap-ref-db",
        default="",
        help="独立补采的市值真值库（float_market_cap_ref）；给了就用它替换那一列，"
             "不给就按仓库原值跑（占位常数会被记成 unproven_float_market_cap）",
    )
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)
    try:
        report = run(args)
    except SystemExit as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return _RC_ERROR
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        target = Path(str(args.report)).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return _RC_OK


if __name__ == "__main__":
    raise SystemExit(main())
