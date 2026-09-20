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

# ``source`` 取该值表示"没读到任何清单文件、只能退回环境变量"——调用方若要求
# "产物存在性即证据"，必须把它当成缺失，而不是当成一份清单。
MANIFEST_SOURCE_ENVIRONMENT = "environment"


def get_build_manifest(root: str | Path | None = None) -> dict[str, object]:
    """读构建清单；``root`` 给出时优先在它下面找 ``build_manifest.json``。

    ``root`` 是给"运行根不一定是 CWD"的调用方用的（M3 冻结/快照 CLI 显式传仓库根，
    容器里就是 ``/app``）。不传时行为与旧版一致：环境变量 → ``/app`` → CWD。
    """
    for path in _manifest_candidates(root):
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


__all__ = [
    "CONFIG_SCHEMA_VERSION",
    "MANIFEST_SOURCE_ENVIRONMENT",
    "RUNTIME_STATE_SCHEMA_VERSION",
    "generated_at_utc",
    "get_build_manifest",
]


def _manifest_candidates(root: str | Path | None = None) -> list[Path]:
    configured = os.getenv("STOCK_ANALYZER_BUILD_MANIFEST", "").strip()
    candidates = [Path(configured)] if configured else []
    if root is not None:
        candidates.append(Path(root) / "build_manifest.json")
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
