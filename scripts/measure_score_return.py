"""判据测量：生产分数与**已实现收益**的关系——回答"夜扫 0 信号是阈值问题还是效力问题"。

## 为什么要它

夜扫长期 0 个 final signal（门槛 70，全场最高 65.89，差 4.11 分）。两种解释的**处置完全相反**：

- **阈值问题**：分数与收益正相关、只是绝对刻度偏低 → 应当改成**按分位选 top-k**，
  而不是拍一个更低的绝对分；
- **效力问题**：分数与收益无关甚至负相关（8 月回测实测"分数-收益负相关"）→ **阈值无解**，
  放宽只会把没有 alpha 的票放进 final（即"为了让输出非空而放宽门禁"）。

判据很干净：**看生产分数与已实现收益的 IC 与分位价差**。

## 数据源

`learning_protocol.duckdb`：
- `signal_snapshots.snapshot_id / decision_time / score_breakdown_json / model_outputs_json`
- `outcome_records.snapshot_id / realized_return / label_mature_time / maturity_status`

两者按 `snapshot_id` 一对一。**只读打开**（`read_only=True`），且带重试——api 服务持有写锁时
（盘中）跨进程读会失败，重试几次拿不到就如实报"读不到"，不猜。

## 用法

    docker exec stock-analyzer-api python3 /app/scripts/measure_score_return.py
    docker exec stock-analyzer-api python3 /app/scripts/measure_score_return.py --json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1] if "__file__" in globals() else None
if _REPO_ROOT is not None and (_REPO_ROOT / "src").is_dir():
    _src = str(_REPO_ROOT / "src")
    if _src not in sys.path:
        sys.path.insert(0, _src)

DEFAULT_DB = "/app/artifacts/training/learning_protocol.duckdb"
# 阈值判定要看的几档：60/65 是夜扫池与等级门槛，70 是 final 门槛
THRESHOLD_LEVELS = (50.0, 60.0, 65.0, 70.0)
# 选股视角的 top-k：判断"按分位选前 k 名"是否优于"卡绝对分"
TOP_KS = (1, 3, 5, 10)


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    n = len(ordered)
    return ordered[min(n - 1, max(0, int(round(q * (n - 1)))))]


def evaluate_pairs(
    pairs: Sequence[tuple[str, float, float]], *, block_days: int = 5
) -> dict[str, Any]:
    """核心判据（纯函数，无 IO）：``pairs`` = [(决策日, 生产分数, 已实现收益)]。

    输出三组判据：
    1. **逐日 IC**（Spearman，复用 ``learning.scoring_eval.compute_rank_ic``）+ moving-block CI；
    2. **分位价差**：按分数分 5 档看各档收益均值与 top−bottom；
    3. **门槛视角**：各绝对分档的样本占比，以及"按分数取 top-k"的收益均值 vs 其余。

    读法：IC 为正且 CI 不跨 0、top−bottom 为正 ⇒ 阈值问题（分数有排序力，只是刻度低）；
    IC ≤ 0 或 CI 跨 0 ⇒ 效力问题（阈值无解）。
    """
    from stock_analyzer.learning.scoring_eval import (
        compute_quantile_returns,
        compute_rank_ic,
        date_block_bootstrap_ci,
    )

    by_day: dict[str, list[tuple[float, float]]] = {}
    for day, score, realized in pairs:
        if not math.isfinite(score) or not math.isfinite(realized):
            continue
        by_day.setdefault(str(day), []).append((score, realized))
    daily_ic: list[tuple[str, float]] = []
    for day, rows in sorted(by_day.items()):
        if len(rows) < 5:
            continue
        ic = compute_rank_ic([score for score, _ in rows], [realized for _, realized in rows])[
            "ic_spearman"
        ]
        if math.isfinite(ic):
            daily_ic.append((day, float(ic)))
    ci = date_block_bootstrap_ci(daily_ic, block_days=block_days)
    ic_mean = sum(value for _, value in daily_ic) / len(daily_ic) if daily_ic else float("nan")

    scores = [score for _, score, _ in pairs if math.isfinite(score)]
    returns = [realized for _, _, realized in pairs if math.isfinite(realized)]
    quantiles = compute_quantile_returns(scores, returns, n_quantiles=5) if scores else {}

    # 门槛视角：各档占比 + top-k 收益均值 vs 其余
    ordered = sorted(
        (
            (score, realized)
            for _, score, realized in pairs
            if math.isfinite(score) and math.isfinite(realized)
        ),
        key=lambda item: -item[0],
    )
    total = len(ordered)
    threshold_stats: dict[str, Any] = {}
    for level in THRESHOLD_LEVELS:
        hits = [realized for score, realized in ordered if score >= level]
        threshold_stats[f">={level:.0f}"] = {
            "count": len(hits),
            "share": round(len(hits) / max(total, 1), 4),
            "mean_return": round(sum(hits) / len(hits), 6) if hits else None,
        }
    top_k_stats: dict[str, Any] = {}
    for k in TOP_KS:
        if total <= k:
            continue
        head = [realized for _, realized in ordered[:k]]
        tail = [realized for _, realized in ordered[k:]]
        top_k_stats[f"top{k}"] = {
            "mean_return": round(sum(head) / len(head), 6),
            "rest_mean_return": round(sum(tail) / len(tail), 6),
            "excess": round(sum(head) / len(head) - sum(tail) / len(tail), 6),
        }
    highest = max(scores) if scores else None

    verdict = "INCONCLUSIVE"
    ci_low = ci["ci_low"]
    ci_high = ci["ci_high"]
    if daily_ic:
        if ci_low > 0.0:
            verdict = (
                "THRESHOLD_ISSUE（分数有正排序力且 CI 不跨 0 → 应改按分位选 top-k，而非拍低阈值）"
            )
        elif ci_high < 0.0:
            verdict = "EFFICACY_ISSUE（分数与收益显著负相关 → 阈值无解，放宽只会放坏票进来）"
        else:
            verdict = "INCONCLUSIVE（IC 的 CI 跨 0 → 证据不足以归因，需更长样本）"
    return {
        "samples": total,
        "days": len(by_day),
        "ic_days": len(daily_ic),
        "ic_mean": ic_mean,
        "ic_ci95": [ci_low, ci_high],
        "ic_block_days": ci.get("block_days"),
        "quantiles": {
            "means": [round(float(v), 6) for v in (quantiles.get("quantile_means") or [])],
            "top_minus_bottom": (
                round(float(quantiles["top_minus_bottom"]), 6) if quantiles else None
            ),
        },
        "threshold_stats": threshold_stats,
        "top_k_stats": top_k_stats,
        "highest_score": highest,
        "nearest_to_threshold": (
            {
                "highest_score": round(highest, 4) if highest is not None else None,
                "distance_to_70": round(70.0 - highest, 4) if highest is not None else None,
            }
            if highest is not None
            else {}
        ),
        "verdict": verdict,
        "method": (
            "生产分数取 score_breakdown_json.score（缺失回落 model_outputs_json.meta）；"
            "收益取 outcome_records.realized_return；"
            "IC 为逐日 Spearman，CI 为连续交易日块 bootstrap"
        ),
    }


def _read_pairs(
    db_path: str, *, retries: int = 3, sleep_sec: float = 5.0
) -> list[tuple[str, float, float]]:
    """只读拉取（分数, 已实现收益）配对；写锁占用时重试，拿不到就如实抛错。"""
    import duckdb

    last: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            con = duckdb.connect(db_path, read_only=True)
        except Exception as exc:  # noqa: BLE001 - 多半是"Conflicting lock"
            last = exc
            print(
                f"[retry {attempt}/{retries}] 只读打开失败（服务可能持有写锁）: {exc}", flush=True
            )
            time.sleep(sleep_sec)
            continue
        try:
            rows = con.execute(
                """
                SELECT s.decision_time,
                       COALESCE(
                           TRY_CAST(
                               json_extract_string(s.score_breakdown_json, '$.score') AS DOUBLE
                           ),
                           TRY_CAST(json_extract_string(s.model_outputs_json, '$.meta') AS DOUBLE)
                       ) AS score,
                       o.realized_return
                FROM signal_snapshots s
                JOIN outcome_records o ON o.snapshot_id = s.snapshot_id
                WHERE o.realized_return IS NOT NULL
                """
            ).fetchall()
        finally:
            con.close()
        pairs: list[tuple[str, float, float]] = []
        for day, score, realized in rows:
            if score is None or realized is None:
                continue
            pairs.append((str(day)[:10], float(score), float(realized)))
        return pairs
    raise RuntimeError(f"读不到学习库（{retries} 次重试均失败）: {last}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--block-days", type=int, default=5)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--selftest", action="store_true", help="用合成数据验证判据公式")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.selftest:
        # 日内要有方差：同一天 8 个标的，分数越高收益越高（强正相关）
        positive = [
            (f"2026-01-{day:02d}", float(rank), 0.001 * rank)
            for day in range(1, 21)
            for rank in range(8)
        ]
        # 反转对照：分数越高收益越低 → 必须判成"效力问题"
        inverted = [
            (f"2026-01-{day:02d}", float(rank), -0.001 * rank)
            for day in range(1, 21)
            for rank in range(8)
        ]
        out = {
            "positive": evaluate_pairs(positive)["verdict"],
            "inverted": evaluate_pairs(inverted)["verdict"],
        }
        print(json.dumps(out, ensure_ascii=False))
        ok = "THRESHOLD_ISSUE" in out["positive"] and "EFFICACY_ISSUE" in out["inverted"]
        print("SELFTEST=" + ("PASS" if ok else "FAIL"))
        return 0 if ok else 1

    pairs = _read_pairs(args.db, retries=args.retries)
    report = evaluate_pairs(pairs, block_days=args.block_days)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"样本 {report['samples']}（{report['days']} 个决策日，IC 有效 {report['ic_days']} 天）")
    lo, hi = report["ic_ci95"]
    print(
        f"IC 均值 {report['ic_mean']:.4f}  95% CI [{lo:.4f}, {hi:.4f}]  "
        f"块长 {report['ic_block_days']}"
    )
    quantiles = report["quantiles"]
    print(f"分位收益 {quantiles['means']}  top-minus-bottom {quantiles['top_minus_bottom']}")
    for level, stats in report["threshold_stats"].items():
        print(f"  分数 {level}: {stats['count']} 条 {stats['share']:.1%} 均 {stats['mean_return']}")
    for name, stats in report["top_k_stats"].items():
        print(
            f"  {name}: 均收益 {stats['mean_return']} vs 其余 "
            f"{stats['rest_mean_return']} 超额 {stats['excess']}"
        )
    print(f"最近门槛: {report['nearest_to_threshold']}")
    print(f"判读: {report['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
