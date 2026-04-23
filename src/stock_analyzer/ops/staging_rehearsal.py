"""Staging rehearsal orchestration."""

# mypy: disable-error-code=misc

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from stock_analyzer.config import StockAnalyzerConfig
from stock_analyzer.evolution.ops.preflight import EvolutionPreflightReport, run_evolution_preflight
from stock_analyzer.ops.release_preflight import ReleasePreflightReport, run_release_preflight
from stock_analyzer.ops.release_smoke import SmokeApiReport, run_smoke_api


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RehearsalStep(_StrictModel):
    """One staging rehearsal step."""

    name: str
    ok: bool
    detail: str


class StagingRehearsalReport(_StrictModel):
    """Combined report for staging release rehearsal."""

    ready: bool
    generated_at: datetime
    project_root: str
    config_path: str
    steps: list[RehearsalStep] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    manual_checks: list[str] = Field(default_factory=list)
    release_preflight: ReleasePreflightReport
    evolution_preflight: EvolutionPreflightReport
    smoke_api: SmokeApiReport


def run_staging_rehearsal(
    config: StockAnalyzerConfig,
    *,
    project_root: str | Path | None = None,
    config_path: str | Path | None = None,
    smoke_port: int = 8012,
    release_preflight_runner: Callable[..., ReleasePreflightReport] = run_release_preflight,
    evolution_preflight_runner: Callable[..., EvolutionPreflightReport] = run_evolution_preflight,
    smoke_runner: Callable[..., SmokeApiReport] = run_smoke_api,
) -> StagingRehearsalReport:
    """Run the automated staging rehearsal suite."""
    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parents[3]
    if config_path is not None:
        resolved_config_path = Path(config_path)
    else:
        resolved_config_path = root / "config" / "default.yaml"

    release_preflight = release_preflight_runner(
        config,
        project_root=root,
        config_path=resolved_config_path,
        bind_port=smoke_port,
    )
    evolution_preflight = evolution_preflight_runner(config=config.evolution, project_root=root)
    smoke_api = smoke_runner(
        project_root=root,
        port=smoke_port,
        start_server=True,
        include_ui=True,
        include_write_checks=True,
    )
    steps = [
        RehearsalStep(
            name="release_preflight",
            ok=release_preflight.ready,
            detail="environment release preflight",
        ),
        RehearsalStep(
            name="evolution_preflight",
            ok=evolution_preflight.ready,
            detail="evolution dry-run preflight",
        ),
        RehearsalStep(
            name="smoke_api",
            ok=smoke_api.ok,
            detail="local uvicorn + key API smoke",
        ),
    ]
    blockers: list[str] = []
    if not release_preflight.ready:
        blockers.extend(release_preflight.blockers)
    if not evolution_preflight.ready:
        blockers.extend(f"evolution:{item}" for item in evolution_preflight.blockers)
    if not smoke_api.ok:
        blockers.extend(f"smoke:{item}" for item in smoke_api.failures)

    manual_checks = [
        (
            "Open /ui/, /ui/portfolio, /ui/news, and /ui/ops in staging and "
            "confirm data refresh works."
        ),
        "Run release snapshot create before deployment and keep the snapshot directory path.",
        (
            "Run release snapshot restore with --dry-run once to confirm rollback "
            "manifest is restorable."
        ),
    ]
    return StagingRehearsalReport(
        ready=not blockers,
        generated_at=datetime.now(),
        project_root=str(root),
        config_path=str(resolved_config_path),
        steps=steps,
        blockers=blockers,
        manual_checks=manual_checks,
        release_preflight=release_preflight,
        evolution_preflight=evolution_preflight,
        smoke_api=smoke_api,
    )
