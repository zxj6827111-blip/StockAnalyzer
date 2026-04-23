from __future__ import annotations

from stock_analyzer.evolution.execution_sensitivity import evaluate_execution_sensitivity


def test_execution_sensitivity_alert_requires_consecutive_breaches() -> None:
    records = [
        {
            "symbol": "600000.SH",
            "vwap_proxy_open": 10.05,
            "vwap_proxy_day": 10.00,
        }
    ]
    result1 = evaluate_execution_sensitivity(
        records=records,
        sensitivity_threshold_bp=30.0,
        sensitivity_days=3,
        previous_breach_history=[],
    )
    assert result1.run_breach is True
    assert result1.execution_sensitivity_alert is False
    assert result1.consecutive_breach_days == 1

    result2 = evaluate_execution_sensitivity(
        records=records,
        sensitivity_threshold_bp=30.0,
        sensitivity_days=3,
        previous_breach_history=result1.breach_history,
    )
    assert result2.execution_sensitivity_alert is False
    assert result2.consecutive_breach_days == 2

    result3 = evaluate_execution_sensitivity(
        records=records,
        sensitivity_threshold_bp=30.0,
        sensitivity_days=3,
        previous_breach_history=result2.breach_history,
    )
    assert result3.execution_sensitivity_alert is True
    assert result3.consecutive_breach_days == 3
    assert result3.worst_symbol == "600000.SH"
