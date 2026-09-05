"""Phase 1 时间安全打分评估 runner（方案 §3，NAS 实库执行）。

流程：
1. market.duckdb 取交易日历；协议库枚举全部 dataset manifests，按 §3 规则
   计算 earliest_valid_eval_date（max(label_mature) + embargo 个交易日）。
2. 评估区间逐交易日选「资格满足且训练截止最新」的 manifest，
   train_on_dataset_manifest 训练（按 manifest 缓存），SignalPredictor 打分。
3. 指标：日 IC + date-block bootstrap CI、五分位收益与 top-bottom 价差、
   AUC/Brier（realized_return 符号标签）、Precision@K、universe 覆盖率、
   lookahead 计算值。
4. 输出 JSON + Markdown 到 out-dir（时间戳命名，不覆盖历史）。

不写业务表、不注册 registry、不触发晋级。
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from stock_analyzer.config import get_config
from stock_analyzer.learning.feature_schema_registry import FeatureSchemaRegistry
from stock_analyzer.learning.label_policy_registry import LabelPolicyRegistry
from stock_analyzer.learning.sample_store import SampleStore
from stock_analyzer.learning.scoring_eval import (
    ManifestEligibility,
    check_lookahead,
    compute_auc_brier,
    compute_precision_at_k,
    compute_quantile_returns,
    compute_rank_ic,
    date_block_bootstrap_ci,
    resolve_manifest_eligibility,
)
from stock_analyzer.models.predictor import SignalPredictor
from stock_analyzer.models.trainer import ModelTrainer

DEFAULT_PROTOCOL_DB = "/app/artifacts/training/learning_protocol.duckdb"
DEFAULT_MARKET_DB = "/app/artifacts/warehouse/market.duckdb"
DEFAULT_OUT_DIR = "/app/artifacts/phase1"


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


def _market_symbols_on_date(market_db: str, day: date) -> int:
    conn = _connect_read_only(market_db)
    try:
        return int(
            conn.execute(
                "SELECT COUNT(DISTINCT symbol) FROM daily_bars WHERE date = ?", [day]
            ).fetchone()[0]
        )
    finally:
        conn.close()


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


def _write_outputs(
    payload: dict[str, object],
    out_dir: Path,
    eval_start: date,
    eval_end: date,
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"phase1_scoring_eval_{stamp}.json"
    md_path = out_dir / f"phase1_scoring_eval_{stamp}.md"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    embargo = payload.get("embargo_trading_days")
    lines: list[str] = [
        f"# Week5 Phase 1 时间安全打分评估（{eval_start} ~ {eval_end}）",
        "",
        f"> 生成：{datetime.now(UTC).isoformat()}；embargo={embargo} 交易日（horizon+settlement）",
        "",
    ]
    summary = payload.get("summary") or {}
    for key, value in summary.items():
        lines.append(f"- **{key}**: {value}")
    header = (
        "| eval_date | model | n_scored | n_labeled | ic_spearman "
        "| top-bottom | coverage | lookahead |"
    )
    lines += ["", "## 逐日记录", "", header, "|---|---|---|---|---|---|---|---|"]
    for record in payload.get("daily") or []:
        if not isinstance(record, dict):
            continue
        lines.append(
            "| {eval_date} | {model} | {n} | {nl} | {ic} | {tb} | {cov} | {la} |".format(
                eval_date=record.get("eval_date"),
                model=str(record.get("model_id", ""))[:32],
                n=record.get("n_scored"),
                nl=record.get("n_labeled"),
                ic=_fmt(record.get("ic_spearman")),
                tb=_fmt(record.get("top_minus_bottom")),
                cov=_fmt(record.get("universe_coverage")),
                la="Y" if record.get("lookahead_bias") else "N",
            )
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def _fmt(value: object) -> str:
    if isinstance(value, float) and math.isnan(value):
        return "NaN"
    return f"{value:.4f}" if isinstance(value, float) else str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 1 time-safe scoring evaluation")
    parser.add_argument("--eval-start", required=True, help="YYYY-MM-DD（含）")
    parser.add_argument("--eval-end", required=True, help="YYYY-MM-DD（含）")
    parser.add_argument("--protocol-db", default=DEFAULT_PROTOCOL_DB)
    parser.add_argument("--market-db", default=DEFAULT_MARKET_DB)
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    parser.add_argument("--k-list", default="5,10")
    args = parser.parse_args()

    eval_start = date.fromisoformat(args.eval_start)
    eval_end = date.fromisoformat(args.eval_end)
    k_list = [int(k) for k in args.k_list.split(",") if k.strip()]

    t0 = time.time()
    cfg = get_config()
    settlement_lag = int(cfg.evolution.execution_spec.settlement_lag)
    embargo_trading_days = int(cfg.labels.horizon_days) + settlement_lag
    print(f"[1] embargo_trading_days={embargo_trading_days}", flush=True)

    all_trading_dates = _load_trading_dates(args.market_db)
    eval_dates = [d for d in all_trading_dates if eval_start <= d <= eval_end]
    if not eval_dates:
        print("no trading dates in eval range", flush=True)
        return 2
    print(f"[1] eval trading dates: {len(eval_dates)}", flush=True)

    store = SampleStore(args.protocol_db)
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
    ineligible = [
        {"manifest_id": e.dataset_manifest_id, "reason": e.invalid_reason}
        for e in eligibility_by_id.values()
        if not e.is_eligible
    ]
    print(f"[2] manifests total={len(eligibility_by_id)} eligible={len(eligible)} "
          f"ineligible={len(ineligible)}", flush=True)
    for item in ineligible:
        print(f"    ineligible {item['manifest_id']}: {item['reason']}", flush=True)

    # manifest 训练必须按 manifest 声明的 schema/policy 解析（B1/B2 切换后
    # 当前 yaml hash 与历史 manifest 不一致，registry=None 的回退会误拒）。
    feature_registry = FeatureSchemaRegistry(args.protocol_db)
    label_registry = LabelPolicyRegistry(args.protocol_db)

    trainer = ModelTrainer(
        training=cfg.training,
        labels=cfg.labels,
        models=cfg.models,
        settlement_lag_days=settlement_lag,
        provider=None,
        market_relative_feature=cfg.market_relative_feature,
    )
    trained: dict[str, tuple[SignalPredictor, dict[str, object]]] = {}

    def ensure_trained(
        elig: ManifestEligibility,
    ) -> tuple[SignalPredictor, dict[str, object]] | None:
        manifest_id = elig.dataset_manifest_id
        if manifest_id in trained:
            return trained[manifest_id]
        started = time.time()
        try:
            result = trainer.train_on_dataset_manifest(
                store=store,
                dataset_manifest_id=manifest_id,
                feature_schema_registry=feature_registry,
                label_policy_registry=label_registry,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"    train fail {manifest_id}: {type(exc).__name__}: {exc}", flush=True)
            eligibility_by_id[manifest_id] = replace(
                elig, invalid_reason=f"train_error:{type(exc).__name__}"
            )
            eligible[:] = [e for e in eligible if e.dataset_manifest_id != manifest_id]
            return None
        predictor = SignalPredictor.from_artifact(result.artifact)
        identity = {
            "model_id": f"phase1_eval_{manifest_id}",
            "dataset_manifest_id": manifest_id,
            "feature_schema_id": result.artifact.feature_schema_id,
            "label_policy_id": result.artifact.label_policy_id,
            "training_cutoff": elig.training_cutoff.isoformat() if elig.training_cutoff else "",
            "max_label_mature_time": (
                elig.max_label_mature_time.isoformat() if elig.max_label_mature_time else ""
            ),
            "embargo_trading_days": elig.embargo_trading_days,
            "earliest_valid_eval_date": (
                elig.earliest_valid_eval_date.isoformat() if elig.earliest_valid_eval_date else ""
            ),
            "train_auc": result.metrics.get("auc"),
            "train_samples_total": result.samples_total,
            "train_seconds": round(time.time() - started, 2),
        }
        trained[manifest_id] = (predictor, identity)
        print(
            f"    trained {manifest_id} {identity['train_seconds']}s "
            f"auc={identity['train_auc']}",
            flush=True,
        )
        return trained[manifest_id]

    daily_records: list[dict[str, object]] = []
    pooled_scores: list[float] = []
    pooled_labels: list[float] = []
    pooled_returns: list[float] = []
    model_usage: dict[str, int] = {}

    for day in eval_dates:
        candidates = [
            e for e in eligible
            if e.earliest_valid_eval_date is not None and e.earliest_valid_eval_date <= day
        ]
        candidates.sort(
            key=lambda x: (x.training_cutoff or datetime.min.replace(tzinfo=UTC)),
            reverse=True,
        )
        chosen = None
        for e in candidates:
            if ensure_trained(e) is not None:
                chosen = e
                break
        if chosen is None:
            daily_records.append({
                "eval_date": day.isoformat(),
                "evaluation_validity": "invalid",
                "invalid_reason": "no_trained_model_available",
                "n_scored": 0,
            })
            continue

        predictor, identity = trained[chosen.dataset_manifest_id]
        window_start = datetime.combine(day, datetime.min.time()).replace(tzinfo=UTC)
        window_end = window_start + timedelta(days=1)
        snapshots = store.list_snapshots(time_window_start=window_start, time_window_end=window_end)
        n_universe = _market_symbols_on_date(args.market_db, day)

        if not snapshots:
            daily_records.append({
                "eval_date": day.isoformat(),
                "evaluation_validity": "invalid",
                "invalid_reason": "no_snapshots_on_eval_date",
                "model_id": identity["model_id"],
                "n_universe_market": n_universe,
            })
            continue

        outcome_map = {
            o.snapshot_id: o
            for o in store.list_outcomes(snapshot_ids=[s.snapshot_id for s in snapshots])
        }
        symbols: list[str] = []
        rows: list[dict[str, float]] = []
        realized_list: list[float | None] = []
        label_list: list[float | None] = []
        for snapshot in snapshots:
            outcome = outcome_map.get(snapshot.snapshot_id)
            realized = None
            if (
                outcome is not None
                and outcome.maturity_status == "label_matured"
                and outcome.realized_return is not None
            ):
                realized = float(outcome.realized_return)
            symbols.append(snapshot.symbol)
            numeric = {
                key: float(value)
                for key, value in snapshot.feature_vector.items()
                if isinstance(value, (int, float))
            }
            rows.append(numeric)
            realized_list.append(realized)
            label_list.append(None if realized is None else (1.0 if realized > 0.0 else 0.0))

        scores = predictor.predict_rows(pd.DataFrame(rows))["meta"]
        scored = [
            (float(score), realized, label)
            for score, realized, label in zip(scores, realized_list, label_list, strict=False)
        ]
        labeled = [
            (score, realized, label)
            for score, realized, label in scored
            if realized is not None and label is not None
        ]
        lookahead = check_lookahead(
            eligibility=chosen,
            eval_date=day,
            snapshot_decision_dates=[window_start.date()],
        )
        score_arr = [item[0] for item in scored]
        ret_arr = [item[1] if item[1] is not None else float("nan") for item in scored]
        ic = compute_rank_ic(score_arr, ret_arr)
        quantiles = compute_quantile_returns(score_arr, ret_arr, n_quantiles=5)
        label_arr = [item[2] if item[2] is not None else float("nan") for item in scored]
        auc_brier = compute_auc_brier(score_arr, label_arr)
        precision = {
            f"precision_at_{k}": compute_precision_at_k(score_arr, label_arr, k=k)["precision_at_k"]
            for k in k_list
        }
        pooled_scores.extend(item[0] for item in labeled)
        pooled_labels.extend(float(item[2]) for item in labeled)
        pooled_returns.extend(float(item[1]) for item in labeled)
        model_usage[str(identity["model_id"])] = model_usage.get(str(identity["model_id"]), 0) + 1

        daily_records.append({
            "eval_date": day.isoformat(),
            "evaluation_validity": "valid",
            "model_id": identity["model_id"],
            "manifest_id": chosen.dataset_manifest_id,
            "training_cutoff": identity["training_cutoff"],
            "embargo_trading_days": chosen.embargo_trading_days,
            "earliest_valid_eval_date": identity["earliest_valid_eval_date"],
            "n_scored": len(scored),
            "n_labeled": len(labeled),
            "n_universe_market": n_universe,
            "universe_coverage": (len(scored) / n_universe) if n_universe else None,
            "ic_spearman": ic["ic_spearman"],
            "ic_pearson": ic["ic_pearson"],
            "quantile_means": quantiles["quantile_means"],
            "top_minus_bottom": quantiles["top_minus_bottom"],
            "auc": auc_brier["auc"],
            "brier": auc_brier["brier"],
            **precision,
            **lookahead,
        })
        print(f"[3] {day} scored={len(scored)} labeled={len(labeled)} "
              f"ic={ic['ic_spearman']:.4f} auc={auc_brier['auc']:.4f}", flush=True)

    daily_ic = []
    for record in daily_records:
        ic_value = record.get("ic_spearman")
        if record.get("evaluation_validity") == "valid" and isinstance(ic_value, float):
            daily_ic.append((date.fromisoformat(str(record["eval_date"])), ic_value))
    pooled_auc = compute_auc_brier(pooled_scores, pooled_labels)
    ci = date_block_bootstrap_ci(daily_ic)
    valid_days = [r for r in daily_records if r.get("evaluation_validity") == "valid"]
    quantile_series = [r.get("quantile_means") for r in valid_days if r.get("quantile_means")]
    mean_quantiles = None
    if quantile_series:
        width = max(len(q) for q in quantile_series)
        mean_quantiles = [
            _nanmean([q[i] for q in quantile_series if i < len(q)]) for i in range(width)
        ]
    summary = {
        "evaluation_validity": "valid" if valid_days else "invalid",
        "model_id": "multi" if len(model_usage) > 1 else next(iter(model_usage), ""),
        "models_used": model_usage,
        "eval_days_total": len(eval_dates),
        "eval_days_valid": len(valid_days),
        "eval_days_invalid": len(daily_records) - len(valid_days),
        "embargo_trading_days": embargo_trading_days,
        "pooled_auc": pooled_auc["auc"],
        "pooled_brier": pooled_auc["brier"],
        "pooled_n_labeled": len(pooled_labels),
        "pooled_mean_realized_return": _nanmean(pooled_returns),
        "daily_ic_mean": _nanmean([v for _, v in daily_ic]),
        "daily_ic_ci95": [ci["ci_low"], ci["ci_high"]],
        "mean_quantile_returns": mean_quantiles,
        "note": "label=realized_return sign（诊断口径）；IC/分位收益用 realized_return 连续值",
    }
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "eval_start": eval_start.isoformat(),
        "eval_end": eval_end.isoformat(),
        "embargo_trading_days": embargo_trading_days,
        "summary": summary,
        "ineligible_manifests": ineligible,
        "daily": daily_records,
    }
    json_path, md_path = _write_outputs(payload, Path(args.out_dir), eval_start, eval_end)
    print(f"[4] json={json_path}", flush=True)
    print(f"[4] md={md_path}", flush=True)
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False, default=str), flush=True)
    print(f"[done] total {time.time() - t0:.1f}s", flush=True)
    return 0


def _nanmean(values: list[float]) -> float:
    cleaned = [v for v in values if not math.isnan(float(v))]
    return sum(cleaned) / len(cleaned) if cleaned else float("nan")


if __name__ == "__main__":
    raise SystemExit(main())
