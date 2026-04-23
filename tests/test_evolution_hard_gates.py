from __future__ import annotations

from stock_analyzer.evolution.hard_gates import evaluate_hard_gates


def _as_text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def test_hard_gates_pass_when_required_checks_are_satisfied() -> None:
    report = evaluate_hard_gates(
        module_details={
            "eval_profiles": {
                "trading_distribution_gate": {
                    "pass": True,
                    "reason_codes": [],
                }
            },
            "utility_execution": {
                "k_base": 20,
                "k_dynamic": 20,
                "trim_reason_codes": [],
                "bucket_sample_count": 300,
                "mapping_level_used": "regime_x_liquidity_x_volatility",
                "mapping_fallback_steps": [],
            },
            "reconcile_drift": {
                "valid_rows": 0,
            },
        },
        min_samples_per_bucket=300,
    )
    assert report["all_passed"] is True
    assert report["failed_gate_count"] == 0


def test_hard_gates_fail_when_trading_distribution_gate_fails() -> None:
    report = evaluate_hard_gates(
        module_details={
            "eval_profiles": {
                "trading_distribution_gate": {
                    "pass": False,
                    "reason_codes": ["no_fill_ratio_limit_breach"],
                }
            },
            "utility_execution": {
                "k_base": 20,
                "k_dynamic": 10,
                "trim_reason_codes": ["turnover_excess"],
                "bucket_sample_count": 300,
                "mapping_level_used": "regime_x_liquidity",
                "mapping_fallback_steps": [],
            },
            "reconcile_drift": {
                "valid_rows": 1,
                "target_vs_filled_weight_p50": 0.01,
                "target_vs_filled_weight_p90": 0.02,
                "target_vs_filled_weight_max": 0.03,
                "filled_vs_eod_weight_p50": 0.01,
                "filled_vs_eod_weight_p90": 0.02,
                "filled_vs_eod_weight_max": 0.03,
                "position_drift_ratio": 0.03,
            },
        },
        min_samples_per_bucket=300,
    )
    assert report["all_passed"] is False
    assert "trading_fill_distribution" in _as_text_list(report["failed_gates"])
