"""Small numerical fallback models for environments without native boosters."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TypeAlias

import numpy as np
import numpy.typing as npt

FloatArray: TypeAlias = npt.NDArray[np.float64]

_LEGACY_NO_SCALER_REASON = "legacy_no_scaler"
_SCALER_EPS = 1e-12


@dataclass(slots=True)
class LogisticProbModel:
    """Binary logistic regression trained by gradient descent.

    Feature standardization contract
    --------------------------------
    The scaler (training-input mean/scale) is recorded FIRST, before any
    weight update, and gradient descent runs on the standardized features;
    ``predict_proba`` applies the exact same transform, so training and
    inference live in one coordinate system. Both halves are serialized
    under the ``scaler`` payload key. Artifacts without a valid ``scaler``
    payload are legacy artifacts: they remain loadable for diagnostics but
    ``predict_proba`` refuses to run with a ``ValueError`` so production
    inference can never silently use unscaled legacy weights.
    """

    learning_rate: float = 0.05
    epochs: int = 200
    l2: float = 1e-3
    seed: int = 42
    weights: FloatArray | None = field(default=None, init=False, repr=False)
    bias: float = field(default=0.0, init=False)
    scaler: dict[str, FloatArray] | None = field(default=None, init=False, repr=False)
    inference_blocked_reason: str = field(default="", init=False)

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

        # Record the feature scaler BEFORE any weight update and train on the
        # scaled coordinates, so the training path and the inference path
        # (predict_proba) operate in exactly the same coordinate system.
        self._record_scaler(x)
        assert self.scaler is not None
        scaled = (np.asarray(x, dtype=float) - self.scaler["mean"]) / self.scaler["scale"]

        rng = np.random.default_rng(self.seed)
        self.weights = rng.normal(loc=0.0, scale=0.01, size=x.shape[1]).astype(float)
        self.bias = 0.0

        for _ in range(self.epochs):
            logits = scaled @ self.weights + self.bias
            probs = _sigmoid(logits)
            errors = (probs - y) * normalized_weight
            grad_w = (scaled.T @ errors) / weight_total + self.l2 * self.weights
            grad_b = float(np.sum(errors) / weight_total)

            self.weights -= self.learning_rate * grad_w
            self.bias -= self.learning_rate * grad_b

    def predict_proba(self, x: FloatArray) -> FloatArray:
        if self.weights is None:
            raise RuntimeError("model is not fitted")
        if self.scaler is None:
            raise ValueError("model inference blocked: artifact is " + _LEGACY_NO_SCALER_REASON)
        mean = self.scaler["mean"]
        scale = self.scaler["scale"]
        scaled = (np.asarray(x, dtype=float) - mean) / scale
        logits = scaled @ self.weights + self.bias
        return _sigmoid(logits)

    def to_dict(self) -> dict[str, object]:
        if self.weights is None:
            raise RuntimeError("model is not fitted")
        payload: dict[str, object] = {
            "learning_rate": self.learning_rate,
            "epochs": self.epochs,
            "l2": self.l2,
            "seed": self.seed,
            "weights": self.weights.tolist(),
            "bias": self.bias,
        }
        if self.scaler is not None:
            payload["scaler"] = {
                "mean": self.scaler["mean"].tolist(),
                "scale": self.scaler["scale"].tolist(),
            }
        return payload

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
        model.scaler = _parse_scaler(payload.get("scaler"), n_features=len(model.weights))
        if model.scaler is None:
            model.inference_blocked_reason = _LEGACY_NO_SCALER_REASON
        return model

    @classmethod
    def with_weights(
        cls,
        *,
        weights: Sequence[float],
        bias: float = 0.0,
        feature_mean: Sequence[float] | None = None,
        feature_scale: Sequence[float] | None = None,
        learning_rate: float = 0.05,
        epochs: int = 200,
        l2: float = 1e-3,
        seed: int = 42,
    ) -> LogisticProbModel:
        """Build a fixed-weight model under an explicit scaler contract.

        Hand-assembled weights are inference-ready only with an explicit
        feature transform. When no ``feature_mean``/``feature_scale`` is
        given the identity transform (mean 0, scale 1) is applied explicitly,
        so the artifact never degrades into the legacy no-scaler state.
        """
        model = cls(
            learning_rate=learning_rate,
            epochs=epochs,
            l2=l2,
            seed=seed,
        )
        model.weights = np.asarray(list(weights), dtype=float)
        model.bias = float(bias)
        n_features = len(model.weights)
        if feature_mean is not None or feature_scale is not None:
            model.scaler = _parse_scaler(
                {
                    "mean": list(feature_mean) if feature_mean is not None else [0.0] * n_features,
                    "scale": list(feature_scale)
                    if feature_scale is not None
                    else [1.0] * n_features,
                },
                n_features=n_features,
            )
        else:
            model.scaler = {
                "mean": np.zeros(n_features, dtype=float),
                "scale": np.ones(n_features, dtype=float),
            }
        model.inference_blocked_reason = ""
        return model

    def _record_scaler(self, x: FloatArray) -> None:
        if x.ndim != 2:
            raise ValueError("x must be a 2D array")
        mean = np.asarray(x, dtype=float).mean(axis=0)
        scale = np.asarray(x, dtype=float).std(axis=0)
        scale = np.where(np.abs(scale) < _SCALER_EPS, 1.0, scale)
        self.scaler = {"mean": mean, "scale": scale}
        self.inference_blocked_reason = ""


def _sigmoid(values: FloatArray) -> FloatArray:
    output = np.empty_like(values, dtype=float)
    positive = values >= 0
    output[positive] = 1.0 / (1.0 + np.exp(-values[positive]))
    negative_values = values[~positive]
    exp_values = np.exp(negative_values)
    output[~positive] = exp_values / (1.0 + exp_values)
    return output


def _parse_scaler(
    raw_value: object,
    *,
    n_features: int,
) -> dict[str, FloatArray] | None:
    """Parse a serialized scaler payload; ``None`` means legacy no-scaler.

    A missing ``scaler`` key marks the artifact as legacy (loadable for
    diagnostics, blocked for inference). A present-but-malformed payload is a
    hard error so a corrupt artifact never silently degrades into a legacy
    one.
    """
    if raw_value is None:
        return None
    if not isinstance(raw_value, dict):
        raise ValueError("invalid scaler in serialized logistic model")
    raw_mean = raw_value.get("mean")
    raw_scale = raw_value.get("scale")
    if not isinstance(raw_mean, list) or not isinstance(raw_scale, list):
        raise ValueError("invalid scaler arrays in serialized logistic model")
    mean = np.asarray(raw_mean, dtype=float)
    scale = np.asarray(raw_scale, dtype=float)
    if mean.shape != (n_features,) or scale.shape != (n_features,):
        raise ValueError("scaler length does not match weights in serialized logistic model")
    if np.any(~np.isfinite(mean)) or np.any(~np.isfinite(scale)):
        raise ValueError("scaler contains non-finite values in serialized logistic model")
    if np.any(np.abs(scale) < _SCALER_EPS):
        raise ValueError("scaler scale is zero in serialized logistic model")
    return {"mean": mean, "scale": scale}


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
