"""Shared utilities for building / refreshing the intraday summary DuckDB.

Reused by build_vendor_intraday_summary.py (full build) and
refresh_vendor_intraday_summary.py (incremental refresh).
"""

from __future__ import annotations

import re
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from stock_analyzer.data.intraday_summary import summarize_minute_bars
from stock_analyzer.data.tdx_offline_provider import _normalize_symbol
from stock_analyzer.data.vendor_zip_overlay import (
    _minute_archive_coverage,
    normalize_vendor_minute_frame,
)

_ENTRY_RE = re.compile(
    r"^(?:sh|sz|bj)?(?P<code>\d{6})(?:\.(?:SH|SZ|BJ))?"
    r"(?:_(?:\d{4}|\d{6}|\d{8}))?\.csv$",
    re.IGNORECASE,
)

_SUPPORTED_INTERVALS: dict[str, str] = {"1m": "1min", "5m": "5min"}
_BATCH_SYMBOLS = 128


def entry_symbol(entry_name: str) -> str:
    """Extract the normalized symbol from a ZIP entry filename."""
    basename = entry_name.replace("\\", "/").rsplit("/", 1)[-1]
    match = _ENTRY_RE.fullmatch(basename)
    if match is None:
        return ""
    return _normalize_symbol(match.group("code"))


def archive_paths(root: Path, interval: str, cutoff: date) -> list[Path]:
    """Return sorted minute ZIP archives whose coverage period end >= cutoff."""
    token = _SUPPORTED_INTERVALS[interval]
    candidates: list[Path] = []
    for directory in root.rglob(f"Stock*_{token}_*-now"):
        if not directory.is_dir():
            continue
        candidates.extend(directory.glob("*.zip"))
    selected: list[Path] = []
    for path in sorted(set(candidates)):
        coverage = _minute_archive_coverage(path)
        if coverage is None or coverage[1] < cutoff:
            continue
        selected.append(path)
    # Annual archives first, same-period monthly last, so a monthly refresh
    # wins on duplicate symbol/date rows.
    selected.sort(
        key=lambda item: (
            _minute_archive_coverage(item)[0] if _minute_archive_coverage(item) else date.min,
            0 if "-" not in item.stem else 1,
            item.as_posix(),
        )
    )
    return selected


def manifest_path(db_path: Path) -> Path:
    """Return the manifest path alongside the DuckDB file."""
    return Path(str(db_path) + ".manifest.json")


def read_entry_summary(
    archive: zipfile.ZipFile,
    entry_names: list[str],
    *,
    interval: str,
    cutoff: date,
    volume_multiplier: float,
    amount_multiplier: float,
) -> pd.DataFrame:
    """Read and summarize minute bars for one symbol from a ZIP archive."""
    pieces: list[pd.DataFrame] = []
    cutoff_ts = pd.Timestamp(cutoff)
    for entry_name in entry_names:
        try:
            with archive.open(entry_name) as stream:
                raw = pd.read_csv(stream)
        except (KeyError, OSError, ValueError, pd.errors.ParserError):
            continue
        normalized = normalize_vendor_minute_frame(
            raw,
            volume_multiplier=volume_multiplier,
            amount_multiplier=amount_multiplier,
        )
        if normalized.empty:
            continue
        normalized = normalized.loc[normalized.index >= cutoff_ts]
        if not normalized.empty:
            pieces.append(normalized)
    if not pieces:
        return pd.DataFrame()
    minute_bars = pd.concat(pieces, axis=0, sort=False)
    minute_bars = minute_bars[~minute_bars.index.duplicated(keep="last")].sort_index()
    return summarize_minute_bars(minute_bars, interval=interval)


def flush_summaries(
    warehouse: Any,
    *,
    interval: str,
    rows: list[pd.DataFrame],
) -> dict[str, int]:
    """Upsert a batch of per-symbol summary frames into the warehouse."""
    if not rows:
        return {"rows": 0, "conflicts": 0}
    frame = pd.concat(rows, axis=0, ignore_index=True)
    rows.clear()
    return warehouse.upsert_intraday_summaries(interval=interval, frame=frame)