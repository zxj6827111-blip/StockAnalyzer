#!/usr/bin/env python3
"""Materialize a request-scoped, read-only-derived DuckDB for historical label rebuilds.

The production-like research database is often a market-wide 30+ GB file. The rebuild
consumer performs many symbol/day lookups, so running it directly against that file can
rescan the full minute table for every request. This utility scans each source table once,
copies only requested symbols and their date windows into a separate local DuckDB, and adds
lookup indexes there. The source file is attached read-only and is never modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.contracts.trend_strategy import DEFAULT_TREND_CONTRACT  # noqa: E402
from stock_analyzer.research.minute_bar_store import MINUTE_TABLES  # noqa: E402
from stock_analyzer.research.minute_bar_store import _ddl as _minute_ddl  # noqa: E402
from stock_analyzer.research.tail_rebuild import RebuildRequest, exit_window_end  # noqa: E402
from stock_analyzer.research.tail_reference_store import (  # noqa: E402
    ADJ_FACTOR_TABLE,
    REFERENCE_TABLES,
    _adj_factor_ddl,  # noqa: E402
)
from stock_analyzer.research.tail_reference_store import _ddl as _reference_ddl  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_requests(path: Path) -> tuple[set[str], date, date]:
    symbols: set[str] = set()
    start: date | None = None
    end: date | None = None
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            request = RebuildRequest(
                symbol=str(row["symbol"]).strip().upper().split(".", 1)[0],
                decision_date=date.fromisoformat(str(row["decision_date"])[:10]),
                entry_date=date.fromisoformat(str(row["entry_date"])[:10]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid request at line {line_number}: {exc}") from exc
        symbols.add(request.symbol)
        start = request.entry_date if start is None else min(start, request.entry_date)
        request_end = exit_window_end(request.entry_date, DEFAULT_TREND_CONTRACT)
        end = request_end if end is None else max(end, request_end)
    if not symbols or start is None or end is None:
        raise ValueError("request file has no valid rows")
    return symbols, start, end


def materialize_subset(source: Path, requests_path: Path, output: Path) -> dict[str, Any]:
    import duckdb

    source = source.expanduser().resolve()
    requests_path = requests_path.expanduser().resolve()
    output = output.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"source database does not exist: {source}")
    if not requests_path.is_file():
        raise FileNotFoundError(f"request file does not exist: {requests_path}")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing subset database: {output}")

    symbols, start, end = _read_requests(requests_path)
    factor_start = start - timedelta(days=31)
    output.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(output))
    copied: dict[str, int] = {}
    try:
        con.execute("SET threads=8")
        source_sql_path = source.as_posix().replace("'", "''")
        con.execute(f"ATTACH '{source_sql_path}' AS source_db (READ_ONLY)")
        con.register("requested_symbols", pd.DataFrame({"symbol": sorted(symbols)}))
        present = {
            str(row[0]) for row in con.execute(
                "SELECT table_name FROM duckdb_tables() WHERE database_name='source_db'"
            ).fetchall()
        }
        table_specs: list[tuple[str, str, str]] = []
        for table in MINUTE_TABLES.values():
            table_specs.append((
                table,
                "JOIN requested_symbols USING (symbol) "
                "WHERE trade_date BETWEEN ? AND ? AND price_basis='raw'",
                "symbol, trade_date, bar_time",
            ))
        for table in REFERENCE_TABLES.values():
            if table == REFERENCE_TABLES["trade_calendar"]:
                where = "WHERE trade_date BETWEEN ? AND ?"
                order_by = "trade_date"
            elif table == REFERENCE_TABLES["security_status"]:
                where = (
                    "JOIN requested_symbols USING (symbol) "
                    "WHERE effective_from <= ? AND COALESCE(effective_to, DATE '9999-12-31') >= ?"
                )
                order_by = "symbol, effective_from, status_type"
            elif table == REFERENCE_TABLES["corporate_action_coverage"]:
                where = "WHERE ex_date BETWEEN ? AND ?"
                order_by = "ex_date"
            elif table == REFERENCE_TABLES["corporate_actions"]:
                where = (
                    "JOIN requested_symbols AS requested "
                    "ON split_part(src.symbol, '.', 1) = requested.symbol "
                    "WHERE ex_date BETWEEN ? AND ?"
                )
                order_by = "split_part(src.symbol, '.', 1), ex_date, action_id"
            else:
                where = (
                    "JOIN requested_symbols USING (symbol) "
                    "WHERE trade_date BETWEEN ? AND ?"
                )
                order_by = "symbol, trade_date"
            table_specs.append((table, where, order_by))
        table_specs.append((
            ADJ_FACTOR_TABLE,
            "JOIN requested_symbols USING (symbol) WHERE trade_date BETWEEN ? AND ?",
            "symbol, trade_date",
        ))

        physical_ordering: dict[str, str] = {}
        for table, where, _date_field in table_specs:
            if table not in present:
                continue
            if table == REFERENCE_TABLES["security_status"]:
                params: list[Any] = [end, start]
            elif table == REFERENCE_TABLES["trade_calendar"]:
                params = [factor_start, end]
            elif table in {
                REFERENCE_TABLES["corporate_action_coverage"], ADJ_FACTOR_TABLE,
            }:
                params = [factor_start, end]
            else:
                params = [start, end]
            print(f"copying {table}", flush=True)
            projection = "src.*"
            if table == REFERENCE_TABLES["corporate_actions"]:
                projection = "src.* REPLACE (split_part(src.symbol, '.', 1) AS symbol)"
            if table in MINUTE_TABLES.values():
                con.execute(_minute_ddl(table))
            elif table == ADJ_FACTOR_TABLE:
                con.execute(_adj_factor_ddl())
            else:
                con.execute(_reference_ddl(table))
            order_by = _date_field
            physical_ordering[table] = order_by
            con.execute(
                f"INSERT INTO main.{table} "
                f"SELECT {projection} FROM source_db.main.{table} AS src {where} "
                f"ORDER BY {order_by}",
                params,
            )
            copied[table] = int(con.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0])

        for table, columns in (
            (MINUTE_TABLES["1m"], "symbol, trade_date"),
            (MINUTE_TABLES["5m"], "symbol, trade_date"),
            (REFERENCE_TABLES["daily_bars"], "symbol, trade_date"),
            (REFERENCE_TABLES["limit_prices"], "symbol, trade_date"),
            (REFERENCE_TABLES["suspend_status"], "symbol, trade_date"),
            (REFERENCE_TABLES["corporate_actions"], "symbol, ex_date"),
            (ADJ_FACTOR_TABLE, "symbol, trade_date"),
        ):
            if copied.get(table, 0):
                print(f"indexing {table}", flush=True)
                con.execute(f"CREATE INDEX idx_{table}_lookup ON {table} ({columns})")

        manifest = {
            "schema": "tail_rebuild_subset.v1",
            "source_database": str(source),
            "source_database_size_bytes": source.stat().st_size,
            "source_database_mtime_ns": source.stat().st_mtime_ns,
            "requests_path": str(requests_path),
            "requests_sha256": _sha256(requests_path),
            "symbols": len(symbols),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "factor_history_start": factor_start.isoformat(),
            "tables": copied,
            "source_read_only": True,
            "physical_ordering": physical_ordering,
            "output_database": str(output),
        }
        return manifest
    finally:
        try:
            con.unregister("requested_symbols")
        except Exception:  # noqa: BLE001 - preserve the original failure
            pass
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--requests", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--manifest", default="")
    args = parser.parse_args(argv)
    try:
        manifest = materialize_subset(
            Path(args.source_db), Path(args.requests), Path(args.out),
        )
    except (OSError, ValueError, FileExistsError) as exc:
        print(f"subset build failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 5
    if args.manifest:
        path = Path(args.manifest).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
