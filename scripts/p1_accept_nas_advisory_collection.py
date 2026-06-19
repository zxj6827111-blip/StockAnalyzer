"""Accept or reject a P1 NAS advisory collection report."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read p1_advisory_collection_report.json and write a deterministic "
            "acceptance report. This is read-only and never calls the API."
        ),
    )
    parser.add_argument(
        "--collection-dir",
        default="artifacts/research/p1_advisory_collection_quick_rerun",
        help="Directory containing p1_advisory_collection_report.json.",
    )
    parser.add_argument(
        "--min-completed-runs",
        type=int,
        default=2,
        help="Minimum completed advisory-only runs required for this acceptance gate.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    collection_dir = _path(args.collection_dir)
    report = build_acceptance_report(
        collection_dir=collection_dir,
        min_completed_runs=max(1, int(args.min_completed_runs)),
    )
    json_path = collection_dir / "p1_advisory_collection_acceptance.json"
    md_path = collection_dir / "p1_advisory_collection_acceptance.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"status": report["status"], "json": str(json_path), "markdown": str(md_path)}, ensure_ascii=False))
    if report["status"] != "pass":
        sys.exit(1)


def build_acceptance_report(
    *,
    collection_dir: Path,
    min_completed_runs: int,
) -> dict[str, object]:
    source_path = collection_dir / "p1_advisory_collection_report.json"
    collection = _load_json(source_path)
    summary = _mapping(collection.get("summary"))
    checks = _checks(collection=collection, summary=summary, min_completed_runs=min_completed_runs)
    status = "pass" if all(bool(item.get("passed")) for item in checks) else "fail"
    return {
        "report_type": "p1_advisory_collection_acceptance",
        "status": status,
        "production_change_allowed": False,
        "collection_dir": str(collection_dir),
        "source_report": str(source_path),
        "min_completed_runs": min_completed_runs,
        "collection_status": str(collection.get("status", "")).strip(),
        "completed_runs": _int(collection.get("completed_runs")),
        "summary": dict(summary),
        "checks": checks,
        "next_actions": _next_actions(status=status, summary=summary, collection=collection),
    }


def render_markdown(report: Mapping[str, object]) -> str:
    summary = _mapping(report.get("summary"))
    lines = [
        "# P1 Advisory Collection Acceptance",
        "",
        f"- status: {report.get('status')}",
        f"- production_change_allowed: {str(report.get('production_change_allowed')).lower()}",
        f"- collection_status: {report.get('collection_status')}",
        f"- completed_runs: {report.get('completed_runs', 0)}",
        f"- passed_runs: {summary.get('passed_runs', 0)}",
        f"- failed_runs: {summary.get('failed_runs', 0)}",
        f"- financial_raw_fields_observed_runs: {summary.get('financial_raw_fields_observed_runs', 0)}",
        f"- roe_present_rows: {summary.get('roe_present_rows', 0)}",
        f"- debt_ratio_present_rows: {summary.get('debt_ratio_present_rows', 0)}",
        f"- financial_source_present_rows: {summary.get('financial_source_present_rows', 0)}",
        f"- financial_report_date_present_rows: {summary.get('financial_report_date_present_rows', 0)}",
        f"- max_candidate_variant_count: {summary.get('max_candidate_variant_count', 0)}",
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
    collection: Mapping[str, object],
    summary: Mapping[str, object],
    min_completed_runs: int,
) -> list[dict[str, object]]:
    completed_runs = _int(collection.get("completed_runs"))
    passed_runs = _int(summary.get("passed_runs"))
    failed_runs = _int(summary.get("failed_runs"))
    return [
        {
            "code": "collection_status_pass",
            "passed": str(collection.get("status", "")).strip() == "pass",
            "detail": "collection runner completed without safety or per-run failure",
        },
        {
            "code": "production_change_disallowed",
            "passed": collection.get("production_change_allowed") is False,
            "detail": "collection report keeps production_change_allowed=false",
        },
        {
            "code": "minimum_completed_runs",
            "passed": completed_runs >= min_completed_runs
            and passed_runs >= min_completed_runs
            and failed_runs == 0,
            "detail": f"requires at least {min_completed_runs} passed advisory-only runs and zero failed runs",
        },
        {
            "code": "no_safety_failure",
            "passed": not bool(_mapping(collection.get("safety_failure"))),
            "detail": "collection report must not contain a safety_failure block",
        },
        {
            "code": "p1_grid_generated_candidates",
            "passed": _int(summary.get("max_candidate_variant_count")) > 0,
            "detail": "P1 probability-scale shadow grid generated at least one candidate variant",
        },
        {
            "code": "financial_raw_fields_observed",
            "passed": _int(summary.get("financial_raw_fields_observed_runs")) > 0
            and _int(summary.get("roe_present_rows")) > 0
            and _int(summary.get("debt_ratio_present_rows")) > 0
            and _int(summary.get("financial_source_present_rows")) > 0
            and _int(summary.get("financial_report_date_present_rows")) > 0,
            "detail": "financial raw evidence includes ROE, debt ratio, source and report date",
        },
        {
            "code": "mature_samples_not_enough_for_production_threshold_change",
            "passed": _int(summary.get("max_mature_return_samples")) < 100,
            "detail": "before 100 mature samples, this report must not justify production threshold changes",
        },
    ]


def _next_actions(
    *,
    status: str,
    summary: Mapping[str, object],
    collection: Mapping[str, object],
) -> list[str]:
    if status != "pass":
        return [
            "Do not use this collection as evidence for production threshold changes.",
            "Fix the failed acceptance checks and rerun advisory-only collection.",
        ]
    actions = [
        "Keep production thresholds unchanged.",
    ]
    mature_samples = _int(summary.get("max_mature_return_samples"))
    if mature_samples < 50:
        actions.append("Continue advisory_only collection until at least 50 mature samples exist.")
    if mature_samples >= 50:
        actions.append("Start ranking P1 variants by mature outcomes, still without production changes.")
    if mature_samples < 100:
        actions.append("Do not recommend production threshold changes before 100 mature samples.")
    if str(collection.get("status", "")).strip() == "pass":
        actions.append("Attach this acceptance report to the NAS handoff summary.")
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
