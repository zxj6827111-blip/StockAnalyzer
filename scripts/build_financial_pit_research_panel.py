#!/usr/bin/env python3
"""Build an immutable PIT financial panel from an existing research warehouse.

The source warehouse is opened read-only.  Financial fields are re-materialized
through ``financial_snapshots.ann_date`` into a new DuckDB file; the source
``daily_bars`` rows are never updated.  The output preserves the date-only
availability precision of the source and explicitly reports same-day rows whose
publication clock is unavailable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.data.financial_pit import apply_financial_snapshots_asof_batch  # noqa: E402

PANEL_COLUMNS = (
    "symbol",
    "date",
    "roe",
    "debt_ratio",
    "financial_data_complete",
    "financial_missing_fields",
    "financial_report_date",
    "financial_as_of",
    "financial_source",
    "financial_trust_level",
    "financial_completeness",
)
SNAPSHOT_COLUMNS = (
    "symbol",
    "end_date",
    "ann_date",
    "roe",
    "debt_ratio",
    "update_flag",
    "financial_report_date",
    "financial_as_of",
    "financial_source",
    "financial_trust_level",
    "financial_missing_fields",
    "financial_data_complete",
    "financial_completeness",
    "coverage_complete",
    "as_of",
    "source",
)


def _hash_frame(frame: pd.DataFrame, *, date_columns: tuple[str, ...] = ()) -> str:
    normalized = frame.copy()
    for column in date_columns:
        if column in normalized.columns:
            normalized[column] = pd.to_datetime(
                normalized[column], errors="coerce"
            ).dt.strftime("%Y-%m-%d")
    normalized = normalized.sort_values(
        list(normalized.columns), kind="mergesort", na_position="first"
    ).reset_index(drop=True)
    payload = normalized.to_csv(
        index=False, lineterminator="\n", na_rep="<NULL>"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_panel_frame(
    daily_keys: pd.DataFrame,
    snapshots: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Rebuild daily financial values with an announcement-date as-of join."""
    if daily_keys.empty:
        raise ValueError("source daily_bars selection is empty")
    if snapshots.empty:
        raise ValueError("source financial_snapshots selection is empty")
    if not {"symbol", "date"}.issubset(daily_keys.columns):
        raise ValueError("daily keys must contain symbol and date")
    missing_snapshot_columns = sorted(set(SNAPSHOT_COLUMNS) - set(snapshots.columns))
    if missing_snapshot_columns:
        raise ValueError(
            f"financial_snapshots missing columns: {missing_snapshot_columns}"
        )

    daily = daily_keys[["symbol", "date"]].copy()
    daily["symbol"] = daily["symbol"].astype(str)
    daily["date"] = pd.to_datetime(daily["date"], errors="coerce")
    if daily["date"].isna().any():
        raise ValueError("daily_bars contains invalid dates in requested range")
    if daily.duplicated(["symbol", "date"]).any():
        raise ValueError("daily_bars has duplicate (symbol, date) keys")

    snapshot_frame = snapshots.loc[:, list(SNAPSHOT_COLUMNS)].copy()
    snapshot_frame["symbol"] = snapshot_frame["symbol"].astype(str)
    raw_snapshot_hash = _hash_frame(
        snapshot_frame, date_columns=("ann_date", "end_date")
    )
    snapshot_frame["ann_date"] = pd.to_datetime(
        snapshot_frame["ann_date"], errors="coerce"
    )
    snapshot_frame["end_date"] = pd.to_datetime(
        snapshot_frame["end_date"], errors="coerce"
    )
    invalid_periods = (
        snapshot_frame["ann_date"].isna()
        | snapshot_frame["end_date"].isna()
        | (snapshot_frame["end_date"] > snapshot_frame["ann_date"])
    )
    invalid_snapshot_rows = snapshot_frame.loc[
        invalid_periods, ["symbol", "end_date", "ann_date"]
    ].copy()
    if invalid_periods.any():
        snapshot_frame = snapshot_frame.loc[~invalid_periods].copy()
    if snapshot_frame.empty:
        raise ValueError("no valid announcement-dated financial snapshots remain")

    enriched = apply_financial_snapshots_asof_batch(
        daily, snapshot_frame, only_fill_pending=False
    )
    panel = enriched.loc[:, list(PANEL_COLUMNS)].copy()
    panel["symbol"] = panel["symbol"].astype(str)
    panel["date"] = pd.to_datetime(panel["date"], errors="coerce").dt.date
    panel["financial_as_of"] = pd.to_datetime(
        panel["financial_as_of"], errors="coerce"
    ).dt.date
    panel["financial_report_date"] = pd.to_datetime(
        panel["financial_report_date"], errors="coerce"
    ).dt.date

    future_availability = int(
        (panel["financial_as_of"].notna()
         & (panel["financial_as_of"] > panel["date"])).sum()
    )
    future_report_period = int(
        (panel["financial_report_date"].notna()
         & (panel["financial_report_date"] > panel["date"])).sum()
    )
    if future_availability or future_report_period:
        raise ValueError(
            "PIT join produced future financial data: "
            f"ann_date={future_availability}, end_date={future_report_period}"
        )

    asof = panel["financial_as_of"]
    daily_dates = panel["date"]
    same_day_availability = int((asof.notna() & (asof == daily_dates)).sum())
    matched = int(asof.notna().sum())
    report = {
        "daily_rows": int(len(panel)),
        "symbols": int(panel["symbol"].nunique()),
        "first_date": str(panel["date"].min()),
        "last_date": str(panel["date"].max()),
        "matched_snapshot_rows": matched,
        "unmatched_rows": int(len(panel) - matched),
        "financial_complete_rows": int(
            panel["financial_data_complete"].fillna(False).astype(bool).sum()
        ),
        "financial_as_of_same_day_rows": same_day_availability,
        "financial_as_of_future_rows": future_availability,
        "financial_report_date_future_rows": future_report_period,
        "raw_source_hash": raw_snapshot_hash,
        "usable_source_hash": _hash_frame(
            snapshot_frame, date_columns=("ann_date", "end_date")
        ),
        "invalid_snapshot_rows_excluded": int(len(invalid_snapshot_rows)),
        "invalid_snapshot_sample": [
            {
                "symbol": str(row.symbol),
                "end_date": str(row.end_date),
                "ann_date": str(row.ann_date),
            }
            for row in invalid_snapshot_rows.head(10).itertuples(index=False)
        ],
        "daily_keys_hash": _hash_frame(daily, date_columns=("date",)),
        "same_day_timestamp_limitation": (
            "ann_date is date-only; the source does not prove an intraday publication "
            "time for same-day announcements"
        ),
    }
    return panel, report


def build_research_panel(
    *, source_db: Path, output_db: Path, start: date, end: date
) -> dict[str, Any]:
    if end < start:
        raise ValueError("end must not precede start")
    source_db = source_db.expanduser().resolve()
    output_db = output_db.expanduser().resolve()
    if not source_db.is_file():
        raise FileNotFoundError(source_db)
    if output_db.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output_db}")
    output_db.parent.mkdir(parents=True, exist_ok=True)
    temp_db = output_db.with_name(output_db.name + ".building")
    if temp_db.exists():
        raise FileExistsError(f"refusing to overwrite existing temporary output: {temp_db}")

    source = duckdb.connect(str(source_db), read_only=True)
    try:
        daily_keys = source.execute(
            "SELECT symbol, date FROM daily_bars WHERE date BETWEEN ? AND ? "
            "ORDER BY symbol, date",
            [start, end],
        ).fetchdf()
        snapshots = source.execute(
            "SELECT " + ", ".join(SNAPSHOT_COLUMNS) + " FROM financial_snapshots "
            "ORDER BY symbol, ann_date, end_date, update_flag"
        ).fetchdf()
    finally:
        source.close()

    panel, summary = build_panel_frame(daily_keys, snapshots)
    manifest = {
        "schema": "financial_pit_research_panel_v1",
        "source_database": str(source_db),
        "source_database_bytes": source_db.stat().st_size,
        "source_database_mtime_ns": source_db.stat().st_mtime_ns,
        "source_tables": ["daily_bars(symbol,date)", "financial_snapshots"],
        "date_range": {"start": start.isoformat(), "end": end.isoformat()},
        "asof_rule": (
            "latest source snapshot with ann_date <= daily_bars.date; same-day is "
            "accepted by the existing financial_pit contract"
        ),
        "timestamp_precision": "date_only",
        "financial_snapshot_rows": int(len(snapshots)),
        "summary": summary,
    }

    created_temp = False
    try:
        target = duckdb.connect(str(temp_db))
        created_temp = True
        try:
            target.register("pit_panel_frame", panel)
            target.execute(
                "CREATE TABLE pit_financial_panel AS SELECT * FROM pit_panel_frame"
            )
            target.execute(
                "CREATE TABLE audit_manifest (manifest_json VARCHAR NOT NULL)"
            )
            target.execute(
                "INSERT INTO audit_manifest VALUES (?)",
                [json.dumps(manifest, ensure_ascii=False, sort_keys=True)],
            )
            target.execute("CREATE UNIQUE INDEX pit_financial_panel_key "
                           "ON pit_financial_panel(symbol, date)")
        finally:
            target.close()
        temp_db.replace(output_db)
    except Exception:
        if created_temp and temp_db.exists():
            temp_db.unlink()
        raise
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    args = parser.parse_args(argv)
    report = build_research_panel(
        source_db=args.source_db,
        output_db=args.out,
        start=args.start,
        end=args.end,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
