"""Build a read-only plan for the next shadow experiments."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path


def build_shadow_experiment_plan(
    *,
    analysis_dir: Path,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    final_report = _load_json(analysis_dir / "final_report_v3.json")
    model_diagnosis = _load_json(analysis_dir / "model_diagnosis_final.json")
    cross_review = _load_json(analysis_dir / "p4_cross_review_failure_analysis_v1.json")
    feature_ablation = _load_json(analysis_dir / "p4_feature_family_ablation_v1.json")
    p5_position = _load_json(analysis_dir / "p5_position" / "position_framework_analysis.json")
    input_completeness = _input_completeness(analysis_dir)

    baseline = _baseline_summary(final_report)
    label_health = _label_health(model_diagnosis)
    source_scope = _combined_source_scope(final_report, cross_review)
    threshold = _threshold_assessment(final_report, cross_review, source_scope)
    feature_plan = _feature_family_plan(feature_ablation)
    position_plan = _position_plan(p5_position)
    experiments = _recommended_experiments(
        baseline=baseline,
        label_health=label_health,
        threshold=threshold,
        feature_plan=feature_plan,
        position_plan=position_plan,
    )
    return {
        "generated_at": (generated_at or datetime.now()).isoformat(),
        "report_type": "p0_shadow_experiment_plan_v1",
        "status": (
            "research_only"
            if input_completeness.get("status") == "complete"
            else "research_inputs_missing"
        ),
        "production_change_allowed": False,
        "input_completeness": input_completeness,
        "source_scope": source_scope,
        "baseline": baseline,
        "label_health": label_health,
        "threshold_assessment": threshold,
        "feature_family_plan": feature_plan,
        "position_plan": position_plan,
        "recommended_experiments": experiments,
        "promotion_gates": {
            "avg_final_equity_gt_baseline": True,
            "avg_max_drawdown_lt": 0.20,
            "total_trades_gte": 100,
            "avg_auc_gte": 0.50,
            "shadow_min_trading_days": 20,
            "auto_promotion_must_remain_disabled": True,
        },
        "nas_validation_focus": [
            "rerun signal_loss_funnel with recent 2-4 week audit events",
            "compare pre_trade_blocked and risk_gate_blocked by symbol and reason",
            "check simulated broker/account alignment before judging execution quality",
            "confirm mature outcome availability before using profitable_observed",
        ],
    }


def _baseline_summary(report: Mapping[str, object]) -> dict[str, object]:
    summaries = _mapping(_mapping(report.get("multisymbol_multiwindow")).get("summaries"))
    baseline = _mapping(summaries.get("baseline"))
    metrics_available = bool(baseline)
    return {
        "source": "final_report_v3.multisymbol_multiwindow.summaries.baseline",
        "status": "available" if metrics_available else "missing_input",
        "avg_final_equity": _float(baseline.get("avg_final_equity")),
        "median_final_equity": _float(baseline.get("median_final_equity")),
        "avg_max_drawdown": _float(baseline.get("avg_max_drawdown")),
        "total_trades": _int(baseline.get("total_trades")),
        "overall_win_rate": _float(baseline.get("overall_win_rate")),
        "losing_ratio": _float(baseline.get("losing_ratio")),
        "production_candidate": _is_production_candidate(baseline),
    }


def _label_health(report: Mapping[str, object]) -> dict[str, object]:
    distribution = _mapping(report.get("label_distribution"))
    test = _mapping(distribution.get("test"))
    test_positive_rate = _float(test.get("positive_rate"))
    test_positive = _int(test.get("positive"))
    if not test:
        return {
            "source": "model_diagnosis_final.label_distribution",
            "test_positive_rate": None,
            "test_positive": 0,
            "status": "missing_input",
            "reason": "model diagnosis artifact is missing or lacks test label distribution",
        }
    return {
        "source": "model_diagnosis_final.label_distribution",
        "test_positive_rate": test_positive_rate,
        "test_positive": test_positive,
        "status": (
            "needs_label_split_repair"
            if test_positive_rate is None or test_positive_rate < 0.03 or test_positive < 5
            else "usable"
        ),
        "reason": "test positive samples are too sparse for stable validation"
        if test_positive_rate is None or test_positive_rate < 0.03 or test_positive < 5
        else "test positives look sufficient",
    }


def _threshold_assessment(
    final_report: Mapping[str, object],
    cross_review_report: Mapping[str, object],
    source_scope: Mapping[str, object],
) -> dict[str, object]:
    threshold_sweep = _mapping(final_report.get("threshold_sweep"))
    cross_review = _mapping(cross_review_report.get("cross_review_analysis"))
    gate_stats = _mapping(cross_review.get("gate_statistics"))
    if not threshold_sweep:
        return {
            "source": "final_report_v3.threshold_sweep + p4_cross_review_failure_analysis_v1",
            "status": "missing_threshold_sweep_input",
            "threshold_sweep_effective": None,
            "total_evaluated_rows": 0,
            "decision_threshold_pass": 0,
            "cross_review_pass": 0,
            "incremental_cross_review_rejection": 0,
            "available_cross_review_rows": _int(gate_stats.get("total_evaluated_rows")),
            "source_scope": dict(source_scope),
        }
    threshold_status = str(threshold_sweep.get("status", "")).strip()
    needs_source_review = bool(source_scope.get("requires_runtime_source_review", False))
    if needs_source_review:
        status = "needs_runtime_source_review"
    elif threshold_status == "not_effective":
        status = "do_not_prioritize_threshold_tuning"
    else:
        status = "needs_review"
    return {
        "source": "final_report_v3.threshold_sweep + p4_cross_review_failure_analysis_v1",
        "status": status,
        "threshold_sweep_effective": threshold_status != "not_effective",
        "p1_probability_scale_shadow_grid": _p1_probability_scale_summary(
            final_report,
        ),
        "total_evaluated_rows": _int(gate_stats.get("total_evaluated_rows")),
        "decision_threshold_pass": _int(gate_stats.get("total_decision_threshold_pass")),
        "cross_review_pass": _int(gate_stats.get("total_cross_review_pass")),
        "incremental_cross_review_rejection": _int(
            gate_stats.get("total_incremental_cross_review_rejection")
        ),
        "source_scope": dict(source_scope),
        "top_candidate_generating_variants": _list(
            threshold_sweep.get("top_candidate_generating_variants")
        )[:10],
    }


def _p1_probability_scale_summary(final_report: Mapping[str, object]) -> dict[str, object]:
    grid = _mapping(final_report.get("p1_probability_scale_shadow_grid"))
    if not grid:
        return {
            "status": "missing_input",
            "production_change_allowed": False,
            "candidate_variant_count": 0,
            "can_rank_by_profitability": False,
        }
    outcome = _mapping(grid.get("outcome_linkage"))
    return {
        "status": str(grid.get("status", "")).strip() or "unknown",
        "production_change_allowed": bool(grid.get("production_change_allowed", False)),
        "candidate_variant_count": _int(grid.get("candidate_variant_count")),
        "max_pass_count": _int(grid.get("max_pass_count")),
        "max_observed_trades_in_variant": _int(
            outcome.get("max_observed_trades_in_variant")
        ),
        "can_rank_by_profitability": bool(outcome.get("can_rank_by_profitability", False)),
        "can_claim_profitability": bool(outcome.get("can_claim_profitability", False)),
        "guardrails": _mapping(grid.get("guardrails")),
    }


def _feature_family_plan(report: Mapping[str, object]) -> dict[str, object]:
    results = _list(report.get("ablation_results"))
    financial_quality = _mapping(report.get("financial_data_quality"))
    financial_raw = _mapping(financial_quality.get("raw_field_coverage"))
    if not results:
        return {
            "source": "p4_feature_family_ablation_v1",
            "status": "missing_input",
            "drop_shadow_candidates": [],
            "keep_shadow_candidates": [],
            "inconclusive_candidates": [],
            "financial_raw_field_coverage": _financial_raw_field_summary(financial_raw),
            "note": "feature ablation artifact is missing; run multi-symbol shadow first",
        }
    drop_candidates: list[dict[str, object]] = []
    keep_candidates: list[dict[str, object]] = []
    weak_candidates: list[dict[str, object]] = []
    for item in results:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("experiment_name", "")).replace("drop_", "")
        metrics = _mapping(item.get("metrics"))
        impact = _mapping(item.get("impact"))
        equity_change = _impact_change(impact, "final_equity")
        auc_change = _impact_change(impact, "avg_auc")
        trade_count = _float(metrics.get("total_trades")) or 0.0
        record = {
            "family": name,
            "experiment_name": str(item.get("experiment_name", "")),
            "final_equity": _float(metrics.get("final_equity")),
            "avg_auc": _float(metrics.get("avg_auc")),
            "total_trades": trade_count,
            "equity_change_pct": equity_change,
            "auc_change_pct": auc_change,
            "sample_scope": "single_symbol",
        }
        if equity_change is not None and equity_change <= -10:
            keep_candidates.append(record)
        elif equity_change is not None and equity_change >= 10 and trade_count >= 10:
            drop_candidates.append(record)
        else:
            weak_candidates.append(record)
    return {
        "source": "p4_feature_family_ablation_v1",
        "status": "needs_multisymbol_confirmation",
        "drop_shadow_candidates": sorted(
            drop_candidates,
            key=lambda item: float(item.get("equity_change_pct") or 0.0),
            reverse=True,
        )[:8],
        "keep_shadow_candidates": sorted(
            keep_candidates,
            key=lambda item: float(item.get("equity_change_pct") or 0.0),
        )[:8],
        "inconclusive_candidates": weak_candidates[:8],
        "financial_raw_field_coverage": _financial_raw_field_summary(financial_raw),
        "note": "feature ablation evidence is single-symbol until rerun on multi-symbol windows",
    }


def _financial_raw_field_summary(raw: Mapping[str, object]) -> dict[str, object]:
    if not raw:
        return {
            "status": "missing_input",
            "same_period_confirmed": "unknown",
            "same_source_confirmed": "unknown",
        }
    return {
        "status": str(raw.get("status", "")).strip() or "unknown",
        "total_rows": _int(raw.get("total_rows")),
        "roe_present_rows": _int(raw.get("roe_present_rows")),
        "debt_ratio_present_rows": _int(raw.get("debt_ratio_present_rows")),
        "both_gate_fields_present_rows": _int(raw.get("both_gate_fields_present_rows")),
        "default_or_fallback_source_rows": _int(raw.get("default_or_fallback_source_rows")),
        "same_period_confirmed": str(raw.get("same_period_confirmed", "unknown")),
        "same_source_confirmed": str(raw.get("same_source_confirmed", "unknown")),
        "semantics": _mapping(raw.get("semantics")),
    }


def _position_plan(report: Mapping[str, object]) -> dict[str, object]:
    if not report:
        return {
            "source": "p5_position/position_framework_analysis.json",
            "status": "missing_input",
            "recommended_shadow": [
                "position_sizing_sensitivity",
                "take_profit_trailing_ab_test",
            ],
        }
    return {
        "source": "p5_position/position_framework_analysis.json",
        "status": "needs_execution_ab",
        "summary_keys": sorted(str(key) for key in report.keys())[:20],
        "reentry_cooldown_shadow": _mapping(report.get("reentry_cooldown_shadow")),
        "recommended_shadow": [
            "position_sizing_sensitivity",
            "stop_loss_cooldown_reentry_shadow",
            "take_profit_trailing_ab_test",
            "cash_fragmentation_sensitivity",
        ],
    }


def _recommended_experiments(
    *,
    baseline: Mapping[str, object],
    label_health: Mapping[str, object],
    threshold: Mapping[str, object],
    feature_plan: Mapping[str, object],
    position_plan: Mapping[str, object],
) -> list[dict[str, object]]:
    experiments: list[dict[str, object]] = []
    if baseline.get("status") == "missing_input":
        experiments.append(
            {
                "priority": "P0",
                "name": "analysis_baseline_rebuild",
                "change_type": "research_artifact",
                "goal": "rebuild multi-symbol multi-window baseline before ranking alpha changes",
                "inputs": ["walk_forward", "multisymbol universe", "current config/default.yaml"],
                "acceptance": {
                    "has_avg_final_equity": True,
                    "has_total_trades": True,
                    "has_drawdown": True,
                },
            }
        )
    if label_health.get("status") in {"needs_label_split_repair", "missing_input"}:
        experiments.append(
            {
                "priority": "P0",
                "name": "label_split_trainability_shadow",
                "change_type": "label_and_validation",
                "goal": (
                    "ensure each fold has enough mature positive samples "
                    "before tuning thresholds"
                ),
                "inputs": ["model_diagnosis_final", "walk_forward fold label distribution"],
                "acceptance": {
                    "min_test_positive_per_fold": 5,
                    "min_test_positive_rate": 0.03,
                },
            }
        )
    experiments.append(
        {
            "priority": "P0",
            "name": "signal_loss_funnel_nas_replay",
            "change_type": "runtime_audit",
            "status": "requires_runtime_artifacts",
            "goal": "attribute recent live signal loss before changing production rules",
            "inputs": ["latest_signals", "audit_events", "week5_report"],
            "acceptance": {
                "has_signal_stages": True,
                "has_execution_stages": True,
                "no_production_trade_change": True,
            },
        }
    )
    if threshold.get("status") in {
        "do_not_prioritize_threshold_tuning",
        "needs_runtime_source_review",
        "needs_review",
    }:
        experiments.append(
            {
                "priority": "P1",
                "name": "cross_review_calibration_shadow",
                "change_type": "gate_calibration",
                "goal": (
                    "calibrate cross-review against the observed probability scale in "
                    "research artifacts only"
                ),
                "inputs": [
                    "cross_review gap",
                    "near_misses",
                    "final_report_v3.p1_probability_scale_shadow_grid",
                ],
                "guardrail": "do not relax production cross-review directly",
                "acceptance": {
                    "candidate_variant_count_gt": 0,
                    "mature_return_samples_gte": 50,
                    "production_change_allowed": False,
                },
            }
        )
    feature_candidates = _list(feature_plan.get("drop_shadow_candidates"))[:5]
    if feature_candidates and baseline.get("status") != "missing_input":
        experiments.append(
            {
                "priority": "P0",
                "name": "feature_family_multisymbol_ablation_shadow",
                "change_type": "feature_selection",
                "goal": "confirm whether noisy families should be removed or downweighted",
                "families": [str(item.get("family", "")) for item in feature_candidates],
                "acceptance": {
                    "avg_final_equity_gt": baseline.get("avg_final_equity"),
                    "avg_max_drawdown_lt": 0.20,
                    "total_trades_gte": 100,
                },
            }
        )
    experiments.append(
        {
            "priority": "P1",
            "name": "execution_position_management_shadow",
            "change_type": "execution_and_position",
            "status": "requires_runtime_artifacts",
            "goal": (
                "measure whether exits, sizing, and cash constraints are "
                "cutting winners or blocking buys"
            ),
            "variants": _list(position_plan.get("recommended_shadow")),
            "guardrail": "keep production risk gates unchanged during shadow",
        }
    )
    return experiments


def _load_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(data) if isinstance(data, Mapping) else {}


def _combined_source_scope(
    final_report: Mapping[str, object],
    cross_review_report: Mapping[str, object],
) -> dict[str, object]:
    final_scope = _mapping(final_report.get("source_scope"))
    cross_review_scope = _mapping(cross_review_report.get("source_scope"))
    if final_scope:
        return dict(final_scope)
    if cross_review_scope:
        return dict(cross_review_scope)
    return {
        "row_count": 0,
        "audit_event_count": 0,
        "is_production_pure": False,
        "requires_runtime_source_review": False,
    }


def _input_completeness(analysis_dir: Path) -> dict[str, object]:
    expected = {
        "final_report_v3": analysis_dir / "final_report_v3.json",
        "model_diagnosis_final": analysis_dir / "model_diagnosis_final.json",
        "cross_review_failure": analysis_dir / "p4_cross_review_failure_analysis_v1.json",
        "feature_family_ablation": analysis_dir / "p4_feature_family_ablation_v1.json",
        "position_framework": analysis_dir / "p5_position" / "position_framework_analysis.json",
    }
    artifacts = {
        name: {
            "path": str(path),
            "present": path.exists(),
        }
        for name, path in expected.items()
    }
    present = sum(1 for item in artifacts.values() if item["present"])
    return {
        "status": "complete" if present == len(expected) else "partial",
        "present": present,
        "expected": len(expected),
        "artifacts": artifacts,
    }


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _int(value: object) -> int:
    parsed = _float(value)
    return int(parsed) if parsed is not None else 0


def _impact_change(impact: Mapping[str, object], metric: str) -> float | None:
    return _float(_mapping(impact.get(metric)).get("change_pct"))


def _is_production_candidate(summary: Mapping[str, object]) -> bool:
    final_equity = _float(summary.get("avg_final_equity")) or 0.0
    drawdown = _float(summary.get("avg_max_drawdown")) or 1.0
    win_rate = _float(summary.get("overall_win_rate")) or 0.0
    trades = _int(summary.get("total_trades"))
    return final_equity > 1.0 and drawdown < 0.20 and win_rate > 0.50 and trades >= 100
