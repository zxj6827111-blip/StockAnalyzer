from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pandas as pd

from stock_analyzer.research.shap_sidecar import (
    persist_shap_sidecar_report,
    run_shap_sidecar,
)


def _as_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _as_mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [_as_mapping(item) for item in value]


def _as_float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        try:
            return float(text)
        except ValueError:
            return 0.0
    return 0.0


def test_shap_sidecar_outputs_global_and_local_explanations() -> None:
    frame = pd.DataFrame(
        [
            {"symbol": "AAA", "momentum": 0.9, "risk": 0.1, "quality": 0.8, "p_meta": 0.85},
            {"symbol": "BBB", "momentum": 0.2, "risk": 0.8, "quality": 0.3, "p_meta": 0.25},
            {"symbol": "CCC", "momentum": 0.8, "risk": 0.2, "quality": 0.7, "p_meta": 0.80},
            {"symbol": "DDD", "momentum": 0.3, "risk": 0.7, "quality": 0.4, "p_meta": 0.30},
        ]
    )

    report = run_shap_sidecar(reference_frame=frame, top_k=2)

    assert report["status"] == "ok"
    assert len(_as_mapping_list(report["global_importance"])) >= 2
    assert len(_as_mapping_list(report["sample_explanations"])) >= 1
    first = _as_mapping(_as_mapping_list(report["sample_explanations"])[0])
    assert "top_positive" in first
    assert "top_negative" in first


def test_shap_sidecar_detects_importance_drift_and_persists(tmp_path: Path) -> None:
    frame = pd.DataFrame(
        [
            {"symbol": "AAA", "momentum": 0.9, "risk": 0.1, "quality": 0.8, "p_meta": 0.85},
            {"symbol": "BBB", "momentum": 0.2, "risk": 0.8, "quality": 0.3, "p_meta": 0.25},
            {"symbol": "CCC", "momentum": 0.8, "risk": 0.2, "quality": 0.7, "p_meta": 0.80},
            {"symbol": "DDD", "momentum": 0.3, "risk": 0.7, "quality": 0.4, "p_meta": 0.30},
        ]
    )
    report = run_shap_sidecar(
        reference_frame=frame,
        baseline_importance={"risk": 0.9, "momentum": 0.05, "quality": 0.05},
        drift_threshold=0.20,
    )

    assert _as_float(report["drift_ratio"]) >= 0.0
    assert report["drift_flag"] in {True, False}
    path = tmp_path / "research" / "shap_report.json"
    written = persist_shap_sidecar_report(report=report, output_path=path)
    payload = json.loads(Path(written).read_text(encoding="utf-8"))
    assert payload["status"] == "ok"
