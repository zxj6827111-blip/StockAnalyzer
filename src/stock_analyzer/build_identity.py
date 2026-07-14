"""Build manifest shared by the API, scheduler, and deployment gates."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CONFIG_SCHEMA_VERSION = "stock-analyzer-config.v1"
RUNTIME_STATE_SCHEMA_VERSION = 9


def get_build_manifest() -> dict[str, object]:
    for path in _manifest_candidates():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return _normalize_manifest(payload, source=str(path))
    return _normalize_manifest(
        {
            "commit": os.getenv("STOCK_ANALYZER_BUILD_COMMIT", "unknown"),
            "short_commit": os.getenv("STOCK_ANALYZER_BUILD_SHORT_COMMIT", ""),
            "dirty": os.getenv("STOCK_ANALYZER_BUILD_DIRTY", "unknown"),
            "built_at_utc": os.getenv("STOCK_ANALYZER_BUILD_TIME_UTC", ""),
            "config_schema": CONFIG_SCHEMA_VERSION,
            "runtime_state_schema": RUNTIME_STATE_SCHEMA_VERSION,
        },
        source="environment",
    )


def _manifest_candidates() -> list[Path]:
    configured = os.getenv("STOCK_ANALYZER_BUILD_MANIFEST", "").strip()
    candidates = [Path(configured)] if configured else []
    candidates.extend([Path("/app/build_manifest.json"), Path("build_manifest.json")])
    return candidates


def _normalize_manifest(raw: dict[str, Any], *, source: str) -> dict[str, object]:
    commit = str(raw.get("commit", "")).strip() or "unknown"
    short_commit = str(raw.get("short_commit", "")).strip()
    if not short_commit and commit != "unknown":
        short_commit = commit[:12]
    dirty_raw = raw.get("dirty", "unknown")
    dirty: bool | str
    if isinstance(dirty_raw, bool):
        dirty = dirty_raw
    elif str(dirty_raw).strip().lower() in {"1", "true", "yes"}:
        dirty = True
    elif str(dirty_raw).strip().lower() in {"0", "false", "no"}:
        dirty = False
    else:
        dirty = "unknown"
    payload: dict[str, object] = {
        "commit": commit,
        "short_commit": short_commit or "unknown",
        "dirty": dirty,
        "built_at_utc": str(raw.get("built_at_utc", "")).strip() or "unknown",
        "config_schema": str(raw.get("config_schema", "")).strip() or CONFIG_SCHEMA_VERSION,
        "runtime_state_schema": int(
            raw.get("runtime_state_schema", RUNTIME_STATE_SCHEMA_VERSION)
        ),
        "source": source,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["manifest_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    payload["trusted"] = bool(commit != "unknown" and dirty != "unknown")
    return payload


def generated_at_utc() -> str:
    return datetime.now(UTC).isoformat()
