from __future__ import annotations

from datetime import datetime

from stock_analyzer.evolution.reconcile_drift import evaluate_daily_reconcile_drift


def _as_float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        try:
            return float(text)
        except ValueError:
            return 0.0
    return 0.0


def test_reconcile_drift_computes_distribution_and_ratio() -> None:
    result = evaluate_daily_reconcile_drift(
        records=[
            {
                "symbol": "600000.SH",
                "trade_date": "2026-03-05",
                "target_weight": 0.20,
                "filled_weight": 0.18,
                "end_of_day_position_weight": 0.17,
            },
            {
                "symbol": "000001.SZ",
                "trade_date": "2026-03-05",
                "target_weight": 0.15,
                "filled_weight": 0.14,
                "end_of_day_position_weight": 0.13,
            },
        ],
        now=datetime.fromisoformat("2026-03-05T20:40:00"),
        model_bundle_hash="bundle-test",
        position_drift_alert_threshold=0.02,
        position_drift_raise_u_threshold_bp=20,
        position_drift_consecutive_days_trigger=3,
        previous_state={},
    )
    report = result.report
    assert report["valid_rows"] == 2
    assert abs(_as_float(report["position_drift_ratio"]) - 0.025) < 1e-9
    assert report["position_drift_alert"] is True
    assert report["position_drift_consecutive_days"] == 1
    assert report["raise_u_threshold_bp_recommendation"] == 0
    assert report["reconcile_record_id"] == "2026-03-05:bundle-test"


def test_reconcile_drift_recommends_raise_u_threshold_after_consecutive_breaches() -> None:
    result = evaluate_daily_reconcile_drift(
        records=[
            {
                "symbol": "600000.SH",
                "trade_date": "2026-03-07",
                "target_weight": 0.22,
                "filled_weight": 0.20,
                "end_of_day_position_weight": 0.16,
            }
        ],
        now=datetime.fromisoformat("2026-03-07T20:40:00"),
        model_bundle_hash="bundle-test",
        position_drift_alert_threshold=0.01,
        position_drift_raise_u_threshold_bp=20,
        position_drift_consecutive_days_trigger=3,
        previous_state={"position_drift_consecutive_days": 2},
    )
    report = result.report
    assert report["position_drift_alert"] is True
    assert report["position_drift_consecutive_days"] == 3
    assert report["raise_u_threshold_bp_recommendation"] == 20
