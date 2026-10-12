#!/usr/bin/env python3
"""Reconcile explicitly reviewed Tushare corporate-action nulls with CNINFO filings.

Blank vendor values stay unknown by default. This command fills only actions listed in a
hash-bound review manifest whose official PDFs are present and match their recorded SHA-256.
It also reports and removes only explicitly listed duplicate disclosure aliases; all source
files remain untouched.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ALLOWED_HOSTS = {"static.cninfo.com.cn", "dataclouds.cninfo.com.cn"}
RECONCILED_SOURCE = "tushare_dividend_ex_date+cninfo_official"
EXTRA_COLUMNS = (
    "source_cash_div_before_reconciliation",
    "source_cash_div_tax_before_reconciliation",
    "source_stk_div_before_reconciliation",
    "source_stk_bo_rate_before_reconciliation",
    "source_stk_co_rate_before_reconciliation",
    "record_source",
    "official_reconciliation_evidence_json",
)


class ReconciliationError(ValueError):
    """Input evidence does not prove a unique, consistent action reconciliation."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _number(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ReconciliationError(f"not a numeric value: {value!r}") from exc
    if not math.isfinite(number):
        raise ReconciliationError(f"non-finite numeric value: {value!r}")
    return number


def _same_number(actual: Any, expected: Any, *, label: str) -> None:
    actual_number = _number(actual)
    expected_number = _number(expected)
    if actual_number is None or expected_number is None:
        if actual_number != expected_number:
            raise ReconciliationError(f"{label} does not match the review manifest")
    elif not math.isclose(actual_number, expected_number, rel_tol=0.0, abs_tol=1e-10):
        raise ReconciliationError(f"{label} does not match the review manifest")


def _calendar_date(value: Any, *, label: str) -> date:
    text = str(value or "").strip()
    try:
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date()
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ReconciliationError(f"{label} is not a valid calendar date: {text!r}") from exc
    if parsed.isoformat() != text:
        raise ReconciliationError(f"{label} must use YYYY-MM-DD or YYYYMMDD: {text!r}")
    return parsed


def _key(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return tuple(
        str(row.get(name) or "").strip()
        for name in (
            "ts_code",
            "end_date",
            "ann_date",
            "record_date",
            "ex_date",
        )
    )  # type: ignore[return-value]


def _validate_document(base: Path, document: dict[str, Any], ex_date: str) -> dict[str, Any]:
    filename = str(document.get("file") or "").strip()
    if not filename or Path(filename).name != filename:
        raise ReconciliationError("evidence file must be a simple filename")
    path = (base / filename).resolve()
    if path.parent != base.resolve() or not path.is_file():
        raise ReconciliationError(
            f"official evidence file is missing or outside the manifest dir: {filename}"
        )
    url = str(document.get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise ReconciliationError(f"official document URL is not an allowed CNINFO host: {url}")
    actual_hash = _sha256(path)
    if actual_hash != str(document.get("sha256") or ""):
        raise ReconciliationError(f"official document SHA-256 mismatch: {filename}")
    contents = path.read_bytes()
    if len(contents) != int(document.get("bytes", -1)) or not contents.startswith(b"%PDF-"):
        raise ReconciliationError(f"official evidence is not the expected PDF: {filename}")
    document_date = str(document.get("document_date") or "")
    parsed_document_date = _calendar_date(document_date, label="official document date")
    parsed_ex_date = _calendar_date(ex_date, label="Tushare ex-date")
    if parsed_document_date > parsed_ex_date:
        raise ReconciliationError(
            f"official document date must be known and no later than ex-date: {filename}"
        )
    if not str(document.get("title") or "").strip():
        raise ReconciliationError(f"official document title is missing: {filename}")
    return {
        "file": filename,
        "url": url,
        "sha256": actual_hash,
        "bytes": len(contents),
        "document_date": document_date,
        "title": str(document["title"]),
    }


def reconcile_events(events_path: Path, manifest_path: Path, output_path: Path) -> dict[str, Any]:
    events_path = events_path.expanduser().resolve()
    manifest_path = manifest_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if events_path == output_path or manifest_path == output_path:
        raise ReconciliationError(
            "output must be a new file; source events and manifest are immutable"
        )
    if not events_path.is_file() or not manifest_path.is_file():
        raise ReconciliationError("events CSV and reconciliation manifest must exist")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "official_corporate_action_reconciliation_v1":
        raise ReconciliationError("unsupported corporate-action reconciliation manifest")
    source_hash = _sha256(events_path)
    if source_hash != manifest.get("source_events_sha256"):
        raise ReconciliationError("source events CSV SHA-256 differs from the reviewed manifest")

    with events_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ReconciliationError("source events CSV has no header")
        fieldnames = list(reader.fieldnames)
        rows = list(reader)
    if "ts_code" not in fieldnames:
        raise ReconciliationError("source events CSV must retain qualified Tushare ts_code")
    lookup: dict[tuple[str, str, str, str, str], list[int]] = {}
    for index, row in enumerate(rows):
        lookup.setdefault(_key(row), []).append(index)

    base = manifest_path.parent
    reconciled_keys: set[tuple[str, str, str, str, str]] = set()
    reconciled_rows: list[dict[str, Any]] = []
    for item in manifest.get("reconciliations", []):
        key = _key(item)
        if key in reconciled_keys:
            raise ReconciliationError(f"duplicate reviewed event key: {key}")
        reconciled_keys.add(key)
        indexes = lookup.get(key, [])
        if len(indexes) != 1:
            raise ReconciliationError(
                f"reviewed event key must match exactly one Tushare row: {key}"
            )
        row = rows[indexes[0]]
        vendor = item.get("vendor_event") or {}
        for field in ("div_proc", "source"):
            if str(row.get(field) or "") != str(vendor.get(field) or ""):
                raise ReconciliationError(f"Tushare {field} changed for event {key}")
        for source_field in ("cash_div", "cash_div_tax", "stk_div", "stk_bo_rate", "stk_co_rate"):
            _same_number(
                row.get(source_field), vendor.get(source_field), label=f"{key} {source_field}"
            )
        if str(row.get("div_proc") or "").strip() != "实施":
            raise ReconciliationError(f"Tushare action is not marked implemented: {key}")

        official = item.get("official_reconciliation") or {}
        if official.get("implementation_status") != "实施":
            raise ReconciliationError(f"official implementation status is not confirmed: {key}")
        expected_cash = _number(official.get("cash_dividend_per_share_before_tax"))
        expected_after_tax = _number(official.get("cash_dividend_per_share_after_tax"))
        total_ratio = _number(official.get("stock_distribution_per_share"))
        bonus_ratio = _number(official.get("bonus_shares_per_share"))
        transfer_ratio = _number(official.get("transfer_shares_per_share"))
        if expected_cash != 0.0 or expected_after_tax != 0.0:
            raise ReconciliationError(
                f"this reconciliation only authorizes an official zero-cash fact: {key}"
            )
        if None in (total_ratio, bonus_ratio, transfer_ratio) or not math.isclose(
            float(total_ratio),
            float(bonus_ratio) + float(transfer_ratio),
            abs_tol=1e-10,
        ):
            raise ReconciliationError(
                f"official share ratios are incomplete or inconsistent: {key}"
            )
        _same_number(row.get("stk_div"), total_ratio, label=f"{key} official total share ratio")
        source_cash = _number(row.get("cash_div"))
        source_cash_after_tax = _number(row.get("cash_div_tax"))
        if source_cash not in (None, 0.0) or source_cash_after_tax not in (None, 0.0):
            raise ReconciliationError(
                f"official zero-cash fact conflicts with nonzero Tushare value: {key}"
            )

        documents = item.get("official_documents") or []
        if not documents:
            raise ReconciliationError(f"event has no official source document: {key}")
        validated_docs = [_validate_document(base, doc, key[4]) for doc in documents]
        evidence = {
            "source": "cninfo_official_disclosure",
            "reviewed_on": str(manifest.get("reviewed_on") or ""),
            "official_reconciliation": official,
            "documents": validated_docs,
        }
        row["source_cash_div_before_reconciliation"] = row.get("cash_div", "")
        row["source_cash_div_tax_before_reconciliation"] = row.get("cash_div_tax", "")
        row["source_stk_div_before_reconciliation"] = row.get("stk_div", "")
        row["source_stk_bo_rate_before_reconciliation"] = row.get("stk_bo_rate", "")
        row["source_stk_co_rate_before_reconciliation"] = row.get("stk_co_rate", "")
        row["cash_div"] = "0.0"
        row["cash_div_tax"] = "0.0"
        row["stk_div"] = str(total_ratio)
        row["stk_bo_rate"] = str(bonus_ratio)
        row["stk_co_rate"] = str(transfer_ratio)
        row["record_source"] = RECONCILED_SOURCE
        row["official_reconciliation_evidence_json"] = json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
        )
        reconciled_rows.append(
            {
                "event_key": key,
                "cash_div_before": source_cash,
                "cash_div_tax_before": source_cash_after_tax,
                "official_cash_dividend_per_share": 0.0,
                "official_stock_distribution_per_share": total_ratio,
                "official_document_sha256": [doc["sha256"] for doc in validated_docs],
            }
        )

    dropped_aliases: list[dict[str, Any]] = []
    for alias in manifest.get("duplicate_disclosures", []):
        alias_key = _key(alias)
        canonical_key = (
            alias_key[0],
            alias_key[1],
            str(alias.get("canonical_ann_date") or ""),
            alias_key[3],
            alias_key[4],
        )
        canonical_indexes = lookup.get(canonical_key, [])
        alias_indexes = lookup.get(alias_key, [])
        if len(canonical_indexes) != 1 or len(alias_indexes) != 1:
            raise ReconciliationError(
                f"duplicate disclosure alias must resolve uniquely: {alias_key}"
            )
        if canonical_key not in reconciled_keys:
            raise ReconciliationError(
                f"duplicate alias canonical event is not officially reconciled: {canonical_key}"
            )
        canonical = rows[canonical_indexes[0]]
        duplicate = rows[alias_indexes[0]]
        for field in (
            "div_proc",
            "stk_div",
            "stk_bo_rate",
            "stk_co_rate",
            "record_date",
            "ex_date",
        ):
            canonical_value = canonical.get(field)
            if (
                field
                in {
                    "stk_div",
                    "stk_bo_rate",
                    "stk_co_rate",
                }
                and canonical.get(f"source_{field}_before_reconciliation") is not None
            ):
                canonical_value = canonical.get(f"source_{field}_before_reconciliation")
            if field in {"div_proc", "record_date", "ex_date"}:
                same = str(canonical_value or "") == str(duplicate.get(field) or "")
            else:
                a, b = _number(canonical_value), _number(duplicate.get(field))
                same = a == b if a is None or b is None else math.isclose(a, b, abs_tol=1e-10)
            if not same:
                raise ReconciliationError(
                    f"duplicate disclosure terms differ in {field}: {alias_key}"
                )
        for field in ("cash_div", "cash_div_tax"):
            amount = _number(duplicate.get(field))
            if amount not in (None, 0.0):
                raise ReconciliationError(
                    f"duplicate disclosure has an unexpected nonzero {field}: {alias_key}"
                )
        if alias_indexes[0] == canonical_indexes[0]:
            raise ReconciliationError("duplicate alias points at its canonical row")
        duplicate["record_source"] = "duplicate_alias_excluded"
        duplicate["duplicate_alias_reason"] = str(alias.get("reason") or "")
        dropped_aliases.append(
            {
                "event_key": alias_key,
                "canonical_event_key": canonical_key,
                "reason": str(alias.get("reason") or ""),
            }
        )

    drop_indexes = {lookup[_key(alias)][0] for alias in manifest.get("duplicate_disclosures", [])}
    kept_rows = [row for index, row in enumerate(rows) if index not in drop_indexes]
    for name in EXTRA_COLUMNS:
        if name not in fieldnames:
            fieldnames.append(name)
    if "duplicate_alias_reason" not in fieldnames:
        fieldnames.append("duplicate_alias_reason")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    os.close(fd)
    temporary_path = Path(temporary)
    try:
        with temporary_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="raise")
            writer.writeheader()
            writer.writerows(kept_rows)
        temporary_path.replace(output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    report = {
        "schema_version": "official_corporate_action_reconciliation_report_v1",
        "source_events_csv": str(events_path),
        "source_events_sha256": source_hash,
        "reconciliation_manifest": str(manifest_path),
        "reconciliation_manifest_sha256": _sha256(manifest_path),
        "output_events_csv": str(output_path),
        "output_events_sha256": _sha256(output_path),
        "source_event_rows": len(rows),
        "reconciled_event_rows": len(reconciled_rows),
        "duplicate_aliases_excluded": dropped_aliases,
        "reconciled_events": reconciled_rows,
        "blank_values_filled_only_from_official_evidence": True,
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events-csv", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)
    try:
        report = reconcile_events(Path(args.events_csv), Path(args.manifest), Path(args.out))
    except (OSError, json.JSONDecodeError, ReconciliationError) as exc:
        parser.exit(5, f"company-action reconciliation blocked: {type(exc).__name__}: {exc}\n")
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        report_path = Path(args.report).expanduser()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
