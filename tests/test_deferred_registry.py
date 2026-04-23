from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from stock_analyzer.deferred_registry import (
    build_deferred_items_registry,
    write_deferred_items_registry,
)


def _as_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _as_mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [_as_mapping(item) for item in value]


def test_deferred_registry_contains_scheme_items() -> None:
    registry = build_deferred_items_registry()

    assert registry["status"] == "completed_research_registry"
    names = {str(item["name"]) for item in _as_mapping_list(registry["items"])}
    assert "TabNet / FT-Transformer" in names
    assert "TFT" in names
    assert "FinRL" in names
    assert all(str(item.get("status", "")) == "completed" for item in _as_mapping_list(registry["items"]))


def test_deferred_registry_persists_json(tmp_path: Path) -> None:
    registry = build_deferred_items_registry()
    output_path = tmp_path / "acceptance" / "deferred_items_registry.json"
    written = write_deferred_items_registry(registry=registry, output_path=output_path)

    payload = json.loads(Path(written).read_text(encoding="utf-8"))
    assert payload["scope"] == "phase_d6"
    assert len(_as_mapping_list(_as_mapping(payload)["items"])) >= 4
