"""Rolling probability-distribution health checks for production inference."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np


@dataclass(slots=True)
class ProbabilityHealthMonitor:
    window_size: int = 20
    abnormal_batches_required: int = 3
    _windows: dict[str, deque[float]] = field(default_factory=dict)
    _consecutive_abnormal: dict[str, int] = field(default_factory=dict)

    def observe(self, probabilities: Mapping[str, object]) -> dict[str, object]:
        model_reports: dict[str, dict[str, object]] = {}
        current_values: list[float] = []
        unhealthy_models: list[str] = []
        for model_name, raw_value in probabilities.items():
            name = str(model_name).strip() or "unknown"
            value = _finite_float(raw_value)
            reasons: list[str] = []
            if value is None:
                reasons.append("non_finite_probability")
            else:
                window = self._windows.setdefault(name, deque(maxlen=max(2, self.window_size)))
                window.append(value)
                current_values.append(value)
                values = np.asarray(window, dtype=float)
                if len(values) >= self.window_size:
                    if float(np.std(values)) < 0.005:
                        reasons.append("rolling_std_below_0_005")
                    if len({round(float(item), 4) for item in values}) <= 2:
                        reasons.append("rolling_unique_values_lte_2")
                    saturation_rate = float(np.mean((values <= 0.001) | (values >= 0.999)))
                    if saturation_rate >= 0.8:
                        reasons.append("probability_saturation")
            abnormal = bool(reasons)
            consecutive = self._consecutive_abnormal.get(name, 0)
            consecutive = consecutive + 1 if abnormal else 0
            self._consecutive_abnormal[name] = consecutive
            unhealthy = "non_finite_probability" in reasons or (
                consecutive >= self.abnormal_batches_required
            )
            if unhealthy:
                unhealthy_models.append(name)
            window_values = list(self._windows.get(name, ()))
            model_reports[name] = {
                "samples": len(window_values),
                "std": round(float(np.std(window_values)), 8) if window_values else None,
                "unique_values_4dp": len({round(item, 4) for item in window_values}),
                "consecutive_abnormal_batches": consecutive,
                "reasons": reasons,
                "unhealthy": unhealthy,
            }
        spread = max(current_values) - min(current_values) if len(current_values) >= 2 else None
        return {
            "status": "model_probability_unhealthy" if unhealthy_models else "healthy",
            "unhealthy_models": unhealthy_models,
            "models": model_reports,
            "inter_model_spread": round(spread, 8) if spread is not None else None,
            "window_size": self.window_size,
            "abnormal_batches_required": self.abnormal_batches_required,
            "promotion_allowed": not unhealthy_models,
        }


def sanitize_probabilities(probabilities: Mapping[str, object]) -> dict[str, float]:
    normalized: dict[str, float] = {}
    for name, raw_value in probabilities.items():
        value = _finite_float(raw_value)
        normalized[str(name)] = min(1.0, max(0.0, value if value is not None else 0.5))
    return normalized


def _finite_float(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None
