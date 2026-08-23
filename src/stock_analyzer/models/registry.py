"""DuckDB-backed model registry with explicit role/lifecycle separation."""

from __future__ import annotations

import hashlib
import importlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

from stock_analyzer._pydantic_compat import BaseModel, ConfigDict, Field, field_validator
from stock_analyzer.models.artifact import ModelArtifact


class ModelRole(StrEnum):
    CHAMPION = "champion"
    CHALLENGER = "challenger"
    SHADOW = "shadow"


class ModelLifecycleState(StrEnum):
    REGISTERED = "registered"
    TRAINED = "trained"
    SHADOW_VALIDATED = "shadow_validated"
    APPROVED = "approved"
    BLOCKED = "blocked"
    REVOKED = "revoked"


class ModelRegistryRecord(BaseModel):
    """One immutable snapshot of model registry state."""

    model_config = ConfigDict(extra="forbid")

    model_id: str
    schema_version: str = "1"
    role: ModelRole = ModelRole.CHALLENGER
    lifecycle_state: ModelLifecycleState = ModelLifecycleState.REGISTERED
    parent_model_id: str = ""
    artifact_uri: str
    artifact_created_at: datetime | None = None
    dataset_manifest_id: str = ""
    feature_schema_id: str = ""
    feature_schema_hash: str = ""
    label_policy_id: str = ""
    label_policy_hash: str = ""
    # 内容寻址 bundle 的 SHA256（64 hex）；空串表示旧版松散工件（未迁移记录）。
    artifact_content_hash: str = ""
    metrics_summary: dict[str, float] = Field(default_factory=dict)
    blocked_reason: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    promoted_at: datetime | None = None
    revoked_at: datetime | None = None

    @field_validator("model_id", "artifact_uri")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("required text field must not be empty")
        return normalized

    @field_validator(
        "schema_version",
        "parent_model_id",
        "dataset_manifest_id",
        "feature_schema_id",
        "feature_schema_hash",
        "label_policy_id",
        "label_policy_hash",
        "blocked_reason",
    )
    @classmethod
    def _normalize_optional_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("metrics_summary")
    @classmethod
    def _validate_metrics_summary(cls, value: dict[str, float]) -> dict[str, float]:
        normalized: dict[str, float] = {}
        for key, item in value.items():
            text = str(key).strip()
            if not text:
                raise ValueError("metrics_summary keys must not be empty")
            normalized[text] = float(item)
        return normalized


class ModelRegistryReadError(RuntimeError):
    """Raised when read-only registry access stays unavailable after retries."""


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


class ModelRegistryCASConflictError(RuntimeError):
    """Raised when the expected-champion precondition fails inside a CAS promotion."""


def build_model_registry_record_from_artifact(
    *,
    artifact: ModelArtifact,
    artifact_uri: str,
    role: ModelRole = ModelRole.CHALLENGER,
    lifecycle_state: ModelLifecycleState = ModelLifecycleState.TRAINED,
    parent_model_id: str = "",
    metrics_summary: Mapping[str, float] | None = None,
    model_id: str | None = None,
    artifact_content_hash: str = "",
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> ModelRegistryRecord:
    """Build one registry record from a saved model artifact."""

    normalized_artifact_uri = str(artifact_uri).strip()
    if not normalized_artifact_uri:
        raise ValueError("artifact_uri must not be empty")
    artifact_created_at = _parse_optional_datetime(artifact.created_at)
    now = created_at or artifact_created_at or datetime.now(UTC)
    metrics_payload = dict(metrics_summary or artifact.training_metrics)
    resolved_model_id = model_id or _build_model_id(
        artifact=artifact,
        artifact_uri=normalized_artifact_uri,
    )
    return ModelRegistryRecord(
        model_id=resolved_model_id,
        role=role,
        lifecycle_state=lifecycle_state,
        parent_model_id=str(parent_model_id).strip(),
        artifact_uri=normalized_artifact_uri,
        artifact_created_at=artifact_created_at,
        dataset_manifest_id=artifact.dataset_manifest_id,
        feature_schema_id=artifact.feature_schema_id,
        feature_schema_hash=artifact.feature_schema_hash,
        label_policy_id=artifact.label_policy_id,
        label_policy_hash=artifact.label_policy_hash,
        artifact_content_hash=str(artifact_content_hash).strip(),
        metrics_summary=metrics_payload,
        created_at=now,
        updated_at=updated_at or now,
    )


class ModelRegistry:
    """Persist model governance metadata in DuckDB."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        table_name: str = "model_registry",
        connection_factory: Callable[..., _DuckConnection] | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._table_name = table_name
        self._connection_factory = connection_factory or _default_connection_factory
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # schema 迁移只跑一次的缓存标记：写路径首次执行后置 True，只读路径不触发。
        self._schema_ready = False

    def ensure_schema(self) -> None:
        """Create/migrate the registry table once via a writable connection.

        服务启动时调用一次；迁移幂等（CREATE IF NOT EXISTS + 条件 ALTER +
        NULL 回填）。只读路径绝不调用本方法，避免重复取写锁。
        """

        if self._schema_ready:
            return
        conn = self._connection_factory(str(self._db_path))
        try:
            self._ensure_table(conn=conn)
            self._schema_ready = True
        finally:
            conn.close()

    def register(self, record: ModelRegistryRecord) -> ModelRegistryRecord:
        """Insert one registry record, returning the existing copy on idempotent replays."""

        _validate_lifecycle_requirements(record)
        existing = self.get_by_id(record.model_id, suppress_read_errors=True)
        if existing is not None:
            if existing.model_dump(mode="json") == record.model_dump(mode="json"):
                return existing
            raise ValueError(
                "model_id already registered with a different record: "
                f"{record.model_id}"
            )
        conn = self._connection_factory(str(self._db_path))
        try:
            self._ensure_table(conn=conn)
            self._schema_ready = True
            conn.execute(
                (
                    f"INSERT INTO {self._table_name} ("
                    "model_id, schema_version, role, lifecycle_state, parent_model_id, "
                    "artifact_uri, artifact_created_at, dataset_manifest_id, feature_schema_id, "
                    "feature_schema_hash, label_policy_id, label_policy_hash, "
                    "artifact_content_hash, metrics_summary_json, blocked_reason, "
                    "created_at, updated_at, promoted_at, revoked_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                _record_parameters(record),
            )
            return record
        finally:
            conn.close()

    def upsert_repair_record(self, record: ModelRegistryRecord) -> ModelRegistryRecord:
        """Insert or replace one explicitly repaired governance record."""

        _validate_lifecycle_requirements(record)
        existing = self.get_by_id(record.model_id, suppress_read_errors=True)
        if existing is None:
            return self.register(record)
        if existing.model_dump(mode="json") == record.model_dump(mode="json"):
            return existing
        replacement = record.model_copy(
            update={"created_at": existing.created_at},
            deep=True,
        )
        return self._replace_record(replacement)

    def register_artifact(
        self,
        *,
        artifact: ModelArtifact,
        artifact_uri: str,
        role: ModelRole = ModelRole.CHALLENGER,
        lifecycle_state: ModelLifecycleState = ModelLifecycleState.TRAINED,
        parent_model_id: str = "",
        metrics_summary: Mapping[str, float] | None = None,
        model_id: str | None = None,
        artifact_content_hash: str = "",
        now: datetime | None = None,
    ) -> ModelRegistryRecord:
        """Build and persist one record directly from a model artifact."""

        return self.register(
            build_model_registry_record_from_artifact(
                artifact=artifact,
                artifact_uri=artifact_uri,
                role=role,
                lifecycle_state=lifecycle_state,
                parent_model_id=parent_model_id,
                metrics_summary=metrics_summary,
                model_id=model_id,
                artifact_content_hash=artifact_content_hash,
                created_at=now,
                updated_at=now,
            )
        )

    def get_by_id(
        self,
        model_id: str,
        *,
        suppress_read_errors: bool = False,
    ) -> ModelRegistryRecord | None:
        """Load one registry record by model id."""

        try:
            conn = self._open_read_connection()
        except ModelRegistryReadError:
            if suppress_read_errors:
                return None
            raise
        if conn is None:
            return None
        try:
            records = self._fetch_records(
                conn,
                where_clause=" WHERE model_id = ? LIMIT 1",
                parameters=[model_id],
            )
            return records[0] if records else None
        except Exception as exc:
            if self._is_missing_table_error(exc):
                return None
            if self._is_retryable_read_error(exc):
                if suppress_read_errors:
                    return None
                raise ModelRegistryReadError(str(exc)) from exc
            raise
        finally:
            conn.close()

    def list_records(
        self,
        *,
        role: ModelRole | None = None,
        lifecycle_state: ModelLifecycleState | None = None,
        limit: int | None = None,
        suppress_read_errors: bool = False,
    ) -> list[ModelRegistryRecord]:
        """List registry entries ordered by newest updates first."""

        conditions: list[str] = []
        parameters: list[object] = []
        if role is not None:
            conditions.append("role = ?")
            parameters.append(role.value)
        if lifecycle_state is not None:
            conditions.append("lifecycle_state = ?")
            parameters.append(lifecycle_state.value)
        where_clause = ""
        if conditions:
            where_clause = " WHERE " + " AND ".join(conditions)
        limit_clause = ""
        if limit is not None and int(limit) > 0:
            limit_clause = f" LIMIT {int(limit)}"

        try:
            conn = self._open_read_connection()
        except ModelRegistryReadError:
            if suppress_read_errors:
                return []
            raise
        if conn is None:
            return []
        try:
            return self._fetch_records(
                conn,
                where_clause=where_clause,
                parameters=parameters,
                order_clause=(
                    " ORDER BY updated_at DESC, created_at DESC, model_id DESC"
                    f"{limit_clause}"
                ),
            )
        except Exception as exc:
            if self._is_missing_table_error(exc):
                return []
            if self._is_retryable_read_error(exc):
                if suppress_read_errors:
                    return []
                raise ModelRegistryReadError(str(exc)) from exc
            raise
        finally:
            conn.close()

    def update_lifecycle(
        self,
        *,
        model_id: str,
        lifecycle_state: ModelLifecycleState,
        blocked_reason: str = "",
        now: datetime | None = None,
    ) -> ModelRegistryRecord:
        """Advance one registry record through the allowed lifecycle graph."""

        current = self.get_by_id(model_id)
        if current is None:
            raise ValueError(f"model_id not found: {model_id}")
        next_record = _transition_record(
            current=current,
            lifecycle_state=lifecycle_state,
            blocked_reason=blocked_reason,
            now=now or datetime.now(UTC),
        )
        return self._replace_record(next_record)

    def update_role(
        self,
        *,
        model_id: str,
        role: ModelRole,
        now: datetime | None = None,
    ) -> ModelRegistryRecord:
        """Update one registry record's governance role."""

        current = self.get_by_id(model_id)
        if current is None:
            raise ValueError(f"model_id not found: {model_id}")
        next_record = current.model_copy(
            update={
                "role": role,
                "updated_at": now or datetime.now(UTC),
            },
            deep=True,
        )
        if (
            role == ModelRole.CHAMPION
            and next_record.lifecycle_state != ModelLifecycleState.APPROVED
        ):
            raise ValueError("champion role requires approved lifecycle state")
        return self._replace_record(next_record)

    def active_champion(
        self,
        *,
        suppress_read_errors: bool = False,
    ) -> ModelRegistryRecord | None:
        """Return the newest approved champion entry, if any."""

        champions = self.list_records(
            role=ModelRole.CHAMPION,
            lifecycle_state=ModelLifecycleState.APPROVED,
            limit=1,
            suppress_read_errors=suppress_read_errors,
        )
        return champions[0] if champions else None

    def promote_model_with_cas(
        self,
        *,
        target_model_id: str,
        expected_previous_champion_id: str = "",
        now: datetime | None = None,
    ) -> tuple[ModelRegistryRecord, list[ModelRegistryRecord]]:
        """Demote the expected champion and promote the target in ONE transaction.

        CAS 语义：事务内校验当前 approved champion 集合必须恰好等于
        ``expected_previous_champion_id``（为空则要求当前无 champion），
        否则整体回滚并抛出 :class:`ModelRegistryCASConflictError`，
        防止并发发布产生双 champion / 无 champion。
        """

        normalized_target = str(target_model_id).strip()
        normalized_expected = str(expected_previous_champion_id).strip()
        timestamp = _dump_datetime(now or datetime.now(UTC))
        conn = self._connection_factory(str(self._db_path))
        try:
            self._ensure_table(conn=conn)
            self._schema_ready = True
            conn.execute("BEGIN TRANSACTION")
            try:
                rows = conn.execute(
                    f"SELECT {', '.join(_SELECT_COLUMNS)} "
                    f"FROM {self._table_name} WHERE role = ?",
                    [ModelRole.CHAMPION.value],
                ).fetchall()
                current_champions = [_row_to_record(row) for row in rows]
                approved_ids = sorted(
                    item.model_id
                    for item in current_champions
                    if item.lifecycle_state == ModelLifecycleState.APPROVED
                )
                expected_ids = [normalized_expected] if normalized_expected else []
                if approved_ids != expected_ids:
                    raise ModelRegistryCASConflictError(
                        "expected-champion precondition failed: "
                        f"expected={expected_ids} actual={approved_ids}"
                    )
                for demoted_id in expected_ids:
                    conn.execute(
                        f"UPDATE {self._table_name} SET role = ?, updated_at = ? "
                        "WHERE model_id = ?",
                        [ModelRole.CHALLENGER.value, timestamp, demoted_id],
                    )
                target_row = conn.execute(
                    f"SELECT {', '.join(_SELECT_COLUMNS)} "
                    f"FROM {self._table_name} WHERE model_id = ? LIMIT 1",
                    [normalized_target],
                ).fetchone()
                if target_row is None:
                    raise ValueError(f"model_id not found: {normalized_target}")
                target_record = _row_to_record(target_row)
                if target_record.lifecycle_state != ModelLifecycleState.APPROVED:
                    raise ValueError(
                        "champion role requires approved lifecycle state: "
                        f"{normalized_target}"
                    )
                conn.execute(
                    f"UPDATE {self._table_name} SET role = ?, updated_at = ?, "
                    "promoted_at = COALESCE(promoted_at, ?) WHERE model_id = ?",
                    [ModelRole.CHAMPION.value, timestamp, timestamp, normalized_target],
                )
                post_count_row = conn.execute(
                    f"SELECT COUNT(*) FROM {self._table_name} "
                    "WHERE role = ? AND lifecycle_state = ?",
                    [ModelRole.CHAMPION.value, ModelLifecycleState.APPROVED.value],
                ).fetchone()
                champion_count_after = (
                    0 if post_count_row is None else int(str(post_count_row[0]))
                )
                if champion_count_after != 1:
                    raise ModelRegistryCASConflictError(
                        "promotion produced invalid champion count: "
                        f"{champion_count_after}"
                    )
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            promoted = self.get_by_id(normalized_target)
            if promoted is None:
                raise ValueError(f"model_id not found after promotion: {normalized_target}")
            demoted = [
                record
                for record in (self.get_by_id(item) for item in expected_ids)
                if record is not None
            ]
            return promoted, demoted
        finally:
            conn.close()

    def _fetch_records(
        self,
        conn: _DuckConnection,
        *,
        where_clause: str = "",
        parameters: Sequence[object] | None = None,
        order_clause: str = "",
    ) -> list[ModelRegistryRecord]:
        # 先按新 schema 查询；旧库缺列（未迁移）时降级到旧列集，绝不触发写迁移。
        query_parameters = list(parameters or [])
        for columns in (_SELECT_COLUMNS, _LEGACY_SELECT_COLUMNS):
            query = (
                f"SELECT {', '.join(columns)} "
                f"FROM {self._table_name}{where_clause}{order_clause}"
            )
            try:
                rows = conn.execute(query, query_parameters).fetchall()
            except Exception as exc:
                if columns is _SELECT_COLUMNS and self._is_missing_column_error(exc):
                    continue
                raise
            return [_row_to_record(row) for row in rows]
        return []

    def _replace_record(self, record: ModelRegistryRecord) -> ModelRegistryRecord:
        _validate_lifecycle_requirements(record)
        conn = self._connection_factory(str(self._db_path))
        try:
            self._ensure_table(conn=conn)
            self._schema_ready = True
            conn.execute(f"DELETE FROM {self._table_name} WHERE model_id = ?", [record.model_id])
            conn.execute(
                (
                    f"INSERT INTO {self._table_name} ("
                    "model_id, schema_version, role, lifecycle_state, parent_model_id, "
                    "artifact_uri, artifact_created_at, dataset_manifest_id, feature_schema_id, "
                    "feature_schema_hash, label_policy_id, label_policy_hash, "
                    "artifact_content_hash, metrics_summary_json, blocked_reason, "
                    "created_at, updated_at, promoted_at, revoked_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                _record_parameters(record),
            )
            return record
        finally:
            conn.close()

    def _ensure_table(self, conn: _DuckConnection) -> None:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._table_name} ("
            "model_id VARCHAR PRIMARY KEY, "
            "schema_version VARCHAR NOT NULL, "
            "role VARCHAR NOT NULL, "
            "lifecycle_state VARCHAR NOT NULL, "
            "parent_model_id VARCHAR NOT NULL, "
            "artifact_uri VARCHAR NOT NULL, "
            "artifact_created_at VARCHAR, "
            "dataset_manifest_id VARCHAR NOT NULL, "
            "feature_schema_id VARCHAR NOT NULL, "
            "feature_schema_hash VARCHAR NOT NULL, "
            "label_policy_id VARCHAR NOT NULL, "
            "label_policy_hash VARCHAR NOT NULL, "
            "artifact_content_hash VARCHAR NOT NULL DEFAULT '', "
            "metrics_summary_json VARCHAR NOT NULL, "
            "blocked_reason VARCHAR NOT NULL, "
            "created_at VARCHAR NOT NULL, "
            "updated_at VARCHAR NOT NULL, "
            "promoted_at VARCHAR, "
            "revoked_at VARCHAR"
            ")"
        )
        self._migrate_table_columns(conn=conn)

    def _migrate_table_columns(self, conn: _DuckConnection) -> None:
        """逐列迁移：缺 artifact_content_hash 的旧表补列并回填空串（幂等）。"""

        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            [self._table_name],
        ).fetchall()
        existing_columns = {str(row[0]).strip().lower() for row in rows}
        if not existing_columns:
            return
        if "artifact_content_hash" not in existing_columns:
            # DuckDB 不支持 ALTER ADD COLUMN 带 NOT NULL/DEFAULT 约束，
            # 先加可空列，再统一回填空串（写入路径始终显式提供值）。
            conn.execute(
                f"ALTER TABLE {self._table_name} ADD COLUMN artifact_content_hash VARCHAR"
            )
        conn.execute(
            f"UPDATE {self._table_name} SET artifact_content_hash = '' "
            "WHERE artifact_content_hash IS NULL"
        )

    def _open_connection(self, *, read_only: bool = False) -> _DuckConnection:
        factory = cast(Callable[..., _DuckConnection], self._connection_factory)
        db_path = str(self._db_path)
        if not read_only:
            return factory(db_path)
        try:
            # Prefer the shared connection mode used by the rest of the learning-protocol
            # stores so DuckDB does not reject mixed-mode handles for the same database file.
            return factory(db_path)
        except Exception as shared_exc:
            try:
                return factory(db_path, read_only=True)
            except TypeError as exc:
                if "read_only" not in str(exc):
                    raise shared_exc from exc
                try:
                    return _default_connection_factory(db_path, read_only=True)
                except Exception as fallback_exc:
                    if self._is_incompatible_connection_config_error(fallback_exc):
                        raise shared_exc from fallback_exc
                    raise
            except Exception as exc:
                if self._is_incompatible_connection_config_error(exc):
                    raise shared_exc from exc
                raise

    def _open_read_connection(self) -> _DuckConnection | None:
        if not self._db_path.exists():
            return None
        attempts = len(_READ_LOCK_RETRY_DELAYS_SEC) + 1
        for attempt in range(attempts):
            try:
                return self._open_connection(read_only=True)
            except Exception as exc:
                if not self._is_retryable_read_error(exc) or attempt >= attempts - 1:
                    raise ModelRegistryReadError(str(exc)) from exc
                time.sleep(_READ_LOCK_RETRY_DELAYS_SEC[attempt])
        return None

    def _is_retryable_read_error(self, exc: Exception) -> bool:
        message = str(exc).strip().lower()
        return any(marker in message for marker in _READ_LOCK_ERROR_MARKERS)

    def _is_missing_table_error(self, exc: Exception) -> bool:
        message = str(exc).strip().lower()
        return (
            "catalog error" in message
            and self._table_name.strip().lower() in message
            and "does not exist" in message
        )

    def _is_missing_column_error(self, exc: Exception) -> bool:
        # DuckDB 两种典型文案：
        # - Binder Error: Referenced column "x" not found in FROM clause!
        # - Catalog Error: Column "x" does not exist!
        message = str(exc).strip().lower()
        return (
            "artifact_content_hash" in message
            and ("not found" in message or "does not exist" in message)
        )

    def _is_incompatible_connection_config_error(self, exc: Exception) -> bool:
        message = str(exc).strip().lower()
        return "same database file with a different configuration" in message


_SELECT_COLUMNS = (
    "model_id",
    "schema_version",
    "role",
    "lifecycle_state",
    "parent_model_id",
    "artifact_uri",
    "artifact_created_at",
    "dataset_manifest_id",
    "feature_schema_id",
    "feature_schema_hash",
    "label_policy_id",
    "label_policy_hash",
    "artifact_content_hash",
    "metrics_summary_json",
    "blocked_reason",
    "created_at",
    "updated_at",
    "promoted_at",
    "revoked_at",
)

# 未迁移旧表的降级列集（无 artifact_content_hash，读取时映射为空串）。
_LEGACY_SELECT_COLUMNS = (
    "model_id",
    "schema_version",
    "role",
    "lifecycle_state",
    "parent_model_id",
    "artifact_uri",
    "artifact_created_at",
    "dataset_manifest_id",
    "feature_schema_id",
    "feature_schema_hash",
    "label_policy_id",
    "label_policy_hash",
    "metrics_summary_json",
    "blocked_reason",
    "created_at",
    "updated_at",
    "promoted_at",
    "revoked_at",
)

_APPROVAL_REQUIRED_FIELDS = (
    "dataset_manifest_id",
    "feature_schema_id",
    "feature_schema_hash",
    "label_policy_id",
    "label_policy_hash",
)

_ALLOWED_LIFECYCLE_TRANSITIONS: dict[ModelLifecycleState, set[ModelLifecycleState]] = {
    ModelLifecycleState.REGISTERED: {
        ModelLifecycleState.TRAINED,
        ModelLifecycleState.BLOCKED,
        ModelLifecycleState.REVOKED,
    },
    ModelLifecycleState.TRAINED: {
        ModelLifecycleState.SHADOW_VALIDATED,
        ModelLifecycleState.BLOCKED,
        ModelLifecycleState.REVOKED,
    },
    ModelLifecycleState.SHADOW_VALIDATED: {
        ModelLifecycleState.APPROVED,
        ModelLifecycleState.BLOCKED,
        ModelLifecycleState.REVOKED,
    },
    ModelLifecycleState.APPROVED: {
        ModelLifecycleState.BLOCKED,
        ModelLifecycleState.REVOKED,
    },
    ModelLifecycleState.BLOCKED: {
        ModelLifecycleState.TRAINED,
        ModelLifecycleState.SHADOW_VALIDATED,
        ModelLifecycleState.REVOKED,
    },
    ModelLifecycleState.REVOKED: set(),
}


def _record_parameters(record: ModelRegistryRecord) -> list[object]:
    return [
        record.model_id,
        record.schema_version,
        record.role.value,
        record.lifecycle_state.value,
        record.parent_model_id,
        record.artifact_uri,
        _dump_optional_datetime(record.artifact_created_at),
        record.dataset_manifest_id,
        record.feature_schema_id,
        record.feature_schema_hash,
        record.label_policy_id,
        record.label_policy_hash,
        record.artifact_content_hash,
        json.dumps(record.metrics_summary, ensure_ascii=True, sort_keys=True),
        record.blocked_reason,
        _dump_datetime(record.created_at),
        _dump_datetime(record.updated_at),
        _dump_optional_datetime(record.promoted_at),
        _dump_optional_datetime(record.revoked_at),
    ]


def _row_to_record(row: Sequence[object]) -> ModelRegistryRecord:
    # 兼容两种行宽：19 列（含 artifact_content_hash）/ 18 列（旧表降级查询）。
    has_content_hash_column = len(row) >= 19
    if has_content_hash_column:
        artifact_content_hash = str(row[12] or "").strip()
        metrics_index, tail_start = 13, 14
    else:
        artifact_content_hash = ""
        metrics_index, tail_start = 12, 13
    metrics_summary = _load_json_dict_float(row[metrics_index])
    return ModelRegistryRecord(
        model_id=str(row[0]),
        schema_version=str(row[1]),
        role=ModelRole(str(row[2])),
        lifecycle_state=ModelLifecycleState(str(row[3])),
        parent_model_id=str(row[4]),
        artifact_uri=str(row[5]),
        artifact_created_at=_parse_optional_datetime(row[6]),
        dataset_manifest_id=str(row[7]),
        feature_schema_id=str(row[8]),
        feature_schema_hash=str(row[9]),
        label_policy_id=str(row[10]),
        label_policy_hash=str(row[11]),
        artifact_content_hash=artifact_content_hash,
        metrics_summary=metrics_summary,
        blocked_reason=str(row[tail_start]),
        created_at=_parse_datetime(row[tail_start + 1]),
        updated_at=_parse_datetime(row[tail_start + 2]),
        promoted_at=_parse_optional_datetime(row[tail_start + 3]),
        revoked_at=_parse_optional_datetime(row[tail_start + 4]),
    )


def _transition_record(
    *,
    current: ModelRegistryRecord,
    lifecycle_state: ModelLifecycleState,
    blocked_reason: str,
    now: datetime,
) -> ModelRegistryRecord:
    if lifecycle_state == current.lifecycle_state:
        next_record = current.model_copy(update={"updated_at": now}, deep=True)
        _validate_lifecycle_requirements(next_record)
        return next_record
    allowed = _ALLOWED_LIFECYCLE_TRANSITIONS.get(current.lifecycle_state, set())
    if lifecycle_state not in allowed:
        raise ValueError(
            "illegal lifecycle transition: "
            f"{current.lifecycle_state.value} -> {lifecycle_state.value}"
        )

    updates: dict[str, object] = {
        "lifecycle_state": lifecycle_state,
        "updated_at": now,
        "blocked_reason": "",
    }
    normalized_blocked_reason = str(blocked_reason).strip()
    if lifecycle_state == ModelLifecycleState.BLOCKED:
        if not normalized_blocked_reason:
            raise ValueError("blocked transition requires blocked_reason")
        updates["blocked_reason"] = normalized_blocked_reason
    if lifecycle_state == ModelLifecycleState.APPROVED:
        updates["promoted_at"] = current.promoted_at or now
    if lifecycle_state == ModelLifecycleState.REVOKED:
        updates["revoked_at"] = current.revoked_at or now
    next_record = current.model_copy(update=updates, deep=True)
    _validate_lifecycle_requirements(next_record)
    return next_record


def _validate_lifecycle_requirements(record: ModelRegistryRecord) -> None:
    if record.lifecycle_state == ModelLifecycleState.BLOCKED and not record.blocked_reason:
        raise ValueError("blocked lifecycle requires blocked_reason")
    if (
        record.role == ModelRole.CHAMPION
        and record.lifecycle_state != ModelLifecycleState.APPROVED
    ):
        raise ValueError("champion role requires approved lifecycle state")
    if record.lifecycle_state == ModelLifecycleState.APPROVED:
        missing = [
            field_name
            for field_name in _APPROVAL_REQUIRED_FIELDS
            if not str(getattr(record, field_name)).strip()
        ]
        if missing:
            raise ValueError(
                "approved lifecycle requires protocol bindings: " + ", ".join(missing)
            )


def _build_model_id(*, artifact: ModelArtifact, artifact_uri: str) -> str:
    payload = {
        "artifact_uri": artifact_uri,
        "artifact_created_at": str(artifact.created_at).strip(),
        "dataset_manifest_id": artifact.dataset_manifest_id,
        "feature_schema_id": artifact.feature_schema_id,
        "feature_schema_hash": artifact.feature_schema_hash,
        "label_policy_id": artifact.label_policy_id,
        "label_policy_hash": artifact.label_policy_hash,
    }
    serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"model_v1_{digest[:12]}"


def _load_json_dict_float(value: object) -> dict[str, float]:
    if not isinstance(value, str):
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): float(item) for key, item in payload.items()}


def _dump_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC).isoformat()
    return value.astimezone(UTC).isoformat()


def _dump_optional_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _dump_datetime(value)


def _parse_datetime(value: object) -> datetime:
    text = str(value).strip()
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


_READ_LOCK_ERROR_MARKERS = (
    "could not set lock on file",
    "conflicting lock is held",
    "database is locked",
)

_READ_LOCK_RETRY_DELAYS_SEC = (0.05, 0.1, 0.2)


def _default_connection_factory(
    database: str,
    *,
    read_only: bool = False,
) -> _DuckConnection:
    duckdb_module = importlib.import_module("duckdb")
    connection = cast(
        _DuckConnection,
        duckdb_module.connect(database=database, read_only=read_only),
    )
    return connection
