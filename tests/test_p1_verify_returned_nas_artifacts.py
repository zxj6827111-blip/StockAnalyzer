from __future__ import annotations

import json
import zipfile
from pathlib import Path

from scripts.p1_verify_returned_nas_artifacts import verify_returned_artifact


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _complete_artifact(root: Path) -> None:
    collection_dir = root / "p1_advisory_collection"
    summary = {
        "passed_runs": 2,
        "failed_runs": 0,
        "financial_raw_fields_observed_runs": 2,
        "roe_present_rows": 10,
        "debt_ratio_present_rows": 10,
        "financial_source_present_rows": 10,
        "financial_report_date_present_rows": 10,
        "max_candidate_variant_count": 400,
        "max_mature_return_samples": 8,
    }
    _write_json(collection_dir / "p1_nas_environment.json", {"status": "pass"})
    _write_json(
        collection_dir / "p1_advisory_collection_report.json",
        {"status": "pass", "summary": summary},
    )
    _write_json(
        collection_dir / "p1_advisory_collection_acceptance.json",
        {"status": "pass", "summary": summary},
    )
    _write_json(
        collection_dir / "p1_goal_completion_audit.json",
        {
            "status": "complete",
            "production_change_allowed": False,
            "min_completed_runs": 2,
            "completed_runs": 2,
            "run_report_count": 2,
            "summary": summary,
            "checks": [
                {"code": "nas_environment_safe_and_current", "passed": True},
                {"code": "collection_report_passed", "passed": True},
                {"code": "acceptance_report_passed", "passed": True},
            ],
        },
    )


def test_verify_returned_artifact_accepts_complete_directory(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifact"
    _complete_artifact(artifact_dir)

    report = verify_returned_artifact(
        artifact=artifact_dir,
        output_dir=tmp_path / "out",
    )

    assert report["status"] == "complete"
    assert all(item["passed"] for item in report["checks"])
    assert report["summary"]["roe_present_rows"] == 10


def test_verify_returned_artifact_accepts_complete_zip(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifact"
    _complete_artifact(artifact_dir)
    zip_path = tmp_path / "artifact.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for path in artifact_dir.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(artifact_dir))

    report = verify_returned_artifact(
        artifact=zip_path,
        output_dir=tmp_path / "out",
    )

    assert report["status"] == "complete"
    assert report["completed_runs"] == 2
    assert report["run_report_count"] == 2


def test_verify_returned_artifact_fails_without_goal_audit(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()

    report = verify_returned_artifact(
        artifact=artifact_dir,
        output_dir=tmp_path / "out",
    )

    assert report["status"] == "incomplete"
    assert report["checks"][0]["code"] == "goal_completion_audit_missing"
