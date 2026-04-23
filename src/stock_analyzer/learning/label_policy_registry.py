"""Registry for immutable label-policy contracts used by the learning protocol."""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from stock_analyzer._pydantic_compat import BaseModel, ConfigDict, Field
from stock_analyzer.config import LabelsConfig


class LabelPolicyRecord(BaseModel):
    """One immutable label-policy contract entry."""

    model_config = ConfigDict(extra="forbid")

    label_policy_id: str
    label_name: str
    schema_version: str = "1"
    take_profit_pct: float
    stop_loss_pct: float
    horizon_days: int
    price_basis: str
    exclude_untradable: bool
    conflict_policy: str
    conflict_soft_label_value: float
    maturity_rule: str = "label_mature_time_v1"
    label_policy_hash: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


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


def build_label_policy_record(
    *,
    label_name: str,
    take_profit_pct: float,
    stop_loss_pct: float,
    horizon_days: int,
    price_basis: str,
    exclude_untradable: bool,
    conflict_policy: str,
    conflict_soft_label_value: float,
    schema_version: str = "1",
    maturity_rule: str = "label_mature_time_v1",
    label_policy_id: str | None = None,
    created_at: datetime | None = None,
) -> LabelPolicyRecord:
    """Build one deterministic label-policy record."""

    normalized_label_name = label_name.strip()
    if not normalized_label_name:
        raise ValueError("label_name must not be empty")
    if int(horizon_days) <= 0:
        raise ValueError("horizon_days must be positive")
    payload = {
        "label_name": normalized_label_name,
        "schema_version": str(schema_version),
        "take_profit_pct": float(take_profit_pct),
        "stop_loss_pct": float(stop_loss_pct),
        "horizon_days": int(horizon_days),
        "price_basis": price_basis.strip(),
        "exclude_untradable": bool(exclude_untradable),
        "conflict_policy": conflict_policy.strip(),
        "conflict_soft_label_value": float(conflict_soft_label_value),
        "maturity_rule": maturity_rule.strip(),
    }
    label_policy_hash = _stable_hash(payload)
    resolved_policy_id = label_policy_id or _default_label_policy_id(
        schema_version=str(schema_version),
        label_policy_hash=label_policy_hash,
    )
    return LabelPolicyRecord(
        label_policy_id=resolved_policy_id,
        label_name=normalized_label_name,
        schema_version=str(schema_version),
        take_profit_pct=float(take_profit_pct),
        stop_loss_pct=float(stop_loss_pct),
        horizon_days=int(horizon_days),
        price_basis=price_basis.strip(),
        exclude_untradable=bool(exclude_untradable),
        conflict_policy=conflict_policy.strip(),
        conflict_soft_label_value=float(conflict_soft_label_value),
        maturity_rule=maturity_rule.strip() or "label_mature_time_v1",
        label_policy_hash=label_policy_hash,
        created_at=created_at or datetime.now(UTC),
    )


class LabelPolicyRegistry:
    """Persist immutable label-policy contracts in DuckDB."""

    def __init__(
        self,
        db_path: str | Path,
        table_name: str = "label_policy_registry",
        connection_factory: Callable[[str], _DuckConnection] | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._table_name = table_name
        self._connection_factory = connection_factory or _default_connection_factory
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    def register(self, record: LabelPolicyRecord) -> LabelPolicyRecord:
        """Persist one label-policy contract, deduping by policy hash."""

        conn = self._connection_factory(str(self._db_path))
        try:
            self._ensure_table(conn=conn)
            existing_by_hash = self._select_one(
                conn=conn,
                where_clause="label_policy_hash = ?",
                parameters=[record.label_policy_hash],
            )
            if existing_by_hash is not None:
                return existing_by_hash

            existing_by_id = self._select_one(
                conn=conn,
                where_clause="label_policy_id = ?",
                parameters=[record.label_policy_id],
            )
            if existing_by_id is not None:
                raise ValueError(
                    "label_policy_id already registered with a different policy contract: "
                    f"{record.label_policy_id}"
                )

            conn.execute(
                (
                    f"INSERT INTO {self._table_name} ("
                    "label_policy_id, label_name, schema_version, take_profit_pct, "
                    "stop_loss_pct, horizon_days, price_basis, exclude_untradable, "
                    "conflict_policy, conflict_soft_label_value, maturity_rule, "
                    "label_policy_hash, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                [
                    record.label_policy_id,
                    record.label_name,
                    record.schema_version,
                    record.take_profit_pct,
                    record.stop_loss_pct,
                    record.horizon_days,
                    record.price_basis,
                    record.exclude_untradable,
                    record.conflict_policy,
                    record.conflict_soft_label_value,
                    record.maturity_rule,
                    record.label_policy_hash,
                    record.created_at.astimezone(UTC).isoformat(),
                ],
            )
            return record
        finally:
            conn.close()

    def register_from_config(
        self,
        labels: LabelsConfig,
        *,
        schema_version: str = "1",
        maturity_rule: str = "label_mature_time_v1",
        label_policy_id: str | None = None,
        created_at: datetime | None = None,
    ) -> LabelPolicyRecord:
        """Register the active runtime label contract from LabelsConfig."""

        return self.register(
            build_label_policy_record(
                label_name=labels.primary,
                schema_version=schema_version,
                take_profit_pct=labels.take_profit_pct,
                stop_loss_pct=labels.stop_loss_pct,
                horizon_days=labels.horizon_days,
                price_basis=labels.pnl_price_basis,
                exclude_untradable=labels.exclude_untradable,
                conflict_policy=labels.conflict_policy,
                conflict_soft_label_value=labels.conflict_soft_label_value,
                maturity_rule=maturity_rule,
                label_policy_id=label_policy_id,
                created_at=created_at,
            )
        )

    def get_by_id(self, label_policy_id: str) -> LabelPolicyRecord | None:
        """Load one label policy by registry id."""

        conn = self._connection_factory(str(self._db_path))
        try:
            self._ensure_table(conn=conn)
            return self._select_one(
                conn=conn,
                where_clause="label_policy_id = ?",
                parameters=[label_policy_id],
            )
        finally:
            conn.close()

    def get_by_hash(self, label_policy_hash: str) -> LabelPolicyRecord | None:
        """Load one label policy by deterministic hash."""

        conn = self._connection_factory(str(self._db_path))
        try:
            self._ensure_table(conn=conn)
            return self._select_one(
                conn=conn,
                where_clause="label_policy_hash = ?",
                parameters=[label_policy_hash],
            )
        finally:
            conn.close()

    def list_records(self) -> list[LabelPolicyRecord]:
        """Return every registered label policy ordered by creation time."""

        conn = self._connection_factory(str(self._db_path))
        try:
            self._ensure_table(conn=conn)
            rows = conn.execute(
                f"SELECT {', '.join(_SELECT_COLUMNS)} "
                f"FROM {self._table_name} ORDER BY created_at, label_policy_id"
            ).fetchall()
            return [_row_to_record(row) for row in rows]
        finally:
            conn.close()

    def _ensure_table(self, conn: _DuckConnection) -> None:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._table_name} ("
            "label_policy_id VARCHAR PRIMARY KEY, "
            "label_name VARCHAR NOT NULL, "
            "schema_version VARCHAR NOT NULL, "
            "take_profit_pct DOUBLE NOT NULL, "
            "stop_loss_pct DOUBLE NOT NULL, "
            "horizon_days INTEGER NOT NULL, "
            "price_basis VARCHAR NOT NULL, "
            "exclude_untradable BOOLEAN NOT NULL, "
            "conflict_policy VARCHAR NOT NULL, "
            "conflict_soft_label_value DOUBLE NOT NULL, "
            "maturity_rule VARCHAR NOT NULL, "
            "label_policy_hash VARCHAR NOT NULL UNIQUE, "
            "created_at VARCHAR NOT NULL"
            ")"
        )

    def _select_one(
        self,
        *,
        conn: _DuckConnection,
        where_clause: str,
        parameters: Sequence[object],
    ) -> LabelPolicyRecord | None:
        row = conn.execute(
            f"SELECT {', '.join(_SELECT_COLUMNS)} "
            f"FROM {self._table_name} WHERE {where_clause} LIMIT 1",
            parameters,
        ).fetchone()
        if row is None:
            return None
        return _row_to_record(row)


_SELECT_COLUMNS = (
    "label_policy_id",
    "label_name",
    "schema_version",
    "take_profit_pct",
    "stop_loss_pct",
    "horizon_days",
    "price_basis",
    "exclude_untradable",
    "conflict_policy",
    "conflict_soft_label_value",
    "maturity_rule",
    "label_policy_hash",
    "created_at",
)


def _row_to_record(row: Sequence[object]) -> LabelPolicyRecord:
    return LabelPolicyRecord(
        label_policy_id=str(row[0]),
        label_name=str(row[1]),
        schema_version=str(row[2]),
        take_profit_pct=float(row[3]),
        stop_loss_pct=float(row[4]),
        horizon_days=int(row[5]),
        price_basis=str(row[6]),
        exclude_untradable=bool(row[7]),
        conflict_policy=str(row[8]),
        conflict_soft_label_value=float(row[9]),
        maturity_rule=str(row[10]),
        label_policy_hash=str(row[11]),
        created_at=_parse_datetime(row[12]),
    )


def _stable_hash(payload: Mapping[str, object]) -> str:
    serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _default_label_policy_id(*, schema_version: str, label_policy_hash: str) -> str:
    return f"label_policy_v{schema_version}_{label_policy_hash[:12]}"


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
