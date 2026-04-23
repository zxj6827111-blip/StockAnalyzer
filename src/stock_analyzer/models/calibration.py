"""Probability calibration tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

import numpy as np
import numpy.typing as npt

FloatArray: TypeAlias = npt.NDArray[np.float64]


@dataclass(slots=True)
class _Block:
    x_right: float
    weight: float
    mean: float


class IsotonicCalibrator:
    """Piecewise-constant isotonic regression via PAV."""

    def __init__(self) -> None:
        self._x_right: FloatArray | None = None
        self._y_hat: FloatArray | None = None

    def fit(self, scores: FloatArray, labels: FloatArray) -> None:
        if scores.ndim != 1 or labels.ndim != 1:
            raise ValueError("scores and labels must be 1D arrays")
        if scores.shape[0] != labels.shape[0]:
            raise ValueError("scores and labels sizes must match")
        if scores.shape[0] == 0:
            raise ValueError("empty calibration data")

        order = np.argsort(scores)
        sorted_scores = scores[order]
        sorted_labels = labels[order]

        blocks: list[_Block] = []
        for score, label in zip(sorted_scores, sorted_labels, strict=True):
            blocks.append(_Block(x_right=float(score), weight=1.0, mean=float(label)))
            while len(blocks) >= 2 and blocks[-2].mean > blocks[-1].mean:
                right = blocks.pop()
                left = blocks.pop()
                weight = left.weight + right.weight
                mean = (left.mean * left.weight + right.mean * right.weight) / weight
                blocks.append(_Block(x_right=right.x_right, weight=weight, mean=mean))

        self._x_right = np.asarray([item.x_right for item in blocks], dtype=float)
        self._y_hat = np.asarray([item.mean for item in blocks], dtype=float)

    def predict(self, scores: FloatArray) -> FloatArray:
        if self._x_right is None or self._y_hat is None:
            raise RuntimeError("calibrator is not fitted")
        positions = np.searchsorted(self._x_right, scores, side="right")
        clipped = np.clip(positions, 0, len(self._y_hat) - 1)
        return self._y_hat[clipped]

    def to_dict(self) -> dict[str, object]:
        if self._x_right is None or self._y_hat is None:
            raise RuntimeError("calibrator is not fitted")
        return {"x_right": self._x_right.tolist(), "y_hat": self._y_hat.tolist()}

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> IsotonicCalibrator:
        calibrator = cls()
        raw_x = payload.get("x_right", [])
        raw_y = payload.get("y_hat", [])
        if not isinstance(raw_x, list) or not isinstance(raw_y, list):
            raise ValueError("invalid isotonic payload")
        calibrator._x_right = np.asarray(raw_x, dtype=float)
        calibrator._y_hat = np.asarray(raw_y, dtype=float)
        return calibrator
