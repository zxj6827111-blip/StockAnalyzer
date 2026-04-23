from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pandas as pd

from stock_analyzer.research.catboost_shadow import (
    persist_catboost_shadow_report,
    run_catboost_shadow,
)


def _as_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _as_mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [_as_mapping(item) for item in value]


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        try:
            return int(float(text))
        except ValueError:
            return 0
    return 0


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


def test_catboost_shadow_runs_as_isolated_sidecar() -> None:
    frame = pd.DataFrame(
        [
            {
                "trade_date": f"2026-03-{day:02d}",
                "symbol": f"S{day:02d}",
                "momentum": 0.80 if day % 2 == 0 else 0.25,
                "quality": 0.75 if day % 2 == 0 else 0.30,
                "risk": 0.20 if day % 2 == 0 else 0.70,
                "turnover": 0.65 if day % 3 == 0 else 0.40,
                "label": 1 if day % 2 == 0 else 0,
                "p_meta": 0.58 if day % 2 == 0 else 0.42,
            }
            for day in range(1, 15)
        ]
    )

    report = run_catboost_shadow(reference_frame=frame, test_ratio=0.25)

    assert report["status"] == "ok"
    assert report["backend"] in {"catboost_shadow", "fallback_logit_shadow"}
    assert report["affects_main_model"] is False
    assert _as_int(report["train_samples"]) > 0
    assert _as_int(report["test_samples"]) > 0
    metrics = _as_mapping(report["metrics"])
    assert _as_float(metrics["logloss"]) >= 0.0
    assert len(_as_mapping_list(report["top_features"])) >= 1


def test_catboost_shadow_persists_report(tmp_path: Path) -> None:
    report = {
        "status": "ok",
        "engine": "catboost_shadow_sidecar",
        "backend": "fallback_logit_shadow",
        "affects_main_model": False,
        "metrics": {"accuracy": 0.6, "brier": 0.2, "logloss": 0.61},
    }
    output_path = tmp_path / "research" / "catboost_shadow_report.json"
    written = persist_catboost_shadow_report(report=report, output_path=output_path)

    payload = json.loads(Path(written).read_text(encoding="utf-8"))
    assert payload["backend"] == "fallback_logit_shadow"
    assert payload["affects_main_model"] is False
