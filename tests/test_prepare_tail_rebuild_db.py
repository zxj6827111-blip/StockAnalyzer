from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import duckdb
import pytest

from scripts.prepare_tail_rebuild_db import materialize_subset


def test_materialize_subset_copies_only_requested_raw_symbol_window(tmp_path: Path) -> None:
    source = tmp_path / "source.duckdb"
    output = tmp_path / "subset.duckdb"
    requests = tmp_path / "requests.jsonl"
    requests.write_text(json.dumps({
        "symbol": "600000.SH",
        "decision_date": "2026-01-05",
        "entry_date": "2026-01-06",
    }) + "\n", encoding="utf-8")

    con = duckdb.connect(str(source))
    con.execute("""
        CREATE TABLE minute_bars_1min (
            symbol VARCHAR, trade_date DATE, bar_time TIMESTAMP,
            open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
            volume DOUBLE, amount DOUBLE, up_limit DOUBLE, down_limit DOUBLE,
            trade_status VARCHAR, price_basis VARCHAR, bar_time_semantics VARCHAR,
            source VARCHAR, ingested_at TIMESTAMP
        )
    """)
    con.execute("""
        INSERT INTO minute_bars_1min VALUES
            ('600000', DATE '2026-01-06', TIMESTAMP '2026-01-06 14:31:00',
             10, 10, 10, 10, 1, 10, NULL, NULL, NULL, 'raw', 'bar_end', 'test', NOW()),
            ('600000', DATE '2026-01-06', TIMESTAMP '2026-01-06 14:32:00',
             10, 10, 10, 10, 1, 10, NULL, NULL, NULL, 'qfq', 'bar_end', 'test', NOW()),
            ('000001', DATE '2026-01-06', TIMESTAMP '2026-01-06 14:30:00',
             10, 10, 10, 10, 1, 10, NULL, NULL, NULL, 'raw', 'bar_end', 'test', NOW()),
            ('600000', DATE '2026-01-06', TIMESTAMP '2026-01-06 14:30:00',
             10, 10, 10, 10, 1, 10, NULL, NULL, NULL, 'raw', 'bar_end', 'test', NOW()),
            ('600000', DATE '2025-12-31', TIMESTAMP '2025-12-31 14:30:00',
             10, 10, 10, 10, 1, 10, NULL, NULL, NULL, 'raw', 'bar_end', 'test', NOW())
    """)
    con.close()

    manifest = materialize_subset(source, requests, output)
    assert manifest["symbols"] == 1
    assert manifest["source_read_only"] is True
    assert manifest["tables"]["minute_bars_1min"] == 2
    assert manifest["physical_ordering"]["minute_bars_1min"] == "symbol, trade_date, bar_time"
    assert manifest["start"] == "2026-01-06"
    assert date.fromisoformat(manifest["end"]) > date(2026, 1, 6)

    con = duckdb.connect(str(output), read_only=True)
    assert con.execute(
        "SELECT symbol, price_basis, bar_time FROM minute_bars_1min"
    ).fetchall() == [
        ("600000", "raw", datetime(2026, 1, 6, 14, 30)),
        ("600000", "raw", datetime(2026, 1, 6, 14, 31)),
    ]
    con.close()
    with pytest.raises(FileExistsError):
        materialize_subset(source, requests, output)

    con = duckdb.connect(str(source), read_only=True)
    assert con.execute("SELECT COUNT(*) FROM minute_bars_1min").fetchone()[0] == 5
    con.close()
