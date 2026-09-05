"""Phase 1.5 终门诊断矩阵（方案 §4，NAS 实库执行，纯诊断环境）。

设计：
- 固定候选：复用 Phase 1 的按日选型（"资格满足且训练截止最新"的 manifest）+
  SignalPredictor 打分 + 横截面 symbol 去重——产出「固定模型 × 固定候选分数」
  的逐日横截面。
- 终门参数矩阵：bias_reject_min ∈ {0.15, 0.20, 0.25} × atr_distance_reject ∈
  {3, 4} × score_floor ∈ {60, 65, 70} × top_K ∈ {3, 5, 10}（3×2×3×3=54 配置）。
  overextension 按 6 档 (bias, atr) 组合用统一指标重判（bias_ma5/atr_distance
  只算一次），score_floor/topK 为纯后过滤。
- 过热指标口径与生产一致（risk/overextension.py）：bias=|close/ma5-1|、
  atr_distance=|close-ma5|/atr14；market.duckdb 提供 close/ma5/atr14（由
  daily_bars 近端窗口现算，与 feature snapshot 的 ma5/atr14 同源）。
- 不改生产配置、不计正式收益、不触发晋级；输出全部 54 配置的漏斗统计 +
  首个拒绝阶段分布 + 拒绝原因计数。

**探索性多重比较结果**：54 个配置为一次性诊断输出，禁止从中挑选"最佳参数"
直接进入生产；生产参数调整必须走 Phase 2/3 的预注册评估流程（方案 §4）。
"""

from __future__ import annotations

import argparse
import itertools
import json
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from stock_analyzer.config import get_config
from stock_analyzer.learning.feature_schema_registry import FeatureSchemaRegistry
from stock_analyzer.learning.gate_metrics import gate_metrics_batch
from stock_analyzer.learning.label_policy_registry import LabelPolicyRegistry
from stock_analyzer.learning.sample_store import SampleStore
from stock_analyzer.learning.scoring_eval import ManifestEligibility, resolve_manifest_eligibility
from stock_analyzer.models.predictor import SignalPredictor
from stock_analyzer.models.trainer import ModelTrainer

DEFAULT_PROTOCOL_DB = "/app/artifacts/training/learning_protocol.duckdb"
DEFAULT_MARKET_DB = "/app/artifacts/warehouse/market.duckdb"
DEFAULT_OUT_DIR = "/app/artifacts/phase1_5"

BIAS_GRID = (0.15, 0.20, 0.25)
ATR_GRID = (3.0, 4.0)
SCORE_FLOOR_GRID = (60.0, 65.0, 70.0)
TOP_K_GRID = (3, 5, 10)


def _connect_read_only(path: str, tries: int = 30):
    last: Exception | None = None
    for _ in range(tries):
        try:
            return duckdb.connect(path, read_only=True)
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2)
    raise RuntimeError(f"lock timeout for {path}: {last}")


def _load_trading_dates(market_db: str) -> list[date]:
    conn = _connect_read_only(market_db)
    try:
        rows = conn.execute(
            "SELECT DISTINCT date FROM daily_bars WHERE date >= '2026-01-01' ORDER BY 1"
        ).fetchall()
    finally:
        conn.close()
    return [row[0] if isinstance(row[0], date) else date.fromisoformat(str(row[0])) for row in rows]


def _manifest_items(conn, manifest_id: str) -> tuple[int, list[str], list[str | None]]:
    rows = conn.execute(
        "SELECT s.decision_time, o.label_mature_time "
        "FROM dataset_manifest_items i "
        "JOIN signal_snapshots s USING (snapshot_id) "
        "LEFT JOIN outcome_records o USING (snapshot_id) "
        "WHERE i.dataset_manifest_id = ?",
        [manifest_id],
    ).fetchall()
    decisions = [str(r[0]) for r in rows]
    matures = [None if r[1] is None else str(r[1]) for r in rows]
    missing = sum(1 for row in rows if row[1] is None)
    return missing, decisions, matures


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 1.5 terminal gate diagnostic matrix")
    parser.add_argument("--eval-start", required=True)
    parser.add_argument("--eval-end", required=True)
    parser.add_argument("--protocol-db", default=DEFAULT_PROTOCOL_DB)
    parser.add_argument("--market-db", default=DEFAULT_MARKET_DB)
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    args = parser.parse_args()

    eval_start = date.fromisoformat(args.eval_start)
    eval_end = date.fromisoformat(args.eval_end)

    t0 = time.time()
    cfg = get_config()
    settlement_lag = int(cfg.evolution.execution_spec.settlement_lag)
    embargo_trading_days = int(cfg.labels.horizon_days) + settlement_lag

    all_trading_dates = _load_trading_dates(args.market_db)
    eval_dates = [d for d in all_trading_dates if eval_start <= d <= eval_end]
    if not eval_dates:
        print("no trading dates in eval range", flush=True)
        return 2

    store = SampleStore(args.protocol_db)
    feature_registry = FeatureSchemaRegistry(args.protocol_db)
    label_registry = LabelPolicyRegistry(args.protocol_db)
    conn = _connect_read_only(args.protocol_db)
    try:
        manifest_ids = [
            str(r[0])
            for r in conn.execute(
                "SELECT dataset_manifest_id FROM dataset_manifests ORDER BY 1"
            ).fetchall()
        ]
        eligibility_by_id: dict[str, ManifestEligibility] = {}
        for manifest_id in manifest_ids:
            missing, decisions, matures = _manifest_items(conn, manifest_id)
            eligibility_by_id[manifest_id] = resolve_manifest_eligibility(
                dataset_manifest_id=manifest_id,
                item_decision_times=decisions,
                item_label_mature_times=matures,
                missing_outcome_items=missing,
                embargo_trading_days=embargo_trading_days,
                trading_dates=all_trading_dates,
            )
    finally:
        conn.close()

    eligible = [e for e in eligibility_by_id.values() if e.is_eligible]
    print(f"[1] eligible manifests: {len(eligible)}/{len(eligibility_by_id)}", flush=True)

    trainer = ModelTrainer(
        training=cfg.training,
        labels=cfg.labels,
        models=cfg.models,
        settlement_lag_days=settlement_lag,
        provider=None,
        market_relative_feature=cfg.market_relative_feature,
    )
    trained: dict[str, tuple[SignalPredictor, dict[str, object]]] = {}

    def ensure_trained(elig: ManifestEligibility):
        manifest_id = elig.dataset_manifest_id
        if manifest_id in trained:
            return trained[manifest_id]
        try:
            result = trainer.train_on_dataset_manifest(
                store=store,
                dataset_manifest=manifest_id,
                feature_schema_registry=feature_registry,
                label_policy_registry=label_registry,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"    train fail {manifest_id}: {type(exc).__name__}: {exc}", flush=True)
            return None
        predictor = SignalPredictor.from_artifact(result.artifact)
        identity = {
            "dataset_manifest_id": manifest_id,
            "training_cutoff": elig.training_cutoff.isoformat() if elig.training_cutoff else "",
            "train_auc": result.metrics.get("auc"),
        }
        trained[manifest_id] = (predictor, identity)
        print(f"    trained {manifest_id} auc={identity['train_auc']}", flush=True)
        return trained[manifest_id]

    # 矩阵配置枚举（54）
    configs = list(itertools.product(BIAS_GRID, ATR_GRID, SCORE_FLOOR_GRID, TOP_K_GRID))
    funnel_totals: dict[tuple, dict[str, int]] = {
        key: {
            "candidates": 0,
            "overheat_reject": 0,
            "below_floor": 0,
            "final_selected": 0,
        }
        for key in configs
    }
    first_reject_stage_counts: dict[tuple, dict[str, int]] = {
        key: {"overheat": 0, "below_floor": 0, "topk": 0, "none": 0}
        for key in configs
    }
    bias_atr_level_counts: dict[tuple[float, float], dict[str, int]] = {
        (b, a): {"none": 0, "warn": 0, "reject": 0}
        for (b, a) in itertools.product(BIAS_GRID, ATR_GRID)
    }
    daily_records: list[dict[str, object]] = []
    score_distribution: list[dict[str, float]] = []

    # 批量预取全部 (symbol, as_of) 的过热指标（一次 SQL，本地现算）。
    print("[1.5] batch-fetching gate metrics...", flush=True)
    universe_symbols = sorted(
        {
            str(item)
            for item in store.list_snapshot_ids()
            if str(item)
        }
    )
    universe_symbols = sorted(
        {
            str(r[0])
            for r in _connect_read_only(args.protocol_db)
            .execute("SELECT DISTINCT symbol FROM signal_snapshots")
            .fetchall()
        }
    )
    gate_metrics_by_day_symbol = gate_metrics_batch(
        args.market_db, universe_symbols, eval_dates
    )
    print(f"[1.5] gate metrics: {len(gate_metrics_by_day_symbol)} entries", flush=True)

    for day in eval_dates:
        candidates_list = [
            e for e in eligible
            if e.earliest_valid_eval_date is not None and e.earliest_valid_eval_date <= day
        ]
        candidates_list.sort(
            key=lambda x: (x.training_cutoff or datetime.min.replace(tzinfo=UTC)),
            reverse=True,
        )
        chosen = None
        for e in candidates_list:
            if ensure_trained(e) is not None:
                chosen = e
                break
        if chosen is None:
            daily_records.append({"eval_date": day.isoformat(), "status": "no_model"})
            continue
        predictor, identity = trained[chosen.dataset_manifest_id]
        window_start = datetime.combine(day, datetime.min.time()).replace(tzinfo=UTC)
        window_end = window_start + timedelta(days=1)
        snapshots = store.list_snapshots(time_window_start=window_start, time_window_end=window_end)
        latest_by_symbol: dict[str, object] = {}
        for snapshot in snapshots:
            current = latest_by_symbol.get(snapshot.symbol)
            if current is None or str(snapshot.decision_time) >= str(current.decision_time):
                latest_by_symbol[snapshot.symbol] = snapshot
        snapshots = list(latest_by_symbol.values())
        if not snapshots:
            daily_records.append({"eval_date": day.isoformat(), "status": "no_snapshots"})
            continue

        rows = [
            {k: float(v) for k, v in s.feature_vector.items() if isinstance(v, (int, float))}
            for s in snapshots
        ]
        scores = predictor.predict_rows(pd.DataFrame(rows))["meta"]
        symbols = [s.symbol for s in snapshots]
        prob_scores = [round(float(s) * 100.0, 2) for s in scores]
        gate_metrics = {
            symbol: gate_metrics_by_day_symbol.get(f"{symbol}|{day.isoformat()}", {})
            for symbol in symbols
        }

        day_scores = [p for p in prob_scores if p >= 40.0]
        if day_scores:
            score_distribution.append(
                {
                    "date": day.isoformat(),
                    "n": len(day_scores),
                    "min": min(day_scores),
                    "p25": sorted(day_scores)[len(day_scores) // 4],
                    "median": sorted(day_scores)[len(day_scores) // 2],
                    "p75": sorted(day_scores)[3 * len(day_scores) // 4],
                    "max": max(day_scores),
                }
            )

        n_candidates = len(symbols)
        for bias_min, atr_min, floor, top_k in configs:
            # 过热判定（warn/reject 与生产 overextension 同层）：仅 reject 拦截。
            levels: list[str] = []
            for symbol in symbols:
                m = gate_metrics.get(symbol)
                bias = m["bias_ma5"] if m else 0.0
                atr_d = m["atr_distance"] if m else 0.0
                if bias >= bias_min or atr_d >= atr_min:
                    levels.append("reject")
                elif (
                    bias >= cfg.overextension.bias_warn_min
                    or atr_d >= cfg.overextension.atr_distance_warn
                ):
                    levels.append("warn")
                else:
                    levels.append("none")
            level_counter = bias_atr_level_counts[(bias_min, atr_min)]
            for level in levels:
                level_counter[level] += 1

            survivors = [
                (symbol, score, level)
                for symbol, score, level in zip(symbols, prob_scores, levels, strict=False)
                if level != "reject"
            ]
            n_overheat = n_candidates - len(survivors)
            passed_floor = [(s, sc) for s, sc, _ in survivors if sc >= floor]
            n_below = len(survivors) - len(passed_floor)
            ranked = sorted(passed_floor, key=lambda x: (-x[1], x[0]))[:top_k]
            key = (bias_min, atr_min, floor, top_k)
            funnel_totals[key]["candidates"] += n_candidates
            funnel_totals[key]["overheat_reject"] += n_overheat
            funnel_totals[key]["below_floor"] += n_below
            funnel_totals[key]["final_selected"] += len(ranked)
            stage_counter = first_reject_stage_counts[key]
            if ranked:
                stage_counter["none"] += 1
            elif n_overheat == n_candidates and n_candidates > 0:
                stage_counter["overheat"] += 1
            elif not passed_floor:
                stage_counter["below_floor"] += 1
            else:
                stage_counter["topk"] += 1

        daily_records.append(
            {
                "eval_date": day.isoformat(),
                "status": "ok",
                "model": identity["dataset_manifest_id"],
                "n_candidates": n_candidates,
                "score_median": (
                    sorted(prob_scores)[len(prob_scores) // 2] if prob_scores else None
                ),
            }
        )
        print(f"[2] {day} n={n_candidates} median={daily_records[-1]['score_median']}", flush=True)

    aggregate = [
        {
            "bias_reject_min": b,
            "atr_distance_reject": a,
            "score_floor": f,
            "top_k": k,
            **funnel_totals[(b, a, f, k)],
            "first_reject_days": first_reject_stage_counts[(b, a, f, k)],
        }
        for (b, a, f, k) in configs
    ]
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "eval_start": eval_start.isoformat(),
        "eval_end": eval_end.isoformat(),
        "embargo_trading_days": embargo_trading_days,
        "grid": {
            "bias": list(BIAS_GRID),
            "atr": list(ATR_GRID),
            "score_floor": list(SCORE_FLOOR_GRID),
            "top_k": list(TOP_K_GRID),
        },
        "disclaimer": (
            "探索性多重比较结果：54 配置一次性诊断输出，禁止挑选最佳参数进入生产；"
            "不计正式收益、不触发晋级（方案 §4）。"
        ),
        "bias_atr_level_counts": {
            f"bias_{b}_atr_{a}": v for (b, a), v in bias_atr_level_counts.items()
        },
        "score_distribution_daily": score_distribution,
        "aggregate": aggregate,
        "daily": daily_records,
    }
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"phase1_5_terminal_gate_matrix_{stamp}.json"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"[3] json={json_path}", flush=True)
    print(f"[done] {time.time() - t0:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
