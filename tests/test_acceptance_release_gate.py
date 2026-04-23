from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from pytest import MonkeyPatch
from typer.testing import CliRunner

import stock_analyzer.cli as cli_module
from stock_analyzer.acceptance_release_gate import (
    build_acceptance_release_gate_report,
    count_not_tested_checks,
)
from stock_analyzer.config import load_config
from stock_analyzer.runtime.service import StockAnalyzerService


def _as_mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, Any], value)
    raise AssertionError(f"Expected mapping, got {type(value).__name__}")


def test_build_acceptance_release_gate_report_passes_when_all_requirements_are_green() -> None:
    v13_report = {
        "status": "pass",
        "baseline_type": "native_baseline",
        "output_path": "artifacts/acceptance/v13_acceptance_report.json",
        "sections": {
            "11.1_mainline_credibility": {
                "checks": [
                    {"name": "native_artifact_load_rate", "status": "pass", "actual": 1.0},
                ]
            }
        },
    }

    report = _as_mapping(
        build_acceptance_release_gate_report(
            v13_acceptance_report=v13_report,
            closed_loop_smoke_passed=True,
            closed_loop_smoke_detail="pytest tests/test_service_closed_loop_flow.py",
        )
    )

    assert count_not_tested_checks(v13_acceptance_report=v13_report) == 0
    assert report["status"] == "pass"
    assert report["summary"]["fail"] == 0


def test_service_generate_acceptance_release_gate_report_persists(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.training.artifact_path = str(tmp_path / "model.json")
    config.training.baseline_report_path = str(
        tmp_path / "artifacts" / "acceptance" / "baseline_report.json"
    )
    service = StockAnalyzerService(config=config)

    v13_path = tmp_path / "artifacts" / "acceptance" / "v13_acceptance_report.json"
    v13_path.parent.mkdir(parents=True, exist_ok=True)
    v13_path.write_text(
        json.dumps(
            {
                "status": "fail",
                "baseline_type": "native_baseline",
                "sections": {
                    "11.4_runtime_quality": {
                        "checks": [
                            {
                                "name": "buy_watch_reasons_ratio",
                                "status": "warn",
                                "actual": "not_tested",
                            }
                        ]
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = _as_mapping(
        service.generate_acceptance_release_gate_report(
            v13_report_path=str(v13_path),
            output_path=str(tmp_path / "artifacts" / "acceptance" / "release_gate_report.json"),
            closed_loop_smoke_passed=True,
            closed_loop_smoke_detail="pytest tests/test_service_closed_loop_flow.py",
        )
    )

    assert Path(report["output_path"]).exists() is True
    assert report["status"] == "fail"
    assert report["summary"]["not_tested_count"] == 1


class _FakeReleaseGateService:
    def __init__(self, config: object) -> None:
        self._config = config

    def generate_acceptance_release_gate_report(
        self,
        *,
        v13_report_path: str | None = None,
        output_path: str | None = None,
        closed_loop_smoke_passed: bool = False,
        closed_loop_smoke_detail: str = "",
    ) -> dict[str, object]:
        return {
            "status": "fail",
            "checks": [],
            "v13_report_path": v13_report_path,
            "output_path": output_path,
            "closed_loop_smoke": {
                "passed": closed_loop_smoke_passed,
                "detail": closed_loop_smoke_detail,
            },
        }


def test_cli_acceptance_release_gate_returns_exit_code_2_when_blocked(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "get_config", lambda: object())
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakeReleaseGateService)

    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        [
            "acceptance-release-gate",
            "--closed-loop-smoke-passed",
            "--fail-on-blocked",
        ],
    )

    assert result.exit_code == 2
