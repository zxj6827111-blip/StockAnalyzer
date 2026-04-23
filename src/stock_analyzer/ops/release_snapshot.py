"""Release snapshot and rollback helpers."""

# mypy: disable-error-code=misc

from __future__ import annotations

import shutil
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from stock_analyzer.config import StockAnalyzerConfig


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SnapshotEntry(_StrictModel):
    """One captured path inside a release snapshot."""

    source_path: str
    restore_path: str
    snapshot_path: str
    item_type: str
    exists: bool


class ReleaseSnapshotManifest(_StrictModel):
    """Manifest describing a release snapshot."""

    snapshot_id: str
    created_at: datetime
    project_root: str
    items: list[SnapshotEntry] = Field(default_factory=list)


class ReleaseSnapshotReport(_StrictModel):
    """Report for snapshot create or restore."""

    ok: bool
    action: str
    snapshot_dir: str
    manifest_path: str
    copied: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    backup_dir: str = ""


def create_release_snapshot(
    *,
    config: StockAnalyzerConfig,
    project_root: str | Path | None = None,
    snapshot_root: str | Path | None = None,
    config_path: str | Path | None = None,
    items: Iterable[str] | None = None,
) -> ReleaseSnapshotReport:
    """Create a release snapshot for rollback recovery."""
    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parents[3]
    snapshot_base = (
        Path(snapshot_root)
        if snapshot_root is not None
        else root / "artifacts" / "release" / "snapshots"
    )
    snapshot_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_dir = snapshot_base / snapshot_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    selected_items = list(items) if items is not None else default_snapshot_items(
        config=config,
        config_path=config_path,
    )
    entries: list[SnapshotEntry] = []
    copied: list[str] = []
    missing: list[str] = []
    for raw_item in selected_items:
        source = _resolve_item_path(root=root, raw_path=raw_item)
        snapshot_rel = _snapshot_relative_path(root=root, source=source)
        entry = SnapshotEntry(
            source_path=str(source),
            restore_path=str(source),
            snapshot_path=str(snapshot_rel),
            item_type="dir" if source.is_dir() else "file",
            exists=source.exists(),
        )
        entries.append(entry)
        if not source.exists():
            missing.append(str(source))
            continue
        destination = snapshot_dir / snapshot_rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
        copied.append(str(source))

    manifest = ReleaseSnapshotManifest(
        snapshot_id=snapshot_id,
        created_at=datetime.now(),
        project_root=str(root),
        items=entries,
    )
    manifest_path = snapshot_dir / "manifest.json"
    manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return ReleaseSnapshotReport(
        ok=True,
        action="create",
        snapshot_dir=str(snapshot_dir),
        manifest_path=str(manifest_path),
        copied=copied,
        missing=missing,
    )


def restore_release_snapshot(
    *,
    snapshot_dir: str | Path,
    dry_run: bool = False,
    backup_existing: bool = True,
) -> ReleaseSnapshotReport:
    """Restore a previously created release snapshot."""
    snapshot_root = Path(snapshot_dir)
    manifest_path = snapshot_root / "manifest.json"
    manifest = ReleaseSnapshotManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    copied: list[str] = []
    missing: list[str] = []
    backup_dir = ""
    backup_root = snapshot_root / f"restore_backup_{datetime.now():%Y%m%d_%H%M%S}"
    if backup_existing and not dry_run:
        backup_root.mkdir(parents=True, exist_ok=True)
        backup_dir = str(backup_root)

    for item in manifest.items:
        if not item.exists:
            missing.append(item.restore_path)
            continue
        source = snapshot_root / item.snapshot_path
        restore_target = Path(item.restore_path)
        if not source.exists():
            missing.append(str(source))
            continue
        if dry_run:
            copied.append(item.restore_path)
            continue
        if backup_existing and restore_target.exists():
            backup_target = backup_root / item.snapshot_path
            backup_target.parent.mkdir(parents=True, exist_ok=True)
            if restore_target.is_dir():
                shutil.copytree(restore_target, backup_target)
            else:
                shutil.copy2(restore_target, backup_target)
        if restore_target.exists():
            if restore_target.is_dir():
                shutil.rmtree(restore_target)
            else:
                restore_target.unlink()
        restore_target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, restore_target)
        else:
            shutil.copy2(source, restore_target)
        copied.append(item.restore_path)

    return ReleaseSnapshotReport(
        ok=not missing,
        action="restore_dry_run" if dry_run else "restore",
        snapshot_dir=str(snapshot_root),
        manifest_path=str(manifest_path),
        copied=copied,
        missing=missing,
        backup_dir=backup_dir,
    )


def default_snapshot_items(
    *,
    config: StockAnalyzerConfig,
    config_path: str | Path | None = None,
) -> list[str]:
    """Return the default release snapshot path list."""
    items = [
        ".env",
        config.command_channel.state_persist_path,
        config.command_channel.history_archive_dir,
        config.acceptance.export_dir,
        config.evolution.manifest_path,
        config.evolution.report_dir,
        config.sim_broker_weekly.export_dir,
    ]
    if config_path is not None:
        items.append(str(config_path))
    else:
        items.append("config/default.yaml")
    return items


def _resolve_item_path(*, root: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate
    return root / candidate


def _snapshot_relative_path(*, root: Path, source: Path) -> Path:
    try:
        return source.resolve().relative_to(root.resolve())
    except ValueError:
        anchor = source.anchor.replace(":", "").replace("\\", "_").replace("/", "_").strip("_")
        anchor_name = anchor or "external"
        parts = [part.replace(":", "_") for part in source.parts if part not in {source.anchor}]
        return Path("external") / anchor_name / Path(*parts)
