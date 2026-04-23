from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from stock_analyzer.research.alphalens_sidecar import (
    persist_alphalens_sidecar_report,
    run_alphalens_sidecar,
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


def test_alphalens_sidecar_reports_factor_health_and_decay() -> None:
    records = []
    closes = {
        "AAA": [10.0, 10.2, 10.4, 10.6, 10.8, 11.0],
        "BBB": [10.0, 10.1, 10.0, 9.9, 9.8, 9.7],
        "CCC": [10.0, 10.05, 10.1, 10.15, 10.2, 10.3],
    }
    quality = {"AAA": 0.9, "BBB": 0.1, "CCC": 0.6}
    noise = {"AAA": 0.4, "BBB": 0.5, "CCC": 0.45}
    for offset in range(6):
        trade_date = f"2026-03-0{offset + 1}"
        for symbol in ("AAA", "BBB", "CCC"):
            records.append(
                {
                    "symbol": symbol,
                    "trade_date": trade_date,
                    "close": closes[symbol][offset],
                    "quality_factor": quality[symbol],
                    "noise_factor": noise[symbol],
                }
            )

    report = run_alphalens_sidecar(records=records, horizons=(1, 2), quantiles=3)

    assert report["status"] == "ok"
    assert _as_int(report["factor_count"]) == 2
    factors = {str(item["factor"]): item for item in _as_mapping_list(report["factors"])}
    assert _as_int(factors["quality_factor"]["best_horizon"]) in {1, 2}
    assert (
        _as_float(factors["quality_factor"]["max_abs_rank_ic"])
        >= _as_float(factors["noise_factor"]["max_abs_rank_ic"])
    )


def test_alphalens_sidecar_persists_report(tmp_path: Path) -> None:
    report = {
        "status": "ok",
        "engine": "alphalens_fallback",
        "records": 10,
        "factor_count": 1,
        "horizons": [1, 5],
        "factors": [{"factor": "demo_factor"}],
    }
    path = tmp_path / "research" / "alphalens_report.json"
    written = persist_alphalens_sidecar_report(report=report, output_path=path)

    assert Path(written).exists() is True
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["factor_count"] == 1
