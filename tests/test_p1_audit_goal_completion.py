from __future__ import annotations

import json
from pathlib import Path

from scripts.p1_audit_goal_completion import build_completion_audit


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _run_report() -> dict[str, object]:
    return {
        "status": "pass",
        "checks": [
            {"code": "no_real_orders", "passed": True},
            {"code": "auto_promotion_disabled", "passed": True},
            {"code": "risk_guardrails_not_relaxed", "passed": True},
            {"code": "latest_pipeline_advisory_only", "passed": True},
            {"code": "signals_latest_from_pipeline_run", "passed": True},
            {"code": "p1_shadow_grid_generates_candidates", "passed": True},
            {"code": "financial_raw_fields_observed", "passed": True},
            {"code": "maturity_gate_enforced", "passed": True},
        ],
    }


def _write_complete_collection(collection_dir: Path) -> None:
    summary = {
        "passed_runs": 2,
        "failed_runs": 0,
        "financial_raw_fields_observed_runs": 2,
        "roe_present_rows": 8,
        "debt_ratio_present_rows": 8,
        "financial_source_present_rows": 8,
        "financial_report_date_present_rows": 8,
        "max_candidate_variant_count": 400,
        "max_mature_return_samples": 12,
    }
    _write_json(
        collection_dir / "p1_nas_environment.json",
        {"status": "pass"},
    )
    _write_json(
        collection_dir / "p1_advisory_collection_report.json",
        {
            "status": "pass",
            "production_change_allowed": False,
            "completed_runs": 2,
            "summary": summary,
        },
    )
    _write_json(
        collection_dir / "p1_advisory_collection_acceptance.json",
        {"status": "pass", "summary": summary},
    )
    _write_json(collection_dir / "run_001" / "nas_validation_report.json", _run_report())
    _write_json(collection_dir / "run_002" / "nas_validation_report.json", _run_report())


def test_goal_completion_audit_marks_complete_collection_complete(tmp_path: Path) -> None:
    collection_dir = tmp_path / "collection"
    _write_complete_collection(collection_dir)

    report = build_completion_audit(collection_dir=collection_dir, min_completed_runs=2)

    assert report["status"] == "complete"
    assert report["production_change_allowed"] is False
    assert all(item["passed"] for item in report["checks"])
    assert "Keep production thresholds unchanged." in report["next_actions"]


def test_goal_completion_audit_requires_environment_evidence(tmp_path: Path) -> None:
    collection_dir = tmp_path / "collection"
    _write_complete_collection(collection_dir)
    _write_json(collection_dir / "p1_nas_environment.json", {"status": "fail"})

    report = build_completion_audit(collection_dir=collection_dir, min_completed_runs=2)

    assert report["status"] == "incomplete"
    failed_codes = {item["code"] for item in report["checks"] if not item["passed"]}
    assert "nas_environment_safe_and_current" in failed_codes


def test_goal_completion_audit_requires_run_level_safety_checks(tmp_path: Path) -> None:
    collection_dir = tmp_path / "collection"
    _write_complete_collection(collection_dir)
    broken = _run_report()
    for check in broken["checks"]:
        if check["code"] == "no_real_orders":
            check["passed"] = False
    _write_json(collection_dir / "run_002" / "nas_validation_report.json", broken)

    report = build_completion_audit(collection_dir=collection_dir, min_completed_runs=2)

    assert report["status"] == "incomplete"
    failed_codes = {item["code"] for item in report["checks"] if not item["passed"]}
    assert "no_real_trading_or_promotion" in failed_codes
