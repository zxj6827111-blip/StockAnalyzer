from __future__ import annotations

import json
from pathlib import Path

import pytest

from stock_analyzer.config import load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.models.adapters import inspect_model_backend_dependencies
from stock_analyzer.feature.engineer import FeatureEngineer
from stock_analyzer.models.predictor import SignalPredictor
from stock_analyzer.models.trainer import ModelTrainer


def test_model_training_and_predictor_roundtrip(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.training.min_samples = 40
    config.training.validation_ratio = 0.2
    artifact_path = tmp_path / "model.json"

    bars = SyntheticProvider(seed_offset=2468).fetch_daily_bars(symbol="600000", lookback_days=320)
    trainer = ModelTrainer(training=config.training, labels=config.labels, models=config.models)
    result = trainer.train_and_save(bars=bars, output_path=artifact_path)

    assert artifact_path.exists()
    assert result.samples_total >= 40
    assert "__random_baseline__" in result.artifact.feature_columns

    predictor = SignalPredictor.load(artifact_path)
    features = FeatureEngineer().transform(bars)
    probabilities = predictor.predict_row(features.iloc[-1])
    assert set(probabilities.keys()) == {"lgbm", "xgb", "meta"}


def test_model_training_persists_native_sidecars_when_dependencies_are_available(
    tmp_path: Path,
) -> None:
    dependencies = inspect_model_backend_dependencies()
    if not dependencies["lightgbm"]["installed"] or not dependencies["xgboost"]["installed"]:
        pytest.skip("native model dependencies are unavailable")

    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.training.min_samples = 40
    config.training.validation_ratio = 0.2
    artifact_path = tmp_path / "native_model.json"

    bars = SyntheticProvider(seed_offset=8642).fetch_daily_bars(symbol="600000", lookback_days=320)
    trainer = ModelTrainer(training=config.training, labels=config.labels, models=config.models)
    trainer.train_and_save(bars=bars, output_path=artifact_path)

    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    lgbm_model = payload["lgbm_model"]
    xgb_model = payload["xgb_model"]
    lgbm_sidecar_path = artifact_path.parent / str(lgbm_model["sidecar_path"])
    xgb_sidecar_path = artifact_path.parent / str(xgb_model["sidecar_path"])

    assert lgbm_model["backend"] == "lightgbm"
    assert xgb_model["backend"] == "xgboost"
    assert "native_blob" not in lgbm_model
    assert "native_blob_b64" not in xgb_model
    assert lgbm_sidecar_path.exists() is True
    assert xgb_sidecar_path.exists() is True

    predictor = SignalPredictor.load(artifact_path)
    mode = predictor.mode_details()
    assert mode["lgbm_load_source"] == "current_sidecar"
    assert mode["xgb_load_source"] == "current_sidecar"
    assert mode["native_sidecar_fallback_used"] is False
