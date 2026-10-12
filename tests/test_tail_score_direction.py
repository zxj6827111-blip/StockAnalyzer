from __future__ import annotations

import pytest

from stock_analyzer.research.tail_score_direction import (
    bootstrap_direction_difference,
    compare_score_directions,
    select_directional_top_k,
    summarize_picks,
)


def test_directional_top_k_uses_the_same_pool_and_stable_symbol_ties() -> None:
    rows = [{"symbol": "600003"}, {"symbol": "600001"}, {"symbol": "600002"}]
    selected = select_directional_top_k(
        rows,
        {"600003": 0.9, "600001": 0.1, "600002": 0.1},
        cap=2,
    )
    assert selected == {"high": ["600003", "600001"], "low": ["600001", "600002"]}


def test_directional_top_k_rejects_missing_duplicate_and_nonfinite_scores() -> None:
    try:
        select_directional_top_k([{"symbol": "600001"}], {}, cap=1)
    except ValueError as exc:
        assert "score missing" in str(exc)
    else:
        raise AssertionError("missing score must fail closed")

    try:
        select_directional_top_k(
            [{"symbol": "600001"}, {"symbol": "600001"}],
            {"600001": 0.5},
        )
    except ValueError as exc:
        assert "duplicate symbol" in str(exc)
    else:
        raise AssertionError("duplicate symbol must fail closed")

    try:
        select_directional_top_k([{"symbol": "600001"}], {"600001": float("nan")})
    except ValueError as exc:
        assert "not finite" in str(exc)
    else:
        raise AssertionError("non-finite score must fail closed")


def test_unmatured_top_pick_is_not_replaced_or_counted_as_a_matured_fill() -> None:
    pool = [
        {"symbol": "600001", "trainable": False, "filled": False, "net_return": None},
        {"symbol": "600002", "trainable": True, "filled": True, "net_return": 0.02},
    ]
    summary = summarize_picks(pool, ["600001"])
    assert summary["symbols"] == ["600001"]
    assert summary["recommendations"] == 1
    assert summary["fills"] == 0
    assert summary["matured_fills"] == 0
    assert summary["net_profit_rate"] is None


def _daily_rows(days: int = 24) -> list[dict]:
    result = []
    for index in range(days):
        high_return = 0.0
        low_return = 0.01
        result.append({
            "trade_date": f"2025-01-{index + 1:02d}",
            "fold_id": 1 if index < days // 2 else 2,
            "session_index": index,
            "high": {
                "recommendations": 1, "fills": 1, "matured_fills": 1,
                "net_profits": int(high_return > 0), "net_return_sum": high_return,
            },
            "low": {
                "recommendations": 1, "fills": 1, "matured_fills": 1,
                "net_profits": int(low_return > 0), "net_return_sum": low_return,
            },
        })
    return result


def test_direction_bootstrap_is_reproducible_and_reports_low_minus_high() -> None:
    daily = _daily_rows()
    first = bootstrap_direction_difference(
        daily, metric="mean_net_return", block_length=10, draws=300, seed=20261010
    )
    second = bootstrap_direction_difference(
        daily, metric="mean_net_return", block_length=10, draws=300, seed=20261010
    )
    assert first == second
    assert first["status"] == "completed"
    assert first["effect_estimate"] == pytest.approx(0.01)
    assert first["ci_low"] == pytest.approx(0.01)
    assert first["ci_high"] == pytest.approx(0.01)


def test_score_direction_comparison_keeps_short_sensitivity_block_blocked() -> None:
    comparison = compare_score_directions(_daily_rows(), draws=300)
    assert comparison["status"] == "completed"
    assert comparison["low_score"]["net_profit_rate"] == 1.0
    assert comparison["high_score"]["net_profit_rate"] == 0.0
    assert comparison["metrics"]["net_profit_rate"]["holm_p_value"] <= 1.0
    assert comparison["metrics"]["mean_net_return"]["bootstrap"]["20"]["status"] == "blocked"
    assert comparison["release_eligible"] is False


def test_direction_bootstrap_blocks_when_a_resample_has_no_matured_fills() -> None:
    daily = _daily_rows()
    for row in daily:
        row["high"].update({"matured_fills": 0, "net_profits": 0, "net_return_sum": 0.0})
        row["low"].update({"matured_fills": 0, "net_profits": 0, "net_return_sum": 0.0})
    result = bootstrap_direction_difference(
        daily, metric="net_profit_rate", block_length=10, draws=30
    )
    assert result["status"] == "blocked"
    assert result["blockers"] == ["bootstrap_replicate_has_no_matured_fills_in_an_arm"]
