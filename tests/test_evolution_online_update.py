from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from stock_analyzer.evolution.online_samples import build_online_sample_audit
from stock_analyzer.evolution.online_update import run_online_partial_fit_policy


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


def _as_mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _records(count: int = 3) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for idx in range(count):
        records.append(
            {
                "symbol": f"6000{idx:02d}.SH",
                "trade_date": f"2026-03-0{idx + 1}",
                "label_mature_time": "2026-03-05T15:00:00",
                "liquidity_tier": "mid",
            }
        )
    return records


def test_online_update_policy_caps_samples_by_daily_budget() -> None:
    now = datetime.fromisoformat("2026-03-06T20:40:00")
    records = _records(count=3)
    audit = build_online_sample_audit(records=records, now=now)
    result = run_online_partial_fit_policy(
        records=records,
        now=now,
        sample_audit=audit,
        m10_status="healthy",
        latency_block_online_update=False,
        max_online_samples_per_day=2,
        cooldown_days=3,
        promotion_min_healthy_days=3,
        previous_state={},
    )
    report = result.report
    assert _as_text(report["status"]) == "updated"
    assert _as_int(report["online_samples_used"]) == 2
    assert abs(_as_float(report["online_update_budget_ratio"]) - 1.0) < 1e-9
    assert _as_int(report["online_samples_skipped"]) >= 1


def test_online_update_policy_blocks_when_latency_or_degraded() -> None:
    now = datetime.fromisoformat("2026-03-06T20:40:00")
    records = _records(count=2)
    audit = build_online_sample_audit(records=records, now=now)
    result = run_online_partial_fit_policy(
        records=records,
        now=now,
        sample_audit=audit,
        m10_status="degraded",
        latency_block_online_update=True,
        max_online_samples_per_day=10,
        cooldown_days=3,
        promotion_min_healthy_days=3,
        previous_state={},
    )
    report = result.report
    assert _as_text(report["status"]) == "blocked"
    assert _as_int(report["online_samples_used"]) == 0
    reasons = _as_text_list(report["blocked_reasons"])
    assert "latency_block_online_update" in reasons
    assert "m10_status:degraded" in reasons


def test_online_update_policy_triggers_and_honors_cooldown() -> None:
    now = datetime.fromisoformat("2026-03-06T20:40:00")
    records = _records(count=1)
    audit = build_online_sample_audit(records=records, now=now)
    first = run_online_partial_fit_policy(
        records=records,
        now=now,
        sample_audit=audit,
        m10_status="degraded",
        latency_block_online_update=False,
        max_online_samples_per_day=10,
        cooldown_days=3,
        promotion_min_healthy_days=3,
        previous_state={},
    )
    second = run_online_partial_fit_policy(
        records=records,
        now=now,
        sample_audit=audit,
        m10_status="degraded",
        latency_block_online_update=False,
        max_online_samples_per_day=10,
        cooldown_days=3,
        promotion_min_healthy_days=3,
        previous_state=first.state,
    )
    third = run_online_partial_fit_policy(
        records=records,
        now=now,
        sample_audit=audit,
        m10_status="healthy",
        latency_block_online_update=False,
        max_online_samples_per_day=10,
        cooldown_days=3,
        promotion_min_healthy_days=3,
        previous_state=second.state,
    )
    assert _as_text(second.report["status"]) == "blocked"
    assert "cooldown_active" in _as_text_list(second.report["blocked_reasons"])
    assert _as_text(third.report["status"]) == "blocked"
    assert "cooldown_active" in _as_text_list(third.report["blocked_reasons"])


def test_online_update_policy_revokes_tier_b_on_degraded() -> None:
    now = datetime.fromisoformat("2026-03-06T20:40:00")
    records = _records(count=1)
    audit = build_online_sample_audit(records=records, now=now)
    result = run_online_partial_fit_policy(
        records=records,
        now=now,
        sample_audit=audit,
        m10_status="degraded",
        latency_block_online_update=False,
        max_online_samples_per_day=10,
        cooldown_days=3,
        promotion_min_healthy_days=3,
        previous_state={"tier_b_promoted": True},
    )
    report = result.report
    assert report["promotion_revoked"] is True
    assert report["tier_b_promoted"] is False
    assert "m10_status:degraded" in _as_text_list(report["revocation_reason_codes"])


def test_online_update_policy_holds_promotion_when_execution_or_reconcile_gate_fails() -> None:
    now = datetime.fromisoformat("2026-03-06T20:40:00")
    records = _records(count=2)
    audit = build_online_sample_audit(records=records, now=now)
    result = run_online_partial_fit_policy(
        records=records,
        now=now,
        sample_audit=audit,
        m10_status="healthy",
        latency_block_online_update=False,
        max_online_samples_per_day=10,
        cooldown_days=3,
        promotion_min_healthy_days=3,
        execution_promotion_gate={
            "passed": False,
            "reason_codes": ["trading_distribution_failed"],
        },
        reconcile_promotion_gate={
            "passed": False,
            "reason_codes": ["position_drift_alert"],
        },
        previous_state={"healthy_streak_days": 2},
    )
    report = result.report
    execution_gate = _as_mapping(report["execution_promotion_gate"])
    reconcile_gate = _as_mapping(report["reconcile_promotion_gate"])

    assert report["promotion_candidate"] is True
    assert report["promotion_gate_passed"] is False
    assert report["tier_b_promoted"] is False
    assert _as_text(report["promotion_decision"]) == "hold"
    assert "execution_trading_distribution_failed" in _as_text_list(
        report["promotion_reason_codes"]
    )
    assert "reconcile_position_drift_alert" in _as_text_list(report["promotion_reason_codes"])
    assert execution_gate["passed"] is False
    assert reconcile_gate["passed"] is False


def test_online_update_policy_revokes_tier_b_when_execution_gate_fails() -> None:
    now = datetime.fromisoformat("2026-03-06T20:40:00")
    records = _records(count=1)
    audit = build_online_sample_audit(records=records, now=now)
    result = run_online_partial_fit_policy(
        records=records,
        now=now,
        sample_audit=audit,
        m10_status="healthy",
        latency_block_online_update=False,
        max_online_samples_per_day=10,
        cooldown_days=3,
        promotion_min_healthy_days=3,
        execution_promotion_gate={
            "passed": False,
            "reason_codes": ["shadow_v2_signal_divergence_limit_breach"],
        },
        previous_state={"tier_b_promoted": True},
    )
    report = result.report
    assert report["promotion_revoked"] is True
    assert report["tier_b_promoted"] is False
    assert _as_text(report["promotion_decision"]) == "revoke"
    assert "execution_shadow_v2_signal_divergence_limit_breach" in _as_text_list(
        report["revocation_reason_codes"]
    )
