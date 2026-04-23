"""Execution-risk sidecar inference."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from stock_analyzer.models.calibration import IsotonicCalibrator
from stock_analyzer.models.execution_risk_artifact import ExecutionRiskArtifact
from stock_analyzer.models.fallback import LogisticProbModel


@dataclass(slots=True)
class ExecutionRiskPredictor:
    """Predict execution-risk probabilities from flattened feature vectors."""

    feature_names: list[str]
    models: dict[str, LogisticProbModel]
    calibrators: dict[str, IsotonicCalibrator]
    artifact_metadata: dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_artifact(cls, artifact: ExecutionRiskArtifact) -> ExecutionRiskPredictor:
        models: dict[str, LogisticProbModel] = {}
        calibrators: dict[str, IsotonicCalibrator] = {}
        for target_name, payload in artifact.target_models.items():
            if not isinstance(payload, dict):
                continue
            model_payload = payload.get("model", {})
            calibrator_payload = payload.get("calibrator", {})
            if not isinstance(model_payload, dict) or not isinstance(calibrator_payload, dict):
                continue
            models[target_name] = LogisticProbModel.from_dict(model_payload)
            calibrators[target_name] = IsotonicCalibrator.from_dict(calibrator_payload)
        if not models:
            raise ValueError("execution-risk artifact has no trained targets")
        return cls(
            feature_names=list(artifact.feature_names),
            models=models,
            calibrators=calibrators,
            artifact_metadata=dict(artifact.metadata),
        )

    @classmethod
    def load(cls, path: str | Path) -> ExecutionRiskPredictor:
        return cls.from_artifact(ExecutionRiskArtifact.load(path))

    def predict_features(self, features: dict[str, float] | object) -> dict[str, float]:
        if not isinstance(features, dict):
            raise ValueError("execution-risk predictor requires a dict-like feature payload")
        vector = np.asarray(
            [float(features.get(name, 0.0)) for name in self.feature_names],
            dtype=float,
        ).reshape(1, -1)
        predictions: dict[str, float] = {}
        for target_name, model in self.models.items():
            calibrator = self.calibrators.get(target_name)
            if calibrator is None:
                continue
            raw_score = model.predict_proba(vector)
            calibrated = calibrator.predict(raw_score)
            predictions[target_name] = _clamp_prob(float(calibrated[0]))
        return predictions


def _clamp_prob(value: float) -> float:
    return max(0.0, min(1.0, value))
