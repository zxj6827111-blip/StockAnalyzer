from __future__ import annotations

from stock_analyzer.evolution.modules.m4_capital_flow import evaluate_m4_capital_flow


def test_m4_capital_flow_inflow_dominant_scores_high() -> None:
    result = evaluate_m4_capital_flow(
        records=[
            {"open": 10.0, "close": 10.5, "volume": 1_000_000},
            {"open": 8.0, "close": 8.2, "volume": 800_000},
            {"open": 6.0, "close": 6.2, "volume": 600_000},
        ]
    )
    assert result.status == "inflow_dominant"
    assert result.score > 50.0
    assert result.metrics.valid_symbols == 3


def test_m4_capital_flow_outflow_dominant_scores_low() -> None:
    result = evaluate_m4_capital_flow(
        records=[
            {"open": 10.0, "close": 9.5, "volume": 1_000_000},
            {"open": 8.0, "close": 7.7, "volume": 800_000},
            {"open": 6.0, "close": 5.8, "volume": 600_000},
        ]
    )
    assert result.status == "outflow_dominant"
    assert result.score < 50.0
    assert result.metrics.outflow_symbols == 3


def test_m4_capital_flow_no_data_returns_neutral() -> None:
    result = evaluate_m4_capital_flow(records=[{"open": 0.0, "close": 0.0, "volume": 0.0}])
    assert result.status == "no_data"
    assert result.score == 50.0
