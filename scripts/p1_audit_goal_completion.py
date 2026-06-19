"""Audit whether the P1 NAS advisory collection satisfies the thread goal."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

REQUIRED_RUN_CHECKS = {
    "no_real_orders",
    "auto_promotion_disabled",
    "risk_guardrails_not_relaxed",
    "latest_pipeline_advisory_only",
    "signals_latest_from_pipeline_run",
    "p1_shadow_grid_generates_candidates",
    "financial_raw_fields_observed",
    "maturity_gate_enforced",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read NAS P1 collection artifacts and write a final completion audit. "
            "This is read-only and never calls the API."
        ),
    )
    parser.add_argument(
        "--collection-dir",
        default="artifacts/research/p1_advisory_collection_quick_rerun",
    )
    parser.add_argument("--min-completed-runs", type=int, default=2)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    collection_dir = _path(args.collection_dir)
    report = build_completion_audit(
        collection_dir=collection_dir,
        min_completed_runs=max(1, int(args.min_completed_runs)),
    )
    json_path = collection_dir / "p1_goal_completion_audit.json"
    md_path = collection_dir / "p1_goal_completion_audit.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"status": report["status"], "json": str(json_path), "markdown": str(md_path)}, ensure_ascii=False))
    if report["status"] != "complete":
        sys.exit(1)


def build_completion_audit(
    *,
    collection_dir: Path,
    min_completed_runs: int,
) -> dict[str, object]:
    environment = _load_json(collection_dir / "p1_nas_environment.json")
    collection = _load_json(collection_dir / "p1_advisory_collection_report.json")
    acceptance = _load_json(collection_dir / "p1_advisory_collection_acceptance.json")
    summary = _mapping(collection.get("summary"))
    run_reports = _load_run_reports(collection_dir)
    checks = _checks(
        environment=environment,
        collection=collection,
        acceptance=acceptance,
        summary=summary,
        run_reports=run_reports,
        min_completed_runs=min_completed_runs,
    )
    status = "complete" if all(bool(item.get("passed")) for item in checks) else "incomplete"
    return {
        "report_type": "p1_goal_completion_audit",
        "status": status,
        "production_change_allowed": False,
        "collection_dir": str(collection_dir),
        "min_completed_runs": min_completed_runs,
        "environment_status": str(environment.get("status", "")).strip(),
        "collection_status": str(collection.get("status", "")).strip(),
        "acceptance_status": str(acceptance.get("status", "")).strip(),
        "completed_runs": _int(collection.get("completed_runs")),
        "summary": dict(summary),
        "run_report_count": len(run_reports),
        "checks": checks,
        "next_actions": _next_actions(status=status, summary=summary),
    }


def render_markdown(report: Mapping[str, object]) -> str:
    summary = _mapping(report.get("summary"))
    lines = [
        "# P1 Goal Completion Audit",
        "",
        f"- status: {report.get('status')}",
        f"- production_change_allowed: {str(report.get('production_change_allowed')).lower()}",
        f"- environment_status: {report.get('environment_status')}",
        f"- collection_status: {report.get('collection_status')}",
        f"- acceptance_status: {report.get('acceptance_status')}",
        f"- completed_runs: {report.get('completed_runs', 0)}",
        f"- run_report_count: {report.get('run_report_count', 0)}",
        f"- max_candidate_variant_count: {summary.get('max_candidate_variant_count', 0)}",
        f"- financial_raw_fields_observed_runs: {summary.get('financial_raw_fields_observed_runs', 0)}",
        f"- roe_present_rows: {summary.get('roe_present_rows', 0)}",
        f"- debt_ratio_present_rows: {summary.get('debt_ratio_present_rows', 0)}",
        f"- financial_source_present_rows: {summary.get('financial_source_present_rows', 0)}",
        f"- financial_report_date_present_rows: {summary.get('financial_report_date_present_rows', 0)}",
        f"- max_mature_return_samples: {summary.get('max_mature_return_samples', 0)}",
        "",
        "## Checks",
        "",
    ]
    for item in _list(report.get("checks")):
        check = _mapping(item)
        lines.append(
            "- [{mark}] {code}: {detail}".format(
                mark="x" if check.get("passed") else " ",
                code=check.get("code", ""),
                detail=check.get("detail", ""),
            )
        )
    lines.extend(["", "## Next Actions", ""])
    for action in _list(report.get("next_actions")):
        lines.append(f"- {action}")
    lines.append("")
    return "\n".join(lines)


def _checks(
    *,
    environment: Mapping[str, object],
    collection: Mapping[str, object],
    acceptance: Mapping[str, object],
    summary: Mapping[str, object],
    run_reports: list[Mapping[str, object]],
    min_completed_runs: int,
) -> list[dict[str, object]]:
    completed_runs = _int(collection.get("completed_runs"))
    passed_runs = _int(summary.get("passed_runs"))
    failed_runs = _int(summary.get("failed_runs"))
    run_check_results = _run_check_results(run_reports)
    return [
        {
            "code": "nas_environment_safe_and_current",
            "passed": str(environment.get("status", "")).strip() == "pass",
            "detail": "NAS environment report confirms branch, HEAD and /health safety",
        },
        {
            "code": "collection_report_passed",
            "passed": str(collection.get("status", "")).strip() == "pass",
            "detail": "collection runner completed without safety or run failure",
        },
        {
            "code": "acceptance_report_passed",
            "passed": str(acceptance.get("status", "")).strip() == "pass",
            "detail": "collection acceptance report passed",
        },
        {
            "code": "continuous_advisory_runs_completed",
            "passed": completed_runs >= min_completed_runs
            and passed_runs >= min_completed_runs
            and failed_runs == 0
            and len(run_reports) >= min_completed_runs,
            "detail": f"requires at least {min_completed_runs} passed NAS run reports",
        },
        {
            "code": "no_real_trading_or_promotion",
            "passed": run_check_results.get("no_real_orders") is True
            and run_check_results.get("auto_promotion_disabled") is True
            and run_check_results.get("risk_guardrails_not_relaxed") is True,
            "detail": "run reports confirm no real orders, no auto promotion and guardrails not relaxed",
        },
        {
            "code": "latest_signals_from_controlled_pipeline",
            "passed": run_check_results.get("latest_pipeline_advisory_only") is True
            and run_check_results.get("signals_latest_from_pipeline_run") is True,
            "detail": "run reports confirm controlled advisory pipeline signals",
        },
        {
            "code": "p1_shadow_and_financial_evidence_present",
            "passed": run_check_results.get("p1_shadow_grid_generates_candidates") is True
            and run_check_results.get("financial_raw_fields_observed") is True
            and _int(summary.get("max_candidate_variant_count")) > 0,
            "detail": "P1 grid generated candidates and financial raw evidence is present",
        },
        {
            "code": "profitability_threshold_change_not_justified",
            "passed": _int(summary.get("max_mature_return_samples")) < 100
            and run_check_results.get("maturity_gate_enforced") is True,
            "detail": "mature samples remain below production-threshold-change minimum",
        },
    ]


def _run_check_results(run_reports: list[Mapping[str, object]]) -> dict[str, bool]:
    results: dict[str, bool] = {}
    for code in REQUIRED_RUN_CHECKS:
        states: list[bool] = []
        for report in run_reports:
            check = _check_by_code(report, code)
            if check:
                states.append(bool(check.get("passed")))
        results[code] = bool(states) and all(states)
    return results


def _check_by_code(report: Mapping[str, object], code: str) -> dict[str, object]:
    for item in _list(report.get("checks")):
        check = _mapping(item)
        if str(check.get("code", "")).strip() == code:
            return check
    return {}


def _load_run_reports(collection_dir: Path) -> list[Mapping[str, object]]:
    reports: list[Mapping[str, object]] = []
    for path in sorted(collection_dir.glob("run_*/nas_validation_report.json")):
        report = _load_json(path)
        if report:
            reports.append(report)
    return reports


def _next_actions(*, status: str, summary: Mapping[str, object]) -> list[str]:
    if status != "complete":
        return [
            "Do not mark the thread goal complete from these artifacts.",
            "Rerun NAS advisory-only collection after fixing failed audit checks.",
        ]
    actions = [
        "Thread goal evidence is complete for the P1 advisory-only collection stage.",
        "Keep production thresholds unchanged.",
    ]
    if _int(summary.get("max_mature_return_samples")) < 50:
        actions.append("Continue advisory_only collection before ranking variants by profitability.")
    if _int(summary.get("max_mature_return_samples")) < 100:
        actions.append("Do not recommend production threshold changes yet.")
    return actions


def _path(raw: str | Path) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


def _load_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


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


if __name__ == "__main__":
    main()
