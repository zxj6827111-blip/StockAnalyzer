from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from stock_analyzer.ops.quality_gate import build_stage_specs, run_quality_gate


def test_quality_gate_builds_clean_scope_and_slow_specs() -> None:
    clean_specs = build_stage_specs("clean-scope")
    slow_specs = build_stage_specs("slow-report")

    assert clean_specs
    assert any(spec.name == "ruff_clean_scope" for spec in clean_specs)
    assert all(spec.command[0] == sys.executable for spec in clean_specs + slow_specs)
    assert any(
        spec.name == "mypy_acceptance_service" and spec.blocking is False for spec in clean_specs
    )
    assert any(
        spec.name == "mypy_market_sync_service" and spec.blocking is True for spec in clean_specs
    )
    assert any(
        spec.name == "mypy_main" and spec.blocking is True for spec in clean_specs
    )
    assert any(
        spec.name == "mypy_api" and spec.blocking is True for spec in clean_specs
    )
    assert any(spec.name == "ruff_clean_scope" for spec in clean_specs)
    ruff_targets = next(
        spec.command for spec in clean_specs if spec.name == "ruff_clean_scope"
    )
    assert "src/stock_analyzer/api" in ruff_targets
    assert len(slow_specs) == 1
    assert slow_specs[0].name == "pytest_slow_report"


def test_quality_gate_all_stage_contains_clean_smoke_and_integration() -> None:
    specs = build_stage_specs("all")
    names = [spec.name for spec in specs]

    assert "ruff_clean_scope" in names
    assert "pytest_smoke" in names
    assert "pytest_integration" in names


def test_run_quality_gate_decodes_windows_mixed_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    completed = subprocess.CompletedProcess(
        args=["python", "-m", "ruff", "check"],
        returncode=0,
        stdout=b"ok\n",
        stderr=b"\xaa quality warning",
    )

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: completed)

    report = run_quality_gate("slow-report", project_root=tmp_path)

    assert report.ok is True
    log_path = tmp_path / "artifacts" / "quality" / "pytest_slow_report.log"
    assert log_path.exists() is True
    content = log_path.read_text(encoding="utf-8")
    assert "ok" in content
    assert "quality warning" in content


def test_run_quality_gate_includes_failure_output_tail(
    tmp_path: Path,
    monkeypatch,
) -> None:
    completed = subprocess.CompletedProcess(
        args=[sys.executable, "-m", "pytest"],
        returncode=1,
        stdout=b"first line\n",
        stderr=b"last failure\n",
    )

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: completed)

    report = run_quality_gate("slow-report", project_root=tmp_path)

    assert report.ok is False
    assert report.commands[0].output_tail.splitlines()[-1] == "last failure"
    assert "last failure" in report.to_json()
