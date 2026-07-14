from stock_analyzer.runtime.services.market_sync_service import (
    classify_market_warehouse_health,
)


def test_market_warehouse_health_classifies_one_point_four_six_percent_as_degraded() -> None:
    health = classify_market_warehouse_health(
        final_failed=146,
        target_total=10_000,
        core_covered=100,
        core_total=100,
    )

    assert health["grade"] == "degraded"
    assert health["final_failure_rate"] == 0.0146


def test_market_warehouse_health_marks_low_core_coverage_critical() -> None:
    health = classify_market_warehouse_health(
        final_failed=0,
        target_total=10_000,
        core_covered=94,
        core_total=100,
    )

    assert health["grade"] == "critical"
