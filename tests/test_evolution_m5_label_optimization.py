from __future__ import annotations

from stock_analyzer.evolution.modules.m5_label_optimization import (
    build_m5_strategy_linkage,
    evaluate_m5_label_optimization,
)


def test_m5_label_optimization_detects_optimized_state() -> None:
    result = evaluate_m5_label_optimization(
        records=[
            {
                "open": 10.0,
                "close": 10.3,
                "label": 1,
                "label_seed_1": 1,
                "label_seed_2": 1,
            },
            {
                "open": 9.8,
                "close": 9.6,
                "label": 0,
                "label_seed_1": 0,
                "label_seed_2": 0,
            },
            {
                "open": 8.0,
                "close": 8.2,
                "label": 1,
                "label_seed_1": 1,
                "label_seed_2": 1,
            },
            {
                "open": 7.2,
                "close": 7.0,
                "label": 0,
                "label_seed_1": 0,
                "label_seed_2": 0,
            },
            {
                "open": 6.0,
                "close": 6.2,
                "label": 1,
                "label_seed_1": 1,
                "label_seed_2": 1,
            },
            {
                "open": 5.5,
                "close": 5.2,
                "label": 0,
                "label_seed_1": 0,
                "label_seed_2": 0,
            },
        ]
    )
    assert result.status == "optimized"
    assert result.score >= 75.0
    assert result.metrics.label_coverage_ratio == 1.0
    assert result.metrics.positive_label_ratio == 0.5


def test_m5_label_optimization_detects_degraded_state_on_extreme_skew() -> None:
    result = evaluate_m5_label_optimization(
        records=[
            {
                "open": 10.0,
                "close": 9.6,
                "label": 1,
                "label_seed_1": 1,
                "label_seed_2": 0,
            },
            {
                "open": 9.5,
                "close": 9.0,
                "label": 1,
                "label_seed_1": 1,
                "label_seed_2": 0,
            },
            {
                "open": 8.8,
                "close": 8.1,
                "label": 1,
                "label_seed_1": 1,
                "label_seed_2": 0,
            },
            {
                "open": 7.2,
                "close": 6.9,
                "label": 1,
                "label_seed_1": 1,
                "label_seed_2": 0,
            },
            {
                "open": 6.3,
                "close": 6.0,
                "label": 1,
                "label_seed_1": 1,
                "label_seed_2": 0,
            },
        ]
    )
    assert result.status == "degraded"
    assert result.score < 60.0
    assert result.metrics.positive_label_ratio >= 0.9


def test_m5_label_optimization_limited_observability_without_labels() -> None:
    result = evaluate_m5_label_optimization(
        records=[
            {"open": 10.0, "close": 10.1},
            {"open": 9.0, "close": 8.9},
            {"open": 8.0, "close": 8.1},
        ]
    )
    assert result.status == "limited_observability"
    assert result.score == 62.0
    assert result.metrics.labeled_samples == 0


def test_m5_label_optimization_no_data_returns_neutral() -> None:
    result = evaluate_m5_label_optimization(
        records=[{"open": 0.0, "close": 0.0, "label": 1}]
    )
    assert result.status == "no_data"
    assert result.score == 50.0
    assert result.metrics.valid_symbols == 0


def test_m5_strategy_linkage_escalates_degraded_result_to_label_change_keys() -> None:
    result = evaluate_m5_label_optimization(
        records=[
            {"open": 10.0, "close": 9.5, "label": 1, "label_seed_1": 1, "label_seed_2": 0},
            {"open": 9.5, "close": 9.1, "label": 1, "label_seed_1": 1, "label_seed_2": 0},
            {"open": 9.0, "close": 8.6, "label": 1, "label_seed_1": 1, "label_seed_2": 0},
            {"open": 8.5, "close": 8.2, "label": 1, "label_seed_1": 1, "label_seed_2": 0},
            {"open": 8.0, "close": 7.6, "label": 1, "label_seed_1": 1, "label_seed_2": 0},
            {"open": 7.5, "close": 7.2, "label": 1, "label_seed_1": 1, "label_seed_2": 0},
        ]
    )
    linkage = build_m5_strategy_linkage(result=result, min_labeled_samples=5)
    assert linkage.mode == "propose_label_tuning"
    assert linkage.change_keys
    assert any("label" in key for key in linkage.change_keys)


def test_m5_strategy_linkage_stays_observe_only_with_insufficient_samples() -> None:
    result = evaluate_m5_label_optimization(
        records=[
            {"open": 10.0, "close": 10.1, "label": 1},
            {"open": 9.9, "close": 9.8, "label": 0},
        ]
    )
    linkage = build_m5_strategy_linkage(result=result, min_labeled_samples=5)
    assert linkage.mode == "observe_only"
    assert linkage.change_keys == []
