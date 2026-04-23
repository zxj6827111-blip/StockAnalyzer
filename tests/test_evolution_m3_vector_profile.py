from __future__ import annotations

from pathlib import Path

import pytest

from stock_analyzer.evolution import m3_vector_profile as m3_vector_profile_module
from stock_analyzer.evolution.m3_vector_profile import (
    M3_DEFAULT_VECTOR_PROFILE_ID,
    M3_LEGACY_VECTOR_PROFILE_ID,
    M3VectorProfileRegistry,
    build_default_m3_vector_profile,
    build_legacy_m3_vector_profile,
    build_m3_vector_from_record,
    build_m3_vector_profile,
    resolve_active_m3_vector_profile,
)


def test_m3_vector_profile_registry_registers_default_profile_and_round_trips(
    tmp_path: Path,
) -> None:
    registry = M3VectorProfileRegistry(db_path=tmp_path / "m3_vector_profiles.duckdb")

    record = registry.register(build_default_m3_vector_profile())

    loaded = registry.get_by_id(record.vector_profile_id)
    assert loaded is not None
    assert loaded.model_dump() == record.model_dump()
    assert loaded.vector_profile_id == M3_DEFAULT_VECTOR_PROFILE_ID
    assert loaded.vector_dim == 20
    assert "constraint_pressure" in loaded.feature_components
    assert loaded.is_query_compatible(record.vector_profile_id) is True
    assert loaded.is_query_compatible(M3_LEGACY_VECTOR_PROFILE_ID) is False


def test_m3_vector_profile_registry_can_keep_legacy_and_default_profiles_side_by_side(
    tmp_path: Path,
) -> None:
    registry = M3VectorProfileRegistry(db_path=tmp_path / "m3_vector_profiles.duckdb")

    legacy = registry.register(build_legacy_m3_vector_profile())
    current = registry.register(build_default_m3_vector_profile())

    assert legacy.vector_profile_id == M3_LEGACY_VECTOR_PROFILE_ID
    assert current.vector_profile_id == M3_DEFAULT_VECTOR_PROFILE_ID
    assert len(registry.list_records()) == 2


def test_m3_vector_profile_registry_rejects_conflicting_reuse_of_profile_id(
    tmp_path: Path,
) -> None:
    registry = M3VectorProfileRegistry(db_path=tmp_path / "m3_vector_profiles.duckdb")
    registry.register(build_default_m3_vector_profile())

    with pytest.raises(ValueError, match="vector_profile_id already registered"):
        registry.register_profile(
            vector_profile_id=M3_DEFAULT_VECTOR_PROFILE_ID,
            vector_dim=6,
            feature_components=["open", "high", "low", "close", "log1p_volume", "atr14"],
            normalization_policy="raw_plus_volatility",
            distance_metric="cosine",
        )


def test_m3_vector_profile_builder_requires_matching_dimension() -> None:
    with pytest.raises(ValueError, match="vector_dim must match feature_components length"):
        build_m3_vector_profile(
            vector_dim=5,
            feature_components=["open", "high", "low", "close"],
        )


def test_build_m3_vector_from_record_supports_default_profile_context() -> None:
    profile = build_default_m3_vector_profile()

    vector = build_m3_vector_from_record(
        {
            "open": 10.0,
            "high": 10.4,
            "low": 9.8,
            "close": 10.2,
            "volume": 2_000_000,
            "turnover": 20_200_000,
            "turnover_rate": 4.5,
        },
        vector_profile=profile,
        regime_state="trend_up",
        no_fill_ratio=0.12,
        partial_fill_ratio=0.18,
        constraint_pressure=0.25,
        position_drift_ratio=0.03,
    )

    assert vector is not None
    assert len(vector) == profile.vector_dim
    assert vector[12] == 1.0
    assert vector[13] == 0.0
    assert vector[14] == 0.0
    assert vector[15] == 0.0
    assert vector[16] == 0.12
    assert vector[17] == 0.18
    assert vector[18] == 0.25
    assert vector[19] == 0.03


def test_resolve_active_m3_vector_profile_returns_configured_profile(tmp_path: Path) -> None:
    registry = M3VectorProfileRegistry(db_path=tmp_path / "m3_vector_profiles.duckdb")
    registry.register(build_legacy_m3_vector_profile())
    registry.register(build_default_m3_vector_profile())

    active, resolution = resolve_active_m3_vector_profile(
        registry,
        configured_profile_id=M3_LEGACY_VECTOR_PROFILE_ID,
    )

    assert active.vector_profile_id == M3_LEGACY_VECTOR_PROFILE_ID
    assert resolution["configured_vector_profile_id"] == M3_LEGACY_VECTOR_PROFILE_ID
    assert resolution["active_vector_profile_id"] == M3_LEGACY_VECTOR_PROFILE_ID
    assert resolution["fallback_used"] is False
    assert resolution["registry_record_count"] == 2


def test_resolve_active_m3_vector_profile_can_fallback_to_default(tmp_path: Path) -> None:
    registry = M3VectorProfileRegistry(db_path=tmp_path / "m3_vector_profiles.duckdb")
    registry.register(build_legacy_m3_vector_profile())
    registry.register(build_default_m3_vector_profile())

    active, resolution = resolve_active_m3_vector_profile(
        registry,
        configured_profile_id="missing_profile",
        allow_fallback_to_default=True,
    )

    assert active.vector_profile_id == M3_DEFAULT_VECTOR_PROFILE_ID
    assert resolution["configured_vector_profile_id"] == "missing_profile"
    assert resolution["active_vector_profile_id"] == M3_DEFAULT_VECTOR_PROFILE_ID
    assert resolution["fallback_used"] is True
    assert resolution["fallback_reason"] != ""


def test_resolve_active_m3_vector_profile_rejects_unknown_profile_without_fallback(
    tmp_path: Path,
) -> None:
    registry = M3VectorProfileRegistry(db_path=tmp_path / "m3_vector_profiles.duckdb")
    registry.register(build_legacy_m3_vector_profile())
    registry.register(build_default_m3_vector_profile())

    with pytest.raises(ValueError, match="configured M3 vector profile is not registered"):
        resolve_active_m3_vector_profile(
            registry,
            configured_profile_id="missing_profile",
            allow_fallback_to_default=False,
        )


def test_default_connection_factory_retries_transient_duckdb_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = {"count": 0}

    class _FakeConnection:
        pass

    connection = _FakeConnection()

    class _FakeDuckDbModule:
        @staticmethod
        def connect(*, database: str) -> _FakeConnection:
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise RuntimeError(
                    'IO Error: Could not set lock on file "test.duckdb": '
                    "Conflicting lock is held in PID 0"
                )
            assert database == "test.duckdb"
            return connection

    monkeypatch.setattr(
        m3_vector_profile_module.importlib,
        "import_module",
        lambda name: _FakeDuckDbModule(),
    )
    monkeypatch.setattr(m3_vector_profile_module.time, "sleep", lambda _: None)

    result = m3_vector_profile_module._default_connection_factory("test.duckdb")

    assert result is connection
    assert attempts["count"] == 3
