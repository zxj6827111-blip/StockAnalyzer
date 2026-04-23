"""Append-only metrics persistence for shadow-online v2 runs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path


class ShadowOnlineV2MetricsStore:
    """Persist shadow v2 run metrics as append-only JSONL records."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def append_run(
        self,
        *,
        result: Mapping[str, object],
        now: datetime | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        metrics = result.get("metrics")
        normalized_metrics = dict(metrics) if isinstance(metrics, Mapping) else {}
        timestamp = _as_utc_datetime(now or datetime.now(UTC))
        record = {
            "schema_version": "1",
            "engine": str(result.get("engine", "protocol_shadow_online_v2_lr")).strip()
            or "protocol_shadow_online_v2_lr",
            "recorded_at": timestamp.isoformat(),
            "status": str(result.get("status", "")).strip(),
            "samples_considered": _as_int(result.get("samples_considered"), default=0),
            "samples_used": _as_int(result.get("samples_used"), default=0),
            "metrics": normalized_metrics,
            "reasons": [
                str(item).strip()
                for item in result.get("reasons", [])
                if str(item).strip()
            ]
            if isinstance(result.get("reasons"), list)
            else [],
            "metadata": dict(metadata or {}),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, ensure_ascii=True) + "\n")
        return record

    def list_recent(self, *, limit: int = 20) -> list[dict[str, object]]:
        records = self._load_all()
        capped = max(1, int(limit))
        return list(reversed(records[-capped:]))

    def status(self) -> dict[str, object]:
        records = self._load_all()
        latest = records[-1] if records else {}
        metrics = latest.get("metrics")
        metrics_mapping = dict(metrics) if isinstance(metrics, Mapping) else {}
        return {
            "exists": self._path.exists(),
            "path": str(self._path),
            "records": len(records),
            "last_recorded_at": str(latest.get("recorded_at", "")),
            "last_status": str(latest.get("status", "")),
            "last_valid_samples": _as_int(metrics_mapping.get("valid_samples"), default=0),
            "last_updates_applied": _as_int(metrics_mapping.get("updates_applied"), default=0),
        }

    def _load_all(self) -> list[dict[str, object]]:
        if not self._path.exists():
            return []
        try:
            raw_lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        records: list[dict[str, object]] = []
        for raw_line in raw_lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, Mapping):
                records.append(dict(payload))
        return records


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
