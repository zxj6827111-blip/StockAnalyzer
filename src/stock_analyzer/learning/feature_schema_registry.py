"""Registry for versioned feature schemas used by the learning protocol."""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import pandas as pd

from stock_analyzer._pydantic_compat import BaseModel, ConfigDict, Field, field_validator


class FeatureSchemaRecord(BaseModel):
    """One immutable feature-schema contract entry."""

    model_config = ConfigDict(extra="forbid")

    feature_schema_id: str
    schema_version: str = "1"
    feature_engineer_name: str = "FeatureEngineer"
    feature_engineer_version: str = "unknown"
    code_version: str = "unknown"
    feature_names: list[str]
    feature_order_signature: str
    fillna_policy: str = "fill_zero"
    dtype_contract: dict[str, str] = Field(default_factory=dict)
    normalization_hint: str = "raw"
    projection_compatible_from: list[str] = Field(default_factory=list)
    feature_schema_hash: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("feature_names")
    @classmethod
    def _validate_feature_names(cls, value: list[str]) -> list[str]:
        normalized = _normalize_feature_names(value)
        if not normalized:
            raise ValueError("feature_names must not be empty")
        return normalized

    @field_validator("projection_compatible_from")
    @classmethod
    def _validate_projection_compatible_from(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = str(item).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            normalized.append(text)
        return normalized


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


def build_feature_schema_record(
    feature_names: Sequence[str],
    *,
    schema_version: str = "1",
    feature_engineer_name: str = "FeatureEngineer",
    feature_engineer_version: str = "unknown",
    code_version: str = "unknown",
    fillna_policy: str = "fill_zero",
    dtype_contract: Mapping[str, str] | None = None,
    normalization_hint: str = "raw",
    projection_compatible_from: Sequence[str] | None = None,
    feature_schema_id: str | None = None,
    created_at: datetime | None = None,
) -> FeatureSchemaRecord:
    """Build one deterministic feature-schema record from ordered feature names."""

    normalized_feature_names = _normalize_feature_names(feature_names)
    normalized_dtype_contract = {
        key: str(value)
        for key, value in sorted((dtype_contract or {}).items())
        if key in normalized_feature_names
    }
    feature_order_signature = _stable_hash({"feature_names": normalized_feature_names})
    feature_schema_hash = _stable_hash(
        {
            "schema_version": str(schema_version),
            "feature_engineer_name": feature_engineer_name.strip(),
            "feature_engineer_version": feature_engineer_version.strip(),
            "code_version": code_version.strip(),
            "feature_names": normalized_feature_names,
            "fillna_policy": fillna_policy.strip(),
            "dtype_contract": normalized_dtype_contract,
            "normalization_hint": normalization_hint.strip(),
        }
    )
    resolved_schema_id = feature_schema_id or _default_feature_schema_id(
        schema_version=str(schema_version),
        feature_schema_hash=feature_schema_hash,
    )
    return FeatureSchemaRecord(
        feature_schema_id=resolved_schema_id,
        schema_version=str(schema_version),
        feature_engineer_name=feature_engineer_name.strip() or "FeatureEngineer",
        feature_engineer_version=feature_engineer_version.strip() or "unknown",
        code_version=code_version.strip() or "unknown",
        feature_names=normalized_feature_names,
        feature_order_signature=feature_order_signature,
        fillna_policy=fillna_policy.strip() or "fill_zero",
        dtype_contract=normalized_dtype_contract,
        normalization_hint=normalization_hint.strip() or "raw",
        projection_compatible_from=list(projection_compatible_from or []),
        feature_schema_hash=feature_schema_hash,
        created_at=created_at or datetime.now(UTC),
    )


def project_frame_to_schema(
    frame: pd.DataFrame,
    *,
    target_schema: FeatureSchemaRecord,
    source_schema_id: str | None = None,
    fill_value: float = 0.0,
    allow_extra_columns: bool = True,
) -> pd.DataFrame:
    """Project one feature frame into the exact column order of a target schema."""

    source_id = (source_schema_id or "").strip()
    if (
        source_id
        and source_id != target_schema.feature_schema_id
        and source_id not in target_schema.projection_compatible_from
    ):
        raise ValueError(
            "source schema is not projection-compatible with target schema: "
            f"{source_id} -> {target_schema.feature_schema_id}"
        )

    extra_columns = [
        column for column in frame.columns if column not in target_schema.feature_names
    ]
    if extra_columns and not allow_extra_columns:
        raise ValueError(f"frame contains extra columns not in target schema: {extra_columns}")

    projected = frame.reindex(columns=target_schema.feature_names, fill_value=fill_value).copy()
    return projected.fillna(fill_value)


class FeatureSchemaRegistry:
    """Persist immutable feature-schema contracts in DuckDB."""

    def __init__(
        self,
        db_path: str | Path,
        table_name: str = "feature_schema_registry",
        connection_factory: Callable[[str], _DuckConnection] | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._table_name = table_name
        self._connection_factory = connection_factory or _default_connection_factory
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    def register(self, record: FeatureSchemaRecord) -> FeatureSchemaRecord:
        """Persist one feature-schema contract, deduping by schema hash."""

        conn = self._connection_factory(str(self._db_path))
        try:
            self._ensure_table(conn=conn)
            existing_by_hash = self._select_one(
                conn=conn,
                where_clause="feature_schema_hash = ?",
                parameters=[record.feature_schema_hash],
            )
            if existing_by_hash is not None:
                return existing_by_hash

            existing_by_id = self._select_one(
                conn=conn,
                where_clause="feature_schema_id = ?",
                parameters=[record.feature_schema_id],
            )
            if existing_by_id is not None:
                raise ValueError(
                    "feature_schema_id already registered with a different schema contract: "
                    f"{record.feature_schema_id}"
                )

            conn.execute(
                (
                    f"INSERT INTO {self._table_name} ("
                    "feature_schema_id, schema_version, feature_engineer_name, "
                    "feature_engineer_version, code_version, feature_names_json, "
                    "feature_order_signature, fillna_policy, dtype_contract_json, "
                    "normalization_hint, projection_compatible_from_json, "
                    "feature_schema_hash, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                _record_parameters(record),
            )
            return record
        finally:
            conn.close()

    def register_feature_names(
        self,
        feature_names: Sequence[str],
        *,
        schema_version: str = "1",
        feature_engineer_name: str = "FeatureEngineer",
        feature_engineer_version: str = "unknown",
        code_version: str = "unknown",
        fillna_policy: str = "fill_zero",
        dtype_contract: Mapping[str, str] | None = None,
        normalization_hint: str = "raw",
        projection_compatible_from: Sequence[str] | None = None,
        feature_schema_id: str | None = None,
        created_at: datetime | None = None,
    ) -> FeatureSchemaRecord:
        """Build and register one schema from an ordered feature-name list."""

        return self.register(
            build_feature_schema_record(
                feature_names=feature_names,
                schema_version=schema_version,
                feature_engineer_name=feature_engineer_name,
                feature_engineer_version=feature_engineer_version,
                code_version=code_version,
                fillna_policy=fillna_policy,
                dtype_contract=dtype_contract,
                normalization_hint=normalization_hint,
                projection_compatible_from=projection_compatible_from,
                feature_schema_id=feature_schema_id,
                created_at=created_at,
            )
        )

    def register_from_frame(
        self,
        feature_frame: pd.DataFrame,
        *,
        schema_version: str = "1",
        feature_engineer_name: str = "FeatureEngineer",
        feature_engineer_version: str = "unknown",
        code_version: str = "unknown",
        fillna_policy: str = "fill_zero",
        normalization_hint: str = "raw",
        projection_compatible_from: Sequence[str] | None = None,
        feature_schema_id: str | None = None,
        created_at: datetime | None = None,
    ) -> FeatureSchemaRecord:
        """Register the exact frame contract observed at snapshot-capture time.

        Phase 1a contract: call this immediately after feature generation and before snapshot
        persistence, so the first registry entry reflects the exact ordered columns the runtime saw.
        """

        dtype_contract = {column: str(dtype) for column, dtype in feature_frame.dtypes.items()}
        return self.register_feature_names(
            feature_names=list(feature_frame.columns),
            schema_version=schema_version,
            feature_engineer_name=feature_engineer_name,
            feature_engineer_version=feature_engineer_version,
            code_version=code_version,
            fillna_policy=fillna_policy,
            dtype_contract=dtype_contract,
            normalization_hint=normalization_hint,
            projection_compatible_from=projection_compatible_from,
            feature_schema_id=feature_schema_id,
            created_at=created_at,
        )

    def get_by_id(self, feature_schema_id: str) -> FeatureSchemaRecord | None:
        """Load one schema by its registry id."""

        conn = self._connection_factory(str(self._db_path))
        try:
            self._ensure_table(conn=conn)
            return self._select_one(
                conn=conn,
                where_clause="feature_schema_id = ?",
                parameters=[feature_schema_id],
            )
        finally:
            conn.close()

    def get_by_hash(self, feature_schema_hash: str) -> FeatureSchemaRecord | None:
        """Load one schema by its deterministic contract hash."""

        conn = self._connection_factory(str(self._db_path))
        try:
            self._ensure_table(conn=conn)
            return self._select_one(
                conn=conn,
                where_clause="feature_schema_hash = ?",
                parameters=[feature_schema_hash],
            )
        finally:
            conn.close()

    def list_records(self) -> list[FeatureSchemaRecord]:
        """Return every registered feature schema ordered by creation time."""

        conn = self._connection_factory(str(self._db_path))
        try:
            self._ensure_table(conn=conn)
            rows = conn.execute(
                f"SELECT {', '.join(_SELECT_COLUMNS)} "
                f"FROM {self._table_name} ORDER BY created_at, feature_schema_id"
            ).fetchall()
            return [_row_to_record(row) for row in rows]
        finally:
            conn.close()

    def resolve_projection_compatible_records(
        self,
        feature_schema_id: str,
        *,
        include_self: bool = True,
    ) -> list[FeatureSchemaRecord]:
        """Return one deterministic target-first projection-compatibility chain."""

        normalized_schema_id = str(feature_schema_id).strip()
        if not normalized_schema_id:
            return []
        records_by_id = {
            record.feature_schema_id: record for record in self.list_records()
        }
        target = records_by_id.get(normalized_schema_id)
        if target is None:
            return []

        ordered: list[FeatureSchemaRecord] = []
        visited: set[str] = set()

        def _visit(schema_id: str) -> None:
            normalized = str(schema_id).strip()
            if not normalized or normalized in visited:
                return
            record = records_by_id.get(normalized)
            if record is None:
                return
            visited.add(normalized)
            ordered.append(record)
            for source_id in record.projection_compatible_from:
                _visit(source_id)

        if include_self:
            _visit(target.feature_schema_id)
        else:
            for source_id in target.projection_compatible_from:
                _visit(source_id)
        return ordered

    def _ensure_table(self, conn: _DuckConnection) -> None:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._table_name} ("
            "feature_schema_id VARCHAR PRIMARY KEY, "
            "schema_version VARCHAR NOT NULL, "
            "feature_engineer_name VARCHAR NOT NULL, "
            "feature_engineer_version VARCHAR NOT NULL, "
            "code_version VARCHAR NOT NULL, "
            "feature_names_json VARCHAR NOT NULL, "
            "feature_order_signature VARCHAR NOT NULL, "
            "fillna_policy VARCHAR NOT NULL, "
            "dtype_contract_json VARCHAR NOT NULL, "
            "normalization_hint VARCHAR NOT NULL, "
            "projection_compatible_from_json VARCHAR NOT NULL, "
            "feature_schema_hash VARCHAR NOT NULL UNIQUE, "
            "created_at VARCHAR NOT NULL"
            ")"
        )

    def _select_one(
        self,
        *,
        conn: _DuckConnection,
        where_clause: str,
        parameters: Sequence[object],
    ) -> FeatureSchemaRecord | None:
        row = conn.execute(
            (
                f"SELECT {', '.join(_SELECT_COLUMNS)} "
                f"FROM {self._table_name} WHERE {where_clause} LIMIT 1"
            ),
            parameters,
        ).fetchone()
        if row is None:
            return None
        return _row_to_record(row)


_SELECT_COLUMNS = (
    "feature_schema_id",
    "schema_version",
    "feature_engineer_name",
    "feature_engineer_version",
    "code_version",
    "feature_names_json",
    "feature_order_signature",
    "fillna_policy",
    "dtype_contract_json",
    "normalization_hint",
    "projection_compatible_from_json",
    "feature_schema_hash",
    "created_at",
)


def _record_parameters(record: FeatureSchemaRecord) -> list[object]:
    return [
        record.feature_schema_id,
        record.schema_version,
        record.feature_engineer_name,
        record.feature_engineer_version,
        record.code_version,
        json.dumps(record.feature_names, ensure_ascii=True),
        record.feature_order_signature,
        record.fillna_policy,
        json.dumps(record.dtype_contract, ensure_ascii=True, sort_keys=True),
        record.normalization_hint,
        json.dumps(record.projection_compatible_from, ensure_ascii=True),
        record.feature_schema_hash,
        record.created_at.astimezone(UTC).isoformat(),
    ]


def _row_to_record(row: Sequence[object]) -> FeatureSchemaRecord:
    return FeatureSchemaRecord(
        feature_schema_id=str(row[0]),
        schema_version=str(row[1]),
        feature_engineer_name=str(row[2]),
        feature_engineer_version=str(row[3]),
        code_version=str(row[4]),
        feature_names=_load_json_list(row[5]),
        feature_order_signature=str(row[6]),
        fillna_policy=str(row[7]),
        dtype_contract=_load_json_dict(row[8]),
        normalization_hint=str(row[9]),
        projection_compatible_from=_load_json_list(row[10]),
        feature_schema_hash=str(row[11]),
        created_at=_parse_datetime(row[12]),
    )


def _normalize_feature_names(feature_names: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for name in feature_names:
        text = str(name).strip()
        if not text:
            raise ValueError("feature name must not be empty")
        if text in seen:
            raise ValueError(f"duplicate feature name detected: {text}")
        seen.add(text)
        normalized.append(text)
    return normalized


def _stable_hash(payload: Mapping[str, Any]) -> str:
    serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _default_feature_schema_id(*, schema_version: str, feature_schema_hash: str) -> str:
    return f"feature_schema_v{schema_version}_{feature_schema_hash[:12]}"


def _load_json_list(value: object) -> list[str]:
    if not isinstance(value, str):
        return []
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded, list):
        return []
    return [str(item).strip() for item in loaded if str(item).strip()]


def _load_json_dict(value: object) -> dict[str, str]:
    if not isinstance(value, str):
        return {}
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {str(key): str(item) for key, item in loaded.items()}


def _parse_datetime(value: object) -> datetime:
    text = str(value).strip()
    if not text:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _default_connection_factory(database: str) -> _DuckConnection:
    duckdb_module = importlib.import_module("duckdb")
    connection = cast(_DuckConnection, duckdb_module.connect(database=database))
    return connection
