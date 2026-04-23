"""Inference predictor based on trained artifact."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from stock_analyzer.models.adapters import LightGBMAdapter, XGBoostAdapter
from stock_analyzer.models.artifact import ModelArtifact
from stock_analyzer.models.calibration import IsotonicCalibrator


@dataclass(slots=True)
class SignalPredictor:
    """Predict lgbm/xgb/meta probabilities from engineered features."""

    feature_columns: list[str]
    lgbm: LightGBMAdapter
    xgb: XGBoostAdapter
    lgbm_calibrator: IsotonicCalibrator
    xgb_calibrator: IsotonicCalibrator
    meta_weights: dict[str, float] = field(default_factory=lambda: {"lgbm": 0.5, "xgb": 0.5})
    artifact_metadata: dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_artifact(
        cls,
        artifact: ModelArtifact,
        *,
        artifact_root: str | Path | None = None,
    ) -> SignalPredictor:
        meta_weights = _normalize_meta_weights(artifact.metadata.get("meta_blend_weights"))
        return cls(
            feature_columns=artifact.feature_columns,
            lgbm=LightGBMAdapter.from_dict(artifact.lgbm_model, base_path=artifact_root),
            xgb=XGBoostAdapter.from_dict(artifact.xgb_model, base_path=artifact_root),
            lgbm_calibrator=IsotonicCalibrator.from_dict(artifact.lgbm_calibrator),
            xgb_calibrator=IsotonicCalibrator.from_dict(artifact.xgb_calibrator),
            meta_weights=meta_weights,
            artifact_metadata=dict(artifact.metadata),
        )

    @classmethod
    def load(cls, path: str | Path) -> SignalPredictor:
        artifact_path = Path(path)
        artifact = ModelArtifact.load(artifact_path)
        return cls.from_artifact(artifact, artifact_root=artifact_path.parent)

    def predict_row(self, features: pd.Series) -> dict[str, float]:
        values = np.asarray(
            [float(features.get(col, 0.0)) for col in self.feature_columns],
            dtype=float,
        )
        matrix = values.reshape(1, -1)

        raw_lgbm = self.lgbm.predict_proba(matrix)
        raw_xgb = self.xgb.predict_proba(matrix)
        lgbm_prob = float(self.lgbm_calibrator.predict(raw_lgbm)[0])
        xgb_prob = float(self.xgb_calibrator.predict(raw_xgb)[0])
        meta = (
            lgbm_prob * self.meta_weights.get("lgbm", 0.5)
            + xgb_prob * self.meta_weights.get("xgb", 0.5)
        )
        return {
            "lgbm": _clamp_prob(lgbm_prob),
            "xgb": _clamp_prob(xgb_prob),
            "meta": _clamp_prob(meta),
        }

    def mode_details(self) -> dict[str, object]:
        lgbm_backend = str(self.artifact_metadata.get("lgbm_backend", self.lgbm.backend))
        xgb_backend = str(self.artifact_metadata.get("xgb_backend", self.xgb.backend))
        lgbm_load_source = str(getattr(self.lgbm, "load_source", "")).strip()
        xgb_load_source = str(getattr(self.xgb, "load_source", "")).strip()
        degraded_model_mode = bool(
            self.artifact_metadata.get(
                "degraded_model_mode",
                lgbm_backend.startswith("fallback") and xgb_backend.startswith("fallback"),
            )
        )
        native_sidecar_fallback_used = "fallback_sidecar" in {
            lgbm_load_source,
            xgb_load_source,
        }
        degraded_reason = ""
        if degraded_model_mode:
            degraded_reason = (
                f"native_backends_unavailable:lgbm={lgbm_backend},xgb={xgb_backend}"
            )
        status_timestamp = datetime.now().isoformat()
        return {
            "predictor_mode": "artifact_loaded",
            "lgbm_backend": lgbm_backend,
            "xgb_backend": xgb_backend,
            "lgbm_load_source": lgbm_load_source,
            "xgb_load_source": xgb_load_source,
            "native_sidecar_fallback_used": native_sidecar_fallback_used,
            "degraded_model_mode": degraded_model_mode,
            "degraded_reason": degraded_reason,
            "degraded_reason_at": status_timestamp if degraded_reason else "",
            "status_timestamp": status_timestamp,
            "created_at": str(self.artifact_metadata.get("artifact_created_at", "")),
            "calibration_method": str(self.artifact_metadata.get("calibration_method", "")),
            "meta_blend_weights": dict(self.meta_weights),
        }


def _normalize_meta_weights(raw_value: object) -> dict[str, float]:
    if not isinstance(raw_value, dict):
        return {"lgbm": 0.5, "xgb": 0.5}
    lgbm = _safe_weight(raw_value.get("lgbm"), default=0.5)
    xgb = _safe_weight(raw_value.get("xgb"), default=0.5)
    total = lgbm + xgb
    if total <= 0:
        return {"lgbm": 0.5, "xgb": 0.5}
    return {"lgbm": lgbm / total, "xgb": xgb / total}


def _safe_weight(value: object, *, default: float) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    if isinstance(value, str):
        try:
            return max(0.0, float(value))
        except ValueError:
            return default
    return default


def _clamp_prob(value: float) -> float:
    return max(0.0, min(1.0, value))
