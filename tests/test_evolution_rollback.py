from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from stock_analyzer.evolution.governance.rollback import (
    RollbackContext,
    RollbackState,
    evaluate_rollback,
    tracking_error_z_score,
)


def test_tracking_error_z_score_uses_std_floor_under_zero_variance() -> None:
    z_score, std_floor = tracking_error_z_score(
        diff_returns=[-0.002, -0.002, -0.002],
        shadow_champion_vol=0.0,
    )
    assert std_floor == pytest.approx(0.001)
    assert z_score == pytest.approx(-2.0)


def test_evaluate_rollback_stays_stable_for_micro_tuning() -> None:
    assessment = evaluate_rollback(
        diff_returns=[0.0002, 0.0001, 0.0002, 0.00015, 0.0001],
        shadow_champion_vol=0.02,
        context=RollbackContext(
            trade_count=30,
            observed_days=12,
            consecutive_soft_days=0,
            consecutive_hard_days=0,
        ),
    )
    assert assessment.state == RollbackState.STABLE
    assert assessment.reason == "within_tolerance"


def test_evaluate_rollback_timeout_triggers_post_rollback_actions() -> None:
    now = datetime(2026, 3, 1, tzinfo=UTC)
    assessment = evaluate_rollback(
        diff_returns=[-0.003] * 10,
        shadow_champion_vol=0.01,
        context=RollbackContext(
            trade_count=30,
            observed_days=15,
            consecutive_soft_days=5,
            consecutive_hard_days=5,
            pending_confirmation_since=now - timedelta(days=4),
        ),
        now=now,
    )
    assert assessment.state == RollbackState.ROLLED_BACK
    assert assessment.reason == "timeout_no_ack"
    assert len(assessment.post_rollback_actions) >= 1


def test_evaluate_rollback_hard_circuit_breaker_short_circuit() -> None:
    assessment = evaluate_rollback(
        diff_returns=[-0.0001, -0.0002],
        shadow_champion_vol=0.02,
        context=RollbackContext(trade_count=1, observed_days=1),
        hard_drawdown_breach=True,
    )
    assert assessment.state == RollbackState.ROLLED_BACK
    assert assessment.reason == "hard_circuit_breaker"


def test_evaluate_rollback_prefers_event_days_for_low_frequency_extension() -> None:
    """事件日口径优先：行数虚高不代表观察充分，事件日不足仍按低频延长处理。"""
    event_day_starved = evaluate_rollback(
        diff_returns=[-0.001] * 30,
        shadow_champion_vol=0.01,
        context=RollbackContext(
            trade_count=1,
            observed_days=30,
            observed_event_days=2,
        ),
    )
    assert event_day_starved.state == RollbackState.STABLE
    assert event_day_starved.reason == "low_frequency_extension"

    # 未提供事件日（0）时回退旧行数口径，保持非 M11 路径兼容。
    legacy_fallback = evaluate_rollback(
        diff_returns=[-0.001] * 30,
        shadow_champion_vol=0.01,
        context=RollbackContext(
            trade_count=1,
            observed_days=30,
            observed_event_days=0,
        ),
    )
    assert legacy_fallback.reason != "low_frequency_extension"
