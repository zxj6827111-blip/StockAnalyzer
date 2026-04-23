from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from stock_analyzer.evolution.governance.compliance import (
    ComplianceEvent,
    ComplianceLogger,
    ComplianceState,
)


class _FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Sequence[object] | None]] = []
        self.closed = False

    def execute(self, query: str, parameters: Sequence[object] | None = None) -> object:
        self.calls.append((query, parameters))
        return object()

    def close(self) -> None:
        self.closed = True


def test_compliance_logger_writes_monthly_partition_table(tmp_path: Path) -> None:
    connections: list[_FakeConnection] = []

    def _factory(_: str) -> _FakeConnection:
        conn = _FakeConnection()
        connections.append(conn)
        return conn

    logger = ComplianceLogger(
        db_path=tmp_path / "compliance.duckdb",
        connection_factory=_factory,
    )
    table_name = logger.log_event(
        ComplianceEvent(
            trace_id="trace-1",
            proposal_id="prop-1",
            state=ComplianceState.GENERATED,
            active_champion_id="champ-1",
            symbol="600000.SH",
            event_time=datetime(2026, 3, 1, tzinfo=UTC),
            code_commit_id="git:abc123",
        )
    )

    assert table_name == "compliance_log_202603"
    assert len(connections) == 1
    calls = connections[0].calls
    assert any("CREATE TABLE IF NOT EXISTS compliance_log_202603" in sql for sql, _ in calls)
    insert_calls = [call for call in calls if "INSERT INTO compliance_log_202603" in call[0]]
    assert len(insert_calls) == 1
    assert connections[0].closed is True
