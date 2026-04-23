from __future__ import annotations

from stock_analyzer.evolution.modules.m6_counterparty import evaluate_m6_counterparty


def test_m6_heavy_sell_pressure_detected() -> None:
    result = evaluate_m6_counterparty(
        records=[
            {"open": 10.0, "high": 10.8, "low": 9.4, "close": 9.6},
            {"open": 8.0, "high": 8.6, "low": 7.2, "close": 7.4},
            {"open": 6.0, "high": 6.5, "low": 5.2, "close": 5.3},
        ]
    )
    assert result.status == "heavy_sell_pressure"
    assert result.score < 50.0
    assert result.metrics.valid_symbols == 3


def test_m6_favorable_pressure_detected() -> None:
    result = evaluate_m6_counterparty(
        records=[
            {"open": 10.0, "high": 10.2, "low": 9.8, "close": 10.15},
            {"open": 8.0, "high": 8.2, "low": 7.9, "close": 8.1},
            {"open": 6.0, "high": 6.1, "low": 5.9, "close": 6.05},
        ]
    )
    assert result.status == "favorable"
    assert result.score > 60.0
    assert result.metrics.bearish_ratio < 0.45


def test_m6_no_data_returns_neutral() -> None:
    result = evaluate_m6_counterparty(
        records=[{"open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0}]
    )
    assert result.status == "no_data"
    assert result.score == 50.0
