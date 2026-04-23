"""Online partial-fit execution policy for evolution runtime."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from stock_analyzer.evolution.online_samples import OnlineSampleAudit


@dataclass(slots=True)
class OnlineUpdatePolicyResult:
    report: Mapping[str, object]
    state: Mapping[str, object]


def run_online_partial_fit_policy(
    *,
    records: Sequence[Mapping[str, object]],
    now: datetime,
    sample_audit: OnlineSampleAudit,
    m10_status: str,
    latency_block_online_update: bool,
    max_online_samples_per_day: int,
    cooldown_days: int,
    promotion_min_healthy_days: int,
    execution_promotion_gate: Mapping[str, object] | None = None,
    reconcile_promotion_gate: Mapping[str, object] | None = None,
    previous_state: Mapping[str, object] | None = None,
) -> OnlineUpdatePolicyResult:
    state = dict(previous_state or {})
    cooldown_remaining = max(0, _as_int(state.get("cooldown_remaining_days"), default=0))
    consecutive_degraded = max(0, _as_int(state.get("consecutive_degraded_days"), default=0))
    healthy_streak = max(0, _as_int(state.get("healthy_streak_days"), default=0))
    tier_b_promoted = bool(state.get("tier_b_promoted", False))
    prev_snapshot_hash = str(state.get("snapshot_hash", "")).strip()
    normalized_execution_gate = _normalize_promotion_gate(
        gate=execution_promotion_gate,
        gate_name="execution",
    )
    normalized_reconcile_gate = _normalize_promotion_gate(
        gate=reconcile_promotion_gate,
        gate_name="reconcile",
    )
    promotion_gate_passed = bool(normalized_execution_gate["passed"]) and bool(
        normalized_reconcile_gate["passed"]
    )
    promotion_gate_reason_codes = _combine_promotion_gate_reason_codes(
        execution_gate=normalized_execution_gate,
        reconcile_gate=normalized_reconcile_gate,
    )

    blocked_reasons: list[str] = []
    is_degraded = m10_status in {"degraded", "limited_observability", "no_data"}
    if is_degraded:
        consecutive_degraded += 1
        healthy_streak = 0
    else:
        consecutive_degraded = 0

    if consecutive_degraded >= 2 and cooldown_remaining <= 0:
        cooldown_remaining = max(1, int(cooldown_days))
        blocked_reasons.append("cooldown_triggered_by_consecutive_degraded")

    if latency_block_online_update:
        blocked_reasons.append("latency_block_online_update")
    if m10_status not in {"healthy", "watch"}:
        blocked_reasons.append(f"m10_status:{m10_status}")
    if cooldown_remaining > 0:
        blocked_reasons.append("cooldown_active")

    block_online_update = len(blocked_reasons) > 0
    cap = max(1, int(max_online_samples_per_day))
    available_samples = max(0, int(sample_audit.online_samples_used))
    online_samples_used = min(available_samples, cap)
    if block_online_update:
        online_samples_used = 0

    online_samples_skipped = max(0, available_samples - online_samples_used)
    downweighted = 0
    if online_samples_used > 0:
        downweighted = _count_downweighted_samples(records=records, max_samples=online_samples_used)
        healthy_streak += 1
    else:
        healthy_streak = 0 if is_degraded else healthy_streak

    online_update_budget_ratio = online_samples_used / max(cap, 1)
    rollback_trigger_source = ""
    snapshot_hash = prev_snapshot_hash
    if online_samples_used > 0:
        try:
            material = {
                "t": now.isoformat(),
                "used": online_samples_used,
                "hash": sample_audit.online_samples_used_hash,
                "prev": prev_snapshot_hash,
            }
            snapshot_hash = hashlib.sha256(
                json.dumps(material, ensure_ascii=True, sort_keys=True).encode("utf-8")
            ).hexdigest()
        except Exception:
            snapshot_hash = prev_snapshot_hash
            rollback_trigger_source = "online_update_hash_failed"

    promotion_candidate = (
        (not tier_b_promoted)
        and (not block_online_update)
        and m10_status == "healthy"
        and online_samples_used > 0
    )
    promotion_decision = "hold"
    promotion_reason_codes: list[str] = []
    promotion_revoked = False
    revocation_reason_codes: list[str] = []
    if promotion_candidate and not promotion_gate_passed:
        promotion_reason_codes.extend(promotion_gate_reason_codes)
    elif promotion_candidate and healthy_streak >= max(1, int(promotion_min_healthy_days)):
        tier_b_promoted = True
        promotion_decision = "promote"
        promotion_reason_codes.append("healthy_streak_reached")
        promotion_reason_codes.append("execution_gate_passed")
        promotion_reason_codes.append("reconcile_gate_passed")
    elif promotion_candidate:
        promotion_reason_codes.append("healthy_streak_not_reached")

    if tier_b_promoted and (is_degraded or not promotion_gate_passed):
        tier_b_promoted = False
        promotion_revoked = True
        if is_degraded:
            revocation_reason_codes.append(f"m10_status:{m10_status}")
        if not promotion_gate_passed:
            revocation_reason_codes.extend(promotion_gate_reason_codes)
        promotion_decision = "revoke"

    if cooldown_remaining > 0:
        cooldown_remaining -= 1

    state_out = {
        "cooldown_remaining_days": cooldown_remaining,
        "consecutive_degraded_days": consecutive_degraded,
        "healthy_streak_days": healthy_streak,
        "tier_b_promoted": tier_b_promoted,
        "snapshot_hash": snapshot_hash,
    }
    report = {
        "status": (
            "blocked"
            if block_online_update
            else ("updated" if online_samples_used > 0 else "idle")
        ),
        "block_online_update": block_online_update,
        "blocked_reasons": blocked_reasons,
        "online_samples_used": online_samples_used,
        "online_samples_used_hash": sample_audit.online_samples_used_hash,
        "online_samples_skipped": (
            online_samples_skipped
            + sample_audit.skipped_not_matured
            + sample_audit.skipped_invalid
        ),
        "online_samples_downweighted": downweighted,
        "online_update_budget_ratio": round(online_update_budget_ratio, 6),
        "max_online_samples_per_day": cap,
        "cooldown_days": max(1, int(cooldown_days)),
        "cooldown_remaining_days": cooldown_remaining,
        "online_handoff_mode": "rebase_then_replay",
        "replay_diff_p_meta_p50": 0.0,
        "replay_diff_p_meta_p90": 0.0,
        "replay_diff_p_meta_max": 0.0,
        "replay_diff_turnover": 0.0,
        "online_handoff_warning": bool(rollback_trigger_source),
        "rollback_trigger_source": rollback_trigger_source,
        "tier_b_promoted": tier_b_promoted,
        "promotion_candidate": promotion_candidate,
        "promotion_gate_passed": promotion_gate_passed,
        "promotion_gate_reason_codes": _dedupe_strings(promotion_gate_reason_codes),
        "promotion_decision": promotion_decision,
        "promotion_reason_codes": _dedupe_strings(promotion_reason_codes),
        "promotion_revoked": promotion_revoked,
        "revocation_reason_codes": _dedupe_strings(revocation_reason_codes),
        "execution_promotion_gate": normalized_execution_gate,
        "reconcile_promotion_gate": normalized_reconcile_gate,
        "deterministic_order_fields": sample_audit.deterministic_order_fields,
        "deterministic_order_applied": sample_audit.deterministic_order_applied,
    }
    return OnlineUpdatePolicyResult(report=report, state=state_out)


def _count_downweighted_samples(
    *,
    records: Sequence[Mapping[str, object]],
    max_samples: int,
) -> int:
    count = 0
    sampled = 0
    for item in records:
        if sampled >= max_samples:
            break
        sampled += 1
        tier = str(item.get("liquidity_tier", "small")).strip().lower()
        if tier not in {"large", "mid", "small"}:
            tier = "small"
        own_participation = _as_float(item.get("own_participation_ratio"), default=0.0)
        realized_slippage = _as_float(item.get("realized_slippage_bp"), default=0.0)
        part_limit = {"large": 0.015, "mid": 0.010, "small": 0.005}[tier]
        slippage_limit = {"large": 8.0, "mid": 15.0, "small": 25.0}[tier]
        if own_participation > part_limit or realized_slippage > slippage_limit:
            count += 1
    return count


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return int(float(text))
        except ValueError:
            return default
    return default


def _as_float(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return float(text)
        except ValueError:
            return default
    return default


def _normalize_promotion_gate(
    *,
    gate: Mapping[str, object] | None,
    gate_name: str,
) -> dict[str, object]:
    if not isinstance(gate, Mapping):
        return {
            "name": gate_name,
            "passed": True,
            "reason_codes": [],
            "metrics": {},
        }

    raw_reasons = gate.get("reason_codes", [])
    reason_codes = (
        [str(item).strip() for item in raw_reasons if str(item).strip()]
        if isinstance(raw_reasons, list)
        else []
    )
    raw_metrics = gate.get("metrics", {})
    metrics = dict(raw_metrics) if isinstance(raw_metrics, Mapping) else {}
    return {
        "name": gate_name,
        "passed": bool(gate.get("passed", True)),
        "reason_codes": _dedupe_strings(reason_codes),
        "metrics": metrics,
    }


def _combine_promotion_gate_reason_codes(
    *,
    execution_gate: Mapping[str, object],
    reconcile_gate: Mapping[str, object],
) -> list[str]:
    combined: list[str] = []
    for gate_name, gate_payload in (
        ("execution", execution_gate),
        ("reconcile", reconcile_gate),
    ):
        if bool(gate_payload.get("passed", True)):
            continue
        raw_reasons = gate_payload.get("reason_codes", [])
        reason_codes = (
            [str(item).strip() for item in raw_reasons if str(item).strip()]
            if isinstance(raw_reasons, list)
            else []
        )
        if not reason_codes:
            combined.append(f"{gate_name}_gate_failed")
            continue
        combined.extend(f"{gate_name}_{reason}" for reason in reason_codes)
    return _dedupe_strings(combined)


def _dedupe_strings(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for item in values:
        normalized = str(item).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped
