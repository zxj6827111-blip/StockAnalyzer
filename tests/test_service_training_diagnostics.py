from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from stock_analyzer.config import load_config
from stock_analyzer.runtime.service import StockAnalyzerService


def _as_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def _as_sequence(value: object) -> Sequence[object]:
    assert isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    return value


def _as_text(value: object) -> str:
    assert isinstance(value, str)
    return value


def _as_int(value: object) -> int:
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


def test_service_generates_training_evaluation_report(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.training.min_samples = 40
    config.training.artifact_path = str(tmp_path / "model.json")

    service = StockAnalyzerService(config=config)
    report = service.generate_training_evaluation_report(
        symbol="600000",
        lookback_days=320,
        output_path=str(tmp_path / "artifacts" / "acceptance" / "training_evaluation_report.json"),
    )

    split_regimes = _as_mapping(report["split_regimes"])
    strict = _as_mapping(split_regimes["strict_temporal"])
    legacy = _as_mapping(split_regimes["legacy_validation_only"])

    assert Path(_as_text(report["output_path"])).exists() is True
    assert strict["uses_distinct_calibration_and_test"] is True
    assert _as_int(strict["calibration_samples"]) > 0
    assert _as_int(strict["test_samples"]) > 0
    assert legacy["uses_distinct_calibration_and_test"] is False
    assert "warning" in legacy


def test_service_generates_label_conflict_shadow_report(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.training.min_samples = 40
    config.training.artifact_path = str(tmp_path / "model.json")

    service = StockAnalyzerService(config=config)
    report = service.generate_label_conflict_shadow_report(
        symbol="600000",
        lookback_days=320,
        output_path=str(
            tmp_path / "artifacts" / "acceptance" / "label_conflict_shadow_report.json"
        ),
    )

    items = _as_sequence(report["policies"])

    assert Path(_as_text(report["output_path"])).exists() is True
    assert report["configured_policy"] == config.labels.conflict_policy
    assert len(items) >= 3
    assert any(_as_mapping(item)["policy"] == "bar_shape_heuristic" for item in items)
    assert any(_as_mapping(item)["policy"] == "soft_label" for item in items)
