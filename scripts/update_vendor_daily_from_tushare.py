"""Nightly incremental vendor daily-K + adjustment-factor updater from Tushare.

Two phases, resumable by design:

Phase A (API fetch, optional worker threads): for every symbol fetch only the
rows the vendor library is actually missing:
- ``pro.daily``          -> raw (unadjusted) daily bars, appended after the
                            last date actually stored in the annual ZIPs;
- ``pro.daily_basic``    -> valuation/turnover columns for the same window;
- ``pro.adj_factor``     -> FULL history (re-anchoring needs the whole
                            series), from which the qfq/hfq factor files are
                            recomputed when any new date appeared.
Rate limiting is enforced globally across threads (tushare throttles per
token) through ``TushareProvider``'s retry policy (exponential backoff,
non-transient errors never replayed).

Phase B (ZIP rebuild, always after phase A so it runs once per archive):
- each affected ANNUAL daily ZIP (``全A日K/<year>.zip``) is rebuilt at most
  once: the updated symbols' year entries are replaced wholesale (old rows of
  that year + newly fetched rows, deduplicated by date), every other entry is
  copied byte-for-byte, a temporary file is written and ``os.replace`` swaps
  it in atomically, and the result is verified (no duplicate entries, no
  missing entries);
- the factor ZIPs (``复权因子/复权因子_前复权.zip`` and
  ``复权因子_后复权.zip``) are each rebuilt once per run; every year entry of
  an updated symbol is REPLACED by the re-anchored series (never merged),
  because a new corporate action shifts the qfq/hfq anchor for ALL history.
  qfq factor = adj_factor / adj_factor_latest (latest date -> exactly 1.0);
  hfq factor = adj_factor / adj_factor_earliest (earliest date -> 1.0).

Resume contract: the skip decision ALWAYS reads the actual last date stored
inside the ZIP entries (daily and factor), never the checkpoint file. The
checkpoint JSON is record-only (observability); re-running after an
interruption between phase A and phase B simply refetches the missing dates
and rebuilds the archives. No ZIP, index or delta warehouse of the runtime
is ever modified by this script.

Writes the vendor library ONLY through the documented formats:
- daily: ``全A日K/<year>.zip`` entries ``<year>/<ts_code>.csv`` with header
  ``code,datetime,open,high,low,close,pre_close,change,pct_chg,volume,amount,
  turnover,turnover_free,volume_ratio,pe,pe_ttm,pb,ps,ps_ttm,dv_yield,dv_ttm,
  total_share,float_share,free_share,total_mv,circ_mv`` (prices raw, i.e.
  unadjusted);
- factors: ``复权因子/复权因子_前复权.zip`` + ``复权因子_后复权.zip`` entries
  ``<year>/<ts_code>.csv`` with header ``股票代码,交易日期,复权因子`` (dates
  ``YYYYMMDD``, qfq latest factor = 1.0).

Runtime usage (NAS cron; the host ``股票历史数据`` directory is mounted at
``/data``, this repo's scripts directory is mounted read-only at ``/tools``
so the container always runs the pinned script):

    docker run --rm -v <vendor_root>:/data:rw -v <本脚本目录>:/tools:ro \
        --env-file <tushare.env> stock-analyzer:latest \
        python3 /tools/update_vendor_daily_from_tushare.py --vendor-root /data \
        --end-date 2026-07-17 --checkpoint /data/.vendor_update_checkpoint.json

Local usage:

    python scripts/update_vendor_daily_from_tushare.py --vendor-root <root> \
        [--daily-dir 全A日K] [--factors-dir 复权因子] [--end-date 2026-07-17] \
        [--symbols-file symbols.txt] [--limit 3000] [--dry-run] \
        [--checkpoint artifacts/vendor_daily_update_checkpoint.json] \
        [--interval-sec 0.6] [--max-retries 3] [--max-workers 1] [--skip-factors]

Exit code: 0 when every symbol succeeded (or dry-run). 1 when any symbol
failed. 2 when no universe or tushare token could be resolved.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_analyzer.data.provider import DataSourceError  # noqa: E402
from stock_analyzer.data.tushare_provider import (  # noqa: E402
    TushareProvider,
    _to_ts_code,
)
from stock_analyzer.ops.nightly_readiness import (  # noqa: E402
    write_nightly_readiness,
)

EARLIEST_DATE = date(1990, 1, 1)
DAILY_ARCHIVE_RE = re.compile(r"^(?P<year>\d{4})(?:\((?P<copy>\d+)\))?\.zip$", re.I)
DAILY_ENTRY_RE = re.compile(r"(?P<code>\d{6})\.(?:SH|SZ|BJ)\.csv$", re.I)
FACTORS_QFQ_ARCHIVE = "复权因子_前复权.zip"
FACTORS_HFQ_ARCHIVE = "复权因子_后复权.zip"
FACTOR_ARCHIVES = (FACTORS_QFQ_ARCHIVE, FACTORS_HFQ_ARCHIVE)

DAILY_COLUMNS = [
    "code",
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "change",
    "pct_chg",
    "volume",
    "amount",
    "turnover",
    "turnover_free",
    "volume_ratio",
    "pe",
    "pe_ttm",
    "pb",
    "ps",
    "ps_ttm",
    "dv_yield",
    "dv_ttm",
    "total_share",
    "float_share",
    "free_share",
    "total_mv",
    "circ_mv",
]

# daily_basic -> vendor daily column mapping; same-name columns are taken
# directly (volume_ratio, pe, pe_ttm, pb, ps, ps_ttm, dv_ttm, total_share,
# float_share, free_share, total_mv, circ_mv).
BASIC_RENAMES = {
    "turnover_rate": "turnover",
    "turnover_rate_f": "turnover_free",
    "dv_ratio": "dv_yield",
}
BASIC_SAME_NAME = [
    "volume_ratio",
    "pe",
    "pe_ttm",
    "pb",
    "ps",
    "ps_ttm",
    "dv_ttm",
    "total_share",
    "float_share",
    "free_share",
    "total_mv",
    "circ_mv",
]

# Tushare single-call row cap used by the batch paging guard.  When a
# trade_date-wide call returns exactly this many rows the caller must page
# with ``offset`` to avoid silent truncation.  The effective cap is ~5000
# rows per call (measured 2026-08 on adj_factor; the 6000 documented earlier
# exceeds it and requests are truncated server-side).
_TUSHARE_PAGE_LIMIT = 5000

DAILY_BASIC_FIELDS = (
    "ts_code,trade_date,turnover_rate,turnover_rate_f,volume_ratio,"
    "pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,dv_ttm,total_share,float_share,"
    "free_share,total_mv,circ_mv"
)


def _is_zip_noise(name: str) -> bool:
    normalized = name.replace("\\", "/")
    return (
        normalized.startswith("__MACOSX/")
        or "/._" in normalized
        or normalized.endswith("/.DS_Store")
        or normalized.endswith(".DS_Store")
    )


def _coerce_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def _select_canonical_daily_archives(daily_root: Path) -> list[tuple[int, Path]]:
    grouped: dict[int, list[Path]] = {}
    for path in daily_root.glob("*.zip"):
        match = DAILY_ARCHIVE_RE.fullmatch(path.name)
        if match is None:
            continue
        grouped.setdefault(int(match.group("year")), []).append(path)
    selected: list[tuple[int, Path]] = []
    for year, candidates in sorted(grouped.items()):
        preferred = next(
            (path for path in candidates if path.name.lower() == f"{year}.zip"),
            sorted(candidates, key=lambda item: item.name)[0],
        )
        selected.append((year, preferred))
    return selected


def _zip_entries_matching(archive_path: Path, pattern: re.Pattern[str]) -> list[str]:
    if not archive_path.exists():
        return []
    with zipfile.ZipFile(archive_path) as archive:
        return [
            name
            for name in archive.namelist()
            if not _is_zip_noise(name) and pattern.search(name) is not None
        ]


def _read_last_line_date(archive_path: Path, entry_name: str) -> date | None:
    """Read the actual last (maximum) trade date of one ZIP entry."""
    with zipfile.ZipFile(archive_path) as archive:
        try:
            with archive.open(entry_name) as stream:
                frame = pd.read_csv(
                    stream,
                    usecols=lambda column: column in {"datetime", "trade_date", "date", "交易日期"},
                )
        except (KeyError, ValueError):
            return None
    if frame.empty:
        return None
    date_column = next(
        (name for name in ("datetime", "trade_date", "date", "交易日期") if name in frame.columns),
        "",
    )
    if not date_column:
        return None
    parsed = pd.to_datetime(frame[date_column].astype(str), errors="coerce")
    parsed = parsed.dropna()
    if parsed.empty:
        return None
    return parsed.max().date()


def _read_last_date_fast(archive_path: Path, entry_name: str) -> date | None:
    """Read the last trade date of one ZIP entry without a full pandas parse."""
    if not archive_path.exists():
        return None
    try:
        with zipfile.ZipFile(archive_path) as archive:
            return _read_last_date_from_open_archive(archive, entry_name)
    except (KeyError, zipfile.BadZipFile):
        return None


def _read_last_date_from_open_archive(
    archive: zipfile.ZipFile,
    entry_name: str,
) -> date | None:
    """Read one entry's last date while reusing an already-open ZIP."""
    try:
        with archive.open(entry_name) as stream:
            last_line: bytes | None = None
            for line in stream:
                if line.strip():
                    last_line = line
    except KeyError:
        return None
    if last_line is None:
        return None
    parts = last_line.decode("utf-8-sig", errors="replace").strip().split(",")
    if len(parts) < 2:
        return None
    parsed = pd.to_datetime(parts[1].strip(), errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def _index_symbol_key(ts_code: str) -> str:
    """Normalize ``000001.SZ`` and ``000001`` to the index's six-digit key."""
    match = re.search(r"(?P<code>\d{6})", str(ts_code))
    return match.group("code") if match is not None else str(ts_code).strip()


def _scan_latest_dates_for_symbols(
    *,
    daily_root: Path,
    updated_symbols: set[str],
) -> dict[str, date]:
    """Batch-scan updated symbols, opening every annual ZIP at most once."""
    unresolved = {_index_symbol_key(symbol) for symbol in updated_symbols}
    latest_dates: dict[str, date] = {}
    for _year, archive_path in reversed(_select_canonical_daily_archives(daily_root)):
        if not unresolved:
            break
        found_in_archive: dict[str, date] = {}
        try:
            with zipfile.ZipFile(archive_path) as archive:
                entries_by_symbol: dict[str, list[str]] = {}
                for name in archive.namelist():
                    if _is_zip_noise(name):
                        continue
                    match = DAILY_ENTRY_RE.search(name)
                    if match is None:
                        continue
                    symbol = match.group("code")
                    if symbol in unresolved:
                        entries_by_symbol.setdefault(symbol, []).append(name)
                for symbol, entry_names in entries_by_symbol.items():
                    for entry_name in entry_names:
                        entry_date = _read_last_date_from_open_archive(
                            archive,
                            entry_name,
                        )
                        current = found_in_archive.get(symbol)
                        if entry_date is not None and (current is None or entry_date > current):
                            found_in_archive[symbol] = entry_date
        except zipfile.BadZipFile:
            continue
        latest_dates.update(found_in_archive)
        unresolved.difference_update(found_in_archive)
    return latest_dates


def _load_last_date_index(path: str | Path) -> dict[str, object] | None:
    """Load the vendor daily last-date index (vendor_zip_overlay format).

    Returns ``None`` when missing/corrupt so callers fall back to a full scan.
    """
    target = Path(path)
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), dict):
        return None
    return payload


def _update_last_date_index(
    *,
    index_path: str | Path,
    daily_root: Path,
    updated_symbols: set[str],
    rebuild_latest_dates: dict[str, date] | None = None,
) -> dict[str, object]:
    """Refresh updated symbols without re-reading freshly rebuilt ZIP entries."""
    payload = _load_last_date_index(index_path)
    if payload is None:
        return {"updated": False, "reason": "index_missing"}
    symbols = cast("dict[str, object]", payload["symbols"])
    provided = {
        _index_symbol_key(symbol): latest for symbol, latest in (rebuild_latest_dates or {}).items()
    }
    missing = {symbol for symbol in updated_symbols if _index_symbol_key(symbol) not in provided}
    scanned = (
        _scan_latest_dates_for_symbols(
            daily_root=daily_root,
            updated_symbols=missing,
        )
        if missing
        else {}
    )
    latest_by_symbol = {**scanned, **provided}
    for code in sorted(updated_symbols):
        normalized_code = _index_symbol_key(code)
        latest = latest_by_symbol.get(normalized_code)
        if latest is None:
            continue
        index_key = normalized_code if normalized_code in symbols or code not in symbols else code
        existing = symbols.get(index_key)
        if isinstance(existing, dict):
            existing["latest_date"] = latest.isoformat()
        else:
            symbols[index_key] = {
                "latest_date": latest.isoformat(),
                "entries": [],
            }
    target = Path(index_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(target)
    return {
        "updated": True,
        "symbols": len(updated_symbols),
        "dates_from_rebuild": len(provided),
        "dates_from_fallback": len(scanned),
    }


def _symbol_daily_last_date(
    daily_root: Path,
    ts_code: str,
    index: dict[str, object] | None = None,
) -> date | None:
    """Max trade date across the symbol's entries in all annual daily ZIPs."""
    if index is not None:
        symbols = index.get("symbols")
        if isinstance(symbols, dict):
            entry = symbols.get(ts_code)
            if isinstance(entry, dict):
                raw = str(entry.get("latest_date", "")).strip()
                parsed = _coerce_date(raw)
                if parsed is not None:
                    return parsed
    latest: date | None = None
    entry_pattern = re.compile(re.escape(ts_code) + r"\.csv$", re.I)
    for _, archive_path in _select_canonical_daily_archives(daily_root):
        for entry_name in _zip_entries_matching(archive_path, entry_pattern):
            entry_date = _read_last_line_date(archive_path, entry_name)
            if entry_date is not None and (latest is None or entry_date > latest):
                latest = entry_date
    return latest


def _symbol_factor_last_date(factors_root: Path, ts_code: str) -> date | None:
    """Max trade date across the symbol's entries in the qfq factor ZIP."""
    archive_path = factors_root / FACTORS_QFQ_ARCHIVE
    if not archive_path.exists():
        return None
    entry_pattern = re.compile(re.escape(ts_code) + r"\.csv$", re.I)
    latest: date | None = None
    for entry_name in _zip_entries_matching(archive_path, entry_pattern):
        entry_date = _read_last_line_date(archive_path, entry_name)
        if entry_date is not None and (latest is None or entry_date > latest):
            latest = entry_date
    return latest


def _list_daily_symbols(daily_root: Path) -> list[str]:
    symbols: set[str] = set()
    for _, archive_path in _select_canonical_daily_archives(daily_root):
        for entry_name in _zip_entries_matching(archive_path, DAILY_ENTRY_RE):
            match = DAILY_ENTRY_RE.search(entry_name)
            if match is not None:
                symbols.add(match.group("code"))
    return sorted(symbols)


def _daily_to_25_columns(
    *,
    ts_code: str,
    daily: pd.DataFrame,
    basic: pd.DataFrame,
) -> pd.DataFrame:
    """Map tushare ``daily`` + ``daily_basic`` into the vendor 25-column CSV."""
    if daily is None or daily.empty or "trade_date" not in daily.columns:
        raise DataSourceError(f"tushare daily empty for {ts_code}")
    if "vol" not in daily.columns:
        raise DataSourceError(f"tushare daily missing vol for {ts_code}")
    frame = daily.copy()
    frame = frame.rename(
        columns={
            "trade_date": "datetime",
            "vol": "volume",
        }
    )
    frame["code"] = ts_code
    frame["datetime"] = frame["datetime"].astype(str).str.replace("-", "", regex=False)
    frame = frame[frame["datetime"].str.fullmatch(r"\d{8}", na=False)]
    if basic is not None and not basic.empty and "trade_date" in basic.columns:
        basic_frame = basic.copy()
        basic_frame = basic_frame.rename(columns={"trade_date": "datetime"})
        keep = ["datetime"]
        renamed = {}
        for source, target in BASIC_RENAMES.items():
            if source in basic_frame.columns:
                keep.append(target)
                renamed[source] = target
        for column in BASIC_SAME_NAME:
            if column in basic_frame.columns:
                keep.append(column)
        basic_frame = basic_frame.rename(columns=renamed)
        basic_frame["datetime"] = (
            basic_frame["datetime"].astype(str).str.replace("-", "", regex=False)
        )
        basic_frame = basic_frame[basic_frame["datetime"].str.fullmatch(r"\d{8}", na=False)]
        basic_frame = basic_frame.drop_duplicates(subset=["datetime"], keep="last")
        basic_frame = basic_frame[[column for column in keep if column in basic_frame.columns]]
        frame = frame.merge(basic_frame, on="datetime", how="left")
    for column in DAILY_COLUMNS:
        if column not in frame.columns:
            frame[column] = pd.NA
    frame = frame[DAILY_COLUMNS]
    frame = frame.sort_values("datetime").drop_duplicates(subset=["datetime"], keep="last")
    return frame


def _factor_rows(
    *,
    ts_code: str,
    adj: pd.DataFrame,
    anchor: str,
) -> pd.DataFrame:
    """Recompute the re-anchored factor series (qfq: latest=1.0; hfq: earliest=1.0)."""
    if adj is None or adj.empty:
        raise DataSourceError(f"tushare adj_factor empty for {ts_code}")
    frame = adj.copy()
    for column in ("trade_date", "adj_factor"):
        if column not in frame.columns:
            raise DataSourceError(f"tushare adj_factor missing column for {ts_code}: {column}")
    frame = frame.rename(columns={"ts_code": "_ts_code"})
    frame["trade_date"] = frame["trade_date"].astype(str).str.replace("-", "", regex=False)
    frame = frame[frame["trade_date"].str.fullmatch(r"\d{8}", na=False)]
    frame["adj_factor"] = pd.to_numeric(frame["adj_factor"], errors="coerce")
    frame = frame.dropna(subset=["trade_date", "adj_factor"])
    frame = frame[frame["adj_factor"] > 0]
    frame = frame.sort_values("trade_date").drop_duplicates(subset=["trade_date"], keep="last")
    if frame.empty:
        raise DataSourceError(f"tushare adj_factor empty after normalize for {ts_code}")
    if anchor == "latest":
        scale = float(frame["adj_factor"].iloc[-1])
    else:
        scale = float(frame["adj_factor"].iloc[0])
    if scale <= 0:
        raise DataSourceError(f"tushare adj_factor anchor invalid for {ts_code}: {scale}")
    rows = pd.DataFrame(
        {
            "股票代码": ts_code,
            "交易日期": frame["trade_date"],
            "复权因子": frame["adj_factor"] / scale,
        }
    )
    return rows


def _frame_to_year_entries(frame: pd.DataFrame) -> dict[str, str]:
    """Split one symbol's factor CSV into ``{<year>/<ts_code>.csv: csv_text}``.

    Factor dates keep the vendor ``YYYYMMDD`` format; entries are grouped by
    year and each year entry is a self-contained CSV with the header
    ``股票代码,交易日期,复权因子``.
    """
    entries: dict[str, str] = {}
    if frame.empty:
        return entries
    code_column = next(
        (column for column in ("code", "股票代码") if column in frame.columns),
        "",
    )
    date_column = next(
        (column for column in ("datetime", "交易日期") if column in frame.columns),
        "",
    )
    if not code_column or not date_column:
        return entries
    code = str(frame[code_column].iloc[0])
    grouped = frame.groupby(pd.to_datetime(frame[date_column].astype(str), errors="coerce").dt.year)
    for year, group in grouped:
        if pd.isna(year):
            continue
        group = group.sort_values(date_column)
        entries[f"{int(year)}/{code}.csv"] = group.to_csv(index=False, lineterminator="\n")
    return entries


def _read_zip_entry_csv(archive_path: Path, entry_name: str) -> pd.DataFrame | None:
    if not archive_path.exists():
        return None
    with zipfile.ZipFile(archive_path) as archive:
        try:
            with archive.open(entry_name) as stream:
                return pd.read_csv(stream)
        except KeyError:
            return None


def _render_daily_csv(frame: pd.DataFrame) -> str:
    """Render one symbol-year daily frame into the vendor 25-column CSV.

    The existing vendor daily files use ``YYYY-MM-DD`` datetimes; rows are
    de-duplicated by date (last wins) and sorted ascending.
    """
    if frame.empty:
        return ""
    out = frame.copy()
    text = out["datetime"].astype(str)
    parsed = pd.to_datetime(text, format="%Y%m%d", errors="coerce")
    parsed = parsed.fillna(pd.to_datetime(text, format="%Y-%m-%d", errors="coerce"))
    parsed = parsed.fillna(pd.to_datetime(text, errors="coerce"))
    out["datetime"] = parsed
    out = out.dropna(subset=["datetime"])
    out["datetime"] = out["datetime"].dt.strftime("%Y-%m-%d")
    out = out.sort_values("datetime").drop_duplicates(subset=["datetime"], keep="last")
    for column in DAILY_COLUMNS:
        if column not in out.columns:
            out[column] = pd.NA
    return out[DAILY_COLUMNS].to_csv(index=False, lineterminator="\n")


def _rebuild_daily_year_zip(
    daily_root: Path,
    year: int,
    updates: dict[str, pd.DataFrame],
) -> dict[str, object]:
    """Rebuild one annual daily ZIP and return merged latest dates."""
    archive_path = daily_root / f"{year}.zip"
    replace_entries: dict[str, str] = {}
    latest_dates: dict[str, str] = {}
    for ts_code, fresh in sorted(updates.items()):
        entry_name = f"{year}/{ts_code}.csv"
        merged = _read_zip_entry_csv(archive_path, entry_name)
        if merged is None or merged.empty:
            merged = fresh
        else:
            merged = pd.concat([merged, fresh], axis=0, sort=False, ignore_index=True)
        rendered = _render_daily_csv(merged)
        if not rendered:
            continue
        replace_entries[entry_name] = rendered
        date_column = next(
            (name for name in ("datetime", "trade_date", "date") if name in merged.columns),
            "",
        )
        if date_column:
            parsed = pd.to_datetime(
                merged[date_column].astype(str),
                errors="coerce",
            ).dropna()
            if not parsed.empty:
                latest_dates[_index_symbol_key(ts_code)] = parsed.max().date().isoformat()
    report = _rebuild_zip(archive_path, replace_entries)
    report["latest_dates"] = latest_dates
    return report


def _rebuild_zip(
    archive_path: Path,
    replace_entries: dict[str, str],
) -> dict[str, object]:
    """Atomically rebuild one ZIP with the given entries replaced wholesale.

    Validation happens against the *temporary* archive BEFORE the atomic
    ``os.replace``, so a failed rebuild never destroys the previous ZIP: the
    old file stays byte-identical unless the new archive fully validates.
    """
    if not archive_path.parent.exists():
        archive_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive_path.with_name(f"{archive_path.name}.{os.getpid()}.tmp")
    old_names: set[str] = set()
    # compresslevel=1: the NAS host CPU is the bottleneck; level 1 is 3-5x
    # faster than the default 6 at a modest size cost (vendor data is read-only).
    with zipfile.ZipFile(
        temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1
    ) as output:
        if archive_path.exists():
            with zipfile.ZipFile(archive_path) as source:
                old_names = {info.filename for info in source.infolist() if not info.is_dir()}
                for info in source.infolist():
                    if info.is_dir() or info.filename in replace_entries:
                        continue
                    output.writestr(info, source.read(info.filename))
        for name, content in replace_entries.items():
            output.writestr(name, content)

    try:
        # Validate the temporary archive before swapping it in.  Opening it
        # (raises BadZipFile on corruption) plus entry checks cover both
        # readability and completeness.
        written_names: list[str] = []
        with zipfile.ZipFile(temporary) as check:
            written_names = [info.filename for info in check.infolist() if not info.is_dir()]
            for name in replace_entries:
                if name not in written_names:
                    raise DataSourceError(f"vendor ZIP rebuild lost entry: {archive_path}!{name}")
            for name in written_names:
                check.read(name)
        duplicate_names = sorted({name for name in written_names if written_names.count(name) > 1})
        if duplicate_names:
            raise DataSourceError(
                f"vendor ZIP rebuild produced duplicate entries: {archive_path}: {duplicate_names}"
            )
        missing_names = sorted(old_names - set(written_names))
        if missing_names:
            raise DataSourceError(
                f"vendor ZIP rebuild dropped entries: {archive_path}: {missing_names}"
            )
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    os.replace(temporary, archive_path)
    return {
        "archive": str(archive_path),
        "entries_total": len(written_names),
        "replaced_entries": sorted(replace_entries),
    }


def _rebuild_factor_zip(
    factors_root: Path,
    archive_name: str,
    updates: dict[str, pd.DataFrame],
) -> dict[str, object]:
    """Rebuild one factor ZIP once; updated symbols' entries replaced everywhere."""
    replace_entries: dict[str, str] = {}
    for _ts_code, frame in sorted(updates.items()):
        replace_entries.update(_frame_to_year_entries(frame))
    return _rebuild_zip(factors_root / archive_name, replace_entries)


def _load_checkpoint(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_checkpoint(path: Path, checkpoint: dict[str, object], symbol: str) -> None:
    checkpoint[symbol] = datetime.now().isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(checkpoint, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _fetch_symbol(
    *,
    api: TushareProvider,
    symbol: str,
    end_date: date,
    daily_root: Path,
    factors_root: Path,
    skip_factors: bool,
    force_factors: bool = False,
    dry_run: bool,
    index: dict[str, object] | None = None,
) -> dict[str, object]:
    """Phase A worker: decide by ZIP contents and fetch only missing data.

    ``force_factors`` refetches and rebuilds adjustment factors even when the
    factor ZIP already covers ``end_date`` (repair truncated factor history).
    """
    ts_code = _to_ts_code(symbol)
    daily_last = _symbol_daily_last_date(daily_root, ts_code, index=index)
    factor_last = None if skip_factors else _symbol_factor_last_date(factors_root, ts_code)
    need_daily = daily_last is None or daily_last < end_date
    need_factors = (not skip_factors) and (
        force_factors or factor_last is None or factor_last < end_date
    )
    if not need_daily and not need_factors:
        return {
            "symbol": symbol,
            "ts_code": ts_code,
            "status": "skipped",
            "daily_last": daily_last.isoformat() if daily_last else "",
            "factor_last": factor_last.isoformat() if factor_last else "",
        }
    result: dict[str, object] = {
        "symbol": symbol,
        "ts_code": ts_code,
        "status": "ok" if not dry_run else "dry-run",
        "daily_last": daily_last.isoformat() if daily_last else "",
        "factor_last": factor_last.isoformat() if factor_last else "",
        "daily_fetch": need_daily,
        "factor_fetch": need_factors,
    }
    if dry_run:
        return result

    pro = api._resolve_pro_api()
    end_s = end_date.strftime("%Y%m%d")

    daily = pd.DataFrame()
    basic = pd.DataFrame()
    if need_daily:
        start = (daily_last + timedelta(days=1)) if daily_last else EARLIEST_DATE
        start_s = start.strftime("%Y%m%d")
        raw_daily = api._call_with_retry(
            lambda: pro.daily(ts_code=ts_code, start_date=start_s, end_date=end_s)
        )
        daily = _as_frame(raw_daily)
        raw_basic = api._call_with_retry(
            lambda: pro.daily_basic(
                ts_code=ts_code,
                start_date=start_s,
                end_date=end_s,
                fields=(
                    "ts_code,trade_date,turnover_rate,turnover_rate_f,volume_ratio,"
                    "pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,dv_ttm,total_share,float_share,"
                    "free_share,total_mv,circ_mv"
                ),
            )
        )
        basic = _as_frame(raw_basic)
    if need_factors:
        adj_start_s = EARLIEST_DATE.strftime("%Y%m%d")
        raw_adj = _fetch_adj_factor_paged(
            api=api,
            ts_code=ts_code,
            start_date=adj_start_s,
            end_date=end_s,
        )
        adj = _as_frame(raw_adj)
        if adj.empty:
            # 全历史窗口必然有数据：空响应代表接口失败/权限/限频被上层
            # 吞掉，而不是「合法无数据」。显式抛错避免 0 数据被当成功
            # 跳过因子重建（8-13 现场 0 成功 0 失败的上游形态）。
            raise DataSourceError(
                f"tushare adj_factor empty for {ts_code} "
                f"({adj_start_s}~{end_s}); treating as failure, not no-data"
            )
        result["adj"] = adj

    if need_daily:
        result["daily"] = _daily_to_25_columns(ts_code=ts_code, daily=daily, basic=basic)
    return result


def _as_frame(raw: object) -> pd.DataFrame:
    if isinstance(raw, pd.DataFrame):
        return raw.copy()
    return pd.DataFrame()


def _fetch_adj_factor_paged(
    api: TushareProvider,
    *,
    ts_code: str = "",
    trade_date: str = "",
    start_date: str = "",
    end_date: str = "",
    page_size: int = _TUSHARE_PAGE_LIMIT,
) -> pd.DataFrame:
    """Fetch ``adj_factor`` with defensive paging when no ``trade_date`` is set.

    A single per-symbol full-history query (1996→today ≈ 7400 rows) exceeds
    the ~5000-row per-response cap and is silently truncated server-side,
    which after re-anchoring drops the pre-truncation years from the rebuilt
    factor files.  Offsetting until a short page is returned reconstructs the
    full series.

    ``trade_date``-wide calls are intentionally NOT paged here: a full market
    day fits in one response and the per-day ``adj_factor`` interface does not
    accept ``offset``.  Only ``trade_date`` is passed (SDK forwards empty
    strings verbatim and tushare may reject them; ``_fetch_market_wide_by_date``
    follows the same convention).
    """
    if trade_date:
        raw = api._call_with_retry(lambda: api._resolve_pro_api().adj_factor(trade_date=trade_date))
        return _as_frame(raw)
    parts: list[pd.DataFrame] = []
    offset = 0
    while True:
        part = _as_frame(
            api._call_with_retry(
                lambda offset=offset: api._resolve_pro_api().adj_factor(
                    ts_code=ts_code,
                    start_date=start_date,
                    end_date=end_date,
                    offset=offset,
                    limit=page_size,
                )
            )
        )
        if part.empty:
            break
        parts.append(part)
        if len(part) < page_size:
            break
        offset += len(part)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def _fetch_market_wide_by_date(
    api: TushareProvider,
    trade_date: str,
) -> dict[str, pd.DataFrame]:
    """Fetch one full-market trading day via trade_date-wide calls.

    ``pro.daily`` is paged defensively with ``offset``; daily_basic and
    adj_factor single calls are assumed to fit the whole market for one day
    (verified by scripts/verify_tushare_batch_permission.py).
    """
    pro = api._resolve_pro_api()
    daily_parts: list[pd.DataFrame] = []
    offset = 0
    while True:
        part = _as_frame(
            api._call_with_retry(
                lambda offset=offset: pro.daily(
                    trade_date=trade_date,
                    offset=offset,
                    limit=_TUSHARE_PAGE_LIMIT,
                )
            )
        )
        if part.empty:
            break
        daily_parts.append(part)
        if len(part) < _TUSHARE_PAGE_LIMIT:
            break
        offset += len(part)
    daily = pd.concat(daily_parts, ignore_index=True) if daily_parts else pd.DataFrame()
    basic = _as_frame(
        api._call_with_retry(
            lambda: pro.daily_basic(
                trade_date=trade_date,
                fields=DAILY_BASIC_FIELDS,
            )
        )
    )
    adj = _as_frame(api._call_with_retry(lambda: pro.adj_factor(trade_date=trade_date)))
    return {"daily": daily, "basic": basic, "adj": adj}


def _distribute_batch_day(
    *,
    daily: pd.DataFrame,
    basic: pd.DataFrame,
    adj: pd.DataFrame,
    updates_by_year: dict[int, dict[str, pd.DataFrame]],
    factor_updates: dict[str, pd.DataFrame],
    skip_factors: bool,
) -> None:
    """Split one full-market day into per-symbol year buckets (25-col format)."""
    if daily is None or daily.empty or "ts_code" not in daily.columns:
        return
    for ts_code, group in daily.groupby("ts_code"):
        code = str(ts_code)
        basic_group: pd.DataFrame | None = None
        if basic is not None and not basic.empty and "ts_code" in basic.columns:
            subset = basic[basic["ts_code"] == ts_code]
            basic_group = subset if not subset.empty else None
        try:
            frame = _daily_to_25_columns(ts_code=code, daily=group, basic=basic_group)
        except DataSourceError:
            continue
        if frame is None or frame.empty:
            continue
        year_groups = frame.groupby(
            pd.to_datetime(frame["datetime"], format="%Y%m%d", errors="coerce").dt.year
        )
        for year, year_frame in year_groups:
            if pd.isna(year):
                continue
            year_key = int(year)
            previous = updates_by_year.setdefault(year_key, {}).get(code)
            updates_by_year[year_key][code] = (
                pd.concat([previous, year_frame], ignore_index=True)
                if previous is not None
                else year_frame
            )
    if skip_factors:
        return
    if adj is None or adj.empty or "ts_code" not in adj.columns:
        return
    for ts_code, group in adj.groupby("ts_code"):
        code = str(ts_code)
        previous = factor_updates.get(code)
        factor_updates[code] = (
            pd.concat([previous, group], ignore_index=True) if previous is not None else group
        )


def _seed_factor_rows(
    *,
    ts_code: str,
    new_rows: pd.DataFrame,
    anchor: str,
) -> pd.DataFrame | None:
    """Seed a factor series from freshly fetched rows when no history exists.

    Used when the stored series is missing entirely or the anchor-day value is
    unavailable (e.g. the stock was suspended on the anchor day); in the
    latter case the archive temporarily holds only the new window until the
    next run re-anchors against the full history.
    """
    seed_series = new_rows.sort_values("trade_date").drop_duplicates(
        subset=["trade_date"], keep="last"
    )
    if seed_series.empty:
        return None
    if anchor == "latest":
        scale = float(seed_series["adj_factor"].iloc[-1])
    else:
        scale = float(seed_series["adj_factor"].iloc[0])
    if scale <= 0:
        return None
    return pd.DataFrame(
        {
            "股票代码": ts_code,
            "交易日期": seed_series["trade_date"],
            "复权因子": seed_series["adj_factor"] / scale,
        }
    )


def _load_factor_entry_map(factors_root: Path, archive_name: str) -> dict[str, pd.DataFrame]:
    """一次遍历 ZIP 全部 entry，建立 {ts_code: 历史因子 DataFrame} 映射。"""
    archive_path = factors_root / archive_name
    if not archive_path.exists():
        return {}
    grouped: dict[str, list[pd.DataFrame]] = {}
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            if info.is_dir() or _is_zip_noise(info.filename):
                continue
            name = info.filename.replace("\\", "/")
            if DAILY_ENTRY_RE.search(name) is None:
                continue
            ts_code = name.rsplit("/", 1)[-1][: -len(".csv")]
            try:
                with archive.open(info.filename) as stream:
                    frame = pd.read_csv(stream)
            except (KeyError, ValueError, OSError):
                continue
            if "交易日期" not in frame.columns or "复权因子" not in frame.columns:
                continue
            frame = frame[["交易日期", "复权因子"]].dropna()
            if frame.empty:
                continue
            grouped.setdefault(ts_code, []).append(frame)
    stored_map: dict[str, pd.DataFrame] = {}
    for ts_code, parts in grouped.items():
        merged = pd.concat(parts, axis=0, sort=False, ignore_index=True)
        merged = merged.drop_duplicates(subset=["交易日期"], keep="last")
        merged = merged.sort_values("交易日期")
        stored_map[ts_code] = merged
    return stored_map


def _merge_factor_rows_scaled(
    *,
    ts_code: str,
    adj_new_day: pd.DataFrame,
    adj_old_day: pd.DataFrame,
    factors_root: Path,
    archive_name: str,
    anchor: str,
    stored_map: dict[str, pd.DataFrame] | None = None,
) -> pd.DataFrame | None:
    """Re-anchor the stored factor series with only the two anchor-day values.

    ``stored_map`` (from ``_load_factor_entry_map``) supplies the stored
    series without rescanning the ZIP per symbol; when omitted the archive is
    opened and scanned as before.

    The vendor factor files store the *normalised* series (qfq latest = 1.0 /
    hfq earliest = 1.0), so the raw adjustment factor scale is not recoverable
    from them.  Instead both anchor days are fetched market-wide (two batch
    calls per night) and the stored series is rescaled by the anchor ratio:

      qfq_new(T) = qfq_old(T) * adj(T_old) / adj(T_new),  qfq_new(T_new) = 1.0
      hfq_new(T) = hfq_old(T),
      hfq_new(T_new) = hfq_old(T_old) * adj(T_new) / adj(T_old)

    which is mathematically identical to re-anchoring the full history.
    """
    if adj_new_day is None or adj_new_day.empty:
        return None
    new_rows = adj_new_day.copy()
    new_rows["trade_date"] = new_rows["trade_date"].astype(str).str.replace("-", "", regex=False)
    new_rows = new_rows[new_rows["trade_date"].str.fullmatch(r"\d{8}", na=False)]
    new_rows["adj_factor"] = pd.to_numeric(new_rows["adj_factor"], errors="coerce")
    new_rows = new_rows.dropna(subset=["trade_date", "adj_factor"])
    new_rows = new_rows[new_rows["adj_factor"] > 0]
    if new_rows.empty:
        return None

    old_scale: float | None = None
    if adj_old_day is not None and not adj_old_day.empty:
        old_rows = adj_old_day.copy()
        old_rows["trade_date"] = (
            old_rows["trade_date"].astype(str).str.replace("-", "", regex=False)
        )
        old_rows["adj_factor"] = pd.to_numeric(old_rows["adj_factor"], errors="coerce")
        old_rows = old_rows.dropna(subset=["adj_factor"])
        old_rows = old_rows[old_rows["adj_factor"] > 0]
        if not old_rows.empty:
            old_scale = float(old_rows["adj_factor"].iloc[0])
    if old_scale is None or old_scale <= 0:
        # Anchor-day value unavailable (e.g. the stock was suspended on the
        # anchor day).  Fall back to seeding from the freshly fetched days so
        # the factor archive never lags the daily archive.
        return _seed_factor_rows(ts_code=ts_code, new_rows=new_rows, anchor=anchor)

    new_scale = float(new_rows["adj_factor"].iloc[-1])
    if new_scale <= 0:
        return None
    ratio = new_scale / old_scale  # adj(T_new) / adj(T_old)

    archive_path = factors_root / archive_name
    old_dates: list[str] = []
    old_values: list[float] = []
    if stored_map is not None:
        stored_frame = stored_map.get(ts_code)
        if stored_frame is not None and not stored_frame.empty:
            old_dates = stored_frame["交易日期"].astype(str).tolist()
            old_values = (
                pd.to_numeric(stored_frame["复权因子"], errors="coerce").fillna(0.0).tolist()
            )
    elif archive_path.exists():
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                if info.is_dir() or _is_zip_noise(info.filename):
                    continue
                if re.search(re.escape(ts_code) + r"\.csv$", info.filename, re.I) is None:
                    continue
                try:
                    with archive.open(info.filename) as stream:
                        old_frame = pd.read_csv(stream)
                except (KeyError, ValueError, OSError):
                    continue
                if "交易日期" not in old_frame.columns or "复权因子" not in old_frame.columns:
                    continue
                old_frame = old_frame[["交易日期", "复权因子"]].dropna()
                old_dates.extend(old_frame["交易日期"].astype(str).tolist())
                old_values.extend(
                    pd.to_numeric(old_frame["复权因子"], errors="coerce").fillna(0.0).tolist()
                )

    if not old_dates:
        # No stored history: seed from the new day only.
        return _seed_factor_rows(ts_code=ts_code, new_rows=new_rows, anchor=anchor)

    stored = pd.DataFrame({"trade_date": old_dates, "value": old_values}).drop_duplicates(
        subset=["trade_date"], keep="last"
    )
    stored = stored[stored["value"] > 0].sort_values("trade_date")
    new_day = new_rows.sort_values("trade_date").drop_duplicates(subset=["trade_date"], keep="last")
    new_by_date = {
        str(row["trade_date"]): float(row["adj_factor"]) for _, row in new_day.iterrows()
    }

    merged_dates: list[str] = []
    merged_values: list[float] = []
    for _, row in stored.iterrows():
        merged_dates.append(str(row["trade_date"]))
        if anchor == "latest":
            merged_values.append(float(row["value"]) / ratio)
        else:
            merged_values.append(float(row["value"]))
    existing_dates = set(merged_dates)
    last_old = float(stored["value"].iloc[-1]) if not stored.empty else 1.0
    for trade_date in sorted(new_by_date):
        if trade_date in existing_dates:
            continue
        merged_dates.append(trade_date)
        if anchor == "latest":
            # qfq(T) = adj(T)/adj(T_end); only the newest day equals 1.0.
            merged_values.append(float(new_by_date[trade_date]) / new_scale)
        else:
            # hfq(T) = hfq_old(T_old) * adj(T)/adj(T_old); history is unchanged.
            merged_values.append(last_old * float(new_by_date[trade_date]) / old_scale)

    merged = (
        pd.DataFrame({"交易日期": merged_dates, "复权因子": merged_values})
        .sort_values("交易日期")
        .drop_duplicates(subset=["交易日期"], keep="last")
    )
    merged = merged[merged["复权因子"] > 0]
    if merged.empty:
        return None
    merged.insert(0, "股票代码", ts_code)
    return merged


def _run_batch(
    *,
    api: TushareProvider,
    end_date: date,
    daily_root: Path,
    factors_root: Path,
    skip_factors: bool,
    dry_run: bool,
    index_path: str,
) -> dict[str, object]:
    """Batch nightly update: one full-market call per trade date."""
    pro = api._resolve_pro_api() if not dry_run else None
    end_s = end_date.strftime("%Y%m%d")

    trade_dates: list[str] = []
    if not dry_run:
        probe_start = (end_date - timedelta(days=5)).strftime("%Y%m%d")
        try:
            raw_cal = api._call_with_retry(
                lambda: pro.trade_cal(
                    exchange="SSE",
                    is_open="1",
                    start_date=probe_start,
                    end_date=end_s,
                )
            )
            cal = _as_frame(raw_cal)
            if not cal.empty and "cal_date" in cal.columns:
                trade_dates = sorted(
                    {
                        str(item)
                        for item in cal["cal_date"].astype(str).tolist()
                        if str(item).isdigit() and len(str(item)) == 8
                    }
                )
        except Exception as exc:  # pragma: no cover - network dependent
            return {
                "attempted": True,
                "ok": False,
                "mode": "batch",
                "errors": [f"trade_cal_failed:{exc.__class__.__name__}:{exc}"],
                "dates_processed": 0,
                "dates_failed": [],
                "symbols_updated": 0,
            }
    if not trade_dates:
        trade_dates = [end_s]

    updates_by_year: dict[int, dict[str, pd.DataFrame]] = {}
    factor_updates: dict[str, pd.DataFrame] = {}
    dates_failed: list[str] = []
    date_errors: list[str] = []
    updated_codes: set[str] = set()
    fetched_symbols = 0

    for trade_date in trade_dates:
        if dry_run:
            continue
        try:
            day = _fetch_market_wide_by_date(api=api, trade_date=trade_date)
        except Exception as exc:  # pragma: no cover - network dependent
            # 异常明细必须记录：8-13 现场 0 成功 + 0 失败，正是这里把
            # 限频/接口失败吞成空 errors，导致 stock_updater.sh 的 judge
            # 误判「仅预期 empty 失败」而放行（rc=0 无告警）。
            dates_failed.append(trade_date)
            date_errors.append(f"{trade_date}:{type(exc).__name__}:{exc}")
            continue
        if day["daily"].empty:
            dates_failed.append(trade_date)
            date_errors.append(f"{trade_date}:empty_market_daily")
            continue
        _distribute_batch_day(
            daily=day["daily"],
            basic=day["basic"],
            adj=day["adj"],
            updates_by_year=updates_by_year,
            factor_updates=factor_updates,
            skip_factors=skip_factors,
        )
        for code in day["daily"]["ts_code"].astype(str).tolist():
            updated_codes.add(code)
        fetched_symbols += len(day["daily"])

    rebuild_reports: list[dict[str, object]] = []
    rebuild_latest_dates: dict[str, date] = {}
    if not dry_run:
        for year in sorted(updates_by_year):
            report = _rebuild_daily_year_zip(daily_root, year, updates_by_year[year])
            rebuild_reports.append(report)
            for symbol, raw_latest in cast(
                "dict[str, object]", report.get("latest_dates", {})
            ).items():
                latest = _coerce_date(raw_latest)
                if latest is None:
                    continue
                current = rebuild_latest_dates.get(symbol)
                if current is None or latest > current:
                    rebuild_latest_dates[symbol] = latest
        if not skip_factors and factor_updates:
            # Two market-wide anchor calls replace the per-symbol full history.
            adj_old_day: pd.DataFrame = pd.DataFrame()
            adj_new_day: pd.DataFrame = pd.DataFrame()
            try:
                adj_new_day = _fetch_adj_factor_paged(api, trade_date=end_s)
                previous_factor_date = _latest_factor_anchor_date(
                    factors_root=factors_root,
                    trade_dates=trade_dates,
                    api=api,
                )
                if previous_factor_date is not None:
                    adj_old_day = _fetch_adj_factor_paged(api, trade_date=previous_factor_date)
            except Exception as exc:  # pragma: no cover - network dependent
                rebuild_reports.append(
                    {
                        "archive": "factors",
                        "error": f"factor_anchor_fetch_failed:{exc.__class__.__name__}",
                    }
                )
            if not adj_new_day.empty:
                qfq_stored = _load_factor_entry_map(factors_root, FACTORS_QFQ_ARCHIVE)
                hfq_stored = _load_factor_entry_map(factors_root, FACTORS_HFQ_ARCHIVE)
                qfq_updates: dict[str, pd.DataFrame] = {}
                hfq_updates: dict[str, pd.DataFrame] = {}
                for code in sorted(factor_updates):
                    merged_qfq = _merge_factor_rows_scaled(
                        ts_code=code,
                        adj_new_day=factor_updates[code],
                        adj_old_day=(
                            adj_old_day[adj_old_day["ts_code"] == code]
                            if not adj_old_day.empty
                            else None
                        ),
                        factors_root=factors_root,
                        archive_name=FACTORS_QFQ_ARCHIVE,
                        anchor="latest",
                        stored_map=qfq_stored,
                    )
                    merged_hfq = _merge_factor_rows_scaled(
                        ts_code=code,
                        adj_new_day=factor_updates[code],
                        adj_old_day=(
                            adj_old_day[adj_old_day["ts_code"] == code]
                            if not adj_old_day.empty
                            else None
                        ),
                        factors_root=factors_root,
                        archive_name=FACTORS_HFQ_ARCHIVE,
                        anchor="earliest",
                        stored_map=hfq_stored,
                    )
                    if merged_qfq is not None:
                        qfq_updates[code] = merged_qfq
                    if merged_hfq is not None:
                        hfq_updates[code] = merged_hfq
                if qfq_updates:
                    rebuild_reports.append(
                        _rebuild_factor_zip(factors_root, FACTORS_QFQ_ARCHIVE, qfq_updates)
                    )
                if hfq_updates:
                    rebuild_reports.append(
                        _rebuild_factor_zip(factors_root, FACTORS_HFQ_ARCHIVE, hfq_updates)
                    )

    index_report: dict[str, object] = {"updated": False, "reason": "not_enabled"}
    if not dry_run and index_path.strip() and updated_codes:
        index_report = _update_last_date_index(
            index_path=index_path,
            daily_root=daily_root,
            updated_symbols=updated_codes,
            rebuild_latest_dates=rebuild_latest_dates,
        )

    return {
        "attempted": True,
        "ok": not dates_failed,
        "mode": "batch",
        "trade_dates": trade_dates,
        "dates_processed": len(trade_dates) - len(dates_failed),
        "dates_failed": dates_failed,
        # 失败原因明细（8-13 现场为空导致 judge 误判的根因），供 shell 层
        # judge 区分「真失败」与「合法 empty」。
        "errors": date_errors,
        "symbols_fetched": fetched_symbols,
        "symbols_updated": len(updated_codes),
        "latest_daily_date": (
            max(rebuild_latest_dates.values()).isoformat() if rebuild_latest_dates else ""
        ),
        "zip_rebuilds": rebuild_reports,
        "index": index_report,
    }


def _latest_factor_anchor_date(
    *,
    factors_root: Path,
    trade_dates: list[str],
    api: TushareProvider,
) -> str | None:
    """Best-effort previous trading day for the qfq anchor rescale."""
    archive_path = factors_root / FACTORS_QFQ_ARCHIVE
    if archive_path.exists():
        latest: str = ""
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                if info.is_dir() or _is_zip_noise(info.filename):
                    continue
                if not info.filename.endswith(".csv"):
                    continue
                try:
                    with archive.open(info.filename) as stream:
                        last_line = b""
                        for line in stream:
                            if line.strip():
                                last_line = line
                except KeyError:
                    continue
                if not last_line:
                    continue
                parts = last_line.decode("utf-8-sig", errors="replace").strip().split(",")
                if len(parts) >= 2 and len(parts[1].strip()) == 8 and parts[1].strip().isdigit():
                    candidate = parts[1].strip()
                    if candidate > latest:
                        latest = candidate
        if latest:
            return latest
    if len(trade_dates) >= 2:
        return trade_dates[-2]
    try:
        probe_from = (
            (pd.to_datetime(trade_dates[0], format="%Y%m%d") - pd.Timedelta(days=10)).strftime(
                "%Y%m%d"
            )
            if trade_dates
            else "20260101"
        )
        cal = _as_frame(
            api._call_with_retry(
                lambda: api._resolve_pro_api().trade_cal(
                    exchange="SSE",
                    is_open="1",
                    start_date=probe_from,
                    end_date=trade_dates[0] if trade_dates else "",
                )
            )
        )
        if not cal.empty and "cal_date" in cal.columns:
            dates = sorted(
                {
                    str(item)
                    for item in cal["cal_date"].astype(str).tolist()
                    if str(item).isdigit() and len(str(item)) == 8
                }
            )
            if len(dates) >= 2:
                return dates[-2]
    except Exception:
        pass
    return None


def _latest_index_date(index_path: str | Path) -> date | None:
    if not str(index_path).strip():
        return None
    payload = _load_last_date_index(index_path)
    if payload is None:
        return None
    symbols = payload.get("symbols")
    if not isinstance(symbols, dict):
        return None
    latest_dates = [
        parsed
        for raw in symbols.values()
        if isinstance(raw, dict)
        for parsed in [_coerce_date(raw.get("latest_date"))]
        if parsed is not None
    ]
    return max(latest_dates, default=None)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vendor-root",
        default="/data",
        help="Vendor library root (container default /data; the NAS mount of the "
        "'股票历史数据' directory containing 全A日K/ and 复权因子/)",
    )
    parser.add_argument(
        "--daily-dir",
        default="全A日K",
        help="Annual daily ZIP directory name under --vendor-root",
    )
    parser.add_argument(
        "--factors-dir",
        default="复权因子",
        help="Adjustment-factor ZIP directory name under --vendor-root",
    )
    parser.add_argument(
        "--end-date",
        default="",
        help="Latest trade date to fetch (YYYY-MM-DD); defaults to today",
    )
    parser.add_argument(
        "--symbols-file",
        default="",
        help="Text file with one symbol per line (6-digit or ts_code); "
        "defaults to symbols already present in the daily ZIPs",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Cap the number of symbols processed this run (0 = unlimited)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report what would be fetched/rebuilt; never call the API or write",
    )
    parser.add_argument(
        "--checkpoint",
        default="",
        help="Checkpoint file path (record-only; skip decisions always read ZIPs)",
    )
    parser.add_argument(
        "--interval-sec",
        type=float,
        default=0.6,
        help="Minimum seconds between API calls and retry backoff base",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Maximum API attempts per call (bounded exponential backoff)",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=1,
        help="Phase A worker threads (API calls stay globally rate-limited)",
    )
    parser.add_argument(
        "--skip-factors",
        action="store_true",
        help="Do not fetch adj_factor and do not rebuild the factor ZIPs",
    )
    parser.add_argument(
        "--force-factors",
        action="store_true",
        help="Refetch and rebuild adjustment factors even when the factor ZIP "
        "already covers --end-date (repair truncated factor history)",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Batch mode: one full-market trade_date call instead of per-symbol "
        "API calls (requires full-market permission, see "
        "scripts/verify_tushare_batch_permission.py)",
    )
    parser.add_argument(
        "--batch-trade-date",
        default="",
        help="Batch mode: single trade date to fetch (YYYY-MM-DD); defaults to --end-date",
    )
    parser.add_argument(
        "--index-path",
        default="",
        help="Last-date index JSON (vendor_zip_overlay format) used to skip "
        "annual-ZIP scans; incrementally updated after a successful run. "
        "Must live OUTSIDE the vendor directory (e.g. /vol1/docker/tools/).",
    )
    parser.add_argument(
        "--sync-vendor-delta",
        default="",
        help="Delta DuckDB path to incrementally sync after a successful run "
        "(import_vendor_zip_to_delta.py --incremental); requires --index-path. "
        "Example: /app/artifacts/vendor_delta/market_delta.duckdb",
    )
    # Legacy intraday summary args: kept for backwards-compat with old
    # stock_updater.sh / crontab invocations that still pass them.  They
    # are now no-ops (the nightly readiness gate replaces the intraday
    # summary build) but must not hard-fail.
    parser.add_argument(
        "--intraday-summary-output",
        default=os.environ.get("SA_INTRADAY_SUMMARY_OUTPUT", ""),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--intraday-summary-keep-days",
        type=int,
        default=int(os.environ.get("SA_INTRADAY_SUMMARY_KEEP_DAYS", "480")),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--intraday-summary-required-latest-date",
        default=os.environ.get("SA_INTRADAY_SUMMARY_REQUIRED_LATEST_DATE", ""),
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    # Surface deprecation warning when legacy args are actually used.
    try:
        import warnings as _warnings

        if str(getattr(args, "intraday_summary_output", "")).strip():
            _warnings.warn(
                "--intraday-summary-output is deprecated and ignored; "
                "the nightly readiness gate replaces the intraday summary build",
                DeprecationWarning,
                stacklevel=2,
            )
    except Exception:
        pass

    vendor_root = Path(args.vendor_root).expanduser()
    daily_root = vendor_root / args.daily_dir
    factors_root = vendor_root / args.factors_dir
    end_date = _coerce_date(args.end_date) or date.today()
    checkpoint_path = Path(args.checkpoint).expanduser() if args.checkpoint.strip() else None
    checkpoint: dict[str, object] = (
        _load_checkpoint(checkpoint_path) if checkpoint_path is not None else {}
    )

    if args.symbols_file.strip():
        raw_path = Path(args.symbols_file.strip()).expanduser()
        symbols = [
            line.strip()
            for line in raw_path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        ]
    else:
        if not daily_root.is_dir():
            print(
                "no daily vendor directory found and no --symbols-file given",
                file=sys.stderr,
            )
            return 2
        symbols = _list_daily_symbols(daily_root)
    normalized = {
        item
        for item in (
            re.sub(
                r"\.(SH|SZ|BJ)$",
                "",
                str(symbol).strip().upper(),
                flags=re.I,
            )
            for symbol in symbols
        )
        if re.fullmatch(r"\d{6}", item)
    }
    symbols = sorted(normalized)

    limit = max(0, int(args.limit))
    if limit:
        symbols = symbols[:limit]

    if not args.batch and not symbols:
        print("empty universe: nothing to update", file=sys.stderr)
        return 2

    token = (
        str(os.environ.get("TUSHARE_TOKEN", "") or "").strip()
        or str(os.environ.get("SA__MARKET_WAREHOUSE__TUSHARE_TOKEN", "") or "").strip()
    )
    if args.dry_run:
        api: TushareProvider | None = None
    else:
        if not token:
            print(
                "tushare token missing; export TUSHARE_TOKEN or "
                "SA__MARKET_WAREHOUSE__TUSHARE_TOKEN",
                file=sys.stderr,
            )
            return 2
        api = TushareProvider(
            token=token,
            retry_delay_sec=max(0.0, float(args.interval_sec)),
            min_request_interval_sec=max(0.0, float(args.interval_sec)),
            max_attempts=max(1, int(args.max_retries)),
            price_series_mode="raw",
        )

    results: list[dict[str, object]] = []
    failures: list[str] = []
    index = _load_last_date_index(args.index_path) if args.index_path.strip() else None

    # Unified batch/per-symbol dispatch: batch only decides how fetch/index
    # is done; delta sync + readiness + summary are shared tail so batch
    # never bypasses the incremental delta path (P0-1 fix).
    batch_payload: dict[str, object] | None = None
    batch_ok = False
    batch_latest_daily: date | None = None
    if args.batch:
        batch_end_date = _coerce_date(args.batch_trade_date) or end_date
        batch_payload = _run_batch(
            api=api,  # type: ignore[arg-type]
            end_date=batch_end_date,
            daily_root=daily_root,
            factors_root=factors_root,
            skip_factors=bool(args.skip_factors),
            dry_run=bool(args.dry_run),
            index_path=args.index_path,
        )
        batch_ok = bool(batch_payload.get("ok", False))
        batch_latest_daily = _coerce_date(batch_payload.get("latest_daily_date", ""))

    max_workers = max(1, int(args.max_workers))
    if len(symbols) == 1 or max_workers == 1 or api is None:
        for symbol in symbols:
            try:
                results.append(
                    _fetch_symbol(
                        api=api,  # type: ignore[arg-type]
                        symbol=symbol,
                        end_date=end_date,
                        daily_root=daily_root,
                        factors_root=factors_root,
                        skip_factors=bool(args.skip_factors),
                        force_factors=bool(args.force_factors),
                        dry_run=bool(args.dry_run),
                        index=index,
                    )
                )
            except Exception as exc:
                failures.append(f"{symbol}:{type(exc).__name__}:{exc}")
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(
                    _fetch_symbol,
                    api=api,
                    symbol=symbol,
                    end_date=end_date,
                    daily_root=daily_root,
                    factors_root=factors_root,
                    skip_factors=bool(args.skip_factors),
                    force_factors=bool(args.force_factors),
                    dry_run=bool(args.dry_run),
                    index=index,
                ): symbol
                for symbol in symbols
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    failures.append(f"{symbol}:{type(exc).__name__}:{exc}")

    # In batch mode per-symbol results are empty — derives success from batch_payload.
    if batch_payload is not None:
        batch_failures: list[str] = []
        raw_batch_errors = batch_payload.get("errors")
        if isinstance(raw_batch_errors, list):
            batch_failures = [str(item) for item in raw_batch_errors if str(item).strip()]
        # Only treat batch as failed when there are real errors; empty batch with
        # no failures is a legitimate no-op (e.g. capped by --limit in tests).
        if not batch_ok and not batch_failures and not args.dry_run:
            # _run_batch reports ok=False only on fatal path; keep failures empty
            # so the readiness gate will block (target date still written via fallback).
            pass
        # Merge batch errors into failures for unified exit-code/ readiness gate.
        failures.extend(batch_failures)
        if not batch_ok and batch_failures:
            # keep failures non-empty so shared tail returns 1
            pass
        # No per-symbol ok_results in batch — delta/index success is derived from
        # batch_payload's own index report; keep ok_results empty so shared ZIP
        # rebuild block is skipped (ZIPs already rebuilt inside _run_batch).
        ok_results: list[dict[str, object]] = []  # type: ignore[no-redef]
        skipped_results: list[dict[str, object]] = []  # type: ignore[no-redef]
    else:
        ok_results = [
            result for result in results if result.get("status") not in {"skipped", "error"}
        ]
        skipped_results = [result for result in results if result.get("status") == "skipped"]

    # Incremental record-only checkpoint: each symbol's fetch completion.
    if checkpoint_path is not None and not args.dry_run:
        for result in ok_results:
            _save_checkpoint(checkpoint_path, checkpoint, str(result.get("symbol")))

    # ---------------------------------------------------------------------
    # Phase B: ZIP rebuild (runs once per affected archive, after fetch).
    # ---------------------------------------------------------------------
    rebuild_reports: list[dict[str, object]] = []
    rebuild_latest_dates: dict[str, date] = {}
    if not args.dry_run:
        daily_updates: dict[str, dict[int, pd.DataFrame]] = {}
        for result in ok_results:
            daily_frame = result.get("daily")
            if not isinstance(daily_frame, pd.DataFrame) or daily_frame.empty:
                continue
            ts_code = str(result.get("ts_code"))
            year_frame = daily_frame.groupby(
                pd.to_datetime(daily_frame["datetime"], format="%Y%m%d").dt.year
            )
            for year, group in year_frame:
                if pd.isna(year):
                    continue
                daily_updates.setdefault(int(year), {})[ts_code] = group

        for year in sorted(daily_updates):
            report = _rebuild_daily_year_zip(daily_root, year, daily_updates[year])
            rebuild_reports.append(report)
            for symbol, raw_latest in cast(
                "dict[str, object]", report.get("latest_dates", {})
            ).items():
                latest = _coerce_date(raw_latest)
                if latest is None:
                    continue
                current = rebuild_latest_dates.get(symbol)
                if current is None or latest > current:
                    rebuild_latest_dates[symbol] = latest

        if not args.skip_factors:
            qfq_updates: dict[str, pd.DataFrame] = {}
            hfq_updates: dict[str, pd.DataFrame] = {}
            for result in ok_results:
                adj = result.get("adj")
                if not isinstance(adj, pd.DataFrame) or adj.empty:
                    continue
                ts_code = str(result.get("ts_code"))
                qfq_updates[ts_code] = _factor_rows(ts_code=ts_code, adj=adj, anchor="latest")
                hfq_updates[ts_code] = _factor_rows(ts_code=ts_code, adj=adj, anchor="earliest")
            if qfq_updates:
                rebuild_reports.append(
                    _rebuild_factor_zip(factors_root, FACTORS_QFQ_ARCHIVE, qfq_updates)
                )
            if hfq_updates:
                rebuild_reports.append(
                    _rebuild_factor_zip(factors_root, FACTORS_HFQ_ARCHIVE, hfq_updates)
                )

    index_report: dict[str, object] = {"updated": False, "reason": "not_enabled"}
    if not args.dry_run and args.index_path.strip() and ok_results:
        updated_codes = {
            str(result.get("ts_code")) for result in ok_results if str(result.get("ts_code"))
        }
        if updated_codes:
            index_report = _update_last_date_index(
                index_path=args.index_path,
                daily_root=daily_root,
                updated_symbols=updated_codes,
                rebuild_latest_dates=rebuild_latest_dates,
            )

    # Delta baseline incremental sync: after the ZIPs and the last-date index
    # are updated, mirror the new rows into the delta DuckDB so the Week5
    # batch keeps reading the fast DuckDB path.
    # NOTE: batch mode has no per-symbol ok_results; gate on failures instead.
    delta_should_sync = bool(batch_payload is not None and batch_ok and not failures)
    if not delta_should_sync:
        delta_should_sync = bool(ok_results)
    delta_sync_report: dict[str, object] = {"updated": False, "reason": "not_enabled"}
    if (
        not args.dry_run
        and args.sync_vendor_delta.strip()
        and args.index_path.strip()
        and delta_should_sync
    ):
        try:
            # 显式按路径加载同目录脚本，不依赖 sys.path 恰好包含 scripts/：
            # ``python -m`` 方式运行本脚本时 sys.path[0] 是 CWD，裸 import
            # 会 ModuleNotFoundError（虽然被降级，但钩子将永远不生效）。
            import contextlib
            import importlib.util
            import io

            _delta_script = Path(__file__).resolve().parent / "import_vendor_zip_to_delta.py"
            _spec = importlib.util.spec_from_file_location(
                "import_vendor_zip_to_delta", _delta_script
            )
            assert _spec is not None and _spec.loader is not None
            _delta_module = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_delta_module)

            # import 脚本的 JSON 报告走自己的 stdout：不重定向会污染本脚本
            # 的 summary 输出（下游解析会失败），把它收进 delta_sync 报告。
            _captured = io.StringIO()
            with contextlib.redirect_stdout(_captured):
                sync_rc = _delta_module._main(  # noqa: SLF001
                    [
                        "--data-root",
                        str(vendor_root),
                        "--index-path",
                        args.index_path,
                        "--delta-db-path",
                        args.sync_vendor_delta,
                        "--incremental",
                    ]
                )
            delta_sync_report = {"updated": sync_rc == 0, "exit_code": sync_rc}
            _delta_output = _captured.getvalue().strip()
            if _delta_output:
                try:
                    delta_sync_report["import_report"] = json.loads(_delta_output)
                except json.JSONDecodeError:
                    delta_sync_report["import_output"] = _delta_output
        except Exception as exc:
            delta_sync_report = {
                "updated": False,
                "reason": f"{type(exc).__name__}:{exc}",
            }

    # Write nightly readiness after successful full run (batch + per-symbol share tail).
    if not args.dry_run and not failures:
        _readiness_ok = True
        # Batch carries its own index report inside batch_payload; surface it when present.
        _batch_index_report: dict[str, object] | None = None
        if batch_payload is not None and isinstance(batch_payload.get("index"), dict):
            _batch_index_report = batch_payload["index"]  # type: ignore[assignment]
        _effective_index_report = (
            _batch_index_report if _batch_index_report is not None else index_report
        )
        if isinstance(_effective_index_report, dict) and str(
            _effective_index_report.get("reason", "")
        ).strip() not in ("", "not_enabled"):
            _readiness_ok = bool(_effective_index_report.get("updated", False))
        if isinstance(delta_sync_report, dict) and str(
            delta_sync_report.get("reason", "")
        ).strip() not in ("", "not_enabled"):
            _readiness_ok = bool(delta_sync_report.get("updated", False))
        # Batch skips delta when neither flag is set — that is expected, not a failure.
        if batch_payload is not None and not args.sync_vendor_delta.strip():
            # No delta requested: delta_sync stays not_enabled, treat as ok.
            _readiness_ok = _readiness_ok and True
        if _readiness_ok:
            try:
                # Batch's latest_daily_date already reflects ZIP rebuild date.
                _readiness_date = (
                    batch_latest_daily
                    if batch_payload is not None and batch_latest_daily is not None
                    else end_date
                )
                write_nightly_readiness(
                    target_trade_date=_readiness_date,
                    db_path=args.sync_vendor_delta,
                    index_path=args.index_path,
                    extra={
                        "source": "batch_update"
                        if batch_payload is not None
                        else "per_symbol_update"
                    },
                )
            except Exception:
                pass  # Non-fatal: readiness is a signal, not a gate.

    # Merge batch index into index_report for observability when in batch mode.
    if batch_payload is not None and isinstance(batch_payload.get("index"), dict):
        index_report = batch_payload["index"]  # type: ignore[assignment]

    summary = {
        "tool": "update_vendor_daily_from_tushare",
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "vendor_root": str(vendor_root),
        "daily_dir": str(daily_root),
        "factors_dir": str(factors_root),
        "end_date": end_date.isoformat(),
        "symbols_total": len(symbols),
        "fetched": len(ok_results)
        if batch_payload is None
        else int(batch_payload.get("symbols_updated", 0) or 0),  # type: ignore[arg-type]
        "skipped": len(skipped_results),
        "failed": len(failures),
        "failures": failures[:100],
        "dry_run": bool(args.dry_run),
        "skip_factors": bool(args.skip_factors),
        "checkpoint": str(checkpoint_path) if checkpoint_path is not None else "",
        "zip_rebuilds": rebuild_reports
        if batch_payload is None
        else list(batch_payload.get("zip_rebuilds", []) or []),  # type: ignore[arg-type]
        "index": index_report,
        "delta_sync": delta_sync_report,
        "mode": "batch" if batch_payload is not None else "per_symbol",
    }
    # Back-compat: also spread batch_payload keys for callers parsing symbols_fetched etc.
    if batch_payload is not None:
        for _k in (
            "symbols_fetched",
            "symbols_updated",
            "trade_dates",
            "dates_failed",
            "latest_daily_date",
        ):
            if _k not in summary and _k in batch_payload:
                summary[_k] = batch_payload[_k]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if failures:
        return 1
    # Batch ok=False with no explicit failures still means blocked readiness; return 1.
    if batch_payload is not None and not batch_ok and not args.dry_run:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
