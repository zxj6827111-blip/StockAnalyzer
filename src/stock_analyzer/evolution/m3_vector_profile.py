"""Registry for immutable M3 vector-profile contracts."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from stock_analyzer._pydantic_compat import BaseModel, ConfigDict, Field, field_validator


class M3VectorProfileRecord(BaseModel):
    """One immutable M3 vector-profile contract entry."""

    model_config = ConfigDict(extra="forbid")

    vector_profile_id: str
    schema_version: str = "1"
    vector_dim: int
    feature_components: list[str]
    normalization_policy: str = "raw"
    distance_metric: str = "cosine"
    compatible_query_profiles: list[str] = Field(default_factory=list)
    vector_profile_hash: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("vector_dim")
    @classmethod
    def _validate_vector_dim(cls, value: int) -> int:
        normalized = int(value)
        if normalized <= 0:
            raise ValueError("vector_dim must be positive")
        return normalized

    @field_validator("feature_components")
    @classmethod
    def _validate_feature_components(cls, value: list[str]) -> list[str]:
        normalized = _normalize_feature_components(value)
        if not normalized:
            raise ValueError("feature_components must not be empty")
        return normalized

    @field_validator("compatible_query_profiles")
    @classmethod
    def _validate_compatible_query_profiles(cls, value: list[str]) -> list[str]:
        return _normalize_profile_ids(value)

    def is_query_compatible(self, query_profile_id: str) -> bool:
        """Return whether one query profile can search against this profile."""

        normalized = str(query_profile_id).strip()
        if not normalized:
            return False
        if not self.compatible_query_profiles:
            return normalized == self.vector_profile_id
        return normalized in self.compatible_query_profiles


class _DuckCursor(Protocol):
    def fetchone(self) -> Sequence[object] | None: ...

    def fetchall(self) -> list[Sequence[object]]: ...


class _DuckConnection(Protocol):
    def execute(
        self,
        query: str,
        parameters: Sequence[object] | None = None,
    ) -> _DuckCursor: ...

    def close(self) -> None: ...


M3_LEGACY_VECTOR_PROFILE_ID = "m3_price_volume_v1"
M3_DEFAULT_VECTOR_PROFILE_ID = "m3_price_shape_execution_v2"


def build_m3_vector_profile(
    *,
    vector_dim: int,
    feature_components: Sequence[str],
    normalization_policy: str = "raw",
    distance_metric: str = "cosine",
    compatible_query_profiles: Sequence[str] | None = None,
    schema_version: str = "1",
    vector_profile_id: str | None = None,
    created_at: datetime | None = None,
) -> M3VectorProfileRecord:
    """Build one deterministic M3 vector-profile record."""

    normalized_dim = int(vector_dim)
    if normalized_dim <= 0:
        raise ValueError("vector_dim must be positive")
    normalized_components = _normalize_feature_components(feature_components)
    if not normalized_components:
        raise ValueError("feature_components must not be empty")
    if len(normalized_components) != normalized_dim:
        raise ValueError(
            "vector_dim must match feature_components length: "
            f"{normalized_dim} != {len(normalized_components)}"
        )
    normalized_compatible_query_profiles = _normalize_profile_ids(compatible_query_profiles or [])
    payload = {
        "schema_version": str(schema_version),
        "vector_dim": normalized_dim,
        "feature_components": normalized_components,
        "normalization_policy": normalization_policy.strip() or "raw",
        "distance_metric": distance_metric.strip() or "cosine",
        "compatible_query_profiles": normalized_compatible_query_profiles,
    }
    vector_profile_hash = _stable_hash(payload)
    resolved_profile_id = vector_profile_id or _default_vector_profile_id(
        schema_version=str(schema_version),
        vector_profile_hash=vector_profile_hash,
    )
    return M3VectorProfileRecord(
        vector_profile_id=resolved_profile_id,
        schema_version=str(schema_version),
        vector_dim=normalized_dim,
        feature_components=normalized_components,
        normalization_policy=normalization_policy.strip() or "raw",
        distance_metric=distance_metric.strip() or "cosine",
        compatible_query_profiles=normalized_compatible_query_profiles,
        vector_profile_hash=vector_profile_hash,
        created_at=created_at or datetime.now(UTC),
    )


def build_legacy_m3_vector_profile(
    *,
    created_at: datetime | None = None,
) -> M3VectorProfileRecord:
    """Build the legacy five-dimensional M3 profile for backward compatibility."""

    return build_m3_vector_profile(
        vector_profile_id=M3_LEGACY_VECTOR_PROFILE_ID,
        vector_dim=5,
        feature_components=["open", "high", "low", "close", "log1p_volume"],
        normalization_policy="raw_ohlc_log1p_volume",
        distance_metric="cosine",
        compatible_query_profiles=[],
        created_at=created_at,
    )


def build_default_m3_vector_profile(
    *,
    created_at: datetime | None = None,
) -> M3VectorProfileRecord:
    """Build the active high-dimensional M3 profile used by the live orchestrator."""

    return build_m3_vector_profile(
        vector_profile_id=M3_DEFAULT_VECTOR_PROFILE_ID,
        vector_dim=20,
        feature_components=[
            "open_rel_close",
            "high_rel_close",
            "low_rel_close",
            "intraday_range_rel_close",
            "real_body_rel_close",
            "upper_shadow_rel_range",
            "lower_shadow_rel_range",
            "close_position_in_range",
            "log1p_volume",
            "log1p_turnover",
            "turnover_rate",
            "vwap_proxy_rel_close",
            "regime_trend_up_flag",
            "regime_trend_down_flag",
            "regime_range_flag",
            "regime_extreme_flag",
            "no_fill_ratio",
            "partial_fill_ratio",
            "constraint_pressure",
            "position_drift_ratio",
        ],
        normalization_policy="shape_turnover_regime_execution_v2",
        distance_metric="cosine",
        compatible_query_profiles=[],
        created_at=created_at,
    )


def build_m3_vector_from_record(
    record: Mapping[str, object],
    *,
    vector_profile: M3VectorProfileRecord,
    regime_state: str = "",
    no_fill_ratio: float = 0.0,
    partial_fill_ratio: float = 0.0,
    constraint_pressure: float = 0.0,
    position_drift_ratio: float = 0.0,
) -> list[float] | None:
    """Build one profile-aligned M3 vector from a market record and runtime context."""

    close = _as_float(record.get("close"), default=0.0)
    if close <= 0.0:
        return None

    open_px = _as_float(record.get("open"), default=0.0)
    high = _as_float(record.get("high"), default=0.0)
    low = _as_float(record.get("low"), default=0.0)
    volume = max(0.0, _as_float(record.get("volume"), default=0.0))
    turnover = max(0.0, _resolve_turnover(record=record, close=close, volume=volume))
    turnover_rate = _normalize_ratio(_as_float(record.get("turnover_rate"), default=0.0))

    close_safe = max(abs(close), 1e-12)
    range_abs = max(high - low, 1e-12)
    real_body = abs(close - open_px)
    upper_shadow = max(high - max(open_px, close), 0.0)
    lower_shadow = max(min(open_px, close) - low, 0.0)
    close_position_in_range = (close - low) / range_abs
    vwap_proxy_rel_close = 0.0
    if volume > 0.0 and turnover > 0.0:
        vwap_proxy_rel_close = (turnover / volume) / close_safe - 1.0

    normalized_regime = str(regime_state).strip().lower()
    feature_values = {
        "open": open_px,
        "high": high,
        "low": low,
        "close": close,
        "log1p_volume": math.log1p(volume),
        "open_rel_close": open_px / close_safe - 1.0,
        "high_rel_close": high / close_safe - 1.0,
        "low_rel_close": low / close_safe - 1.0,
        "intraday_range_rel_close": (high - low) / close_safe,
        "real_body_rel_close": real_body / close_safe,
        "upper_shadow_rel_range": upper_shadow / range_abs,
        "lower_shadow_rel_range": lower_shadow / range_abs,
        "close_position_in_range": close_position_in_range,
        "log1p_turnover": math.log1p(turnover),
        "turnover_rate": turnover_rate,
        "vwap_proxy_rel_close": vwap_proxy_rel_close,
        "regime_trend_up_flag": 1.0 if normalized_regime == "trend_up" else 0.0,
        "regime_trend_down_flag": 1.0 if normalized_regime == "trend_down" else 0.0,
        "regime_range_flag": 1.0 if normalized_regime == "range" else 0.0,
        "regime_extreme_flag": 1.0 if normalized_regime == "extreme" else 0.0,
        "no_fill_ratio": _normalize_ratio(no_fill_ratio),
        "partial_fill_ratio": _normalize_ratio(partial_fill_ratio),
        "constraint_pressure": max(0.0, float(constraint_pressure)),
        "position_drift_ratio": max(0.0, _normalize_ratio(position_drift_ratio)),
    }
    vector: list[float] = []
    for component in vector_profile.feature_components:
        if component not in feature_values:
            raise ValueError(f"unsupported M3 vector component: {component}")
        vector.append(float(feature_values[component]))
    return vector


class M3VectorProfileRegistry:
    """Persist immutable M3 vector-profile contracts in DuckDB."""

    def __init__(
        self,
        db_path: str | Path,
        table_name: str = "m3_vector_profile_registry",
        connection_factory: Callable[[str], _DuckConnection] | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._table_name = table_name
        self._connection_factory = connection_factory or _default_connection_factory
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    def register(self, record: M3VectorProfileRecord) -> M3VectorProfileRecord:
        """Persist one vector profile, deduping by contract hash."""

        _validate_record_consistency(record)
        conn = self._connection_factory(str(self._db_path))
        try:
            self._ensure_table(conn=conn)
            existing_by_hash = self._select_one(
                conn=conn,
                where_clause="vector_profile_hash = ?",
                parameters=[record.vector_profile_hash],
            )
            if existing_by_hash is not None:
                return existing_by_hash

            existing_by_id = self._select_one(
                conn=conn,
                where_clause="vector_profile_id = ?",
                parameters=[record.vector_profile_id],
            )
            if existing_by_id is not None:
                raise ValueError(
                    "vector_profile_id already registered with a different profile contract: "
                    f"{record.vector_profile_id}"
                )

            conn.execute(
                (
                    f"INSERT INTO {self._table_name} ("
                    "vector_profile_id, schema_version, vector_dim, feature_components_json, "
                    "normalization_policy, distance_metric, compatible_query_profiles_json, "
                    "vector_profile_hash, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                _record_parameters(record),
            )
            return record
        finally:
            conn.close()

    def register_profile(
        self,
        *,
        vector_dim: int,
        feature_components: Sequence[str],
        normalization_policy: str = "raw",
        distance_metric: str = "cosine",
        compatible_query_profiles: Sequence[str] | None = None,
        schema_version: str = "1",
        vector_profile_id: str | None = None,
        created_at: datetime | None = None,
    ) -> M3VectorProfileRecord:
        """Build and register one vector profile."""

        return self.register(
            build_m3_vector_profile(
                vector_dim=vector_dim,
                feature_components=feature_components,
                normalization_policy=normalization_policy,
                distance_metric=distance_metric,
                compatible_query_profiles=compatible_query_profiles,
                schema_version=schema_version,
                vector_profile_id=vector_profile_id,
                created_at=created_at,
            )
        )

    def get_by_id(self, vector_profile_id: str) -> M3VectorProfileRecord | None:
        """Load one profile by registry id."""

        conn = self._connection_factory(str(self._db_path))
        try:
            self._ensure_table(conn=conn)
            return self._select_one(
                conn=conn,
                where_clause="vector_profile_id = ?",
                parameters=[vector_profile_id],
            )
        finally:
            conn.close()

    def get_by_hash(self, vector_profile_hash: str) -> M3VectorProfileRecord | None:
        """Load one profile by deterministic contract hash."""

        conn = self._connection_factory(str(self._db_path))
        try:
            self._ensure_table(conn=conn)
            return self._select_one(
                conn=conn,
                where_clause="vector_profile_hash = ?",
                parameters=[vector_profile_hash],
            )
        finally:
            conn.close()

    def list_records(self) -> list[M3VectorProfileRecord]:
        """Return every registered vector profile ordered by creation time."""

        conn = self._connection_factory(str(self._db_path))
        try:
            self._ensure_table(conn=conn)
            rows = conn.execute(
                f"SELECT {', '.join(_SELECT_COLUMNS)} "
                f"FROM {self._table_name} ORDER BY created_at, vector_profile_id"
            ).fetchall()
            return [_row_to_record(row) for row in rows]
        finally:
            conn.close()

    def _ensure_table(self, conn: _DuckConnection) -> None:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._table_name} ("
            "vector_profile_id VARCHAR PRIMARY KEY, "
            "schema_version VARCHAR NOT NULL, "
            "vector_dim INTEGER NOT NULL, "
            "feature_components_json VARCHAR NOT NULL, "
            "normalization_policy VARCHAR NOT NULL, "
            "distance_metric VARCHAR NOT NULL, "
            "compatible_query_profiles_json VARCHAR NOT NULL, "
            "vector_profile_hash VARCHAR NOT NULL, "
            "created_at VARCHAR NOT NULL"
            ")"
        )

    def _select_one(
        self,
        *,
        conn: _DuckConnection,
        where_clause: str,
        parameters: Sequence[object],
    ) -> M3VectorProfileRecord | None:
        row = conn.execute(
            f"SELECT {', '.join(_SELECT_COLUMNS)} "
            f"FROM {self._table_name} WHERE {where_clause} LIMIT 1",
            list(parameters),
        ).fetchone()
        if row is None:
            return None
        return _row_to_record(row)


def resolve_active_m3_vector_profile(
    registry: M3VectorProfileRegistry,
    *,
    configured_profile_id: str,
    allow_fallback_to_default: bool = False,
    fallback_profile_id: str = M3_DEFAULT_VECTOR_PROFILE_ID,
) -> tuple[M3VectorProfileRecord, dict[str, object]]:
    """Resolve the active runtime M3 vector profile from the registry."""

    normalized_configured_profile_id = str(configured_profile_id).strip()
    if not normalized_configured_profile_id:
        normalized_configured_profile_id = M3_DEFAULT_VECTOR_PROFILE_ID
    known_records = registry.list_records()
    known_profile_ids = [record.vector_profile_id for record in known_records]
    active_record = registry.get_by_id(normalized_configured_profile_id)
    if active_record is not None:
        return active_record, {
            "configured_vector_profile_id": normalized_configured_profile_id,
            "active_vector_profile_id": active_record.vector_profile_id,
            "fallback_used": False,
            "fallback_reason": "",
            "registry_record_count": len(known_records),
            "registry_known_profile_ids": known_profile_ids,
        }
    if allow_fallback_to_default:
        fallback_record = registry.get_by_id(str(fallback_profile_id).strip())
        if fallback_record is not None:
            return fallback_record, {
                "configured_vector_profile_id": normalized_configured_profile_id,
                "active_vector_profile_id": fallback_record.vector_profile_id,
                "fallback_used": True,
                "fallback_reason": (
                    "configured profile is missing from registry and default fallback was used"
                ),
                "registry_record_count": len(known_records),
                "registry_known_profile_ids": known_profile_ids,
            }
    known_ids_text = ", ".join(known_profile_ids) if known_profile_ids else "<empty>"
    raise ValueError(
        "configured M3 vector profile is not registered: "
        f"{normalized_configured_profile_id} (known: {known_ids_text})"
    )


_SELECT_COLUMNS = [
    "vector_profile_id",
    "schema_version",
    "vector_dim",
    "feature_components_json",
    "normalization_policy",
    "distance_metric",
    "compatible_query_profiles_json",
    "vector_profile_hash",
    "created_at",
]

_DUCKDB_LOCK_RETRY_ATTEMPTS = 8
_DUCKDB_LOCK_RETRY_BASE_DELAY_SEC = 0.25
_DUCKDB_LOCK_RETRY_MAX_DELAY_SEC = 2.0


def _record_parameters(record: M3VectorProfileRecord) -> list[object]:
    return [
        record.vector_profile_id,
        record.schema_version,
        record.vector_dim,
        json.dumps(record.feature_components, ensure_ascii=True, separators=(",", ":")),
        record.normalization_policy,
        record.distance_metric,
        json.dumps(record.compatible_query_profiles, ensure_ascii=True, separators=(",", ":")),
        record.vector_profile_hash,
        record.created_at.astimezone(UTC).isoformat(),
    ]


def _row_to_record(row: Sequence[object]) -> M3VectorProfileRecord:
    feature_components = json.loads(str(row[3]))
    compatible_query_profiles = json.loads(str(row[6]))
    if not isinstance(feature_components, list):
        raise ValueError("invalid M3 vector profile row: feature_components_json")
    if not isinstance(compatible_query_profiles, list):
        raise ValueError("invalid M3 vector profile row: compatible_query_profiles_json")
    record = M3VectorProfileRecord(
        vector_profile_id=str(row[0]),
        schema_version=str(row[1]),
        vector_dim=int(row[2]),
        feature_components=[str(item) for item in feature_components],
        normalization_policy=str(row[4]),
        distance_metric=str(row[5]),
        compatible_query_profiles=[str(item) for item in compatible_query_profiles],
        vector_profile_hash=str(row[7]),
        created_at=datetime.fromisoformat(str(row[8])),
    )
    _validate_record_consistency(record)
    return record


def _validate_record_consistency(record: M3VectorProfileRecord) -> None:
    if len(record.feature_components) != record.vector_dim:
        raise ValueError(
            "vector_dim must match feature_components length: "
            f"{record.vector_dim} != {len(record.feature_components)}"
        )


def _resolve_turnover(*, record: Mapping[str, object], close: float, volume: float) -> float:
    for key in ("turnover", "amount", "成交额"):
        value = _as_float(record.get(key), default=0.0)
        if value > 0.0:
            return value
    return close * max(0.0, volume)


def _normalize_ratio(value: float) -> float:
    normalized = float(value)
    if normalized > 1.0 and normalized <= 100.0:
        normalized = normalized / 100.0
    return normalized


def _as_float(value: object, default: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _normalize_feature_components(values: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    for item in values:
        text = str(item).strip()
        if not text:
            raise ValueError("feature_components must not contain empty names")
        normalized.append(text)
    return normalized


def _normalize_profile_ids(values: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def _stable_hash(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _default_vector_profile_id(*, schema_version: str, vector_profile_hash: str) -> str:
    return f"m3_vector_profile_v{schema_version}_{vector_profile_hash[:12]}"


def _default_connection_factory(database: str) -> _DuckConnection:
    duckdb_module = importlib.import_module("duckdb")
    delay_sec = _DUCKDB_LOCK_RETRY_BASE_DELAY_SEC
    for attempt in range(1, _DUCKDB_LOCK_RETRY_ATTEMPTS + 1):
        try:
            return cast(_DuckConnection, duckdb_module.connect(database=database))
        except Exception as exc:
            if not _is_retryable_duckdb_lock_error(exc) or attempt >= _DUCKDB_LOCK_RETRY_ATTEMPTS:
                raise
            time.sleep(delay_sec)
            delay_sec = min(_DUCKDB_LOCK_RETRY_MAX_DELAY_SEC, delay_sec * 2.0)
    raise RuntimeError("unreachable")


def _is_retryable_duckdb_lock_error(exc: Exception) -> bool:
    message = str(exc).strip().lower()
    return (
        "could not set lock on file" in message
        or "conflicting lock is held" in message
    )
