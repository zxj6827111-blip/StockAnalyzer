from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

from stock_analyzer.config import load_config
from stock_analyzer.runtime.service import StockAnalyzerService


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    raise AssertionError(f"Expected mapping, got {type(value).__name__}")


def test_service_generates_v13_acceptance_bundle(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.training.min_samples = 40
    config.training.artifact_path = str(tmp_path / "model.json")
    config.training.baseline_report_path = str(
        tmp_path / "artifacts" / "acceptance" / "baseline_report.json"
    )

    service = StockAnalyzerService(config=config)
    service.train_models(
        symbol="600000",
        lookback_days=320,
        artifact_path=config.training.artifact_path,
        full_market=False,
    )

    bundle = _as_mapping(
        service.generate_v13_acceptance_bundle(
            symbol="600000",
            lookback_days=320,
            baseline_output_path=config.training.baseline_report_path,
            v13_output_path=str(
                tmp_path / "artifacts" / "acceptance" / "v13_acceptance_report.json"
            ),
            run_week5_scan=False,
        )
    )

    baseline = _as_mapping(bundle["baseline"])
    phase_checkpoints = _as_mapping(bundle["phase_checkpoints"])
    m9_failure_retention = _as_mapping(bundle["m9_failure_retention"])
    portfolio_execution = _as_mapping(bundle["portfolio_execution"])
    label_conflict_shadow = _as_mapping(bundle["label_conflict_shadow"])
    v13_acceptance = _as_mapping(bundle["v13_acceptance"])
    shadow_summary = _as_mapping(v13_acceptance["label_conflict_shadow_summary"])

    assert baseline["baseline_type"] == "native_baseline"
    assert Path(str(baseline["output_path"])).exists() is True
    assert set(phase_checkpoints.keys()) == {"A", "B", "C"}
    assert Path(str(m9_failure_retention["output_path"])).exists() is True
    assert Path(str(portfolio_execution["output_path"])).exists() is True
    assert Path(str(label_conflict_shadow["output_path"])).exists() is True
    assert shadow_summary["report_present"] is True
    assert int(shadow_summary["policy_count"]) >= 3
    assert Path(str(v13_acceptance["output_path"])).exists() is True
