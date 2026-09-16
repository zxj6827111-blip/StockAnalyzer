"""判据测量：生产分数与**已实现收益**的关系——回答"夜扫 0 信号是阈值问题还是效力问题"。

## 为什么要它

夜扫长期 0 个 final signal（门槛 70，全场最高 65.89，差 4.11 分）。两种解释的**处置完全相反**：

- **阈值问题**：分数与收益正相关、只是绝对刻度偏低 → 应当改成**按分位选 top-k**，
  而不是拍一个更低的绝对分；
- **效力问题**：分数与收益无关甚至负相关（8 月回测实测"分数-收益负相关"）→ **阈值无解**，
  放宽只会把没有 alpha 的票放进 final（即"为了让输出非空而放宽门禁"）。

判据很干净：**看生产分数与已实现收益的 IC 与分位价差**。

## 数据源（2026-09-16 修正）

**分数取自夜扫产物**，不是学习库：

    artifacts/runtime/scheduler_job_results/*/week5_night_scan.*.json
      → results[0].payload.report.source_report.signal_pool.candidates[]
         {snapshot_id, symbol, score(0~100), grade, action, prefilter_score, ...}

为什么不取 `signal_snapshots.score_breakdown_json`：**那里根本没有 0~100 的生产总分**。
快照存的是打分的**输入分量**（`lgbm/xgb/meta/board/completion[/news/theme_boost]`），
总分是 `ScoreEngine` 用策略权重现算的，没落库；`score_breakdown_json` 里也没有
`score` 这个键。按旧写法取 `$.score` 会整列为空、静默回落到 `model_outputs_json.meta`
（那是 0~1 的校准概率），于是"分数 ≥70 有几条"永远算成 0——把口径错误伪装成
"确认了阈值偏高"。产物里的 `score` 与 `week5.final_signal_min_threshold`（默认 70）
同一量纲，才是门槛问题该看的那一列。

`night_pool`（最终池，通常 0~1 条）不够做横截面，故默认取 `signal_pool.candidates`
（每晚 50 条左右），这正是"分数到底有没有排序力"该看的样本。

**收益取自学习库**，按 `snapshot_id` 关联（产物里每条候选都带 `snapshot_id`）：

    outcome_records(snapshot_id, realized_return, maturity_status)
    signal_snapshots(snapshot_id, decision_time)   # 决策日按 decision_time 取，不靠猜

**只读打开**（`read_only=True`），带重试——写锁在服务手里的那段时间跨进程读会失败
（实测 `Conflicting lock is held`；SampleStore 是每次操作开合连接，故只在写入期间占用），
重试几次拿不到就如实报"读不到"，不猜。

## 用法

    docker exec stock-analyzer-api python3 /app/scripts/measure_score_return.py
    docker exec stock-analyzer-api python3 /app/scripts/measure_score_return.py --json
    docker exec stock-analyzer-api python3 /app/scripts/measure_score_return.py --selftest
"""

from __future__ import annotations

import argparse
import glob
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
# 夜扫产物落点（分数来源）：按 job 分组存放，故用 */<job>.*.json
DEFAULT_RESULTS_ROOT = "/app/artifacts/runtime/scheduler_job_results"
# 生产 final 门槛，与 config.week5.final_signal_min_threshold 默认值一致（config/default.yaml
# 未覆盖，故取 dataclass 默认 70.0）。放这里只为在报告里并排显示，不做判定。
FINAL_SIGNAL_MIN_THRESHOLD = 70.0
# 判据要看的几档：60/65 是夜扫池与等级门槛，70 是 final 门槛
THRESHOLD_LEVELS = (50.0, 60.0, 65.0, FINAL_SIGNAL_MIN_THRESHOLD)
# 选股视角的 top-k：判断"按分位选前 k 名"是否优于"卡绝对分"
TOP_KS = (1, 3, 5, 10)
# 少于这个配对数就不给判读：IC 的块 bootstrap 在这种量级上没有分辨力，
# 硬给一个 THRESHOLD/EFFICACY 的结论等于把噪声说成定论。
MIN_PAIRS_FOR_VERDICT = 40
# 标签成熟口径**抄训练侧**（learning/dataset_manifest.py 的 _DEFAULT_MATURITY_STATUSES），
# 不自己另立一套：pending 的 realized_return 是中途市值标记，不是模型学过的那个标签，
# 混进来会把"分数能否预测标签"答成另一道题（并稀释 IC）。
_DEFAULT_MATURITY_STATUSES = ("label_matured", "reconciled", "fully_matured")


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

    # 分位收益必须用**成对**的点：compute_quantile_returns 内部只做同位置掩码，
    # 默认两侧本就一一对应。早先这里分数、收益各自过滤，于是"有限分数+NaN 收益"与
    # "NaN 分数+有限收益"两条会被错位配成一条——最高分那一档会被一条无关收益污染，
    # 而 top−bottom 正是本脚本要用来说"该不该改按分位选股"的那个数。
    paired = [
        (score, realized)
        for _, score, realized in pairs
        if math.isfinite(score) and math.isfinite(realized)
    ]
    scores = [score for score, _ in paired]
    returns = [realized for _, realized in paired]
    quantiles = compute_quantile_returns(scores, returns, n_quantiles=5) if scores else {}

    # 门槛视角：各档占比 + top-k 收益均值 vs 其余
    ordered = sorted(paired, key=lambda item: -item[0])
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
    # "离门槛还差几分"问的是**系统产出的最高分**，与这条有没有成熟标签无关：
    # 用全量有限分数，不用上面那组成对点（否则最高分恰好还没结算就会被漏掉）。
    all_scores = [score for _, score, _ in pairs if math.isfinite(score)]
    highest = max(all_scores) if all_scores else None

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


def _read_candidates(
    results_root: str, *, job: str = "week5_night_scan"
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """从夜扫产物里取带分数的候选（分数与门槛 70 同量纲）。

    返回 (候选列表, 扫描摘要)。候选按 ``snapshot_id`` 去重（同一晚可能跑了多次，
    重复的以最后一次为准）。读不到的产物**跳过并计数**，不静默当成"没有候选"。
    """
    pattern = str(Path(results_root) / "*" / f"{job}.*.json")
    files = sorted(glob.glob(pattern), key=lambda item: Path(item).stat().st_mtime)
    by_snapshot: dict[str, dict[str, Any]] = {}
    summary: dict[str, Any] = {
        "artifact_files": len(files),
        "runs_with_candidates": 0,
        "runs_unreadable": 0,
        "runs_without_candidates": 0,
        "candidate_source": "source_report.signal_pool.candidates",
        "night_pool_total": 0,
        "commit_set": [],
    }
    commits: set[str] = set()
    for path in files:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 产物损坏不该让整次测量失效
            summary["runs_unreadable"] += 1
            continue
        results = payload.get("results")
        entry = results[0] if isinstance(results, list) and results else {}
        entry_payload = entry.get("payload") if isinstance(entry, dict) else None
        report = (
            (entry_payload or {}).get("report") or {} if isinstance(entry_payload, dict) else {}
        )
        source = report.get("source_report") or {}
        candidates = ((source.get("signal_pool") or {}).get("candidates")) or []
        # night_pool 只用于报告"最终池有多大"，不进横截面（通常 0~1 条，做不了排序）
        pool = report.get("night_pool") or []
        summary["night_pool_total"] += len(pool)
        commit = str((payload.get("build") or {}).get("commit") or "")[:12]
        if commit:
            commits.add(commit)
        if not isinstance(candidates, list) or not candidates:
            summary["runs_without_candidates"] += 1
            continue
        taken = 0
        for item in candidates:
            if not isinstance(item, dict):
                continue
            snapshot_id = str(item.get("snapshot_id") or "").strip()
            score = item.get("score")
            if not snapshot_id or score is None:
                continue
            by_snapshot[snapshot_id] = {
                "snapshot_id": snapshot_id,
                "symbol": str(item.get("symbol") or ""),
                "score": float(score),
                "grade": str(item.get("grade") or ""),
                "action": str(item.get("action") or ""),
                "prefilter_score": item.get("prefilter_score"),
                "run_timestamp": str(payload.get("timestamp") or ""),
            }
            taken += 1
        if taken:
            summary["runs_with_candidates"] += 1
    summary["commit_set"] = sorted(commits)
    return list(by_snapshot.values()), summary


def _read_returns(
    db_path: str,
    snapshot_ids: Sequence[str],
    *,
    retries: int = 3,
    sleep_sec: float = 5.0,
    maturity_statuses: Sequence[str] = _DEFAULT_MATURITY_STATUSES,
) -> tuple[dict[str, tuple[str, float]], dict[str, Any]]:
    """按 ``snapshot_id`` 取已实现收益与**决策日**（决策日取库里 decision_time，不靠猜）。

    返回 (snapshot_id -> (决策日, 收益), 账本)。账本就是"剔掉了多少"的证据：
    口径一旦过滤，必须让人看见过滤掉了什么，否则"样本少了"和"确实没样本"分不清。
    写锁占用时重试，拿不到就如实抛错。
    """
    import duckdb

    wanted = {str(item) for item in maturity_statuses}
    ledger: dict[str, Any] = {
        "requested": len(snapshot_ids),
        "matched_outcome": 0,
        "return_null": 0,
        "immature_or_other": 0,
        "not_in_outcome_records": 0,
        "maturity_breakdown": {},
    }
    if not snapshot_ids:
        return {}, ledger

    placeholders = ", ".join("?" for _ in snapshot_ids)
    sql = (
        "SELECT o.snapshot_id, o.realized_return, o.maturity_status, s.decision_time "
        "FROM outcome_records o "
        "LEFT JOIN signal_snapshots s ON s.snapshot_id = o.snapshot_id "
        f"WHERE o.snapshot_id IN ({placeholders})"
    )
    last: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            con = duckdb.connect(db_path, read_only=True)
        except Exception as exc:  # noqa: BLE001 - 多半是"Conflicting lock"
            last = exc
            # 走 stderr：`--json` 时 stdout 必须是**干净的一个 JSON 对象**，
            # 混一行重试提示进 stdout 会让下游 json.loads 直接炸（实测踩过）。
            print(
                f"[retry {attempt}/{retries}] 只读打开失败（服务可能持有写锁）: {exc}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(sleep_sec)
            continue
        try:
            rows = con.execute(sql, list(snapshot_ids)).fetchall()
        finally:
            con.close()
        out: dict[str, tuple[str, float]] = {}
        breakdown = ledger["maturity_breakdown"]
        for snapshot_id, realized, maturity, decision_time in rows:
            status = str(maturity or "")
            breakdown[status] = breakdown.get(status, 0) + 1
            ledger["matched_outcome"] += 1
            if realized is None:
                ledger["return_null"] += 1
                continue
            if wanted and status not in wanted:
                ledger["immature_or_other"] += 1
                continue
            day = str(decision_time or "")[:10]
            out[str(snapshot_id)] = (day, float(realized))
        ledger["not_in_outcome_records"] = max(0, len(snapshot_ids) - ledger["matched_outcome"])
        return out, ledger
    raise RuntimeError(f"读不到学习库（{retries} 次重试均失败）: {last}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--block-days", type=int, default=5)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument(
        "--artifacts-root",
        default=DEFAULT_RESULTS_ROOT,
        help="夜扫产物目录（分数来源）；默认取 artifacts 卷上的 scheduler_job_results",
    )
    parser.add_argument("--job", default="week5_night_scan", help="产物名前缀（默认夜扫）")
    parser.add_argument(
        "--maturity",
        choices=("matured", "all"),
        default="matured",
        help=(
            "标签成熟口径：matured=只算训练侧认的成熟标签（默认，见 _DEFAULT_MATURITY_STATUSES）；"
            "all=连 pending 一起算（口径对照用，不用于定论）"
        ),
    )
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

    maturity = () if args.maturity == "all" else _DEFAULT_MATURITY_STATUSES
    candidates, source_summary = _read_candidates(args.artifacts_root, job=args.job)
    returns, ledger = _read_returns(
        args.db,
        [item["snapshot_id"] for item in candidates],
        retries=args.retries,
        maturity_statuses=maturity,
    )
    pairs = [
        (returns[item["snapshot_id"]][0], item["score"], returns[item["snapshot_id"]][1])
        for item in candidates
        if item["snapshot_id"] in returns
    ]
    report = evaluate_pairs(pairs, block_days=args.block_days)
    score_values = [item["score"] for item in candidates]
    days = sorted({day for day, _, _ in pairs})
    report["candidate_source"] = source_summary
    report["label_status"] = {
        "maturity_filter": args.maturity,
        "included_statuses": list(maturity) or ["<all>"],
        "breakdown": dict(sorted(ledger["maturity_breakdown"].items())),
        "breakdown_scope": "命中 outcome_records 的候选的成熟度分布",
        "requested_snapshots": ledger["requested"],
        "matched_outcome": ledger["matched_outcome"],
        "not_in_outcome_records": ledger["not_in_outcome_records"],
        "return_null": ledger["return_null"],
        "excluded_by_maturity": ledger["immature_or_other"],
    }
    report["score_scale"] = {
        "min": min(score_values) if score_values else None,
        "max": max(score_values) if score_values else None,
        "threshold": FINAL_SIGNAL_MIN_THRESHOLD,
        "note": "候选生产总分（0~100），与 week5.final_signal_min_threshold 同量纲",
    }
    report["decision_day_range"] = [days[0], days[-1]] if days else []
    if len(pairs) < MIN_PAIRS_FOR_VERDICT:
        report["verdict"] = (
            f"INSUFFICIENT_SAMPLE（可用配对 {len(pairs)} < {MIN_PAIRS_FOR_VERDICT}，"
            "样本量不足以归因；先扩样本再定论）"
        )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0
    src = report["candidate_source"]
    print(
        f"候选来源 {src['candidate_source']}  产物 {src['artifact_files']} 份"
        f"（有候选 {src['runs_with_candidates']}、无候选 {src['runs_without_candidates']}、"
        f"不可读 {src['runs_unreadable']}）"
        f"  最终池合计 {src['night_pool_total']} 条"
    )
    print(f"构建版本 {src['commit_set'] or '（未知）'}")
    scale = report["score_scale"]
    print(
        f"候选分数 {scale['min']}~{scale['max']}（门槛 {scale['threshold']}）"
        f"  进入判据 {report['samples']} 条"
    )
    label = report["label_status"]
    print(
        f"标签口径 {label['maturity_filter']}（{'+'.join(label['included_statuses'])}）"
        f"  成熟度分布 {label['breakdown']}"
        f"  因成熟度剔掉 {label['excluded_by_maturity']}"
        f"  未命中结果表 {label['not_in_outcome_records']}"
        f"  收益为空 {label['return_null']}"
    )
    print(
        f"决策日 {report['decision_day_range'] or '（无）'}"
        f"  样本 {report['samples']}（{report['days']} 个决策日，IC 有效 {report['ic_days']} 天）"
    )
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
