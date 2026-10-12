from __future__ import annotations

import csv
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pytest

from scripts.audit_tail_pool_adj_factor_coverage import audit_factor_coverage
from stock_analyzer.contracts.trend_strategy import DEFAULT_TREND_CONTRACT
from stock_analyzer.research.tail_rebuild import exit_window_end


def _seed_databases(tmp_path: Path, *, include_all_symbols: bool = False):
    reference = tmp_path / "reference.duckdb"
    factors = tmp_path / "factors.duckdb"
    start = date(2026, 1, 1)
    end = exit_window_end(date(2026, 3, 16), DEFAULT_TREND_CONTRACT) + timedelta(days=20)
    sessions = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            sessions.append(day)
        day += timedelta(days=1)

    ref = duckdb.connect(str(reference))
    ref.execute(
        "CREATE TABLE ref_trade_calendar "
        "(trade_date DATE, is_open BOOLEAN, exchange VARCHAR)"
    )
    ref.executemany(
        "INSERT INTO ref_trade_calendar VALUES (?, true, 'SSE')",
        [(session,) for session in sessions],
    )
    ref.execute(
        "CREATE TABLE ref_security_status "
        "(symbol VARCHAR, effective_from DATE, status_type VARCHAR, coverage_complete BOOLEAN)"
    )
    ref.executemany(
        "INSERT INTO ref_security_status VALUES (?, ?, 'listing_period', true)",
        [
            ("000001", date(2020, 1, 1)),
            ("000002", date(2026, 2, 10)),
            ("000003", date(2020, 1, 1)),
        ],
    )
    ref.close()

    factor = duckdb.connect(str(factors))
    factor.execute(
        "CREATE TABLE ref_adj_factor_history "
        "(symbol VARCHAR, trade_date DATE, adj_factor DOUBLE, coverage_complete BOOLEAN)"
    )
    entries = []
    for symbol, listing in [("000001", date(2020, 1, 1)), ("000002", date(2026, 2, 10))]:
        if symbol == "000002" and not include_all_symbols:
            continue
        entries.extend(
            (symbol, session, 1.0, True)
            for session in sessions
            if session >= max(date(2026, 3, 2) - timedelta(days=31), listing)
        )
    factor.executemany(
        "INSERT INTO ref_adj_factor_history VALUES (?, ?, ?, ?)", entries
    )
    factor.close()
    return factors, reference, sessions


def _request_file(path: Path) -> Path:
    rows = [
        {"symbol": symbol, "entry_date": "2026-03-02"}
        for symbol in ("000001", "000002")
    ]
    rows.append({"symbol": "000003", "entry_date": "2026-03-16"})
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def test_audit_uses_listing_date_only_for_proven_prelisting_prefixes(tmp_path: Path) -> None:
    factor_db, reference_db, _sessions = _seed_databases(tmp_path, include_all_symbols=True)
    requests = _request_file(tmp_path / "requests.jsonl")

    report = audit_factor_coverage(
        factor_db=factor_db,
        reference_db=reference_db,
        cohorts=[("march", requests)],
        out_dir=tmp_path / "audit",
    )

    cohort = report["cohorts"]["march"]
    assert cohort["symbols"] == 3
    assert cohort["complete_symbols"] == 2
    assert cohort["incomplete_symbols"] == 1
    assert cohort["listing_date_unproven_symbols"] == 0
    details = (tmp_path / "audit" / "tail_pool_adj_factor_coverage_march.csv").read_text()
    assert "000002" in details
    assert "2026-02-10" in details
    rows = {
        row["symbol"]: row
        for row in csv.DictReader(details.splitlines())
    }
    assert rows["000001"]["factor_start_date"] == "2026-01-30"
    assert rows["000003"]["factor_start_date"] == "2026-02-13"
    missing = (tmp_path / "audit" / "tail_pool_adj_factor_missing_symbols_march.txt")
    assert missing.read_text() == "000003\n"
    bundle = report["collection_bundle"]
    assert bundle["symbols"] == 1
    assert bundle["request_rows"] == 1
    collection_requests = Path(bundle["requests_file"])
    assert json.loads(collection_requests.read_text().strip()) == {
        "symbol": "000003",
        "entry_date": "2026-03-16",
    }
    scope = json.loads(Path(bundle["scope_manifest_file"]).read_text())
    assert scope["request_sha256"] == hashlib.sha256(collection_requests.read_bytes()).hexdigest()
    normalized_symbols = "\n".join(
        sorted(Path(bundle["symbols_file"]).read_text().splitlines())
    ).encode()
    assert scope["symbols_sha256"] == hashlib.sha256(normalized_symbols).hexdigest()
    assert bundle["symbols_file_sha256"] == hashlib.sha256(
        Path(bundle["symbols_file"]).read_bytes()
    ).hexdigest()


def test_audit_refuses_to_overwrite_existing_evidence(tmp_path: Path) -> None:
    factor_db, reference_db, _sessions = _seed_databases(tmp_path)
    requests = _request_file(tmp_path / "requests.jsonl")
    out_dir = tmp_path / "audit"
    kwargs = {
        "factor_db": factor_db,
        "reference_db": reference_db,
        "cohorts": [("march", requests)],
        "out_dir": out_dir,
    }
    audit_factor_coverage(**kwargs)
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        audit_factor_coverage(**kwargs)


def test_audit_exempts_only_proven_suspended_no_trade_factor_gap(tmp_path: Path) -> None:
    factor_db, reference_db, _sessions = _seed_databases(tmp_path, include_all_symbols=True)
    con = duckdb.connect(str(factor_db))
    try:
        missing_day = date(2026, 2, 3)
        con.execute(
            "DELETE FROM ref_adj_factor_history WHERE symbol='000001' AND trade_date=?",
            [missing_day],
        )
        con.execute(
            "CREATE TABLE ref_suspend_status (symbol VARCHAR, trade_date DATE, "
            "suspended BOOLEAN, suspend_type VARCHAR, trade_status VARCHAR, "
            "source VARCHAR, as_of VARCHAR, coverage_complete BOOLEAN)"
        )
        con.execute(
            "INSERT INTO ref_suspend_status VALUES "
            "('000001', ?, true, 'S', 'suspended', 'tushare_suspend_d', '2026-10-11', true)",
            [missing_day],
        )
        con.execute("CREATE TABLE ref_daily_bars_raw (symbol VARCHAR, trade_date DATE)")
        con.execute("CREATE TABLE minute_bars_1min (symbol VARCHAR, trade_date DATE)")
        con.execute("CREATE TABLE ref_corporate_actions (symbol VARCHAR, ex_date DATE)")
        con.execute(
            "CREATE TABLE ref_corporate_action_coverage "
            "(ex_date DATE, source VARCHAR, coverage_complete BOOLEAN, "
            "truncated BOOLEAN, error VARCHAR)"
        )
        con.execute(
            "INSERT INTO ref_corporate_action_coverage VALUES "
            "(?, 'tushare_dividend_ex_date', true, false, '')",
            [missing_day],
        )
    finally:
        con.close()

    requests = tmp_path / "suspended_gap.jsonl"
    requests.write_text(
        json.dumps({"symbol": "000001", "entry_date": "2026-03-02"}) + "\n",
        encoding="utf-8",
    )
    report = audit_factor_coverage(
        factor_db=factor_db,
        reference_db=reference_db,
        cohorts=[("suspended", requests)],
        out_dir=tmp_path / "suspended_audit",
    )

    cohort = report["cohorts"]["suspended"]
    assert cohort["complete_symbols"] == 1
    assert cohort["incomplete_symbols"] == 0
    assert cohort["raw_missing_symbol_sessions"] == 1
    assert cohort["suspension_exempt_symbol_sessions"] == 1
    assert cohort["missing_symbol_sessions"] == 0
    rows = csv.DictReader(
        (tmp_path / "suspended_audit" / "tail_pool_adj_factor_coverage_suspended.csv")
        .read_text()
        .splitlines()
    )
    detail = next(rows)
    assert detail["missing_dates_json"] == '["2026-02-03"]'
    assert detail["suspension_exempt_dates_json"] == '["2026-02-03"]'
    assert detail["effective_missing_dates_json"] == "[]"
    assert report["collection_bundle"] is None
