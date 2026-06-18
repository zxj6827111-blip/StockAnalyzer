"""Audit whether a NAS P0 probe output satisfies the current research objective."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_GRID = {
    "xgb_min": [0.25, 0.3, 0.33],
    "meta_min": [0.45, 0.48, 0.5],
    "max_diff": [0.18, 0.25, 0.3],
    "score_min": [40.0, 45.0, 50.0, 55.0],
}
EXPECTED_FOCUS_SYMBOLS = {"000159", "001258", "600956"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit a p0_run_nas_advisory_probe output directory against the goal.",
    )
    parser.add_argument("probe_output_dir", help="Output directory from p0_run_nas_advisory_probe.")
    parser.add_argument(
        "--output",
        default="",
        help=(
            "Optional JSON output path. Defaults to "
            "<probe_output_dir>/p0_goal_completion_audit.json."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    probe_dir = _path(args.probe_output_dir)
    report = build_goal_completion_audit(probe_dir)
    output = _path(args.output) if args.output else probe_dir / "p0_goal_completion_audit.json"
    _write_json(output, report)
    markdown_output = output.with_suffix(".md")
    markdown_output.write_text(render_markdown_report(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "output": str(output),
                "markdown": str(markdown_output),
            },
            ensure_ascii=False,
        )
    )


def build_goal_completion_audit(probe_dir: Path) -> dict[str, object]:
    analysis_dir = probe_dir / "analysis"
    manifest = _load_json(analysis_dir / "p0_analysis_inputs_manifest.json")
    final_report = _load_json(analysis_dir / "final_report_v3.json")
    feature_report = _load_json(analysis_dir / "p4_feature_family_ablation_v1.json")
    position_report = _load_json(analysis_dir / "p5_position" / "position_framework_analysis.json")
    shadow_plan = _load_json(analysis_dir / "p0_shadow_experiment_plan_v1.json")
    nas_validation = _load_json(probe_dir / "nas_advisory_validation_report.json")
    checks = [
        _check_research_inputs_complete(manifest=manifest, shadow_plan=shadow_plan),
        _check_threshold_shadow(final_report=final_report),
        _check_financial_data_quality(feature_report=feature_report),
        _check_position_framework(position_report=position_report),
        _check_nas_advisory(nas_validation=nas_validation),
    ]
    status = "complete" if all(item["passed"] for item in checks) else "needs_work"
    return {
        "report_type": "p0_goal_completion_audit",
        "probe_output_dir": str(probe_dir),
        "status": status,
        "production_change_allowed": False,
        "checks": checks,
        "next_actions": _next_actions(checks),
    }


def render_markdown_report(report: Mapping[str, object]) -> str:
    checks = [
        item
        for item in _list(report.get("checks"))
        if isinstance(item, Mapping)
    ]
    lines = [
        "# P0 Goal Completion Audit",
        "",
        f"- status: `{report.get('status', 'unknown')}`",
        f"- production_change_allowed: `{report.get('production_change_allowed', False)}`",
        f"- probe_output_dir: `{report.get('probe_output_dir', '')}`",
        "",
        "## Checks",
        "",
    ]
    for item in checks:
        status = "PASS" if item.get("passed") else "FAIL"
        lines.append(f"- `{status}` `{item.get('code', '')}`: {item.get('detail', '')}")
        evidence = _mapping(item.get("evidence"))
        if evidence:
            lines.extend(_markdown_evidence_lines(evidence))
    actions = [str(item) for item in _list(report.get("next_actions"))]
    lines.extend(["", "## Next Actions", ""])
    if actions:
        lines.extend(f"- {item}" for item in actions)
    else:
        lines.append("- None")
    lines.append("")
    return "\n".join(lines)


def _check_research_inputs_complete(
    *,
    manifest: Mapping[str, object],
    shadow_plan: Mapping[str, object],
) -> dict[str, object]:
    missing = list(manifest.get("remaining_expected_inputs", []))
    input_status = str(_mapping(shadow_plan.get("input_completeness")).get("status", "")).strip()
    return {
        "code": "research_inputs_complete",
        "passed": bool(manifest) and missing == [] and input_status == "complete",
        "detail": "final_report_v3, feature_family_ablation and position framework are present",
        "evidence": {
            "remaining_expected_inputs": missing,
            "shadow_plan_status": shadow_plan.get("status"),
            "input_completeness": shadow_plan.get("input_completeness"),
        },
    }


def _check_threshold_shadow(final_report: Mapping[str, object]) -> dict[str, object]:
    sweep = _mapping(final_report.get("threshold_sweep"))
    grid = _mapping(sweep.get("grid"))
    grid_matches = all(
        _as_float_list(grid.get(key)) == values
        for key, values in EXPECTED_GRID.items()
    )
    results = _list(sweep.get("results"))
    diagnostics = _mapping(sweep.get("blocking_diagnostics"))
    outcome_linkage = _mapping(sweep.get("outcome_linkage"))
    has_return_fields = any(
        isinstance(item, Mapping)
        and "observed_trade_count" in item
        and "win_rate" in item
        and "final_equity" in item
        and "max_drawdown" in item
        for item in results
    )
    can_rank_profitability = bool(outcome_linkage.get("can_rank_by_profitability", False))
    return {
        "code": "cross_review_shadow_grid_covered",
        "passed": (
            bool(sweep)
            and grid_matches
            and len(results) == 108
            and bool(diagnostics)
            and has_return_fields
        ),
        "detail": (
            "threshold sweep covers the requested 3x3x3x4 grid, explains blockers, "
            "and carries return/risk fields for outcome-linked variants"
        ),
        "evidence": {
            "grid": grid,
            "result_count": len(results),
            "has_return_fields": has_return_fields,
            "can_rank_by_profitability": can_rank_profitability,
            "blocking_interpretation": diagnostics.get("interpretation"),
            "minimum_candidate_thresholds": sweep.get("minimum_candidate_thresholds"),
            "outcome_linkage": outcome_linkage,
        },
    }


def _check_financial_data_quality(feature_report: Mapping[str, object]) -> dict[str, object]:
    quality = _mapping(feature_report.get("financial_data_quality"))
    classification = _mapping(quality.get("classification"))
    low_roe = _mapping(classification.get("low_roe_evidence"))
    overblock = _mapping(classification.get("short_term_strength_may_be_overblocked"))
    has_evidence_split = all(
        key in low_roe
        for key in (
            "confirmed_true_low_roe_rows",
            "inferred_low_roe_rows",
            "ambiguous_low_roe_rows",
        )
    )
    return {
        "code": "financial_data_quality_split_available",
        "passed": bool(quality) and has_evidence_split and bool(overblock),
        "detail": "financial penalties are split into confirmed/inferred/ambiguous evidence",
        "evidence": {
            "status": quality.get("status"),
            "reason_counts": quality.get("reason_counts"),
            "low_roe_evidence": low_roe,
            "short_term_strength_may_be_overblocked": overblock,
        },
    }


def _check_position_framework(position_report: Mapping[str, object]) -> dict[str, object]:
    loss_path = _mapping(position_report.get("loss_path_analysis"))
    focus = _mapping(position_report.get("focus_symbols"))
    focus_symbols = [
        str(item.get("symbol", "")).strip()
        for item in _list(focus.get("symbols"))
        if isinstance(item, Mapping)
    ]
    focus_symbol_set = set(focus_symbols)
    missing_focus_symbols = sorted(EXPECTED_FOCUS_SYMBOLS - focus_symbol_set)
    has_loss_path_fields = all(
        key in loss_path
        for key in (
            "loss_symbol_count",
            "reentry_after_loss_symbol_count",
            "top_loss_symbols",
        )
    )
    return {
        "code": "position_framework_available",
        "passed": bool(position_report)
        and bool(position_report.get("position_controls"))
        and bool(position_report.get("recommended_shadow"))
        and has_loss_path_fields
        and not missing_focus_symbols,
        "detail": (
            "position sizing, stop/take-profit and re-entry shadow plan is available "
            "with loss-path evidence fields and focus-symbol tracking"
        ),
        "evidence": {
            "status": position_report.get("status"),
            "execution_path_summary": position_report.get("execution_path_summary"),
            "loss_path_analysis": loss_path,
            "has_loss_path_fields": has_loss_path_fields,
            "focus_symbols": focus,
            "focus_symbol_count": len(focus_symbols),
            "missing_focus_symbols": missing_focus_symbols,
            "recommended_shadow": position_report.get("recommended_shadow"),
        },
    }


def _check_nas_advisory(nas_validation: Mapping[str, object]) -> dict[str, object]:
    checks = _list(nas_validation.get("checks"))
    check_map = {
        str(item.get("code")): bool(item.get("passed"))
        for item in checks
        if isinstance(item, Mapping)
    }
    required = {
        "ops_state_confirms_advisory_only",
        "auto_promotion_disabled",
        "risk_guardrails_not_relaxed",
        "runtime_state_latest_signals_persisted",
        "runtime_state_latest_signals_source_is_pipeline_run",
        "signals_latest_uses_latest_not_week5_fallback",
        "latest_pipeline_is_advisory_only",
        "pipeline_has_empty_executions",
        "pipeline_has_advisory_attempt_fields",
        "signal_quality_keeps_advisory_out_of_execution",
    }
    return {
        "code": "nas_advisory_probe_passed",
        "passed": str(nas_validation.get("status", "")).strip() == "pass"
        and all(check_map.get(item) is True for item in required),
        "detail": "NAS runtime evidence proves latest_signals persistence and advisory audit shape",
        "evidence": {
            "status": nas_validation.get("status"),
            "latest_signals": nas_validation.get("latest_signals"),
            "latest_pipeline_run": nas_validation.get("latest_pipeline_run"),
            "check_map": check_map,
        },
    }


def _next_actions(checks: Sequence[Mapping[str, object]]) -> list[str]:
    failed = [str(item.get("code", "")) for item in checks if not item.get("passed")]
    if not failed:
        return [
            (
                "Goal evidence is structurally complete. If can_rank_by_profitability is "
                "false, collect more mature outcomes before changing production thresholds."
            )
        ]
    actions: list[str] = []
    if "research_inputs_complete" in failed:
        actions.append("Rerun p0_run_nas_advisory_probe and inspect analysis manifest.")
    if "cross_review_shadow_grid_covered" in failed:
        actions.append("Inspect final_report_v3.threshold_sweep grid/results/blocking_diagnostics.")
    if "financial_data_quality_split_available" in failed:
        actions.append(
            "Inspect p4_feature_family_ablation_v1 financial_data_quality classification."
        )
    if "position_framework_available" in failed:
        actions.append("Inspect p5_position/position_framework_analysis.json.")
    if "nas_advisory_probe_passed" in failed:
        actions.append(
            "Rerun NAS probe only after advisory_only=true and inspect validation report."
        )
    return actions


def _markdown_evidence_lines(evidence: Mapping[str, object]) -> list[str]:
    keys = (
        "remaining_expected_inputs",
        "shadow_plan_status",
        "result_count",
        "has_return_fields",
        "can_rank_by_profitability",
        "status",
        "has_loss_path_fields",
        "focus_symbol_count",
        "missing_focus_symbols",
    )
    lines: list[str] = []
    for key in keys:
        if key in evidence:
            lines.append(f"  - {key}: `{_compact_value(evidence.get(key))}`")
    check_map = _mapping(evidence.get("check_map"))
    if check_map:
        passed = sum(1 for value in check_map.values() if bool(value))
        lines.append(f"  - nas_validation_checks: `{passed}/{len(check_map)}`")
    outcome = _mapping(evidence.get("outcome_linkage"))
    if outcome:
        lines.append(
            "  - outcome_linkage: "
            f"`symbols_with_returns={outcome.get('symbols_with_returns')}, "
            f"can_rank={outcome.get('can_rank_by_profitability')}`"
        )
    low_roe = _mapping(evidence.get("low_roe_evidence"))
    if low_roe:
        lines.append(
            "  - low_roe_evidence: "
            f"`confirmed={low_roe.get('confirmed_true_low_roe_rows')}, "
            f"inferred={low_roe.get('inferred_low_roe_rows')}, "
            f"ambiguous={low_roe.get('ambiguous_low_roe_rows')}`"
        )
    loss_path = _mapping(evidence.get("loss_path_analysis"))
    if loss_path:
        lines.append(
            "  - loss_path: "
            f"`loss_symbols={loss_path.get('loss_symbol_count')}, "
            f"reentry_after_loss={loss_path.get('reentry_after_loss_symbol_count')}`"
        )
    focus = _mapping(evidence.get("focus_symbols"))
    if focus:
        lines.append(
            "  - focus_symbols: "
            f"`observed={focus.get('observed_count')}, "
            f"loss_observed={focus.get('loss_observed_count')}, "
            f"missing={focus.get('missing_evidence_symbols')}`"
        )
    return lines


def _compact_value(value: object) -> str:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return str(value)
    if isinstance(value, list):
        return f"{len(value)} item(s)"
    if isinstance(value, Mapping):
        return f"{len(value)} key(s)"
    return str(value)


def _load_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _path(raw: str | Path) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _as_float_list(value: object) -> list[float]:
    if not isinstance(value, list):
        return []
    parsed: list[float] = []
    for item in value:
        if isinstance(item, bool):
            continue
        if isinstance(item, (int, float)):
            parsed.append(float(item))
    return parsed


if __name__ == "__main__":
    main()
