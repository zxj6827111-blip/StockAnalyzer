"""Small numerical fallback models for environments without native boosters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeAlias

import numpy as np
import numpy.typing as npt

FloatArray: TypeAlias = npt.NDArray[np.float64]


@dataclass(slots=True)
class LogisticProbModel:
    """Binary logistic regression trained by gradient descent."""

    learning_rate: float = 0.05
    epochs: int = 200
    l2: float = 1e-3
    seed: int = 42
    weights: FloatArray | None = field(default=None, init=False, repr=False)
    bias: float = field(default=0.0, init=False)

    def fit(
        self,
        x: FloatArray,
        y: FloatArray,
        sample_weight: FloatArray | None = None,
    ) -> None:
        if x.ndim != 2:
            raise ValueError("x must be a 2D array")
        if y.ndim != 1:
            raise ValueError("y must be a 1D array")
        if x.shape[0] != y.shape[0]:
            raise ValueError("x and y sample sizes must match")
        if x.shape[0] == 0:
            raise ValueError("empty training set")
        if sample_weight is None:
            normalized_weight = np.ones(x.shape[0], dtype=float)
        else:
            if sample_weight.ndim != 1:
                raise ValueError("sample_weight must be a 1D array")
            if sample_weight.shape[0] != x.shape[0]:
                raise ValueError("sample_weight size must match x rows")
            normalized_weight = np.asarray(sample_weight, dtype=float)
            if np.any(normalized_weight <= 0.0):
                raise ValueError("sample_weight must be positive")
        normalized_weight = normalized_weight / max(float(np.mean(normalized_weight)), 1e-12)
        weight_total = max(float(np.sum(normalized_weight)), 1e-12)

        rng = np.random.default_rng(self.seed)
        self.weights = rng.normal(loc=0.0, scale=0.01, size=x.shape[1]).astype(float)
        self.bias = 0.0

        for _ in range(self.epochs):
            logits = x @ self.weights + self.bias
            probs = _sigmoid(logits)
            errors = (probs - y) * normalized_weight
            grad_w = (x.T @ errors) / weight_total + self.l2 * self.weights
            grad_b = float(np.sum(errors) / weight_total)

            self.weights -= self.learning_rate * grad_w
            self.bias -= self.learning_rate * grad_b

    def predict_proba(self, x: FloatArray) -> FloatArray:
        if self.weights is None:
            raise RuntimeError("model is not fitted")
        logits = x @ self.weights + self.bias
        return _sigmoid(logits)

    def to_dict(self) -> dict[str, object]:
        if self.weights is None:
            raise RuntimeError("model is not fitted")
        return {
            "learning_rate": self.learning_rate,
            "epochs": self.epochs,
            "l2": self.l2,
            "seed": self.seed,
            "weights": self.weights.tolist(),
            "bias": self.bias,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> LogisticProbModel:
        model = cls(
            learning_rate=_to_float(payload.get("learning_rate"), default=0.05),
            epochs=_to_int(payload.get("epochs"), default=200),
            l2=_to_float(payload.get("l2"), default=1e-3),
            seed=_to_int(payload.get("seed"), default=42),
        )
        raw_weights = payload.get("weights", [])
        if not isinstance(raw_weights, list):
            raise ValueError("invalid weights in serialized logistic model")
        model.weights = np.asarray(raw_weights, dtype=float)
        model.bias = _to_float(payload.get("bias"), default=0.0)
        return model


def _sigmoid(values: FloatArray) -> FloatArray:
    output = np.empty_like(values, dtype=float)
    positive = values >= 0
    output[positive] = 1.0 / (1.0 + np.exp(-values[positive]))
    negative_values = values[~positive]
    exp_values = np.exp(negative_values)
    output[~positive] = exp_values / (1.0 + exp_values)
    return output


def _to_float(value: object, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, (float, int)):
        return float(value)
    if isinstance(value, str):
        return float(value)
    raise ValueError("cannot parse float from payload")


def _to_int(value: object, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(value)
    raise ValueError("cannot parse int from payload")
