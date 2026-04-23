from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from stock_analyzer.config import load_config
from stock_analyzer.stress.scenarios import run_default_stress_suite


def _as_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def _as_mapping_list(value: object) -> list[Mapping[str, object]]:
    assert isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    items = [_as_mapping(item) for item in value]
    assert len(items) == len(value)
    return items


def _as_int(value: object) -> int:
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


def test_default_stress_suite_returns_all_required_scenarios() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    report = run_default_stress_suite(config=config)

    summary = _as_mapping(report["summary"])
    assert _as_int(summary["scenario_count"]) >= 6
    assert _as_int(summary["failed_count"]) == 0
    scenarios = _as_mapping_list(report["scenarios"])
    assert len(scenarios) == _as_int(summary["scenario_count"])

    scenario_names = {item["name"] for item in scenarios}
    assert "2015_crash" in scenario_names
    assert "2023_grind_down" in scenario_names
