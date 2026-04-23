"""Utility execution mapping and dynamic-K policy."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypedDict


@dataclass(slots=True)
class UtilityExecutionResult:
    report: dict[str, object]
    state: dict[str, object]


class MappingLevelResolution(TypedDict):
    level: str
    bucket_sample_count: int
    fallback_steps: list[str]


def evaluate_utility_execution_policy(
    *,
    records: Sequence[Mapping[str, object]],
    min_samples_per_bucket: int,
    mapping_fallback_order: Sequence[str],
    mapping_update_cooldown_days: int,
    mapping_ema_alpha: float,
    k_base: int,
    k_min: int,
    turnover_limit: float,
    participation_cap: float,
    previous_state: Mapping[str, object] | None = None,
) -> UtilityExecutionResult:
    state = dict(previous_state or {})
    fallback_order = _normalize_fallback_order(mapping_fallback_order)
    min_bucket = max(1, int(min_samples_per_bucket))
    cooldown_days = max(1, int(mapping_update_cooldown_days))
    ema_alpha = min(1.0, max(0.0, float(mapping_ema_alpha)))

    level_counter: dict[str, int] = {}
    sample_counts: list[int] = []
    fallback_steps: list[str] = []
    turnover_values: list[float] = []
    participation_values: list[float] = []
    utility_values: list[float] = []

    for item in records:
        parsed = _resolve_mapping_level(
            item=item,
            min_samples_per_bucket=min_bucket,
            fallback_order=fallback_order,
        )
        level_counter[parsed["level"]] = level_counter.get(parsed["level"], 0) + 1
        sample_counts.append(parsed["bucket_sample_count"])
        fallback_steps.extend(parsed["fallback_steps"])

        turnover_ratio = _as_float(item.get("turnover_ratio"), default=-1.0)
        if turnover_ratio >= 0.0:
            turnover_values.append(turnover_ratio)
        own_participation = _as_float(
            item.get("own_participation_ratio", item.get("participation_ratio")),
            default=-1.0,
        )
        if own_participation >= 0.0:
            participation_values.append(own_participation)
        utility_bp = _resolve_utility_bp(item=item)
        utility_values.append(utility_bp)

    candidate_level = _select_dominant_level(
        level_counter=level_counter,
        fallback_order=fallback_order,
    )
    prev_level = _normalize_level(state.get("active_mapping_level"), fallback_order=fallback_order)
    cooldown_remaining = max(0, _as_int(state.get("mapping_cooldown_remaining_days"), default=0))
    if not candidate_level:
        candidate_level = prev_level or fallback_order[0]

    if prev_level and candidate_level != prev_level and cooldown_remaining > 0:
        mapping_level_used = prev_level
        fallback_steps.append("cooldown_hold_previous_mapping_level")
        cooldown_remaining -= 1
    else:
        mapping_level_used = candidate_level
        if prev_level and mapping_level_used != prev_level:
            cooldown_remaining = max(0, cooldown_days - 1)
            fallback_steps.append("mapping_level_switch")
        elif cooldown_remaining > 0:
            cooldown_remaining -= 1

    raw_expected_return_bp = (
        float(sum(utility_values) / len(utility_values)) if utility_values else 0.0
    )
    prev_ema = _as_float(
        state.get("mapping_expected_return_ema_bp"),
        default=raw_expected_return_bp,
    )
    ema_expected_return_bp = (
        ema_alpha * raw_expected_return_bp + (1.0 - ema_alpha) * prev_ema
    )

    turnover = float(sum(turnover_values) / len(turnover_values)) if turnover_values else 0.0
    participation = max(participation_values) if participation_values else 0.0
    safe_turnover_limit = max(1e-9, float(turnover_limit))
    safe_participation_cap = max(1e-9, float(participation_cap))
    turnover_excess_ratio = max(0.0, turnover / safe_turnover_limit - 1.0)
    capacity_excess_ratio = max(0.0, participation / safe_participation_cap - 1.0)
    constraint_pressure = max(turnover_excess_ratio, capacity_excess_ratio)
    alpha = min(1.0, max(0.4, 1.0 - constraint_pressure))
    safe_k_base = max(1, int(k_base))
    safe_k_min = max(1, min(int(k_min), safe_k_base))
    k_dynamic = max(safe_k_min, int(math.floor(safe_k_base * alpha)))

    trim_reason_codes: list[str] = []
    if k_dynamic < safe_k_base:
        if turnover_excess_ratio > 0.0:
            trim_reason_codes.append("turnover_excess")
        if capacity_excess_ratio > 0.0:
            trim_reason_codes.append("capacity_excess")
        if not trim_reason_codes:
            trim_reason_codes.append("constraint_pressure")

    negative_u_filtered_count = sum(1 for value in utility_values if value <= 0.0)
    ordered_fallback_steps = _dedupe_strings(fallback_steps)

    report = {
        "mapping_level_used": mapping_level_used,
        "bucket_sample_count": min(sample_counts) if sample_counts else 0,
        "mapping_fallback_steps": ordered_fallback_steps,
        "min_samples_per_bucket": min_bucket,
        "mapping_update_cooldown_days": cooldown_days,
        "mapping_cooldown_remaining_days": cooldown_remaining,
        "mapping_ema_alpha": round(ema_alpha, 6),
        "mapping_expected_return_raw_bp": round(raw_expected_return_bp, 6),
        "mapping_expected_return_ema_bp": round(ema_expected_return_bp, 6),
        "k_base": safe_k_base,
        "k_dynamic": k_dynamic,
        "k_min": safe_k_min,
        "turnover_excess_ratio": round(turnover_excess_ratio, 6),
        "capacity_excess_ratio": round(capacity_excess_ratio, 6),
        "constraint_pressure": round(constraint_pressure, 6),
        "alpha": round(alpha, 6),
        "trim_reason_codes": trim_reason_codes,
        "negative_u_filtered_count": negative_u_filtered_count,
    }
    state_out = {
        "active_mapping_level": mapping_level_used,
        "mapping_cooldown_remaining_days": cooldown_remaining,
        "mapping_expected_return_ema_bp": round(ema_expected_return_bp, 6),
    }
    return UtilityExecutionResult(report=report, state=state_out)


def _resolve_mapping_level(
    *,
    item: Mapping[str, object],
    min_samples_per_bucket: int,
    fallback_order: Sequence[str],
) -> MappingLevelResolution:
    sparse_history_flag = bool(item.get("sparse_history_flag", False))
    has_bucket_sample_count = "bucket_sample_count" in item
    bucket_sample_count = max(
        0,
        _as_int(
            item.get("bucket_sample_count"),
            default=(min_samples_per_bucket if not has_bucket_sample_count else 0),
        ),
    )
    initial = _normalize_level(item.get("mapping_level_used"), fallback_order=fallback_order)
    if not initial:
        initial = "regime_x_liquidity" if sparse_history_flag else fallback_order[0]

    steps: list[str] = []
    selected = initial
    if sparse_history_flag and selected == "regime_x_liquidity_x_volatility":
        selected = "regime_x_liquidity"
        steps.append("fallback_to_regime_x_liquidity")

    while has_bucket_sample_count and bucket_sample_count < min_samples_per_bucket:
        next_level = _next_fallback_level(level=selected, fallback_order=fallback_order)
        if not next_level or next_level == selected:
            break
        selected = next_level
        steps.append("fallback_due_bucket_min_samples")
    raw_steps = item.get("mapping_fallback_steps")
    if isinstance(raw_steps, list):
        for raw in raw_steps:
            if isinstance(raw, str) and raw.strip():
                steps.append(raw.strip())
    return {
        "level": selected,
        "bucket_sample_count": bucket_sample_count,
        "fallback_steps": steps,
    }


def _next_fallback_level(*, level: str, fallback_order: Sequence[str]) -> str:
    try:
        index = list(fallback_order).index(level)
    except ValueError:
        return ""
    if index >= len(fallback_order) - 1:
        return ""
    return fallback_order[index + 1]


def _select_dominant_level(
    *,
    level_counter: Mapping[str, int],
    fallback_order: Sequence[str],
) -> str:
    if not level_counter:
        return ""
    max_count = max(level_counter.values())
    tied = [level for level, count in level_counter.items() if count == max_count]
    for level in fallback_order:
        if level in tied:
            return level
    return tied[0]


def _normalize_fallback_order(raw_order: Sequence[str]) -> list[str]:
    defaults = ["regime_x_liquidity_x_volatility", "regime_x_liquidity", "regime", "global"]
    normalized: list[str] = []
    for item in raw_order:
        normalized_level = _normalize_level(item, fallback_order=defaults)
        if normalized_level and normalized_level not in normalized:
            normalized.append(normalized_level)
    for item in defaults:
        if item not in normalized:
            normalized.append(item)
    return normalized


def _normalize_level(value: object, *, fallback_order: Sequence[str]) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip().lower()
    if text in fallback_order:
        return text
    if text in {"regime_liquidity_volatility", "regime_liquidity_vol"}:
        return "regime_x_liquidity_x_volatility"
    if text in {"regime_liquidity"}:
        return "regime_x_liquidity"
    return ""


def _resolve_utility_bp(*, item: Mapping[str, object]) -> float:
    explicit = _as_float(item.get("utility_bp"), default=float("nan"))
    if math.isfinite(explicit):
        return explicit
    expected_return_bp = _as_float(item.get("expected_return_bp"), default=float("nan"))
    if math.isfinite(expected_return_bp):
        return expected_return_bp
    p_meta = _as_float(item.get("p_meta", item.get("p_final")), default=0.5)
    return (p_meta - 0.5) * 200.0


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


def _dedupe_strings(values: Sequence[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = item.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    return deduped
