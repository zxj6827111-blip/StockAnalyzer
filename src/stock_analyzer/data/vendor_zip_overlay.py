"""Read-only local-vendor ZIP history with a small writable DuckDB overlay."""

from __future__ import annotations

import csv
import json
import re
import zipfile
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast

import numpy as np
import pandas as pd

from stock_analyzer.data.intraday_summary import summarize_minute_bars
from stock_analyzer.data.market_warehouse import MarketWarehouse
from stock_analyzer.data.provider import DataSourceError
from stock_analyzer.data.tdx_offline_provider import (
    _normalize_frame,
    _normalize_symbol,
)

VENDOR_ZIP_INDEX_VERSION = 1
_YEAR_ARCHIVE_RE = re.compile(r"^(?P<year>\d{4})(?:\((?P<copy>\d+)\))?\.zip$", re.I)
_DAILY_ENTRY_RE = re.compile(r"(?P<code>\d{6})\.(?:SH|SZ|BJ)\.csv$", re.I)
_MINUTE_ARCHIVE_RE = re.compile(r"^(?P<year>\d{4})(?:-(?P<month>\d{2}))?", re.I)

# Explicit access modes for the parsed-ZIP / delta DuckDB cache:
# - read_write: production API, the delta DuckDB can be created and written;
# - read_only: probes/audits, the delta DuckDB is opened read-only and can
#   never be created, mutated or have its schema touched;
# - disabled: no delta DuckDB at all, the overlay reads ZIP archives only.
_DELTA_ACCESS_MODES = frozenset({"read_write", "read_only", "disabled"})


def build_vendor_zip_daily_index(
    *,
    root: str | Path,
    daily_dir_name: str = "全A日K",
) -> dict[str, object]:
    """Build a compact entry index without extracting the annual ZIP archives."""
    source_root = Path(root).expanduser().resolve()
    daily_root = source_root / daily_dir_name
    if not daily_root.exists() or not daily_root.is_dir():
        raise DataSourceError(f"vendor daily directory does not exist: {daily_root}")

    selected_archives, ignored_archives = _select_canonical_daily_archives(daily_root)
    if not selected_archives:
        raise DataSourceError(f"no annual vendor daily ZIP archives found under: {daily_root}")

    symbol_entries: dict[str, list[dict[str, object]]] = {}
    for year, archive_path in selected_archives:
        relative_archive = archive_path.relative_to(source_root).as_posix()
        with zipfile.ZipFile(archive_path) as archive:
            for entry in archive.infolist():
                if entry.is_dir() or _is_zip_noise(entry.filename):
                    continue
                match = _DAILY_ENTRY_RE.search(entry.filename)
                if match is None:
                    continue
                symbol = _normalize_symbol(match.group("code"))
                if not symbol:
                    continue
                symbol_entries.setdefault(symbol, []).append(
                    {
                        "year": year,
                        "zip": relative_archive,
                        "entry": entry.filename,
                        "size": int(entry.file_size),
                    }
                )

    latest_refs_by_archive: dict[str, list[tuple[str, str]]] = {}
    for symbol, entries in symbol_entries.items():
        entries.sort(
            key=lambda item: (
                _coerce_int(item.get("year")),
                str(item.get("zip", "")),
            )
        )
        latest_entry = entries[-1]
        latest_refs_by_archive.setdefault(str(latest_entry["zip"]), []).append(
            (symbol, str(latest_entry["entry"]))
        )

    latest_dates: dict[str, str] = {}
    for relative_archive, refs in latest_refs_by_archive.items():
        archive_path = source_root / Path(relative_archive)
        with zipfile.ZipFile(archive_path) as archive:
            for symbol, entry_name in refs:
                latest_date = _read_last_trade_date(
                    archive=archive,
                    entry_name=entry_name,
                )
                if latest_date:
                    latest_dates[symbol] = latest_date

    symbols_payload: dict[str, object] = {}
    for symbol in sorted(symbol_entries):
        symbols_payload[symbol] = {
            "latest_date": latest_dates.get(symbol, ""),
            "entries": symbol_entries[symbol],
        }

    return {
        "version": VENDOR_ZIP_INDEX_VERSION,
        "generated_at": datetime.now().isoformat(),
        "root": str(source_root),
        "daily_dir": daily_dir_name,
        "archives_total": len(selected_archives),
        "symbols_total": len(symbols_payload),
        "ignored_duplicate_archives": [
            path.relative_to(source_root).as_posix() for path in ignored_archives
        ],
        "symbols": symbols_payload,
    }


def write_vendor_zip_daily_index(
    *,
    root: str | Path,
    output_path: str | Path,
    daily_dir_name: str = "全A日K",
) -> dict[str, object]:
    payload = build_vendor_zip_daily_index(root=root, daily_dir_name=daily_dir_name)
    target = Path(output_path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return payload


def load_vendor_zip_daily_index(path: str | Path) -> dict[str, object]:
    target = Path(path).expanduser()
    if not target.exists():
        raise DataSourceError(
            "vendor ZIP index does not exist: "
            f"{target}; run scripts/build_vendor_zip_daily_index.py first"
        )
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataSourceError(f"vendor ZIP index is unreadable: {target}: {exc}") from exc
    if not isinstance(payload, dict):
        raise DataSourceError(f"vendor ZIP index must contain a JSON object: {target}")
    version = int(payload.get("version", 0))
    if version != VENDOR_ZIP_INDEX_VERSION:
        raise DataSourceError(
            f"unsupported vendor ZIP index version: {version}; expected {VENDOR_ZIP_INDEX_VERSION}"
        )
    if not isinstance(payload.get("symbols"), dict):
        raise DataSourceError(f"vendor ZIP index has no symbols mapping: {target}")
    return cast(dict[str, object], payload)


@dataclass(slots=True)
class VendorZipOverlayProvider:
    """Merge immutable annual ZIP history with recent rows from DuckDB."""

    data_root: str
    index_path: str
    delta_db_path: str
    delta_package_root: str = ""
    daily_dir_name: str = "全A日K"
    price_series_mode: str = "raw"
    daily_volume_multiplier: float = 100.0
    daily_turnover_multiplier: float = 1000.0
    minute_volume_multiplier: float = 100.0
    minute_amount_multiplier: float = 1.0
    intraday_enabled: bool = True
    memory_cache_symbols: int = 32
    delta_access_mode: str = "read_write"
    _root: Path = field(init=False)
    _index: dict[str, object] = field(init=False)
    _warehouse: MarketWarehouse | None = field(init=False)
    _daily_cache: OrderedDict[str, pd.DataFrame] = field(default_factory=OrderedDict, init=False)
    _intraday_cache: OrderedDict[str, pd.DataFrame] = field(default_factory=OrderedDict, init=False)
    _minute_archives: dict[str, list[Path]] = field(default_factory=dict, init=False)
    _minute_entry_index: dict[Path, dict[str, list[str]]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._root = Path(self.data_root).expanduser()
        if not self._root.exists() or not self._root.is_dir():
            raise DataSourceError(f"vendor ZIP data root does not exist: {self._root}")
        self._index = load_vendor_zip_daily_index(self.index_path)
        normalized_mode = str(self.price_series_mode or "raw").strip().lower()
        if normalized_mode != "raw":
            raise DataSourceError(
                "vendor ZIP overlay currently supports only raw prices; "
                f"got: {self.price_series_mode}"
            )
        self.price_series_mode = normalized_mode
        access_mode = str(self.delta_access_mode or "read_write").strip().lower()
        if access_mode not in _DELTA_ACCESS_MODES:
            raise DataSourceError(
                f"unsupported delta_access_mode: {self.delta_access_mode}; "
                f"expected one of {sorted(_DELTA_ACCESS_MODES)}"
            )
        self.delta_access_mode = access_mode
        if access_mode == "disabled":
            self._warehouse = None
        else:
            package_root = (
                Path(self.delta_package_root).expanduser()
                if str(self.delta_package_root).strip()
                else Path(self.delta_db_path).expanduser().parent / "package"
            )
            self._warehouse = MarketWarehouse(
                db_path=Path(self.delta_db_path).expanduser(),
                package_root=package_root,
                package_writes_enabled=False,
                read_only=access_mode == "read_only",
            )
        self.memory_cache_symbols = max(1, int(self.memory_cache_symbols))

    def list_symbols(self) -> list[str]:
        symbols = self._symbols_mapping()
        return sorted(symbol for symbol in (_normalize_symbol(item) for item in symbols) if symbol)

    def latest_daily_dates(self, *, symbols: list[str] | None = None) -> dict[str, date]:
        requested = {
            normalized
            for normalized in (_normalize_symbol(item) for item in (symbols or []))
            if normalized
        }
        latest: dict[str, date] = {}
        for symbol, raw in self._symbols_mapping().items():
            normalized = _normalize_symbol(symbol)
            if not normalized or (requested and normalized not in requested):
                continue
            if not isinstance(raw, dict):
                continue
            parsed = _coerce_date(raw.get("latest_date"))
            if parsed is not None:
                latest[normalized] = parsed
        delta_warehouse = self._delta_warehouse()
        if delta_warehouse is not None:
            delta_latest = delta_warehouse.latest_daily_dates(
                symbols=sorted(requested) if requested else None
            )
            for symbol, value in delta_latest.items():
                current = latest.get(symbol)
                if current is None or value > current:
                    latest[symbol] = value
        return latest

    def fetch_daily_bars(
        self,
        symbol: str,
        lookback_days: int = 120,
        *,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        normalized = _normalize_symbol(symbol)
        if not normalized:
            raise DataSourceError(f"invalid vendor ZIP symbol: {symbol}")
        baseline = self._load_vendor_daily(normalized)
        delta_warehouse = self._delta_warehouse()
        delta = (
            delta_warehouse.fetch_daily_bars(
                normalized,
                lookback_days=max(1, int(lookback_days)),
                end_date=end_date,
            )
            if delta_warehouse is not None
            else pd.DataFrame()
        )
        if end_date is not None and not baseline.empty:
            baseline = baseline.loc[baseline.index <= pd.Timestamp(end_date)]
        merged = _merge_overlay_frames(baseline, delta)
        if merged.empty:
            raise DataSourceError(f"vendor ZIP and delta warehouse are empty for {normalized}")
        result = merged.tail(max(1, int(lookback_days))).copy()
        result.attrs["source"] = "vendor_zip_overlay"
        result.attrs["historical_root"] = str(self._root)
        result.attrs["delta_db_path"] = str(Path(self.delta_db_path).expanduser())
        return result

    def fetch_universe_quality_metrics(
        self,
        *,
        symbols: list[str],
        lookback_days: int,
    ) -> pd.DataFrame:
        """Batch-fetch per-symbol daily history (ZIP baseline + delta overlay).

        Single call covering the whole requested universe, mirroring
        ``MarketWarehouse.fetch_universe_quality_metrics`` so the Week5
        quality selector can treat the overlay as a full-market batch source.

        I/O rules:
        - requested symbols are normalized, de-duplicated and sorted;
        - the delta DuckDB is queried exactly once;
        - ZIP entries are grouped by annual archive and each annual archive is
          opened at most once per call, newest year first;
        - per symbol, reading stops once ``lookback_days`` rows accumulated,
          so earlier (older) annual files are skipped;
        - ZIP archives are never extracted and neither ZIPs, index, DuckDB nor
          named volumes are modified;
        - per-symbol rows never enter the small per-symbol LRU cache.

        Merge rules: ZIP provides the historical baseline, delta provides the
        recent increment, and on duplicate symbol/date the delta row wins.
        The final frame keeps at most the last ``lookback_days`` rows per
        symbol and is sorted by symbol, date (date is a plain column).
        """
        limit = max(1, int(lookback_days))
        normalized = sorted(
            {item for item in (_normalize_symbol(value) for value in (symbols or [])) if item}
        )
        if not normalized:
            return pd.DataFrame()

        delta = pd.DataFrame()
        delta_warehouse = self._delta_warehouse()
        if delta_warehouse is not None:
            delta = delta_warehouse.fetch_universe_quality_metrics(
                symbols=normalized,
                lookback_days=limit,
            )
            if not delta.empty:
                delta = delta[delta["symbol"].isin(normalized)]

        zip_frames = self._load_vendor_daily_batch(symbols=normalized, limit=limit)
        pieces = [frame for frame in zip_frames if not frame.empty]
        if not delta.empty:
            pieces.append(delta)
        if not pieces:
            return pd.DataFrame()

        combined = pd.concat(pieces, axis=0, sort=False, ignore_index=True)
        combined["date"] = pd.to_datetime(combined["date"], errors="coerce")
        combined = combined.dropna(subset=["date"])
        combined["symbol"] = combined["symbol"].astype(str)
        # Stable sort keeps ZIP rows before delta rows on the same date so
        # drop_duplicates(keep="last") lets the delta row win.
        combined = combined.sort_values(["symbol", "date"], kind="stable")
        combined = combined.drop_duplicates(subset=["symbol", "date"], keep="last")
        combined = combined.groupby("symbol", sort=False).tail(limit)
        combined = combined.sort_values(["symbol", "date"]).reset_index(drop=True)
        combined = self._apply_financial_snapshots_batch(combined, symbols=normalized)
        return combined

    def _apply_financial_snapshots_batch(
        self,
        frame: pd.DataFrame,
        *,
        symbols: list[str],
    ) -> pd.DataFrame:
        """Batch PIT as-of join of financial snapshots onto the merged frame.

        Reads the delta warehouse ``financial_snapshots`` table once for the
        whole universe and joins with a single vectorized ``merge_asof``
        (never one query per symbol). ZIP-only rows without any snapshot stay
        honestly missing; delta rows that already carry reported/derived
        financials are preserved.
        """
        if frame is None or frame.empty:
            return frame
        if not symbols:
            return frame
        delta_warehouse = self._delta_warehouse()
        if delta_warehouse is None:
            return frame
        snapshots = delta_warehouse.fetch_financial_snapshots_batch(symbols=symbols)
        if snapshots.empty:
            return frame
        from stock_analyzer.data.financial_pit import apply_financial_snapshots_asof_batch

        return cast(
            pd.DataFrame,
            apply_financial_snapshots_asof_batch(
                frame,
                snapshots,
                only_fill_pending=True,
            ),
        )

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        normalized = _normalize_symbol(symbol)
        interval_key = str(interval).strip().lower()
        if not normalized or interval_key not in {"1m", "5m"}:
            return pd.DataFrame()
        baseline = (
            self._load_vendor_intraday_summary(
                symbol=normalized,
                interval=interval_key,
                lookback_days=max(1, int(lookback_days)),
            )
            if self.intraday_enabled
            else pd.DataFrame()
        )
        delta = pd.DataFrame()
        delta_warehouse = self._delta_warehouse()
        if delta_warehouse is not None:
            delta = delta_warehouse.fetch_intraday_summary(
                normalized,
                interval_key,
                lookback_days=max(1, int(lookback_days)),
            )
        return _merge_overlay_frames(baseline, delta).tail(max(1, int(lookback_days)))

    def clear_cache(self) -> None:
        self._daily_cache.clear()
        self._intraday_cache.clear()

    def status(self) -> dict[str, object]:
        delta_warehouse = self._delta_warehouse()
        return {
            "source": "vendor_zip_overlay",
            "root": str(self._root),
            "index_path": str(Path(self.index_path).expanduser()),
            "daily_dir": self.daily_dir_name,
            "index_generated_at": str(self._index.get("generated_at", "")),
            "index_archives_total": _coerce_int(self._index.get("archives_total")),
            "symbols_total": len(self._symbols_mapping()),
            "delta_db_path": str(Path(self.delta_db_path).expanduser()),
            "delta_db_exists": bool(
                delta_warehouse is not None and delta_warehouse.db_path.exists()
            ),
            "delta_access_mode": self.delta_access_mode,
            "delta_package_root": str(
                delta_warehouse.package_root if delta_warehouse is not None else ""
            ),
            "delta_package_writes_enabled": bool(
                delta_warehouse is not None and delta_warehouse.package_writes_enabled
            ),
            "price_series_mode": self.price_series_mode,
            "intraday_enabled": bool(self.intraday_enabled),
        }

    def _delta_warehouse(self) -> MarketWarehouse | None:
        return self._warehouse

    def enforce_read_only_delta(self) -> None:
        """Flip the delta DuckDB access to read-only after construction.

        Probes construct the overlay through the normal read-write wiring and
        then guarantee immutability: the mode label and the underlying
        warehouse are both switched, so no database file is created, no table
        is written and the file's content/schema/mtime stay untouched.
        """
        self.delta_access_mode = "read_only"
        if self._warehouse is not None:
            self._warehouse.enforce_read_only()

    def _symbols_mapping(self) -> dict[str, object]:
        raw = self._index.get("symbols")
        return cast(dict[str, object], raw) if isinstance(raw, dict) else {}

    def _load_vendor_daily(self, symbol: str) -> pd.DataFrame:
        cached = self._daily_cache.get(symbol)
        if cached is not None:
            self._daily_cache.move_to_end(symbol)
            return cached.copy()
        raw_symbol = self._symbols_mapping().get(symbol)
        if not isinstance(raw_symbol, dict):
            return pd.DataFrame()
        raw_entries = raw_symbol.get("entries")
        if not isinstance(raw_entries, list):
            return pd.DataFrame()
        frames: list[pd.DataFrame] = []
        for item in raw_entries:
            if not isinstance(item, dict):
                continue
            relative_zip = str(item.get("zip", "")).strip()
            entry_name = str(item.get("entry", "")).strip()
            if not relative_zip or not entry_name:
                continue
            archive_path = self._root / Path(relative_zip)
            if not archive_path.exists():
                raise DataSourceError(f"vendor ZIP archive is missing: {archive_path}")
            with zipfile.ZipFile(archive_path) as archive:
                try:
                    with archive.open(entry_name) as stream:
                        raw = pd.read_csv(stream)
                except KeyError as exc:
                    raise DataSourceError(
                        f"vendor ZIP entry is missing: {archive_path}!{entry_name}"
                    ) from exc
            normalized = self._normalize_vendor_daily(raw=raw, symbol=symbol)
            if not normalized.empty:
                frames.append(normalized)
        merged = _merge_overlay_frames(*frames)
        self._remember(self._daily_cache, symbol, merged)
        return merged.copy()

    def _load_vendor_daily_batch(
        self,
        *,
        symbols: list[str],
        limit: int,
    ) -> list[pd.DataFrame]:
        """Read ZIP daily history for many symbols without per-symbol calls.

        Returns one long frame per symbol (date as a plain column), reading
        annual archives newest year first and stopping per symbol once
        ``limit`` rows have been accumulated. Each annual ZIP archive is
        opened at most once.
        """
        entries_by_symbol: dict[str, list[dict[str, object]]] = {}
        for symbol in symbols:
            raw_symbol = self._symbols_mapping().get(symbol)
            if not isinstance(raw_symbol, dict):
                continue
            raw_entries = raw_symbol.get("entries")
            if not isinstance(raw_entries, list):
                continue
            entries = [
                item
                for item in raw_entries
                if isinstance(item, dict)
                and str(item.get("zip", "")).strip()
                and str(item.get("entry", "")).strip()
            ]
            if entries:
                entries_by_symbol[symbol] = entries

        by_archive: dict[str, dict[str, str]] = {}
        archive_years: dict[str, int] = {}
        for symbol, entries in entries_by_symbol.items():
            for item in entries:
                relative_zip = str(item.get("zip", "")).strip()
                entry_name = str(item.get("entry", "")).strip()
                year = _coerce_int(item.get("year"))
                by_archive.setdefault(relative_zip, {})[symbol] = entry_name
                archive_years[relative_zip] = max(archive_years.get(relative_zip, 0), year)

        accumulated: dict[str, list[pd.DataFrame]] = {symbol: [] for symbol in symbols}
        remaining = {
            symbol: max(0, limit - sum(len(frame) for frame in frames))
            for symbol, frames in accumulated.items()
        }
        archive_order = sorted(archive_years, key=lambda path: archive_years[path], reverse=True)
        for relative_zip in archive_order:
            refs = by_archive[relative_zip]
            needed_symbols = [symbol for symbol in refs if remaining[symbol] > 0]
            if not needed_symbols:
                continue
            archive_path = self._root / Path(relative_zip)
            if not archive_path.exists():
                raise DataSourceError(f"vendor ZIP archive is missing: {archive_path}")
            with zipfile.ZipFile(archive_path) as archive:
                for symbol in needed_symbols:
                    entry_name = refs[symbol]
                    try:
                        with archive.open(entry_name) as stream:
                            raw = pd.read_csv(stream)
                    except KeyError as exc:
                        raise DataSourceError(
                            f"vendor ZIP entry is missing: {archive_path}!{entry_name}"
                        ) from exc
                    normalized = self._normalize_vendor_daily(raw=raw, symbol=symbol)
                    if not normalized.empty:
                        frames = accumulated[symbol]
                        frames.append(normalized)
                        remaining[symbol] = max(0, remaining[symbol] - len(normalized))

        long_frames: list[pd.DataFrame] = []
        for symbol in symbols:
            frames = accumulated[symbol]
            if not frames:
                continue
            merged = _merge_overlay_frames(*frames)
            if merged.empty:
                continue
            frame = merged.tail(limit).reset_index()
            frame.insert(0, "symbol", symbol)
            long_frames.append(frame)
        return long_frames

    def _normalize_vendor_daily(self, *, raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
        if raw.empty:
            return pd.DataFrame()
        frame = raw.copy()
        date_column = next(
            (name for name in ("datetime", "trade_date", "date") if name in frame.columns),
            "",
        )
        if not date_column:
            raise DataSourceError(f"vendor daily file missing date column for {symbol}")
        frame["date"] = pd.to_datetime(frame[date_column], errors="coerce")
        for column in ("open", "high", "low", "close", "volume"):
            if column not in frame.columns:
                raise DataSourceError(
                    f"vendor daily file missing required column for {symbol}: {column}"
                )
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame["volume"] = frame["volume"] * max(0.0, float(self.daily_volume_multiplier))
        if "amount" in frame.columns:
            frame["turnover"] = pd.to_numeric(frame["amount"], errors="coerce") * max(
                0.0, float(self.daily_turnover_multiplier)
            )
        elif "turnover" not in frame.columns:
            frame["turnover"] = frame["close"] * frame["volume"]
        if "circ_mv" in frame.columns:
            frame["float_market_cap"] = pd.to_numeric(frame["circ_mv"], errors="coerce") * 10000.0
        elif "float_market_cap" not in frame.columns:
            frame["float_market_cap"] = np.nan
        frame["suspended"] = False
        frame["name"] = ""
        frame["is_st"] = False
        frame["is_delisting_risk"] = False
        frame["financial_source"] = "local_vendor"
        frame["financial_data_complete"] = False
        frame["financial_missing_fields"] = "roe,debt_ratio"
        frame["financial_trust_level"] = "missing"
        frame["financial_completeness"] = 0.0
        frame["background_data_source"] = f"local_vendor_{self.price_series_mode}"
        frame["background_data_complete"] = False
        frame["background_missing_fields"] = (
            "holder_count,block_trade_net,financing_balance,"
            "margin_financing_balance,northbound_net,dragon_tiger_flag"
        )
        frame["price_series_mode"] = self.price_series_mode
        frame["adjustment_source"] = (
            "local_vendor_raw" if self.price_series_mode == "raw" else "local_vendor_declared"
        )
        frame["adjustment_anchor_date"] = ""
        frame["adjustment_anchor_factor"] = np.nan
        return cast(pd.DataFrame, _normalize_frame(frame=frame, symbol=symbol))

    def _load_vendor_intraday_summary(
        self,
        *,
        symbol: str,
        interval: str,
        lookback_days: int,
    ) -> pd.DataFrame:
        cache_key = f"{interval}:{symbol}:{lookback_days}"
        cached = self._intraday_cache.get(cache_key)
        if cached is not None:
            self._intraday_cache.move_to_end(cache_key)
            return cached.copy()
        cutoff = date.today() - timedelta(days=max(45, lookback_days * 2))
        minute_frames: list[pd.DataFrame] = []
        for archive_path in self._minute_archive_paths(interval=interval, cutoff=cutoff):
            entry_index = self._minute_entries_for_archive(archive_path)
            for entry_name in entry_index.get(symbol, []):
                with zipfile.ZipFile(archive_path) as archive:
                    with archive.open(entry_name) as stream:
                        raw = pd.read_csv(stream)
                normalized = self._normalize_vendor_minute(raw)
                if not normalized.empty:
                    normalized_index = pd.DatetimeIndex(normalized.index)
                    normalized = normalized.loc[normalized_index >= pd.Timestamp(cutoff)]
                    if not normalized.empty:
                        minute_frames.append(normalized)
        minute_bars = _merge_overlay_frames(*minute_frames)
        summary = (
            summarize_minute_bars(minute_bars, interval=interval)
            if not minute_bars.empty
            else pd.DataFrame()
        )
        self._remember(self._intraday_cache, cache_key, summary)
        return summary.copy()

    def _normalize_vendor_minute(self, raw: pd.DataFrame) -> pd.DataFrame:
        if raw.empty or "datetime" not in raw.columns:
            return pd.DataFrame()
        frame = raw.copy()
        frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce")
        for column in ("open", "high", "low", "close"):
            if column not in frame.columns:
                return pd.DataFrame()
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        volume_source = (
            frame["volume"]
            if "volume" in frame.columns
            else pd.Series(0.0, index=frame.index, dtype=float)
        )
        amount_source = (
            frame["amount"]
            if "amount" in frame.columns
            else pd.Series(0.0, index=frame.index, dtype=float)
        )
        frame["volume"] = pd.to_numeric(volume_source, errors="coerce").fillna(0.0) * max(
            0.0,
            float(self.minute_volume_multiplier),
        )
        frame["amount"] = pd.to_numeric(amount_source, errors="coerce").fillna(0.0) * max(
            0.0,
            float(self.minute_amount_multiplier),
        )
        frame = frame.dropna(subset=["datetime", "open", "high", "low", "close"])
        frame = frame.set_index("datetime").sort_index()
        frame.index.name = "datetime"
        return frame[["open", "high", "low", "close", "volume", "amount"]]

    def _minute_archive_paths(self, *, interval: str, cutoff: date) -> list[Path]:
        cached = self._minute_archives.get(interval)
        if cached is None:
            candidates: list[Path] = []
            interval_dir_token = {"1m": "1min", "5m": "5min"}[interval]
            for directory in self._root.rglob(f"Stock*_{interval_dir_token}_*-now"):
                if directory.is_dir():
                    candidates.extend(directory.glob("*.zip"))
            cached = sorted(set(candidates))
            self._minute_archives[interval] = cached
        selected: list[Path] = []
        for path in cached:
            coverage = _minute_archive_coverage(path)
            if coverage is None:
                continue
            _, period_end = coverage
            if period_end >= cutoff:
                selected.append(path)
        return selected

    def _minute_entries_for_archive(self, archive_path: Path) -> dict[str, list[str]]:
        cached = self._minute_entry_index.get(archive_path)
        if cached is not None:
            return cached
        entries: dict[str, list[str]] = {}
        with zipfile.ZipFile(archive_path) as archive:
            for entry in archive.infolist():
                if entry.is_dir() or _is_zip_noise(entry.filename):
                    continue
                name = Path(entry.filename).name
                match = re.fullmatch(r"(?:sh|sz|bj)?(?P<code>\d{6})\.csv", name, re.I)
                if match is None:
                    continue
                symbol = _normalize_symbol(match.group("code"))
                if symbol:
                    entries.setdefault(symbol, []).append(entry.filename)
        self._minute_entry_index[archive_path] = entries
        return entries

    def _remember(
        self,
        cache: OrderedDict[str, pd.DataFrame],
        key: str,
        frame: pd.DataFrame,
    ) -> None:
        cache[key] = frame.copy()
        cache.move_to_end(key)
        while len(cache) > self.memory_cache_symbols:
            cache.popitem(last=False)


def _select_canonical_daily_archives(
    daily_root: Path,
) -> tuple[list[tuple[int, Path]], list[Path]]:
    grouped: dict[int, list[Path]] = {}
    for path in daily_root.glob("*.zip"):
        match = _YEAR_ARCHIVE_RE.fullmatch(path.name)
        if match is None:
            continue
        grouped.setdefault(int(match.group("year")), []).append(path)
    selected: list[tuple[int, Path]] = []
    ignored: list[Path] = []
    for year, candidates in sorted(grouped.items()):
        preferred = next(
            (path for path in candidates if path.name.lower() == f"{year}.zip"),
            sorted(candidates, key=lambda item: item.name)[0],
        )
        selected.append((year, preferred))
        ignored.extend(path for path in candidates if path != preferred)
    return selected, ignored


def _read_last_trade_date(*, archive: zipfile.ZipFile, entry_name: str) -> str:
    try:
        with archive.open(entry_name) as stream:
            text = stream.read().decode("utf-8-sig", errors="replace")
    except KeyError:
        return ""
    rows = list(csv.reader(line for line in text.splitlines() if line.strip()))
    if len(rows) < 2:
        return ""
    header = [str(item).strip().lower() for item in rows[0]]
    date_index = next(
        (header.index(name) for name in ("datetime", "trade_date", "date") if name in header),
        -1,
    )
    if date_index < 0:
        return ""
    for row in reversed(rows[1:]):
        if date_index >= len(row):
            continue
        parsed = pd.to_datetime(row[date_index], errors="coerce")
        if not pd.isna(parsed):
            return parsed.date().isoformat()
    return ""


def _minute_archive_coverage(path: Path) -> tuple[date, date] | None:
    match = _MINUTE_ARCHIVE_RE.match(path.stem)
    if match is None:
        return None
    year = int(match.group("year"))
    month_text = match.group("month")
    if not month_text:
        return date(year, 1, 1), date(year, 12, 31)
    month = int(month_text)
    if month < 1 or month > 12:
        return None
    start = date(year, month, 1)
    next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, next_month - timedelta(days=1)


def _merge_overlay_frames(*frames: pd.DataFrame) -> pd.DataFrame:
    materialized = [
        frame for frame in frames if isinstance(frame, pd.DataFrame) and not frame.empty
    ]
    if not materialized:
        return pd.DataFrame()
    merged = pd.concat(materialized, axis=0, sort=False)
    if not isinstance(merged.index, pd.DatetimeIndex):
        merged.index = pd.to_datetime(merged.index, errors="coerce")
    merged = merged.loc[merged.index.notna()]
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    merged.index.name = materialized[-1].index.name or "date"
    return merged


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


def _coerce_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def vendor_zip_index_symbols(payload: dict[str, object]) -> Iterable[str]:
    raw = payload.get("symbols")
    if not isinstance(raw, dict):
        return []
    return sorted(str(symbol) for symbol in raw)
