"""把**排序层**单独量出来：不看概率模型，只看同一合格池里按某类信息排前 3 会不会更好。

改进计划 §2 要区分「最终推荐变差来自候选池、预测、排序还是交易规则」，§4 又要六个指标一起看。
`validate_tail_selection_quality.py` 那条路必须先过校准段方向门（raw AUC>0.5）才产出概率，
而 §4.2 已实测四类信息方向不稳定，于是四个指标一直空着。这条脚本绕开概率模型但**不动那道门**：
直接按单类信息排序取每日前 3，用同一批已成熟成交样本算六项读数与交易日分块 bootstrap。

三条口径限制，不然后面的读数会被误用：

1. 只在**已成交**的样本上排序。按 §3.3，未成交不进盈亏样本，成交率单独在下面按池子报。
2. 默认方向是「数值越大越好」（趋势位置/相对强弱的自然先验）。同时也报反向臂，
   因为两臂里挑好看的那条等于做了 2 次选择——报告必须把这条 multiplicity 代价写出来，
   不能只贴赢的那一臂。
3. 评估段是**最后 N 个决策日**，与 §4 的滚动折不同构，所以这里的数字**只是排序层诊断与上限**，
   不构成 §4 的选股质量验收，更不产出任何命中率声明。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.feature.trend_candidate_contract import (  # noqa: E402
    FEATURE_GROUPS,
)

DATE_FIELD = "decision_date"
LABEL_FIELD = "label"
NET_FIELD = "net_return"
REFERENCE_NOTIONAL = 10_000.0
DAILY_CAP = 3
BOOTSTRAP_DRAWS = 2_000
SEED = 20261008


def load_fills(path: Path) -> list[dict[str, Any]]:
    """只吃带真实成熟标签且已成交的样本；缺净收益就停，不补 0。"""
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get(LABEL_FIELD) not in (0, 1, 0.0, 1.0):
                continue
            if not row.get("filled"):
                continue
            if not isinstance(row.get(NET_FIELD), (int, float)):
                raise SystemExit(
                    f"样本缺 {NET_FIELD}（{row.get('symbol')} {row.get(DATE_FIELD)}）："
                    "净收益核算不出来就不能进盈亏统计，补 0 等于把亏损读成持平"
                )
            rows.append(row)
    rows.sort(key=lambda r: (str(r[DATE_FIELD]), str(r.get("symbol", ""))))
    if not rows:
        raise SystemExit("没有已成交且有成熟标签的样本")
    return rows


def _day_groups(rows: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(str(row[DATE_FIELD]), []).append(dict(row))
    return out


def select_top3(
    rows: Sequence[dict[str, Any]], field: str, *, descending: bool, days: int
) -> list[dict[str, Any]]:
    """每日取该类信息的前 3 只，同分按股票代码升序（与 §3.4 的 tiebreak 一致）。"""
    by_day = _day_groups(rows)
    test_days = sorted(by_day)[-days:]
    picked: list[dict[str, Any]] = []
    for day in test_days:
        pool = [r for r in by_day[day] if isinstance(r.get(field), (int, float))]
        order = sorted(
            pool,
            key=lambda r: (-float(r[field]) if descending else float(r[field]), str(r["symbol"])),
        )
        picked.extend(order[:DAILY_CAP])
    return picked


def metrics(
    picked: Sequence[dict[str, Any]], *, candidate_days: int, all_fills: int
) -> dict[str, Any]:
    if not picked:
        return {"picks": 0}
    hits = sum(1 for r in picked if int(r[LABEL_FIELD]) == 1)
    nets = sorted(float(r[NET_FIELD]) for r in picked)
    tail_index = max(0, min(len(nets) - 1, int(0.05 * len(nets))))
    losing = [v for v in nets if v < 0.0]
    worst = min(nets)
    return {
        "picks": len(picked),
        "hit_rate": round(hits / len(picked), 4),
        "mean_net_return": round(sum(nets) / len(nets), 6),
        "p05_net_return": nets[tail_index],
        "worst_net_return": worst,
        "share_le_minus_5pct": round(sum(1 for v in nets if v <= -0.05) / len(nets), 4),
        "mean_loss_when_losing": round(sum(losing) / len(losing), 6) if losing else None,
        "coverage_days": len({str(r[DATE_FIELD]) for r in picked}),
        "coverage_share_of_days": round(
            len({str(r[DATE_FIELD]) for r in picked}) / max(1, candidate_days), 4
        ),
        "capital_deployed_yuan": round(len(picked) * REFERENCE_NOTIONAL, 2),
        "capital_utilisation": round(len(picked) / (candidate_days * DAILY_CAP), 4),
        "fills_per_day": round(all_fills / max(1, candidate_days), 2),
    }


def day_hit_series(picked: Sequence[dict[str, Any]]) -> dict[str, tuple[int, int]]:
    """按决策日聚合 (命中数, 推荐数)，分块 bootstrap 以交易日为重采样单位。"""
    per_day: dict[str, list[int]] = {}
    for row in picked:
        per_day.setdefault(str(row[DATE_FIELD]), []).append(int(row[LABEL_FIELD]))
    return {day: (sum(v), len(v)) for day, v in per_day.items()}


def block_bootstrap_delta(
    treatment: dict[str, tuple[int, int]], baseline: dict[str, tuple[int, int]]
) -> dict[str, Any]:
    days = sorted(set(treatment) & set(baseline))
    if len(days) < 4:
        return {"note": "交易日不足 4 个，不做 bootstrap"}
    rng = random.Random(SEED)
    deltas: list[float] = []
    for _ in range(BOOTSTRAP_DRAWS):
        hits_t = fills_t = hits_b = fills_b = 0
        for _ in range(len(days)):
            day = days[rng.randrange(len(days))]
            ht, ft = treatment[day]
            hb, fb = baseline[day]
            hits_t += ht
            fills_t += ft
            hits_b += hb
            fills_b += fb
        if fills_t and fills_b:
            deltas.append(hits_t / fills_t - hits_b / fills_b)
    if not deltas:
        return {"note": "bootstrap 没有有效重采样"}
    deltas.sort()
    lo = deltas[int(0.025 * len(deltas))]
    hi = deltas[int(0.975 * len(deltas))]
    return {
        "draws": len(deltas),
        "days": len(days),
        "mean_delta_pp": round(100 * sum(deltas) / len(deltas), 3),
        "ci_low_pp": round(100 * lo, 3),
        "ci_high_pp": round(100 * hi, 3),
        "ci_low_above_zero": bool(lo > 0.0),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--test-days", type=int, default=40)
    parser.add_argument("--features", default="")
    args = parser.parse_args(argv)

    rows = load_fills(Path(args.samples))
    by_day = _day_groups(rows)
    test_days = sorted(by_day)[-args.test_days:]
    window = [r for d in test_days for r in by_day[d]]
    names = [f.strip() for f in args.features.split(",") if f.strip()] or list(PROBE_NAMES)
    group_of = {
        feature: group
        for group, features in FEATURE_GROUPS.items()
        for feature in features
    }

    pool = metrics(window, candidate_days=len(test_days), all_fills=len(window))
    pool_day_hits = day_hit_series(window)
    report: dict[str, Any] = {
        "scope": f"已成交且有真实成熟标签的样本；评估段=最后 {len(test_days)} 个决策日",
        "note": "排序层诊断与上限，不是 §4 选股质量验收；不产出命中率声明",
        "samples_total": len(rows),
        "decision_days_total": len(by_day),
        "test_days": [test_days[0], test_days[-1]],
        "pool_arm_within_test_window": pool,
        "pool_fill_rate_all_days": None,
        "arms": [],
    }
    for field in names:
        for direction in ("desc", "asc"):
            picked = select_top3(
                window, field, descending=(direction == "desc"), days=len(test_days)
            )
            if not picked:
                continue
            arm = {
                "field": field,
                "group": group_of.get(field, "unmapped"),
                "direction": direction,
                "metrics": metrics(
                    picked, candidate_days=len(test_days), all_fills=len(window)
                ),
                "delta_vs_pool": block_bootstrap_delta(day_hit_series(picked), pool_day_hits),
            }
            report["arms"].append(arm)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"评估段 {report['test_days']} 池内 {pool.get('picks')} 笔，命中 {pool.get('hit_rate')}")
    for arm in report["arms"]:
        m = arm["metrics"]
        d = arm["delta_vs_pool"]
        print(
            f"{arm['field']:<18} {arm['direction']:<5} group={arm['group']:<20} "
            f"hit={m.get('hit_rate')} mean_net={m.get('mean_net_return')} "
            f"p05={m.get('p05_net_return')} cov={m.get('coverage_share_of_days')} "
            f"delta={d.get('mean_delta_pp')} ci=[{d.get('ci_low_pp')}, {d.get('ci_high_pp')}] "
            f"ci_low>0={d.get('ci_low_above_zero')}"
        )
    print(f"报告已写入 {out_path}")
    return 0


#: 四组各挑代表，加上上一轮实测过的极值/过热列。
PROBE_NAMES = (
    "close_to_ma20",
    "range_position_60",
    "ma20_slope",
    "excess_ret_20",
    "relative_strength",
    "avg_turnover_20",
    "turnover",
    "volume_ratio_5",
    "atr14_pct",
    "realized_vol_20",
    "gap_up_pct",
)


if __name__ == "__main__":
    raise SystemExit(main())
