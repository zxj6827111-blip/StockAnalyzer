"""Build the read-only intraday summary DuckDB from vendor minute ZIPs.

The builder is intentionally offline-only. Runtime providers consume the resulting
DuckDB and never execute this ZIP path in production.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from stock_analyzer.data.intraday_summary_builder import (
    _BATCH_SYMBOLS,
    _SUPPORTED_INTERVALS,
    archive_paths,
    entry_symbol,
    flush_summaries,
    manifest_path,
    read_entry_summary,
)
from stock_analyzer.data.market_warehouse import MarketWarehouse


def _update_archive_fingerprint(
    *,
    root: Path,
    path: Path,
    infos: list[zipfile.ZipInfo],
    digest: Any,
    records: list[dict[str, Any]],
) -> None:
    """Record a ZIP fingerprint while the archive is already open for aggregation."""
    relative = path.relative_to(root).as_posix()
    stat = path.stat()
    digest.update(relative.encode("utf-8"))
    digest.update(str(stat.st_size).encode("ascii"))
    digest.update(str(stat.st_mtime_ns).encode("ascii"))
    digest.update(str(len(infos)).encode("ascii"))
    for info in infos:
        digest.update(
            f"{info.filename}\0{info.CRC}\0{info.file_size}\0{info.date_time}".encode(
                "utf-8", errors="replace"
            )
        )
    records.append(
        {
            "path": relative,
            "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
            "entries": len(infos),
        }
    )


def _build_interval(
    *,
    root: Path,
    warehouse: MarketWarehouse,
    interval: str,
    cutoff: date,
    volume_multiplier: float,
    amount_multiplier: float,
) -> dict[str, Any]:
    archives = archive_paths(root, interval, cutoff)
    if not archives:
        raise RuntimeError(f"no {interval} minute ZIP archives cover {cutoff.isoformat()}")
    symbols_seen: set[str] = set()
    rows: list[pd.DataFrame] = []
    rows_written = 0
    conflicts = 0
    entry_count = 0
    archive_count = 0
    fingerprint_digest = hashlib.sha256()
    fingerprint_records: list[dict[str, Any]] = []
    for archive_path in archives:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            _update_archive_fingerprint(
                root=root,
                path=archive_path,
                infos=infos,
                digest=fingerprint_digest,
                records=fingerprint_records,
            )
            grouped: dict[str, list[str]] = {}
            for info in infos:
                if info.is_dir() or info.filename.startswith("__MACOSX/"):
                    continue
                symbol = entry_symbol(info.filename)
                if not symbol:
                    continue
                grouped.setdefault(symbol, []).append(info.filename)
            if not grouped:
                continue
            archive_count += 1
            entry_count += sum(len(items) for items in grouped.values())
            for symbol, entry_names in sorted(grouped.items()):
                summary = read_entry_summary(
                    archive,
                    entry_names,
                    interval=interval,
                    cutoff=cutoff,
                    volume_multiplier=volume_multiplier,
                    amount_multiplier=amount_multiplier,
                )
                if summary.empty:
                    continue
                summary = summary.reset_index().rename(columns={"index": "date"})
                summary.insert(0, "symbol", symbol)
                rows.append(summary)
                symbols_seen.add(symbol)
                if len(rows) >= _BATCH_SYMBOLS:
                    result = flush_summaries(warehouse, interval=interval, rows=rows)
                    rows_written += int(result["rows"])
                    conflicts += int(result["conflicts"])
            result = flush_summaries(warehouse, interval=interval, rows=rows)
            rows_written += int(result["rows"])
            conflicts += int(result["conflicts"])
    coverage = warehouse.intraday_coverage(interval=interval)
    if int(coverage.get("rows", 0)) <= 0:
        raise RuntimeError(f"{interval} summary build produced no rows")
    return {
        "archives": archive_count,
        "entries": entry_count,
        "symbols": len(symbols_seen),
        "rows_written": rows_written,
        "conflicts": conflicts,
        "coverage": coverage,
        "zip_fingerprint": {
            "sha256": fingerprint_digest.hexdigest(),
            "archives": fingerprint_records,
        },
    }


def _validate(db_path: Path, intervals: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    warehouse = MarketWarehouse(
        db_path=db_path,
        package_root=db_path.parent / "package",
        package_writes_enabled=False,
        read_only=True,
    )
    coverage = {interval: warehouse.intraday_coverage(interval=interval) for interval in intervals}
    for interval, payload in coverage.items():
        if int(payload.get("rows", 0)) <= 0 or int(payload.get("symbols", 0)) <= 0:
            raise RuntimeError(f"invalid {interval} summary coverage: {payload}")
        if not str(payload.get("max_date", "")).strip():
            raise RuntimeError(f"invalid {interval} summary max_date: {payload}")
    return coverage


def _promote(output: Path, built: Path, built_manifest: Path) -> None:
    previous = Path(str(output) + ".previous")
    previous_manifest = manifest_path(previous)
    final_manifest = manifest_path(output)
    had_output = output.exists()
    had_manifest = final_manifest.exists()
    output_backed_up = False
    manifest_backed_up = False
    built_promoted = False
    manifest_promoted = False
    for stale in (previous, previous_manifest):
        if stale.exists():
            stale.unlink()
    try:
        if had_output:
            os.replace(output, previous)
            output_backed_up = True
        if had_manifest:
            os.replace(final_manifest, previous_manifest)
            manifest_backed_up = True
        os.replace(built, output)
        built_promoted = True
        os.replace(built_manifest, final_manifest)
        manifest_promoted = True
    except Exception:
        if built_promoted and output.exists():
            output.unlink()
        if manifest_promoted and final_manifest.exists():
            final_manifest.unlink()
        if output_backed_up and previous.exists():
            os.replace(previous, output)
        if manifest_backed_up and previous_manifest.exists():
            os.replace(previous_manifest, final_manifest)
        raise


def build_summary(
    *,
    root: str | Path,
    output: str | Path,
    keep_days: int = 480,
    intervals: tuple[str, ...] = ("1m", "5m"),
    volume_multiplier: float = 100.0,
    amount_multiplier: float = 1.0,
    required_latest_date: str | date | None = None,
) -> dict[str, Any]:
    source_root = Path(root).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    if not source_root.is_dir():
        raise RuntimeError(f"vendor root does not exist: {source_root}")
    normalized_intervals = tuple(dict.fromkeys(intervals))
    if not normalized_intervals or any(
        item not in _SUPPORTED_INTERVALS for item in normalized_intervals
    ):
        raise ValueError(f"unsupported intervals: {normalized_intervals}")
    cutoff = date.today() - timedelta(days=max(1, int(keep_days)))
    built = Path(str(output_path) + ".next")
    built_manifest = manifest_path(built)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    for stale in (built, built_manifest):
        if stale.exists():
            stale.unlink()
    warehouse = MarketWarehouse(
        db_path=built,
        package_root=built.parent / "package",
        package_writes_enabled=False,
        read_only=False,
    )
    interval_reports: dict[str, Any] = {}
    try:
        for interval in normalized_intervals:
            interval_reports[interval] = _build_interval(
                root=source_root,
                warehouse=warehouse,
                interval=interval,
                cutoff=cutoff,
                volume_multiplier=volume_multiplier,
                amount_multiplier=amount_multiplier,
            )
        coverage = _validate(built, normalized_intervals)
        required_latest = _coerce_required_latest_date(required_latest_date)
        if required_latest is not None:
            stale = {
                interval: str(payload.get("max_date", "")).strip()
                for interval, payload in coverage.items()
                if _coerce_required_latest_date(payload.get("max_date")) is None
                or _coerce_required_latest_date(payload.get("max_date")) < required_latest
            }
            if stale:
                raise RuntimeError(
                    "intraday summary does not cover required latest date "
                    f"{required_latest.isoformat()}: {stale}"
                )
        generation = datetime.now(UTC).isoformat()
        manifest = {
            "schema_version": 1,
            "generation": generation,
            "source_root": str(source_root),
            "cutoff_date": cutoff.isoformat(),
            "keep_natural_days": max(1, int(keep_days)),
            "required_latest_date": (
                required_latest.isoformat() if required_latest is not None else ""
            ),
            "intervals": list(normalized_intervals),
            "coverage": coverage,
            "interval_reports": interval_reports,
            "zip_fingerprint": {
                interval: report["zip_fingerprint"] for interval, report in interval_reports.items()
            },
            "unit_contract": {
                "volume_multiplier": float(volume_multiplier),
                "amount_multiplier": float(amount_multiplier),
            },
        }
        built_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        _promote(output_path, built, built_manifest)
        return manifest
    except Exception:
        for stale in (built, built_manifest):
            if stale.exists():
                stale.unlink()
        raise


def _coerce_required_latest_date(value: object) -> date | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid required latest date: {value!r}") from None
    if pd.isna(parsed):
        raise ValueError(f"invalid required latest date: {value!r}")
    return parsed.date()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="vendor history root")
    parser.add_argument("--output", required=True, help="summary DuckDB output")
    parser.add_argument("--keep-days", type=int, default=480)
    parser.add_argument("--interval", action="append", dest="intervals")
    parser.add_argument("--volume-multiplier", type=float, default=100.0)
    parser.add_argument("--amount-multiplier", type=float, default=1.0)
    parser.add_argument(
        "--require-latest-date",
        default="",
        help="fail without promoting when any interval max_date is older than YYYY-MM-DD",
    )
    args = parser.parse_args()
    intervals = tuple(args.intervals or ("1m", "5m"))
    report = build_summary(
        root=args.root,
        output=args.output,
        keep_days=args.keep_days,
        intervals=intervals,
        volume_multiplier=args.volume_multiplier,
        amount_multiplier=args.amount_multiplier,
        required_latest_date=args.require_latest_date,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
