"""M4-H Historical Evidence Eligibility Map。

对历史数据区间做**证据等级**分级：每个区间给出
``DEVELOPMENT_CONTAMINATED / LOCKED_OOS_ELIGIBLE / HISTORICAL_UNTOUCHED_HOLDOUT /
DATA_INCOMPLETE / UNAVAILABLE`` 之一，并附**可复现的证据**。

核心纪律（用户 §7）：
- **不得默认任何年份是 untouched**；无法证明"此区间未参与模型/特征/阈值/策略选择"
  就不得标 ``HISTORICAL_UNTOUCHED_HOLDOUT``。
- 已知污染窗口 ``2025-06-02 → 2026-03-31`` 必须整段标
  ``DEVELOPMENT_CONTAMINATED``，重新跑一次不能改称 untouched。
- 本阶段真正使用的是 ``LOCKED_OOS_ELIGIBLE``：即使不是 untouched，也可以
  "协议先冻结、结果后运行、运行后不得回改协议"形成 Historical Locked OOS 证据，
  但报告必须保留 contamination level。

用法::

    python scripts/alpha_v2_m4h_evidence_map.py \
        --inventory artifacts/alpha_v2/m4h/historical_data_inventory.json \
        --out artifacts/alpha_v2/m4h/historical_evidence_inventory.json
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb

MAP_SCHEMA = "alpha_v2_m4h_historical_evidence_inventory.v1"

DEVELOPMENT_CONTAMINATED_RANGE = ("2025-06-02", "2026-03-31")

# 数据链分段（依据盘点实测，不是假设）：
# - 2025-09 起 volume 单位开始从"股"切换到"手"，2025-10 起约 71% 行为"手"，
#   同一时点存在两种单位 → 该边界之后的量能类特征不可比。
# - is_st 比例在同一时期异常塌陷（2026 年仅 0.607%），说明状态字段亦换源。
DATA_REGIME_STABLE_END = "2025-08-29"
DATA_REGIME_MIXED_START = "2025-09-01"

# 已知被项目反复用于开发/选择的证据（文档 + 提交记录），用于标注 contamination。
CONTAMINATION_EVIDENCE: tuple[dict[str, str], ...] = (
    {
        "window": "2025-06-02..2026-03-31",
        "source": "docs/alpha_v2/M2_Implementation_Report.md（S19/S11 研究窗口）、"
        "M3 冻结基线 33d0f7f9",
        "note": "M1/M2/M3 的模型、特征、阈值、协议均在该窗口上开发与选择",
    },
    {
        "window": "2024-03-04..2026-03-31",
        "source": "index_daily 覆盖区间（本地库实测）",
        "note": "基准指数数据仅此区间存在；该区间的 market_relative 特征族曾进入特征审计",
    },
    {
        "window": "2016-01-04..2026-03-31",
        "source": "Week5 历史回测与 learning 链（docs/week5_*）",
        "note": "项目历史回测链路覆盖多年，无法证明任一历史年份未被查看过",
    },
)


def _trading_dates(market_db: Path, *, start: str, end: str) -> list[str]:
    con = duckdb.connect(str(market_db), read_only=True)
    try:
        rows = con.execute(
            "SELECT DISTINCT date FROM daily_bars WHERE date >= ?::DATE AND date <= ?::DATE "
            "ORDER BY date",
            [start, end],
        ).fetchall()
    finally:
        con.close()
    return [str(row[0]) for row in rows]


def _last_trading_day_on_or_before(market_db: Path, day: str) -> str:
    dates = _trading_dates(market_db, start="2016-01-01", end=day)
    return dates[-1] if dates else day


def _first_trading_day_after(market_db: Path, day: str, *, until: str = "2026-12-31") -> str:
    dates = _trading_dates(market_db, start=day, end=until)
    for item in dates:
        if item > day:
            return item
    return day


def build_evidence_map(inventory: dict[str, Any], market_db: Path) -> dict[str, Any]:
    span = inventory["daily_bars"]["span"]
    earliest = str(span["earliest_trade_date"])
    latest = str(span["latest_trade_date"])

    # 残片尾部：盘点显示最后两个"交易日"只有几十行（写入口径不同的碎片）。
    coverage = {int(row["year"]): row for row in inventory["daily_bars"]["coverage_by_year"]}
    contaminated_start, contaminated_end = DEVELOPMENT_CONTAMINATED_RANGE
    locked_end = _last_trading_day_on_or_before(market_db, _previous_day(contaminated_start))

    intervals: list[dict[str, Any]] = [
        {
            "interval_id": "H1",
            "start": earliest,
            "end": locked_end,
            "classification": "LOCKED_OOS_ELIGIBLE",
            "contamination_level": "UNVERIFIED_NOT_UNTOUCHED",
            "evidence": [
                "起始于本地库最早交易日",
                "终止于 DEVELOPMENT_CONTAMINATED 窗口前最后一个交易日",
                "位于 volume 单位断点（2025-09 起）之前，量能口径内部一致",
                "项目历史回测/学习链路覆盖多年 —— 无法证明该区间未被查看，"
                "故**不得**标 HISTORICAL_UNTOUCHED_HOLDOUT",
            ],
            "usable_for": "Historical Locked OOS（协议冻结后运行）",
        },
        {
            "interval_id": "H2",
            "start": contaminated_start,
            "end": DATA_REGIME_STABLE_END,
            "classification": "DEVELOPMENT_CONTAMINATED",
            "contamination_level": "KNOWN_CONTAMINATED",
            "evidence": [
                "用户明确指定的开发污染窗口起点 2025-06-02",
                "M2 研究窗口 2025-09-01..2026-03-31 涵盖其大部分",
            ],
            "usable_for": "回归验证 / pipeline sanity check / 与旧结果对账",
        },
        {
            "interval_id": "H3",
            "start": DATA_REGIME_MIXED_START,
            "end": contaminated_end,
            "classification": "DEVELOPMENT_CONTAMINATED",
            "contamination_level": "KNOWN_CONTAMINATED",
            "evidence": [
                "污染窗口 2025-06-02..2026-03-31 的剩余部分",
                "**同时**处于数据链切换期：volume 单位混合"
                "（实测 2025-10 起约 71% 行为手）、"
                "is_st 比例异常塌陷（2026 年 0.607%）",
            ],
            "usable_for": "对照观察（量能类特征不可比，不作为主证据）",
        },
        {
            "interval_id": "H4",
            "start": _next_day(contaminated_end),
            "end": latest,
            "classification": "DATA_INCOMPLETE",
            "contamination_level": "KNOWN_CONTAMINATED",
            "evidence": [
                f"尾部残片：{latest} 当日仅 {_tail_fragment_rows(market_db, inventory)} 行",
                "写入口径与主链不同（单位亦不同）",
            ],
            "usable_for": "不使用",
        },
    ]

    # 显式声明：本阶段**不存在**可证明的 untouched holdout。
    untouched = {
        "classification": "HISTORICAL_UNTOUCHED_HOLDOUT",
        "found": False,
        "reason": (
            "没有任何区间具备'此前未参与模型/特征/阈值/策略选择'的明确证据；"
            "项目历史回测与学习链路覆盖多年。按用户 §7 规则，无法证明不得标 untouched。"
        ),
        "candidates_evaluated": [
            {"interval": "2016-01-04..2025-05-30", "verdict": "REJECTED",
             "reason": "无法证明未参与；且幸存者偏差不可修复"},
            {"interval": "2025-06-02..2026-03-31", "verdict": "REJECTED",
             "reason": "已知开发污染"},
        ],
    }

    data_defects = {
        "survivorship_bias": {
            "severity": "UNMITIGABLE",
            "evidence": {
                "symbols_with_last_bar_before_2025_01_01": inventory["survivorship"][
                    "symbols_with_last_bar_before_2025_01_01"
                ],
                "symbols_with_last_bar_before_2026_01_01": inventory["survivorship"][
                    "symbols_with_last_bar_before_2026_01_01"
                ],
                "known_delisted_code_probe": inventory["survivorship"]["delisted_code_probe"],
            },
            "impact": (
                "2016-2025 历史只含活到 2026 的标的；已退市标的完全缺席，"
                "任何多年横截面评估都带幸存者偏差。本阶段**无法修复**，只能如实标注。"
            ),
        },
        "volume_unit_regime_break": {
            "severity": "MITIGATED_BY_WINDOW_CHOICE",
            "ratio_by_year": inventory["unit_regime"]["ratio_by_year"],
            "impact": (
                "2025-09 起 volume 由股切手、float_market_cap 同步 /100；"
                "跨该边界的量能/换手类特征不可比。M4-H 主评估窗止于 2025-05-30 以规避。"
            ),
        },
        "financial_data_not_pit": {
            "severity": "HIGH",
            "evidence": inventory["financial_source_mix"],
            "impact": (
                "financial_report_date 99.93% 为同一快照日 2026-03-02，"
                "financial_source 96% 为 tdxgp_heuristic+default —— 财务字段不是逐日 PIT，"
                "历史行是**事后回填**。凡消费这些列的特征在历史区间都不满足 PIT 可得性。"
            ),
        },
        "benchmark_coverage_short": {
            "severity": "MEDIUM",
            "evidence": inventory["benchmark"],
            "impact": (
                "index_daily 仅 000300.SH 且自 2024-03-04 起；"
                "market_relative 特征族（11 个，属 120 schema）在更早历史恒为常数，"
                "构成按 fold 的特征覆盖漂移。"
            ),
        },
        "limit_price_absent": {
            "severity": "MEDIUM",
            "evidence": {
                "up_limit_non_null_2025": _non_null(coverage, 2025, "up_limit"),
                "up_limit_non_null_2026": _non_null(coverage, 2026, "up_limit"),
                "total_rows": span["daily_bars"],
            },
            "impact": (
                "涨跌停价仅 154 行有值；执行契约的涨停不可买判定需由 "
                "prev_close + 板块规则推导（panel.py 已如此实现），"
                "推导口径与真实涨跌停存在偏差风险。"
            ),
        },
        "suspension_status_all_false": {
            "severity": "MEDIUM",
            "evidence": "suspended 列全量非空但恒为 False；security_status 表 0 行",
            "impact": (
                "停牌无法与数据缺失区分；停牌日直接缺 bar，"
                "由 no_fill 语义承接（不推迟成交）。"
            ),
        },
    }

    return {
        "schema": MAP_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "source_inventory": inventory["source"],
        "data_span": {"earliest": earliest, "latest": latest, "trading_days": span["trading_days"],
                      "symbols": span["symbols"], "daily_bars": span["daily_bars"]},
        "classification_legend": {
            "DEVELOPMENT_CONTAMINATED": "明确参与过训练/调参/看结果/选择策略/特征选择",
            "LOCKED_OOS_ELIGIBLE": (
                "非 untouched，但可在协议先冻结后运行的前提下形成 Locked OOS 证据"
            ),
            "HISTORICAL_UNTOUCHED_HOLDOUT": "有明确证据证明此前未参与任何选择",
            "DATA_INCOMPLETE": "覆盖不足或为残片",
            "UNAVAILABLE": "本地无数据",
        },
        "intervals": intervals,
        "untouched_holdout": untouched,
        "contamination_evidence": list(CONTAMINATION_EVIDENCE),
        "data_defects": data_defects,
        "m4h_primary_evaluation_window": {
            "start": earliest,
            "end": locked_end,
            "classification": "LOCKED_OOS_ELIGIBLE",
            "trading_days": len(_trading_dates(market_db, start=earliest, end=locked_end)),
        },
    }


def _previous_day(day: str) -> str:
    from datetime import timedelta

    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


def _next_day(day: str) -> str:
    from datetime import timedelta

    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def _non_null(coverage: dict[int, dict[str, Any]], year: int, column: str) -> Any:
    return coverage.get(year, {}).get("non_null", {}).get(column)


def _tail_fragment_rows(market_db: Path, inventory: dict[str, Any]) -> int:
    """尾部残片行数：取最新交易日（而非整个年份）当日的 bar 行数。"""
    latest = str(inventory["daily_bars"]["span"]["latest_trade_date"])
    con = duckdb.connect(str(market_db), read_only=True)
    try:
        row = con.execute(
            "SELECT count(*) FROM daily_bars WHERE date = ?::DATE", [latest]
        ).fetchone()
    finally:
        con.close()
    return int(row[0]) if row else 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="M4-H historical evidence eligibility map")
    parser.add_argument(
        "--inventory", default="artifacts/alpha_v2/m4h/historical_data_inventory.json"
    )
    parser.add_argument(
        "--out", default="artifacts/alpha_v2/m4h/historical_evidence_inventory.json"
    )
    parser.add_argument("--market-db", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    inventory = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    payload = build_evidence_map(inventory, Path(args.market_db))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    window = payload["m4h_primary_evaluation_window"]
    print(
        f"[m4h-evidence] primary window {window['start']}..{window['end']} "
        f"days={window['trading_days']} classification={window['classification']} -> {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
