from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from stock_analyzer.learning.feature_schema_registry import (
    FeatureSchemaRegistry,
    build_feature_schema_record,
    project_frame_to_schema,
)


def test_feature_schema_registry_registers_from_frame_and_round_trips(tmp_path: Path) -> None:
    registry = FeatureSchemaRegistry(db_path=tmp_path / "feature_schema.duckdb")
    frame = pd.DataFrame(
        {
            "ret_1d": pd.Series([0.01, 0.02], dtype="float64"),
            "volume_ratio_5": pd.Series([1.2, 1.1], dtype="float64"),
            "atr14": pd.Series([0.3, 0.4], dtype="float64"),
        }
    )

    record = registry.register_from_frame(
        frame,
        feature_engineer_version="2026.03.25",
        code_version="git:abc123",
        fillna_policy="zero_after_shift",
        normalization_hint="t1_shifted",
    )

    loaded = registry.get_by_id(record.feature_schema_id)
    assert loaded is not None
    assert loaded.model_dump() == record.model_dump()
    assert loaded.feature_names == ["ret_1d", "volume_ratio_5", "atr14"]
    assert loaded.dtype_contract == {
        "atr14": "float64",
        "ret_1d": "float64",
        "volume_ratio_5": "float64",
    }


def test_feature_schema_registry_dedupes_identical_contract_by_hash(tmp_path: Path) -> None:
    registry = FeatureSchemaRegistry(db_path=tmp_path / "feature_schema.duckdb")
    feature_names = ["ret_1d", "volume_ratio_5", "atr14"]

    first = registry.register_feature_names(
        feature_names,
        feature_engineer_version="2026.03.25",
        code_version="git:abc123",
    )
    second = registry.register_feature_names(
        feature_names,
        feature_engineer_version="2026.03.25",
        code_version="git:abc123",
        feature_schema_id="custom_but_same_payload",
    )

    assert second.feature_schema_id == first.feature_schema_id
    assert len(registry.list_records()) == 1


def test_feature_schema_registry_rejects_conflicting_reuse_of_schema_id(tmp_path: Path) -> None:
    registry = FeatureSchemaRegistry(db_path=tmp_path / "feature_schema.duckdb")
    registry.register_feature_names(
        ["ret_1d", "atr14"],
        feature_schema_id="feature_schema_v1_fixed",
        feature_engineer_version="2026.03.25",
        code_version="git:abc123",
    )

    with pytest.raises(ValueError, match="feature_schema_id already registered"):
        registry.register_feature_names(
            ["ret_1d", "volume_ratio_5", "atr14"],
            feature_schema_id="feature_schema_v1_fixed",
            feature_engineer_version="2026.03.25",
            code_version="git:def456",
        )


def test_project_frame_to_schema_reorders_and_fills_projection_compatible_frame() -> None:
    legacy = build_feature_schema_record(
        ["ret_1d", "atr14"],
        feature_engineer_version="2026.03.25",
        code_version="git:abc123",
        feature_schema_id="feature_schema_legacy",
    )
    current = build_feature_schema_record(
        ["ret_1d", "volume_ratio_5", "atr14"],
        feature_engineer_version="2026.03.26",
        code_version="git:def456",
        projection_compatible_from=[legacy.feature_schema_id],
        feature_schema_id="feature_schema_current",
    )
    frame = pd.DataFrame({"atr14": [0.4], "ret_1d": [0.01]})

    projected = project_frame_to_schema(
        frame,
        target_schema=current,
        source_schema_id=legacy.feature_schema_id,
    )

    assert list(projected.columns) == ["ret_1d", "volume_ratio_5", "atr14"]
    assert projected.iloc[0].to_dict() == {
        "ret_1d": 0.01,
        "volume_ratio_5": 0.0,
        "atr14": 0.4,
    }


def test_project_frame_to_schema_rejects_incompatible_source_schema() -> None:
    target = build_feature_schema_record(
        ["ret_1d", "volume_ratio_5", "atr14"],
        feature_engineer_version="2026.03.26",
        code_version="git:def456",
        feature_schema_id="feature_schema_current",
    )
    frame = pd.DataFrame({"ret_1d": [0.01], "atr14": [0.4]})

    with pytest.raises(ValueError, match="not projection-compatible"):
        project_frame_to_schema(
            frame,
            target_schema=target,
            source_schema_id="feature_schema_unknown",
        )


def test_feature_schema_registry_resolves_recursive_projection_compatible_chain(
    tmp_path: Path,
) -> None:
    registry = FeatureSchemaRegistry(db_path=tmp_path / "feature_schema.duckdb")
    legacy = registry.register_feature_names(
        ["ret_1d"],
        feature_schema_id="feature_schema_legacy",
        feature_engineer_version="2026.03.25",
        code_version="git:abc123",
    )
    bridge = registry.register_feature_names(
        ["ret_1d", "atr14"],
        feature_schema_id="feature_schema_bridge",
        feature_engineer_version="2026.03.26",
        code_version="git:def456",
        projection_compatible_from=[legacy.feature_schema_id],
    )
    current = registry.register_feature_names(
        ["ret_1d", "volume_ratio_5", "atr14"],
        feature_schema_id="feature_schema_current",
        feature_engineer_version="2026.03.27",
        code_version="git:ghi789",
        projection_compatible_from=[bridge.feature_schema_id],
    )

    chain = registry.resolve_projection_compatible_records(current.feature_schema_id)

    assert [record.feature_schema_id for record in chain] == [
        current.feature_schema_id,
        bridge.feature_schema_id,
        legacy.feature_schema_id,
    ]
