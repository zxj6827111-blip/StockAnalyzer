#!/usr/bin/env python3
"""Import hash-verified exchange suspension evidence into a local research DuckDB."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.research.suspension_evidence import (  # noqa: E402
    SSE_OFFICIAL_SUSPENSION_SOURCE,
    is_verified_full_day_suspension,
)
from stock_analyzer.research.tail_reference_store import (  # noqa: E402
    REFERENCE_TABLES,
    TailReferenceError,
    TailReferenceStore,
)

SCHEMA = "tail_official_suspension_evidence.v1"
RANGE_SCHEMA = "tail_official_suspension_evidence.v2"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _day(value: Any) -> date:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid suspension date: {text!r}") from exc


def _read_manifest(manifest_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest_path = manifest_path.expanduser().resolve()
    if not manifest_path.is_file():
        raise ValueError("evidence manifest does not exist")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    schema = payload.get("schema")
    if schema not in {SCHEMA, RANGE_SCHEMA} or not str(payload.get("as_of") or "").strip():
        raise ValueError("unsupported or undated official suspension manifest")
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ValueError("official suspension manifest has no evidence rows")

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, date]] = set()
    base = manifest_path.parent.resolve()
    for raw in raw_rows:
        if not isinstance(raw, dict):
            raise ValueError("evidence row must be an object")
        symbol = str(raw.get("symbol") or "").strip()
        trade_date = _day(raw.get("trade_date"))
        document_date = _day(raw.get("document_date") or raw.get("trade_date"))
        period_start_value = raw.get("evidence_start_date")
        period_end_value = raw.get("evidence_end_date")
        if schema == RANGE_SCHEMA and (period_start_value is None or period_end_value is None):
            raise ValueError("range evidence requires both interval endpoints")
        if (period_start_value is None) != (period_end_value is None):
            raise ValueError("official suspension evidence has a partial date range")
        period_start = _day(period_start_value) if period_start_value is not None else trade_date
        period_end = _day(period_end_value) if period_end_value is not None else trade_date
        if period_start > trade_date or trade_date > period_end:
            raise ValueError("official suspension date is outside its evidence interval")
        if not re.fullmatch(r"\d{6}", symbol):
            raise ValueError("evidence symbol must be a six-digit code")
        if (symbol, trade_date) in seen:
            raise ValueError("duplicate official suspension symbol/date")
        seen.add((symbol, trade_date))
        if not (
            raw.get("suspended") is True
            and str(raw.get("suspend_type") or "").strip().upper() == "S"
            and str(raw.get("trade_status") or "").strip().lower() == "suspended"
            and raw.get("coverage_complete") is True
            and raw.get("full_day") is True
            and raw.get("source") == SSE_OFFICIAL_SUSPENSION_SOURCE
        ):
            raise ValueError("manifest row does not assert a verified full-day suspension")
        relative = Path(str(raw.get("document_path") or ""))
        if relative.is_absolute():
            raise ValueError("evidence document path must be relative to the manifest")
        document = (base / relative).resolve()
        if not document.is_relative_to(base) or not document.is_file():
            raise ValueError("evidence document is missing or escapes its manifest directory")
        expected_hash = str(raw.get("document_sha256") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash) or _sha256(document) != expected_hash:
            raise ValueError("official suspension document hash mismatch")
        evidence = {
            "suspended": True,
            "suspend_type": "S",
            "trade_status": "suspended",
            "coverage_complete": True,
            "source": SSE_OFFICIAL_SUSPENSION_SOURCE,
            "as_of": str(payload["as_of"]),
            "trade_date": trade_date.isoformat(),
            "evidence_url": str(raw.get("document_url") or "").strip(),
            "evidence_title": str(raw.get("document_title") or "").strip(),
            "evidence_document_date": document_date.isoformat(),
            "evidence_sha256": expected_hash,
            "evidence_full_day": True,
            "evidence_as_of": str(payload["as_of"]),
            "evidence_start_date": period_start.isoformat(),
            "evidence_end_date": period_end.isoformat(),
        }
        if not is_verified_full_day_suspension(evidence):
            raise ValueError("official source URL/title/date failed source validation")
        rows.append(
            {
                "symbol": symbol,
                "trade_date": trade_date,
                "suspended": True,
                "suspend_type": "S",
                "trade_status": "suspended",
                "coverage_complete": True,
                "source": SSE_OFFICIAL_SUSPENSION_SOURCE,
                "as_of": str(payload["as_of"]),
                "evidence_url": evidence["evidence_url"],
                "evidence_title": evidence["evidence_title"],
                "evidence_document_date": document_date,
                "evidence_sha256": expected_hash,
                "evidence_full_day": True,
                "evidence_start_date": period_start,
                "evidence_end_date": period_end,
                "document_path": str(document),
            }
        )
    return payload, rows


def sync_bundle(*, db_path: Path, manifest_path: Path) -> dict[str, Any]:
    db_path = db_path.expanduser().resolve()
    db_text = str(db_path).lower()
    if db_path.suffix.lower() != ".duckdb" or "market_warehouse" in db_path.name.lower():
        raise ValueError("target must be an explicitly named research .duckdb file")
    if db_text.startswith("/vol1/docker/stockanalyzer/"):
        raise ValueError("refusing to write inside the production NAS project")
    if not db_path.is_file():
        raise ValueError("research DuckDB target does not exist")
    manifest_path = manifest_path.expanduser().resolve()
    manifest, rows = _read_manifest(manifest_path)

    status_table = REFERENCE_TABLES["suspend_status"]
    proof_table = REFERENCE_TABLES["suspend_evidence"]
    as_of = str(manifest["as_of"])
    with TailReferenceStore(db_path) as reference:
        reference._con.execute("BEGIN TRANSACTION")
        try:
            for row in rows:
                existing = reference._con.execute(
                    f"SELECT suspended, suspend_type, trade_status, source, coverage_complete "
                    f"FROM {status_table} WHERE symbol = ? AND trade_date = ?",
                    [row["symbol"], row["trade_date"]],
                ).fetchone()
                if existing is not None and (
                    existing[3] != SSE_OFFICIAL_SUSPENSION_SOURCE
                    or existing[0] is not True
                    or str(existing[1] or "").upper() != "S"
                    or str(existing[2] or "").lower() != "suspended"
                    or existing[4] is not True
                ):
                    raise ValueError(
                        f"refusing to replace conflicting status source for "
                        f"{row['symbol']} {row['trade_date']}"
                    )
                proof = reference._con.execute(
                    f"SELECT evidence_url, evidence_title, evidence_document_date, "
                    f"evidence_sha256, evidence_full_day FROM {proof_table} "
                    "WHERE symbol = ? AND trade_date = ? AND source = ?",
                    [row["symbol"], row["trade_date"], SSE_OFFICIAL_SUSPENSION_SOURCE],
                ).fetchone()
                expected_proof = (
                    row["evidence_url"],
                    row["evidence_title"],
                    row["evidence_document_date"],
                    row["evidence_sha256"],
                    True,
                )
                if proof is not None and tuple(proof) != expected_proof:
                    raise ValueError(
                        f"refusing to replace conflicting official evidence for "
                        f"{row['symbol']} {row['trade_date']}"
                    )
            status_frame = pd.DataFrame(rows)[
                [
                    "symbol",
                    "trade_date",
                    "suspended",
                    "suspend_type",
                    "trade_status",
                    "coverage_complete",
                    "source",
                    "as_of",
                ]
            ]
            evidence_frame = pd.DataFrame(rows)[
                [
                    "symbol",
                    "trade_date",
                    "source",
                    "evidence_url",
                    "evidence_title",
                    "evidence_document_date",
                    "evidence_sha256",
                    "evidence_full_day",
                    "evidence_start_date",
                    "evidence_end_date",
                ]
            ]
            reference.upsert_official_suspension_evidence(evidence_frame, as_of=as_of)
            status_written = reference.upsert_suspend_status(status_frame, as_of=as_of)
            reference._con.execute("COMMIT")
        except Exception:
            reference._con.execute("ROLLBACK")
            raise

    return {
        "db": str(db_path),
        "source": SSE_OFFICIAL_SUSPENSION_SOURCE,
        "as_of": as_of,
        "rows_written": status_written,
        "evidence_rows": len(rows),
        "manifest_sha256": _sha256(manifest_path),
        "documents": [
            {"path": row["document_path"], "sha256": row["evidence_sha256"]} for row in rows
        ],
        "symbol_dates": [f"{row['symbol']}:{row['trade_date']}" for row in rows],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="existing local research DuckDB")
    parser.add_argument("--evidence", required=True, help="hash-bound evidence.json manifest")
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)
    try:
        report = sync_bundle(db_path=Path(args.db), manifest_path=Path(args.evidence))
    except (OSError, ValueError, TailReferenceError, json.JSONDecodeError) as exc:
        print(f"official suspension import failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 5
    if args.report:
        Path(args.report).expanduser().write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
