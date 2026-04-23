"""Recovery helpers with time-window firewall and dependency preflight."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from stock_analyzer._pydantic_compat import BaseModel, ConfigDict, Field
from stock_analyzer.evolution.scheduler.time_guard import TimeGuard, TimeGuardMode


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DependencyStatus(_StrictModel):
    """Single dependency preflight result."""

    name: str
    available: bool
    detail: str


class DependencyCheckResult(_StrictModel):
    """Dependency preflight summary."""

    all_available: bool
    statuses: list[DependencyStatus]


class ManifestCheckpoint(_StrictModel):
    """One checkpoint entry in run manifest."""

    step: str
    status: str
    timestamp: datetime


class RunManifest(_StrictModel):
    """Persistent run manifest for recovery resume."""

    run_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    checkpoints: list[ManifestCheckpoint] = Field(default_factory=list)


def assert_recovery_time_window(
    now: datetime | None = None, guard: TimeGuard | None = None
) -> None:
    """Enforce recovery time firewall before any recovery action."""
    active_guard = guard or TimeGuard()
    decision = active_guard.evaluate(moment=now)
    if decision.mode == TimeGuardMode.HARD_STOP:
        raise RuntimeError("recovery blocked during hard_stop window")


def check_environment_dependencies(
    required_cli: tuple[str, ...] = ("cpulimit",),
    required_modules: tuple[str, ...] = ("duckdb", "faiss"),
) -> DependencyCheckResult:
    """Check required CLI tools and Python modules for recovery."""
    statuses: list[DependencyStatus] = []

    for cli_name in required_cli:
        available, detail = _cli_available(cli_name=cli_name)
        statuses.append(
            DependencyStatus(
                name=cli_name,
                available=available,
                detail=detail,
            )
        )

    for module_name in required_modules:
        available = _module_available(module_name=module_name)
        detail = "importable" if available else "module not installed"
        statuses.append(
            DependencyStatus(
                name=module_name,
                available=available,
                detail=detail,
            )
        )

    return DependencyCheckResult(
        all_available=all(status.available for status in statuses),
        statuses=statuses,
    )


def load_manifest(path: str | Path) -> RunManifest:
    """Load run_manifest.json."""
    manifest_path = Path(path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    return RunManifest.model_validate(payload)


def save_manifest(path: str | Path, manifest: RunManifest) -> None:
    """Write run_manifest.json."""
    manifest_path = Path(path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    updated = manifest.model_copy(update={"updated_at": datetime.now(UTC)})
    manifest_path.write_text(updated.model_dump_json(indent=2), encoding="utf-8")


def _module_available(module_name: str) -> bool:
    if module_name == "faiss":
        return (
            importlib.util.find_spec("faiss") is not None
            or importlib.util.find_spec("faiss_cpu") is not None
        )
    return importlib.util.find_spec(module_name) is not None


def _cli_available(cli_name: str) -> tuple[bool, str]:
    path = shutil.which(cli_name)
    if path is not None:
        return True, path
    if cli_name == "cpulimit" and sys.platform.startswith("win"):
        windows_fallback = shutil.which("pwsh") or shutil.which("powershell")
        if windows_fallback is not None:
            return True, f"windows_fallback:{windows_fallback}"
        return False, "cpulimit unsupported on windows and pwsh/powershell not found"
    return False, "not found in PATH"
