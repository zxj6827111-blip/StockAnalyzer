from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

import stock_analyzer.cli as cli_module
from stock_analyzer.phase_d_status import build_phase_d_status_report


def test_build_phase_d_status_report_lists_deferred_items() -> None:
    report = build_phase_d_status_report()

    items = cast(Sequence[Mapping[str, object]], report["items"])
    ids = [str(item.get("id", "")) for item in items]

    assert report["phase"] == "D"
    assert report["overall_status"] == "completed"
    assert ids == [
        "alphalens_sidecar",
        "shap_sidecar",
        "catboost_shadow",
        "finbert_sidecar",
        "qlib_bridge",
    ]
    assert all(str(item.get("status", "")) == "completed" for item in items)
    assert all(str(item.get("delivery_mode", "")) == "research_sidecar" for item in items)


class _FakePhaseDService:
    def __init__(self, config: object) -> None:
        self._config = config

    def generate_phase_d_status_report(
        self, *, output_path: str | None = None
    ) -> dict[str, object]:
        return {"phase": "D", "overall_status": "completed", "output_path": output_path or ""}

    def build_phase_d_alphalens_report(self, **_: object) -> dict[str, object]:
        return {"research_id": "alphalens_sidecar", "status": "ok"}

    def build_phase_d_finbert_report(self, **_: object) -> dict[str, object]:
        return {"research_id": "finbert_sidecar", "status": "ok"}

    def build_phase_d_tabular_deep_report(self, **_: object) -> dict[str, object]:
        return {"research_id": "tabnet_ft_transformer", "status": "ok"}

    def build_phase_d_finrl_report(self, **_: object) -> dict[str, object]:
        return {"research_id": "finrl_sidecar", "status": "ok"}

    def generate_phase_d6_registry_report(
        self, *, output_path: str | None = None
    ) -> dict[str, object]:
        return {
            "scope": "phase_d6",
            "status": "completed_research_registry",
            "output_path": output_path or "",
        }


def test_cli_phase_d_status_outputs_report(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", lambda: object())
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakePhaseDService)

    runner = CliRunner()
    result = runner.invoke(cli_module.app, ["phase-d-status"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["phase"] == "D"


def test_cli_phase_d6_registry_outputs_report(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", lambda: object())
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakePhaseDService)

    runner = CliRunner()
    result = runner.invoke(cli_module.app, ["phase-d6-registry"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["scope"] == "phase_d6"


def test_phase_d_backlog_doc_exists() -> None:
    root = Path(__file__).resolve().parents[1]
    doc_path = root / "docs" / "phase_d_extension_backlog.md"

    assert doc_path.exists() is True
    assert "phase-d-status" in doc_path.read_text(encoding="utf-8")
    assert "phase-d6-registry" in doc_path.read_text(encoding="utf-8")


def test_cli_phase_d_research_commands_output_report(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "get_config", lambda: object())
    monkeypatch.setattr(cli_module, "StockAnalyzerService", _FakePhaseDService)

    runner = CliRunner()
    alpha = runner.invoke(cli_module.app, ["phase-d-alphalens"])
    finbert = runner.invoke(
        cli_module.app,
        [
            "phase-d-finbert",
            "--records",
            '[{"symbol":"600000.SH","headline":"Positive outlook"}]',
        ],
    )
    tabular = runner.invoke(cli_module.app, ["phase-d-tabular-deep"])
    finrl = runner.invoke(cli_module.app, ["phase-d-finrl"])

    assert alpha.exit_code == 0
    assert json.loads(alpha.stdout)["research_id"] == "alphalens_sidecar"
    assert finbert.exit_code == 0
    assert json.loads(finbert.stdout)["research_id"] == "finbert_sidecar"
    assert tabular.exit_code == 0
    assert json.loads(tabular.stdout)["research_id"] == "tabnet_ft_transformer"
    assert finrl.exit_code == 0
    assert json.loads(finrl.stdout)["research_id"] == "finrl_sidecar"
