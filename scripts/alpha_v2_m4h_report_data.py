"""M4-H 报告数据提取：把落盘工件汇总成报告可直接引用的数字。

原则：**报告里的每个数字都由本脚本从工件算出**，不从 fold 日志或对话里抄写。
本脚本只读 `artifacts/alpha_v2/m4h/**`，不改任何既有工件。

用法::

    python scripts/alpha_v2_m4h_report_data.py --root artifacts/alpha_v2/m4h
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from stock_analyzer.alpha_v2.research import metrics as met  # noqa: E402
from stock_analyzer.alpha_v2.research.outcomes import round_trip_cost_rate  # noqa: E402
from stock_analyzer.alpha_v2.research.purged_walk_forward import (  # noqa: E402
    newey_west_mean_ci,
    non_overlapping_anchor,
)
from stock_analyzer.backtest.matcher import ExecutionMatcher  # noqa: E402
from stock_analyzer.config import BacktestMatcherConfig  # noqa: E402
from stock_analyzer.data.limit_rule import LimitRuleConfig  # noqa: E402

SCHEMA = "alpha_v2_m4h_report_data.v1"
PRIMARY_HORIZON = 5
CONFIRMATION_HORIZON = 3
DECAY_HORIZONS = (10, 15)
REFERENCE_NOTIONAL = 100_000.0


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def phase_a_prediction_paths(root: Path) -> list[Path]:
    """phase A 的逐票预测。

    刻意**不用** ``fold_*.json``：那会同时匹配 phase B 的 ``fold_001_b.json``，
    把 pooled 样本翻倍（实测 29 → 58 个文件），使报告里的 pooled 数字无法复现。
    """
    return sorted((root / "predictions").glob("fold_[0-9][0-9][0-9].json"))


def load_pooled_predictions(root: Path) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for path in phase_a_prediction_paths(root):
        payload = _load_json(path)
        records = payload.get("records") or []
        if records:
            frames.append(pd.DataFrame(records))
    if not frames:
        return pd.DataFrame()
    pooled = pd.concat(frames, ignore_index=True)
    pooled["decision_date"] = pd.to_datetime(pooled["decision_date"], errors="coerce")
    for column in ("rank_score", "net_return_5d", "excess_return_5d", "mae_5d", "mfe_5d"):
        if column in pooled.columns:
            pooled[column] = pd.to_numeric(pooled[column], errors="coerce")
    return pooled


def fold_summary(results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    ok = [item for item in results if item.get("status") == "ok"]
    rows: list[dict[str, Any]] = []
    for item in ok:
        fold = item.get("fold") or {}
        evaluation = item.get("evaluation") or {}
        horizon_key = f"{PRIMARY_HORIZON}d"
        ic = (evaluation.get("rank_ic") or {}).get(horizon_key) or {}
        topk = (evaluation.get("topk") or {}).get(horizon_key) or {}
        quantile = (evaluation.get("quantile") or {}).get(horizon_key) or {}
        top1 = topk.get("top1") or {}
        top5 = topk.get("top5") or {}
        rows.append(
            {
                "fold_id": int(fold.get("fold_id", 0)),
                "test_start": fold.get("test_start"),
                "test_end": fold.get("test_end"),
                "train_rows": item.get("train_rows"),
                "calibration_rows": item.get("calibration_rows"),
                "test_rows": item.get("test_rows"),
                "maturity_purged_rows": item.get("maturity_purged_rows"),
                "elapsed_seconds": item.get("elapsed_seconds"),
                "mean_ic_5d": ic.get("mean_ic"),
                "median_ic_5d": ic.get("median_ic"),
                "mature_dates_5d": ic.get("mature_dates"),
                "ci95_5d": ic.get("ci95"),
                "ci_crosses_zero_5d": ic.get("ci_crosses_zero"),
                "top_minus_bottom_5d": quantile.get("top_minus_bottom"),
                "top1_excess_5d": top1.get("excess_return_5d"),
                "top1_net_5d": top1.get("net_return_5d"),
                "top1_hit_5d": top1.get("excess_return_5d_hit_rate"),
                "top5_excess_5d": top5.get("excess_return_5d"),
                "top5_net_5d": top5.get("net_return_5d"),
                "top5_hit_5d": top5.get("excess_return_5d_hit_rate"),
                "fill_rate": (evaluation.get("fill") or {}).get("fill_rate"),
            }
        )
    ic_values = [r["mean_ic_5d"] for r in rows if isinstance(r["mean_ic_5d"], (int, float))]
    ci_excluding_zero = [
        r for r in rows
        if isinstance(r.get("ci95_5d"), list) and len(r["ci95_5d"]) == 2
        and isinstance(r["ci95_5d"][0], (int, float))
        and (r["ci95_5d"][0] > 0 or r["ci95_5d"][1] < 0)
    ]
    return {
        "folds_usable": len(rows),
        "folds_positive_ic": int(sum(1 for v in ic_values if v > 0)),
        "folds_negative_ic": int(sum(1 for v in ic_values if v < 0)),
        "positive_fold_ratio": (float(sum(1 for v in ic_values if v > 0) / len(ic_values))
                                if ic_values else None),
        "fold_ic_mean": float(np.mean(ic_values)) if ic_values else None,
        "fold_ic_median": float(np.median(ic_values)) if ic_values else None,
        "fold_ic_min": float(np.min(ic_values)) if ic_values else None,
        "fold_ic_max": float(np.max(ic_values)) if ic_values else None,
        "folds_ci_excluding_zero": len(ci_excluding_zero),
        "folds_ci_positive": int(sum(1 for r in ci_excluding_zero if r["ci95_5d"][0] > 0)),
        "total_mature_dates_5d": int(sum(r["mature_dates_5d"] or 0 for r in rows)),
        "total_test_rows": int(sum(r["test_rows"] or 0 for r in rows)),
        "rows": rows,
    }


def pooled_metrics(pooled: pd.DataFrame) -> dict[str, Any]:
    if pooled.empty:
        return {"status": "empty"}
    out: dict[str, Any] = {"status": "ok", "rows": int(len(pooled)),
                           "decision_dates": int(pooled["decision_date"].nunique())}
    for horizon in (3, 5, 10, 15):
        metric = met.metric_column("excess_return", horizon)
        if metric not in pooled.columns or pooled[metric].isna().all():
            continue
        # 同时报**绝对净收益**：超额里费用被基准抵消（见 cost_reconciliation），
        # 只有绝对净收益能回答"这套组合扣掉成本后到底赚不赚钱"。
        net_metric = met.metric_column("net_return", horizon)
        metric_columns = [
            column for column in (metric, net_metric) if column in pooled.columns
        ]
        daily = met.daily_rank_ic(pooled, score_column="rank_score", metric_column_=metric)
        block = met.ic_summary(daily)
        block["non_overlapping"] = non_overlapping_anchor(daily, horizon=horizon)
        block["newey_west"] = newey_west_mean_ci(daily, lag=max(1, horizon - 1))
        out[f"rank_ic_{horizon}d"] = block
        out[f"topk_{horizon}d"] = met.topk_metrics(
            pooled, score_column="rank_score", metric_columns=metric_columns, ks=(1, 3, 5)
        )
        out[f"quantile_{horizon}d"] = met.quantile_monotonicity(
            met.quantile_returns(pooled, score_column="rank_score", metric_column_=metric)
        )
        # ``downside_metrics`` 在传入 score_column 时固定取 Top5（metrics.py 内 k=5）；
        # 报告需要 Top1 的下行口径，因此显式先切 Top1、再以 score_column=None 调用
        # 同一个已验收模块（不复制它的取数逻辑）。
        out[f"downside_{horizon}d"] = met.downside_metrics(
            pooled, horizon=horizon, score_column="rank_score"
        )
        out[f"downside_top1_{horizon}d"] = met.downside_metrics(
            met.select_top_k(pooled, score_column="rank_score", k=1),
            horizon=horizon,
            score_column=None,
        )
    executable = pooled.get("executable")
    if executable is not None:
        mask = executable.astype(bool)
        out["fill"] = {
            "rows": int(len(pooled)),
            "filled": int(mask.sum()),
            "fill_rate": float(mask.mean()),
        }
    return out


def cost_reconciliation(pooled: pd.DataFrame) -> dict[str, Any]:
    """成本口径核对：全部数字由代码实测，不从报告转抄。

    三件事：

    1. **权威费率**：直接调用 ``round_trip_cost_rate``（研究链唯一成本入口，
       内部走 ``ExecutionMatcher.estimate_cost``）算出佣金 / 过户费 / 卖出印花税。
    2. **滑点实测**：从落盘的 ``entry_price_raw`` / ``entry_price_net`` 反算真实
       生效的入场滑点，而不是引用配置里的名义值。
    3. **费用抵消的数值证据**：基准层是同一 ``net_return`` 列的等权均值，而费率
       是全样本常数，因此 ``excess = net − 常数``——费用在超额里精确抵消。
       这里用"同一 decision_date 内 ``net − excess`` 必须是常数"直接检验，
       并再做一次平移不变性实测（把 net 整体平移 δ，超额不变）。
    """
    matcher = ExecutionMatcher(BacktestMatcherConfig(), limit_rule=LimitRuleConfig())
    rates = round_trip_cost_rate(
        matcher=matcher, trade_date=None, reference_notional=REFERENCE_NOTIONAL
    )
    out: dict[str, Any] = {
        "cost_model": "round_trip_rate_from_matcher_config",
        "reference_notional": REFERENCE_NOTIONAL,
        **{key: float(value) for key, value in rates.items()},
        "stamp_tax_resolution": "config_default_flat（研究链统一口径：限额规则未装载日期化费率表）",
    }

    net = pooled.get("net_return_5d")
    excess = pooled.get("excess_return_5d")
    if net is None or excess is None:
        out["status"] = "no_pooled_columns"
        return out

    slippage_net = pooled.get("entry_price_net")
    slippage_raw = pooled.get("entry_price_raw")
    slippage_columns_present = slippage_net is not None and slippage_raw is not None
    if slippage_columns_present:
        ratio = pd.to_numeric(slippage_net, errors="coerce") / pd.to_numeric(
            slippage_raw, errors="coerce"
        ) - 1.0
        ratio = ratio.replace([np.inf, -np.inf], np.nan).dropna()
        if ratio.empty:
            slippage_columns_present = False
        else:
            out["entry_slippage_measured_median"] = float(ratio.median())
            out["entry_slippage_measured_rows"] = int(ratio.shape[0])
    if not slippage_columns_present:
        # 逐票预测的投影里没有 entry_price_net（或整列为空）→ 无法从落盘工件反算
        # 真实生效的滑点。**显式记成缺口**而不是静默省略，报告中据实标注。
        out["entry_slippage_measured_median"] = None
        out["entry_slippage_measured_rows"] = 0
        out["entry_slippage_note"] = (
            "landed predictions omit entry_price_net -> realized slippage cannot be "
            "re-derived from artifacts; value is the protocol-declared 0.0015/side"
        )

    # 检验 1：同一 decision_date 内 net − excess 必须是常数（= 当日基准均值）
    implied = (net - excess).dropna()
    if not implied.empty:
        by_date = implied.groupby(pooled.loc[implied.index, "decision_date"]).agg(
            ["min", "max"]
        )
        spread = (by_date["max"] - by_date["min"]).abs()
        out["benchmark_constant_within_date"] = {
            "dates": int(spread.shape[0]),
            "max_abs_spread": float(spread.max()),
            "is_constant": bool(spread.max() <= 1e-9),
        }
        # 基准自身的绝对净收益（直接测量，供报告与 Top-K 绝对净收益对照）
        out["benchmark_net_return_5d_mean"] = float(implied.mean())

    # 检验 2：费率平移不变性。基准 = 当日池内等权均值，故 net 整体平移 δ 时
    # excess = net − benchmark 逐位不变（此处用落盘数据直接重算当日基准，不引用
    # 报告里已有的超额数字）。
    def _top1_excess(frame: pd.DataFrame, net_column: str) -> float:
        working = frame.assign(
            __net=pd.to_numeric(frame[net_column], errors="coerce")
        ).dropna(subset=["__net"])
        working["__bench"] = working.groupby("decision_date")["__net"].transform("mean")
        working["__excess"] = working["__net"] - working["__bench"]
        selected = met.select_top_k(working, score_column="rank_score", k=1)
        return float(selected["__excess"].mean())

    delta = 0.0005
    shifted = pooled.assign(
        __net_shifted=pd.to_numeric(pooled["net_return_5d"], errors="coerce") - delta
    )
    before = _top1_excess(pooled, "net_return_5d")
    after = _top1_excess(shifted, "__net_shifted")
    out["fee_shift_invariance"] = {
        "delta_applied": delta,
        "top1_excess_recomputed": before,
        "top1_excess_after_shift": after,
        "abs_diff": abs(before - after),
        "identical": bool(abs(before - after) <= 1e-12),
    }
    out["status"] = "ok"
    return out


def benchmark_comparison(results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """三层基准对比。

    层专属超额列（``excess_return_5d__<layer>``）只落在 fold 级指标里，未进逐票预测，
    因此这里做 **fold 级汇总**（每层的 TopK 均值与正负 fold 分布），并在报告中写明口径。
    """
    layers: dict[str, list[dict[str, Any]]] = {}
    primary_layers: dict[str, list[float]] = {}
    for item in results:
        if item.get("status") != "ok":
            continue
        for layer, block in (item.get("benchmark_layer_metrics") or {}).items():
            if layer == "horizon" or not isinstance(block, dict):
                continue
            layers.setdefault(layer, []).append(block)
            top1 = (block.get("top1") or {}).get(f"excess_return_5d__{layer}")
            if isinstance(top1, (int, float)):
                primary_layers.setdefault(layer, []).append(float(top1))
        main_topk = ((item.get("evaluation") or {}).get("topk") or {}).get("5d") or {}
        main_top1 = (main_topk.get("top1") or {}).get("excess_return_5d")
        if isinstance(main_top1, (int, float)):
            primary_layers.setdefault("eligible_ew(label_column)", []).append(float(main_top1))

    out: dict[str, Any] = {"layers": {}, "aggregation": "fold_level_mean"}
    for layer, values in primary_layers.items():
        if not values:
            continue
        array = np.asarray(values, dtype=float)
        out["layers"][layer] = {
            "folds": int(array.size),
            "top1_excess_5d_mean": float(array.mean()),
            "top1_excess_5d_median": float(np.median(array)),
            "positive_folds": int((array > 0).sum()),
            "negative_folds": int((array < 0).sum()),
        }
    topk_by_layer: dict[str, Any] = {}
    for layer, blocks in layers.items():
        entry: dict[str, Any] = {}
        for key in ("top1", "top3", "top5"):
            values = [
                float(block[key][f"excess_return_5d__{layer}"])
                for block in blocks
                if isinstance(block.get(key, {}).get(f"excess_return_5d__{layer}"), (int, float))
            ]
            entry[key] = {
                "folds": len(values),
                "mean_excess_5d": float(np.mean(values)) if values else None,
                "positive_folds": int(sum(1 for v in values if v > 0)) if values else 0,
            }
        topk_by_layer[layer] = entry
    out["topk_by_layer"] = topk_by_layer
    return out


def fold_horizon_summary(results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """按 horizon 的 fold 级汇总（3D/5D/10D/15D）。

    逐票预测只落了 5D 列，因此 3D/10D/15D 无法做 pooled 重算；
    这里用**每个 fold 自己的 IC / TopK** 跨 29 折汇总，口径在报告中写明。
    """
    out: dict[str, Any] = {}
    for horizon in (3, 5, 10, 15):
        key = f"{horizon}d"
        fold_ic: list[float] = []
        top1_excess: list[float] = []
        top5_excess: list[float] = []
        spread: list[float] = []
        for item in results:
            if item.get("status") != "ok":
                continue
            evaluation = item.get("evaluation") or {}
            ic = (evaluation.get("rank_ic") or {}).get(key) or {}
            value = ic.get("mean_ic")
            if isinstance(value, (int, float)):
                fold_ic.append(float(value))
            topk = (evaluation.get("topk") or {}).get(key) or {}
            for target, bucket in (("top1", top1_excess), ("top5", top5_excess)):
                entry = (topk.get(target) or {}).get(f"excess_return_{horizon}d")
                if isinstance(entry, (int, float)):
                    bucket.append(float(entry))
            quantile = (evaluation.get("quantile") or {}).get(key) or {}
            spread_value = quantile.get("top_minus_bottom")
            if isinstance(spread_value, (int, float)):
                spread.append(float(spread_value))
        if not fold_ic:
            continue
        array = np.asarray(fold_ic, dtype=float)
        out[key] = {
            "folds": int(array.size),
            "fold_ic_mean": float(array.mean()),
            "fold_ic_median": float(np.median(array)),
            "fold_ic_min": float(array.min()),
            "fold_ic_max": float(array.max()),
            "positive_folds": int((array > 0).sum()),
            "positive_fold_ratio": float((array > 0).mean()),
            "top1_excess_mean": float(np.mean(top1_excess)) if top1_excess else None,
            "top1_positive_folds": int(sum(1 for v in top1_excess if v > 0)),
            "top5_excess_mean": float(np.mean(top5_excess)) if top5_excess else None,
            "top5_positive_folds": int(sum(1 for v in top5_excess if v > 0)),
            "top_minus_bottom_mean": float(np.mean(spread)) if spread else None,
        }
    return out


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="M4-H report data extraction")
    parser.add_argument("--root", default=str(REPO_ROOT / "artifacts/alpha_v2/m4h"))
    parser.add_argument("--out", default="")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root)
    fold_results = (_load_json(root / "folds" / "fold_results.json") or {}).get("results", [])
    summary = _load_json(root / "metrics" / "metrics_summary.json")
    leakage = _load_json(root / "audit" / "leakage_audit.json")
    yearly = _load_json(root / "metrics" / "strata_yearly.json")
    regime = _load_json(root / "metrics" / "strata_regime.json")
    pooled = load_pooled_predictions(root)

    payload = {
        "schema": SCHEMA,
        "protocol_id": summary.get("protocol_id"),
        "protocol_hash": summary.get("protocol_hash"),
        "h_gates": summary.get("h_gates"),
        "live_gates": summary.get("live_gates"),
        "historical_locked_oos_days": summary.get("historical_locked_oos_mature_decision_dates"),
        "fold_summary": fold_summary(fold_results),
        "pooled": pooled_metrics(pooled),
        "cost_reconciliation": cost_reconciliation(pooled),
        "benchmark_layers": benchmark_comparison(fold_results),
        "fold_horizon_summary": fold_horizon_summary(fold_results),
        "leakage_audit": leakage,
        "yearly": yearly.get("years", yearly),
        "regime": {k: v for k, v in (regime.get("regimes") or {}).items() if k != "daily_series"},
        "yearly_available": bool(yearly),
    }
    out = Path(args.out) if args.out else (root / "reports" / "report_data.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    fs = payload["fold_summary"]
    print(
        f"[m4h-report-data] folds={fs['folds_usable']} "
        f"fold_ic_mean={fs['fold_ic_mean']} positive={fs['folds_positive_ic']}/"
        f"{fs['folds_usable']} mature_dates={fs['total_mature_dates_5d']} -> {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
