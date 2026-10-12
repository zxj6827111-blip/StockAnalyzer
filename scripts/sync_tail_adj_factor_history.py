#!/usr/bin/env python3
"""Import a hash-checked, symbol-scoped adj_factor collection to a research DuckDB."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.research.tail_reference_store import (  # noqa: E402
    TailReferenceError,
    TailReferenceStore,
)

SOURCE = "tushare_adj_factor_history"
LEGACY_SYMBOL_SOURCE = "tushare_adj_factor_symbol_history"
SCOPE_SCHEMA = "tail_adj_factor_collection_manifest.v2"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _flag(value: Any, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "t"}:
        return True
    if text in {"false", "0", "f"}:
        return False
    raise ValueError(f"invalid {field}")


def _symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text.split(".", 1)[0]


def _date(value: Any) -> date:
    text = str(value or "").strip()
    try:
        if len(text) == 8:
            return datetime.strptime(text, "%Y%m%d").date()
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("invalid adj_factor trade_date") from exc


def _timestamp(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid {field}") from exc
    return text


def sync_bundle(
    *,
    db_path: Path,
    factors_path: Path,
    coverage_path: Path,
    report_path: Path,
    scope_manifest_path: Path,
    symbols_path: Path,
    requests_path: Path,
    allow_partial: bool = False,
) -> dict[str, Any]:
    import pandas as pd

    db_path = db_path.expanduser().resolve()
    if db_path.suffix.lower() != ".duckdb" or "market_warehouse" in db_path.name.lower():
        raise ValueError("target must be an explicitly named research .duckdb file")
    if "/vol1/docker/stockanalyzer" in str(db_path).lower():
        raise ValueError("refusing to write inside the production NAS project")
    if not db_path.is_file():
        raise ValueError("research DuckDB target does not exist")

    factors_path = factors_path.expanduser().resolve()
    coverage_path = coverage_path.expanduser().resolve()
    report_path = report_path.expanduser().resolve()
    scope_manifest_path = scope_manifest_path.expanduser().resolve()
    symbols_path = symbols_path.expanduser().resolve()
    requests_path = requests_path.expanduser().resolve()
    for path in (
        factors_path,
        coverage_path,
        report_path,
        scope_manifest_path,
        symbols_path,
        requests_path,
    ):
        if not path.is_file():
            raise ValueError(f"missing factor collection input: {path.name}")

    scope = json.loads(scope_manifest_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if scope.get("schema") != SCOPE_SCHEMA:
        raise ValueError("unsupported factor collection scope manifest")
    if scope.get("collector") != "scripts/collect_adj_factor_history.py":
        raise ValueError("scope manifest collector identity mismatch")
    if scope.get("request_sha256") != _sha256(requests_path):
        raise ValueError("request file does not match scope manifest")
    # The collector binds the normalized, sorted symbol sequence (newline joined, without
    # a trailing newline), not the incidental bytes of the human-readable symbols file.
    # Validate the same representation here so a normal final newline does not make a
    # correctly collected bundle impossible to import.
    normalized_symbol_sha256 = hashlib.sha256("\n".join(
        sorted(
            {
                _symbol(line.split(",", 1)[0])
                for line in symbols_path.read_text(encoding="utf-8").splitlines()
                if line.strip() and line.strip().lower() not in {"symbol", "ts_code"}
            }
        )
    ).encode("utf-8")).hexdigest()
    if scope.get("symbols_sha256") != normalized_symbol_sha256:
        raise ValueError("symbol list does not match scope manifest")
    collection_source = str(report.get("source") or "")
    if (
        collection_source not in {SOURCE, LEGACY_SYMBOL_SOURCE}
        or report.get("scope") != "requested_symbols_only"
    ):
        raise ValueError("collector report source or scope mismatch")
    query_mode = str(report.get("query_mode") or "symbol")
    if query_mode not in {"symbol", "trade_date_market_batch"}:
        raise ValueError("unsupported factor collector query mode")
    report_as_of_raw = report.get("as_of")
    report_as_of = (
        _timestamp(report_as_of_raw, field="collector report as_of")
        if report_as_of_raw not in (None, "")
        else None
    )
    collected_at_raw = report.get("collected_at") or report_as_of
    if collected_at_raw in (None, ""):
        raise ValueError("collector report has neither a common as_of nor collected_at")
    collection_as_of = _timestamp(collected_at_raw, field="collector report collected_at")
    calendar = report.get("trade_calendar")
    if not isinstance(calendar, dict) or not calendar.get("normalized_sha256"):
        raise ValueError("collector report has no verified trade calendar identity")
    if int(calendar.get("open_sessions", -1)) != int(report.get("requested_sessions", -2)):
        raise ValueError("collector calendar session count mismatch")

    symbols = sorted(
        {
            _symbol(line.split(",", 1)[0])
            for line in symbols_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and line.strip().lower() not in {"symbol", "ts_code"}
        }
    )
    if len(symbols) != int(scope.get("symbols", -1)):
        raise ValueError("symbol list count differs from scope manifest")
    if int(report.get("requested_symbols", -1)) != len(symbols):
        raise ValueError("collector symbol count mismatch")

    coverage = pd.read_csv(
        coverage_path,
        dtype={"symbol": "string", "ts_code": "string", "source": "string", "as_of": "string"},
        keep_default_na=False,
    )
    factors = pd.read_csv(
        factors_path,
        dtype={
            "symbol": "string",
            "ts_code": "string",
            "trade_date": "string",
            "source": "string",
            "as_of": "string",
        },
        keep_default_na=False,
    )
    required_coverage = {
        "symbol",
        "ts_code",
        "source",
        "as_of",
        "requested_sessions",
        "rows_returned",
        "missing_sessions",
        "coverage_complete",
        "truncated",
        "error",
        "response_sha256",
        "duplicate_dates",
        "out_of_range_dates",
    }
    required_factors = {
        "symbol",
        "ts_code",
        "trade_date",
        "adj_factor",
        "source",
        "as_of",
        "coverage_complete",
    }
    if not required_coverage <= set(coverage.columns):
        raise ValueError("coverage CSV is missing required fields")
    if not required_factors <= set(factors.columns):
        raise ValueError("factor CSV is missing required fields")
    coverage["symbol"] = coverage["symbol"].map(_symbol)
    factors["symbol"] = factors["symbol"].map(_symbol)
    if coverage["symbol"].duplicated().any() or set(coverage["symbol"]) != set(symbols):
        raise ValueError("coverage rows do not exactly match requested symbols")
    for frame in (coverage, factors):
        if (
            not frame.empty
            and not frame.apply(
                lambda row: _symbol(row.get("ts_code")) == _symbol(row.get("symbol")),
                axis=1,
            ).all()
        ):
            raise ValueError("factor symbol and ts_code disagree")
    if factors.duplicated(["symbol", "trade_date"]).any():
        raise ValueError("factor CSV has duplicate symbol dates")
    if not set(factors["symbol"]) <= set(symbols):
        raise ValueError("factor CSV contains an unrequested symbol")

    sessions = int(report["requested_sessions"])
    start = _date(scope.get("start"))
    end = _date(scope.get("end"))
    coverage["coverage_complete"] = coverage["coverage_complete"].map(
        lambda value: _flag(value, field="coverage_complete")
    )
    coverage["truncated"] = coverage["truncated"].map(lambda value: _flag(value, field="truncated"))
    coverage["as_of"] = coverage["as_of"].map(
        lambda value: _timestamp(value, field="coverage as_of")
    )
    if not (coverage["source"] == collection_source).all():
        raise ValueError("coverage source differs from collector report")
    if report_as_of is not None and not (coverage["as_of"] == report_as_of).all():
        raise ValueError("coverage response times differ from collector report as_of")
    if not (pd.to_numeric(coverage["requested_sessions"], errors="raise") == sessions).all():
        raise ValueError("per-symbol session count differs from collector calendar")
    if coverage["truncated"].any():
        raise ValueError("factor collection hit provider row cap")

    factors["coverage_complete"] = factors["coverage_complete"].map(
        lambda value: _flag(value, field="coverage_complete")
    )
    factors["as_of"] = factors["as_of"].map(
        lambda value: _timestamp(value, field="factor as_of")
    )
    factors["trade_date"] = factors["trade_date"].map(_date)
    factors["adj_factor"] = pd.to_numeric(factors["adj_factor"], errors="raise")
    if not factors["source"].eq(collection_source).all():
        raise ValueError("factor source differs from collector report")
    as_of_by_symbol = coverage.set_index("symbol")["as_of"].to_dict()
    date_responses: dict[str, Any] = {}
    if query_mode == "trade_date_market_batch":
        raw_responses = report.get("date_responses")
        raw_sessions = calendar.get("sessions") if isinstance(calendar, dict) else None
        if not isinstance(raw_responses, dict) or not isinstance(raw_sessions, list):
            raise ValueError("daily batch report is missing date-level response evidence")
        sessions_by_date = {_date(item).isoformat() for item in raw_sessions}
        if len(sessions_by_date) != sessions or set(raw_responses) != {
            _date(item).strftime("%Y%m%d") for item in raw_sessions
        }:
            raise ValueError("daily batch response dates differ from the verified calendar")
        for day, response in raw_responses.items():
            if not isinstance(response, dict):
                raise ValueError("daily batch response evidence is malformed")
            if response.get("batch_complete") is not True or response.get("truncated") is True:
                raise ValueError("daily batch contains an incomplete or truncated response")
            _timestamp(response.get("as_of"), field="daily batch response as_of")
            digest = str(response.get("response_sha256") or "")
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("daily batch response hash is malformed")
            response_rows = int(response.get("rows_returned", -1))
            if (
                response_rows < 3_000
                or response_rows >= 6_000
                or int(response.get("unique_source_symbols", -1)) != response_rows
            ):
                raise ValueError("daily batch response row counts are inconsistent")
            missing_symbols = response.get("missing_requested_symbols")
            if (
                not isinstance(missing_symbols, list)
                or missing_symbols != sorted(set(str(item) for item in missing_symbols))
                or not set(missing_symbols) <= set(symbols)
            ):
                raise ValueError("daily batch missing-symbol evidence is malformed")
            day_value = _date(day)
            returned_symbols = set(
                factors.loc[factors["trade_date"] == day_value, "symbol"].astype(str)
            )
            expected_missing = sorted(set(symbols) - returned_symbols)
            if missing_symbols != expected_missing:
                raise ValueError("daily batch missing-symbol evidence differs from factor rows")
        date_responses = raw_responses
        if not factors.empty and not factors.apply(
            lambda row: row["as_of"] == _timestamp(
                date_responses[row["trade_date"].strftime("%Y%m%d")].get("as_of"),
                field="daily batch response as_of",
            ),
            axis=1,
        ).all():
            raise ValueError("factor response time differs from its trade-date batch")
    elif not factors.apply(
        lambda row: row["as_of"] == as_of_by_symbol[row["symbol"]], axis=1
    ).all():
        raise ValueError("factor response time differs from its symbol coverage")
    if ((factors["trade_date"] < start) | (factors["trade_date"] > end)).any():
        raise ValueError("factor CSV contains an out-of-scope date")
    if not factors["adj_factor"].map(lambda value: math.isfinite(float(value)) and value > 0).all():
        raise ValueError("factor CSV contains invalid values")
    raw_complete_by_symbol = coverage.set_index("symbol")["coverage_complete"].to_dict()
    invalid_values_by_symbol = report.get("symbols_with_invalid_factor_values") or {}
    unexpected_codes_by_symbol = report.get("symbols_with_unexpected_source_codes") or {}
    observation_complete_by_symbol: dict[str, bool] = {}
    for row in coverage.itertuples(index=False):
        symbol = str(row.symbol)
        duplicate_dates = json.loads(str(row.duplicate_dates))
        out_of_range_dates = json.loads(str(row.out_of_range_dates))
        if not isinstance(duplicate_dates, list) or not isinstance(out_of_range_dates, list):
            raise ValueError("factor coverage date diagnostics are malformed")
        observation_complete_by_symbol[symbol] = bool(
            not row.truncated
            and not row.error
            and not duplicate_dates
            and not out_of_range_dates
            and not invalid_values_by_symbol.get(symbol)
            and not unexpected_codes_by_symbol.get(symbol)
        )
    for symbol, group in factors.groupby("symbol"):
        range_complete = bool(raw_complete_by_symbol[str(symbol)])
        row_flags = set(bool(value) for value in group["coverage_complete"])
        expected_row_flag = observation_complete_by_symbol[str(symbol)]
        # Older collector bundles copied symbol-range completeness onto every
        # observation. Accept that exact legacy encoding only after the response
        # hash is checked below; store the corrected row-level meaning.
        if row_flags not in ({expected_row_flag}, {range_complete}):
            raise ValueError("factor observation completeness is inconsistent")
        factors.loc[factors["symbol"] == symbol, "coverage_complete"] = expected_row_flag
    counts = factors.groupby("symbol").size().to_dict()
    factors_by_symbol = {
        symbol: set(group["trade_date"].tolist()) for symbol, group in factors.groupby("symbol")
    }
    effective_complete_by_symbol = dict(raw_complete_by_symbol)
    listing_prefix_exceptions: dict[str, str] = {}
    import duckdb

    try:
        metadata = duckdb.connect(str(db_path), read_only=True)
        try:
            for row in coverage.itertuples(index=False):
                if bool(row.coverage_complete):
                    continue
                missing_sessions = json.loads(str(row.missing_sessions))
                if not isinstance(missing_sessions, list) or not missing_sessions:
                    continue
                if row.error or bool(row.truncated):
                    continue
                listing_rows = metadata.execute(
                    "SELECT effective_from, effective_to, as_of FROM ref_security_status "
                    "WHERE symbol = ? AND status_type = 'listing_period' "
                    "AND source = 'tushare_stock_basic_namechange' "
                    "AND coverage_complete = TRUE",
                    [row.symbol],
                ).fetchall()
                if len(listing_rows) != 1:
                    continue
                listing_date = _date(listing_rows[0][0])
                listing_end = _date(listing_rows[0][1]) if listing_rows[0][1] else None
                listing_as_of = str(listing_rows[0][2] or "")[:10]
                if (
                    listing_end is not None
                    or listing_date <= start
                    or listing_date > end
                    or not listing_as_of
                    or listing_as_of > str(row.as_of)[:10]
                ):
                    continue
                try:
                    missing_dates = {_date(item) for item in missing_sessions}
                except ValueError:
                    continue
                if any(day >= listing_date for day in missing_dates):
                    continue
                expected_active = {
                    _date(item[0])
                    for item in metadata.execute(
                        "SELECT trade_date FROM ref_trade_calendar "
                        "WHERE is_open = TRUE AND trade_date >= ? AND trade_date <= ?",
                        [listing_date, end],
                    ).fetchall()
                }
                if (
                    not expected_active
                    or factors_by_symbol.get(row.symbol, set()) != expected_active
                ):
                    continue
                if int(row.rows_returned) != len(expected_active):
                    continue
                effective_complete_by_symbol[row.symbol] = True
                listing_prefix_exceptions[row.symbol] = listing_date.isoformat()
        finally:
            metadata.close()
    except duckdb.Error:
        # A missing/invalid local calendar or listing table cannot justify relaxing coverage.
        pass

    for row in coverage.itertuples(index=False):
        effective_complete = effective_complete_by_symbol[row.symbol]
        expected_count = sessions
        if row.symbol in listing_prefix_exceptions:
            expected_count = len(factors_by_symbol[row.symbol])
        if effective_complete and int(counts.get(row.symbol, 0)) != expected_count:
            raise ValueError("complete symbol does not have one factor per applicable open session")
        source_rows = factors.loc[
            factors["symbol"] == row.symbol, ["ts_code", "trade_date", "adj_factor"]
        ].copy()
        source_rows["trade_date"] = source_rows["trade_date"].map(
            lambda value: value.strftime("%Y%m%d")
        )
        # Preserve provider response order: the collection hash is over the
        # original response row order (Tushare commonly returns newest first).
        digest = hashlib.sha256(
            source_rows.to_csv(index=False, lineterminator="\n").encode("utf-8")
        ).hexdigest()
        if digest != str(row.response_sha256):
            raise ValueError("factor rows do not match the collector response hash")
    if int(report.get("completed_symbols", -1)) != int(coverage["coverage_complete"].sum()):
        raise ValueError("complete symbol count differs from collector report")
    if int(report.get("rows_total", -1)) != len(factors):
        raise ValueError("factor row count differs from collector report")
    reported_incomplete = set(str(item) for item in (report.get("incomplete_symbols") or {}))
    actual_incomplete = set(coverage.loc[~coverage["coverage_complete"], "symbol"].tolist())
    if reported_incomplete != actual_incomplete:
        raise ValueError("incomplete symbol identities differ from collector report")

    collection_complete = (
        bool(effective_complete_by_symbol)
        and all(effective_complete_by_symbol.values())
        and not bool(coverage["truncated"].any())
        and not bool(coverage["error"].astype(bool).any())
    )
    if not collection_complete and not allow_partial:
        raise ValueError(
            "factor collection is incomplete; pass --allow-partial to import proven rows"
        )

    hash_by_symbol = coverage.set_index("symbol")["response_sha256"].to_dict()
    factors["response_sha256"] = factors["symbol"].map(hash_by_symbol)
    factors["trade_date"] = factors["trade_date"].map(date.isoformat)
    # Keep each validated observation usable even if another session in the
    # same requested range is absent. The missing dates remain absent and the
    # bundle-level report above remains incomplete.
    factors["coverage_complete"] = factors["symbol"].map(
        observation_complete_by_symbol
    ).astype(bool)

    with TailReferenceStore(db_path) as reference:
        reference._con.execute("BEGIN TRANSACTION")
        try:
            written = reference.upsert_adj_factors(
                factors,
                source=collection_source,
                as_of=collection_as_of,
            )
            reference._con.execute("COMMIT")
        except Exception:
            reference._con.execute("ROLLBACK")
            raise
        readiness = reference.coverage()["sources"]["adj_factor_history"]

    return {
        "db": str(db_path),
        "collection_complete": collection_complete,
        "requested_symbols": len(symbols),
        "collector_complete_symbols": int(coverage["coverage_complete"].sum()),
        "complete_symbols": sum(effective_complete_by_symbol.values()),
        "complete_observations": int(factors["coverage_complete"].sum()),
        "incomplete_symbols": sorted(
            symbol for symbol, complete in effective_complete_by_symbol.items() if not complete
        ),
        "listing_prefix_coverage_resolutions": listing_prefix_exceptions,
        "factor_rows_written": written,
        "scope_manifest_sha256": _sha256(scope_manifest_path),
        "requests_sha256": _sha256(requests_path),
        "symbols_sha256": _sha256(symbols_path),
        "factor_csv_sha256": _sha256(factors_path),
        "coverage_csv_sha256": _sha256(coverage_path),
        "collector_report_sha256": _sha256(report_path),
        "adj_factor_coverage": readiness,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="existing local research DuckDB")
    parser.add_argument("--factors-csv", required=True)
    parser.add_argument("--coverage-csv", required=True)
    parser.add_argument("--collector-report", required=True)
    parser.add_argument("--scope-manifest", required=True)
    parser.add_argument("--symbols-file", required=True)
    parser.add_argument("--requests", required=True)
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)
    try:
        report = sync_bundle(
            db_path=Path(args.db),
            factors_path=Path(args.factors_csv),
            coverage_path=Path(args.coverage_csv),
            report_path=Path(args.collector_report),
            scope_manifest_path=Path(args.scope_manifest),
            symbols_path=Path(args.symbols_file),
            requests_path=Path(args.requests),
            allow_partial=args.allow_partial,
        )
    except (OSError, ValueError, TailReferenceError) as exc:
        print(f"adj_factor import failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 5
    if args.report:
        Path(args.report).expanduser().write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["collection_complete"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
