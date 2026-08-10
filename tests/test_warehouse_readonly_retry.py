"""Tests for market warehouse read-only connection separation and lock retry."""

from __future__ import annotations

import duckdb

from stock_analyzer.data.market_warehouse import (
    _connect_with_lock_retry,
    _is_retryable_duckdb_lock_error,
    MarketWarehouse,
)


class _FakeLockError(Exception):
    pass


def _make_lock_error() -> _FakeLockError:
    return _FakeLockError("IO Error: could not set lock on file \"/tmp/market.duckdb\"")


def test_is_retryable_duckdb_lock_error_matches_lock_messages() -> None:
    assert _is_retryable_duckdb_lock_error(_make_lock_error()) is True
    assert (
        _is_retryable_duckdb_lock_error(
            Exception('Catalog Error: Table with name "bars" does not exist')
        )
        is False
    )
    assert (
        _is_retryable_duckdb_lock_error(Exception("conflicting lock is held"))
        is True
    )


def test_connect_with_lock_retry_succeeds_after_transient_lock_errors(monkeypatch) -> None:
    attempts: list[int] = []
    calls = {"count": 0}

    def flaky_connect():
        calls["count"] += 1
        attempts.append(calls["count"])
        if calls["count"] < 3:
            raise _make_lock_error()
        return "connected"

    result = _connect_with_lock_retry(flaky_connect)
    assert result == "connected"
    assert attempts == [1, 2, 3]


def test_connect_with_lock_retry_raises_after_exhausting_attempts(monkeypatch) -> None:
    calls = {"count": 0}

    def always_fail():
        calls["count"] += 1
        raise _make_lock_error()

    import pytest

    with pytest.raises(_FakeLockError):
        _connect_with_lock_retry(always_fail)
    assert calls["count"] == 5


def test_connect_with_lock_retry_does_not_retry_non_lock_errors() -> None:
    calls = {"count": 0}

    def non_lock_fail():
        calls["count"] += 1
        raise RuntimeError("some other error")

    import pytest

    with pytest.raises(RuntimeError):
        _connect_with_lock_retry(non_lock_fail)
    assert calls["count"] == 1


def test_readonly_connect_opens_existing_db_in_read_only_mode(tmp_path) -> None:
    db_path = tmp_path / "market.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.execute("INSERT INTO t VALUES (1), (2)")
    conn.close()

    warehouse = MarketWarehouse(db_path=str(db_path), package_root=str(tmp_path / "pkg"))
    with warehouse._connect_readonly() as read_conn:  # noqa: SLF001
        rows = read_conn.execute("SELECT COUNT(*) FROM t").fetchone()
        assert rows == (2,)
        # Writes must be rejected on a read-only connection.
        try:
            read_conn.execute("CREATE TABLE forbidden (id INTEGER)")
            raised = False
        except Exception:  # noqa: BLE001 - duckdb raises a generic error
            raised = True
        assert raised is True


def test_readonly_connect_absent_db_falls_back_to_legacy(tmp_path) -> None:
    db_path = tmp_path / "missing.duckdb"
    warehouse = MarketWarehouse(db_path=str(db_path), package_root=str(tmp_path / "pkg"))
    # Legacy behaviour: opening a non-existent DB creates an empty one.
    with warehouse._connect_readonly() as conn:  # noqa: SLF001
        result = conn.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 't'"
        ).fetchone()
        assert result == (0,)
    assert db_path.exists()
