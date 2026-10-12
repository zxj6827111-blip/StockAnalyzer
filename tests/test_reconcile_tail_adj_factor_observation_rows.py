from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from scripts.reconcile_tail_adj_factor_observation_rows import reconcile_observations


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _response_sha(rows: list[dict[str, str]]) -> str:
    frame = pd.DataFrame(rows, columns=["ts_code", "trade_date", "adj_factor"])
    return hashlib.sha256(frame.to_csv(index=False, lineterminator="\n").encode()).hexdigest()


def _inputs(tmp_path: Path):
    symbols = tmp_path / "symbols.txt"
    symbols.write_text("000001.SZ\n600000.SH\n", encoding="utf-8")
    requests = tmp_path / "requests.jsonl"
    requests.write_text('{"symbol":"000001","entry_date":"2026-01-06"}\n', encoding="utf-8")
    scope = tmp_path / "scope.json"
    scope.write_text(
        json.dumps(
            {
                "schema": "tail_adj_factor_collection_manifest.v2",
                "collector": "scripts/collect_adj_factor_history.py",
                "request_sha256": _sha(requests),
                # Legacy audit producer hashed the newline-terminated file bytes.
                "symbols_sha256": _sha(symbols),
                "symbols": 2,
                "start": "20260105",
                "end": "20260106",
            }
        ),
        encoding="utf-8",
    )
    factors = tmp_path / "factors.csv"
    factor_rows = [
        {
            "symbol": "000001",
            "ts_code": "000001.SZ",
            "trade_date": "20260105",
            "adj_factor": "1.0",
            "source": "tushare_adj_factor_history",
            "as_of": "2026-10-11T09:00:00+00:00",
            "coverage_complete": "False",
        },
        {
            "symbol": "600000",
            "ts_code": "600000.SH",
            "trade_date": "20260106",
            "adj_factor": "1.01",
            "source": "tushare_adj_factor_history",
            "as_of": "2026-10-11T09:00:01+00:00",
            "coverage_complete": "True",
        },
        {
            "symbol": "600000",
            "ts_code": "600000.SH",
            "trade_date": "20260105",
            "adj_factor": "1.0",
            "source": "tushare_adj_factor_history",
            "as_of": "2026-10-11T09:00:01+00:00",
            "coverage_complete": "True",
        },
    ]
    pd.DataFrame(factor_rows).to_csv(factors, index=False)
    coverage = tmp_path / "coverage.csv"
    coverage_rows = [
        {
            "symbol": "000001",
            "ts_code": "000001.SZ",
            "source": "tushare_adj_factor_history",
            "as_of": "2026-10-11T09:00:00+00:00",
            "requested_sessions": 2,
            "rows_returned": 1,
            "missing_sessions": '["20260106"]',
            "duplicate_dates": "[]",
            "out_of_range_dates": "[]",
            "coverage_complete": False,
            "truncated": False,
            "error": "",
            "response_sha256": _response_sha([factor_rows[0]]),
        },
        {
            "symbol": "600000",
            "ts_code": "600000.SH",
            "source": "tushare_adj_factor_history",
            "as_of": "2026-10-11T09:00:01+00:00",
            "requested_sessions": 2,
            "rows_returned": 2,
            "missing_sessions": "[]",
            "duplicate_dates": "[]",
            "out_of_range_dates": "[]",
            "coverage_complete": True,
            "truncated": False,
            "error": "",
            "response_sha256": _response_sha([factor_rows[1], factor_rows[2]]),
        },
    ]
    pd.DataFrame(coverage_rows).to_csv(coverage, index=False)
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "source": "tushare_adj_factor_history",
                "scope": "requested_symbols_only",
                "query_mode": "symbol",
                "requested_symbols": 2,
                "requested_sessions": 2,
                "rows_total": 3,
                "completed_symbols": 1,
                "coverage_complete": False,
                "symbols_failed": {},
                "symbols_at_or_above_row_cap": [],
                "symbols_with_invalid_factor_values": {},
                "symbols_with_unexpected_source_codes": {},
                "incomplete_symbols": {"000001": ["20260106"]},
            }
        ),
        encoding="utf-8",
    )
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "schema": "tail_adj_factor_missing7_collection_evidence.v1",
                "scope_manifest_sha256": _sha(scope),
                "collector_sha256": "a" * 64,
                "collector_build_commit": "abc123",
                "collector_image_id": "sha256:" + "b" * 64,
                "inputs": {
                    "symbols": {"sha256": _sha(symbols)},
                    "requests": {"sha256": _sha(requests)},
                    "scope": {"sha256": _sha(scope)},
                },
                "outputs": {
                    "factors.csv": {"sha256": _sha(factors)},
                    "coverage.csv": {"sha256": _sha(coverage)},
                    "report.json": {"sha256": _sha(report)},
                },
            }
        ),
        encoding="utf-8",
    )
    db = tmp_path / "research.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE ref_adj_factor_history (symbol VARCHAR, trade_date DATE, "
        "adj_factor DOUBLE, source VARCHAR, as_of VARCHAR, coverage_complete BOOLEAN, "
        "response_sha256 VARCHAR, ingested_at TIMESTAMP)"
    )
    con.executemany(
        "INSERT INTO ref_adj_factor_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                row["symbol"],
                date(2026, 1, 5 if row["trade_date"] == "20260105" else 6),
                float(row["adj_factor"]),
                row["source"],
                row["as_of"],
                row["coverage_complete"] == "True",
                None,
                datetime(2026, 10, 11),
            )
            for row in factor_rows
        ],
    )
    con.close()
    return db, factors, coverage, report, evidence, symbols, requests, scope


def test_reconcile_keeps_verified_rows_and_preserves_missing_dates(tmp_path: Path) -> None:
    db, factors, coverage, report, evidence, symbols, requests, scope = _inputs(tmp_path)
    output = tmp_path / "reconciliation.json"

    result = reconcile_observations(
        target_db=db,
        factors_path=factors,
        coverage_path=coverage,
        report_path=report,
        evidence_manifest_path=evidence,
        symbols_path=symbols,
        requests_path=requests,
        scope_manifest_path=scope,
        out_manifest_path=output,
    )

    assert result["row_level_complete_observations"] == 3
    assert result["range_level_incomplete_symbols"] == ["000001"]
    assert result["missing_source_sessions"] == {"000001": ["20260106"]}
    assert result["scope_symbol_hash_legacy_encoding"] is True
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute(
            "SELECT symbol, trade_date, coverage_complete FROM ref_adj_factor_history "
            "ORDER BY symbol, trade_date"
        ).fetchall() == [
            ("000001", date(2026, 1, 5), True),
            ("600000", date(2026, 1, 5), True),
            ("600000", date(2026, 1, 6), True),
        ]
        assert (
            con.execute(
                "SELECT COUNT(*) FROM ref_adj_factor_history "
                "WHERE symbol='000001' AND trade_date=date '2026-01-06'"
            ).fetchone()[0]
            == 0
        )
    finally:
        con.close()
    assert output.is_file()


def test_reconcile_rejects_changed_source_rows_before_database_write(tmp_path: Path) -> None:
    db, factors, coverage, report, evidence, symbols, requests, scope = _inputs(tmp_path)
    original = factors.read_text(encoding="utf-8")
    factors.write_text(original.replace(",1.0,", ",1.5,"), encoding="utf-8")
    before = _sha(db)

    with pytest.raises(ValueError, match="factors file differs from collector evidence"):
        reconcile_observations(
            target_db=db,
            factors_path=factors,
            coverage_path=coverage,
            report_path=report,
            evidence_manifest_path=evidence,
            symbols_path=symbols,
            requests_path=requests,
            scope_manifest_path=scope,
            out_manifest_path=tmp_path / "reconciliation.json",
        )
    assert _sha(db) == before
