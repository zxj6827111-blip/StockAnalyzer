#!/usr/bin/env python3
"""Audit RAW label adj_factor coverage for frozen wide-pool request files.

The audit is read-only. It accounts for the prior-session factor window required
by corporate-action reconciliation and only excuses pre-listing prefixes when a
complete listing interval exists in the independent reference database. Outputs
are new, hash-bound collection symbol lists and per-symbol missing-session reports.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.contracts.trend_strategy import DEFAULT_TREND_CONTRACT  # noqa: E402
from stock_analyzer.research.suspension_evidence import (  # noqa: E402
    SSE_OFFICIAL_SUSPENSION_SOURCE,
    is_verified_no_trade_adj_factor_gap,
)
from stock_analyzer.research.tail_rebuild import exit_window_end  # noqa: E402

_SYMBOL_RE = re.compile(r"^\d{1,6}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_symbol(value: Any) -> str:
    raw = str(value or "").strip().upper().split(".", 1)[0]
    if not _SYMBOL_RE.fullmatch(raw):
        raise ValueError("request symbol must be a numeric A-share code")
    return raw.zfill(6)


def read_request_windows(path: Path) -> tuple[dict[str, tuple[date, date]], dict[str, Any]]:
    windows: dict[str, tuple[date, date]] = {}
    requests = 0
    request_dates: set[date] = set()
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            symbol = _normalize_symbol(row["symbol"])
            entry = date.fromisoformat(str(row["entry_date"])[:10])
            end = exit_window_end(entry, DEFAULT_TREND_CONTRACT)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid request row {line_no}: {type(exc).__name__}") from exc
        previous = windows.get(symbol)
        windows[symbol] = (
            min(previous[0], entry) if previous else entry,
            max(previous[1], end) if previous else end,
        )
        requests += 1
        request_dates.add(entry)
    if not windows:
        raise ValueError(f"request file is empty: {path}")
    return windows, {"requests": requests, "decision_dates": len(request_dates)}


def _load_reference_state(
    factor_db: Path, reference_db: Path
) -> tuple[list[date], dict[str, date], dict[tuple[str, date], tuple[float | None, bool]]]:
    calendar_db = duckdb.connect(str(reference_db), read_only=True)
    factor_conn = duckdb.connect(str(factor_db), read_only=True)
    try:
        calendar = [
            row[0]
            for row in calendar_db.execute(
                "SELECT trade_date FROM ref_trade_calendar "
                "WHERE exchange = 'SSE' AND is_open = TRUE ORDER BY trade_date"
            ).fetchall()
        ]
        if not calendar:
            raise ValueError("reference trade calendar is empty")
        listing_rows = calendar_db.execute(
            "SELECT symbol, effective_from, COUNT(*) AS rows "
            "FROM ref_security_status WHERE status_type = 'listing_period' "
            "AND coverage_complete = TRUE GROUP BY symbol, effective_from"
        ).fetchall()
        listing_dates: dict[str, date] = {}
        ambiguous_listing: set[str] = set()
        for symbol, effective_from, _count in listing_rows:
            normalized = _normalize_symbol(symbol)
            if normalized in listing_dates and listing_dates[normalized] != effective_from:
                ambiguous_listing.add(normalized)
            else:
                listing_dates[normalized] = effective_from
        for symbol in ambiguous_listing:
            listing_dates.pop(symbol, None)
        factor_rows = factor_conn.execute(
            "SELECT symbol, trade_date, adj_factor, coverage_complete "
            "FROM ref_adj_factor_history"
        ).fetchall()
        factors: dict[tuple[str, date], tuple[float | None, bool]] = {}
        for symbol, trade_day, value, complete in factor_rows:
            normalized = _normalize_symbol(symbol)
            number = float(value) if value is not None else None
            factors[(normalized, trade_day)] = (number, bool(complete))
        return calendar, listing_dates, factors
    finally:
        calendar_db.close()
        factor_conn.close()


def _suspension_exempt_factor_gaps(
    con: duckdb.DuckDBPyConnection,
    *,
    symbol: str,
    missing_days: list[date],
    factors: dict[tuple[str, date], tuple[float | None, bool]],
) -> list[date]:
    """Return only gaps proven irrelevant because no trade/action could occur."""
    exempt: list[date] = []
    symbol_factors = sorted(
        (day, value)
        for (candidate, day), (value, complete) in factors.items()
        if candidate == symbol and complete and value is not None and value > 0
    )
    for day in missing_days:
        try:
            status_rows = con.execute(
                "SELECT suspended, suspend_type, trade_status, source, as_of, coverage_complete "
                "FROM ref_suspend_status WHERE symbol = ? AND trade_date = ?",
                [symbol, day],
            ).fetchall()
            if not status_rows:
                continue
            columns = [item[0] for item in con.description]
            evidence: dict[str, Any] = dict(zip(columns, status_rows[0], strict=True))
            evidence["trade_date"] = day.isoformat()
            if evidence.get("source") == SSE_OFFICIAL_SUSPENSION_SOURCE:
                proof_rows = con.execute(
                    "SELECT evidence_url, evidence_title, evidence_document_date, "
                    "evidence_sha256, evidence_full_day, evidence_as_of, raw_payload "
                    "FROM ref_suspend_evidence WHERE symbol = ? AND trade_date = ? "
                    "AND source = ?",
                    [symbol, day, SSE_OFFICIAL_SUSPENSION_SOURCE],
                ).fetchall()
                if not proof_rows:
                    continue
                proof_columns = [item[0] for item in con.description]
                proof = dict(zip(proof_columns, proof_rows[0], strict=True))
                payload = json.loads(str(proof.get("raw_payload") or "{}"))
                evidence.update(
                    {
                        **proof,
                        "evidence_document_date": str(proof["evidence_document_date"]),
                        "evidence_start_date": payload.get("evidence_start_date"),
                        "evidence_end_date": payload.get("evidence_end_date"),
                    }
                )
            daily_count = con.execute(
                "SELECT COUNT(*) FROM ref_daily_bars_raw WHERE symbol = ? AND trade_date = ?",
                [symbol, day],
            ).fetchone()[0]
            minute_count = con.execute(
                "SELECT COUNT(*) FROM minute_bars_1min WHERE symbol = ? AND trade_date = ?",
                [symbol, day],
            ).fetchone()[0]
            actions = con.execute(
                "SELECT COUNT(*) FROM ref_corporate_actions WHERE symbol = ? AND ex_date = ?",
                [symbol, day],
            ).fetchone()[0]
            action_coverage = con.execute(
                "SELECT coverage_complete, truncated, error "
                "FROM ref_corporate_action_coverage "
                "WHERE ex_date = ? AND source = 'tushare_dividend_ex_date'",
                [day],
            ).fetchall()
        except duckdb.Error:
            # Missing reference tables cannot prove an exception.
            return []
        complete_action_coverage = bool(
            action_coverage
            and all(bool(row[0]) and not bool(row[1]) and not str(row[2] or "").strip()
                    for row in action_coverage)
        )
        prior = [item for item in symbol_factors if item[0] < day]
        following = [item for item in symbol_factors if item[0] > day]
        if not prior or not following:
            continue
        if is_verified_no_trade_adj_factor_gap(
            evidence,
            has_raw_daily_bar=bool(daily_count),
            raw_minute_bar_count=int(minute_count),
            action_coverage_complete=complete_action_coverage,
            has_corporate_action=bool(actions),
            previous_factor=prior[-1][1],
            next_factor=following[0][1],
        ):
            exempt.append(day)
    return exempt


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
    ) as handle:
        temp = Path(handle.name)
        handle.write(content)
        handle.flush()
    temp.replace(path)


def audit_factor_coverage(
    *,
    factor_db: Path,
    reference_db: Path,
    cohorts: list[tuple[str, Path]],
    out_dir: Path,
) -> dict[str, Any]:
    factor_db = factor_db.expanduser().resolve()
    reference_db = reference_db.expanduser().resolve()
    out_dir = out_dir.expanduser().resolve()
    for path in (factor_db, reference_db):
        if not path.is_file():
            raise FileNotFoundError(path)
    if not cohorts:
        raise ValueError("at least one cohort is required")
    names = [name for name, _path in cohorts]
    if len(names) != len(set(names)) or any(not re.fullmatch(r"[a-zA-Z0-9_-]+", n) for n in names):
        raise ValueError("cohort names must be unique letters, digits, '_' or '-'")

    parsed: list[tuple[str, Path, dict[str, tuple[date, date]], dict[str, Any]]] = []
    for name, request_path in cohorts:
        path = request_path.expanduser().resolve()
        windows, summary = read_request_windows(path)
        parsed.append((name, path, windows, summary))
    calendar, listing_dates, factors = _load_reference_state(factor_db, reference_db)
    cohort_reports: dict[str, Any] = {}
    all_missing_symbols: set[str] = set()
    pending_outputs: list[tuple[Path, bytes]] = []

    for name, request_path, windows, request_summary in parsed:
        first_entry = min(start for start, _end in windows.values())
        last_exit = max(end for _start, end in windows.values())
        factor_start = first_entry - timedelta(days=31)
        if factor_start < calendar[0] or last_exit > calendar[-1]:
            raise ValueError(f"calendar does not cover factor window for cohort {name}")
        detail_rows: list[dict[str, Any]] = []
        missing_symbols: list[str] = []
        total_expected = 0
        total_covered = 0
        missing_symbol_days = 0
        raw_missing_symbol_days = 0
        suspension_exempt_symbol_days = 0
        listing_unknown = 0
        for symbol, (entry, exit_day) in sorted(windows.items()):
            listing_date = listing_dates.get(symbol)
            # Each symbol only needs the contract lookback before its own first
            # requested entry. A cohort-wide start would overstate coverage for
            # symbols that first appear later in the request window.
            start = entry - timedelta(days=31)
            listing_exception = False
            if listing_date is None:
                listing_unknown += 1
            elif listing_date > entry:
                # A request before the proved listing date contradicts its source evidence.
                listing_exception = True
                start = max(start, listing_date)
            elif listing_date > start:
                start = listing_date
                listing_exception = True
            expected = [day for day in calendar if start <= day <= exit_day]
            if not expected:
                missing_symbols.append(symbol)
                missing_symbol_days += 1
                detail_rows.append({
                    "symbol": symbol,
                    "first_entry_date": entry.isoformat(),
                    "last_exit_date": exit_day.isoformat(),
                    "factor_start_date": start.isoformat(),
                    "listing_date": listing_date.isoformat() if listing_date else "",
                    "listing_prefix_exception": listing_exception,
                    "expected_sessions": 0,
                    "covered_sessions": 0,
                    "missing_sessions": 0,
                    "missing_dates_json": "[]",
                    "suspension_exempt_sessions": 0,
                    "suspension_exempt_dates_json": "[]",
                    "effective_missing_sessions": 0,
                    "effective_missing_dates_json": "[]",
                    "coverage_complete": False,
                    "error": "no_expected_sessions",
                })
                continue
            missing = [
                day
                for day in expected
                if (symbol, day) not in factors
                or not factors[(symbol, day)][1]
                or factors[(symbol, day)][0] is None
                or factors[(symbol, day)][0] <= 0
            ]
            exempt: list[date] = []
            if missing:
                evidence_con = duckdb.connect(str(factor_db), read_only=True)
                try:
                    exempt = _suspension_exempt_factor_gaps(
                        evidence_con,
                        symbol=symbol,
                        missing_days=missing,
                        factors=factors,
                    )
                finally:
                    evidence_con.close()
            effective_missing = [day for day in missing if day not in set(exempt)]
            covered = len(expected) - len(effective_missing)
            complete = not effective_missing and not (
                listing_exception and listing_date > entry
            )
            if not complete:
                missing_symbols.append(symbol)
            total_expected += len(expected)
            total_covered += covered
            raw_missing_symbol_days += len(missing)
            suspension_exempt_symbol_days += len(exempt)
            missing_symbol_days += len(effective_missing)
            detail_rows.append({
                "symbol": symbol,
                "first_entry_date": entry.isoformat(),
                "last_exit_date": exit_day.isoformat(),
                "factor_start_date": start.isoformat(),
                "listing_date": listing_date.isoformat() if listing_date else "",
                "listing_prefix_exception": listing_exception,
                "expected_sessions": len(expected),
                "covered_sessions": covered,
                "missing_sessions": len(missing),
                "missing_dates_json": json.dumps(
                    [day.isoformat() for day in missing], separators=(",", ":")
                ),
                "suspension_exempt_sessions": len(exempt),
                "suspension_exempt_dates_json": json.dumps(
                    [day.isoformat() for day in exempt], separators=(",", ":")
                ),
                "effective_missing_sessions": len(effective_missing),
                "effective_missing_dates_json": json.dumps(
                    [day.isoformat() for day in effective_missing], separators=(",", ":")
                ),
                "coverage_complete": complete,
                "error": (
                    "request_before_proved_listing"
                    if listing_exception and listing_date and listing_date > entry
                    else ""
                ),
            })

        missing_symbols = sorted(set(missing_symbols))
        all_missing_symbols.update(missing_symbols)
        start_text = factor_start.strftime("%Y%m%d")
        end_text = last_exit.strftime("%Y%m%d")
        symbols_path = out_dir / f"tail_pool_adj_factor_missing_symbols_{name}.txt"
        details_path = out_dir / f"tail_pool_adj_factor_coverage_{name}.csv"
        symbols_bytes = ("\n".join(missing_symbols) + ("\n" if missing_symbols else "")).encode()
        fieldnames = [
            "symbol", "first_entry_date", "last_exit_date", "factor_start_date", "listing_date",
            "listing_prefix_exception", "expected_sessions", "covered_sessions",
            "missing_sessions", "missing_dates_json", "suspension_exempt_sessions",
            "suspension_exempt_dates_json", "effective_missing_sessions",
            "effective_missing_dates_json", "coverage_complete", "error",
        ]
        from io import StringIO

        buffer = StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(detail_rows)
        details_bytes = buffer.getvalue().encode("utf-8")
        pending_outputs.extend([(symbols_path, symbols_bytes), (details_path, details_bytes)])
        request_sha = _sha256(request_path)
        cohort_reports[name] = {
            "requests_path": str(request_path),
            "requests_sha256": request_sha,
            "request_rows": request_summary["requests"],
            "decision_dates": request_summary["decision_dates"],
            "symbols": len(windows),
            "factor_window_start": factor_start.isoformat(),
            "factor_window_end": last_exit.isoformat(),
            "factor_window_sessions": sum(factor_start <= day <= last_exit for day in calendar),
            "expected_symbol_sessions": total_expected,
            "covered_symbol_sessions": total_covered,
            "missing_symbol_sessions": missing_symbol_days,
            "raw_missing_symbol_sessions": raw_missing_symbol_days,
            "suspension_exempt_symbol_sessions": suspension_exempt_symbol_days,
            "complete_symbols": len(windows) - len(missing_symbols),
            "incomplete_symbols": len(missing_symbols),
            "listing_date_unproven_symbols": listing_unknown,
            "coverage_share": round(total_covered / total_expected, 6) if total_expected else 0.0,
            "collector_start": start_text,
            "collector_end": end_text,
            "symbols_file": str(symbols_path),
            "symbols_file_sha256": hashlib.sha256(symbols_bytes).hexdigest(),
            "coverage_csv": str(details_path),
            "coverage_csv_sha256": hashlib.sha256(details_bytes).hexdigest(),
        }

    union_path = out_dir / "tail_pool_adj_factor_missing_symbols_union.txt"
    union_symbols = sorted(all_missing_symbols)
    union_bytes = ("\n".join(union_symbols) + ("\n" if union_symbols else "")).encode()
    normalized_symbols_sha256 = hashlib.sha256("\n".join(union_symbols).encode()).hexdigest()
    symbols_file_sha256 = hashlib.sha256(union_bytes).hexdigest()
    pending_outputs.append((union_path, union_bytes))
    collection_bundle: dict[str, Any] | None = None
    if union_symbols:
        union_set = set(union_symbols)
        request_lines: list[str] = []
        for _name, request_path, _windows, _request_summary in parsed:
            for line in request_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if _normalize_symbol(row["symbol"]) in union_set:
                    request_lines.append(line.rstrip() + "\n")
        collection_requests_bytes = "".join(request_lines).encode("utf-8")
        collection_requests_path = out_dir / "tail_pool_adj_factor_union_requests.jsonl"
        pending_outputs.append((collection_requests_path, collection_requests_bytes))
        collection_scope_path = out_dir / "tail_pool_adj_factor_collection_scope.json"
        collection_start = min(
            cohort["collector_start"] for cohort in cohort_reports.values()
        )
        collection_end = max(cohort["collector_end"] for cohort in cohort_reports.values())
        collection_scope = {
            "schema": "tail_adj_factor_collection_manifest.v2",
            "collector": "scripts/collect_adj_factor_history.py",
            "request_sha256": hashlib.sha256(collection_requests_bytes).hexdigest(),
            "symbols_sha256": normalized_symbols_sha256,
            "symbols": len(union_symbols),
            "start": collection_start,
            "end": collection_end,
        }
        collection_scope_bytes = (
            json.dumps(collection_scope, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        pending_outputs.append((collection_scope_path, collection_scope_bytes))
        collection_bundle = {
            "symbols_file": str(union_path),
            "symbols_sha256": normalized_symbols_sha256,
            "symbols_file_sha256": symbols_file_sha256,
            "symbols": len(union_symbols),
            "requests_file": str(collection_requests_path),
            "requests_sha256": hashlib.sha256(collection_requests_bytes).hexdigest(),
            "request_rows": len(request_lines),
            "scope_manifest_file": str(collection_scope_path),
            "scope_manifest_sha256": hashlib.sha256(collection_scope_bytes).hexdigest(),
            "collector_start": collection_start,
            "collector_end": collection_end,
        }
    report_path = out_dir / "tail_pool_adj_factor_coverage_report.json"
    report = {
        "schema": "tail_pool_adj_factor_coverage_audit.v1",
        "source": {
            "factor_db": str(factor_db),
            "factor_db_bytes": factor_db.stat().st_size,
            "factor_db_mtime_ns": factor_db.stat().st_mtime_ns,
            "reference_db": str(reference_db),
            "reference_db_bytes": reference_db.stat().st_size,
            "reference_db_mtime_ns": reference_db.stat().st_mtime_ns,
            "factor_table_symbols": len({symbol for symbol, _day in factors}),
            "factor_table_rows": len(factors),
            "calendar_rows": len(calendar),
            "listing_symbols_with_unambiguous_intervals": len(listing_dates),
        },
        "contract": {
            "holding_days": int(DEFAULT_TREND_CONTRACT.holding_days),
            "required_prior_factor_calendar_days": 31,
        },
        "cohorts": cohort_reports,
        "collection_bundle": collection_bundle,
        "missing_symbols_union": {
            "path": str(union_path),
            "symbols": len(union_symbols),
            "sha256": hashlib.sha256(union_bytes).hexdigest(),
        },
    }
    report_bytes = (
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    pending_outputs.append((report_path, report_bytes))
    existing = [str(path) for path, _content in pending_outputs if path.exists()]
    if existing:
        raise FileExistsError(f"refusing to overwrite output(s): {existing}")
    for path, content in pending_outputs:
        _atomic_write(path, content)
    return report


def _parse_cohort(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("cohort must be NAME=REQUESTS.jsonl")
    name, path = value.split("=", 1)
    return name.strip(), Path(path.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factor-db", required=True, type=Path)
    parser.add_argument("--reference-db", required=True, type=Path)
    parser.add_argument("--cohort", required=True, action="append", type=_parse_cohort)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    report = audit_factor_coverage(
        factor_db=args.factor_db,
        reference_db=args.reference_db,
        cohorts=args.cohort,
        out_dir=args.out_dir,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
