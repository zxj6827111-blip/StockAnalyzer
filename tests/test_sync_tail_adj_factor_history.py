from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from scripts.sync_tail_adj_factor_history import SOURCE, sync_bundle
from stock_analyzer.research.tail_reference_store import TailReferenceStore


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _response_sha(rows: list[dict[str, object]]) -> str:
    frame = pd.DataFrame(rows, columns=["ts_code", "trade_date", "adj_factor"])
    frame["trade_date"] = frame["trade_date"].astype(str).str[:8]
    return hashlib.sha256(frame.to_csv(index=False, lineterminator="\n").encode()).hexdigest()


def _bundle(tmp_path: Path, *, complete: bool = True):
    symbols = tmp_path / "symbols.txt"
    symbols.write_text("000001.SZ\n600000.SH\n", encoding="utf-8")
    requests = tmp_path / "requests.jsonl"
    requests.write_text("{}\n", encoding="utf-8")
    scope = tmp_path / "scope.json"
    scope.write_text(
        json.dumps(
            {
                "schema": "tail_adj_factor_collection_manifest.v2",
                "collector": "scripts/collect_adj_factor_history.py",
                "request_sha256": _sha(requests),
                "symbols_sha256": hashlib.sha256(
                    b"000001\n600000"
                ).hexdigest(),
                "symbols": 2,
                "start": "20260105",
                "end": "20260106",
            }
        ),
        encoding="utf-8",
    )
    report = tmp_path / "collector_report.json"
    report.write_text(
        json.dumps(
            {
                "source": SOURCE,
                "scope": "requested_symbols_only",
                "requested_symbols": 2,
                "requested_sessions": 2,
                "completed_symbols": 2 if complete else 1,
                "coverage_complete": complete,
                "as_of": "2026-10-10T10:00:00+08:00",
                "trade_calendar": {"normalized_sha256": "a" * 64, "open_sessions": 2},
            }
        ),
        encoding="utf-8",
    )
    factor_rows = []
    for symbol, ts_code, flag in (("000001", "000001.SZ", complete), ("600000", "600000.SH", True)):
        # Mimic Tushare's newest-first symbol response order; response hashes are
        # order-sensitive and the importer must preserve it.
        days = ["20260106", "20260105"] if flag else ["20260105"]
        factor_rows.extend(
            {
                "symbol": symbol,
                "ts_code": ts_code,
                "trade_date": day,
                "adj_factor": 1.0,
                "source": SOURCE,
                "as_of": "2026-10-10T10:00:00+08:00",
                "coverage_complete": flag,
            }
            for day in days
        )
    factors = tmp_path / "factors.csv"
    pd.DataFrame(factor_rows).to_csv(factors, index=False)
    coverage = tmp_path / "coverage.csv"
    response_rows = {
        "000001": [row for row in factor_rows if row["symbol"] == "000001"],
        "600000": [row for row in factor_rows if row["symbol"] == "600000"],
    }
    pd.DataFrame(
        [
            {
                "symbol": "000001",
                "ts_code": "000001.SZ",
                "source": SOURCE,
                "as_of": "2026-10-10T10:00:00+08:00",
                "requested_sessions": 2,
                "rows_returned": len(response_rows["000001"]),
                "missing_sessions": "[]" if complete else '["20260106"]',
                "duplicate_dates": "[]",
                "out_of_range_dates": "[]",
                "coverage_complete": complete,
                "truncated": False,
                "error": "",
                "response_sha256": _response_sha(response_rows["000001"]),
            },
            {
                "symbol": "600000",
                "ts_code": "600000.SH",
                "source": SOURCE,
                "as_of": "2026-10-10T10:00:00+08:00",
                "requested_sessions": 2,
                "rows_returned": 2,
                "missing_sessions": "[]",
                "duplicate_dates": "[]",
                "out_of_range_dates": "[]",
                "coverage_complete": True,
                "truncated": False,
                "error": "",
                "response_sha256": _response_sha(response_rows["600000"]),
            },
        ]
    ).to_csv(coverage, index=False)
    report_payload = json.loads(report.read_text(encoding="utf-8"))
    report_payload["rows_total"] = len(factor_rows)
    report_payload["incomplete_symbols"] = {} if complete else {"000001": []}
    report.write_text(json.dumps(report_payload), encoding="utf-8")
    return symbols, requests, scope, report, coverage, factors


def _daily_batch_bundle(tmp_path: Path):
    symbols, requests, scope, report, coverage, factors = _bundle(tmp_path)
    days = ["20260105", "20260106"]
    as_of_by_day = {
        days[0]: "2026-10-10T10:00:01+08:00",
        days[1]: "2026-10-10T10:00:02+08:00",
    }
    factor_frame = pd.read_csv(
        factors,
        dtype={"symbol": "string", "ts_code": "string", "trade_date": "string"},
    )
    factor_frame["as_of"] = factor_frame["trade_date"].map(as_of_by_day)
    factor_frame.to_csv(factors, index=False)
    coverage_frame = pd.read_csv(
        coverage,
        dtype={"symbol": "string", "ts_code": "string", "as_of": "string"},
    )
    coverage_frame["as_of"] = as_of_by_day[days[-1]]
    coverage_frame.to_csv(coverage, index=False)
    response_rows = {
        symbol: factor_frame.loc[factor_frame["symbol"] == symbol].to_dict("records")
        for symbol in ("000001", "600000")
    }
    coverage_frame["response_sha256"] = coverage_frame["symbol"].map(
        lambda symbol: _response_sha(response_rows[str(symbol)])
    )
    coverage_frame.to_csv(coverage, index=False)
    report_payload = json.loads(report.read_text(encoding="utf-8"))
    report_payload.update(
        {
            "query_mode": "trade_date_market_batch",
            "as_of": None,
            "collected_at": "2026-10-10T10:00:03+08:00",
            "date_responses": {
                day: {
                    "as_of": as_of_by_day[day],
                    "rows_returned": 3000,
                    "unique_source_symbols": 3000,
                    "response_sha256": hashlib.sha256(day.encode()).hexdigest(),
                    "batch_complete": True,
                    "truncated": False,
                    "error": "",
                    "missing_requested_symbols": [],
                }
                for day in days
            },
        }
    )
    report_payload["trade_calendar"]["sessions"] = days
    report.write_text(json.dumps(report_payload), encoding="utf-8")
    return symbols, requests, scope, report, coverage, factors


def test_sync_imports_hash_bound_complete_factor_collection(tmp_path: Path) -> None:
    symbols, requests, scope, report, coverage, factors = _bundle(tmp_path)
    db = tmp_path / "research.duckdb"
    with TailReferenceStore(db):
        pass

    result = sync_bundle(
        db_path=db,
        factors_path=factors,
        coverage_path=coverage,
        report_path=report,
        scope_manifest_path=scope,
        symbols_path=symbols,
        requests_path=requests,
    )

    assert result["collection_complete"] is True
    assert result["requested_symbols"] == result["complete_symbols"] == 2
    assert result["factor_rows_written"] == 4
    con = __import__("duckdb").connect(str(db), read_only=True)
    try:
        rows = con.execute(
            "SELECT symbol, COUNT(*), BOOL_AND(coverage_complete), "
            "COUNT(DISTINCT response_sha256) FROM ref_adj_factor_history "
            "GROUP BY symbol ORDER BY symbol"
        ).fetchall()
        assert rows == [("000001", 2, True, 1), ("600000", 2, True, 1)]
    finally:
        con.close()


def test_sync_preserves_per_symbol_response_times_from_resumed_collection(tmp_path: Path) -> None:
    symbols, requests, scope, report, coverage, factors = _bundle(tmp_path)
    report_payload = json.loads(report.read_text(encoding="utf-8"))
    report_payload["as_of"] = None
    report_payload["collected_at"] = "2026-10-11T09:05:00+08:00"
    report.write_text(json.dumps(report_payload), encoding="utf-8")

    coverage_frame = pd.read_csv(coverage, dtype={"symbol": "string", "as_of": "string"})
    factor_frame = pd.read_csv(factors, dtype={"symbol": "string", "as_of": "string"})
    response_times = {
        "000001": "2026-10-10T10:00:00+08:00",
        "600000": "2026-10-11T09:00:00+08:00",
    }
    coverage_frame["as_of"] = coverage_frame["symbol"].map(response_times)
    factor_frame["as_of"] = factor_frame["symbol"].map(response_times)
    coverage_frame.to_csv(coverage, index=False)
    factor_frame.to_csv(factors, index=False)

    db = tmp_path / "research_resumed.duckdb"
    with TailReferenceStore(db):
        pass
    result = sync_bundle(
        db_path=db,
        factors_path=factors,
        coverage_path=coverage,
        report_path=report,
        scope_manifest_path=scope,
        symbols_path=symbols,
        requests_path=requests,
    )

    assert result["collection_complete"] is True
    con = __import__("duckdb").connect(str(db), read_only=True)
    try:
        assert con.execute(
            "SELECT symbol, MIN(as_of), MAX(as_of) "
            "FROM ref_adj_factor_history GROUP BY symbol ORDER BY symbol"
        ).fetchall() == [
            ("000001", response_times["000001"], response_times["000001"]),
            ("600000", response_times["600000"], response_times["600000"]),
        ]
    finally:
        con.close()


def test_sync_validates_trade_date_batch_times_and_imports_rows(tmp_path: Path) -> None:
    symbols, requests, scope, report, coverage, factors = _daily_batch_bundle(tmp_path)
    db = tmp_path / "research_daily_batch.duckdb"
    with TailReferenceStore(db):
        pass

    result = sync_bundle(
        db_path=db,
        factors_path=factors,
        coverage_path=coverage,
        report_path=report,
        scope_manifest_path=scope,
        symbols_path=symbols,
        requests_path=requests,
    )

    assert result["collection_complete"] is True
    assert result["factor_rows_written"] == 4
    con = __import__("duckdb").connect(str(db), read_only=True)
    try:
        rows = con.execute(
            "SELECT symbol, trade_date, as_of FROM ref_adj_factor_history "
            "ORDER BY symbol, trade_date"
        ).fetchall()
        rows = [(symbol, trade_day.isoformat(), as_of) for symbol, trade_day, as_of in rows]
        assert rows == [
            ("000001", "2026-01-05", "2026-10-10T10:00:01+08:00"),
            ("000001", "2026-01-06", "2026-10-10T10:00:02+08:00"),
            ("600000", "2026-01-05", "2026-10-10T10:00:01+08:00"),
            ("600000", "2026-01-06", "2026-10-10T10:00:02+08:00"),
        ]
    finally:
        con.close()


def test_sync_refuses_partial_collection_before_opening_database(tmp_path: Path) -> None:
    symbols, requests, scope, report, coverage, factors = _bundle(tmp_path, complete=False)
    db = tmp_path / "must_not_be_created.duckdb"
    db.touch()

    with pytest.raises(ValueError, match="factor collection is incomplete"):
        sync_bundle(
            db_path=db,
            factors_path=factors,
            coverage_path=coverage,
            report_path=report,
            scope_manifest_path=scope,
            symbols_path=symbols,
            requests_path=requests,
        )
    assert db.stat().st_size == 0


def test_sync_partial_range_keeps_returned_observations_valid(tmp_path: Path) -> None:
    symbols, requests, scope, report, coverage, factors = _bundle(tmp_path, complete=False)
    db = tmp_path / "research_partial.duckdb"
    with TailReferenceStore(db):
        pass

    result = sync_bundle(
        db_path=db,
        factors_path=factors,
        coverage_path=coverage,
        report_path=report,
        scope_manifest_path=scope,
        symbols_path=symbols,
        requests_path=requests,
        allow_partial=True,
    )

    assert result["collection_complete"] is False
    assert result["complete_symbols"] == 1
    assert result["complete_observations"] == 3
    con = __import__("duckdb").connect(str(db), read_only=True)
    try:
        rows = con.execute(
            "SELECT symbol, trade_date, coverage_complete "
            "FROM ref_adj_factor_history ORDER BY symbol, trade_date"
        ).fetchall()
        assert rows == [
            ("000001", date(2026, 1, 5), True),
            ("600000", date(2026, 1, 5), True),
            ("600000", date(2026, 1, 6), True),
        ]
    finally:
        con.close()


def test_resolves_missing_factor_prefix_before_verified_listing_date(tmp_path: Path) -> None:
    symbols, requests, scope, report, coverage, factors = _bundle(tmp_path, complete=False)
    factor_frame = pd.read_csv(
        factors, dtype={"symbol": "string", "ts_code": "string", "trade_date": "string"}
    )
    factor_frame.loc[factor_frame["symbol"] == "000001", "trade_date"] = "20260106"
    factor_frame.to_csv(factors, index=False)
    coverage_frame = pd.read_csv(coverage, dtype={"symbol": "string", "ts_code": "string"})
    prefix_rows = factor_frame.loc[factor_frame["symbol"] == "000001"].to_dict("records")
    coverage_frame.loc[coverage_frame["symbol"] == "000001", "missing_sessions"] = '["20260105"]'
    coverage_frame.loc[coverage_frame["symbol"] == "000001", "response_sha256"] = _response_sha(
        prefix_rows
    )
    coverage_frame.to_csv(coverage, index=False)
    db = tmp_path / "research_with_ipo.duckdb"
    with TailReferenceStore(db) as reference:
        reference.upsert_calendar(["2026-01-05", "2026-01-06"], as_of="2026-10-10")
        reference.upsert_security_status(
            pd.DataFrame(
                [
                    {
                        "symbol": "000001",
                        "effective_from": "2026-01-06",
                        "status_type": "listing_period",
                        "status_value": "Test Co",
                        "source": "tushare_stock_basic_namechange",
                        "as_of": "2025-12-31",
                        "coverage_complete": True,
                    }
                ]
            ),
            as_of="2026-10-10",
        )

    result = sync_bundle(
        db_path=db,
        factors_path=factors,
        coverage_path=coverage,
        report_path=report,
        scope_manifest_path=scope,
        symbols_path=symbols,
        requests_path=requests,
    )

    assert result["collection_complete"] is True
    assert result["complete_symbols"] == 2
    assert result["listing_prefix_coverage_resolutions"] == {"000001": "2026-01-06"}
    con = __import__("duckdb").connect(str(db), read_only=True)
    try:
        assert con.execute(
            "SELECT coverage_complete FROM ref_adj_factor_history WHERE symbol='000001'"
        ).fetchone() == (True,)
    finally:
        con.close()


def test_sync_rejects_modified_factor_csv_before_database_write(tmp_path: Path) -> None:
    symbols, requests, scope, report, coverage, factors = _bundle(tmp_path)
    frame = pd.read_csv(factors, dtype={"symbol": "string", "ts_code": "string"})
    frame.loc[0, "ts_code"] = "600000.SH"
    frame.to_csv(factors, index=False)
    db = tmp_path / "research.duckdb"
    db.touch()

    with pytest.raises(ValueError, match="factor symbol and ts_code disagree"):
        sync_bundle(
            db_path=db,
            factors_path=factors,
            coverage_path=coverage,
            report_path=report,
            scope_manifest_path=scope,
            symbols_path=symbols,
            requests_path=requests,
        )
