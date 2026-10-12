#!/usr/bin/env python3
"""Add auditable session markers where full RAW minutes prove a stock traded.

The source database remains read-only. The output is a new copy. A marker is added
only when a missing daily row can be proven from one complete 241-bar RAW session,
positive traded volume, an exact limit row, and a complete pointwise adjustment
factor. The marker deliberately has NULL OHLC: labels use RAW minutes for every
price decision, and a partial intraday aggregate must not masquerade as a daily bar.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.research.minute_bar_store import MinuteBarStore  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _row_sha256(rows: list[tuple[Any, ...]]) -> str:
    payload = [
        [value.isoformat() if isinstance(value, (date, datetime)) else value for value in row]
        for row in rows
    ]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _complete_traded_markers(con: Any, *, source_sha256: str) -> list[dict[str, Any]]:
    missing_keys = con.execute(
        """
        SELECT DISTINCT m.symbol, m.trade_date
        FROM minute_bars_1min AS m
        LEFT JOIN ref_daily_bars_raw AS d USING (symbol, trade_date)
        WHERE d.symbol IS NULL AND m.price_basis='raw'
        ORDER BY m.symbol, m.trade_date
        """
    ).fetchall()
    markers: list[dict[str, Any]] = []
    for symbol, day in missing_keys:
        rows = con.execute(
            """
            SELECT bar_time, open, high, low, close, volume, price_basis,
                   bar_time_semantics, source
            FROM minute_bars_1min
            WHERE symbol=? AND trade_date=? AND price_basis='raw'
            ORDER BY bar_time
            """,
            [symbol, day],
        ).fetchall()
        bars = [
            (row[0], {
                "open": row[1], "high": row[2], "low": row[3], "close": row[4],
                "volume": row[5], "price_basis": row[6],
                "source_bar_time_semantics": row[7], "source": row[8],
            })
            for row in rows
        ]
        coverage = MinuteBarStore.minute_session_coverage(bars, trading_day=day)
        if not coverage["complete"]:
            continue
        if any(row[6] != "raw" or row[8] != "vendor_zip" for row in rows):
            continue
        volumes = [float(row[5]) for row in rows if row[5] is not None]
        valid_volumes = all(math.isfinite(value) and value >= 0 for value in volumes)
        if len(volumes) != len(rows) or not valid_volumes:
            continue
        total_volume = sum(volumes)
        if total_volume <= 0:
            continue

        limit = con.execute(
            "SELECT up_limit, down_limit, approximated, coverage_complete "
            "FROM ref_limit_prices WHERE symbol=? AND trade_date=?",
            [symbol, day],
        ).fetchone()
        factor = con.execute(
            "SELECT adj_factor, coverage_complete FROM ref_adj_factor_history "
            "WHERE symbol=? AND trade_date=?",
            [symbol, day],
        ).fetchone()
        if (
            limit is None or limit[0] is None or limit[1] is None or limit[2] or not limit[3]
            or factor is None or factor[0] is None or factor[0] <= 0 or not factor[1]
        ):
            continue

        row_hash = _row_sha256(rows)
        as_of = (
            f"derived_from_complete_raw_1m; source_db_sha256={source_sha256}; "
            f"minute_rows_sha256={row_hash}; count={len(rows)}; volume={total_volume:g}"
        )
        markers.append({
            "symbol": str(symbol),
            "trade_date": day,
            "rows": rows,
            "volume": total_volume,
            "row_sha256": row_hash,
            "as_of": as_of,
            "source": "complete_raw_1m_traded_session_marker",
            "bar_coverage": coverage,
        })
    return markers


def augment_database(source: Path, output: Path) -> dict[str, Any]:
    import duckdb

    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"source database does not exist: {source}")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing output database: {output}")
    if source == output:
        raise ValueError("source and output database must differ")

    source_hash = _sha256(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, output)
    con = duckdb.connect(str(output))
    try:
        markers = _complete_traded_markers(con, source_sha256=source_hash)
        con.execute("BEGIN TRANSACTION")
        for marker in markers:
            # This row declares a session only. The OHLC fields stay NULL so no consumer
            # can mistake an intraday reconstruction for the authoritative daily series.
            con.execute(
                """
                INSERT INTO ref_daily_bars_raw (
                    symbol, trade_date, open, high, low, close, volume, turnover,
                    float_market_cap, name, is_st, is_delisting_risk, board,
                    price_basis, source, as_of, ingested_at
                ) VALUES (?, ?, NULL, NULL, NULL, NULL, ?, NULL, NULL, NULL, NULL, NULL,
                          NULL, 'raw', ?, ?, CURRENT_TIMESTAMP)
                """,
                [marker["symbol"], marker["trade_date"], marker["volume"],
                 marker["source"], marker["as_of"]],
            )
            con.execute(
                """
                INSERT INTO ref_suspend_status (
                    symbol, trade_date, suspended, suspend_type, trade_status, source,
                    as_of, coverage_complete, ingested_at
                ) VALUES (?, ?, FALSE, NULL, 'normal', ?, ?, TRUE, CURRENT_TIMESTAMP)
                """,
                [marker["symbol"], marker["trade_date"], marker["source"], marker["as_of"]],
            )
        con.execute("COMMIT")
        remaining = int(con.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT DISTINCT m.symbol, m.trade_date
                FROM minute_bars_1min AS m
                LEFT JOIN ref_daily_bars_raw AS d USING (symbol, trade_date)
                WHERE d.symbol IS NULL AND m.price_basis='raw'
            )
            """
        ).fetchone()[0])
        total_rows = int(con.execute("SELECT COUNT(*) FROM ref_daily_bars_raw").fetchone()[0])
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 - preserve original exception
            pass
        raise
    finally:
        con.close()

    manifest = {
        "schema": "tail_daily_reference_minute_markers.v1",
        "source_database": str(source),
        "source_database_sha256": source_hash,
        "source_database_size_bytes": source.stat().st_size,
        "output_database": str(output),
        "output_database_size_bytes": output.stat().st_size,
        "source_read_only": True,
        "daily_reference_rows_before": total_rows - len(markers),
        "daily_reference_rows_after": total_rows,
        "inserted_traded_session_markers": len(markers),
        "remaining_minute_keys_without_daily_reference": remaining,
        "price_fields": "NULL; marker is for session/status only, exits remain RAW 1m",
        "eligibility": (
            "complete 241-slot RAW vendor_zip minute session; valid prices; positive total volume; "
            "exact complete limit prices; complete positive adj_factor"
        ),
        "markers": [
            {
                **{key: marker[key] for key in (
                    "symbol", "source", "as_of", "row_sha256", "volume", "bar_coverage",
                )},
                "trade_date": marker["trade_date"].isoformat(),
            }
            for marker in markers
        ],
    }
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)
    try:
        manifest = augment_database(Path(args.source_db), Path(args.out))
    except (OSError, ValueError, FileExistsError) as exc:
        print(f"minute session marker build failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 5
    manifest_path = Path(args.manifest).expanduser()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2, default=str)
    manifest_path.write_text(manifest_text, encoding="utf-8")
    print(manifest_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
