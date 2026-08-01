from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from stock_analyzer.ops.quality_gate import build_stage_specs

REPO_ROOT = Path(__file__).resolve().parents[1]


def _script() -> str:
    return (REPO_ROOT / "scripts" / "nas_deploy_update.sh").read_text(
        encoding="utf-8",
    )


def test_nas_deploy_update_uses_bounded_health_readiness_polling() -> None:
    script = _script()

    assert 'HEALTH_ATTEMPTS="${HEALTH_ATTEMPTS:-30}"' in script
    assert 'HEALTH_SLEEP_SEC="${HEALTH_SLEEP_SEC:-2}"' in script
    assert 'while [[ "${attempt}" -le "${HEALTH_ATTEMPTS}" ]]' in script
    assert "--connect-timeout 3 --max-time 10" in script
    assert 'sleep "${HEALTH_SLEEP_SEC}"' in script
    assert "health not ready" in script
    assert "sleep 3" not in script


@pytest.mark.skipif(os.name == "nt", reason="Bash syntax is verified on Linux CI")
def test_nas_deploy_update_has_valid_bash_syntax() -> None:
    completed = subprocess.run(
        ["bash", "-n", str(REPO_ROOT / "scripts" / "nas_deploy_update.sh")],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_nas_deploy_update_health_gate_remains_fail_closed() -> None:
    script = _script()

    assert 'h.get("status") != "ok"' in script
    assert 'build.get("commit") != expected' in script
    assert 'build.get("short_commit") != expected[:7]' in script
    assert 'build.get("trusted") is not True' in script
    assert 'runtime.get("advisory_only") is not True' in script
    assert 'runtime.get("training_enabled") is not False' in script
    assert "identity or safety validation failed" in script


def test_nas_deploy_update_keeps_scheduler_start_opt_in() -> None:
    script = _script()

    assert "START_SCHEDULER=0" in script
    assert "--start-scheduler" in script
    assert "scheduler: not started" in script


def test_nas_deploy_update_checks_run_in_quality_gate() -> None:
    clean_scope = next(
        spec for spec in build_stage_specs("clean-scope") if spec.name == "ruff_clean_scope"
    )
    smoke = next(spec for spec in build_stage_specs("smoke") if spec.name == "pytest_smoke")

    assert "tests/test_nas_deploy_update_script.py" in clean_scope.command
    assert "tests/test_nas_deploy_update_script.py" in smoke.command
