from __future__ import annotations

from datetime import datetime
from pathlib import Path

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.evolution.ops.preflight import DependencyCheckResult, EvolutionPreflightReport
from stock_analyzer.ops.release_preflight import ReleasePreflightReport
from stock_analyzer.ops.release_smoke import SmokeApiReport
from stock_analyzer.ops.staging_rehearsal import run_staging_rehearsal


def _load_base_config() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    return load_config(root / "config" / "default.yaml")


def _release_report(ready: bool) -> ReleasePreflightReport:
    return ReleasePreflightReport(
        ready=ready,
        checked_at=datetime.now(),
        project_root="tmp",
        config_path="tmp/config.yaml",
        app_mode="simulation",
        bind_host="127.0.0.1",
        bind_port=8012,
        frontend_dist="tmp/frontend/dist",
        blockers=[] if ready else ["network:bind_port"],
        warnings=[],
        checks=[],
    )


def _evolution_report(ready: bool) -> EvolutionPreflightReport:
    return EvolutionPreflightReport(
        ready=ready,
        checked_at=datetime.now(),
        project_root="tmp",
        strict_dependency_check=False,
        dependency=DependencyCheckResult(all_available=True, statuses=[]),
        path_checks=[],
        config_checks=[],
        blockers=[] if ready else ["config:code_commit_id"],
    )


def _smoke_report(ok: bool) -> SmokeApiReport:
    return SmokeApiReport(
        ok=ok,
        started_at=datetime.now(),
        finished_at=datetime.now(),
        base_url="http://127.0.0.1:8012",
        started_local_server=True,
        process_id=123,
        stdout_path="",
        stderr_path="",
        checks=[],
        failures=[] if ok else ["week7_run: timeout"],
    )


def test_staging_rehearsal_ready_when_all_steps_pass(tmp_path: Path) -> None:
    config = _load_base_config()
    report = run_staging_rehearsal(
        config,
        project_root=tmp_path,
        release_preflight_runner=lambda *args, **kwargs: _release_report(True),
        evolution_preflight_runner=lambda *args, **kwargs: _evolution_report(True),
        smoke_runner=lambda *args, **kwargs: _smoke_report(True),
    )

    assert report.ready is True
    assert len(report.steps) == 3
    assert report.manual_checks


def test_staging_rehearsal_collects_blockers(tmp_path: Path) -> None:
    config = _load_base_config()
    report = run_staging_rehearsal(
        config,
        project_root=tmp_path,
        release_preflight_runner=lambda *args, **kwargs: _release_report(False),
        evolution_preflight_runner=lambda *args, **kwargs: _evolution_report(True),
        smoke_runner=lambda *args, **kwargs: _smoke_report(False),
    )

    assert report.ready is False
    assert "network:bind_port" in report.blockers
    assert any(item.startswith("smoke:") for item in report.blockers)

