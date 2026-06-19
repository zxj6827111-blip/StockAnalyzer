from __future__ import annotations

import json
from pathlib import Path

from scripts.p1_nas_shadow_validation import (
    build_p1_validation_report,
    render_markdown_report,
)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_complete_probe(probe_dir: Path) -> None:
    _write_json(
        probe_dir / "nas_advisory_validation_report.json",
        {
            "status": "pass",
            "safety_config": {
                "auto_promotion_enabled": False,
                "risk_guardrails_status": "pass",
            },
        },
    )
    _write_json(
        probe_dir / "commands" / "pipeline_advisory.json",
        {
            "trace_id": "trace-p1",
            "execution_mode": "advisory_only",
            "portfolio_update": {
                "status": "skipped_advisory_only",
                "executions": [],
            },
        },
    )
    _write_json(
        probe_dir / "commands" / "signals_latest_after.json",
        {
            "source": "pipeline_run",
            "storage_source": "memory",
            "signals": [{"symbol": "600000", "action": "hold"}],
        },
    )
    _write_json(
        probe_dir / "commands" / "signal_quality_after.json",
        {
            "status": "ok",
            "signal_source": "pipeline_run",
            "signal_storage_source": "memory",
            "source_signal_count": 1,
        },
    )
    _write_json(
        probe_dir / "commands" / "config_safety_snapshot.json",
        {"auto_promotion": {"enabled": False}},
    )
    _write_json(
        probe_dir / "analysis" / "final_report_v3.json",
        {
            "production_change_allowed": False,
            "outcome_coverage": {"observed_returns": 8},
            "p1_probability_scale_shadow_grid": {
                "status": "candidate_generating",
                "production_change_allowed": False,
                "candidate_variant_count": 12,
                "max_pass_count": 5,
                "outcome_linkage": {
                    "max_observed_trades_in_variant": 8,
                    "can_rank_by_profitability": False,
                    "can_claim_profitability": False,
                },
                "guardrails": {
                    "do_not_relax_production_cross_review": True,
                },
            },
        },
    )
    _write_json(
        probe_dir / "analysis" / "p4_feature_family_ablation_v1.json",
        {
            "financial_data_quality": {
                "raw_field_coverage": {
                    "status": "raw_fields_observed",
                    "total_rows": 100,
                    "roe_present_rows": 80,
                    "debt_ratio_present_rows": 75,
                    "both_gate_fields_present_rows": 70,
                    "financial_source_present_rows": 90,
                    "financial_report_date_present_rows": 65,
                    "financial_missing_fields_present_rows": 10,
                    "default_or_fallback_source_rows": 8,
                    "same_period_confirmed": "unknown",
                    "same_source_confirmed": "unknown",
                    "semantics": {
                        "financial_data_complete": "gate_required_fields_present_only"
                    },
                }
            }
        },
    )
    _write_json(
        probe_dir / "analysis" / "p5_position" / "position_framework_analysis.json",
        {
            "focus_symbols": {"loss_observed_count": 2},
            "reentry_cooldown_shadow": {
                "status": "shadow_design_only",
                "production_change_allowed": False,
                "focus_symbols": ["000159", "001258", "600956"],
                "loss_symbols": ["000159"],
                "reentry_hint_symbols": ["000159"],
                "stop_loss_symbols": ["000159"],
                "variants": [
                    {"name": "stop_loss_reentry_cooldown_3d"},
                    {"name": "take_profit_trailing_remainder_shadow"},
                ],
                "guardrails": {"do_not_write_week6_controls": True},
            },
        },
    )
    _write_json(
        probe_dir / "analysis" / "p0_shadow_experiment_plan_v1.json",
        {
            "status": "research_only",
            "production_change_allowed": False,
            "threshold_assessment": {
                "p1_probability_scale_shadow_grid": {"candidate_variant_count": 12}
            },
            "feature_family_plan": {"financial_raw_field_coverage": {"status": "ok"}},
            "position_plan": {
                "reentry_cooldown_shadow": {"status": "shadow_design_only"}
            },
        },
    )


def test_p1_nas_shadow_validation_passes_complete_research_artifacts(tmp_path: Path) -> None:
    probe_dir = tmp_path / "probe"
    _write_complete_probe(probe_dir)

    report = build_p1_validation_report(probe_dir=probe_dir)

    assert report["status"] == "pass"
    checks = {str(item["code"]): bool(item["passed"]) for item in report["checks"]}
    assert all(checks.values())
    assert report["p1_probability_scale_shadow_grid"]["candidate_variant_count"] == 12
    assert report["financial_raw_field_coverage"]["same_period_confirmed"] == "unknown"
    assert report["reentry_cooldown_shadow"]["focus_symbols"] == [
        "000159",
        "001258",
        "600956",
    ]
    assert report["maturity"]["can_claim_profitability"] is False
    markdown = render_markdown_report(report)
    assert "P1 NAS Shadow Validation Report" in markdown
    assert "PASS: p1_shadow_grid_generates_candidates" in markdown
    assert "Do not change production thresholds before 100 mature samples." in markdown


def test_p1_nas_shadow_validation_flags_missing_or_unsafe_artifacts(tmp_path: Path) -> None:
    probe_dir = tmp_path / "probe"
    _write_complete_probe(probe_dir)
    _write_json(
        probe_dir / "commands" / "pipeline_advisory.json",
        {
            "execution_mode": "portfolio_auto_apply",
            "portfolio_update": {
                "status": "simulated_auto_applied",
                "executions": [{"symbol": "600000"}],
            },
        },
    )
    _write_json(
        probe_dir / "analysis" / "final_report_v3.json",
        {"p1_probability_scale_shadow_grid": {"candidate_variant_count": 0}},
    )

    report = build_p1_validation_report(probe_dir=probe_dir)

    assert report["status"] == "needs_review"
    checks = {str(item["code"]): bool(item["passed"]) for item in report["checks"]}
    assert checks["no_real_orders"] is False
    assert checks["latest_pipeline_advisory_only"] is False
    assert checks["p1_shadow_grid_generates_candidates"] is False
