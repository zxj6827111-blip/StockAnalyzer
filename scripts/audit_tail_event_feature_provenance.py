#!/usr/bin/env python3
"""Audit historical moneyflow/top-list research files without claiming PIT proof."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import sys
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_ARTIFACTS = _ROOT / "artifacts" / "research"

_SOURCES: dict[str, dict[str, Any]] = {
    "moneyflow": {
        "filename": "moneyflow_2025.csv.gz",
        "report_filename": "moneyflow_2025_report.json",
        "documentation_url": "https://tushare.pro/document/2?doc_id=170",
        "columns": (
            "trade_date",
            "ts_code",
            "buy_sm_vol",
            "sell_sm_vol",
            "buy_md_vol",
            "sell_md_vol",
            "buy_lg_vol",
            "sell_lg_vol",
            "buy_elg_vol",
            "sell_elg_vol",
            "net_mf_vol",
            "net_mf_amount",
        ),
    },
    "top_list": {
        "filename": "top_list_2025.csv.gz",
        "report_filename": "top_list_2025_report.json",
        "documentation_url": "https://tushare.pro/document/2?doc_id=106",
        "columns": (
            "trade_date",
            "ts_code",
            "name",
            "close",
            "pct_change",
            "turnover_rate",
            "amount",
            "l_sell",
            "l_buy",
            "net_amount",
            "reason",
        ),
    },
}


def _parse_yyyymmdd(value: str) -> date:
    if len(value) != 8 or not value.isdigit():
        raise ValueError(f"invalid YYYYMMDD date: {value!r}")
    return date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:8]}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _local_file_record(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "sha256": _sha256(path),
        "bytes": stat.st_size,
        # Filesystem mtime is local-copy metadata, never the vendor capture time.
        "local_mtime_utc": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
    }


def _load_expected_trade_dates(
    path: Path, *, start: str, end: str
) -> set[str]:
    lower = _parse_yyyymmdd(start)
    upper = _parse_yyyymmdd(end)
    expected: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "is_open" not in reader.fieldnames:
            raise ValueError(f"{path}: trading calendar requires is_open")
        date_field = next(
            (field for field in ("trade_date", "date") if field in reader.fieldnames),
            None,
        )
        if date_field is None:
            raise ValueError(f"{path}: trading calendar requires trade_date or date")
        for _line_number, row in enumerate(reader, 2):
            trade_date = date.fromisoformat(str(row.get(date_field) or "")[:10])
            if lower <= trade_date <= upper and str(row.get("is_open") or "").lower() in {
                "1",
                "true",
                "yes",
                "y",
                "open",
            }:
                expected.add(trade_date.strftime("%Y%m%d"))
    if not expected:
        raise ValueError(f"{path}: no open sessions in requested period")
    return expected


def _audit_source(
    name: str,
    artifacts_dir: Path,
    expected_trade_dates: set[str] | None,
) -> dict[str, Any]:
    spec = _SOURCES[name]
    data_path = artifacts_dir / str(spec["filename"])
    report_path = artifacts_dir / str(spec["report_filename"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError(f"{report_path}: report must be a JSON object")
    if report.get("api") != name:
        raise ValueError(f"{report_path}: expected api={name!r}")

    requested_days = int(report.get("requested_days", -1))
    collected_days = int(report.get("collected_days", -1))
    failures = report.get("days_failed")
    empty_days = report.get("empty_days")
    truncated_days = report.get("days_at_row_limit_possibly_truncated")
    report_coverage_ok = (
        requested_days > 0
        and collected_days == requested_days
        and failures == {}
        and empty_days == []
        and truncated_days == []
    )

    counts: Counter[str] = Counter()
    pairs: set[tuple[str, str]] = set()
    duplicate_pairs = 0
    duplicate_pair_keys: set[tuple[str, str]] = set()
    reasons_by_pair: dict[tuple[str, str], set[str]] = {}
    blank_symbols = 0
    invalid_dates = 0
    row_count = 0
    with gzip.open(data_path, "rt", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = tuple(reader.fieldnames or ())
        if fieldnames != tuple(spec["columns"]):
            raise ValueError(f"{data_path}: unexpected columns {fieldnames!r}")
        for _line_number, row in enumerate(reader, 2):
            row_count += 1
            trade_date = str(row.get("trade_date") or "").strip()
            symbol = str(row.get("ts_code") or "").strip()
            try:
                if len(trade_date) != 8 or not trade_date.isdigit():
                    raise ValueError
                date.fromisoformat(
                    f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
                )
            except ValueError:
                invalid_dates += 1
            if not symbol:
                blank_symbols += 1
            counts[trade_date] += 1
            pair = (trade_date, symbol)
            if pair in pairs:
                duplicate_pairs += 1
                duplicate_pair_keys.add(pair)
            pairs.add(pair)
            if name == "top_list":
                reasons_by_pair.setdefault(pair, set()).add(
                    str(row.get("reason") or "").strip()
                )

    expected_rows = int(report.get("rows_total", -1))
    observed_min = min(counts.values(), default=0)
    observed_max = max(counts.values(), default=0)
    observed_trade_dates = set(counts)
    missing_trade_dates = (
        sorted(expected_trade_dates - observed_trade_dates) if expected_trade_dates else None
    )
    unexpected_trade_dates = (
        sorted(observed_trade_dates - expected_trade_dates) if expected_trade_dates else None
    )
    count_matches = (
        row_count == expected_rows
        and observed_min == int(report.get("rows_per_day_min", -1))
        and observed_max == int(report.get("rows_per_day_max", -1))
    )
    if invalid_dates or blank_symbols:
        raise ValueError(
            f"{data_path}: invalid_dates={invalid_dates}, blank_symbols={blank_symbols}"
        )
    if duplicate_pairs and name == "moneyflow":
        raise ValueError(f"{data_path}: moneyflow has {duplicate_pairs} duplicate symbol-days")

    return {
        "api": name,
        "documentation_url": spec["documentation_url"],
        "data_file": _local_file_record(data_path),
        "collector_report": _local_file_record(report_path),
        "report_claims": {
            "start": report.get("start"),
            "end": report.get("end"),
            "requested_days": requested_days,
            "collected_days": collected_days,
            "days_failed": failures,
            "empty_days": empty_days,
            "days_at_row_limit_possibly_truncated": truncated_days,
        },
        "observed": {
            "rows": row_count,
            "unique_trade_dates": len(counts),
            "min_rows_per_date": observed_min,
            "max_rows_per_date": observed_max,
            "unique_symbol_days": len(pairs),
            "duplicate_symbol_day_rows": duplicate_pairs,
            "duplicate_symbol_day_keys": len(duplicate_pair_keys),
            "duplicate_symbol_days_with_multiple_reasons": sum(
                1
                for pair in duplicate_pair_keys
                if len(reasons_by_pair.get(pair, set())) > 1
            ),
            "duplicate_symbol_days_with_same_reason": sum(
                1
                for pair in duplicate_pair_keys
                if len(reasons_by_pair.get(pair, set())) == 1
            ),
            "blank_symbols": blank_symbols,
            "invalid_dates": invalid_dates,
            "matches_collector_counts": count_matches,
            "missing_calendar_dates": missing_trade_dates,
            "unexpected_calendar_dates": unexpected_trade_dates,
        },
        "reported_daily_response_coverage": "complete"
        if (
            report_coverage_ok
            and count_matches
            and expected_trade_dates is not None
            and observed_trade_dates == expected_trade_dates
            and len(expected_trade_dates) == requested_days
        )
        else "incomplete_or_unverified",
        "source_capture_timestamp": None,
        "per_day_response_hashes": None,
        "point_in_time_as_of_snapshot": False,
    }


def build_audit(
    artifacts_dir: Path,
    trading_calendar_path: Path | None = None,
) -> dict[str, Any]:
    calendar_path = trading_calendar_path or (
        artifacts_dir / "tail_trading_calendar_2025_raw_v2.csv"
    )
    calendar_file = _local_file_record(calendar_path) if calendar_path.is_file() else None
    expected_by_source: dict[str, set[str] | None] = {}
    for name in _SOURCES:
        report_path = artifacts_dir / str(_SOURCES[name]["report_filename"])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if calendar_file is None:
            expected_by_source[name] = None
        else:
            expected_by_source[name] = _load_expected_trade_dates(
                calendar_path,
                start=str(report.get("start") or ""),
                end=str(report.get("end") or ""),
            )
    sources = {
        name: _audit_source(name, artifacts_dir, expected_by_source[name])
        for name in _SOURCES
    }
    blockers = [
        "historical_vendor_capture_timestamp_and_per_day_response_hashes_absent",
        "vendor_documentation_does_not_establish_historical_point_in_time_vintages_or_release_cutoff_for_these_files",
        "moneyflow_missing_symbol_day_semantics_are_not_proven_and_must_not_be_zero_filled",
    ]
    if sources["top_list"]["observed"]["duplicate_symbol_day_rows"]:
        blockers.append(
            "top_list_has_duplicate_symbol_day_rows; deduplicate_by_symbol_day_before_counting"
        )
    if any(
        source["reported_daily_response_coverage"] != "complete" for source in sources.values()
    ):
        blockers.append("one_or_more_daily_response_sets_are_incomplete_or_unverified")
    if calendar_file is None:
        blockers.append("matching_trading_calendar_missing")
    return {
        "schema": "tail_event_feature_provenance_audit.v1",
        "scope": "historical_2025_moneyflow_and_top_list_research_files",
        "status": "blocked",
        "point_in_time_proven": False,
        "formal_stage2_event_arms_eligible": False,
        "blockers": blockers,
        "trading_calendar": calendar_file,
        "sources": sources,
        "interpretation": (
            "Row coverage and file integrity can be checked, but the files do not establish "
            "what vendor values were available at each historical decision cutoff. A one-session "
            "feature lag does not restore missing historical source vintages."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts-dir", type=Path, default=_DEFAULT_ARTIFACTS)
    parser.add_argument("--trading-calendar", type=Path)
    parser.add_argument(
        "--out",
        type=Path,
        default=_DEFAULT_ARTIFACTS / "tail_event_pit_provenance_audit_20261011_v3.json",
    )
    args = parser.parse_args(argv)
    try:
        report = build_audit(
            args.artifacts_dir.expanduser().resolve(),
            args.trading_calendar.expanduser().resolve() if args.trading_calendar else None,
        )
        out_path = args.out.expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"event provenance audit failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    summary = {"out": str(out_path), "status": report["status"], "blockers": report["blockers"]}
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
