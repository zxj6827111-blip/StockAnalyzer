from __future__ import annotations

import importlib.util
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from stock_analyzer.evolution.ops.recovery import (
    ManifestCheckpoint,
    RunManifest,
    assert_recovery_time_window,
    check_environment_dependencies,
    load_manifest,
    save_manifest,
)


def test_assert_recovery_time_window_blocks_hard_stop() -> None:
    with pytest.raises(RuntimeError):
        assert_recovery_time_window(datetime(2026, 3, 2, 9, 0))


def test_manifest_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "run_manifest.json"
    manifest = RunManifest(
        run_id="run-1",
        checkpoints=[
            ManifestCheckpoint(
                step="M9",
                status="completed",
                timestamp=datetime(2026, 3, 1, tzinfo=UTC),
            )
        ],
    )
    save_manifest(path=path, manifest=manifest)
    loaded = load_manifest(path=path)
    assert loaded.run_id == "run-1"
    assert loaded.checkpoints[0].step == "M9"


def test_dependency_check_uses_cli_and_module_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_which(name: str) -> str | None:
        return "/usr/local/bin/cpulimit" if name == "cpulimit" else None

    def _fake_find_spec(name: str) -> Any:
        if name in {"duckdb", "faiss_cpu"}:
            return object()
        return None

    monkeypatch.setattr(shutil, "which", _fake_which)
    monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec)

    result = check_environment_dependencies()
    assert result.all_available is True
    status_by_name = {status.name: status for status in result.statuses}
    assert status_by_name["cpulimit"].available is True
    assert status_by_name["duckdb"].available is True
    assert status_by_name["faiss"].available is True


def test_dependency_check_allows_windows_cpulimit_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_which(name: str) -> str | None:
        if name == "powershell":
            return r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        return None

    def _fake_find_spec(name: str) -> Any:
        if name in {"duckdb", "faiss_cpu"}:
            return object()
        return None

    monkeypatch.setattr(shutil, "which", _fake_which)
    monkeypatch.setattr(importlib.util, "find_spec", _fake_find_spec)
    monkeypatch.setattr("stock_analyzer.evolution.ops.recovery.sys.platform", "win32")

    result = check_environment_dependencies()
    assert result.all_available is True
    status_by_name = {status.name: status for status in result.statuses}
    assert status_by_name["cpulimit"].available is True
    assert "windows_fallback:" in status_by_name["cpulimit"].detail
