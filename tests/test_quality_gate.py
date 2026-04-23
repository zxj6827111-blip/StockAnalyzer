from __future__ import annotations

import subprocess
from pathlib import Path

from stock_analyzer.ops.quality_gate import build_stage_specs, run_quality_gate


def test_quality_gate_builds_clean_scope_and_slow_specs() -> None:
    clean_specs = build_stage_specs("clean-scope")
    slow_specs = build_stage_specs("slow-report")

    assert clean_specs
    assert any(spec.name == "ruff_clean_scope" for spec in clean_specs)
    assert any(
        spec.name == "mypy_acceptance_service" and spec.blocking is False
        for spec in clean_specs
    )
    assert any(
        spec.name == "mypy_market_sync_service" and spec.blocking is True
        for spec in clean_specs
    )
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
        stdout="ok\n".encode("utf-8"),
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
