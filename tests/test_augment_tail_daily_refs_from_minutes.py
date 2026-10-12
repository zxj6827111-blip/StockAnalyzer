from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

import duckdb

from scripts.augment_tail_daily_refs_from_minutes import augment_database


def _minute_times() -> list[datetime]:
    values = [datetime.combine(date(2026, 1, 5), time(9, 30))]
    values.extend(datetime.combine(date(2026, 1, 5), time(9, 30)) + timedelta(minutes=i)
                  for i in range(1, 121))
    values.extend(datetime.combine(date(2026, 1, 5), time(13, 0)) + timedelta(minutes=i)
                  for i in range(1, 121))
    return values


def _write_source(path: Path) -> None:
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE minute_bars_1min (
            symbol VARCHAR, trade_date DATE, bar_time TIMESTAMP, open DOUBLE, high DOUBLE,
            low DOUBLE, close DOUBLE, volume DOUBLE, amount DOUBLE, up_limit DOUBLE,
            down_limit DOUBLE, trade_status VARCHAR, price_basis VARCHAR,
            bar_time_semantics VARCHAR, source VARCHAR, ingested_at TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE ref_daily_bars_raw (
            symbol VARCHAR, trade_date DATE, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
            volume DOUBLE, turnover DOUBLE, float_market_cap DOUBLE, name VARCHAR,
            is_st BOOLEAN, is_delisting_risk BOOLEAN, board VARCHAR, price_basis VARCHAR,
            source VARCHAR, as_of VARCHAR, ingested_at TIMESTAMP,
            PRIMARY KEY(symbol, trade_date)
        )
    """)
    con.execute("""
        CREATE TABLE ref_suspend_status (
            symbol VARCHAR, trade_date DATE, suspended BOOLEAN, suspend_type VARCHAR,
            trade_status VARCHAR, source VARCHAR, as_of VARCHAR, coverage_complete BOOLEAN,
            ingested_at TIMESTAMP, PRIMARY KEY(symbol, trade_date)
        )
    """)
    con.execute("""
        CREATE TABLE ref_limit_prices (
            symbol VARCHAR, trade_date DATE, up_limit DOUBLE, down_limit DOUBLE,
            approximated BOOLEAN, source VARCHAR, as_of VARCHAR, coverage_complete BOOLEAN
        )
    """)
    con.execute("""
        CREATE TABLE ref_adj_factor_history (
            symbol VARCHAR, trade_date DATE, adj_factor DOUBLE, coverage_complete BOOLEAN
        )
    """)
    rows = [
        ("600000", date(2026, 1, 5), moment, 10.0, 10.1, 9.9, 10.0, 100.0,
         "raw", "bar_end", "vendor_zip")
        for moment in _minute_times()
    ]
    insert_minutes = """
        INSERT INTO minute_bars_1min VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?, CURRENT_TIMESTAMP
        )
    """
    con.executemany(insert_minutes, rows)
    # Full but zero-volume session must not imply normal trading.
    zero_rows = [tuple(["600001", *row[1:7], 0.0, *row[8:]]) for row in rows]
    con.executemany(insert_minutes, zero_rows)
    missing_factor_rows = [tuple(["600003", *row[1:]]) for row in rows]
    con.executemany(insert_minutes, missing_factor_rows)
    # A traded, complete session is still not enough if exact limits are missing.
    limit_rows = [
        ("600000", date(2026, 1, 5), 11.0, 9.0, False, "test", "test", True),
        ("600001", date(2026, 1, 5), 11.0, 9.0, False, "test", "test", True),
        ("600003", date(2026, 1, 5), 11.0, 9.0, False, "test", "test", True),
    ]
    con.executemany("INSERT INTO ref_limit_prices VALUES (?, ?, ?, ?, ?, ?, ?, ?)", limit_rows)
    factor_rows = [
        ("600000", date(2026, 1, 5), 1.0, True),
        ("600001", date(2026, 1, 5), 1.0, True),
    ]
    con.executemany("INSERT INTO ref_adj_factor_history VALUES (?, ?, ?, ?)", factor_rows)
    # This session marker has no minute session and must remain absent.
    con.execute(
        "INSERT INTO ref_limit_prices VALUES "
        "('600002', DATE '2026-01-05', 11, 9, FALSE, 'test', 'test', TRUE)"
    )
    con.execute("INSERT INTO ref_adj_factor_history VALUES ('600002', DATE '2026-01-05', 1, TRUE)")
    con.close()


def test_only_complete_traded_raw_sessions_create_null_price_markers(tmp_path: Path) -> None:
    source = tmp_path / "source.duckdb"
    output = tmp_path / "output.duckdb"
    _write_source(source)

    manifest = augment_database(source, output)

    assert manifest["source_read_only"] is True
    assert manifest["inserted_traded_session_markers"] == 1
    assert manifest["remaining_minute_keys_without_daily_reference"] == 2
    assert manifest["markers"][0]["symbol"] == "600000"
    assert manifest["markers"][0]["trade_date"] == "2026-01-05"
    json.dumps(manifest)
    con = duckdb.connect(str(output), read_only=True)
    row = con.execute(
        "SELECT open, high, low, close, volume, price_basis, source FROM ref_daily_bars_raw"
    ).fetchone()
    status = con.execute(
        "SELECT suspended, trade_status, coverage_complete FROM ref_suspend_status"
    ).fetchone()
    con.close()
    assert row == (None, None, None, None, 24100.0, "raw", "complete_raw_1m_traded_session_marker")
    assert status == (False, "normal", True)
    source_con = duckdb.connect(str(source), read_only=True)
    source_rows = source_con.execute("SELECT COUNT(*) FROM ref_daily_bars_raw").fetchone()[0]
    source_status_rows = source_con.execute("SELECT COUNT(*) FROM ref_suspend_status").fetchone()[0]
    source_con.close()
    assert source_rows == 0
    assert source_status_rows == 0


def test_existing_output_is_never_overwritten(tmp_path: Path) -> None:
    source = tmp_path / "source.duckdb"
    output = tmp_path / "output.duckdb"
    _write_source(source)
    output.write_bytes(b"existing evidence")

    try:
        augment_database(source, output)
    except FileExistsError:
        pass
    else:
        raise AssertionError("existing output must be preserved")
    assert output.read_bytes() == b"existing evidence"
