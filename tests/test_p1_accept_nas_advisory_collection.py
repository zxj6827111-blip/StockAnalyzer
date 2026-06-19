from __future__ import annotations

import json
from pathlib import Path

from scripts.p1_accept_nas_advisory_collection import build_acceptance_report


def _write_collection(path: Path, payload: dict[str, object]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "p1_advisory_collection_report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def test_acceptance_passes_valid_p1_collection_report(tmp_path: Path) -> None:
    collection_dir = tmp_path / "collection"
    _write_collection(
        collection_dir,
        {
            "status": "pass",
            "production_change_allowed": False,
            "completed_runs": 2,
            "summary": {
                "passed_runs": 2,
                "failed_runs": 0,
                "financial_raw_fields_observed_runs": 2,
                "roe_present_rows": 12,
                "debt_ratio_present_rows": 12,
                "financial_source_present_rows": 12,
                "financial_report_date_present_rows": 12,
                "max_candidate_variant_count": 400,
                "max_mature_return_samples": 8,
            },
            "runs": [{"status": "pass"}, {"status": "pass"}],
        },
    )

    report = build_acceptance_report(collection_dir=collection_dir, min_completed_runs=2)

    assert report["status"] == "pass"
    assert report["production_change_allowed"] is False
    assert all(item["passed"] for item in report["checks"])
    assert "Do not recommend production threshold changes before 100 mature samples." in report[
        "next_actions"
    ]


def test_acceptance_fails_safety_failure_report(tmp_path: Path) -> None:
    collection_dir = tmp_path / "collection"
    _write_collection(
        collection_dir,
        {
            "status": "safety_check_failed",
            "production_change_allowed": False,
            "completed_runs": 0,
            "safety_failure": {"failed_check": "api_health_advisory_only"},
            "summary": {
                "passed_runs": 0,
                "failed_runs": 0,
                "financial_raw_fields_observed_runs": 0,
                "roe_present_rows": 0,
                "debt_ratio_present_rows": 0,
                "financial_source_present_rows": 0,
                "financial_report_date_present_rows": 0,
                "max_candidate_variant_count": 0,
                "max_mature_return_samples": 0,
            },
        },
    )

    report = build_acceptance_report(collection_dir=collection_dir, min_completed_runs=2)

    assert report["status"] == "fail"
    failed_codes = {item["code"] for item in report["checks"] if not item["passed"]}
    assert "collection_status_pass" in failed_codes
    assert "minimum_completed_runs" in failed_codes
    assert "no_safety_failure" in failed_codes
    assert "financial_raw_fields_observed" in failed_codes


def test_acceptance_fails_when_financial_raw_counts_are_missing(
    tmp_path: Path,
) -> None:
    collection_dir = tmp_path / "collection"
    _write_collection(
        collection_dir,
        {
            "status": "pass",
            "production_change_allowed": False,
            "completed_runs": 2,
            "summary": {
                "passed_runs": 2,
                "failed_runs": 0,
                "financial_raw_fields_observed_runs": 0,
                "roe_present_rows": 0,
                "debt_ratio_present_rows": 0,
                "financial_source_present_rows": 0,
                "financial_report_date_present_rows": 0,
                "max_candidate_variant_count": 400,
                "max_mature_return_samples": 8,
            },
        },
    )

    report = build_acceptance_report(collection_dir=collection_dir, min_completed_runs=2)

    assert report["status"] == "fail"
    financial_check = next(
        item for item in report["checks"] if item["code"] == "financial_raw_fields_observed"
    )
    assert financial_check["passed"] is False
