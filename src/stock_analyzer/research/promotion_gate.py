"""Deterministic manual Go/No-Go gate for shadow model and threshold candidates."""

from __future__ import annotations

from collections.abc import Mapping

_FLOAT_TOLERANCE = 1e-12


def evaluate_shadow_promotion(evidence: Mapping[str, object]) -> dict[str, object]:
    matured_samples = _int(evidence.get("matured_samples"))
    trading_days = _int(evidence.get("trading_days"))
    baseline_precision = _float(evidence.get("baseline_precision"))
    candidate_precision = _float(evidence.get("candidate_precision"))
    baseline_drawdown = _float(evidence.get("baseline_max_drawdown"))
    candidate_drawdown = _float(evidence.get("candidate_max_drawdown"))
    checks = [
        _check("matured_samples_gte_100", matured_samples >= 100, matured_samples),
        _check("trading_days_gte_20", trading_days >= 20, trading_days),
        _check(
            "precision_improvement_gte_0_03",
            candidate_precision - baseline_precision >= 0.03 - _FLOAT_TOLERANCE,
            round(candidate_precision - baseline_precision, 6),
        ),
        _check(
            "drawdown_degradation_lte_0_02",
            candidate_drawdown - baseline_drawdown <= 0.02 + _FLOAT_TOLERANCE,
            round(candidate_drawdown - baseline_drawdown, 6),
        ),
        _check("probability_health", bool(evidence.get("probability_healthy", False)), None),
        _check("coverage_gate", bool(evidence.get("coverage_passed", False)), None),
        _check("stability_gate", bool(evidence.get("stability_passed", False)), None),
        _check("time_split_replay", bool(evidence.get("time_split_replay_passed", False)), None),
        _check("state_consistency", bool(evidence.get("state_consistency_passed", False)), None),
        _check("scheduler_reliability", bool(evidence.get("scheduler_passed", False)), None),
        _check("data_provenance", bool(evidence.get("provenance_passed", False)), None),
        _check("no_safety_regression", bool(evidence.get("safety_passed", False)), None),
    ]
    passed = all(bool(item["passed"]) for item in checks)
    return {
        "decision": "GO_PENDING_MANUAL_APPROVAL" if passed else "NO_GO",
        "checks": checks,
        "failed_checks": [item["code"] for item in checks if not item["passed"]],
        "manual_approval_required": True,
        "production_change_allowed": False,
        "auto_promotion_allowed": False,
        "training_enable_change_allowed": False,
    }


def _check(code: str, passed: bool, observed: object) -> dict[str, object]:
    return {"code": code, "passed": passed, "observed": observed}


def _int(value: object) -> int:
    if not isinstance(value, (str, int, float, bool)):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: object) -> float:
    if not isinstance(value, (str, int, float, bool)):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
