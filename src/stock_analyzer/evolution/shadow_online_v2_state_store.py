"""Independent persistence for shadow-online v2 learner state."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path


class ShadowOnlineV2StateStore:
    """Atomically persist and load shadow v2 learner state."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load_state(self) -> dict[str, object]:
        payload = self.load_payload()
        state = payload.get("state")
        return dict(state) if isinstance(state, Mapping) else {}

    def load_payload(self) -> dict[str, object]:
        if not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return dict(payload) if isinstance(payload, Mapping) else {}

    def save_state(
        self,
        *,
        state: Mapping[str, object],
        now: datetime | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        timestamp = _as_utc_datetime(now or datetime.now(UTC))
        payload = {
            "schema_version": "1",
            "engine": str(state.get("engine", "protocol_shadow_online_v2_lr")).strip()
            or "protocol_shadow_online_v2_lr",
            "updated_at": timestamp.isoformat(),
            "state": dict(state),
            "metadata": dict(metadata or {}),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(
            self._path,
            json.dumps(payload, ensure_ascii=True, indent=2),
        )
        return payload

    def status(self) -> dict[str, object]:
        payload = self.load_payload()
        state = payload.get("state")
        state_mapping = dict(state) if isinstance(state, Mapping) else {}
        feature_names_raw = state_mapping.get("feature_names")
        feature_count = (
            len(feature_names_raw)
            if isinstance(feature_names_raw, list)
            else len(state_mapping.get("weights", {}))
            if isinstance(state_mapping.get("weights"), Mapping)
            else 0
        )
        return {
            "exists": self._path.exists(),
            "path": str(self._path),
            "schema_version": str(payload.get("schema_version", "")),
            "engine": str(payload.get("engine", "")),
            "updated_at": str(payload.get("updated_at", "")),
            "cumulative_updates": _as_int(state_mapping.get("cumulative_updates"), default=0),
            "feature_count": int(feature_count),
        }


def _atomic_write_text(path: Path, content: str) -> None:
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def _as_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return int(float(text))
        except ValueError:
            return default
    return default
