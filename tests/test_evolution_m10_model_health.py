from __future__ import annotations

from stock_analyzer.evolution.modules.m10_model_health import evaluate_m10_model_health


def test_m10_model_health_healthy_with_consistent_predictions() -> None:
    result = evaluate_m10_model_health(
        records=[
            {
                "open": 10.0,
                "close": 10.1,
                "p_lgbm": 0.62,
                "p_xgb": 0.60,
                "p_meta": 0.61,
            },
            {
                "open": 8.0,
                "close": 8.1,
                "p_lgbm": 0.55,
                "p_xgb": 0.57,
                "p_meta": 0.56,
            },
            {
                "open": 6.0,
                "close": 6.02,
                "p_lgbm": 0.48,
                "p_xgb": 0.50,
                "p_meta": 0.49,
            },
        ]
    )
    assert result.status == "healthy"
    assert result.score >= 70.0
    assert result.metrics.prediction_coverage_ratio == 1.0
    assert result.metrics.high_conflict_ratio == 0.0


def test_m10_model_health_degraded_when_conflict_is_high() -> None:
    result = evaluate_m10_model_health(
        records=[
            {
                "open": 10.0,
                "close": 10.2,
                "p_lgbm": 0.95,
                "p_xgb": 0.20,
                "p_meta": 0.85,
            },
            {
                "open": 12.0,
                "close": 11.7,
                "p_lgbm": 0.90,
                "p_xgb": 0.10,
                "p_meta": 0.80,
            },
            {
                "open": 8.0,
                "close": 8.1,
                "p_lgbm": 0.88,
                "p_xgb": 0.12,
                "p_meta": 0.78,
            },
        ]
    )
    assert result.status == "degraded"
    assert result.score < 70.0
    assert result.metrics.high_conflict_ratio >= 0.5


def test_m10_model_health_limited_observability_without_predictions() -> None:
    result = evaluate_m10_model_health(
        records=[
            {"open": 10.0, "close": 10.1},
            {"open": 8.0, "close": 7.9},
            {"open": 6.0, "close": 6.1},
        ]
    )
    assert result.status == "limited_observability"
    assert result.score >= 50.0
    assert result.metrics.prediction_coverage_ratio == 0.0


def test_m10_model_health_respects_custom_conflict_thresholds() -> None:
    result = evaluate_m10_model_health(
        records=[
            {
                "open": 10.0,
                "close": 10.1,
                "p_lgbm": 0.90,
                "p_xgb": 0.10,
                "p_meta": 0.50,
            },
            {
                "open": 8.0,
                "close": 8.1,
                "p_lgbm": 0.88,
                "p_xgb": 0.12,
                "p_meta": 0.50,
            },
        ],
        conflict_watch_ratio=0.90,
        conflict_degraded_ratio=1.10,
    )
    assert result.status == "watch"
