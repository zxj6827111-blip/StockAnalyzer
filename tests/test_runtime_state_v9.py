from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from stock_analyzer.runtime.state_v9 import (
    migrate_runtime_state_v9,
    rollback_runtime_state_v9,
)


def _large_v8_payload() -> dict[str, object]:
    payload = "x" * 900
    return {
        "state_version": 8,
        "watchlist": ["600000", "000001"],
        "week6_history": [
            {"timestamp": f"2026-03-{index % 28 + 1:02d}", "data": payload}
            for index in range(4500)
        ],
        "evolution_history": [{"run_id": f"run-{index}", "data": payload} for index in range(4500)],
        "week4_acceptance_history": [{"timestamp": "2026-03-01", "overall": "pass"}],
    }


def test_runtime_state_v9_dry_run_migrate_idempotently_and_roll_back_exact_bytes(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "runtime_state.json"
    original = json.dumps(_large_v8_payload(), ensure_ascii=False, indent=2).encode("utf-8")
    state_path.write_bytes(original)
    assert len(original) > 7_450_000

    dry_run = migrate_runtime_state_v9(state_path, dry_run=True)
    assert dry_run["status"] == "planned"
    assert state_path.read_bytes() == original
    assert not (tmp_path / "runtime_state_backups").exists()

    migrated = migrate_runtime_state_v9(state_path)
    compact = state_path.read_bytes()
    payload = json.loads(compact)
    assert migrated["status"] == "migrated"
    assert migrated["original_sha256"] == hashlib.sha256(original).hexdigest()
    assert len(compact) < 1_000_000
    assert payload["state_version"] == 9
    assert "week6_history" not in payload
    assert payload["runtime_history_sidecars"]["records"]["week6_history"]["records"] == 4500

    before_idempotent = state_path.read_bytes()
    assert migrate_runtime_state_v9(state_path)["status"] == "already_v9"
    assert state_path.read_bytes() == before_idempotent

    sidecar = tmp_path / "runtime_state_history" / "week6_history.jsonl"
    rollback = rollback_runtime_state_v9(
        state_path,
        backup_path=str(migrated["backup_path"]),
    )
    assert rollback["status"] == "rolled_back"
    assert rollback["sidecars_preserved"] is True
    assert state_path.read_bytes() == original
    assert sidecar.exists()


def test_runtime_state_v9_rollback_rejects_corrupted_backup(tmp_path: Path) -> None:
    state_path = tmp_path / "runtime_state.json"
    backup = tmp_path / "corrupt.json"
    state_path.write_text('{"state_version":9}', encoding="utf-8")
    backup.write_text("not-json", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        rollback_runtime_state_v9(state_path, backup_path=backup)
