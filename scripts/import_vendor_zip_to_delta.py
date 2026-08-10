"""Import the annual vendor ZIP history into the delta DuckDB baseline.

The Week5 universe quality batch used to decompress the full-market annual
ZIPs on every run (~60 min for 5472 symbols x 240 days). This script moves
that cost into a one-shot baseline import plus a nightly incremental sync,
so the batch path reads the delta DuckDB instead.

Modes:
- full (default): read every requested symbol from the ZIPs (newest annual
  archives first, ``--limit-days`` rows per symbol) and upsert them into the
  delta ``daily_bars`` table. Idempotent: dates already present in the delta
  are left untouched (the delta row wins, same rule as the overlay merge),
  so re-running after an interruption simply fills the gap; pass
  ``--overwrite-existing`` to replace already-present dates instead.
- incremental: for every symbol compare the delta's latest date with the ZIP
  index ``latest_date`` and upsert only the newer rows; symbols with no delta
  baseline yet are imported in full. qfq symbols whose factor file drifted
  since the delta anchor (a corporate action re-anchors ALL history) are
  detected via the anchor-day factor value and refreshed from the ZIPs with
  ``overwrite_existing=True``.

The read path reuses the production normalization pipeline
(``VendorZipOverlayProvider._load_vendor_daily_batch`` +
``_normalize_vendor_daily``), never copying it: qfq-missing symbols are
skipped and reported exactly like the batch path (WARNING + skip, no
failure). Financial/background columns the ZIPs cannot provide stay NULL /
honestly missing, matching what the overlay returns today.

Usage (container, NAS: the vendor history root is mounted at /data and the
delta DuckDB lives under /app/artifacts/vendor_delta/):

    python scripts/import_vendor_zip_to_delta.py \
        --data-root /data --index-path /app/artifacts/vendor_overlay/daily_index.json \
        --delta-db-path /app/artifacts/vendor_delta/market_delta.duckdb
    python scripts/import_vendor_zip_to_delta.py --dry-run ...
    python scripts/import_vendor_zip_to_delta.py --incremental ...
    python scripts/import_vendor_zip_to_delta.py --config config/default.yaml \
        --symbols 600000,000001 --limit-days 400 --dry-run

Exit code: 0 on success, 1 on any failure, 2 on invalid arguments/data.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_analyzer.data.market_warehouse import MarketWarehouse  # noqa: E402
from stock_analyzer.data.provider import DataSourceError  # noqa: E402
from stock_analyzer.data.tushare_provider import _to_ts_code  # noqa: E402
from stock_analyzer.data.vendor_zip_overlay import (  # noqa: E402
    VendorZipOverlayProvider,
    _is_zip_noise,
    _parse_vendor_factor_frame,
)

_QFQ_FACTORS_DIR_NAME = "复权因子"
_QFQ_FACTORS_ARCHIVE_NAME = "复权因子_前复权.zip"
# qfq factors are anchored at the latest date (latest factor == 1.0); a value
# on the delta anchor date that differs from 1.0 means the series was
# re-anchored by a corporate action since the baseline import.
_QFQ_ANCHOR_TOLERANCE = 1e-6


def _resolve_provider(
    args: argparse.Namespace,
) -> VendorZipOverlayProvider:
    """Build a read-write overlay provider from CLI overrides or the config."""
    data_root = ""
    index_path = ""
    delta_db_path = ""
    price_series_mode = "qfq"
    daily_dir = "全A日K"
    if args.config.strip():
        from stock_analyzer.config import load_config

        config = load_config(args.config)
        source = config.data_source
        data_root = source.local_data_root
        index_path = source.vendor_zip_index_path
        delta_db_path = source.warehouse_db_path
        price_series_mode = source.vendor_zip_price_series_mode
        daily_dir = source.vendor_zip_daily_dir
    if args.data_root:
        data_root = args.data_root
    if args.index_path:
        index_path = args.index_path
    if args.delta_db_path:
        delta_db_path = args.delta_db_path
    if args.price_series_mode:
        price_series_mode = args.price_series_mode
    if not data_root or not index_path or not delta_db_path:
        raise DataSourceError(
            "data_root, index_path and delta_db_path are required "
            "(either via --config or the dedicated CLI flags)"
        )
    return VendorZipOverlayProvider(
        data_root=data_root,
        index_path=index_path,
        delta_db_path=delta_db_path,
        daily_dir_name=daily_dir,
        price_series_mode=price_series_mode,
        delta_access_mode="read_write",
    )


def _resolve_symbols(
    provider: VendorZipOverlayProvider,
    symbols_text: str,
) -> list[str]:
    if symbols_text.strip():
        raw = [item.strip() for item in symbols_text.split(",") if item.strip()]
        from stock_analyzer.data.tdx_offline_provider import _normalize_symbol

        return sorted(
            {item for item in (_normalize_symbol(value) for value in raw) if item}
        )
    return provider.list_symbols()


def _index_latest_dates(index_path: str) -> dict[str, date]:
    """ZIP ``latest_date`` per symbol (the source of truth for freshness).

    Parsed leniently: both the full ``build_vendor_zip_daily_index`` payload
    and the last-date index maintained by ``update_vendor_daily_from_tushare``
    carry ``symbols[<code>].latest_date`` and may lack the strict version
    header.
    """
    payload = json.loads(Path(index_path).read_text(encoding="utf-8"))
    raw_symbols = payload.get("symbols") if isinstance(payload, dict) else None
    if not isinstance(raw_symbols, dict):
        return {}
    latest: dict[str, date] = {}
    for symbol, raw in raw_symbols.items():
        if not isinstance(raw, dict):
            continue
        parsed = _coerce_date(raw.get("latest_date"))
        if parsed is not None:
            latest[str(symbol).strip()] = parsed
    return latest


def _delta_latest_dates(
    warehouse: MarketWarehouse,
    symbols: list[str],
) -> dict[str, date]:
    if warehouse is None:
        return {}
    return warehouse.latest_daily_dates(symbols=symbols)


def _factor_entry_index(factor_archive: Path) -> dict[str, str]:
    """Map ``<ts_code>.csv`` -> entry name (one namelist pass, then direct lookups)."""
    mapping: dict[str, str] = {}
    with zipfile.ZipFile(factor_archive) as archive:
        for name in archive.namelist():
            if _is_zip_noise(name):
                continue
            file_name = Path(name.replace("\\", "/")).name
            if file_name.lower().endswith(".csv"):
                mapping[file_name[:-4].upper()] = name
    return mapping


def _factor_value_on_anchor(
    factor_archive: Path,
    entry_index: dict[str, str],
    symbol: str,
    anchor_date: date,
) -> float | None:
    """Factor value on the delta anchor date, or None when undecidable.

    None means "no drift signal": the symbol has no factor entry at all or
    the anchor date is not a factor trading day. A value != 1.0 (beyond
    tolerance) means the qfq series was re-anchored since the baseline.
    """
    ts_code = _to_ts_code(symbol)
    entry_name = entry_index.get(ts_code.upper())
    if entry_name is None:
        return None
    with zipfile.ZipFile(factor_archive) as archive:
        try:
            with archive.open(entry_name) as stream:
                raw = pd.read_csv(stream)
        except KeyError:
            return None
    try:
        series = _parse_vendor_factor_frame(raw, symbol=symbol)
    except DataSourceError:
        return None
    anchor_ts = pd.Timestamp(anchor_date)
    if anchor_ts not in series.index:
        return None
    return float(series.loc[anchor_ts])


def _delta_anchor_dates(
    warehouse: MarketWarehouse,
    symbols: list[str],
) -> dict[str, date]:
    """Latest ``adjustment_anchor_date`` per symbol stored in the delta."""
    if warehouse is None or not warehouse.db_path.exists():
        return {}
    from stock_analyzer.data.market_warehouse import _DAILY_TABLE

    if not warehouse._table_exists(_DAILY_TABLE):  # noqa: SLF001
        return {}
    placeholders = ", ".join("?" for _ in symbols)
    query = f"""
        WITH latest AS (
            SELECT symbol, MAX(date) AS max_date
            FROM {_DAILY_TABLE}
            WHERE symbol IN ({placeholders})
            GROUP BY symbol
        )
        SELECT t.symbol, t.adjustment_anchor_date
        FROM {_DAILY_TABLE} AS t
        JOIN latest AS m
          ON t.symbol = m.symbol AND t.date = m.max_date
    """
    try:
        with warehouse._connect_readonly() as connection:  # noqa: SLF001
            rows = connection.execute(query, symbols).fetchall()
    except Exception:
        # 旧库没有 adjustment_anchor_date 列：无法检测漂移，保守跳过。
        return {}
    anchors: dict[str, date] = {}
    for raw_symbol, raw_date in rows:
        parsed = _coerce_date(raw_date)
        if parsed is not None:
            anchors[str(raw_symbol).strip()] = parsed
    return anchors


def _filter_fresh_rows(
    frames: list[pd.DataFrame],
    *,
    delta_latest: dict[str, date],
) -> tuple[list[pd.DataFrame], int]:
    """Keep only rows newer than each symbol's delta latest date."""
    filtered: list[pd.DataFrame] = []
    fresh_rows = 0
    for frame in frames:
        symbol = str(frame["symbol"].iloc[0])
        cutoff = pd.Timestamp(delta_latest.get(symbol, date(1970, 1, 1)))
        fresh = frame[frame["date"] > cutoff]
        if not fresh.empty:
            fresh_rows += len(fresh)
            filtered.append(fresh)
    return filtered, fresh_rows


def _build_report(
    *,
    mode: str,
    requested_symbols: int,
    loaded_symbols: list[str],
    skipped_symbols: list[str],
    fresh_rows: int,
    drift_symbols: list[str],
    full_import_symbols: list[str],
    dry_run: bool,
    elapsed_sec: float,
) -> dict[str, object]:
    return {
        "script": "import_vendor_zip_to_delta",
        "mode": mode,
        "dry_run": dry_run,
        "requested_symbols": requested_symbols,
        "loaded_symbols": len(loaded_symbols),
        "skipped_symbols": skipped_symbols,
        "skipped_symbol_count": len(skipped_symbols),
        "incremental_new_rows": fresh_rows,
        "drift_refreshed_symbols": drift_symbols,
        "drift_refreshed_symbol_count": len(drift_symbols),
        "full_import_symbol_count": len(full_import_symbols),
        "elapsed_sec": round(elapsed_sec, 3),
    }


def _coerce_date(value: object) -> date | None:
    if isinstance(value, pd.Timestamp):
        return value.date()
    from datetime import datetime

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        parsed = pd.to_datetime(value, errors="coerce")
        if not pd.isna(parsed):
            return parsed.date()
    return None


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="", help="Path to the YAML config")
    parser.add_argument("--data-root", default="", help="Vendor history root (overrides config)")
    parser.add_argument("--index-path", default="", help="daily_index.json path (overrides config)")
    parser.add_argument(
        "--delta-db-path", default="", help="Delta DuckDB path (overrides config)"
    )
    parser.add_argument(
        "--price-series-mode", default="", help="raw or qfq (default: config or qfq)"
    )
    parser.add_argument(
        "--symbols", default="", help="Comma-separated symbol subset (default: full market)"
    )
    parser.add_argument(
        "--limit-days",
        type=int,
        default=400,
        help="Rows per symbol read from the ZIPs for full imports (default 400; "
        "the Week5 batch lookback is 240)",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="Incremental sync: only dates newer than the delta baseline, plus "
        "qfq factor-drift refresh for re-anchored symbols",
    )
    parser.add_argument(
        "--incremental-lookback",
        type=int,
        default=30,
        help="Rows read from the ZIPs per symbol to cover the incremental window "
        "(default 30; must exceed the max gap between ZIP updates)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and report only; never create or write the delta DuckDB",
    )
    parser.add_argument(
        "--overwrite-existing",
        action="store_true",
        help="Full mode: replace already-present (symbol, date) rows instead of "
        "keeping the delta row (default keeps delta wins)",
    )
    args = parser.parse_args(argv)

    started_at = time.perf_counter()
    try:
        provider = _resolve_provider(args)
        symbols = _resolve_symbols(provider, args.symbols)
        if not symbols:
            print(json.dumps({"script": "import_vendor_zip_to_delta", "ok": False,
                              "error": "no_symbols"}, ensure_ascii=False, indent=2))
            return 2
        limit = max(60, int(args.limit_days))
        incremental_lookback = max(5, int(args.incremental_lookback))
        index_latest = _index_latest_dates(provider.index_path)
        warehouse = provider._warehouse  # noqa: SLF001

        if args.incremental:
            delta_latest = _delta_latest_dates(warehouse, symbols)
            anchors = (
                _delta_anchor_dates(warehouse, symbols)
                if warehouse is not None
                else {}
            )
            factor_archive = (
                provider._root / _QFQ_FACTORS_DIR_NAME / _QFQ_FACTORS_ARCHIVE_NAME
            )
            entry_index = (
                _factor_entry_index(factor_archive)
                if provider.price_series_mode == "qfq" and factor_archive.exists()
                else {}
            )
            fresh_symbols: list[str] = []
            full_import_symbols: list[str] = []
            drift_symbols: list[str] = []
            for symbol in symbols:
                delta_date = delta_latest.get(symbol)
                if delta_date is None:
                    full_import_symbols.append(symbol)
                    continue
                # 因子漂移检测覆盖所有已有 delta 基线的符号：即使当日无新
                # 数据（停牌），除权也会重标定全部历史，需要整段重算。
                anchor_date = anchors.get(symbol)
                if anchor_date is not None and entry_index:
                    factor_value = _factor_value_on_anchor(
                        factor_archive,
                        entry_index,
                        symbol,
                        anchor_date,
                    )
                    if (
                        factor_value is not None
                        and abs(factor_value - 1.0) > _QFQ_ANCHOR_TOLERANCE
                    ):
                        drift_symbols.append(symbol)
                        continue
                zip_date = index_latest.get(symbol)
                if zip_date is None or zip_date <= delta_date:
                    continue
                fresh_symbols.append(symbol)

            fresh_frames: list[pd.DataFrame] = []
            fresh_rows = 0
            if fresh_symbols:
                frames = provider._load_vendor_daily_batch(  # noqa: SLF001
                    symbols=fresh_symbols, limit=incremental_lookback
                )
                fresh_frames, fresh_rows = _filter_fresh_rows(
                    frames, delta_latest=delta_latest
                )
            full_frames: list[pd.DataFrame] = []
            if full_import_symbols:
                full_frames = provider._load_vendor_daily_batch(  # noqa: SLF001
                    symbols=full_import_symbols, limit=limit
                )
            drift_frames: list[pd.DataFrame] = []
            if drift_symbols:
                drift_frames = provider._load_vendor_daily_batch(  # noqa: SLF001
                    symbols=drift_symbols, limit=limit
                )

            all_frames = fresh_frames + full_frames + drift_frames
            loaded_symbols = sorted({str(frame["symbol"].iloc[0]) for frame in all_frames})
            skipped_symbols = sorted(set(symbols) - set(loaded_symbols))

            report = _build_report(
                mode="incremental",
                requested_symbols=len(symbols),
                loaded_symbols=loaded_symbols,
                skipped_symbols=skipped_symbols,
                fresh_rows=fresh_rows,
                drift_symbols=drift_symbols,
                full_import_symbols=full_import_symbols,
                dry_run=bool(args.dry_run),
                elapsed_sec=time.perf_counter() - started_at,
            )

            if not args.dry_run and warehouse is not None and all_frames:
                warehouse.ensure_schema()
                combined = pd.concat(all_frames, axis=0, sort=False, ignore_index=True)
                stored_fresh = warehouse.upsert_daily_bars(frame=combined)
                report["rows_stored"] = stored_fresh
                if drift_frames:
                    drift_combined = pd.concat(
                        drift_frames, axis=0, sort=False, ignore_index=True
                    )
                    stored_drift = warehouse.upsert_daily_bars(
                        frame=drift_combined, overwrite_existing=True
                    )
                    report["drift_rows_replaced"] = stored_drift
            report["ok"] = True
        else:
            frames = provider._load_vendor_daily_batch(symbols=symbols, limit=limit)
            loaded_symbols = sorted({str(frame["symbol"].iloc[0]) for frame in frames})
            skipped_symbols = sorted(set(symbols) - set(loaded_symbols))
            rows = sum(len(frame) for frame in frames)
            report = _build_report(
                mode="full",
                requested_symbols=len(symbols),
                loaded_symbols=loaded_symbols,
                skipped_symbols=skipped_symbols,
                fresh_rows=0,
                drift_symbols=[],
                full_import_symbols=loaded_symbols,
                dry_run=bool(args.dry_run),
                elapsed_sec=time.perf_counter() - started_at,
            )
            report["rows_read"] = rows
            if not args.dry_run and warehouse is not None and frames:
                warehouse.ensure_schema()
                combined = pd.concat(frames, axis=0, sort=False, ignore_index=True)
                report["rows_stored"] = warehouse.upsert_daily_bars(
                    frame=combined,
                    overwrite_existing=bool(args.overwrite_existing),
                )
            report["ok"] = True
    except Exception as exc:
        print(
            json.dumps(
                {
                    "script": "import_vendor_zip_to_delta",
                    "ok": False,
                    "error": f"{type(exc).__name__}:{exc}",
                    "elapsed_sec": round(time.perf_counter() - started_at, 3),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(_main())
