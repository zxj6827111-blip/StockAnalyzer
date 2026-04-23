"""Compliance logging with monthly DuckDB partitions."""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast

from stock_analyzer._pydantic_compat import BaseModel, ConfigDict, Field


class ComplianceState(StrEnum):
    """Proposal lifecycle state for audit logs."""

    GENERATED = "generated"
    VALIDATED = "validated"
    SHADOWING = "shadowing"
    APPROVED = "approved"
    PROMOTED = "promoted"
    ROLLED_BACK = "rolled_back"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    RETRY_PENDING = "retry_pending"


class ComplianceEvent(BaseModel):
    """Single compliance audit event."""

    model_config = ConfigDict(extra="forbid")

    trace_id: str
    proposal_id: str
    state: ComplianceState
    active_champion_id: str
    symbol: str
    event_time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    input_data_hash: str | None = None
    confidence_level: float | None = None
    llm_prompt_version: str | None = None
    code_commit_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class _DuckConnection(Protocol):
    def execute(
        self,
        query: str,
        parameters: Sequence[object] | None = None,
    ) -> object: ...

    def close(self) -> None: ...


class ComplianceLogger:
    """Write compliance events into monthly DuckDB tables."""

    def __init__(
        self,
        db_path: str | Path,
        table_prefix: str = "compliance_log",
        connection_factory: Callable[[str], _DuckConnection] | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._table_prefix = table_prefix
        self._connection_factory = connection_factory or _default_connection_factory
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    def log_event(self, event: ComplianceEvent) -> str:
        """Persist one compliance event and return monthly table name."""
        table_name = self._table_name(event.event_time)
        conn = self._connection_factory(str(self._db_path))
        try:
            self._ensure_table(conn=conn, table_name=table_name)
            conn.execute(
                (
                    f"INSERT INTO {table_name} "
                    "("
                    "trace_id, proposal_id, state, active_champion_id, symbol, "
                    "event_time, input_data_hash, confidence_level, llm_prompt_version, "
                    "code_commit_id, metadata_json"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                [
                    event.trace_id,
                    event.proposal_id,
                    event.state.value,
                    event.active_champion_id,
                    event.symbol,
                    event.event_time,
                    event.input_data_hash,
                    event.confidence_level,
                    event.llm_prompt_version,
                    event.code_commit_id,
                    json.dumps(event.metadata, ensure_ascii=True, sort_keys=True),
                ],
            )
        finally:
            conn.close()
        return table_name

    def _table_name(self, event_time: datetime) -> str:
        return f"{self._table_prefix}_{event_time.strftime('%Y%m')}"

    def _ensure_table(self, conn: _DuckConnection, table_name: str) -> None:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table_name} ("
            "trace_id VARCHAR NOT NULL, "
            "proposal_id VARCHAR NOT NULL, "
            "state VARCHAR NOT NULL, "
            "active_champion_id VARCHAR NOT NULL, "
            "symbol VARCHAR NOT NULL, "
            "event_time TIMESTAMP NOT NULL, "
            "input_data_hash VARCHAR, "
            "confidence_level DOUBLE, "
            "llm_prompt_version VARCHAR, "
            "code_commit_id VARCHAR, "
            "metadata_json VARCHAR"
            ")"
        )


def _default_connection_factory(database: str) -> _DuckConnection:
    duckdb_module = importlib.import_module("duckdb")
    connection = cast(_DuckConnection, duckdb_module.connect(database=database))
    return connection
