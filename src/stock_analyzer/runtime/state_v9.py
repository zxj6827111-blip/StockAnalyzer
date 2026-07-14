"""Atomic runtime-state v8 to v9 migration and rollback helpers."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

RUNTIME_STATE_VERSION = 9
RUNTIME_STATE_HISTORY_FIELDS = (
    "reconcile_history",
    "run_summaries",
    "latency_history_ms",
    "audit_events",
    "week4_acceptance_history",
    "week5_scan_history",
    "week6_history",
    "market_warehouse_history",
    "evolution_history",
    "evolution_release_gate_history",
    "evolution_release_approval_history",
    "evolution_release_ticket_history",
    "learning_model_proposal_history",
    "learning_model_approval_history",
    "learning_model_release_ticket_history",
    "execution_risk_training_history",
    "execution_aware_report_history",
)


def migrate_runtime_state_v9(
    state_path: str | Path,
    *,
    dry_run: bool = False,
) -> dict[str, object]:
    path = Path(state_path)
    if not path.exists():
        return {"status": "missing", "path": str(path), "changed": False}
    original = path.read_bytes()
    original_sha = hashlib.sha256(original).hexdigest()
    payload = json.loads(original.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("runtime state payload must be a JSON object")
    from_version = _as_int(payload.get("state_version"), default=0)
    history_counts = {
        field: len(value)
        for field in RUNTIME_STATE_HISTORY_FIELDS
        if isinstance((value := payload.get(field)), list)
    }
    report: dict[str, object] = {
        "status": "already_v9" if from_version >= RUNTIME_STATE_VERSION else "planned",
        "path": str(path),
        "changed": from_version < RUNTIME_STATE_VERSION,
        "dry_run": dry_run,
        "from_version": from_version,
        "to_version": RUNTIME_STATE_VERSION,
        "original_sha256": original_sha,
        "original_size_bytes": len(original),
        "history_counts": history_counts,
    }
    if from_version >= RUNTIME_STATE_VERSION or dry_run:
        return report

    backup_dir = path.with_name("runtime_state_backups")
    backup_path = backup_dir / f"runtime_state.v{from_version}.{original_sha[:16]}.json"
    backup_dir.mkdir(parents=True, exist_ok=True)
    if not backup_path.exists():
        _atomic_write_bytes(backup_path, original)
    if hashlib.sha256(backup_path.read_bytes()).hexdigest() != original_sha:
        raise RuntimeError("runtime state backup checksum mismatch")

    sidecar_dir = path.with_name("runtime_state_history")
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    records: dict[str, dict[str, object]] = {}
    for field in RUNTIME_STATE_HISTORY_FIELDS:
        rows = payload.pop(field, None)
        sidecar_path = sidecar_dir / f"{field}.jsonl"
        merged = _merge_jsonl_rows(sidecar_path, rows if isinstance(rows, list) else [])
        _atomic_write_jsonl(sidecar_path, merged)
        sidecar_bytes = sidecar_path.read_bytes()
        records[field] = {
            "path": str(sidecar_path),
            "records": len(merged),
            "sha256": hashlib.sha256(sidecar_bytes).hexdigest(),
            "size_bytes": len(sidecar_bytes),
        }

    migrated_at = datetime.now(UTC).isoformat()
    payload["state_version"] = RUNTIME_STATE_VERSION
    payload["runtime_history_sidecars"] = {
        "format": "jsonl",
        "base_dir": str(sidecar_dir),
        "records": records,
    }
    payload["state_migration"] = {
        "from_version": from_version,
        "to_version": RUNTIME_STATE_VERSION,
        "migrated_at": migrated_at,
        "backup_path": str(backup_path),
        "backup_sha256": original_sha,
        "history_counts": history_counts,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    _atomic_write_bytes(path, encoded)
    report.update(
        {
            "status": "migrated",
            "backup_path": str(backup_path),
            "migrated_sha256": hashlib.sha256(encoded).hexdigest(),
            "migrated_size_bytes": len(encoded),
            "sidecars": records,
        }
    )
    return report


def rollback_runtime_state_v9(
    state_path: str | Path,
    *,
    backup_path: str | Path | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    path = Path(state_path)
    selected = Path(backup_path) if backup_path is not None else _latest_backup(path)
    if selected is None or not selected.exists():
        return {"status": "backup_missing", "path": str(path), "changed": False}
    backup = selected.read_bytes()
    json.loads(backup.decode("utf-8"))
    report: dict[str, object] = {
        "status": "planned" if dry_run else "rolled_back",
        "path": str(path),
        "backup_path": str(selected),
        "backup_sha256": hashlib.sha256(backup).hexdigest(),
        "changed": not dry_run,
        "sidecars_preserved": True,
    }
    if dry_run:
        return report
    if path.exists():
        current = path.read_bytes()
        current_sha = hashlib.sha256(current).hexdigest()
        rollback_archive = path.with_name("runtime_state_backups") / (
            f"runtime_state.pre_rollback.{current_sha[:16]}.json"
        )
        if not rollback_archive.exists():
            _atomic_write_bytes(rollback_archive, current)
        report["pre_rollback_archive"] = str(rollback_archive)
    _atomic_write_bytes(path, backup)
    return report


def _merge_jsonl_rows(path: Path, new_rows: list[object]) -> list[dict[str, object]]:
    merged: list[dict[str, object]] = []
    existing_counts: dict[str, int] = {}
    if path.exists():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                normalized = {str(key): value for key, value in row.items()}
                merged.append(normalized)
                key = _canonical_key(normalized)
                existing_counts[key] = existing_counts.get(key, 0) + 1
    seen_new: dict[str, int] = {}
    for row in new_rows:
        if isinstance(row, dict):
            normalized = {str(key): value for key, value in row.items()}
            key = _canonical_key(normalized)
            seen_new[key] = seen_new.get(key, 0) + 1
            if seen_new[key] > existing_counts.get(key, 0):
                merged.append(normalized)
    return merged


def _canonical_key(row: dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _atomic_write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    content = "".join(
        f"{json.dumps(row, ensure_ascii=False, separators=(',', ':'), default=str)}\n"
        for row in rows
    ).encode("utf-8")
    _atomic_write_bytes(path, content)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temp_path.open("wb") as fp:
        fp.write(content)
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(temp_path, path)


def _latest_backup(path: Path) -> Path | None:
    backup_dir = path.with_name("runtime_state_backups")
    candidates = sorted(
        backup_dir.glob("runtime_state.v*.json"),
        key=lambda item: item.stat().st_mtime_ns,
        reverse=True,
    ) if backup_dir.exists() else []
    return candidates[0] if candidates else None


def _as_int(value: object, *, default: int) -> int:
    if not isinstance(value, (str, int, float, bool)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
