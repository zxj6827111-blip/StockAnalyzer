"""Independent loader for M5 label-optimization input records."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path


def load_m5_label_records(path: str | Path) -> list[dict[str, object]]:
    """Load M5 label records from ``json`` or ``jsonl`` artifacts.

    The loader is intentionally permissive and returns an empty list for missing
    files, parse errors, or unsupported structures so the caller can fall back
    to in-memory records.

    Args:
        path: Artifact file path.

    Returns:
        A normalized list of record dictionaries.
    """
    artifact_path = Path(path)
    if not artifact_path.exists():
        return []

    try:
        records = _load_raw_records(path=artifact_path)
    except (OSError, json.JSONDecodeError, ValueError):
        return []

    return [_normalize_record(record) for record in records]


def _load_raw_records(path: Path) -> list[Mapping[str, object]]:
    if path.suffix.lower() == ".jsonl":
        rows: list[Mapping[str, object]] = []
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if isinstance(payload, Mapping):
                rows.append(payload)
        return rows

    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        for key in ("records", "items", "label_records", "results", "data"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, Mapping)]
        return [payload]
    return []


def _normalize_record(record: Mapping[str, object]) -> dict[str, object]:
    normalized = {str(key): value for key, value in record.items() if isinstance(key, str)}

    # Flatten known nested payload blocks if present.
    for nested_key in ("label_result", "label", "features"):
        nested = record.get(nested_key)
        if isinstance(nested, Mapping):
            for key, value in nested.items():
                if isinstance(key, str) and key not in normalized:
                    normalized[key] = value
    return normalized
