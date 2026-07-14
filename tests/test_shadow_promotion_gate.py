from stock_analyzer.research.promotion_gate import evaluate_shadow_promotion


def _passing_evidence() -> dict[str, object]:
    return {
        "matured_samples": 100,
        "trading_days": 20,
        "baseline_precision": 0.60,
        "candidate_precision": 0.63,
        "baseline_max_drawdown": 0.10,
        "candidate_max_drawdown": 0.12,
        "probability_healthy": True,
        "coverage_passed": True,
        "stability_passed": True,
        "time_split_replay_passed": True,
        "state_consistency_passed": True,
        "scheduler_passed": True,
        "provenance_passed": True,
        "safety_passed": True,
    }


def test_shadow_promotion_requires_manual_approval_even_after_go() -> None:
    report = evaluate_shadow_promotion(_passing_evidence())

    assert report["decision"] == "GO_PENDING_MANUAL_APPROVAL"
    assert report["manual_approval_required"] is True
    assert report["production_change_allowed"] is False


def test_shadow_promotion_is_no_go_before_minimum_observation_window() -> None:
    evidence = _passing_evidence()
    evidence.update({"matured_samples": 99, "trading_days": 19})
    report = evaluate_shadow_promotion(evidence)

    assert report["decision"] == "NO_GO"
    assert report["failed_checks"][:2] == [
        "matured_samples_gte_100",
        "trading_days_gte_20",
    ]


def test_shadow_promotion_accepts_exact_decimal_boundaries() -> None:
    evidence = _passing_evidence()
    evidence.update(
        {
            "baseline_precision": 0.26,
            "candidate_precision": 0.29,
            "baseline_max_drawdown": 0.13,
            "candidate_max_drawdown": 0.15,
        }
    )

    report = evaluate_shadow_promotion(evidence)

    assert report["decision"] == "GO_PENDING_MANUAL_APPROVAL"
