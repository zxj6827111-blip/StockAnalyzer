from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from stock_analyzer.config import load_config
from stock_analyzer.models.adapters import inspect_model_backend_dependencies
from stock_analyzer.runtime.service import StockAnalyzerService


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    raise AssertionError(f"Expected mapping, got {type(value).__name__}")


def _as_int(value: object) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    raise AssertionError(f"Expected int, got {value!r}")


def test_service_can_generate_baseline_report(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.training.artifact_path = str(tmp_path / "missing_model.json")
    config.training.min_samples = 40
    output_path = tmp_path / "artifacts" / "acceptance" / "baseline_report.json"

    service = StockAnalyzerService(config=config)
    report = _as_mapping(
        service.generate_baseline_report(
            symbol="600000",
            lookback_days=320,
            output_path=str(output_path),
        )
    )

    assert output_path.exists() is True
    assert report["baseline_type"] in {"heuristic_baseline", "fallback_baseline", "native_baseline"}
    walk_forward = _as_mapping(report["walk_forward"])
    summary = _as_mapping(walk_forward["summary"])
    background_factor_coverage = _as_mapping(report["background_factor_coverage"])
    assert _as_int(summary["folds"]) >= 1
    assert "holder_count" in background_factor_coverage


def test_service_baseline_report_uses_native_backends_after_training(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    artifact_path = tmp_path / "model.json"
    output_path = tmp_path / "artifacts" / "acceptance" / "baseline_report.json"
    config.training.artifact_path = str(artifact_path)
    config.training.min_samples = 40

    service = StockAnalyzerService(config=config)
    training = service.train_models(
        symbol="600000",
        lookback_days=320,
        artifact_path=str(artifact_path),
        full_market=False,
    )

    assert training["predictor_loaded"] is True

    report = _as_mapping(
        service.generate_baseline_report(
            symbol="600000",
            lookback_days=320,
            output_path=str(output_path),
        )
    )

    dependency_status = _as_mapping(report["dependency_status"])
    model_status = _as_mapping(report["model_status"])
    assert output_path.exists() is True
    assert report["baseline_type"] == "native_baseline"
    assert _as_mapping(dependency_status["lightgbm"])["installed"] is True
    assert _as_mapping(dependency_status["xgboost"])["installed"] is True
    assert model_status["lgbm_backend"] == "lightgbm"
    assert model_status["xgb_backend"] == "xgboost"
    assert model_status["lgbm_load_source"] == "current_sidecar"
    assert model_status["xgb_load_source"] == "current_sidecar"
    assert model_status["native_sidecar_fallback_used"] is False
    assert model_status["degraded_model_mode"] is False


def test_service_restart_preserves_native_sidecar_backends(tmp_path: Path) -> None:
    dependencies = inspect_model_backend_dependencies()
    if not dependencies["lightgbm"]["installed"] or not dependencies["xgboost"]["installed"]:
        pytest.skip("native model dependencies are unavailable")

    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    artifact_path = tmp_path / "model.json"
    config.training.artifact_path = str(artifact_path)
    config.training.min_samples = 40

    first = StockAnalyzerService(config=config)
    training = _as_mapping(
        first.train_models(
            symbol="600000",
            lookback_days=320,
            artifact_path=str(artifact_path),
            full_market=False,
        )
    )

    assert training["predictor_loaded"] is True

    restarted = StockAnalyzerService(config=config)
    status = _as_mapping(restarted.provider_status())

    assert status["predictor_mode"] == "artifact_loaded"
    assert status["lgbm_backend"] == "lightgbm"
    assert status["xgb_backend"] == "xgboost"
    assert status["lgbm_load_source"] == "current_sidecar"
    assert status["xgb_load_source"] == "current_sidecar"
    assert status["native_sidecar_fallback_used"] is False
