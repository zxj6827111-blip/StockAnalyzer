"""P1-1 regression tests: legacy no-scaler artifacts must not predict.

A legacy fallback artifact (logistic payload without the fitted feature
scaler) must stay loadable for diagnostics, but every production inference
entry point must refuse it with ``ValueError``; scaler-bearing fallback
artifacts keep predicting normally; the pipeline fails closed (hold,
score 0, explicit reason) when the predictor refuses.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stock_analyzer.config import load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.models.adapters import LightGBMAdapter, XGBoostAdapter
from stock_analyzer.models.artifact import ModelArtifact
from stock_analyzer.models.calibration import IsotonicCalibrator
from stock_analyzer.models.fallback import LogisticProbModel
from stock_analyzer.models.predictor import SignalPredictor
from stock_analyzer.pipeline import AnalyzerPipeline


def _fit_scaled_model(seed: int = 11) -> LogisticProbModel:
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(60, 4))
    y = ((x[:, 0] + x[:, 1]) > 0.0).astype(float)
    model = LogisticProbModel(epochs=60, seed=seed)
    model.fit(x, y)
    assert model.scaler is not None
    return model


def _calibrator() -> IsotonicCalibrator:
    calibrator = IsotonicCalibrator()
    calibrator.fit(
        np.asarray([0.1, 0.2, 0.4, 0.6, 0.8, 0.9], dtype=float),
        np.asarray([0, 0, 0, 1, 1, 1], dtype=float),
    )
    return calibrator


def _legacy_payload() -> dict[str, object]:
    model = _fit_scaled_model(seed=7)
    payload = model.to_dict()
    payload.pop("scaler", None)
    return payload


def _artifact_from_payload(payload: dict[str, object], *, path: Path) -> ModelArtifact:
    calibrator = _calibrator()
    artifact = ModelArtifact.create(
        feature_columns=[f"f{i}" for i in range(4)],
        lgbm_model={"backend": "fallback_logit", "payload": payload},
        xgb_model={"backend": "fallback_logit", "payload": payload},
        lgbm_calibrator=calibrator.to_dict(),
        xgb_calibrator=calibrator.to_dict(),
        training_metrics={"accuracy": 0.5},
        metadata={
            "lgbm_backend": "fallback_logit",
            "xgb_backend": "fallback_logit",
            "degraded_model_mode": True,
        },
    )
    artifact.save(path)
    return artifact


def _feature_row() -> pd.Series:
    return pd.Series({"f0": 0.1, "f1": -0.2, "f2": 0.3, "f3": 0.0})


def test_legacy_artifact_loads_and_reports_diagnostics(tmp_path: Path) -> None:
    artifact_path = tmp_path / "legacy.json"
    _artifact_from_payload(_legacy_payload(), path=artifact_path)

    predictor = SignalPredictor.load(artifact_path)
    details = predictor.mode_details()

    assert details["predictor_mode"] == "artifact_loaded"
    assert details["inference_allowed"] is False
    assert "legacy_no_scaler" in str(details["inference_blocked_reason"])
    assert details["degraded_model_mode"] is True


def test_legacy_predict_row_raises_value_error(tmp_path: Path) -> None:
    artifact_path = tmp_path / "legacy.json"
    _artifact_from_payload(_legacy_payload(), path=artifact_path)

    predictor = SignalPredictor.load(artifact_path)
    with pytest.raises(ValueError, match="legacy_no_scaler"):
        predictor.predict_row(_feature_row())


def test_legacy_adapter_predict_proba_raises_value_error() -> None:
    legacy = LogisticProbModel.from_dict(_legacy_payload())
    assert legacy.inference_blocked_reason == "legacy_no_scaler"
    with pytest.raises(ValueError, match="legacy_no_scaler"):
        legacy.predict_proba(np.zeros((1, 4), dtype=float))

    lgbm = LightGBMAdapter()
    lgbm._model = legacy
    assert lgbm.inference_blocked_reason() == "legacy_no_scaler"
    with pytest.raises(ValueError, match="legacy_no_scaler"):
        lgbm.predict_proba(np.zeros((1, 4), dtype=float))

    xgb = XGBoostAdapter()
    xgb._model = legacy
    assert xgb.inference_blocked_reason() == "legacy_no_scaler"
    with pytest.raises(ValueError, match="legacy_no_scaler"):
        xgb.predict_proba(np.zeros((1, 4), dtype=float))


def test_scaler_fallback_artifact_still_predicts(tmp_path: Path) -> None:
    artifact_path = tmp_path / "scaled.json"
    _artifact_from_payload(_fit_scaled_model(seed=9).to_dict(), path=artifact_path)

    predictor = SignalPredictor.load(artifact_path)
    details = predictor.mode_details()
    assert details["inference_allowed"] is True
    assert details["inference_blocked_reason"] == ""

    probabilities = predictor.predict_row(_feature_row())
    assert set(probabilities.keys()) == {"lgbm", "xgb", "meta"}
    for value in probabilities.values():
        assert 0.0 <= value <= 1.0


def test_predict_rows_matches_predict_row_per_row(tmp_path: Path) -> None:
    artifact_path = tmp_path / "scaled_batch.json"
    _artifact_from_payload(_fit_scaled_model(seed=19).to_dict(), path=artifact_path)
    predictor = SignalPredictor.load(artifact_path)

    # First frame drops f3 entirely: predict_rows must fill 0.0 exactly like
    # predict_row does for a row without that column.
    partial_rows = [
        {"f0": 0.1, "f1": -0.2, "f2": 0.3},
        {"f0": 0.5, "f1": 0.25, "f2": -0.4},
        {"f0": -0.7, "f1": 0.1, "f2": 0.6},
        {"f0": 0.2, "f1": -0.4, "f2": 0.0},
    ]
    partial_frame = pd.DataFrame(partial_rows)
    partial_batch = predictor.predict_rows(partial_frame)
    assert set(partial_batch.keys()) == {"lgbm", "xgb", "meta"}
    assert all(len(values) == len(partial_frame) for values in partial_batch.values())
    for index, row in enumerate(partial_rows):
        single = predictor.predict_row(pd.Series(row))
        for key in ("lgbm", "xgb", "meta"):
            assert partial_batch[key][index] == single[key]

    # Full feature frame must agree too, and extra columns must be ignored.
    full_rows = [
        {"f0": 0.1, "f1": -0.2, "f2": 0.3, "f3": 0.0},
        {"f0": 0.5, "f1": 0.25, "f2": -0.4, "f3": 0.9},
        {"f0": -0.7, "f1": 0.1, "f2": 0.6, "f3": -0.3},
        {"f0": 0.2, "f1": -0.4, "f2": 0.0, "f3": 0.1},
    ]
    full_frame = pd.DataFrame(full_rows)
    full_batch = predictor.predict_rows(full_frame)
    assert all(len(values) == len(full_frame) for values in full_batch.values())
    for index, row in enumerate(full_rows):
        single = predictor.predict_row(pd.Series(row))
        for key in ("lgbm", "xgb", "meta"):
            assert full_batch[key][index] == single[key]
    widened = full_frame.copy()
    widened["unused_extra"] = 1.0
    assert predictor.predict_rows(widened) == full_batch

    # Every value stays a clamped probability.
    for values in full_batch.values():
        for value in values:
            assert 0.0 <= value <= 1.0


def test_predict_rows_rejects_legacy_artifact(tmp_path: Path) -> None:
    artifact_path = tmp_path / "legacy_batch.json"
    _artifact_from_payload(_legacy_payload(), path=artifact_path)

    predictor = SignalPredictor.load(artifact_path)
    frame = pd.DataFrame([_feature_row()])
    with pytest.raises(ValueError, match="legacy_no_scaler"):
        predictor.predict_rows(frame)


def test_predict_rows_empty_frame_returns_empty_lists(tmp_path: Path) -> None:
    artifact_path = tmp_path / "scaled_empty.json"
    _artifact_from_payload(_fit_scaled_model(seed=23).to_dict(), path=artifact_path)

    predictor = SignalPredictor.load(artifact_path)
    batch = predictor.predict_rows(pd.DataFrame(columns=[f"f{i}" for i in range(4)]))
    assert batch == {"lgbm": [], "xgb": [], "meta": []}


def test_scaler_fallback_adapter_from_dict_roundtrip() -> None:
    serialized = {"backend": "fallback_logit", "payload": _fit_scaled_model(seed=3).to_dict()}
    lgbm = LightGBMAdapter.from_dict(serialized)
    assert lgbm.inference_blocked_reason() == ""
    xgb = XGBoostAdapter.from_dict(serialized)
    assert xgb.inference_blocked_reason() == ""
    matrix = np.zeros((1, 4), dtype=float)
    assert lgbm.predict_proba(matrix).shape == (1,)
    assert xgb.predict_proba(matrix).shape == (1,)


def test_pipeline_fails_closed_on_legacy_artifact(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    artifact_path = tmp_path / "legacy.json"
    _artifact_from_payload(_legacy_payload(), path=artifact_path)
    config.training.artifact_path = str(artifact_path)

    provider = SyntheticProvider(seed_offset=4242)
    pipeline = AnalyzerPipeline(config=config, provider=provider)
    status = pipeline.provider_status()
    assert status["model_loaded"] is True
    assert status["inference_allowed"] is False
    assert "legacy_no_scaler" in str(status.get("inference_blocked_reason", ""))

    report = pipeline.run_once(symbols=["600000"], strategy="trend", current_equity=1.0)
    signal = report.signals[0]
    assert signal.action == "hold"
    assert signal.score == 0.0
    assert signal.target_position == 0.0
    assert any(reason.startswith("predictor_rejected:") for reason in signal.reasons)


def test_pipeline_predicts_with_scaler_fallback_artifact(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    artifact_path = tmp_path / "scaled.json"
    _artifact_from_payload(_fit_scaled_model(seed=13).to_dict(), path=artifact_path)
    config.training.artifact_path = str(artifact_path)

    provider = SyntheticProvider(seed_offset=5151)
    pipeline = AnalyzerPipeline(config=config, provider=provider)
    status = pipeline.provider_status()
    assert status["model_loaded"] is True
    assert status["inference_allowed"] is True

    report = pipeline.run_once(symbols=["600000"], strategy="trend", current_equity=1.0)
    signal = report.signals[0]
    assert not any(reason.startswith("predictor_rejected:") for reason in signal.reasons)


def _sigmoid(values: np.ndarray) -> np.ndarray:
    output = np.empty_like(values, dtype=float)
    positive = values >= 0
    output[positive] = 1.0 / (1.0 + np.exp(-values[positive]))
    negative = values[~positive]
    exp_negative = np.exp(negative)
    output[~positive] = exp_negative / (1.0 + exp_negative)
    return output


def _manual_scaled_gd(
    *,
    x: np.ndarray,
    y: np.ndarray,
    epochs: int,
    learning_rate: float,
    l2: float,
    seed: int,
) -> tuple[np.ndarray, float]:
    """Independent gradient descent on standardized features.

    Mirrors the model's training contract: scaler recorded from x first,
    then gradient descent on the scaled coordinates. Used to prove the
    fitted weights live in the scaled coordinate system.
    """
    mean = x.mean(axis=0)
    scale = x.std(axis=0)
    scale = np.where(np.abs(scale) < 1e-12, 1.0, scale)
    scaled = (x - mean) / scale
    rng = np.random.default_rng(seed)
    weights = rng.normal(loc=0.0, scale=0.01, size=x.shape[1]).astype(float)
    bias = 0.0
    for _ in range(epochs):
        logits = scaled @ weights + bias
        errors = _sigmoid(logits) - y
        weights -= learning_rate * (scaled.T @ errors / len(y) + l2 * weights)
        bias -= learning_rate * float(errors.sum() / len(y))
    return weights, bias


def test_fit_trains_and_predicts_in_the_same_scaled_coordinates() -> None:
    rng = np.random.default_rng(99)
    x = rng.normal(size=(80, 5)) * np.asarray([3.0, 0.5, 10.0, 2.0, 0.1])
    x = x + np.asarray([100.0, -5.0, 0.0, 25.0, 0.3])
    y = ((x[:, 0] / 3.0 + x[:, 2] / 10.0 - 0.2 * x[:, 4]) > 0).astype(float)

    model = LogisticProbModel(learning_rate=0.1, epochs=400, l2=1e-3, seed=21)
    model.fit(x, y)

    # Scaler is recorded from the training input.
    expected_mean = x.mean(axis=0)
    expected_scale = np.where(np.abs(x.std(axis=0)) < 1e-12, 1.0, x.std(axis=0))
    assert model.scaler is not None
    assert np.allclose(model.scaler["mean"], expected_mean)
    assert np.allclose(model.scaler["scale"], expected_scale)

    # The fitted weights must match an independent gradient-descent run on
    # the scaled coordinates - a raw-coordinate fit would diverge here.
    reference_weights, reference_bias = _manual_scaled_gd(
        x=x,
        y=y,
        epochs=400,
        learning_rate=0.1,
        l2=1e-3,
        seed=21,
    )
    assert np.allclose(model.weights, reference_weights, atol=1e-9)
    assert abs(model.bias - reference_bias) < 1e-9

    # Training-path and inference-path probabilities are identical: the
    # mean absolute difference is exactly zero, not ~0.2.
    scaled = (x - model.scaler["mean"]) / model.scaler["scale"]
    train_path = _sigmoid(scaled @ model.weights + model.bias)
    inference_path = model.predict_proba(x)
    mean_abs_diff = float(np.mean(np.abs(train_path - inference_path)))
    assert mean_abs_diff == 0.0

    # And the inference path is the well-calibrated one on the training set.
    train_brier = float(np.mean((inference_path - y) ** 2))
    assert train_brier < 0.10
