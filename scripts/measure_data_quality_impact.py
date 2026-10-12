"""把散在各份报告里的数据质量读数合成一张表（改进计划 §2 第 4 个诊断问题）。

§2 问「数据缺失、旧缓存、特征滞后、模型降级分别影响多少股票」。此前每个数字都只活在
它自己那份报告里（标签侧的 `insufficient`、重放侧的 `float_cap_reference`、
稳定性的 `unusable`、就绪审计的 `column_concentration_*`），谁也没被并到一张表上，
所以这个问题一直只能分段回答，也没法核对分母。

这条脚本只做**汇总**，一条新的测量都不产生：每个数字都指回它原来的工件与键名，
并显式写出**分母**（没有分母的数字一律标 `denominator=unknown`，不猜）。
读不到某个工件就记进 `missing_artifacts` 并如实退出非零 —— 一张缺行的表比没有表更糟，
因为它看起来是完整的。

口径提醒（写进输出里，免得表被单独引用时误读）：
- symbol-day 与 symbol 不是同一个单位；跨行加总会重复计数。
- `replayed_recompute` 的分母是重放请求数，与生产夜扫的实际观察量无关。
- 「旧缓存」在这里只能是**可核实的替代痕迹**（占位常数、无真值可替换的行），
  不是"缓存过期"的直接测量。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

CAUSE_MISSING = "数据缺失"
CAUSE_STALE = "旧缓存或不可信常量"
CAUSE_LAG = "特征滞后或历史不可复现"
CAUSE_MODEL = "模型降级或身份失败"

# (cause, metric, artifact key path, denominator description)
ROWS = [
    (CAUSE_MISSING, "entry_day_limit_prices 阻塞的请求数",
     "labels.entry_day_limit_prices", "重放请求总数"),
    (CAUSE_MISSING, "entry_daily_bar 阻塞的请求数", "labels.entry_daily_bar", "重放请求总数"),
    (CAUSE_MISSING, "daily_bar_session_holes 阻塞的请求数",
     "labels.daily_bar_session_holes", "重放请求总数"),
    (CAUSE_MISSING, "entry_minute_bars 阻塞的请求数", "labels.entry_minute_bars", "重放请求总数"),
    (CAUSE_MISSING, "因数据不足而未生成标签（合计）", "labels.insufficient_total", "重放请求总数"),
    (CAUSE_MISSING, "无法买入·低于最小申报量", "labels.not_filled_below_lot_size", "已判定请求数"),
    (CAUSE_MISSING, "无法买入·涨停封死", "labels.not_filled_limit_up_locked", "已判定请求数"),
    (CAUSE_MISSING, "交易状态来源缺失的符号数",
     "labels.trade_status_source_gap_symbols", "池内符号数"),
    (CAUSE_STALE, "市值仍是占位常量且无真值可替换的行",
     "float_cap.rows_left_placeholder_without_reference", "含占位常量的行数"),
    (CAUSE_STALE, "占位常量占比（替换前）",
     "float_cap.placeholder_share_before", "全市场 symbol-day"),
    (CAUSE_STALE, "占位常量占比（用独立真值替换后）",
     "float_cap.placeholder_share_after", "全市场 symbol-day"),
    (CAUSE_STALE, "市值硬门整天无从判定的天数", "float_cap.days_with_non_evaluable_gate_inputs",
     "重放决策日总数"),
    (CAUSE_STALE, "库内自写值与独立真值差出 1% 以上的行",
     "float_cap.rows_measured_differing_beyond_1pct", "两侧都声称测过的行"),
    (CAUSE_LAG, "历史重放里整列不可复现、因此不进训练的特征列",
     "stability.unusable_columns", "契约登记的列数"),
    (CAUSE_MODEL, "滚动验证被拦下的折数（概率模型从未产出可排序分数）",
     "validation.blocked_fold_count", "请求折数"),
    (CAUSE_MODEL, "验证整体状态", "validation.status", "同一工件"),
    (CAUSE_MODEL, "影子验证已达标的完整交易日", "shadow.observed_trade_days", "门槛 60 天"),
    (CAUSE_MODEL, "影子验证已达标的成熟模拟成交", "shadow.matured_fills", "门槛 100 笔"),
]


def _load(path: Path, sink: dict[str, Any]) -> Any:
    if not path.is_file():
        sink.setdefault("missing_artifacts", []).append(str(path))
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def collect(artifacts: dict[str, Path]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ctx: dict[str, Any] = {"missing_artifacts": []}
    labels = _load(artifacts["labels"], ctx) or {}
    cap = _load(artifacts["replay_report"], ctx) or {}
    stability = _load(artifacts["stability"], ctx) or {}
    validation = _load(artifacts["validation"], ctx) or {}

    reasons = dict(labels.get("status_reason_counts") or {})
    missing = dict(labels.get("missing_reference_inputs") or {})
    capref = dict(cap.get("float_cap_reference") or {})
    unusable = stability.get("unusable") or []

    values = {
        "labels.entry_day_limit_prices": missing.get("entry_day_limit_prices"),
        "labels.entry_daily_bar": missing.get("entry_daily_bar"),
        "labels.daily_bar_session_holes": missing.get("daily_bar_session_holes"),
        "labels.entry_minute_bars": missing.get("entry_minute_bars"),
        "labels.insufficient_total": labels.get("insufficient"),
        "labels.not_filled_below_lot_size": reasons.get("not_filled:below_lot_size"),
        "labels.not_filled_limit_up_locked": reasons.get("not_filled:limit_up_locked"),
        "labels.trade_status_source_gap_symbols": labels.get("trade_status_source_gap_symbols"),
        "float_cap.rows_left_placeholder_without_reference": capref.get(
            "rows_left_placeholder_without_reference"
        ),
        "float_cap.placeholder_share_before": capref.get("placeholder_share_before"),
        "float_cap.placeholder_share_after": capref.get("placeholder_share_after"),
        "float_cap.days_with_non_evaluable_gate_inputs": len(
            cap.get("days_with_non_evaluable_gate_inputs") or []
        ) if cap.get("days_with_non_evaluable_gate_inputs") is not None else None,
        "float_cap.rows_measured_differing_beyond_1pct": capref.get(
            "rows_measured_differing_beyond_1pct"
        ),
        "stability.unusable_columns": sorted({str(u.get("column")) for u in unusable}),
        "validation.blocked_fold_count": len(validation.get("blocked_folds") or []),
        "validation.status": validation.get("status"),
        "shadow.observed_trade_days": (validation.get("shadow_readiness") or {}).get(
            "observed_trade_days"),
        "shadow.matured_fills": (validation.get("shadow_readiness") or {}).get(
            "matured_simulated_fills"),
    }
    scale = {
        "重放请求总数": labels.get("requests"),
        "已判定请求数": labels.get("label_records"),
        "池内符号数": None,
        "含占位常量的行数": capref.get("rows_total"),
        "全市场 symbol-day": capref.get("rows_total"),
        "重放决策日总数": cap.get("universe_fact_days"),
        "两侧都声称测过的行": capref.get("rows_both_claim_measured"),
        "契约登记的列数": len(stability.get("columns") or []),
        "请求折数": validation.get("folds_requested"),
        "同一工件": None,
        "门槛 60 天": 60,
        "门槛 100 笔": 100,
    }
    rows: list[dict[str, Any]] = []
    for cause, metric, key, denom in ROWS:
        rows.append({
            "cause": cause,
            "metric": metric,
            "value": values.get(key),
            "denominator": None if values.get(key) is None else {"label": denom,
                                                                "value": scale.get(denom)},
            "source_artifact": str(artifacts["labels"] if key.startswith("labels.")
                                   else artifacts["replay_report"] if key.startswith("float_cap.")
                                   else artifacts["stability"] if key.startswith("stability.")
                                   else artifacts["validation"])
            + f"::{key}",
        })
    return rows, ctx


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels-report", required=True)
    parser.add_argument("--replay-report", required=True)
    parser.add_argument("--stability-report", required=True)
    parser.add_argument("--validation-report", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    artifacts = {
        "labels": Path(args.labels_report),
        "replay_report": Path(args.replay_report),
        "stability": Path(args.stability_report),
        "validation": Path(args.validation_report),
    }
    rows, ctx = collect(artifacts)
    report = {
        "scope": "§2 第 4 个诊断问题的合并读数；只做汇总，不产生新测量",
        "cautions": [
            "symbol-day 与 symbol 单位不同，跨行加总会重复计数",
            "分母是重放请求，与生产夜扫的实际观察量无关",
            "「旧缓存」在这里只是可核实的替代痕迹（占位常量/无真值行），不是缓存过期的直接测量",
        ],
        "inputs": {k: str(v) for k, v in artifacts.items()},
        "rows": rows,
        **ctx,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    missing = ctx.get("missing_artifacts") or []
    for row in rows:
        d = row["denominator"]
        dstamp = f"{d['label']}={d['value']}" if d else "分母未知"
        print(f"{row['cause']:<12} {row['metric']:<44} {row['value']} ({dstamp})")
    print(f"表已写入 {out}")
    if missing:
        print("缺工件，表不完整：" + ", ".join(missing), file=sys.stderr)
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
