"""Inference predictor based on trained artifact."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

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
        batch = self.predict_rows(pd.DataFrame([features.to_dict()]))
        return {key: values[0] for key, values in batch.items()}

    def _predict_matrix(self, features: pd.DataFrame) -> dict[str, list[float]]:
        """raw 与 calibrated 的全量分数（内部单一实现，供两个公共入口共用）。

        返回 ``raw_lgbm/raw_xgb/raw_blend``（校准前）与 ``lgbm/xgb/meta``（校准后）。
        C3 的 ``raw_blend`` 变体靠它拿校准前分数：修后现有模型的校准器仍在塌
        （唯一值 7、spread 为负），需要区分伤害排序的是校准还是模型本身。
        """
        blocked = self.inference_blocked_reason()
        if blocked:
            raise ValueError(
                f"predictor_rejected:{blocked}; refusing production inference on a legacy artifact"
            )
        if features.empty:
            return {
                "raw_lgbm": [],
                "raw_xgb": [],
                "raw_blend": [],
                "lgbm": [],
                "xgb": [],
                "meta": [],
            }
        frame = features
        if any(column not in frame.columns for column in self.feature_columns):
            frame = frame.reindex(columns=self.feature_columns, fill_value=0.0)
        matrix = frame[self.feature_columns].to_numpy(dtype=float)

        raw_lgbm = self.lgbm.predict_proba(matrix)
        raw_xgb = self.xgb.predict_proba(matrix)
        lgbm_probs = self.lgbm_calibrator.predict(raw_lgbm)
        xgb_probs = self.xgb_calibrator.predict(raw_xgb)
        lgbm_weight = self.meta_weights.get("lgbm", 0.5)
        xgb_weight = self.meta_weights.get("xgb", 0.5)
        meta_probs = lgbm_probs * lgbm_weight + xgb_probs * xgb_weight
        raw_blend = raw_lgbm * lgbm_weight + raw_xgb * xgb_weight
        return {
            "raw_lgbm": [_clamp_prob(float(value)) for value in raw_lgbm],
            "raw_xgb": [_clamp_prob(float(value)) for value in raw_xgb],
            "raw_blend": [_clamp_prob(float(value)) for value in raw_blend],
            "lgbm": [_clamp_prob(float(value)) for value in lgbm_probs],
            "xgb": [_clamp_prob(float(value)) for value in xgb_probs],
            "meta": [_clamp_prob(float(value)) for value in meta_probs],
        }

    def predict_rows(self, features: pd.DataFrame) -> dict[str, list[float]]:
        """Vectorized inference for a matrix of engineered features.

        Missing feature columns are filled with 0.0, mirroring ``predict_row``.
        Returns per-column lists ``{"lgbm": [...], "xgb": [...], "meta": [...]}``.
        """
        full = self._predict_matrix(features)
        return {"lgbm": full["lgbm"], "xgb": full["xgb"], "meta": full["meta"]}

    def predict_rows_with_raw(self, features: pd.DataFrame) -> dict[str, list[float]]:
        """同 ``predict_rows``，但额外带**校准前**的 ``raw_*`` 分数。

        独立入口而非扩宽 ``predict_rows`` 的返回：后者有按精确字典的契约测试，
        且 ``service.py`` 在生产推理路径消费同一返回。raw 只服务诊断与研究
        （C3 的 raw_blend 变体、B2 的 raw/calibrated 同口径对比）。
        """
        return self._predict_matrix(features)

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
            degraded_reason = f"native_backends_unavailable:lgbm={lgbm_backend},xgb={xgb_backend}"
        inference_blocked_reason = self.inference_blocked_reason()
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
            "inference_allowed": not bool(inference_blocked_reason),
            "inference_blocked_reason": inference_blocked_reason,
            "status_timestamp": status_timestamp,
            "created_at": str(self.artifact_metadata.get("artifact_created_at", "")),
            "calibration_method": str(self.artifact_metadata.get("calibration_method", "")),
            "meta_blend_weights": dict(self.meta_weights),
            # 发布别名自描述字段：当前加载模型在 registry 中的身份与 bundle 内容哈希。
            "registry_model_id": str(self.artifact_metadata.get("registry_model_id", "")),
            "bundle_content_hash": str(self.artifact_metadata.get("bundle_content_hash", "")),
        }

    def inference_blocked_reason(self) -> str:
        """Return a non-empty reason when production inference must be refused.

        A legacy fallback artifact without a feature scaler is loadable for
        diagnostics (``mode_details``) but must never produce predictions;
        native backends and scaler-bearing fallback models return "".
        """
        reasons: list[str] = []
        lgbm_blocked = self.lgbm.inference_blocked_reason()
        xgb_blocked = self.xgb.inference_blocked_reason()
        if lgbm_blocked:
            reasons.append(f"lgbm:{lgbm_blocked}")
        if xgb_blocked:
            reasons.append(f"xgb:{xgb_blocked}")
        return ";".join(reasons)


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
