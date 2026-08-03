from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

from stock_analyzer.config import load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.runtime.service import StockAnalyzerService
from stock_analyzer.training_diagnostics import build_label_conflict_shadow_report


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


def test_label_conflict_shadow_report_reports_embargo_days_not_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The diagnostics report must expose trading days as embargo_days.

    A two-symbol panel has more embargo rows than embargo trading days
    (reviewer repro: metrics embargo_days=6 / embargo_rows=12 /
    samples_embargo=12). The report must output 6 as embargo_days and 12 as
    embargo_rows, never 12 under the embargo_days name.
    """
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.training.min_samples = 40

    panel_like_result = SimpleNamespace(
        samples_train=50,
        samples_calibration=10,
        samples_test=10,
        samples_embargo=12,
        lgbm_backend="fallback_logit",
        xgb_backend="fallback_logit",
        metrics={"embargo_days": 6.0, "embargo_rows": 12.0, "auc": 0.5},
    )

    class _FakeTrainer:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def train_on_feature_label(self, **kwargs: object) -> object:
            return panel_like_result

    monkeypatch.setattr(
        "stock_analyzer.training_diagnostics.ModelTrainer",
        _FakeTrainer,
    )
    bars = SyntheticProvider(seed_offset=3131).fetch_daily_bars(symbol="600000", lookback_days=320)
    report = build_label_conflict_shadow_report(
        bars=bars,
        training=config.training,
        labels=config.labels,
        models=config.models,
    )

    items = _as_sequence(report["policies"])
    assert len(items) >= 1
    for item in items:
        item_view = _as_mapping(item)
        assert _as_int(item_view["embargo_days"]) == 6
        assert _as_int(item_view["embargo_rows"]) == 12
        metrics = _as_mapping(item_view["metrics"])
        assert float(metrics["embargo_days"]) == 6.0
        assert float(metrics["embargo_rows"]) == 12.0
