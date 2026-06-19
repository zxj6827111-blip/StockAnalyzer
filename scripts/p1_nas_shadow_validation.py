"""Validate P1 shadow calibration artifacts after a NAS advisory probe."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a P1 NAS shadow validation report from generated artifacts.",
    )
    parser.add_argument(
        "--probe-dir",
        default="artifacts/research/p1_shadow_calibration_nas",
        help="Probe output directory containing commands/ and analysis/ artifacts.",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Directory for nas_validation_report.md/json. Defaults to --probe-dir.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    probe_dir = _path(args.probe_dir)
    output_dir = _path(args.output_dir) if args.output_dir else probe_dir
    report = build_p1_validation_report(probe_dir=probe_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "nas_validation_report.json"
    md_path = output_dir / "nas_validation_report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown_report(report), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "markdown": str(md_path)}, ensure_ascii=False))


def build_p1_validation_report(*, probe_dir: Path) -> dict[str, object]:
    analysis_dir = probe_dir / "analysis"
    commands_dir = probe_dir / "commands"
    final_report = _load_json(analysis_dir / "final_report_v3.json")
    feature_report = _load_json(analysis_dir / "p4_feature_family_ablation_v1.json")
    position_report = _load_json(
        analysis_dir / "p5_position" / "position_framework_analysis.json"
    )
    shadow_plan = _load_json(analysis_dir / "p0_shadow_experiment_plan_v1.json")
    advisory_report = _load_first_json(
        [
            probe_dir / "nas_advisory_validation_report.json",
            probe_dir / "nas_validation_report.json",
        ]
    )
    pipeline = _load_json(commands_dir / "pipeline_advisory.json")
    latest = _load_json(commands_dir / "signals_latest_after.json")
    signal_quality = _load_json(commands_dir / "signal_quality_after.json")
    config_snapshot = _load_json(commands_dir / "config_safety_snapshot.json")

    safety = _safety_summary(
        advisory_report=advisory_report,
        pipeline=pipeline,
        latest=latest,
        config_snapshot=config_snapshot,
    )
    p1_grid = _p1_grid_summary(final_report)
    finance = _financial_raw_summary(feature_report)
    reentry = _reentry_shadow_summary(position_report)
    plan = _plan_summary(shadow_plan)
    maturity = _maturity_summary(final_report, p1_grid=p1_grid)
    checks = _checks(
        safety=safety,
        p1_grid=p1_grid,
        finance=finance,
        reentry=reentry,
        plan=plan,
        maturity=maturity,
    )
    return {
        "report_type": "p1_nas_shadow_validation",
        "probe_dir": str(probe_dir),
        "production_change_allowed": False,
        "safety": safety,
        "p1_probability_scale_shadow_grid": p1_grid,
        "financial_raw_field_coverage": finance,
        "reentry_cooldown_shadow": reentry,
        "shadow_plan": plan,
        "maturity": maturity,
        "signal_quality": {
            "status": str(signal_quality.get("status", "")).strip(),
            "signal_source": str(signal_quality.get("signal_source", "")).strip(),
            "signal_storage_source": str(signal_quality.get("signal_storage_source", "")).strip(),
            "source_signal_count": _int(signal_quality.get("source_signal_count")),
        },
        "checks": checks,
        "status": "pass" if all(bool(item.get("passed")) for item in checks) else "needs_review",
        "next_actions": _next_actions(checks=checks, maturity=maturity, p1_grid=p1_grid),
    }


def render_markdown_report(report: Mapping[str, object]) -> str:
    safety = _mapping(report.get("safety"))
    p1_grid = _mapping(report.get("p1_probability_scale_shadow_grid"))
    finance = _mapping(report.get("financial_raw_field_coverage"))
    reentry = _mapping(report.get("reentry_cooldown_shadow"))
    maturity = _mapping(report.get("maturity"))
    checks = [item for item in _list(report.get("checks")) if isinstance(item, Mapping)]
    lines = [
        "# P1 NAS Shadow Validation Report",
        "",
        f"- status: {report.get('status')}",
        f"- production_change_allowed: {str(report.get('production_change_allowed')).lower()}",
        f"- probe_dir: {report.get('probe_dir')}",
        f"- real_orders_placed: {safety.get('real_orders_placed')}",
        f"- auto_promotion_enabled: {safety.get('auto_promotion_enabled')}",
        f"- risk_guardrails_status: {safety.get('risk_guardrails_status')}",
        f"- latest_signals_source: {safety.get('latest_signals_source')}",
        f"- latest_pipeline_execution_mode: {safety.get('pipeline_execution_mode')}",
        f"- latest_pipeline_executions_count: {safety.get('executions_count')}",
        f"- p1_grid_status: {p1_grid.get('status')}",
        f"- p1_candidate_variant_count: {p1_grid.get('candidate_variant_count')}",
        f"- p1_max_pass_count: {p1_grid.get('max_pass_count')}",
        f"- financial_raw_status: {finance.get('status')}",
        f"- roe_present_rows: {finance.get('roe_present_rows')}",
        f"- debt_ratio_present_rows: {finance.get('debt_ratio_present_rows')}",
        f"- same_period_confirmed: {finance.get('same_period_confirmed')}",
        f"- reentry_shadow_status: {reentry.get('status')}",
        f"- focus_symbols: {reentry.get('focus_symbols')}",
        f"- mature_return_samples: {maturity.get('mature_return_samples')}",
        f"- can_rank_by_profitability: {maturity.get('can_rank_by_profitability')}",
        f"- can_claim_profitability: {maturity.get('can_claim_profitability')}",
        "",
        "## Checks",
        "",
    ]
    for item in checks:
        mark = "PASS" if item.get("passed") else "REVIEW"
        lines.append(f"- {mark}: {item.get('code')} - {item.get('detail')}")
    lines.extend(["", "## Next Actions", ""])
    for item in _list(report.get("next_actions")):
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def _safety_summary(
    *,
    advisory_report: Mapping[str, object],
    pipeline: Mapping[str, object],
    latest: Mapping[str, object],
    config_snapshot: Mapping[str, object],
) -> dict[str, object]:
    advisory_safety = _mapping(advisory_report.get("safety_config"))
    pipeline_update = _mapping(pipeline.get("portfolio_update"))
    executions = pipeline_update.get("executions")
    config_auto = _mapping(config_snapshot.get("auto_promotion"))
    return {
        "advisory_report_status": str(advisory_report.get("status", "")).strip(),
        "pipeline_execution_mode": str(pipeline.get("execution_mode", "")).strip(),
        "portfolio_status": str(pipeline_update.get("status", "")).strip(),
        "executions_count": len(executions) if isinstance(executions, list) else None,
        "real_orders_placed": isinstance(executions, list) and len(executions) > 0,
        "latest_signals_source": str(latest.get("source", "")).strip(),
        "latest_signals_storage_source": str(latest.get("storage_source", "")).strip(),
        "latest_signals_count": len(_list(latest.get("signals"))),
        "auto_promotion_enabled": _as_bool(
            advisory_safety.get("auto_promotion_enabled")
        )
        or _as_bool(config_auto.get("enabled")),
        "risk_guardrails_status": str(
            advisory_safety.get("risk_guardrails_status", "")
        ).strip()
        or "unknown",
    }


def _p1_grid_summary(final_report: Mapping[str, object]) -> dict[str, object]:
    grid = _mapping(final_report.get("p1_probability_scale_shadow_grid"))
    outcome = _mapping(grid.get("outcome_linkage"))
    guardrails = _mapping(grid.get("guardrails"))
    return {
        "present": bool(grid),
        "status": str(grid.get("status", "")).strip(),
        "production_change_allowed": _as_bool(grid.get("production_change_allowed")),
        "candidate_variant_count": _int(grid.get("candidate_variant_count")),
        "max_pass_count": _int(grid.get("max_pass_count")),
        "max_observed_trades_in_variant": _int(
            outcome.get("max_observed_trades_in_variant")
        ),
        "can_rank_by_profitability": _as_bool(outcome.get("can_rank_by_profitability")),
        "can_claim_profitability": _as_bool(outcome.get("can_claim_profitability")),
        "do_not_relax_production_cross_review": _as_bool(
            guardrails.get("do_not_relax_production_cross_review")
        ),
    }


def _financial_raw_summary(feature_report: Mapping[str, object]) -> dict[str, object]:
    financial = _mapping(feature_report.get("financial_data_quality"))
    raw = _mapping(financial.get("raw_field_coverage"))
    semantics = _mapping(raw.get("semantics"))
    return {
        "present": bool(raw),
        "status": str(raw.get("status", "")).strip(),
        "total_rows": _int(raw.get("total_rows")),
        "roe_present_rows": _int(raw.get("roe_present_rows")),
        "debt_ratio_present_rows": _int(raw.get("debt_ratio_present_rows")),
        "both_gate_fields_present_rows": _int(raw.get("both_gate_fields_present_rows")),
        "financial_source_present_rows": _int(raw.get("financial_source_present_rows")),
        "financial_report_date_present_rows": _int(
            raw.get("financial_report_date_present_rows")
        ),
        "financial_missing_fields_present_rows": _int(
            raw.get("financial_missing_fields_present_rows")
        ),
        "default_or_fallback_source_rows": _int(raw.get("default_or_fallback_source_rows")),
        "same_period_confirmed": str(raw.get("same_period_confirmed", "")).strip(),
        "same_source_confirmed": str(raw.get("same_source_confirmed", "")).strip(),
        "financial_data_complete_semantics": str(
            semantics.get("financial_data_complete", "")
        ).strip(),
    }


def _reentry_shadow_summary(position_report: Mapping[str, object]) -> dict[str, object]:
    shadow = _mapping(position_report.get("reentry_cooldown_shadow"))
    variants = [_mapping(item) for item in _list(shadow.get("variants"))]
    focus = _mapping(position_report.get("focus_symbols"))
    return {
        "present": bool(shadow),
        "status": str(shadow.get("status", "")).strip(),
        "production_change_allowed": _as_bool(shadow.get("production_change_allowed")),
        "focus_symbols": _list(shadow.get("focus_symbols")),
        "loss_symbols": _list(shadow.get("loss_symbols")),
        "reentry_hint_symbols": _list(shadow.get("reentry_hint_symbols")),
        "stop_loss_symbols": _list(shadow.get("stop_loss_symbols")),
        "variant_names": [str(item.get("name", "")).strip() for item in variants],
        "guardrails": _mapping(shadow.get("guardrails")),
        "focus_loss_observed_count": _int(focus.get("loss_observed_count")),
    }


def _plan_summary(shadow_plan: Mapping[str, object]) -> dict[str, object]:
    threshold = _mapping(shadow_plan.get("threshold_assessment"))
    feature = _mapping(shadow_plan.get("feature_family_plan"))
    position = _mapping(shadow_plan.get("position_plan"))
    return {
        "present": bool(shadow_plan),
        "status": str(shadow_plan.get("status", "")).strip(),
        "production_change_allowed": _as_bool(shadow_plan.get("production_change_allowed")),
        "has_p1_grid": bool(threshold.get("p1_probability_scale_shadow_grid")),
        "has_financial_raw_coverage": bool(feature.get("financial_raw_field_coverage")),
        "has_reentry_cooldown_shadow": bool(position.get("reentry_cooldown_shadow")),
    }


def _maturity_summary(
    final_report: Mapping[str, object],
    *,
    p1_grid: Mapping[str, object],
) -> dict[str, object]:
    coverage = _mapping(final_report.get("outcome_coverage"))
    observed = _int(coverage.get("observed_returns"))
    max_grid_observed = _int(p1_grid.get("max_observed_trades_in_variant"))
    mature = max(observed, max_grid_observed)
    return {
        "mature_return_samples": mature,
        "minimum_for_rank": 50,
        "minimum_for_profitability_claim": 100,
        "can_rank_by_profitability": mature >= 50
        and _as_bool(p1_grid.get("can_rank_by_profitability")),
        "can_claim_profitability": mature >= 100
        and _as_bool(p1_grid.get("can_claim_profitability")),
    }


def _checks(
    *,
    safety: Mapping[str, object],
    p1_grid: Mapping[str, object],
    finance: Mapping[str, object],
    reentry: Mapping[str, object],
    plan: Mapping[str, object],
    maturity: Mapping[str, object],
) -> list[dict[str, object]]:
    return [
        {
            "code": "no_real_orders",
            "passed": not bool(safety.get("real_orders_placed"))
            and _int(safety.get("executions_count")) == 0,
            "detail": "latest advisory pipeline did not produce real executions",
        },
        {
            "code": "auto_promotion_disabled",
            "passed": not bool(safety.get("auto_promotion_enabled")),
            "detail": "auto_promotion remains disabled",
        },
        {
            "code": "risk_guardrails_not_relaxed",
            "passed": str(safety.get("risk_guardrails_status", "")).strip() == "pass",
            "detail": "captured safety snapshot reports conservative guardrails",
        },
        {
            "code": "latest_pipeline_advisory_only",
            "passed": str(safety.get("pipeline_execution_mode", "")).strip()
            == "advisory_only",
            "detail": "latest pipeline response used advisory_only execution mode",
        },
        {
            "code": "signals_latest_from_pipeline_run",
            "passed": str(safety.get("latest_signals_source", "")).strip()
            == "pipeline_run"
            and _int(safety.get("latest_signals_count")) > 0,
            "detail": "/signals/latest came from controlled pipeline_run",
        },
        {
            "code": "p1_shadow_grid_present",
            "passed": bool(p1_grid.get("present"))
            and not bool(p1_grid.get("production_change_allowed")),
            "detail": "final_report_v3 includes report-only P1 probability-scale grid",
        },
        {
            "code": "p1_shadow_grid_generates_candidates",
            "passed": _int(p1_grid.get("candidate_variant_count")) > 0
            and _int(p1_grid.get("max_pass_count")) > 0,
            "detail": "P1 probability-scale grid generates at least one shadow candidate",
        },
        {
            "code": "p1_grid_guardrail_keeps_production_cross_review",
            "passed": bool(p1_grid.get("do_not_relax_production_cross_review")),
            "detail": "P1 grid is explicitly blocked from production cross-review relaxation",
        },
        {
            "code": "financial_raw_coverage_present",
            "passed": bool(finance.get("present"))
            and str(finance.get("financial_data_complete_semantics", "")).strip()
            == "gate_required_fields_present_only",
            "detail": "financial raw field coverage is present with non-overstated semantics",
        },
        {
            "code": "financial_same_period_not_overclaimed",
            "passed": str(finance.get("same_period_confirmed", "")).strip() == "unknown"
            and str(finance.get("same_source_confirmed", "")).strip() == "unknown",
            "detail": "financial report does not claim same-period/same-source proof",
        },
        {
            "code": "reentry_cooldown_shadow_present",
            "passed": bool(reentry.get("present"))
            and not bool(reentry.get("production_change_allowed")),
            "detail": "position report includes report-only re-entry cooldown shadow",
        },
        {
            "code": "reentry_shadow_guardrails_report_only",
            "passed": bool(_mapping(reentry.get("guardrails")).get("do_not_write_week6_controls")),
            "detail": "re-entry shadow does not write week6 controls",
        },
        {
            "code": "shadow_plan_consumes_p1_artifacts",
            "passed": bool(plan.get("has_p1_grid"))
            and bool(plan.get("has_financial_raw_coverage"))
            and bool(plan.get("has_reentry_cooldown_shadow"))
            and not bool(plan.get("production_change_allowed")),
            "detail": "shadow plan consumes P1 grid, financial raw coverage and re-entry shadow",
        },
        {
            "code": "maturity_gate_enforced",
            "passed": not bool(maturity.get("can_claim_profitability"))
            or _int(maturity.get("mature_return_samples")) >= _int(
                maturity.get("minimum_for_profitability_claim")
            ),
            "detail": "profitability claims require the configured mature sample minimum",
        },
    ]


def _next_actions(
    *,
    checks: Sequence[Mapping[str, object]],
    maturity: Mapping[str, object],
    p1_grid: Mapping[str, object],
) -> list[str]:
    failed = {str(item.get("code", "")) for item in checks if not item.get("passed")}
    actions: list[str] = []
    if failed:
        actions.append("Review failed checks before using this run as P1 evidence.")
    if "p1_shadow_grid_generates_candidates" in failed:
        actions.append("Inspect p1_probability_scale_shadow_grid distributions and source rows.")
    if _int(maturity.get("mature_return_samples")) < 50:
        actions.append("Continue advisory_only collection until at least 50 mature samples exist.")
    if _int(maturity.get("mature_return_samples")) < 100:
        actions.append("Do not change production thresholds before 100 mature samples.")
    if not bool(p1_grid.get("can_rank_by_profitability")):
        actions.append("Rank P1 variants by candidate coverage only; profitability rank is not ready.")
    if not actions:
        actions.append("Continue advisory_only shadow collection and compare P1 variants weekly.")
    return actions


def _load_json(path: Path) -> dict[str, object]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _load_first_json(paths: Sequence[Path]) -> dict[str, object]:
    for path in paths:
        payload = _load_json(path)
        if payload:
            return payload
    return {}


def _path(raw: str | Path) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


if __name__ == "__main__":
    main()
