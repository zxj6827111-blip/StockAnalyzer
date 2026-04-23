"""Dual evaluation profiles and trading fill-distribution gates."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypedDict


@dataclass(slots=True)
class DualEvalProfileResult:
    report: dict[str, object]


class ProfileSummary(TypedDict):
    samples: int
    no_fill_count: int
    partial_fill_count: int
    full_fill_count: int
    no_fill_ratio: float
    partial_fill_ratio: float


def evaluate_dual_eval_profiles(
    *,
    records: Sequence[Mapping[str, object]],
    profile_set_id: str,
    no_fill_ratio_limit: float,
    partial_fill_ratio_limit: float,
    no_fill_ratio_delta_limit: float,
    partial_fill_ratio_delta_limit: float,
    baseline_no_fill_ratio: float,
    baseline_partial_fill_ratio: float,
) -> DualEvalProfileResult:
    trading = _summarize_profile(
        records=records,
        status_keys=("fill_status", "entry_fill_status", "execution_fill_status"),
        ratio_keys=("fill_ratio", "entry_fill_ratio", "execution_fill_ratio"),
        default_status="full_fill",
    )
    stockpick = _summarize_profile(
        records=records,
        status_keys=("stockpick_fill_status",),
        ratio_keys=("stockpick_fill_ratio",),
        default_status="full_fill",
    )

    no_fill_ratio = float(trading["no_fill_ratio"])
    partial_fill_ratio = float(trading["partial_fill_ratio"])
    no_fill_ratio_delta = max(0.0, no_fill_ratio - max(0.0, baseline_no_fill_ratio))
    partial_fill_ratio_delta = max(0.0, partial_fill_ratio - max(0.0, baseline_partial_fill_ratio))

    pass_absolute = (
        no_fill_ratio <= max(0.0, no_fill_ratio_limit)
        and partial_fill_ratio <= max(0.0, partial_fill_ratio_limit)
    )
    pass_delta = (
        no_fill_ratio_delta <= max(0.0, no_fill_ratio_delta_limit)
        and partial_fill_ratio_delta <= max(0.0, partial_fill_ratio_delta_limit)
    )
    gate_pass = pass_absolute or pass_delta

    reason_codes: list[str] = []
    if no_fill_ratio > max(0.0, no_fill_ratio_limit):
        reason_codes.append("no_fill_ratio_limit_breach")
    if partial_fill_ratio > max(0.0, partial_fill_ratio_limit):
        reason_codes.append("partial_fill_ratio_limit_breach")
    if no_fill_ratio_delta > max(0.0, no_fill_ratio_delta_limit):
        reason_codes.append("no_fill_ratio_delta_limit_breach")
    if partial_fill_ratio_delta > max(0.0, partial_fill_ratio_delta_limit):
        reason_codes.append("partial_fill_ratio_delta_limit_breach")

    report = {
        "profile_set_id": str(profile_set_id).strip() or "dual_eval_v1",
        "profiles": {
            "trading_eval_profile": trading,
            "stockpick_eval_profile": stockpick,
        },
        "trading_distribution_gate": {
            "pass": gate_pass,
            "pass_absolute": pass_absolute,
            "pass_delta": pass_delta,
            "reason_codes": reason_codes,
            "no_fill_ratio_limit": max(0.0, no_fill_ratio_limit),
            "partial_fill_ratio_limit": max(0.0, partial_fill_ratio_limit),
            "no_fill_ratio_delta_limit": max(0.0, no_fill_ratio_delta_limit),
            "partial_fill_ratio_delta_limit": max(0.0, partial_fill_ratio_delta_limit),
        },
        "no_fill_ratio": no_fill_ratio,
        "partial_fill_ratio": partial_fill_ratio,
        "no_fill_ratio_delta": no_fill_ratio_delta,
        "partial_fill_ratio_delta": partial_fill_ratio_delta,
    }
    return DualEvalProfileResult(report=report)


def _summarize_profile(
    *,
    records: Sequence[Mapping[str, object]],
    status_keys: Sequence[str],
    ratio_keys: Sequence[str],
    default_status: str,
) -> ProfileSummary:
    total = 0
    no_fill = 0
    partial_fill = 0
    for item in records:
        status = _resolve_fill_status(
            item=item,
            status_keys=status_keys,
            ratio_keys=ratio_keys,
            default_status=default_status,
        )
        total += 1
        if status == "no_fill":
            no_fill += 1
        elif status == "partial_fill":
            partial_fill += 1
    if total <= 0:
        return {
            "samples": 0,
            "no_fill_count": 0,
            "partial_fill_count": 0,
            "full_fill_count": 0,
            "no_fill_ratio": 0.0,
            "partial_fill_ratio": 0.0,
        }
    no_fill_ratio = no_fill / total
    partial_fill_ratio = partial_fill / total
    return {
        "samples": total,
        "no_fill_count": no_fill,
        "partial_fill_count": partial_fill,
        "full_fill_count": max(0, total - no_fill - partial_fill),
        "no_fill_ratio": round(no_fill_ratio, 6),
        "partial_fill_ratio": round(partial_fill_ratio, 6),
    }


def _resolve_fill_status(
    *,
    item: Mapping[str, object],
    status_keys: Sequence[str],
    ratio_keys: Sequence[str],
    default_status: str,
) -> str:
    for key in status_keys:
        parsed = _normalize_status(item.get(key))
        if parsed:
            return parsed

    for key in ratio_keys:
        ratio = _as_float(item.get(key), default=-1.0)
        if ratio < 0.0:
            continue
        if ratio <= 0.0:
            return "no_fill"
        if ratio < 1.0:
            return "partial_fill"
        return "full_fill"
    return _normalize_status(default_status) or "full_fill"


def _normalize_status(value: object) -> str:
    if not isinstance(value, str):
        return ""
    lowered = value.strip().lower()
    if not lowered:
        return ""
    if lowered in {"no_fill", "nofill", "none"}:
        return "no_fill"
    if lowered in {"partial_fill", "partial"}:
        return "partial_fill"
    if lowered in {"full_fill", "full", "filled"}:
        return "full_fill"
    return ""


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
