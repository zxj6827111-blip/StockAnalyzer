from __future__ import annotations

import json
from pathlib import Path

from scripts.p0_goal_completion_audit import build_goal_completion_audit


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_goal_completion_audit_passes_complete_probe_output(tmp_path: Path) -> None:
    probe = tmp_path / "probe"
    analysis = probe / "analysis"
    _write_json(
        analysis / "p0_analysis_inputs_manifest.json",
        {"remaining_expected_inputs": []},
    )
    _write_json(
        analysis / "p0_shadow_experiment_plan_v1.json",
        {
            "status": "research_only",
            "input_completeness": {"status": "complete", "present": 5, "expected": 5},
        },
    )
    _write_json(
        analysis / "final_report_v3.json",
        {
            "threshold_sweep": {
                "grid": {
                    "xgb_min": [0.25, 0.3, 0.33],
                    "meta_min": [0.45, 0.48, 0.5],
                    "max_diff": [0.18, 0.25, 0.3],
                    "score_min": [40.0, 45.0, 50.0, 55.0],
                },
                "results": [
                    {
                        "pass_count": 1,
                        "observed_trade_count": 1,
                        "win_rate": 1.0,
                        "final_equity": 1.02,
                        "max_drawdown": 0.0,
                    }
                ]
                * 108,
                "blocking_diagnostics": {"interpretation": "probability scale"},
                "minimum_candidate_thresholds": {"status": "suggested_from_complete_rows"},
                "outcome_linkage": {"can_rank_by_profitability": False},
            }
        },
    )
    _write_json(
        analysis / "p4_feature_family_ablation_v1.json",
        {
            "financial_data_quality": {
                "status": "needs_data_quality_review",
                "reason_counts": {"low_roe_penalty": 2},
                "classification": {
                    "low_roe_evidence": {
                        "confirmed_true_low_roe_rows": 1,
                        "inferred_low_roe_rows": 1,
                        "ambiguous_low_roe_rows": 0,
                    },
                    "short_term_strength_may_be_overblocked": {
                        "rows": 1,
                        "data_quality_issue_rows": 0,
                    },
                },
            }
        },
    )
    _write_json(
        analysis / "p5_position" / "position_framework_analysis.json",
        {
            "status": "runtime_artifact_replay",
            "position_controls": {"soup_strategy": {}},
            "execution_path_summary": {"records": 1},
            "loss_path_analysis": {
                "loss_symbol_count": 0,
                "reentry_after_loss_symbol_count": 0,
                "top_loss_symbols": [],
            },
            "recommended_shadow": ["position_sizing_sensitivity"],
        },
    )
    _write_json(
        probe / "nas_advisory_validation_report.json",
        {
            "status": "pass",
            "latest_signals": {"source": "pipeline_run"},
            "latest_pipeline_run": {"execution_mode": "advisory_only"},
            "checks": [
                {"code": "runtime_state_latest_signals_persisted", "passed": True},
                {
                    "code": "runtime_state_latest_signals_source_is_pipeline_run",
                    "passed": True,
                },
                {"code": "signals_latest_uses_latest_not_week5_fallback", "passed": True},
                {"code": "latest_pipeline_is_advisory_only", "passed": True},
                {"code": "pipeline_has_empty_executions", "passed": True},
                {"code": "pipeline_has_advisory_attempt_fields", "passed": True},
                {"code": "signal_quality_keeps_advisory_out_of_execution", "passed": True},
            ],
        },
    )

    report = build_goal_completion_audit(probe)

    assert report["status"] == "complete"
    assert all(item["passed"] for item in report["checks"])


def test_goal_completion_audit_flags_missing_validation_and_inputs(tmp_path: Path) -> None:
    probe = tmp_path / "probe"
    analysis = probe / "analysis"
    _write_json(
        analysis / "p0_analysis_inputs_manifest.json",
        {"remaining_expected_inputs": ["final_report_v3.json"]},
    )
    _write_json(
        analysis / "p0_shadow_experiment_plan_v1.json",
        {"status": "research_inputs_missing", "input_completeness": {"status": "partial"}},
    )

    report = build_goal_completion_audit(probe)
    check_map = {str(item["code"]): bool(item["passed"]) for item in report["checks"]}

    assert report["status"] == "needs_work"
    assert check_map["research_inputs_complete"] is False
    assert check_map["nas_advisory_probe_passed"] is False
    assert report["next_actions"]


def test_goal_completion_audit_requires_return_and_loss_path_fields(tmp_path: Path) -> None:
    probe = tmp_path / "probe"
    analysis = probe / "analysis"
    _write_json(
        analysis / "p0_analysis_inputs_manifest.json",
        {"remaining_expected_inputs": []},
    )
    _write_json(
        analysis / "p0_shadow_experiment_plan_v1.json",
        {"status": "research_only", "input_completeness": {"status": "complete"}},
    )
    _write_json(
        analysis / "final_report_v3.json",
        {
            "threshold_sweep": {
                "grid": {
                    "xgb_min": [0.25, 0.3, 0.33],
                    "meta_min": [0.45, 0.48, 0.5],
                    "max_diff": [0.18, 0.25, 0.3],
                    "score_min": [40.0, 45.0, 50.0, 55.0],
                },
                "results": [{"pass_count": 0}] * 108,
                "blocking_diagnostics": {"interpretation": "probability scale"},
            }
        },
    )
    _write_json(
        analysis / "p4_feature_family_ablation_v1.json",
        {
            "financial_data_quality": {
                "classification": {
                    "low_roe_evidence": {
                        "confirmed_true_low_roe_rows": 0,
                        "inferred_low_roe_rows": 0,
                        "ambiguous_low_roe_rows": 0,
                    },
                    "short_term_strength_may_be_overblocked": {"rows": 0},
                }
            }
        },
    )
    _write_json(
        analysis / "p5_position" / "position_framework_analysis.json",
        {
            "position_controls": {"soup_strategy": {}},
            "recommended_shadow": ["position_sizing_sensitivity"],
            "loss_path_analysis": {"loss_symbol_count": 0},
        },
    )

    report = build_goal_completion_audit(probe)
    check_map = {str(item["code"]): item for item in report["checks"]}

    assert report["status"] == "needs_work"
    assert check_map["cross_review_shadow_grid_covered"]["passed"] is False
    assert check_map["position_framework_available"]["passed"] is False
    assert (
        check_map["cross_review_shadow_grid_covered"]["evidence"]["has_return_fields"]
        is False
    )
    assert check_map["position_framework_available"]["evidence"]["has_loss_path_fields"] is False
