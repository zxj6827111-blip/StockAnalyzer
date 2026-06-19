"""Verify returned NAS P1 artifacts from a zip file or directory."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify returned NAS artifacts contain a complete P1 goal completion "
            "audit. This is read-only for source code and never calls the API."
        ),
    )
    parser.add_argument("artifact", help="Path to NAS artifact zip or extracted directory.")
    parser.add_argument(
        "--output-dir",
        default="artifacts/research/returned_nas_verification",
        help="Directory where the verification summary is written.",
    )
    parser.add_argument(
        "--keep-extracted",
        action="store_true",
        help="Keep extracted zip contents under the output directory.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = verify_returned_artifact(
        artifact=Path(args.artifact),
        output_dir=_path(args.output_dir),
        keep_extracted=bool(args.keep_extracted),
    )
    output_dir = _path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "returned_nas_artifact_verification.json"
    md_path = output_dir / "returned_nas_artifact_verification.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"status": report["status"], "json": str(json_path), "markdown": str(md_path)}, ensure_ascii=False))
    if report["status"] != "complete":
        sys.exit(1)


def verify_returned_artifact(
    *,
    artifact: Path,
    output_dir: Path,
    keep_extracted: bool = False,
) -> dict[str, object]:
    artifact = artifact.expanduser().resolve()
    if not artifact.exists():
        return _failure(artifact=artifact, reason="artifact_not_found", detail=str(artifact))

    cleanup_dir: Path | None = None
    if artifact.is_file():
        if artifact.suffix.lower() != ".zip":
            return _failure(artifact=artifact, reason="unsupported_file_type", detail=artifact.suffix)
        extract_root = output_dir / "extracted" if keep_extracted else Path(
            tempfile.mkdtemp(prefix="p1_returned_nas_")
        )
        if keep_extracted and extract_root.exists():
            shutil.rmtree(extract_root)
        extract_root.mkdir(parents=True, exist_ok=True)
        cleanup_dir = None if keep_extracted else extract_root
        with zipfile.ZipFile(artifact) as archive:
            archive.extractall(extract_root)
        root = extract_root
    else:
        root = artifact

    try:
        report = _verify_extracted_root(artifact=artifact, root=root)
    finally:
        if cleanup_dir is not None:
            shutil.rmtree(cleanup_dir, ignore_errors=True)
    return report


def render_markdown(report: Mapping[str, object]) -> str:
    lines = [
        "# Returned NAS Artifact Verification",
        "",
        f"- status: {report.get('status')}",
        f"- artifact: {report.get('artifact')}",
        f"- audit_path: {report.get('audit_path', '')}",
        f"- collection_dir: {report.get('collection_dir', '')}",
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
    lines.extend(["", "## Summary", ""])
    summary = _mapping(report.get("summary"))
    for key in [
        "completed_runs",
        "run_report_count",
        "max_candidate_variant_count",
        "financial_raw_fields_observed_runs",
        "roe_present_rows",
        "debt_ratio_present_rows",
        "financial_source_present_rows",
        "financial_report_date_present_rows",
        "max_mature_return_samples",
    ]:
        lines.append(f"- {key}: {summary.get(key, report.get(key, 0))}")
    lines.append("")
    return "\n".join(lines)


def _verify_extracted_root(*, artifact: Path, root: Path) -> dict[str, object]:
    audit_paths = sorted(root.rglob("p1_goal_completion_audit.json"))
    if not audit_paths:
        return _failure(
            artifact=artifact,
            reason="goal_completion_audit_missing",
            detail="p1_goal_completion_audit.json was not found",
        )
    audit_path = audit_paths[0]
    audit = _load_json(audit_path)
    collection_dir = audit_path.parent
    collection = _load_json(collection_dir / "p1_advisory_collection_report.json")
    acceptance = _load_json(collection_dir / "p1_advisory_collection_acceptance.json")
    environment = _load_json(collection_dir / "p1_nas_environment.json")
    checks = _checks(
        audit=audit,
        collection=collection,
        acceptance=acceptance,
        environment=environment,
    )
    status = "complete" if all(bool(item.get("passed")) for item in checks) else "incomplete"
    summary = dict(_mapping(audit.get("summary")))
    return {
        "report_type": "returned_nas_artifact_verification",
        "status": status,
        "artifact": str(artifact),
        "audit_path": str(audit_path),
        "collection_dir": str(collection_dir),
        "completed_runs": _int(audit.get("completed_runs")),
        "run_report_count": _int(audit.get("run_report_count")),
        "summary": summary,
        "checks": checks,
    }


def _checks(
    *,
    audit: Mapping[str, object],
    collection: Mapping[str, object],
    acceptance: Mapping[str, object],
    environment: Mapping[str, object],
) -> list[dict[str, object]]:
    return [
        {
            "code": "goal_completion_status_complete",
            "passed": str(audit.get("status", "")).strip() == "complete",
            "detail": "p1_goal_completion_audit.json reports status=complete",
        },
        {
            "code": "audit_production_change_disallowed",
            "passed": audit.get("production_change_allowed") is False,
            "detail": "goal audit keeps production_change_allowed=false",
        },
        {
            "code": "collection_report_present_and_passed",
            "passed": str(collection.get("status", "")).strip() == "pass",
            "detail": "collection report exists and status=pass",
        },
        {
            "code": "acceptance_report_present_and_passed",
            "passed": str(acceptance.get("status", "")).strip() == "pass",
            "detail": "acceptance report exists and status=pass",
        },
        {
            "code": "environment_report_present_and_passed",
            "passed": str(environment.get("status", "")).strip() == "pass",
            "detail": "NAS environment report exists and status=pass",
        },
        {
            "code": "audit_checks_all_passed",
            "passed": _all_report_checks_passed(audit),
            "detail": "all checks inside p1_goal_completion_audit.json passed",
        },
        {
            "code": "run_reports_present",
            "passed": _int(audit.get("run_report_count")) >= _int(audit.get("min_completed_runs")),
            "detail": "goal audit saw enough per-run NAS validation reports",
        },
    ]


def _all_report_checks_passed(report: Mapping[str, object]) -> bool:
    checks = [_mapping(item) for item in _list(report.get("checks"))]
    return bool(checks) and all(bool(item.get("passed")) for item in checks)


def _failure(*, artifact: Path, reason: str, detail: str) -> dict[str, object]:
    return {
        "report_type": "returned_nas_artifact_verification",
        "status": "incomplete",
        "artifact": str(artifact),
        "checks": [
            {
                "code": reason,
                "passed": False,
                "detail": detail,
            }
        ],
        "summary": {},
    }


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
