from __future__ import annotations

from collections.abc import Sequence

from stock_analyzer.evolution.utility_execution import evaluate_utility_execution_policy


def _as_int(value: object) -> int:
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


def _as_float(value: object) -> float:
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    return float(value)


def _as_text(value: object) -> str:
    assert isinstance(value, str)
    return value


def _as_text_list(value: object) -> list[str]:
    assert isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    items = [_as_text(item) for item in value]
    assert len(items) == len(value)
    return items


def test_utility_execution_falls_back_and_adjusts_dynamic_k_when_constraints_pressured() -> None:
    result = evaluate_utility_execution_policy(
        records=[
            {
                "symbol": "600000.SH",
                "mapping_level_used": "regime_x_liquidity_x_volatility",
                "bucket_sample_count": 120,
                "sparse_history_flag": True,
                "turnover_ratio": 1.6,
                "own_participation_ratio": 0.020,
                "utility_bp": 25.0,
            },
            {
                "symbol": "000001.SZ",
                "mapping_level_used": "regime_x_liquidity_x_volatility",
                "bucket_sample_count": 140,
                "sparse_history_flag": False,
                "turnover_ratio": 1.2,
                "own_participation_ratio": 0.012,
                "utility_bp": -5.0,
            },
        ],
        min_samples_per_bucket=300,
        mapping_fallback_order=[
            "regime_x_liquidity_x_volatility",
            "regime_x_liquidity",
            "regime",
            "global",
        ],
        mapping_update_cooldown_days=3,
        mapping_ema_alpha=0.30,
        k_base=20,
        k_min=8,
        turnover_limit=1.0,
        participation_cap=0.01,
        previous_state={},
    )
    report = result.report
    assert _as_text(report["mapping_level_used"]) in {"regime_x_liquidity", "regime", "global"}
    assert "fallback_due_bucket_min_samples" in _as_text_list(report["mapping_fallback_steps"])
    assert _as_float(report["k_dynamic"]) < _as_float(report["k_base"])
    trim_reason_codes = _as_text_list(report["trim_reason_codes"])
    assert "turnover_excess" in trim_reason_codes
    assert "capacity_excess" in trim_reason_codes
    assert _as_int(report["negative_u_filtered_count"]) >= 1


def test_utility_execution_holds_mapping_level_during_cooldown() -> None:
    result = evaluate_utility_execution_policy(
        records=[
            {
                "symbol": "600000.SH",
                "mapping_level_used": "regime_x_liquidity_x_volatility",
                "bucket_sample_count": 600,
                "utility_bp": 10.0,
            }
        ],
        min_samples_per_bucket=300,
        mapping_fallback_order=[
            "regime_x_liquidity_x_volatility",
            "regime_x_liquidity",
            "regime",
            "global",
        ],
        mapping_update_cooldown_days=3,
        mapping_ema_alpha=0.30,
        k_base=20,
        k_min=8,
        turnover_limit=1.0,
        participation_cap=0.01,
        previous_state={
            "active_mapping_level": "regime",
            "mapping_cooldown_remaining_days": 2,
            "mapping_expected_return_ema_bp": 5.0,
        },
    )
    report = result.report
    assert _as_text(report["mapping_level_used"]) == "regime"
    assert "cooldown_hold_previous_mapping_level" in _as_text_list(report["mapping_fallback_steps"])
    assert _as_int(report["mapping_cooldown_remaining_days"]) == 1
