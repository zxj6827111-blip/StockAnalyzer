from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from scripts.sync_tail_official_suspensions import sync_bundle
from stock_analyzer.research.tail_reference_store import TailReferenceStore


def _bundle(tmp_path: Path) -> Path:
    document = tmp_path / "notice.pdf"
    document.write_bytes(b"test official SSE notice bytes")
    manifest = tmp_path / "evidence.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "tail_official_suspension_evidence.v1",
                "as_of": "2026-10-10T20:24:19+08:00",
                "rows": [
                    {
                        "symbol": "688005",
                        "trade_date": "2026-01-15",
                        "suspended": True,
                        "suspend_type": "S",
                        "trade_status": "suspended",
                        "coverage_complete": True,
                        "full_day": True,
                        "source": "sse_official_suspension_notice",
                        "document_path": "notice.pdf",
                        "document_url": "https://dataclouds.cninfo.com.cn/notice.pdf",
                        "document_title": "SSE trading suspension report",
                        "document_date": "2026-01-15",
                        "document_sha256": hashlib.sha256(document.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest


def test_sync_imports_hash_verified_official_full_day_suspension(tmp_path: Path) -> None:
    manifest = _bundle(tmp_path)
    db = tmp_path / "research.duckdb"
    with TailReferenceStore(db):
        pass

    result = sync_bundle(db_path=db, manifest_path=manifest)
    assert result["rows_written"] == result["evidence_rows"] == 1
    with TailReferenceStore(db) as reference:
        evidence = reference.verified_full_day_suspension("688005", date(2026, 1, 15))
    assert evidence is not None
    assert evidence["source"] == "sse_official_suspension_notice"
    assert evidence["evidence_title"] == "SSE trading suspension report"

    # Retry is idempotent and preserves the exact source proof.
    assert sync_bundle(db_path=db, manifest_path=manifest)["rows_written"] == 1


def test_sync_accepts_an_official_document_for_a_suspension_interval(tmp_path: Path) -> None:
    document = tmp_path / "resume_notice.pdf"
    document.write_bytes(b"official notice covering the suspension interval")
    manifest = tmp_path / "interval_evidence.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "tail_official_suspension_evidence.v2",
                "as_of": "2026-10-10T21:00:00+08:00",
                "rows": [
                    {
                        "symbol": "688766",
                        "trade_date": "2025-12-03",
                        "evidence_start_date": "2025-11-25",
                        "evidence_end_date": "2025-12-08",
                        "suspended": True,
                        "suspend_type": "S",
                        "trade_status": "suspended",
                        "coverage_complete": True,
                        "full_day": True,
                        "source": "sse_official_suspension_notice",
                        "document_path": "resume_notice.pdf",
                        "document_url": "https://dataclouds.cninfo.com.cn/resume_notice.pdf",
                        "document_title": "Official resumption notice",
                        "document_date": "2025-12-09",
                        "document_sha256": hashlib.sha256(document.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    db = tmp_path / "research.duckdb"
    with TailReferenceStore(db):
        pass

    result = sync_bundle(db_path=db, manifest_path=manifest)

    assert result["rows_written"] == result["evidence_rows"] == 1
    with TailReferenceStore(db) as reference:
        evidence = reference.verified_full_day_suspension("688766", date(2025, 12, 3))
        assert evidence is not None
        assert evidence["evidence_document_date"] == "2025-12-09"
        assert evidence["evidence_start_date"] == "2025-11-25"
        assert evidence["evidence_end_date"] == "2025-12-08"
        assert reference.verified_full_day_suspension("688766", date(2025, 12, 9)) is None


def test_sync_rejects_document_hash_mismatch_before_database_write(tmp_path: Path) -> None:
    manifest = _bundle(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["rows"][0]["document_sha256"] = "0" * 64
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    db = tmp_path / "research.duckdb"
    with TailReferenceStore(db):
        pass

    with pytest.raises(ValueError, match="document hash mismatch"):
        sync_bundle(db_path=db, manifest_path=manifest)
    with TailReferenceStore(db) as reference:
        assert reference._con.execute("SELECT COUNT(*) FROM ref_suspend_status").fetchone()[0] == 0


def test_sync_refuses_to_replace_other_suspension_source(tmp_path: Path) -> None:
    import pandas as pd

    manifest = _bundle(tmp_path)
    db = tmp_path / "research.duckdb"
    with TailReferenceStore(db) as reference:
        reference.upsert_suspend_status(
            pd.DataFrame(
                [
                    {
                        "symbol": "688005",
                        "trade_date": "2026-01-15",
                        "suspended": True,
                        "suspend_type": "S",
                        "trade_status": "suspended",
                        "coverage_complete": True,
                    }
                ]
            ),
            source="other_source",
            as_of="2026-10-10",
        )

    with pytest.raises(ValueError, match="conflicting status source"):
        sync_bundle(db_path=db, manifest_path=manifest)
    with TailReferenceStore(db) as reference:
        row = reference._con.execute(
            "SELECT source FROM ref_suspend_status WHERE symbol='688005'"
        ).fetchone()
    assert row == ("other_source",)
