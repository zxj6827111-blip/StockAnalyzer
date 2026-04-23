"""Specification hashing utilities for evolution runtime."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from stock_analyzer.config import EvolutionConfig


def hash_payload(payload: Mapping[str, object]) -> str:
    """Return deterministic sha256 hash for one JSON-compatible payload."""
    canonical = _stable_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_spec_hash_bundle(config: EvolutionConfig) -> dict[str, object]:
    """Build execution/runtime/universe hashes and materialized payload snapshots."""
    execution_payload = config.execution_spec.model_dump(mode="json")
    runtime_payload = config.runtime_spec.model_dump(mode="json")
    universe_payload = config.universe_spec.model_dump(mode="json")
    return {
        "execution_spec_hash": hash_payload(execution_payload),
        "runtime_config_hash": hash_payload(runtime_payload),
        "universe_spec_hash": hash_payload(universe_payload),
        "execution_spec": execution_payload,
        "runtime_spec": runtime_payload,
        "universe_spec": universe_payload,
    }


def _stable_json(payload: Mapping[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _json_default(value: Any) -> object:
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, tuple):
        return list(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    raise TypeError(f"object of type {type(value)!r} is not JSON serializable")

