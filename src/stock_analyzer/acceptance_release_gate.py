"""Release gate helpers for V1.3 acceptance artifacts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path


def count_not_tested_checks(*, v13_acceptance_report: Mapping[str, object]) -> int:
    sections = v13_acceptance_report.get("sections", {})
    if not isinstance(sections, Mapping):
        return 0
    count = 0
    for section in sections.values():
        if not isinstance(section, Mapping):
            continue
        checks = section.get("checks", [])
        if not isinstance(checks, list):
            continue
        for check in checks:
            if not isinstance(check, Mapping):
                continue
            if str(check.get("actual", "")).strip().lower() == "not_tested":
                count += 1
    return count


def build_acceptance_release_gate_report(
    *,
    v13_acceptance_report: Mapping[str, object],
    closed_loop_smoke_passed: bool,
    closed_loop_smoke_detail: str = "",
    generated_at: str | None = None,
) -> dict[str, object]:
    v13_status = str(v13_acceptance_report.get("status", "")).strip().lower()
    baseline_type = str(v13_acceptance_report.get("baseline_type", "")).strip()
    not_tested_count = count_not_tested_checks(v13_acceptance_report=v13_acceptance_report)

    checks = [
        _check(
            name="v13_acceptance_pass",
            passed=v13_status == "pass",
            actual=v13_status or "missing",
            threshold="pass",
            detail="v1.3 acceptance overall status",
        ),
        _check(
            name="native_baseline",
            passed=baseline_type == "native_baseline",
            actual=baseline_type or "missing",
            threshold="native_baseline",
            detail="baseline report type",
        ),
        _check(
            name="not_tested_count_zero",
            passed=not_tested_count == 0,
            actual=not_tested_count,
            threshold="0",
            detail="count of acceptance checks still marked as not_tested",
        ),
        _check(
            name="closed_loop_smoke_pass",
            passed=closed_loop_smoke_passed,
            actual=1 if closed_loop_smoke_passed else 0,
            threshold="1",
            detail=closed_loop_smoke_detail or "tests/test_service_closed_loop_flow.py",
        ),
    ]
    overall = "pass" if all(item["status"] == "pass" for item in checks) else "fail"
    return {
        "generated_at": generated_at or datetime.now().isoformat(),
        "status": overall,
        "checks": checks,
        "summary": {
            "pass": sum(1 for item in checks if item["status"] == "pass"),
            "fail": sum(1 for item in checks if item["status"] == "fail"),
            "not_tested_count": not_tested_count,
        },
        "v13_acceptance": {
            "status": v13_status,
            "baseline_type": baseline_type,
            "output_path": str(v13_acceptance_report.get("output_path", "")),
        },
        "closed_loop_smoke": {
            "passed": closed_loop_smoke_passed,
            "detail": closed_loop_smoke_detail or "tests/test_service_closed_loop_flow.py",
        },
    }


def persist_acceptance_release_gate_report(
    *,
    report: Mapping[str, object],
    output_path: str | Path,
) -> str:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _check(
    *,
    name: str,
    passed: bool,
    actual: object,
    threshold: object,
    detail: str,
) -> dict[str, object]:
    return {
        "name": name,
        "status": "pass" if passed else "fail",
        "actual": actual,
        "threshold": threshold,
        "detail": detail,
    }
