#!/usr/bin/env python3
"""Re-import hash-bound adj_factor observations without range-wide flag leakage.

This migration is deliberately narrower than the normal sync command: it only
corrects rows already present in an isolated research database, and refuses to
add or remove any symbol/date observation.
"""

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

SOURCE = "tushare_adj_factor_history"
FACTOR_COLUMNS = ("ts_code", "trade_date", "adj_factor")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _symbol(value: Any) -> str:
    text = str(value or "").strip().upper().split(".", 1)[0]
    if not re.fullmatch(r"\d{1,6}", text):
        raise ValueError("invalid factor symbol")
    return text.zfill(6)


def _date(value: Any) -> date:
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d").date()
    return date.fromisoformat(text)


def _digest_frame(frame: Any) -> str:
    payload = (
        frame.loc[:, list(FACTOR_COLUMNS)].to_csv(index=False, lineterminator="\n").encode("utf-8")
    )
    return hashlib.sha256(payload).hexdigest()


def reconcile_observations(
    *,
    target_db: Path,
    factors_path: Path,
    coverage_path: Path,
    report_path: Path,
    evidence_manifest_path: Path,
    symbols_path: Path,
    requests_path: Path,
    scope_manifest_path: Path,
    out_manifest_path: Path,
) -> dict[str, Any]:
    import duckdb
    import pandas as pd

    paths = {
        "target_db": target_db.expanduser().resolve(),
        "factors": factors_path.expanduser().resolve(),
        "coverage": coverage_path.expanduser().resolve(),
        "report": report_path.expanduser().resolve(),
        "evidence_manifest": evidence_manifest_path.expanduser().resolve(),
        "symbols": symbols_path.expanduser().resolve(),
        "requests": requests_path.expanduser().resolve(),
        "scope_manifest": scope_manifest_path.expanduser().resolve(),
        "out_manifest": out_manifest_path.expanduser().resolve(),
    }
    target = paths["target_db"]
    if target.suffix.lower() != ".duckdb" or "/vol1/docker/stockanalyzer" in str(target).lower():
        raise ValueError("target must be a non-production research DuckDB")
    if not target.is_file():
        raise ValueError("research DuckDB target does not exist")
    if paths["out_manifest"].exists():
        raise FileExistsError(f"refusing to overwrite evidence: {paths['out_manifest']}")
    for name, path in paths.items():
        if name not in {"target_db", "out_manifest"} and not path.is_file():
            raise FileNotFoundError(f"missing {name} input: {path}")

    evidence = json.loads(paths["evidence_manifest"].read_text(encoding="utf-8"))
    scope = json.loads(paths["scope_manifest"].read_text(encoding="utf-8"))
    report = json.loads(paths["report"].read_text(encoding="utf-8"))
    if evidence.get("schema") != "tail_adj_factor_missing7_collection_evidence.v1":
        raise ValueError("unsupported collector evidence manifest")
    if evidence.get("scope_manifest_sha256") != _sha256(paths["scope_manifest"]):
        raise ValueError("scope manifest differs from collector evidence")
    for name in ("factors", "coverage", "report"):
        expected = (
            (evidence.get("outputs") or {})
            .get(f"{name}.csv" if name != "report" else "report.json", {})
            .get("sha256")
        )
        if expected != _sha256(paths[name]):
            raise ValueError(f"{name} file differs from collector evidence")
    for input_name, path_name in (
        ("symbols", "symbols"),
        ("requests", "requests"),
        ("scope", "scope_manifest"),
    ):
        # The evidence bundle stores the original scope hash separately from the
        # scope document's own symbols-file convention.
        expected = (evidence.get("inputs") or {}).get(input_name, {}).get("sha256")
        if expected != _sha256(paths[path_name]):
            raise ValueError(f"{input_name} file differs from collector evidence")

    if scope.get("schema") != "tail_adj_factor_collection_manifest.v2":
        raise ValueError("unsupported factor collection scope")
    if scope.get("collector") != "scripts/collect_adj_factor_history.py":
        raise ValueError("collector identity mismatch")
    if scope.get("request_sha256") != _sha256(paths["requests"]):
        raise ValueError("request file differs from scope manifest")
    symbols = sorted(
        {
            _symbol(line.split(",", 1)[0])
            for line in paths["symbols"].read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    )
    normalized_symbols_sha = hashlib.sha256("\n".join(symbols).encode()).hexdigest()
    raw_symbols_sha = _sha256(paths["symbols"])
    # The existing audit emitted a raw newline-terminated file hash into the
    # collector's normalized-symbol field. Accept that exact legacy encoding
    # only because the enclosing evidence manifest binds the original bytes.
    if scope.get("symbols_sha256") not in {normalized_symbols_sha, raw_symbols_sha}:
        raise ValueError("symbols do not match collection scope")
    if len(symbols) != int(scope.get("symbols", -1)):
        raise ValueError("symbol count differs from collection scope")

    query_mode = report.get("query_mode")
    query_mode_source = "collector_report"
    if not query_mode:
        query_mode = evidence.get("query_mode")
        query_mode_source = "collector_evidence_manifest"
    if (
        report.get("source") != SOURCE
        or report.get("scope") != "requested_symbols_only"
        or query_mode != "symbol"
        or int(report.get("requested_symbols", -1)) != len(symbols)
    ):
        raise ValueError("collector report identity or scope mismatch")
    if report.get("symbols_failed") or report.get("symbols_at_or_above_row_cap"):
        raise ValueError("collector report contains failed or truncated symbol queries")
    if report.get("symbols_with_invalid_factor_values") or report.get(
        "symbols_with_unexpected_source_codes"
    ):
        raise ValueError("collector report contains malformed factor observations")

    coverage = pd.read_csv(paths["coverage"], dtype=str, keep_default_na=False)
    factors = pd.read_csv(paths["factors"], dtype=str, keep_default_na=False)
    required_coverage = {
        "symbol",
        "ts_code",
        "source",
        "as_of",
        "requested_sessions",
        "rows_returned",
        "missing_sessions",
        "duplicate_dates",
        "out_of_range_dates",
        "coverage_complete",
        "truncated",
        "error",
        "response_sha256",
    }
    required_factors = set(FACTOR_COLUMNS) | {"symbol", "source", "as_of", "coverage_complete"}
    if not required_coverage <= set(coverage.columns) or not required_factors <= set(
        factors.columns
    ):
        raise ValueError("collector CSV is missing required columns")
    coverage["symbol"] = coverage["symbol"].map(_symbol)
    factors["symbol"] = factors["symbol"].map(_symbol)
    if set(coverage["symbol"]) != set(symbols) or coverage["symbol"].duplicated().any():
        raise ValueError("coverage symbols do not match requested symbols")
    if not set(factors["symbol"]) == set(symbols):
        raise ValueError("factor rows do not cover exactly the requested symbols")
    if factors.duplicated(["symbol", "trade_date"]).any():
        raise ValueError("duplicate symbol/date factor observations")
    if not (coverage["source"] == SOURCE).all() or not (factors["source"] == SOURCE).all():
        raise ValueError("factor source identity mismatch")

    coverage_by_symbol = coverage.set_index("symbol").to_dict("index")
    report_incomplete = {
        _symbol(symbol): days for symbol, days in (report.get("incomplete_symbols") or {}).items()
    }
    actual_incomplete = {
        symbol
        for symbol, summary in coverage_by_symbol.items()
        if summary["coverage_complete"].lower() not in {"true", "1"}
    }
    if report_incomplete != {
        symbol: json.loads(coverage_by_symbol[symbol]["missing_sessions"])
        for symbol in sorted(actual_incomplete)
    }:
        raise ValueError("collector incomplete-symbol evidence differs from coverage rows")
    if int(report.get("rows_total", -1)) != len(factors):
        raise ValueError("collector row total differs from factor CSV")
    invalid_ranges: list[str] = []
    response_hashes: dict[str, str] = {}
    row_records: list[tuple[Any, ...]] = []
    for symbol in symbols:
        summary = coverage_by_symbol[symbol]
        group = factors.loc[factors["symbol"] == symbol]
        try:
            missing = json.loads(summary["missing_sessions"])
            duplicates = json.loads(summary["duplicate_dates"])
            out_of_range = json.loads(summary["out_of_range_dates"])
        except json.JSONDecodeError as exc:
            raise ValueError("coverage diagnostics are malformed") from exc
        if (
            not isinstance(missing, list)
            or not isinstance(duplicates, list)
            or not isinstance(out_of_range, list)
            or duplicates
            or out_of_range
            or summary["error"]
            or summary["truncated"].lower() not in {"false", "0"}
        ):
            invalid_ranges.append(symbol)
            continue
        if int(summary["rows_returned"]) != len(group):
            raise ValueError(f"collector row count mismatch for {symbol}")
        if int(summary["requested_sessions"]) - len(group) != len(missing):
            raise ValueError(f"missing-session count mismatch for {symbol}")
        digest = _digest_frame(group)
        if digest != summary["response_sha256"]:
            raise ValueError(f"factor observations differ from source response for {symbol}")
        response_hashes[symbol] = digest
        for row in group.to_dict("records"):
            day = datetime.strptime(row["trade_date"], "%Y%m%d").date()
            factor = float(row["adj_factor"])
            if not math.isfinite(factor) or factor <= 0:
                raise ValueError(f"invalid factor value for {symbol}")
            as_of = datetime.fromisoformat(row["as_of"].replace("Z", "+00:00"))
            if not as_of.tzinfo:
                raise ValueError(f"factor observation has a timezone-free as_of for {symbol}")
            row_records.append(
                (
                    symbol,
                    day,
                    factor,
                    SOURCE,
                    row["as_of"],
                    True,
                    digest,
                    datetime.now(),
                )
            )
    if invalid_ranges:
        raise ValueError(f"factor ranges contain unsupported failures: {sorted(invalid_ranges)}")
    if len(row_records) != len(factors):
        raise ValueError("not all factor rows were validated")

    # Require the target to already contain exactly these source observations.
    # This makes the operation a correction of row flags, never a data backfill.
    con = duckdb.connect(str(target))
    try:
        columns = {
            row[0]
            for row in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'ref_adj_factor_history'"
            ).fetchall()
        }
        if not {"symbol", "trade_date", "adj_factor", "source", "coverage_complete"} <= columns:
            raise ValueError("target has no compatible ref_adj_factor_history table")
        start_day = _date(scope["start"])
        end_day = _date(scope["end"])
        key_sql = ",".join("?" for _ in symbols)
        existing = con.execute(
            "SELECT symbol, trade_date, adj_factor, source FROM ref_adj_factor_history "
            "WHERE symbol IN (" + key_sql + ") "
            "AND trade_date BETWEEN ? AND ?",
            [*symbols, start_day, end_day],
        ).fetchall()
        existing_values = {(str(row[0]), row[1]): (float(row[2]), str(row[3])) for row in existing}
        incoming_values = {(row[0], row[1]): (row[2], row[3]) for row in row_records}
        if set(existing_values) != set(incoming_values):
            raise ValueError("target factor keys differ; refusing to add or remove observations")
        for key, (value, source) in incoming_values.items():
            old_value, old_source = existing_values[key]
            if (
                not math.isclose(old_value, value, rel_tol=0.0, abs_tol=1e-12)
                or old_source != source
            ):
                raise ValueError("target factor values differ from the collected observations")

        con.execute("BEGIN TRANSACTION")
        try:
            con.execute(
                "DELETE FROM ref_adj_factor_history WHERE symbol IN ("
                + key_sql
                + ") AND trade_date BETWEEN ? AND ?",
                [*symbols, start_day, end_day],
            )
            con.executemany(
                "INSERT INTO ref_adj_factor_history "
                "(symbol, trade_date, adj_factor, source, as_of, coverage_complete, "
                "response_sha256, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                row_records,
            )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        post = con.execute(
            "SELECT COUNT(*), BOOL_AND(coverage_complete) FROM ref_adj_factor_history "
            "WHERE symbol IN (" + key_sql + ") "
            "AND trade_date BETWEEN ? AND ?",
            [*symbols, start_day, end_day],
        ).fetchone()
        if int(post[0]) != len(row_records) or post[1] is not True:
            raise RuntimeError("post-write factor observation verification failed")
    finally:
        con.close()

    result = {
        "schema": "tail_adj_factor_observation_reconciliation.v1",
        "target_db": str(target),
        "target_symbols": symbols,
        "target_range": {"start": scope["start"], "end": scope["end"]},
        "row_level_complete_observations": len(row_records),
        "range_level_incomplete_symbols": sorted(
            symbol
            for symbol, summary in coverage_by_symbol.items()
            if summary["coverage_complete"].lower() not in {"true", "1"}
        ),
        "missing_source_sessions": {
            symbol: json.loads(coverage_by_symbol[symbol]["missing_sessions"])
            for symbol in symbols
            if json.loads(coverage_by_symbol[symbol]["missing_sessions"])
        },
        "response_sha256_by_symbol": response_hashes,
        "scope_symbol_hash_legacy_encoding": scope.get("symbols_sha256") == raw_symbols_sha,
        "inputs": {
            name: {"path": str(paths[name]), "sha256": _sha256(paths[name])}
            for name in (
                "factors",
                "coverage",
                "report",
                "evidence_manifest",
                "symbols",
                "requests",
                "scope_manifest",
            )
        },
        "collector": {
            "build_commit": evidence.get("collector_build_commit"),
            "image_id": evidence.get("collector_image_id"),
            "collector_sha256": evidence.get("collector_sha256"),
            "query_mode": query_mode,
            "query_mode_source": query_mode_source,
        },
    }
    result["script_sha256"] = _sha256(Path(__file__).resolve())
    paths["out_manifest"].parent.mkdir(parents=True, exist_ok=True)
    paths["out_manifest"].write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "target-db",
        "factors",
        "coverage",
        "report",
        "evidence-manifest",
        "symbols",
        "requests",
        "scope-manifest",
        "out-manifest",
    ):
        parser.add_argument(f"--{name}", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = reconcile_observations(
            target_db=args.target_db,
            factors_path=args.factors,
            coverage_path=args.coverage,
            report_path=args.report,
            evidence_manifest_path=args.evidence_manifest,
            symbols_path=args.symbols,
            requests_path=args.requests,
            scope_manifest_path=args.scope_manifest,
            out_manifest_path=args.out_manifest,
        )
    except Exception as exc:  # noqa: BLE001 - CLI must expose a real failure status.
        print(
            f"adj_factor observation reconcile blocked: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 5
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
