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
                    usecols=lambda column: column
                    in {"datetime", "trade_date", "date", "交易日期"},
                )
        except (KeyError, ValueError):
            return None
    if frame.empty:
        return None
    date_column = next(
        (
            name
            for name in ("datetime", "trade_date", "date", "交易日期")
            if name in frame.columns
        ),
        "",
    )
    if not date_column:
        return None
    parsed = pd.to_datetime(frame[date_column].astype(str), errors="coerce")
    parsed = parsed.dropna()
    if parsed.empty:
        return None
    return parsed.max().date()


def _symbol_daily_last_date(daily_root: Path, ts_code: str) -> date | None:
    """Max trade date across the symbol's entries in all annual daily ZIPs."""
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
    frame["datetime"] = (
        frame["datetime"].astype(str).str.replace("-", "", regex=False)
    )
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
    frame = frame.sort_values("trade_date").drop_duplicates(
        subset=["trade_date"], keep="last"
    )
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
    """Rebuild one annual daily ZIP once; updated symbols' entries replaced.

    The replacement entry of a symbol/year is the MERGE of the rows already
    stored in the archive (same year, kept byte-semantics via re-parsing) and
    the newly fetched rows, deduplicated by date with the newest row winning.
    """
    archive_path = daily_root / f"{year}.zip"
    replace_entries: dict[str, str] = {}
    for ts_code, fresh in sorted(updates.items()):
        entry_name = f"{year}/{ts_code}.csv"
        merged = _read_zip_entry_csv(archive_path, entry_name)
        if merged is None or merged.empty:
            merged = fresh
        else:
            merged = pd.concat([merged, fresh], axis=0, sort=False, ignore_index=True)
        rendered = _render_daily_csv(merged)
        if rendered:
            replace_entries[entry_name] = rendered
    return _rebuild_zip(archive_path, replace_entries)


def _rebuild_zip(
    archive_path: Path,
    replace_entries: dict[str, str],
) -> dict[str, object]:
    """Atomically rebuild one ZIP with the given entries replaced wholesale."""
    if not archive_path.parent.exists():
        archive_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive_path.with_name(f"{archive_path.name}.{os.getpid()}.tmp")
    old_names: set[str] = set()
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as output:
        if archive_path.exists():
            with zipfile.ZipFile(archive_path) as source:
                old_names = {
                    info.filename
                    for info in source.infolist()
                    if not info.is_dir()
                }
                for info in source.infolist():
                    if info.is_dir() or info.filename in replace_entries:
                        continue
                    output.writestr(info, source.read(info.filename))
        for name, content in replace_entries.items():
            output.writestr(name, content)
    os.replace(temporary, archive_path)

    written_names: list[str] = []
    with zipfile.ZipFile(archive_path) as check:
        written_names = [
            info.filename for info in check.infolist() if not info.is_dir()
        ]
        for name in replace_entries:
            if name not in written_names:
                raise DataSourceError(
                    f"vendor ZIP rebuild lost entry: {archive_path}!{name}"
                )
    duplicate_names = sorted(
        {name for name in written_names if written_names.count(name) > 1}
    )
    if duplicate_names:
        raise DataSourceError(
            f"vendor ZIP rebuild produced duplicate entries: {archive_path}: "
            f"{duplicate_names}"
        )
    missing_names = sorted(old_names - set(written_names))
    if missing_names:
        raise DataSourceError(
            f"vendor ZIP rebuild dropped entries: {archive_path}: {missing_names}"
        )
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
    dry_run: bool,
) -> dict[str, object]:
    """Phase A worker: decide by ZIP contents and fetch only missing data."""
    ts_code = _to_ts_code(symbol)
    daily_last = _symbol_daily_last_date(daily_root, ts_code)
    factor_last = None if skip_factors else _symbol_factor_last_date(factors_root, ts_code)
    need_daily = daily_last is None or daily_last < end_date
    need_factors = (not skip_factors) and (factor_last is None or factor_last < end_date)
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
        raw_adj = api._call_with_retry(
            lambda: pro.adj_factor(
                ts_code=ts_code,
                start_date=adj_start_s,
                end_date=end_s,
            )
        )
        result["adj"] = _as_frame(raw_adj)

    if need_daily:
        result["daily"] = _daily_to_25_columns(ts_code=ts_code, daily=daily, basic=basic)
    return result


def _as_frame(raw: object) -> pd.DataFrame:
    if isinstance(raw, pd.DataFrame):
        return raw.copy()
    return pd.DataFrame()


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
    args = parser.parse_args(argv)

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
    if not symbols:
        print("empty universe: nothing to update", file=sys.stderr)
        return 2

    limit = max(0, int(args.limit))
    if limit:
        symbols = symbols[:limit]

    token = str(os.environ.get("TUSHARE_TOKEN", "") or "").strip() or str(
        os.environ.get("SA__MARKET_WAREHOUSE__TUSHARE_TOKEN", "") or ""
    ).strip()
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
                        dry_run=bool(args.dry_run),
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
                    dry_run=bool(args.dry_run),
                ): symbol
                for symbol in symbols
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    failures.append(f"{symbol}:{type(exc).__name__}:{exc}")

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
            rebuild_reports.append(
                _rebuild_daily_year_zip(daily_root, year, daily_updates[year])
            )

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

    summary = {
        "tool": "update_vendor_daily_from_tushare",
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "vendor_root": str(vendor_root),
        "daily_dir": str(daily_root),
        "factors_dir": str(factors_root),
        "end_date": end_date.isoformat(),
        "symbols_total": len(symbols),
        "fetched": len(ok_results),
        "skipped": len(skipped_results),
        "failed": len(failures),
        "failures": failures[:100],
        "dry_run": bool(args.dry_run),
        "skip_factors": bool(args.skip_factors),
        "checkpoint": str(checkpoint_path) if checkpoint_path is not None else "",
        "zip_rebuilds": rebuild_reports,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
