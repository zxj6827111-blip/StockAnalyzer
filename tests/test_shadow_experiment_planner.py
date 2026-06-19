from __future__ import annotations

import json
from pathlib import Path

from stock_analyzer.research.shadow_experiment_planner import build_shadow_experiment_plan


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_shadow_experiment_planner_recommends_p0_research_without_production_change(
    tmp_path: Path,
) -> None:
    analysis_dir = tmp_path / "analysis"
    _write_json(
        analysis_dir / "final_report_v3.json",
        {
            "threshold_sweep": {"status": "not_effective"},
            "multisymbol_multiwindow": {
                "summaries": {
                    "baseline": {
                        "avg_final_equity": 0.6472,
                        "median_final_equity": 0.7669,
                        "avg_max_drawdown": 0.3936,
                        "total_trades": 658,
                        "overall_win_rate": 0.2629,
                        "losing_ratio": 0.7333,
                    }
                }
            },
        },
    )
    _write_json(
        analysis_dir / "model_diagnosis_final.json",
        {"label_distribution": {"test": {"samples": 59, "positive": 1, "positive_rate": 0.0169}}},
    )
    _write_json(
        analysis_dir / "p4_cross_review_failure_analysis_v1.json",
        {
            "cross_review_analysis": {
                "gate_statistics": {
                    "total_evaluated_rows": 540,
                    "total_decision_threshold_pass": 25,
                    "total_cross_review_pass": 17,
                    "total_incremental_cross_review_rejection": 8,
                }
            }
        },
    )
    _write_json(
        analysis_dir / "p4_feature_family_ablation_v1.json",
        {
            "ablation_results": [
                {
                    "experiment_name": "drop_volatility",
                    "metrics": {"final_equity": 1.117025, "avg_auc": 0.515732, "total_trades": 18},
                    "impact": {
                        "final_equity": {"change_pct": 34.73},
                        "avg_auc": {"change_pct": 10.86},
                    },
                },
                {
                    "experiment_name": "drop_market_relative",
                    "metrics": {"final_equity": 0.655088, "avg_auc": 0.42, "total_trades": 20},
                    "impact": {
                        "final_equity": {"change_pct": -20.98},
                        "avg_auc": {"change_pct": -9.0},
                    },
                },
            ],
            "financial_data_quality": {
                "raw_field_coverage": {
                    "status": "raw_fields_observed",
                    "total_rows": 100,
                    "roe_present_rows": 80,
                    "debt_ratio_present_rows": 75,
                    "both_gate_fields_present_rows": 70,
                    "default_or_fallback_source_rows": 12,
                    "same_period_confirmed": "unknown",
                    "same_source_confirmed": "unknown",
                    "semantics": {
                        "financial_data_complete": "gate_required_fields_present_only"
                    },
                }
            },
        },
    )
    _write_json(
        analysis_dir / "p5_position" / "position_framework_analysis.json",
        {"recommended_shadow": ["cash_floor_10pct"]},
    )

    plan = build_shadow_experiment_plan(analysis_dir=analysis_dir)

    assert plan["status"] == "research_only"
    assert plan["production_change_allowed"] is False
    assert plan["input_completeness"]["status"] == "complete"
    assert plan["label_health"]["status"] == "needs_label_split_repair"
    assert plan["threshold_assessment"]["status"] == "do_not_prioritize_threshold_tuning"
    feature_plan = plan["feature_family_plan"]
    assert feature_plan["drop_shadow_candidates"][0]["family"] == "volatility"
    assert feature_plan["keep_shadow_candidates"][0]["family"] == "market_relative"
    raw = feature_plan["financial_raw_field_coverage"]
    assert raw["roe_present_rows"] == 80
    assert raw["same_period_confirmed"] == "unknown"
    experiment_names = {item["name"] for item in plan["recommended_experiments"]}
    assert "signal_loss_funnel_nas_replay" in experiment_names
    assert "feature_family_multisymbol_ablation_shadow" in experiment_names


def test_shadow_experiment_planner_marks_missing_inputs_before_feature_ranking(
    tmp_path: Path,
) -> None:
    plan = build_shadow_experiment_plan(analysis_dir=tmp_path / "empty-analysis")

    assert plan["status"] == "research_inputs_missing"
    assert plan["input_completeness"]["status"] == "partial"
    assert plan["baseline"]["status"] == "missing_input"
    assert plan["label_health"]["status"] == "missing_input"
    assert plan["threshold_assessment"]["status"] == "missing_threshold_sweep_input"
    assert plan["feature_family_plan"]["status"] == "missing_input"
    experiment_names = [item["name"] for item in plan["recommended_experiments"]]
    assert experiment_names[0] == "analysis_baseline_rebuild"
    assert "feature_family_multisymbol_ablation_shadow" not in experiment_names
    runtime_replay = next(
        item
        for item in plan["recommended_experiments"]
        if item["name"] == "signal_loss_funnel_nas_replay"
    )
    assert runtime_replay["status"] == "requires_runtime_artifacts"


def test_shadow_experiment_planner_does_not_infer_threshold_result_from_partial_inputs(
    tmp_path: Path,
) -> None:
    analysis_dir = tmp_path / "analysis"
    _write_json(
        analysis_dir / "p4_cross_review_failure_analysis_v1.json",
        {
            "cross_review_analysis": {
                "gate_statistics": {
                    "total_evaluated_rows": 540,
                    "total_decision_threshold_pass": 25,
                    "total_cross_review_pass": 17,
                }
            }
        },
    )

    plan = build_shadow_experiment_plan(analysis_dir=analysis_dir)

    threshold = plan["threshold_assessment"]
    assert threshold["status"] == "missing_threshold_sweep_input"
    assert threshold["threshold_sweep_effective"] is None
    assert threshold["available_cross_review_rows"] == 540


def test_shadow_experiment_planner_requires_source_review_for_mixed_runtime_inputs(
    tmp_path: Path,
) -> None:
    analysis_dir = tmp_path / "analysis"
    _write_json(
        analysis_dir / "final_report_v3.json",
        {
            "source_scope": {
                "row_count": 100,
                "fallback_signal_rows": 100,
                "dry_run_events": 1,
                "requires_runtime_source_review": True,
                "is_production_pure": False,
            },
            "threshold_sweep": {
                "status": "candidate_generating",
                "top_candidate_generating_variants": [
                    {
                        "xgb_min": 0.25,
                        "meta_min": 0.45,
                        "max_diff": 0.3,
                        "score_min": 40.0,
                        "pass_count": 12,
                    }
                ],
            },
            "p1_probability_scale_shadow_grid": {
                "status": "candidate_generating",
                "production_change_allowed": False,
                "candidate_variant_count": 7,
                "max_pass_count": 12,
                "outcome_linkage": {
                    "max_observed_trades_in_variant": 8,
                    "can_rank_by_profitability": False,
                    "can_claim_profitability": False,
                },
                "guardrails": {
                    "do_not_relax_production_cross_review": True,
                },
            },
            "multisymbol_multiwindow": {
                "summaries": {
                    "baseline": {
                        "avg_final_equity": None,
                        "median_final_equity": None,
                        "avg_max_drawdown": None,
                        "total_trades": 0,
                        "overall_win_rate": None,
                        "losing_ratio": None,
                    }
                }
            },
        },
    )
    _write_json(
        analysis_dir / "model_diagnosis_final.json",
        {"label_distribution": {"test": {"samples": 100, "positive": 13, "positive_rate": 0.13}}},
    )
    _write_json(
        analysis_dir / "p4_cross_review_failure_analysis_v1.json",
        {
            "cross_review_analysis": {
                "gate_statistics": {
                    "total_evaluated_rows": 100,
                    "total_decision_threshold_pass": 0,
                    "total_cross_review_pass": 0,
                    "total_incremental_cross_review_rejection": 0,
                }
            }
        },
    )
    _write_json(analysis_dir / "p4_feature_family_ablation_v1.json", {"ablation_results": []})
    _write_json(
        analysis_dir / "p5_position" / "position_framework_analysis.json",
        {
            "recommended_shadow": ["stop_loss_cooldown_reentry_shadow"],
            "reentry_cooldown_shadow": {
                "status": "shadow_design_only",
                "production_change_allowed": False,
                "guardrails": {"do_not_write_week6_controls": True},
            },
        },
    )

    plan = build_shadow_experiment_plan(analysis_dir=analysis_dir)

    assert plan["status"] == "research_only"
    assert plan["threshold_assessment"]["status"] == "needs_runtime_source_review"
    assert plan["threshold_assessment"]["source_scope"]["fallback_signal_rows"] == 100
    p1 = plan["threshold_assessment"]["p1_probability_scale_shadow_grid"]
    assert p1["candidate_variant_count"] == 7
    assert p1["guardrails"]["do_not_relax_production_cross_review"] is True
    assert p1["can_claim_profitability"] is False
    assert plan["source_scope"]["dry_run_events"] == 1
    assert plan["production_change_allowed"] is False
    assert plan["position_plan"]["reentry_cooldown_shadow"]["production_change_allowed"] is False
