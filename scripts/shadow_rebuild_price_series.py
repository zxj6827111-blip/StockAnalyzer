"""Shadow rebuild tool for price_series_mode consistency.

Reads daily_bars from a source DuckDB in read-only mode and writes a
clean, mode-uniform copy to a NEW target DuckDB path. Never modifies the
source. The source and target paths must differ.

When ``--target-mode qfq`` and the source contains raw rows, the tool
applies REAL price adjustment using the vendor qfq factor archive
(``复权因子/复权因子_前复权.zip``), not just a label change. Symbols
whose factor data is missing or unreadable are skipped and reported as
``symbols_missing_factors`` — they are NOT written with a fake qfq label.

When ``--target-mode raw`` and the source contains qfq rows, the tool
performs the inverse: divides OHLC by the qfq factor to recover raw
prices. Again, missing factors cause skip, not fake-label writes.

Usage:
    python scripts/shadow_rebuild_price_series.py \
        --source-db artifacts/vendor_delta/market_delta.duckdb \
        --target-db artifacts/shadow_rebuild/market_rebuilt.duckdb \
        --target-mode qfq \
        --vendor-root /data \
        [--symbols 600000,000001] [--symbols-file symbols.txt] \
        [--dry-run] [--limit 100]

Exit code: 0 on success, 1 on any failure, 2 on invalid arguments.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import cast

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_analyzer.data.market_warehouse import MarketWarehouse  # noqa: E402
from stock_analyzer.data.provider import DataSourceError  # noqa: E402
from stock_analyzer.data.tdx_offline_provider import _normalize_symbol  # noqa: E402
from stock_analyzer.data.tushare_provider import _to_ts_code  # noqa: E402
from stock_analyzer.data.vendor_zip_overlay import (  # noqa: E402
    _is_zip_noise,
    _parse_vendor_factor_frame,
)

_QFQ_FACTORS_DIR_NAME = "复权因子"
_QFQ_FACTORS_ARCHIVE_NAME = "复权因子_前复权.zip"


def _resolve_symbols(
    args: argparse.Namespace,
    source_warehouse: MarketWarehouse,
) -> list[str]:
    raw: list[str] = []
    if args.symbols.strip():
        raw = [item.strip() for item in args.symbols.split(",") if item.strip()]
    elif args.symbols_file.strip():
        file_path = Path(args.symbols_file.strip()).expanduser()
        raw = [
            line.strip()
            for line in file_path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        ]
    else:
        raw = source_warehouse.list_symbols()
    return sorted(
        {item for item in (_normalize_symbol(value) for value in raw) if item}
    )


def _load_qfq_factors(
    vendor_root: Path,
    symbol: str,
) -> pd.Series | None:
    """Load the qfq factor series for ``symbol`` from the vendor archive.

    Returns None when the archive or factor entry is missing — callers
    must treat this as fail-closed (skip the symbol, do NOT fake-label).
    """
    factors_dir = vendor_root / _QFQ_FACTORS_DIR_NAME
    archive_path = factors_dir / _QFQ_FACTORS_ARCHIVE_NAME
    if not archive_path.exists():
        return None
    ts_code = _to_ts_code(symbol)
    expected_name = f"{ts_code}.csv"
    with zipfile.ZipFile(archive_path) as archive:
        entries: list[str] = []
        for name in archive.namelist():
            if _is_zip_noise(name):
                continue
            if Path(name.replace("\\", "/")).name.lower() == expected_name.lower():
                entries.append(name)
        if not entries:
            return None
        frames: list[pd.Series] = []
        for entry_name in entries:
            try:
                with archive.open(entry_name) as stream:
                    raw = pd.read_csv(stream)
            except (KeyError, ValueError, OSError):
                continue
            try:
                parsed = _parse_vendor_factor_frame(raw, symbol=symbol)
            except DataSourceError:
                continue
            if not parsed.empty:
                frames.append(parsed)
    if not frames:
        return None
    merged = cast(pd.Series, pd.concat(frames, axis=0))
    merged = merged[merged.index.notna()]
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    if merged.empty:
        return None
    return merged


def _apply_qfq_adjustment(
    frame: pd.DataFrame,
    factors: pd.Series,
) -> pd.DataFrame:
    """Multiply OHLC by qfq factors — ONLY for rows not already qfq.

    Already-qfq rows are left untouched to avoid double adjustment.
    ``pre_close`` is also adjusted when present. Volume and turnover
    are never adjusted (they reflect actual traded units).
    """
    adjusted = frame.copy()
    mode_col = adjusted.get("price_series_mode")
    if mode_col is not None:
        needs_adj = mode_col.astype(str).str.strip().str.lower() != "qfq"
    else:
        needs_adj = pd.Series([True] * len(adjusted))
    date_index = pd.to_datetime(adjusted["date"], errors="coerce")
    if date_index.isna().any():
        raise DataSourceError("daily bars contain invalid dates for qfq adjustment")
    aligned = factors.reindex(date_index, method="ffill")
    if aligned.isna().any():
        na_mask = aligned.isna().to_numpy()
        na_dates = date_index[na_mask]
        if not na_dates.empty:
            factor_start = factors.index.min()
            if bool((na_dates >= factor_start).any()):
                raise DataSourceError(
                    "qfq factors could not be aligned to daily bars "
                    "(mid-series NaN)"
                )
            aligned = aligned.fillna(1.0)
    factor_values = aligned.to_numpy(dtype=float)
    adj_mask = needs_adj.to_numpy()
    for column in ("open", "high", "low", "close", "pre_close"):
        if column in adjusted.columns:
            adjusted[column] = pd.to_numeric(adjusted[column], errors="coerce")
            adjusted.loc[adj_mask, column] = (
                adjusted.loc[adj_mask, column] * factor_values[adj_mask]
            )
    return adjusted


def _invert_qfq_adjustment(
    frame: pd.DataFrame,
    factors: pd.Series,
) -> pd.DataFrame:
    """Recover raw OHLC by dividing qfq prices by the factor.

    Only rows NOT already raw are adjusted — already-raw rows are
    left untouched.
    """
    adjusted = frame.copy()
    mode_col = adjusted.get("price_series_mode")
    if mode_col is not None:
        needs_adj = mode_col.astype(str).str.strip().str.lower() != "raw"
    else:
        needs_adj = pd.Series([True] * len(adjusted))
    date_index = pd.to_datetime(adjusted["date"], errors="coerce")
    if date_index.isna().any():
        raise DataSourceError("daily bars contain invalid dates for inverse adjustment")
    aligned = factors.reindex(date_index, method="ffill")
    if aligned.isna().any():
        na_mask = aligned.isna().to_numpy()
        na_dates = date_index[na_mask]
        if not na_dates.empty:
            factor_start = factors.index.min()
            if bool((na_dates >= factor_start).any()):
                raise DataSourceError(
                    "qfq factors could not be aligned for inverse adjustment "
                    "(mid-series NaN)"
                )
            aligned = aligned.fillna(1.0)
    factor_values = aligned.to_numpy(dtype=float)
    adj_mask = needs_adj.to_numpy()
    for column in ("open", "high", "low", "close", "pre_close"):
        if column in adjusted.columns:
            adjusted[column] = pd.to_numeric(adjusted[column], errors="coerce")
            adjusted.loc[adj_mask, column] = (
                adjusted.loc[adj_mask, column] / factor_values[adj_mask]
            )
    return adjusted


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", required=True, help="Source DuckDB (read-only)")
    parser.add_argument("--target-db", required=True, help="Target DuckDB (new path)")
    parser.add_argument(
        "--target-mode",
        default="qfq",
        choices=["qfq", "raw"],
        help="Uniform price_series_mode to enforce in the target (default: qfq)",
    )
    parser.add_argument(
        "--vendor-root",
        default="",
        help="Vendor data root (containing 复权因子/ and 全A日K/); "
        "required for raw→qfq or qfq→raw price adjustment",
    )
    parser.add_argument("--symbols", default="", help="Comma-separated symbol subset")
    parser.add_argument("--symbols-file", default="", help="One symbol per line")
    parser.add_argument("--dry-run", action="store_true", help="Report only; never write")
    parser.add_argument(
        "--limit", type=int, default=0, help="Cap symbols processed (0 = unlimited)"
    )
    args = parser.parse_args(argv)

    source_path = Path(args.source_db).expanduser().resolve()
    target_path = Path(args.target_db).expanduser().resolve()

    # 安全检查：source 和 target 必须不同
    if source_path == target_path:
        print(
            json.dumps(
                {"error": "source_db and target_db must differ", "source": str(source_path)},
                ensure_ascii=False,
            )
        )
        return 2
    if not source_path.exists():
        print(
            json.dumps(
                {"error": "source_db does not exist", "source": str(source_path)},
                ensure_ascii=False,
            )
        )
        return 2

    vendor_root = Path(args.vendor_root).expanduser() if args.vendor_root.strip() else None

    # source 用只读模式打开
    source_package = source_path.parent / "package_source"
    source_warehouse = MarketWarehouse(
        db_path=source_path,
        package_root=str(source_package),
        package_writes_enabled=False,
        read_only=True,
    )

    # 检测 source 中的 mixed-mode symbols
    mixed_report = source_warehouse.detect_price_series_mixed()

    symbols = _resolve_symbols(args, source_warehouse)
    limit = max(0, int(args.limit))
    if limit:
        symbols = symbols[:limit]

    target_package = target_path.parent / "package"
    if not args.dry_run:
        target_path.parent.mkdir(parents=True, exist_ok=True)
    target_warehouse = MarketWarehouse(
        db_path=target_path,
        package_root=str(target_package),
        package_writes_enabled=False,
    )

    symbols_rebuilt: list[str] = []
    symbols_already_consistent: list[str] = []
    symbols_missing_factors: list[str] = []
    symbols_unknown_mode: list[str] = []
    symbols_adjusted: list[str] = []
    rows_read = 0
    rows_written = 0

    for symbol in symbols:
        frame = source_warehouse.fetch_all_daily_bars(symbol=symbol)
        if frame is None or frame.empty:
            continue
        rows_read += len(frame)

        # fetch_all_daily_bars 返回的 frame 以 date 为索引，先 reset_index
        frame = frame.reset_index()
        if "date" in frame.columns:
            frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
        # 补上 symbol 列（fetch_all_daily_bars 不返回 symbol 列）
        if "symbol" not in frame.columns:
            frame.insert(0, "symbol", symbol)

        if "price_series_mode" not in frame.columns:
            symbols_unknown_mode.append(symbol)
            continue
        normalized_modes = (
            frame["price_series_mode"].fillna("").astype(str).str.strip().str.lower()
        )
        if bool((~normalized_modes.isin({"qfq", "raw"})).any()):
            symbols_unknown_mode.append(symbol)
            continue
        existing_modes = set(normalized_modes.unique())
        needs_rebuild = existing_modes != {args.target_mode}

        if needs_rebuild:
            symbols_rebuilt.append(symbol)
            # 跨 mode 重建需要真正的复权因子
            if vendor_root is None:
                symbols_missing_factors.append(symbol)
                continue
            factors = _load_qfq_factors(vendor_root, symbol)
            if factors is None or factors.empty:
                symbols_missing_factors.append(symbol)
                continue
            try:
                if args.target_mode == "qfq":
                    # raw → qfq：用因子乘 OHLC
                    frame = _apply_qfq_adjustment(frame, factors)
                else:
                    # qfq → raw：用因子除 OHLC
                    frame = _invert_qfq_adjustment(frame, factors)
            except DataSourceError:
                symbols_missing_factors.append(symbol)
                continue
            symbols_adjusted.append(symbol)
            # 更新复权契约元数据
            anchor_date = factors.index.max()
            anchor_factor = float(factors.iloc[-1])
            frame["adjustment_anchor_date"] = (
                anchor_date.date().isoformat()
                if hasattr(anchor_date, "date")
                else str(anchor_date)[:10]
            )
            frame["adjustment_anchor_factor"] = anchor_factor
        else:
            symbols_already_consistent.append(symbol)

        # 统一设置 price_series_mode 标签和复权来源
        frame["price_series_mode"] = args.target_mode
        if "adjustment_source" in frame.columns:
            frame["adjustment_source"] = f"shadow_rebuild_{args.target_mode}"

        if not args.dry_run:
            stored = target_warehouse.upsert_daily_bars(
                frame=frame,
                overwrite_existing=True,
                # shadow rebuild 有意统一 mode，跳过一致性校验
                enforce_price_series_mode=False,
            )
            rows_written += stored

    report = {
        "tool": "shadow_rebuild_price_series",
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "source_db": str(source_path),
        "target_db": str(target_path),
        "target_mode": args.target_mode,
        "vendor_root": str(vendor_root) if vendor_root else "",
        "dry_run": bool(args.dry_run),
        "symbols_requested": len(symbols),
        "symbols_processed": len(symbols),
        "symbols_rebuilt": symbols_rebuilt,
        "symbols_rebuilt_count": len(symbols_rebuilt),
        "symbols_already_consistent": symbols_already_consistent,
        "symbols_already_consistent_count": len(symbols_already_consistent),
        "symbols_adjusted": symbols_adjusted,
        "symbols_adjusted_count": len(symbols_adjusted),
        "symbols_missing_factors": symbols_missing_factors,
        "symbols_missing_factors_count": len(symbols_missing_factors),
        "symbols_unknown_mode": symbols_unknown_mode,
        "symbols_unknown_mode_count": len(symbols_unknown_mode),
        "rows_read": rows_read,
        "rows_written": rows_written,
        "mixed_symbols_detected": {
            "mixed_symbol_count": mixed_report.get("mixed_symbol_count", 0),
            "mixed_symbols": [
                {"symbol": s.get("symbol"), "modes": s.get("modes")}
                for s in mixed_report.get("mixed_symbols", [])
            ],
            "mode_distribution": mixed_report.get("mode_distribution", {}),
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # 缺少复权因子的 symbol 视为失败：不能冒充成功
    if symbols_missing_factors or symbols_unknown_mode:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
