"""Daily target/filled/EOD reconcile drift metrics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np


@dataclass(slots=True)
class ReconcileDriftResult:
    report: Mapping[str, object]
    state: Mapping[str, object]


def evaluate_daily_reconcile_drift(
    *,
    records: Sequence[Mapping[str, object]],
    now: datetime,
    model_bundle_hash: str,
    position_drift_alert_threshold: float,
    position_drift_raise_u_threshold_bp: int,
    position_drift_consecutive_days_trigger: int,
    previous_state: Mapping[str, object] | None = None,
) -> ReconcileDriftResult:
    state = dict(previous_state or {})
    target_vs_filled: list[float] = []
    filled_vs_eod: list[float] = []
    target_vs_eod_abs: list[float] = []
    valid_rows = 0

    for item in records:
        target_weight = _as_float(
            item.get("target_weight", item.get("target_weight_i")),
            default=float("nan"),
        )
        filled_weight = _resolve_filled_weight(item)
        eod_weight = _as_float(
            item.get("end_of_day_position_weight", item.get("end_of_day_position_weight_i")),
            default=float("nan"),
        )
        if (
            not np.isfinite(target_weight)
            or not np.isfinite(filled_weight)
            or not np.isfinite(eod_weight)
        ):
            continue
        valid_rows += 1
        target_vs_filled.append(abs(target_weight - filled_weight))
        filled_vs_eod.append(abs(filled_weight - eod_weight))
        target_vs_eod_abs.append(abs(target_weight - eod_weight))

    target_stats = _summarize_distribution(target_vs_filled)
    filled_stats = _summarize_distribution(filled_vs_eod)
    position_drift_ratio = (
        float(sum(target_vs_eod_abs) / 2.0) if target_vs_eod_abs else 0.0
    )

    threshold = max(0.0, float(position_drift_alert_threshold))
    drift_alert = position_drift_ratio > threshold
    consecutive_days = max(0, _as_int(state.get("position_drift_consecutive_days"), default=0))
    if drift_alert:
        consecutive_days += 1
    else:
        consecutive_days = 0

    trigger_days = max(1, int(position_drift_consecutive_days_trigger))
    raise_u_threshold_bp = (
        max(0, int(position_drift_raise_u_threshold_bp))
        if consecutive_days >= trigger_days
        else 0
    )
    trading_date = str(
        _first_non_empty(
            [
                str(_first_record_value(records, "trading_date")),
                str(_first_record_value(records, "trade_date")),
                now.date().isoformat(),
            ]
        )
    )
    reconcile_record_id = f"{trading_date}:{str(model_bundle_hash).strip()}"

    report = {
        "trading_date": trading_date,
        "model_bundle_hash": str(model_bundle_hash).strip(),
        "reconcile_record_id": reconcile_record_id,
        "valid_rows": valid_rows,
        "target_vs_filled_weight_p50": target_stats["p50"],
        "target_vs_filled_weight_p90": target_stats["p90"],
        "target_vs_filled_weight_max": target_stats["max"],
        "filled_vs_eod_weight_p50": filled_stats["p50"],
        "filled_vs_eod_weight_p90": filled_stats["p90"],
        "filled_vs_eod_weight_max": filled_stats["max"],
        "position_drift_ratio": round(position_drift_ratio, 6),
        "position_drift_alert_threshold": threshold,
        "position_drift_alert": drift_alert,
        "position_drift_consecutive_days": consecutive_days,
        "raise_u_threshold_bp_recommendation": raise_u_threshold_bp,
    }
    state_out = {
        "position_drift_consecutive_days": consecutive_days,
    }
    return ReconcileDriftResult(report=report, state=state_out)


def _resolve_filled_weight(item: Mapping[str, object]) -> float:
    explicit = _as_float(
        item.get("filled_weight", item.get("filled_weight_i")),
        default=float("nan"),
    )
    if np.isfinite(explicit):
        return explicit
    prev_position_value = _as_float(item.get("prev_position_value_i"), default=float("nan"))
    net_filled_value = _as_float(item.get("net_filled_value_i_today"), default=float("nan"))
    portfolio_nav = _as_float(item.get("portfolio_nav_today"), default=float("nan"))
    if (
        not np.isfinite(prev_position_value)
        or not np.isfinite(net_filled_value)
        or not np.isfinite(portfolio_nav)
    ):
        return float("nan")
    if abs(portfolio_nav) <= 1e-12:
        return float("nan")
    return (prev_position_value + net_filled_value) / portfolio_nav


def _summarize_distribution(values: Sequence[float]) -> dict[str, float]:
    if not values:
        return {"p50": 0.0, "p90": 0.0, "max": 0.0}
    arr = np.asarray(values, dtype=float)
    return {
        "p50": round(float(np.quantile(arr, 0.50)), 6),
        "p90": round(float(np.quantile(arr, 0.90)), 6),
        "max": round(float(np.max(arr)), 6),
    }


def _first_record_value(records: Sequence[Mapping[str, object]], key: str) -> object:
    for item in records:
        if key in item:
            return item.get(key)
    return ""


def _first_non_empty(values: Sequence[str]) -> str:
    for item in values:
        text = item.strip()
        if text and text.lower() != "none":
            return text
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
