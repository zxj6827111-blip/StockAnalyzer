"""Independent loader for M11 shadow-portfolio result artifacts."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class M11ShadowObservation:
    """One normalized M11 shadow observation."""

    symbol: str
    champion_shadow_return: float
    challenger_shadow_return: float
    champion_signal: int | None
    challenger_signal: int | None


def load_m11_shadow_records(path: str | Path) -> list[dict[str, object]]:
    """Load raw shadow-result records while flattening known nested payloads."""
    artifact_path = Path(path)
    if not artifact_path.exists():
        return []

    try:
        raw_records = _load_raw_records(artifact_path)
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    return [_normalize_record(record) for record in raw_records]


def load_m11_shadow_observations(path: str | Path) -> list[M11ShadowObservation]:
    """Load normalized M11 shadow observations from one artifact file.

    Supports ``.json`` and ``.jsonl`` inputs. Unknown structures or parse errors
    return an empty list so the caller can gracefully fall back to in-memory
    records.

    Args:
        path: Artifact path, absolute or relative.

    Returns:
        A list of parsed and normalized observations.
    """
    raw_records = load_m11_shadow_records(path=path)
    if not raw_records:
        return []
    return parse_m11_shadow_records(records=raw_records)


def parse_m11_shadow_records(records: Sequence[Mapping[str, object]]) -> list[M11ShadowObservation]:
    """Normalize raw record mappings into M11 shadow observations.

    Accepted key aliases include:
    - champion side: ``champion_shadow_return`` / ``champion_return``
    - challenger side: ``challenger_shadow_return`` / ``challenger_return``
    - signals: ``champion_signal``, ``challenger_signal``
    - symbol: ``symbol`` / ``code`` / ``ticker``

    Nested sections ``shadow_result`` / ``shadow`` / ``result`` are also read.

    Args:
        records: Raw records from API payloads or persisted artifacts.

    Returns:
        Parsed observations with invalid rows removed.
    """
    normalized: list[M11ShadowObservation] = []
    for record in records:
        parsed = _parse_one(record)
        if parsed is not None:
            normalized.append(parsed)
    return normalized


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
        for key in ("records", "items", "shadow_results", "results", "data"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, Mapping)]
        return [payload]
    return []


def _normalize_record(record: Mapping[str, object]) -> dict[str, object]:
    normalized = {str(key): value for key, value in record.items() if isinstance(key, str)}
    for nested_key in ("shadow_result", "shadow", "result"):
        nested = record.get(nested_key)
        if isinstance(nested, Mapping):
            for key, value in nested.items():
                if isinstance(key, str) and key not in normalized:
                    normalized[key] = value
    return normalized


def _parse_one(record: Mapping[str, object]) -> M11ShadowObservation | None:
    champion_ret = _read_float(
        record,
        keys=("champion_shadow_return", "champion_return", "champion_ret"),
    )
    challenger_ret = _read_float(
        record,
        keys=("challenger_shadow_return", "challenger_return", "challenger_ret"),
    )
    if champion_ret is None or challenger_ret is None:
        return None

    symbol = _read_string(record, keys=("symbol", "code", "ticker")) or "UNKNOWN"
    champion_signal = _read_int(record, keys=("champion_signal",))
    challenger_signal = _read_int(record, keys=("challenger_signal",))
    return M11ShadowObservation(
        symbol=symbol,
        champion_shadow_return=champion_ret,
        challenger_shadow_return=challenger_ret,
        champion_signal=champion_signal,
        challenger_signal=challenger_signal,
    )


def _read_from_scopes(record: Mapping[str, object], key: str) -> object:
    scopes: list[Mapping[str, object]] = [record]
    for nested_key in ("shadow_result", "shadow", "result"):
        nested = record.get(nested_key)
        if isinstance(nested, Mapping):
            scopes.append(nested)
    for scope in scopes:
        if key in scope:
            return scope[key]
    return None


def _read_float(record: Mapping[str, object], keys: Sequence[str]) -> float | None:
    for key in keys:
        value = _read_from_scopes(record, key)
        parsed = _as_float_or_none(value)
        if parsed is not None:
            return parsed
    return None


def _read_int(record: Mapping[str, object], keys: Sequence[str]) -> int | None:
    for key in keys:
        value = _read_from_scopes(record, key)
        parsed = _as_int_or_none(value)
        if parsed is not None:
            return parsed
    return None


def _read_string(record: Mapping[str, object], keys: Sequence[str]) -> str | None:
    for key in keys:
        value = _read_from_scopes(record, key)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
    return None


def _as_float_or_none(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _as_int_or_none(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None
