"""Model adapters that prefer native libs and fall back to local logistic models."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Protocol, TypeAlias, cast

import numpy as np
import numpy.typing as npt

from stock_analyzer.models.fallback import LogisticProbModel

FloatArray: TypeAlias = npt.NDArray[np.float64]


class ProbabilityModel(Protocol):
    backend: str

    def fit(
        self,
        x: FloatArray,
        y: FloatArray,
        sample_weight: FloatArray | None = None,
    ) -> None:
        """Fit model."""

    def predict_proba(self, x: FloatArray) -> FloatArray:
        """Return probabilities."""

    def to_dict(self) -> dict[str, object]:
        """Serialize model."""


def inspect_model_backend_dependencies() -> dict[str, dict[str, object]]:
    """Return explicit dependency availability for optional native backends."""
    return {
        "lightgbm": _inspect_optional_dependency("lightgbm"),
        "xgboost": _inspect_optional_dependency("xgboost"),
    }


@dataclass(slots=True)
class LightGBMAdapter:
    """LightGBM-compatible model wrapper with native + fallback backends."""

    backend: str = "fallback_logit"
    load_source: str = "uninitialized"
    _model: LogisticProbModel = field(
        default_factory=lambda: LogisticProbModel(
            learning_rate=0.05,
            epochs=300,
            l2=1e-3,
            seed=2026,
        ),
        init=False,
        repr=False,
    )
    _native: Any = field(default=None, init=False, repr=False)
    _native_predict: Any = field(default=None, init=False, repr=False)

    def fit(
        self,
        x: FloatArray,
        y: FloatArray,
        sample_weight: FloatArray | None = None,
    ) -> None:
        try:
            import lightgbm as lgb

            dataset = lgb.Dataset(x, label=y, weight=sample_weight)
            params = {
                "objective": "binary",
                "metric": "binary_logloss",
                "learning_rate": 0.05,
                "num_leaves": 31,
                "verbose": -1,
            }
            booster = lgb.train(params=params, train_set=dataset, num_boost_round=80)
            self._native = booster
            self._native_predict = booster.predict
            self.backend = "lightgbm"
            self.load_source = "native_runtime"
            return
        except Exception:
            pass

        self._model.fit(x, y, sample_weight=sample_weight)
        self.backend = "fallback_logit"
        self.load_source = "fallback_runtime"

    def predict_proba(self, x: FloatArray) -> FloatArray:
        if self.backend == "lightgbm" and self._native_predict is not None:
            return cast(FloatArray, np.asarray(self._native_predict(x), dtype=float))
        return self._model.predict_proba(x)

    def to_dict(self) -> dict[str, object]:
        if self.backend == "lightgbm":
            if self._native is None or not hasattr(self._native, "model_to_string"):
                raise RuntimeError("native lightgbm model is not fitted")
            return {
                "backend": self.backend,
                "sidecar_format": "txt",
                "native_blob": str(self._native.model_to_string()),
            }
        return {"backend": self.backend, "payload": self._model.to_dict()}

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, object],
        *,
        base_path: str | Path | None = None,
    ) -> LightGBMAdapter:
        backend = str(payload.get("backend", "")).strip().lower()
        if backend == "fallback_logit":
            raw_model = payload.get("payload")
            if not isinstance(raw_model, dict):
                raise ValueError("invalid lgbm payload")
            model = cls()
            model.backend = backend
            model.load_source = "inline_payload"
            model._model = LogisticProbModel.from_dict(raw_model)
            return model
        if backend != "lightgbm":
            raise ValueError(f"unsupported serialized backend: {backend}")

        blob, load_source = _load_native_blob(payload=payload, base_path=base_path)
        try:
            import lightgbm as lgb
        except Exception as exc:
            raise RuntimeError("lightgbm runtime dependency is unavailable") from exc

        booster = lgb.Booster(model_str=blob.decode("utf-8"))
        model = cls()
        model.backend = "lightgbm"
        model.load_source = load_source
        model._native = booster
        model._native_predict = booster.predict
        return model


@dataclass(slots=True)
class XGBoostAdapter:
    """XGBoost-compatible model wrapper with native + fallback backends."""

    backend: str = "fallback_logit"
    load_source: str = "uninitialized"
    _model: LogisticProbModel = field(
        default_factory=lambda: LogisticProbModel(
            learning_rate=0.06,
            epochs=280,
            l2=5e-4,
            seed=2027,
        ),
        init=False,
        repr=False,
    )
    _native: Any = field(default=None, init=False, repr=False)
    _native_predict: Any = field(default=None, init=False, repr=False)

    def fit(
        self,
        x: FloatArray,
        y: FloatArray,
        sample_weight: FloatArray | None = None,
    ) -> None:
        try:
            import xgboost as xgb

            train_matrix = xgb.DMatrix(x, label=y, weight=sample_weight)
            base_score = float(np.clip(np.mean(y), 1e-3, 1.0 - 1e-3))
            booster = xgb.train(
                params={
                    "objective": "binary:logistic",
                    "eval_metric": "logloss",
                    "max_depth": 4,
                    "eta": 0.05,
                    "subsample": 0.9,
                    "colsample_bytree": 0.9,
                    "seed": 2026,
                    "nthread": 1,
                    "base_score": base_score,
                },
                dtrain=train_matrix,
                num_boost_round=100,
            )
            self._native = booster
            self._native_predict = lambda matrix: booster.predict(xgb.DMatrix(matrix))
            self.backend = "xgboost"
            self.load_source = "native_runtime"
            return
        except Exception:
            pass

        self._model.fit(x, y, sample_weight=sample_weight)
        self.backend = "fallback_logit"
        self.load_source = "fallback_runtime"

    def predict_proba(self, x: FloatArray) -> FloatArray:
        if self.backend == "xgboost" and self._native_predict is not None:
            return cast(FloatArray, np.asarray(self._native_predict(x), dtype=float))
        return self._model.predict_proba(x)

    def to_dict(self) -> dict[str, object]:
        if self.backend == "xgboost":
            booster = (
                self._native.get_booster() if hasattr(self._native, "get_booster") else self._native
            )
            if booster is None or not hasattr(booster, "save_raw"):
                raise RuntimeError("native xgboost model is not fitted")
            raw = booster.save_raw()
            payload = bytes(raw) if not isinstance(raw, bytes) else raw
            return {
                "backend": self.backend,
                "sidecar_format": "bin",
                "native_blob_b64": base64.b64encode(payload).decode("ascii"),
            }
        return {"backend": self.backend, "payload": self._model.to_dict()}

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, object],
        *,
        base_path: str | Path | None = None,
    ) -> XGBoostAdapter:
        backend = str(payload.get("backend", "")).strip().lower()
        if backend == "fallback_logit":
            raw_model = payload.get("payload")
            if not isinstance(raw_model, dict):
                raise ValueError("invalid xgb payload")
            model = cls()
            model.backend = backend
            model.load_source = "inline_payload"
            model._model = LogisticProbModel.from_dict(raw_model)
            return model
        if backend != "xgboost":
            raise ValueError(f"unsupported serialized backend: {backend}")

        blob, load_source = _load_native_blob(payload=payload, base_path=base_path)
        try:
            import xgboost as xgb
        except Exception as exc:
            raise RuntimeError("xgboost runtime dependency is unavailable") from exc

        booster = xgb.Booster()
        booster.load_model(bytearray(blob))
        model = cls()
        model.backend = "xgboost"
        model.load_source = load_source
        model._native = booster
        model._native_predict = lambda matrix: booster.predict(xgb.DMatrix(matrix))
        return model


def _inspect_optional_dependency(module_name: str) -> dict[str, object]:
    try:
        import_module(module_name)
        try:
            module_version = version(module_name)
        except PackageNotFoundError:
            module_version = ""
        return {"installed": True, "version": module_version}
    except Exception:
        return {"installed": False, "version": ""}


def _load_native_blob(
    *,
    payload: dict[str, object],
    base_path: str | Path | None,
) -> tuple[bytes, str]:
    inline_blob = payload.get("native_blob")
    if isinstance(inline_blob, str):
        return inline_blob.encode("utf-8"), "inline_text"

    inline_b64 = payload.get("native_blob_b64")
    if isinstance(inline_b64, str) and inline_b64.strip():
        return base64.b64decode(inline_b64.encode("ascii")), "inline_base64"

    candidates = [
        (
            payload.get("sidecar_path"),
            payload.get("sidecar_sha256"),
            "current_sidecar",
        ),
        (
            payload.get("fallback_sidecar_path"),
            payload.get("fallback_sidecar_sha256"),
            "fallback_sidecar",
        ),
    ]
    errors: list[str] = []
    for raw_path, raw_hash, source_name in candidates:
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        resolved = _resolve_sidecar_path(raw_path, base_path=base_path)
        if not resolved.exists():
            errors.append(f"missing:{resolved}")
            continue
        content = resolved.read_bytes()
        expected_hash = str(raw_hash).strip().lower() if isinstance(raw_hash, str) else ""
        if expected_hash and hashlib.sha256(content).hexdigest() != expected_hash:
            errors.append(f"hash_mismatch:{resolved}")
            continue
        return content, source_name

    detail = "; ".join(errors) if errors else "no inline or sidecar payload"
    raise ValueError(f"native model payload is unavailable: {detail}")


def _resolve_sidecar_path(raw_path: str, *, base_path: str | Path | None) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute() or base_path is None:
        return candidate
    return Path(base_path) / candidate
