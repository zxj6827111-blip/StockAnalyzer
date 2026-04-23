"""M10 model-health scoring with conflict and calibration diagnostics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class M10HealthMetrics:
    """Interpretable metrics for model-health assessment."""

    valid_symbols: int
    prediction_coverage_ratio: float
    mean_model_spread: float
    high_conflict_ratio: float
    calibration_gap: float
    return_volatility: float


@dataclass(frozen=True, slots=True)
class M10ModelHealthResult:
    """M10 output with normalized score and status."""

    score: float
    status: str
    metrics: M10HealthMetrics


def evaluate_m10_model_health(
    records: Sequence[Mapping[str, object]],
    conflict_warn: float = 0.25,
    calibration_gap_warn: float = 0.15,
    return_volatility_warn: float = 0.06,
    conflict_watch_ratio: float = 0.25,
    conflict_degraded_ratio: float = 0.50,
    calibration_degraded_multiplier: float = 1.5,
    limited_observability_score: float = 65.0,
) -> M10ModelHealthResult:
    """Evaluate M10 model-health diagnostics.

    The evaluator supports records containing optional per-symbol predictions:
    ``p_lgbm``, ``p_xgb`` and ``p_meta``. If prediction triplets are unavailable,
    the module still returns a bounded fallback score in ``limited_observability``
    status instead of failing hard.
    """
    returns: list[float] = []
    triplets: list[tuple[float, float, float]] = []

    for record in records:
        open_px = _as_float(record.get("open"), default=0.0)
        close_px = _as_float(record.get("close"), default=0.0)
        if open_px > 0.0 and close_px > 0.0:
            returns.append(close_px / max(open_px, 1e-6) - 1.0)

        p_lgbm = _as_probability(record.get("p_lgbm"))
        p_xgb = _as_probability(record.get("p_xgb"))
        p_meta = _as_probability(record.get("p_meta"))
        if p_lgbm is None or p_xgb is None or p_meta is None:
            continue
        triplets.append((p_lgbm, p_xgb, p_meta))

    valid_symbols = len(returns)
    if valid_symbols == 0:
        metrics = M10HealthMetrics(
            valid_symbols=0,
            prediction_coverage_ratio=0.0,
            mean_model_spread=0.0,
            high_conflict_ratio=0.0,
            calibration_gap=0.0,
            return_volatility=0.0,
        )
        return M10ModelHealthResult(
            score=50.0,
            status="no_data",
            metrics=metrics,
        )

    return_volatility = float(np.std(np.asarray(returns, dtype=float)))
    prediction_coverage_ratio = len(triplets) / max(valid_symbols, 1)
    spreads: list[float] = []
    calibration_gaps: list[float] = []
    for p_lgbm, p_xgb, p_meta in triplets:
        spread = abs(p_lgbm - p_xgb)
        spreads.append(spread)
        calibration_gaps.append(abs(p_meta - (p_lgbm + p_xgb) * 0.5))

    mean_model_spread = float(np.mean(spreads)) if spreads else 0.0
    high_conflict_ratio = (
        sum(1 for item in spreads if item >= conflict_warn) / len(spreads) if spreads else 0.0
    )
    calibration_gap = float(np.mean(calibration_gaps)) if calibration_gaps else 0.0

    vol_penalty = 0.0
    if return_volatility > return_volatility_warn:
        vol_penalty = (
            (return_volatility - return_volatility_warn) / max(return_volatility_warn, 1e-6)
        ) * 15.0

    if triplets:
        spread_penalty = min(1.0, mean_model_spread / max(conflict_warn, 1e-6)) * 20.0
        conflict_penalty = high_conflict_ratio * 35.0
        calibration_penalty = (
            min(1.0, calibration_gap / max(calibration_gap_warn, 1e-6)) * 25.0
        )
        coverage_penalty = (1.0 - prediction_coverage_ratio) * 20.0
        score = _clamp(
            100.0
            - spread_penalty
            - conflict_penalty
            - calibration_penalty
            - coverage_penalty
            - vol_penalty
        )
        if (
            high_conflict_ratio >= conflict_degraded_ratio
            or calibration_gap >= calibration_gap_warn * calibration_degraded_multiplier
        ):
            status = "degraded"
        elif (
            score < 70.0
            or high_conflict_ratio >= conflict_watch_ratio
            or calibration_gap >= calibration_gap_warn
        ):
            status = "watch"
        else:
            status = "healthy"
    else:
        score = _clamp(limited_observability_score - vol_penalty)
        status = "limited_observability" if score >= 50.0 else "degraded"

    metrics = M10HealthMetrics(
        valid_symbols=valid_symbols,
        prediction_coverage_ratio=prediction_coverage_ratio,
        mean_model_spread=mean_model_spread,
        high_conflict_ratio=high_conflict_ratio,
        calibration_gap=calibration_gap,
        return_volatility=return_volatility,
    )
    return M10ModelHealthResult(score=score, status=status, metrics=metrics)


def _as_float(value: object, default: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _as_probability(value: object) -> float | None:
    parsed = _as_float(value, default=-1.0)
    if parsed < 0.0:
        return None
    return max(0.0, min(1.0, parsed))


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))
