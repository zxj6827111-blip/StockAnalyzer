from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from stock_analyzer.config import load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.models.adapters import inspect_model_backend_dependencies
from stock_analyzer.models.artifact import ModelArtifact
from stock_analyzer.models.calibration import IsotonicCalibrator
from stock_analyzer.models.fallback import LogisticProbModel
from stock_analyzer.models.predictor import SignalPredictor
from stock_analyzer.models.trainer import ModelTrainer
from stock_analyzer.pipeline import AnalyzerPipeline


def test_pipeline_loads_trained_artifact_for_inference(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.training.min_samples = 40
    config.training.artifact_path = str(tmp_path / "artifact.json")

    provider = SyntheticProvider(seed_offset=1234)
    bars = provider.fetch_daily_bars("600000", lookback_days=300)
    trainer = ModelTrainer(training=config.training, labels=config.labels)
    trainer.train_and_save(bars=bars, output_path=config.training.artifact_path)

    pipeline = AnalyzerPipeline(config=config, provider=provider)
    status = pipeline.provider_status()
    assert status["model_loaded"] is True
    assert str(status["status_timestamp"]).strip()
    if status["degraded_model_mode"]:
        assert str(status["degrade_reason"]).strip()
        assert str(status["degraded_reason_at"]).strip()

    report = pipeline.run_once(symbols=["600000"], strategy="trend", current_equity=1.0)
    assert len(report.signals) == 1


def test_pipeline_exposes_controlled_heuristic_status_when_artifact_is_missing(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.training.artifact_path = str(tmp_path / "missing_artifact.json")

    provider = SyntheticProvider(seed_offset=1234)
    pipeline = AnalyzerPipeline(config=config, provider=provider)
    status = pipeline.provider_status()

    assert status["model_loaded"] is False
    assert status["predictor_mode"] == "controlled_heuristic"
    assert status["reason"] == "artifact_missing"
    assert str(status["degraded_reason_at"]).strip()
    assert str(status["status_timestamp"]).strip()


def test_pipeline_exposes_controlled_heuristic_status_when_artifact_is_corrupted(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.training.artifact_path = str(tmp_path / "broken_artifact.json")
    Path(config.training.artifact_path).write_text("{invalid-json", encoding="utf-8")

    provider = SyntheticProvider(seed_offset=1234)
    pipeline = AnalyzerPipeline(config=config, provider=provider)
    status = pipeline.provider_status()

    assert status["model_loaded"] is False
    assert status["predictor_mode"] == "controlled_heuristic"
    assert str(status["reason"]).startswith("artifact_load_failed:")
    assert str(status["degraded_reason_at"]).strip()
    assert str(status["status_timestamp"]).strip()


def test_pipeline_exposes_fallback_backend_status_with_timestamp(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    artifact_path = tmp_path / "fallback_artifact.json"
    config.training.artifact_path = str(artifact_path)

    fallback = LogisticProbModel()
    x = np.asarray([[0.0], [1.0], [2.0], [3.0]], dtype=float)
    y = np.asarray([0.0, 0.0, 1.0, 1.0], dtype=float)
    fallback.fit(x, y)
    calibrator = IsotonicCalibrator()
    calibrator.fit(np.asarray([0.1, 0.2, 0.8, 0.9], dtype=float), y)
    artifact = ModelArtifact.create(
        feature_columns=["feature_1"],
        lgbm_model={"backend": "fallback_logit", "payload": fallback.to_dict()},
        xgb_model={"backend": "fallback_logit", "payload": fallback.to_dict()},
        lgbm_calibrator=calibrator.to_dict(),
        xgb_calibrator=calibrator.to_dict(),
        training_metrics={"accuracy": 1.0},
        metadata={
            "lgbm_backend": "fallback_logit",
            "xgb_backend": "fallback_logit",
            "degraded_model_mode": True,
        },
    )
    artifact.save(artifact_path)

    provider = SyntheticProvider(seed_offset=1234)
    pipeline = AnalyzerPipeline(config=config, provider=provider)
    status = pipeline.provider_status()

    assert status["model_loaded"] is True
    assert status["predictor_mode"] == "artifact_loaded"
    assert status["degraded_model_mode"] is True
    assert str(status["degraded_reason"]).startswith("native_backends_unavailable:")
    assert str(status["degraded_reason_at"]).strip()
    assert str(status["status_timestamp"]).strip()


def test_predictor_can_fallback_to_previous_native_sidecar_when_latest_is_corrupted(
    tmp_path: Path,
) -> None:
    dependencies = inspect_model_backend_dependencies()
    if not dependencies["lightgbm"]["installed"] or not dependencies["xgboost"]["installed"]:
        pytest.skip("native model dependencies are unavailable")

    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    artifact_path = tmp_path / "native_model.json"
    config.training.artifact_path = str(artifact_path)
    config.training.min_samples = 40

    provider = SyntheticProvider(seed_offset=4321)
    bars = provider.fetch_daily_bars("600000", lookback_days=320)
    trainer = ModelTrainer(training=config.training, labels=config.labels, models=config.models)
    result = trainer.train_and_save(bars=bars, output_path=artifact_path)
    result.artifact.save(artifact_path)

    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    lgbm_model = payload["lgbm_model"]
    xgb_model = payload["xgb_model"]
    assert str(lgbm_model.get("fallback_sidecar_path", "")).strip()
    assert str(xgb_model.get("fallback_sidecar_path", "")).strip()

    (artifact_path.parent / str(lgbm_model["sidecar_path"])).write_text(
        "corrupted lightgbm sidecar",
        encoding="utf-8",
    )
    (artifact_path.parent / str(xgb_model["sidecar_path"])).write_bytes(b"broken-xgboost-sidecar")

    predictor = SignalPredictor.load(artifact_path)
    status = predictor.mode_details()

    assert status["predictor_mode"] == "artifact_loaded"
    assert status["lgbm_backend"] == "lightgbm"
    assert status["xgb_backend"] == "xgboost"
    assert status["lgbm_load_source"] == "fallback_sidecar"
    assert status["xgb_load_source"] == "fallback_sidecar"
    assert status["native_sidecar_fallback_used"] is True
