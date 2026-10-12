from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from scripts.reconcile_tail_corporate_actions import ReconciliationError, reconcile_events


def _write_fixture(tmp_path: Path, *, include_duplicate: bool = False) -> tuple[Path, Path, Path]:
    events_path = tmp_path / "events.csv"
    fields = [
        "ts_code",
        "end_date",
        "ann_date",
        "div_proc",
        "stk_div",
        "stk_bo_rate",
        "stk_co_rate",
        "cash_div",
        "cash_div_tax",
        "record_date",
        "ex_date",
        "pay_date",
        "div_listdate",
        "imp_ann_date",
        "source",
    ]
    canonical = {
        "ts_code": "688448.SH",
        "end_date": "20251231",
        "ann_date": "20260512",
        "div_proc": "实施",
        "stk_div": "0.35",
        "stk_bo_rate": "",
        "stk_co_rate": "0.35",
        "cash_div": "",
        "cash_div_tax": "",
        "record_date": "20260601",
        "ex_date": "20260602",
        "pay_date": "",
        "div_listdate": "20260602",
        "imp_ann_date": "20260526",
        "source": "tushare_dividend_ex_date",
    }
    rows = [canonical]
    if include_duplicate:
        rows.append({**canonical, "ann_date": "20260421", "cash_div": "0.0", "cash_div_tax": "0.0"})
    with events_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    pdf = tmp_path / "official.pdf"
    pdf.write_bytes(b"%PDF-1.4\nreviewed source fixture\n")
    manifest_path = tmp_path / "reconciliation.json"
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    manifest = {
        "schema_version": "official_corporate_action_reconciliation_v1",
        "reviewed_on": "2026-10-11",
        "source_events_sha256": hashlib.sha256(events_path.read_bytes()).hexdigest(),
        "reconciliations": [
            {
                "ts_code": "688448.SH",
                "end_date": "20251231",
                "ann_date": "20260512",
                "record_date": "20260601",
                "ex_date": "20260602",
                "vendor_event": {
                    "div_proc": "实施",
                    "source": "tushare_dividend_ex_date",
                    "cash_div": None,
                    "cash_div_tax": None,
                    "stk_div": 0.35,
                    "stk_bo_rate": None,
                    "stk_co_rate": 0.35,
                },
                "official_reconciliation": {
                    "implementation_status": "实施",
                    "cash_dividend_per_share_before_tax": 0.0,
                    "cash_dividend_per_share_after_tax": 0.0,
                    "stock_distribution_per_share": 0.35,
                    "bonus_shares_per_share": 0.0,
                    "transfer_shares_per_share": 0.35,
                    "verified_statement": "no cash; 0.35 share capital reserve transfer",
                },
                "official_documents": [
                    {
                        "file": "official.pdf",
                        "url": "https://static.cninfo.com.cn/finalpage/2026-05-26/test.PDF",
                        "sha256": sha,
                        "bytes": len(pdf.read_bytes()),
                        "document_date": "2026-05-26",
                        "title": "Official implementation notice",
                    }
                ],
            }
        ],
        "duplicate_disclosures": [],
    }
    if include_duplicate:
        manifest["duplicate_disclosures"] = [
            {
                "ts_code": "688448.SH",
                "end_date": "20251231",
                "ann_date": "20260421",
                "record_date": "20260601",
                "ex_date": "20260602",
                "canonical_ann_date": "20260512",
                "reason": "same implemented event alias",
            }
        ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return events_path, manifest_path, pdf


def test_official_reconciliation_fills_only_evidenced_nulls_and_keeps_source_values(
    tmp_path,
) -> None:
    events, manifest, _ = _write_fixture(tmp_path)
    output = tmp_path / "events_reconciled.csv"
    report = reconcile_events(events, manifest, output)
    row = next(csv.DictReader(output.open(encoding="utf-8", newline="")))
    assert report["reconciled_event_rows"] == 1
    assert row["cash_div"] == "0.0"
    assert row["cash_div_tax"] == "0.0"
    assert row["source_cash_div_before_reconciliation"] == ""
    assert row["record_source"] == "tushare_dividend_ex_date+cninfo_official"
    evidence = json.loads(row["official_reconciliation_evidence_json"])
    assert evidence["documents"][0]["sha256"]


def test_exact_duplicate_disclosure_alias_is_excluded_and_reported(tmp_path) -> None:
    events, manifest, _ = _write_fixture(tmp_path, include_duplicate=True)
    output = tmp_path / "events_reconciled.csv"
    report = reconcile_events(events, manifest, output)
    rows = list(csv.DictReader(output.open(encoding="utf-8", newline="")))
    assert len(rows) == 1
    assert rows[0]["ann_date"] == "20260512"
    assert len(report["duplicate_aliases_excluded"]) == 1
    assert report["duplicate_aliases_excluded"][0]["event_key"][2] == "20260421"


def test_official_reconciliation_rejects_tampered_pdf_hash(tmp_path) -> None:
    events, manifest, _ = _write_fixture(tmp_path)
    payload = json.loads(manifest.read_text())
    payload["reconciliations"][0]["official_documents"][0]["sha256"] = "0" * 64
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReconciliationError, match="SHA-256 mismatch"):
        reconcile_events(events, manifest, tmp_path / "out.csv")


def test_official_reconciliation_rejects_document_dated_after_ex_date(tmp_path) -> None:
    events, manifest, _ = _write_fixture(tmp_path)
    payload = json.loads(manifest.read_text())
    payload["reconciliations"][0]["official_documents"][0]["document_date"] = "2026-06-03"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReconciliationError, match="no later than ex-date"):
        reconcile_events(events, manifest, tmp_path / "out.csv")


def test_official_reconciliation_rejects_malformed_document_date(tmp_path) -> None:
    events, manifest, _ = _write_fixture(tmp_path)
    payload = json.loads(manifest.read_text())
    payload["reconciliations"][0]["official_documents"][0]["document_date"] = "2026-02-30"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReconciliationError, match="valid calendar date"):
        reconcile_events(events, manifest, tmp_path / "out.csv")


def test_official_reconciliation_rejects_nonzero_vendor_cash_conflict(tmp_path) -> None:
    events, manifest, _ = _write_fixture(tmp_path)
    rows = list(csv.DictReader(events.open(encoding="utf-8", newline="")))
    rows[0]["cash_div"] = "0.01"
    fields = list(rows[0])
    with events.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    payload = json.loads(manifest.read_text())
    payload["source_events_sha256"] = hashlib.sha256(events.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReconciliationError, match="cash_div does not match"):
        reconcile_events(events, manifest, tmp_path / "out.csv")
