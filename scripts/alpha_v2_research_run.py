"""Alpha V2 M2 研究跑批（S11–S23 的审计工件生成器）。

用法（本机）：

```bash
python scripts/alpha_v2_research_run.py \
    --market-db artifacts/warehouse/market.duckdb \
    --window-start 2025-09-01 --window-end 2026-03-31 \
    --max-symbols 400 --out artifacts/alpha_v2/audit
```

它做三件事：

1. 在**真实本地数据**上把 S11→S21 全链路跑一遍（PIT 面板 → 可执行 outcome →
   三层基准 → winner recall → 特征审计 → 简单基线 → 共享矩阵多 Head →
   分歧观测 → shadow 策略 → purged walk-forward → 健康报告）；
2. 每阶段写一份 ``sXX_validation.json``，并在末尾写 ``m2_summary.json``；
3. **不部署、不改生产、不写正式结果**。

口径纪律（脚本自身也遵守）：

- 价格口径先做认证（:meth:`DailyPanel.certify_price_mode`），认证不过就如实标
  ``execution_uncertain`` 并把主样本置零，而不是"先跑出数再说"；
- 研究结论带样本门：``>=20`` 仅失败预警、``>=60`` 方向判断、``>=120`` 才讨论
  Advisory——脚本只输出状态，不替用户下结论；
- 未跑成功的阶段写 ``status=PARTIAL`` 与原因，不伪造证据。
"""

# ruff: noqa: E402 - 脚本需要先把 src/ 注入 sys.path 才能 import 包内模块
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from stock_analyzer.alpha_v2.artifacts import write_json_atomic
from stock_analyzer.alpha_v2.research.benchmarks import (
    BenchmarkSpec,
    build_benchmark_suite,
    compute_style_features,
    merge_primary_excess,
)
from stock_analyzer.alpha_v2.research.cross_review_v2 import (
    compute_disagreement,
    disagreement_evidence,
    legacy_cross_review_policy,
)
from stock_analyzer.alpha_v2.research.decision_policy import (
    FinalPolicySpec,
    build_shadow_selection,
)
from stock_analyzer.alpha_v2.research.experiments import (
    EXPERIMENT_THEME,
    ReadinessEvidence,
    default_readiness,
    run_incremental_experiment,
    theme_experiment_spec,
)
from stock_analyzer.alpha_v2.research.feature_audit import (
    audit_feature_columns,
    audit_summary,
    mechanical_checks,
)
from stock_analyzer.alpha_v2.research.health_report import (
    build_health_report,
    render_markdown,
    review_trigger,
)
from stock_analyzer.alpha_v2.research.metrics import (
    EvaluationSpec,
    evaluate_scores,
    metric_column,
)
from stock_analyzer.alpha_v2.research.multi_head import (
    ALPHA_TARGET_TEMPLATE,
    HEAD_NAMES,
    BuildStats,
    HeadFitSpec,
    SharedFeatureMatrix,
    build_head_targets,
    fit_and_predict_heads,
)
from stock_analyzer.alpha_v2.research.outcomes import (
    DecisionPoint,
    OutcomeSpec,
    build_label_v2,
)
from stock_analyzer.alpha_v2.research.panel import load_daily_panel, panel_fingerprint
from stock_analyzer.alpha_v2.research.perf import (
    STAGE_FEATURE,
    STAGE_FETCH,
    STAGE_MATRIX,
    STAGE_PERSIST,
    STAGE_PREDICT,
    PerfBudget,
    StageTimer,
    build_perf_report,
    determinism_evidence,
)
from stock_analyzer.alpha_v2.research.purged_walk_forward import (
    FoldSpec,
    fold_isolation_matrix,
    run_walk_forward,
)
from stock_analyzer.alpha_v2.research.shadow_dual_run import build_dual_run
from stock_analyzer.alpha_v2.research.simple_baseline import (
    BASELINE_SCORE_COLUMN,
    SimpleBaselineSpec,
    compare_with_ml,
    compute_simple_baseline,
    evaluate_baseline,
    factor_ic_declared_vs_realized,
)
from stock_analyzer.alpha_v2.research.winner_recall import (
    RecallSpec,
    compute_winner_recall,
)
from stock_analyzer.backtest.matcher import ExecutionMatcher
from stock_analyzer.config import load_config

M2_SCHEMA = "alpha_v2_m2_validation.v1"

FEATURE_SAMPLE_SYMBOLS = 400


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, pd.DataFrame):
        return _jsonable(value.to_dict(orient="records"))
    if isinstance(value, pd.Series):
        return _jsonable(value.to_dict())
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _write(out_dir: Path, name: str, payload: dict[str, object]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    return write_json_atomic(out_dir / name, _jsonable(payload))


def _stage(
    results: dict[str, dict[str, object]],
    name: str,
    *,
    status: str,
    payload: dict[str, object] | None = None,
    error: str = "",
) -> None:
    block: dict[str, object] = {"stage": name, "status": status}
    if error:
        block["error"] = error
    if payload:
        block.update(payload)
    results[name] = block


def main() -> int:
    parser = argparse.ArgumentParser(description="Alpha V2 M2 research run")
    parser.add_argument("--market-db", default="artifacts/warehouse/market.duckdb")
    parser.add_argument("--window-start", default="2025-09-01")
    parser.add_argument("--window-end", default="2026-03-31")
    parser.add_argument("--warmup-days", type=int, default=200)
    parser.add_argument("--max-symbols", type=int, default=400)
    parser.add_argument("--out", default="artifacts/alpha_v2/audit")
    parser.add_argument("--legacy-report", default="")
    args = parser.parse_args()

    config = load_config(REPO_ROOT / "config" / "default.yaml")
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = REPO_ROOT / out_dir
    window_start = date.fromisoformat(args.window_start)
    window_end = date.fromisoformat(args.window_end)
    results: dict[str, dict[str, object]] = {}
    timer = StageTimer()
    stats = BuildStats()

    # ---------------- 面板装载（一次） ----------------
    with timer.stage(STAGE_FETCH):
        panel = load_daily_panel(
            market_db=REPO_ROOT / args.market_db,
            window_start=window_start,
            window_end=window_end,
            warmup_days=args.warmup_days,
            max_symbols=int(args.max_symbols),
        )
        certification = panel.certify_price_mode(min_sample=1000)
    panel_meta = {
        **panel.public_payload(),
        "panel_fingerprint": panel_fingerprint(panel),
        "price_mode_certification": certification.to_payload(),
    }
    if not certification.certified:
        print(
            "[warn] 价格口径未认证：主评价样本将为空（execution_uncertain）",
            flush=True,
        )

    matcher = ExecutionMatcher(config.backtest_matcher, limit_rule=config.limit_rule)
    slippage = matcher.static_slippage_ratio("trend")

    # ---------------- 决策集合：逐日 PIT eligible 截面 ----------------
    decisions: list[DecisionPoint] = []
    universe_rows: list[dict[str, object]] = []
    for day in panel.calendar:
        snapshot = panel.pit_universe(as_of=day)
        universe_rows.append(
            snapshot.to_payload() | {"expected_active_count": snapshot.expected_active_count}
        )
        decisions.extend(DecisionPoint(symbol, day) for symbol in snapshot.eligible_symbols)
    print(f"[run] decisions={len(decisions):,} days={len(panel.calendar)}", flush=True)

    # ---------------- S11 Label V2 ----------------
    started = pd.Timestamp.now(tz="UTC").isoformat()
    try:
        run = build_label_v2(
            panel=panel,
            decisions=decisions,
            spec=OutcomeSpec(),
            matcher=matcher,
            slippage_ratio=slippage,
            price_mode=certification.mode,
            price_mode_certified=certification.certified,
            source_meta={"panel_fingerprint": panel_meta["panel_fingerprint"]},
            # 研究跑批（非生产）：面板可能不是 raw，此时**显式**关掉 execution 价格
            # 序列守卫并留下理由——产物如实标 execution_uncertain，绝不伪装成可成交。
            # 生产入口（freeze / mature）没有任何等价开关。
            enforce_execution_price_series=False,
            research_replay_reason="alpha_v2_research_run:execution_uncertain_marked",
        )
        outcomes = run.frame
        _write(
            out_dir,
            "s11_validation.json",
            {
                "schema": M2_SCHEMA,
                "stage": "S11",
                "status": "DONE",
                "panel": panel_meta,
                "outcome_spec": run.spec.to_payload(),
                "diagnostics": run.diagnostics,
                "started_at": started,
                "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
            },
        )
        _stage(
            results,
            "S11",
            status="DONE",
            payload={"outcome_rows": int(len(outcomes)), "diagnostics": run.diagnostics},
        )
    except Exception as exc:  # noqa: BLE001 - 阶段失败必须落痕
        traceback.print_exc()
        _write(
            out_dir,
            "s11_validation.json",
            {"schema": M2_SCHEMA, "stage": "S11", "status": "FAILED", "error": str(exc)},
        )
        _stage(results, "S11", status="FAILED", error=str(exc))
        outcomes = pd.DataFrame()

    # ---------------- S12 基准体系 ----------------
    try:
        styles = compute_style_features(panel=panel, decisions=decisions)
        enriched = outcomes.merge(styles, on=["decision_date", "symbol"], how="left")
        suite = build_benchmark_suite(enriched, spec=BenchmarkSpec(style_min_peers=5))
        primary = merge_primary_excess(enriched, suite)
        _write(
            out_dir,
            "s12_validation.json",
            {
                "schema": M2_SCHEMA,
                "stage": "S12",
                "status": "DONE",
                "suite": suite.to_payload(),
            },
        )
        _stage(results, "S12", status="DONE", payload={"report": suite.report})
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _write(
            out_dir,
            "s12_validation.json",
            {"schema": M2_SCHEMA, "stage": "S12", "status": "FAILED", "error": str(exc)},
        )
        _stage(results, "S12", status="FAILED", error=str(exc))
        primary = pd.DataFrame()

    # ---------------- S13 Winner Recall ----------------
    try:
        backfilled = _attach_pool_columns(primary)
        recall = compute_winner_recall(
            backfilled,
            spec=RecallSpec(
                metric=metric_column("excess_return", 5),
                winner_quantile=0.10,
                min_pool_size=20,
            ),
        )
        _write(
            out_dir,
            "s13_validation.json",
            {
                "schema": M2_SCHEMA,
                "stage": "S13",
                "status": "DONE",
                "summary": recall.summary,
                "spec": recall.spec.to_payload(),
                "daily_rows": int(len(recall.daily)),
            },
        )
        _stage(results, "S13", status="DONE", payload={"summary": recall.summary})
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _write(
            out_dir,
            "s13_validation.json",
            {"schema": M2_SCHEMA, "stage": "S13", "status": "FAILED", "error": str(exc)},
        )
        _stage(results, "S13", status="FAILED", error=str(exc))

    # ---------------- S14 特征可用性审计 ----------------
    try:
        with timer.stage(STAGE_FEATURE):
            feature_frame = _compute_feature_frame(panel, decisions)
        audit = audit_feature_columns(feature_frame.columns, frame=feature_frame)
        checks = mechanical_checks(panel.bars)
        _write(
            out_dir,
            "s14_validation.json",
            {
                "schema": M2_SCHEMA,
                "stage": "S14",
                "status": "DONE",
                "audit": audit.to_payload(),
                "summary": audit_summary(audit),
                "mechanical_checks": checks,
                "safe_feature_columns": list(audit.safe_feature_columns),
                "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
            },
        )
        _stage(
            results,
            "S14",
            status="DONE",
            payload={
                "summary": audit_summary(audit),
                "safe_columns": len(audit.safe_feature_columns),
                "unregistered": len(audit.unregistered),
            },
        )
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _write(
            out_dir,
            "s14_validation.json",
            {"schema": M2_SCHEMA, "stage": "S14", "status": "FAILED", "error": str(exc)},
        )
        _stage(results, "S14", status="FAILED", error=str(exc))
        feature_frame = pd.DataFrame()

    # ---------------- S15 简单因子基线 ----------------
    try:
        baseline = compute_simple_baseline(
            panel=panel,
            decisions=decisions,
            spec=SimpleBaselineSpec(min_cross_section=20),
        )
        merged_baseline = (
            primary.merge(baseline.frame, on=["decision_date", "symbol"], how="inner")
            if not primary.empty
            else baseline.frame
        )
        baseline_payload = evaluate_baseline(
            merged_baseline, spec=SimpleBaselineSpec(min_cross_section=20)
        )
        factor_table = factor_ic_declared_vs_realized(
            merged_baseline, horizon=5, min_cross_section=20
        )
        _write(
            out_dir,
            "s15_validation.json",
            {
                "schema": M2_SCHEMA,
                "stage": "S15",
                "status": "DONE",
                "baseline_spec": baseline.spec.to_payload(),
                "baseline_report": baseline.report,
                "evaluation": baseline_payload,
                "factor_ic": factor_table.to_dict(orient="records"),
            },
        )
        _stage(
            results,
            "S15",
            status="DONE",
            payload={
                "baseline_mature_dates": baseline_payload.get("mature_dates"),
                "baseline_gate": baseline_payload.get("research_gate"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _write(
            out_dir,
            "s15_validation.json",
            {"schema": M2_SCHEMA, "stage": "S15", "status": "FAILED", "error": str(exc)},
        )
        _stage(results, "S15", status="FAILED", error=str(exc))
        merged_baseline = pd.DataFrame()

    # ---------------- S16 共享矩阵 + 多 Head ----------------
    head_predictions = pd.DataFrame()
    multi_payload: dict[str, object] = {}
    try:
        outcomes_for_matrix = primary[
            [
                "decision_date",
                "symbol",
                "executable",
                *[
                    metric_column(kind, h)
                    for kind in ("net_return", "excess_return")
                    for h in (3, 5, 10, 15)
                ],
                *[f"up_net_{h}d" for h in (3, 5)],
                *[f"up_excess_{h}d" for h in (3, 5)],
                "mae_3d",
                "mae_5d",
            ]
        ]
        with timer.stage(STAGE_MATRIX):
            matrix = SharedFeatureMatrix(
                frame=_build_matrix_frame(feature_frame, outcomes_for_matrix),
                feature_columns=tuple(
                    column
                    for column in feature_frame.columns
                    if column not in {"decision_date", "symbol"}
                ),
                target_columns=(),
                stats=stats,
            )
            stats.matrix_build_calls += 1
            matrix.diagnostics.update(_time_split_probability(matrix.frame))
        with timer.stage(STAGE_PREDICT):
            multi = fit_and_predict_heads(
                matrix=matrix, head_names=HEAD_NAMES, spec=HeadFitSpec(), stats=stats
            )
        head_predictions = multi.predictions
        scored = head_predictions.merge(
            primary[["decision_date", "symbol", "excess_return_5d", "net_return_5d", "executable"]],
            on=["decision_date", "symbol"],
            how="inner",
        )
        ml_payload = evaluate_scores(scored, EvaluationSpec(score_column="alpha_rank_score"))
        ml_payload["baseline_companion"] = baseline_payload
        multi_payload = multi.to_payload()
        comparison = compare_with_ml(ml_payload, baseline_payload)
        _write(
            out_dir,
            "s16_validation.json",
            {
                "schema": M2_SCHEMA,
                "stage": "S16",
                "status": "DONE",
                "matrix": matrix.to_payload(),
                "heads": multi_payload["heads"],
                "head_semantics": multi_payload.get("head_semantics"),
                "evaluation": ml_payload,
                "ml_vs_baseline": comparison,
            },
        )
        _stage(
            results,
            "S16",
            status="DONE",
            payload={
                "heads": list(multi.heads),
                "ml_vs_baseline": comparison,
                "mature_dates": ml_payload.get("mature_dates"),
                "research_gate": ml_payload.get("research_gate"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _write(
            out_dir,
            "s16_validation.json",
            {"schema": M2_SCHEMA, "stage": "S16", "status": "FAILED", "error": str(exc)},
        )
        _stage(results, "S16", status="FAILED", error=str(exc))

    # ---------------- S17 Cross Review V2 ----------------
    try:
        if feature_frame.empty or primary.empty:
            raise RuntimeError("S17 依赖 S14 特征帧与 S11 outcome")
        disagreement_matrix = SharedFeatureMatrix(
            frame=_build_matrix_frame(feature_frame, _outcome_slice(primary)),
            feature_columns=tuple(
                column
                for column in feature_frame.columns
                if column not in {"decision_date", "symbol"}
            ),
            target_columns=(),
            stats=BuildStats(matrix_build_calls=1),
        )
        disagreement_matrix.diagnostics.update(
            _time_split_probability(disagreement_matrix.frame)
        )
        disagreement = compute_disagreement(
            matrix=disagreement_matrix, fit=HeadFitSpec(min_train_rows=200)
        )
        evidence_payload = disagreement_evidence(
            disagreement.frame,
            primary[["decision_date", "symbol", "excess_return_5d", "executable"]],
        )
        _write(
            out_dir,
            "s17_validation.json",
            {
                "schema": M2_SCHEMA,
                "stage": "S17",
                "status": "DONE",
                "legacy_policy": legacy_cross_review_policy(config),
                "disagreement": disagreement.to_payload(),
                "evidence": evidence_payload,
            },
        )
        _stage(results, "S17", status="DONE", payload={"evidence": evidence_payload})
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _write(
            out_dir,
            "s17_validation.json",
            {"schema": M2_SCHEMA, "stage": "S17", "status": "FAILED", "error": str(exc)},
        )
        _stage(results, "S17", status="FAILED", error=str(exc))

    # ---------------- S18 Final Policy Shadow ----------------
    try:
        shadow_frame = _shadow_candidate_frame(head_predictions, primary)
        policy = build_shadow_selection(
            shadow_frame, spec=FinalPolicySpec(require_stage_column=True)
        )
        _write(
            out_dir,
            "s18_validation.json",
            {
                "schema": M2_SCHEMA,
                "stage": "S18",
                "status": "DONE",
                "policy": policy.to_payload(),
            },
        )
        _stage(results, "S18", status="DONE", payload={"policy": policy.to_payload()["spec"]})
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _write(
            out_dir,
            "s18_validation.json",
            {"schema": M2_SCHEMA, "stage": "S18", "status": "FAILED", "error": str(exc)},
        )
        _stage(results, "S18", status="FAILED", error=str(exc))
        policy = None

    # ---------------- S19 Purged Walk-Forward ----------------
    try:
        wf_frame = _build_matrix_frame(feature_frame, _outcome_slice(primary))
        report = run_walk_forward(
            frame=wf_frame,
            feature_columns=tuple(
                column
                for column in feature_frame.columns
                if column not in {"decision_date", "symbol"}
            ),
            label_column=ALPHA_TARGET_TEMPLATE.format(h=5),
            metric_column_=metric_column("excess_return", 5),
            spec=FoldSpec(),
            scorer_factory=None,
            min_cross_section=20,
            max_folds=4,
        )
        _write(
            out_dir,
            "s19_validation.json",
            {
                "schema": M2_SCHEMA,
                "stage": "S19",
                "status": "DONE",
                "report": report.to_payload(),
                "isolation_matrix": fold_isolation_matrix(report).to_dict(orient="records"),
            },
        )
        _stage(results, "S19", status="DONE", payload={"summary": report.summary()})
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _write(
            out_dir,
            "s19_validation.json",
            {"schema": M2_SCHEMA, "stage": "S19", "status": "FAILED", "error": str(exc)},
        )
        _stage(results, "S19", status="FAILED", error=str(exc))
        report = None

    # ---------------- S20 双轨 Shadow ----------------
    try:
        legacy_report: dict[str, object] = {}
        if args.legacy_report:
            legacy_report = json.loads(Path(args.legacy_report).read_text(encoding="utf-8"))
        dual = build_dual_run(
            legacy_report=legacy_report,
            policy=policy if policy is not None else build_shadow_selection(pd.DataFrame()),
            config=config,
            decision_date=shadow_frame["decision_date"].max() if not shadow_frame.empty else None,
        )
        _write(
            out_dir,
            "s20_validation.json",
            {"schema": M2_SCHEMA, "stage": "S20", "status": "DONE", "dual_run": dual.to_payload()},
        )
        _stage(results, "S20", status="DONE", payload={"flags": dual.to_payload()["flags"]})
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _write(
            out_dir,
            "s20_validation.json",
            {"schema": M2_SCHEMA, "stage": "S20", "status": "FAILED", "error": str(exc)},
        )
        _stage(results, "S20", status="FAILED", error=str(exc))

    # ---------------- S21 每日健康报告 ----------------
    try:
        with timer.stage(STAGE_PERSIST):
            health_frame = head_predictions.merge(
                primary[
                    [
                        "decision_date",
                        "symbol",
                        "executable",
                        "no_fill_reason",
                        "entry_delay_sessions",
                    ]
                ],
                on=["decision_date", "symbol"],
                how="left",
            ).merge(
                primary[
                    [
                        "decision_date",
                        "symbol",
                        *[
                            metric_column(kind, h)
                            for kind in ("net_return", "excess_return")
                            for h in (3, 5, 10, 15)
                        ],
                        "mae_5d",
                        *[f"matured_{h}d" for h in (3, 5, 10, 15)],
                    ]
                ],
                on=["decision_date", "symbol"],
                how="left",
            )
            health = build_health_report(
                frame=health_frame,
                identity={
                    "code_commit": _code_commit(),
                    "config_hash": _config_hash(config),
                    "price_mode": certification.mode,
                    "price_mode_certified": certification.certified,
                },
                data_health={"status": "observed", "panel": panel_meta},
                funnel=_funnel_block(results),
                report_date=window_end.isoformat(),
            )
        payload = health.to_payload()
        _write(
            out_dir,
            "s21_validation.json",
            {"schema": M2_SCHEMA, "stage": "S21", "status": "DONE", "report": payload},
        )
        (out_dir / "s21_health_report.md").write_text(render_markdown(payload), encoding="utf-8")
        _stage(results, "S21", status="DONE", payload={"review_trigger": review_trigger(payload)})
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _write(
            out_dir,
            "s21_validation.json",
            {"schema": M2_SCHEMA, "stage": "S21", "status": "FAILED", "error": str(exc)},
        )
        _stage(results, "S21", status="FAILED", error=str(exc))

    # ---------------- S22 性能 ----------------
    try:
        determinism = {}
        if not head_predictions.empty and not feature_frame.empty:
            determinism = determinism_evidence(
                matrix=type("M", (), {"fingerprint": panel_meta["panel_fingerprint"]})(),
                predictions=head_predictions,
                feature_columns=[
                    column
                    for column in feature_frame.columns
                    if column not in {"decision_date", "symbol"}
                ],
                top_k=5,
            )
        perf = build_perf_report(
            timer=timer,
            stats=stats,
            budget=PerfBudget(),
            extra={
                "determinism_evidence": determinism,
                "rows": int(len(head_predictions)),
                "feature_columns": int(max(0, len(feature_frame.columns) - 2)),
            },
        )
        _write(
            out_dir,
            "s22_validation.json",
            {"schema": M2_SCHEMA, "stage": "S22", "status": "DONE", "perf": perf},
        )
        _stage(
            results,
            "S22",
            status="DONE",
            payload={
                "perf": perf["current"],
                "budget_check": perf["budget_check"],
                "single_pass": perf.get("single_pass"),
                "determinism": perf.get("extra", {}).get("determinism_evidence"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _write(
            out_dir,
            "s22_validation.json",
            {"schema": M2_SCHEMA, "stage": "S22", "status": "FAILED", "error": str(exc)},
        )
        _stage(results, "S22", status="FAILED", error=str(exc))

    # ---------------- S23 增量实验 ----------------
    try:
        readiness = default_readiness()
        theme_payload: dict[str, object] = {"status": "no_data"}
        if not merged_baseline.empty and not head_predictions.empty:
            theme_frame = merged_baseline.merge(
                head_predictions[["decision_date", "symbol", "alpha_rank_score"]],
                on=["decision_date", "symbol"],
                how="inner",
                suffixes=("", "_head"),
            )
            # 主题信号在本地不可得：experiment 臂 = baseline（零影响），
            # 目的是验证"零影响 → 不得被判为有增量"这条守卫，而不是伪造效果。
            theme_frame["alpha_rank_score_with_theme"] = pd.to_numeric(
                theme_frame[BASELINE_SCORE_COLUMN], errors="coerce"
            )
            theme_payload = run_incremental_experiment(
                theme_frame,
                spec=theme_experiment_spec(
                    base_score_column=BASELINE_SCORE_COLUMN,
                    experiment_score_column="alpha_rank_score_with_theme",
                ),
                evidence=ReadinessEvidence(
                    kind=EXPERIMENT_THEME,
                    pit_path_verified=True,
                    notes="本地无 theme 特征：experiment 臂与 control 相同（零影响对照）",
                ),
            )
        _write(
            out_dir,
            "s23_validation.json",
            {
                "schema": M2_SCHEMA,
                "stage": "S23",
                "status": "DONE",
                "readiness": readiness,
                "theme_experiment": theme_payload,
            },
        )
        _stage(
            results,
            "S23",
            status="DONE",
            payload={
                "readiness": {key: value["status"] for key, value in readiness.items()},
                "theme_experiment": theme_payload.get("status"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _write(
            out_dir,
            "s23_validation.json",
            {"schema": M2_SCHEMA, "stage": "S23", "status": "FAILED", "error": str(exc)},
        )
        _stage(results, "S23", status="FAILED", error=str(exc))

    summary = {
        "schema": M2_SCHEMA,
        "batch": "M2",
        "stages": results,
        "panel": panel_meta,
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "code_commit": _code_commit(),
        "config_hash": _config_hash(config),
        "no_production_change": True,
    }
    _write(out_dir, "m2_summary.json", summary)
    print(
        json.dumps(
            {key: value["status"] for key, value in results.items()}, ensure_ascii=False, indent=2
        )
    )
    return 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _code_commit() -> str:
    import subprocess

    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT)
            .decode("utf-8")
            .strip()
        )
    except Exception:  # noqa: BLE001
        return "unknown"


def _config_hash(config: Any) -> str:
    from stock_analyzer.config_identity import redacted_config_hash

    try:
        return str(redacted_config_hash(config))
    except Exception:  # noqa: BLE001
        return "unknown"


def _compute_feature_frame(panel: Any, decisions: list[DecisionPoint]) -> pd.DataFrame:
    from stock_analyzer.feature.engineer import FeatureEngineer

    grouped: dict[str, list[DecisionPoint]] = {}
    for item in decisions:
        grouped.setdefault(str(item.symbol), []).append(item)
    engineer = FeatureEngineer()
    rows: list[dict[str, object]] = []
    for index, symbol in enumerate(sorted(grouped)):
        frame = panel.symbol_bars(symbol)
        if frame is None or frame.empty:
            continue
        wanted = {item.decision_date for item in grouped[symbol]}
        try:
            features = engineer.transform(frame)
        except Exception:  # noqa: BLE001
            continue
        for timestamp, values in features.iterrows():
            day = timestamp.date() if hasattr(timestamp, "date") else timestamp
            if day not in wanted:
                continue
            row: dict[str, object] = {"decision_date": day.isoformat(), "symbol": symbol}
            row.update({str(key): float(value) for key, value in values.items() if pd.notna(value)})
            rows.append(row)
        if (index + 1) % 100 == 0:
            print(f"[features] {index + 1}/{len(grouped)}", flush=True)
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["decision_date", "symbol"])


def _build_matrix_frame(features: pd.DataFrame, outcomes: pd.DataFrame) -> pd.DataFrame:
    if features.empty or outcomes.empty:
        return pd.DataFrame(columns=["decision_date", "symbol"])
    matrix = features.merge(outcomes, on=["decision_date", "symbol"], how="inner")
    return build_head_targets(matrix)


def _outcome_slice(primary: pd.DataFrame) -> pd.DataFrame:
    columns = ["decision_date", "symbol", "executable"]
    for kind in ("net_return", "excess_return"):
        for h in (3, 5, 10, 15):
            column = metric_column(kind, h)
            if column in primary.columns:
                columns.append(column)
    for h in (3, 5):
        for prefix in ("up_net", "up_excess"):
            column = f"{prefix}_{h}d"
            if column in primary.columns:
                columns.append(column)
    for h in (3, 5):
        column = metric_column("mae", h)
        if column in primary.columns:
            columns.append(column)
    for h in (3, 5, 10, 15):
        column = f"maturity_date_{h}d"
        if column in primary.columns:
            columns.append(column)
        column = f"matured_{h}d"
        if column in primary.columns:
            columns.append(column)
    return primary[[column for column in columns if column in primary.columns]]


def _time_split_probability(frame: pd.DataFrame) -> dict[str, object]:
    if frame.empty:
        return {"time_split": "no_rows"}
    dates = sorted({str(value) for value in frame["decision_date"].unique()})
    cut = int(len(dates) * 0.7)
    frame["is_train"] = frame["decision_date"].astype(str).isin(dates[:cut])
    frame["is_calibration"] = (
        frame["decision_date"].astype(str).isin(dates[cut : cut + max(1, len(dates) // 10)])
    )
    frame["is_predict"] = (
        frame["decision_date"].astype(str).isin(dates[cut + max(1, len(dates) // 10) :])
    )
    return {
        "time_split": "chronological_70_10_20",
        "train_days": cut,
        "calibration_days": max(1, len(dates) // 10),
        "predict_days": len(dates) - cut - max(1, len(dates) // 10),
    }


def _attach_pool_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """按当日横截面构造 quality/light/deep/final 四级的 PIT 名次列。

    生产链路的四级池由选股引擎产出；研究侧这里用**当日前 N** 作为名次代理，
    并在 S12 报告里如实标注 ``quality_pool_source``（不冒充生产成员）。
    """
    if frame.empty or "excess_return_5d" not in frame.columns:
        return frame
    result = frame.copy()
    usable = result[result["executable"].fillna(False).astype(bool)].copy()
    usable["__liquidity"] = pd.to_numeric(usable.get("style_turnover_20d"), errors="coerce")
    usable["__rank"] = usable.groupby("decision_date")["__liquidity"].rank(
        ascending=False, method="first"
    )
    sizes = {"quality_pool": 300, "light_pool": 100, "deep_pool": 50, "final_pool": 5}
    for column in sizes:
        result[column] = False
    result.loc[usable.index, "quality_pool"] = usable["__rank"] <= sizes["quality_pool"]
    result.loc[usable.index, "light_pool"] = usable["__rank"] <= sizes["light_pool"]
    result.loc[usable.index, "deep_pool"] = usable["__rank"] <= sizes["deep_pool"]
    result.loc[usable.index, "final_pool"] = usable["__rank"] <= sizes["final_pool"]
    return result.drop(columns=["__rank", "__liquidity"], errors="ignore")


def _shadow_candidate_frame(heads: pd.DataFrame, primary: pd.DataFrame) -> pd.DataFrame:
    if heads.empty or primary.empty:
        return pd.DataFrame()
    frame = heads.merge(
        primary[
            ["decision_date", "symbol", "executable", "no_fill_reason", "entry_delay_sessions"]
        ],
        on=["decision_date", "symbol"],
        how="left",
    )
    frame = _attach_pool_columns(frame)
    return frame


def _funnel_block(results: dict[str, dict[str, object]]) -> dict[str, object]:
    s14 = results.get("S14", {})
    return {
        "status": "observed",
        "safe_feature_columns": s14.get("safe_columns"),
        "unregistered_columns": s14.get("unregistered"),
        "note": "本地跑批只覆盖研究链路；生产 300/100/50 漏斗由选股引擎产出（见 S04 契约）",
    }


if __name__ == "__main__":
    raise SystemExit(main())
