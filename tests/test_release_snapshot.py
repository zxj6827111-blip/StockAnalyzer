from __future__ import annotations

from pathlib import Path

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.ops.release_snapshot import (
    create_release_snapshot,
    restore_release_snapshot,
)


def _load_base_config() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    return load_config(root / "config" / "default.yaml")


def test_release_snapshot_create_and_restore(tmp_path: Path) -> None:
    config = _load_base_config()
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)
    env_file = project_root / ".env"
    state_file = project_root / "artifacts" / "runtime" / "runtime_state.json"
    env_file.write_text("version=old\n", encoding="utf-8")
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text('{"mode":"old"}\n', encoding="utf-8")

    snapshot = create_release_snapshot(
        config=config,
        project_root=project_root,
        items=[".env", "artifacts/runtime/runtime_state.json"],
    )

    env_file.write_text("version=new\n", encoding="utf-8")
    state_file.write_text('{"mode":"new"}\n', encoding="utf-8")

    restore = restore_release_snapshot(snapshot_dir=snapshot.snapshot_dir)

    assert snapshot.ok is True
    assert restore.ok is True
    assert env_file.read_text(encoding="utf-8") == "version=old\n"
    assert state_file.read_text(encoding="utf-8") == '{"mode":"old"}\n'
    assert restore.backup_dir


def test_release_snapshot_restore_dry_run(tmp_path: Path) -> None:
    config = _load_base_config()
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)
    file_path = project_root / ".env"
    file_path.write_text("v1\n", encoding="utf-8")

    snapshot = create_release_snapshot(
        config=config,
        project_root=project_root,
        items=[".env"],
    )

    file_path.write_text("v2\n", encoding="utf-8")
    restore = restore_release_snapshot(snapshot_dir=snapshot.snapshot_dir, dry_run=True)

    assert restore.ok is True
    assert file_path.read_text(encoding="utf-8") == "v2\n"
