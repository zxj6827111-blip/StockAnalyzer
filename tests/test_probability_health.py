from __future__ import annotations

import math

from stock_analyzer.models.probability_health import (
    ProbabilityHealthMonitor,
    sanitize_probabilities,
)


def test_probability_health_marks_constant_model_unhealthy_after_three_batches() -> None:
    monitor = ProbabilityHealthMonitor(window_size=20, abnormal_batches_required=3)
    report: dict[str, object] = {}
    for _ in range(22):
        report = monitor.observe({"lgbm": 0.2642, "xgb": 0.4, "meta": 0.5})

    assert report["status"] == "model_probability_unhealthy"
    assert "lgbm" in report["unhealthy_models"]
    assert report["promotion_allowed"] is False


def test_probability_health_rejects_non_finite_and_sanitizes_for_shadow_logging() -> None:
    monitor = ProbabilityHealthMonitor()
    report = monitor.observe({"lgbm": math.nan, "xgb": 0.7, "meta": 0.6})
    normalized = sanitize_probabilities({"lgbm": math.nan, "xgb": 1.2, "meta": -0.2})

    assert report["status"] == "model_probability_unhealthy"
    assert normalized == {"lgbm": 0.5, "xgb": 1.0, "meta": 0.0}
