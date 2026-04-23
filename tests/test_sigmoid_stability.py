from __future__ import annotations

import math

import numpy as np

from stock_analyzer.models.fallback import _sigmoid as fallback_sigmoid
from stock_analyzer.pipeline import _sigmoid as pipeline_sigmoid


def test_fallback_sigmoid_is_numerically_stable_for_extreme_values() -> None:
    values = np.asarray([-1000.0, -100.0, 0.0, 100.0, 1000.0], dtype=float)
    probs = fallback_sigmoid(values)
    assert np.isfinite(probs).all()
    assert float(probs[0]) == 0.0
    assert float(probs[2]) == 0.5
    assert float(probs[-1]) == 1.0


def test_pipeline_sigmoid_is_numerically_stable_for_extreme_values() -> None:
    low = pipeline_sigmoid(-1000.0)
    mid = pipeline_sigmoid(0.0)
    high = pipeline_sigmoid(1000.0)
    assert math.isfinite(low)
    assert math.isfinite(mid)
    assert math.isfinite(high)
    assert low == 0.0
    assert mid == 0.5
    assert high == 1.0
