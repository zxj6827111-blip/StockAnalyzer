"""Build StockAnalyzer offline bars package from TongDaXin local files.

This script produces `bars/{symbol}.csv` with StockAnalyzer-required columns.
It uses:
1) `vipdoc/*/lday/*.day` for OHLCV
2) `tdxgp.zip` for heuristic financial/background factors
3) `infoharbor_ex.code` for stock names

Notes:
- `tdxfin.zip` is summarized in manifest metadata but not used for field mapping,
  because public field dictionaries are not stable across client versions.
- The generated package is intended for offline runtime compatibility.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from struct import calcsize, unpack

import numpy as np
import pandas as pd

DAY_DTYPE = np.dtype(
    [
        ("date", "<u4"),
        ("open", "<u4"),
        ("high", "<u4"),
        ("low", "<u4"),
        ("close", "<u4"),
        ("amount", "<f4"),
        ("volume", "<u4"),
        ("reserved", "<u4"),
    ]
)

GP_DTYPE = np.dtype(
    [
        ("flag", "u1"),
        ("date", "<u4"),
        ("value", "<f4"),
        ("aux", "<u4"),
    ]
)

OUTPUT_COLUMNS = [
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "float_market_cap",
    "suspended",
    "name",
    "is_st",
    "is_delisting_risk",
    "roe",
    "debt_ratio",
    "financial_data_complete",
    "financial_missing_fields",
    "financial_source",
    "financial_report_date",
    "holder_count",
    "block_trade_net",
    "financing_balance",
    "margin_financing_balance",
    "northbound_net",
    "dragon_tiger_flag",
    "background_data_source",
    "background_data_complete",
    "board",
]

DEFAULT_FLOAT_MARKET_CAP = 12_000_000_000.0
DEFAULT_ROE = 0.08
DEFAULT_DEBT_RATIO = 0.55
DEFAULT_HOLDER_COUNT = 60_000.0
DEFAULT_FINANCING_BALANCE = 2_500_000_000.0
_TDXFIN_TOTAL_SHARES_COLUMN = "col238"
_TDXFIN_FLOAT_SHARES_COLUMN = "col239"

_GP_FLAG_HOLDER_A = 1
_GP_FLAG_HOLDER_B = 3
_GP_FLAG_BLOCK_NET = 13
_GP_FLAG_FINANCING = 16
_GP_FLAG_DEBT_RATIO = 20
_GP_FLAG_ROE = 44
_GP_FLAG_DRAGON_TIGER = 36
_GP_FLAG_NORTHBOUND = 47

_NEEDED_GP_FLAGS = {
    _GP_FLAG_HOLDER_A,
    _GP_FLAG_HOLDER_B,
    _GP_FLAG_BLOCK_NET,
    _GP_FLAG_FINANCING,
    _GP_FLAG_DEBT_RATIO,
    _GP_FLAG_ROE,
    _GP_FLAG_DRAGON_TIGER,
    _GP_FLAG_NORTHBOUND,
}


@dataclass(frozen=True, slots=True)
class DayFileItem:
    market: str
    symbol: str
    path: Path


def main() -> None:
    args = _parse_args()
    vipdoc_root = Path(args.vipdoc_root).expanduser().resolve()
    output_root = Path(args.output_root).expanduser().resolve()
    bars_root = output_root / "bars"
    bars_root.mkdir(parents=True, exist_ok=True)

    symbols_filter = {
        item.strip()
        for item in args.symbols.split(",")
        if item.strip()
    } if args.symbols else set()

    day_files = _collect_day_files(
        vipdoc_root=vipdoc_root,
        include_bj=bool(args.include_bj),
        symbols_filter=symbols_filter,
        max_symbols=args.max_symbols,
    )
    if not day_files:
        raise RuntimeError("no eligible .day files found")

    name_path = _resolve_name_file(vipdoc_root=vipdoc_root, explicit=args.name_file)
    name_map = _load_name_map(name_path) if name_path is not None else {}

    gp_zip_path = vipdoc_root / "tdxgp.zip"
    gp_zip = zipfile.ZipFile(gp_zip_path) if gp_zip_path.exists() and not args.skip_gp else None
    gp_name_set = set(gp_zip.namelist()) if gp_zip is not None else set()

    fin_zip_path = vipdoc_root / "tdxfin.zip"
    fin_summary = _read_tdxfin_summary(fin_zip_path)
    fin_export = _export_latest_tdxfin_raw(
        fin_zip_path=fin_zip_path,
        output_root=output_root,
    )
    share_history_by_symbol, share_history_summary = _load_share_history_from_tdxfin(
        fin_zip_path=fin_zip_path,
        symbols={item.symbol for item in day_files},
    )
    spot_snapshot_by_symbol, spot_snapshot_summary = (
        ({}, {"requested": 0, "received": 0, "failed_batches": 0})
        if args.skip_spot
        else _fetch_tencent_spot_snapshots([item.symbol for item in day_files])
    )

    failures: list[dict[str, str]] = []
    date_min: pd.Timestamp | None = None
    date_max: pd.Timestamp | None = None
    with_gp_count = 0

    for idx, item in enumerate(day_files, start=1):
        if idx % 300 == 0 or idx == 1:
            print(f"[{idx}/{len(day_files)}] processing {item.symbol}")
        try:
            bars = _read_day_bars(item.path)
            if bars.empty:
                raise ValueError("empty day bars")

            gp_features = _read_gp_features(
                gp_zip=gp_zip,
                gp_name_set=gp_name_set,
                market=item.market,
                symbol=item.symbol,
            )
            if not gp_features.empty:
                with_gp_count += 1

            stock_name = name_map.get(item.symbol, "")
            final = _build_output_frame(
                symbol=item.symbol,
                bars=bars,
                gp_features=gp_features,
                stock_name=stock_name,
                share_history=share_history_by_symbol.get(item.symbol),
                spot_snapshot=spot_snapshot_by_symbol.get(item.symbol),
            )
            final.to_csv(bars_root / f"{item.symbol}.csv", index_label="date")

            current_min = final.index.min()
            current_max = final.index.max()
            date_min = current_min if date_min is None else min(date_min, current_min)
            date_max = current_max if date_max is None else max(date_max, current_max)
        except Exception as exc:  # noqa: BLE001
            failures.append({"symbol": item.symbol, "error": str(exc)})

    if gp_zip is not None:
        gp_zip.close()

    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "vipdoc_root": str(vipdoc_root),
        "output_root": str(output_root),
        "package_version": "tdx-offline-v1",
        "symbol_files_total": len(day_files),
        "symbol_files_written": len(day_files) - len(failures),
        "symbol_files_failed": len(failures),
        "symbol_files_with_gp_factors": with_gp_count,
        "date_min": date_min.strftime("%Y-%m-%d") if date_min is not None else "",
        "date_max": date_max.strftime("%Y-%m-%d") if date_max is not None else "",
        "tdxfin_summary": fin_summary,
        "tdxfin_export": fin_export,
        "share_capital_summary": share_history_summary,
        "spot_snapshot_summary": spot_snapshot_summary,
        "heuristic_mapping": {
            "holder_count": f"flag {_GP_FLAG_HOLDER_A} fallback {_GP_FLAG_HOLDER_B}",
            "block_trade_net": f"flag {_GP_FLAG_BLOCK_NET}",
            "financing_balance": f"flag {_GP_FLAG_FINANCING}",
            "debt_ratio": f"flag {_GP_FLAG_DEBT_RATIO}",
            "roe": f"flag {_GP_FLAG_ROE}",
            "northbound_net": f"flag {_GP_FLAG_NORTHBOUND}",
            "dragon_tiger_flag": f"flag {_GP_FLAG_DRAGON_TIGER}",
        },
        "failed_samples": failures[:50],
    }
    manifest_path = output_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("done")
    print(f"output: {output_root}")
    print(f"manifest: {manifest_path}")
    print(f"written: {manifest['symbol_files_written']} / {manifest['symbol_files_total']}")
    print(f"failed: {manifest['symbol_files_failed']}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build StockAnalyzer offline data package from TDX files"
    )
    parser.add_argument(
        "--vipdoc-root",
        required=True,
        help="TongDaXin vipdoc root, e.g. D:\\通达信\\vipdoc",
    )
    parser.add_argument(
        "--output-root",
        default="data/tdx_offline_package",
        help="Output package directory",
    )
    parser.add_argument(
        "--name-file",
        default="",
        help="Optional path to infoharbor_ex.code",
    )
    parser.add_argument(
        "--symbols",
        default="",
        help="Optional comma-separated symbols, e.g. 600000,000001",
    )
    parser.add_argument(
        "--max-symbols",
        type=int,
        default=0,
        help="Optional cap for symbol count (0 means all)",
    )
    parser.add_argument(
        "--include-bj",
        action="store_true",
        help="Include Beijing market symbols",
    )
    parser.add_argument(
        "--skip-gp",
        action="store_true",
        help="Skip tdxgp.zip enrichment",
    )
    parser.add_argument(
        "--skip-spot",
        action="store_true",
        help="Skip Tencent spot snapshot enrichment",
    )
    return parser.parse_args()


def _collect_day_files(
    vipdoc_root: Path,
    include_bj: bool,
    symbols_filter: set[str],
    max_symbols: int,
) -> list[DayFileItem]:
    result: list[DayFileItem] = []
    markets = ["sh", "sz"] + (["bj"] if include_bj else [])
    for market in markets:
        lday_dir = vipdoc_root / market / "lday"
        if not lday_dir.exists():
            continue
        for path in sorted(lday_dir.glob("*.day")):
            match = re.match(r"^(sh|sz|bj)(\d{6})\.day$", path.name.lower())
            if not match:
                continue
            _, symbol = match.groups()
            if symbols_filter and symbol not in symbols_filter:
                continue
            if not _is_a_share(market=market, symbol=symbol):
                continue
            result.append(DayFileItem(market=market, symbol=symbol, path=path))
            if max_symbols > 0 and len(result) >= max_symbols:
                return result
    return result


def _is_a_share(market: str, symbol: str) -> bool:
    if market == "sh":
        return symbol.startswith(("600", "601", "603", "605", "688", "689"))
    if market == "sz":
        return symbol.startswith(("000", "001", "002", "003", "300", "301"))
    if market == "bj":
        return symbol.startswith(("4", "8"))
    return False


def _resolve_name_file(vipdoc_root: Path, explicit: str) -> Path | None:
    if explicit.strip():
        path = Path(explicit).expanduser().resolve()
        return path if path.exists() else None
    candidate = vipdoc_root.parent / "T0002" / "hq_cache" / "infoharbor_ex.code"
    if candidate.exists():
        return candidate
    return None


def _load_name_map(path: Path) -> dict[str, str]:
    for encoding in ("utf-8", "gbk", "gb2312"):
        try:
            text = path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
        except OSError:
            return {}
        mapping: dict[str, str] = {}
        for line in text.splitlines():
            parts = line.split("|")
            if len(parts) < 2:
                continue
            symbol = parts[0].strip()
            name = parts[1].strip()
            if re.fullmatch(r"\d{6}", symbol):
                mapping[symbol] = name
        if mapping:
            return mapping
    return {}


def _read_day_bars(path: Path) -> pd.DataFrame:
    raw = np.fromfile(path, dtype=DAY_DTYPE)
    if raw.size == 0:
        return pd.DataFrame()

    frame = pd.DataFrame(
        {
            "date": raw["date"].astype(np.uint32),
            "open": raw["open"].astype(np.float64) / 100.0,
            "high": raw["high"].astype(np.float64) / 100.0,
            "low": raw["low"].astype(np.float64) / 100.0,
            "close": raw["close"].astype(np.float64) / 100.0,
            "volume": raw["volume"].astype(np.float64),
            "turnover": raw["amount"].astype(np.float64),
        }
    )
    frame["date"] = pd.to_datetime(frame["date"].astype(str), format="%Y%m%d", errors="coerce")
    frame = frame.dropna(subset=["date"])
    frame = frame.sort_values("date")
    frame = frame.drop_duplicates(subset=["date"], keep="last")
    frame = frame.set_index("date")
    frame.index.name = "date"
    return frame


def _read_gp_features(
    gp_zip: zipfile.ZipFile | None,
    gp_name_set: set[str],
    market: str,
    symbol: str,
) -> pd.DataFrame:
    if gp_zip is None:
        return pd.DataFrame()
    gp_file = {
        "sh": f"gpsh{symbol}.dat",
        "sz": f"gpsz{symbol}.dat",
        "bj": f"gpbj{symbol}.dat",
    }.get(market, "")
    if not gp_file or gp_file not in gp_name_set:
        return pd.DataFrame()

    raw_bytes = gp_zip.read(gp_file)
    if not raw_bytes or len(raw_bytes) % 13 != 0:
        return pd.DataFrame()

    raw = np.frombuffer(raw_bytes, dtype=GP_DTYPE, count=len(raw_bytes) // 13)
    frame = pd.DataFrame(
        {
            "date": raw["date"].astype(np.uint32),
            "flag": raw["flag"].astype(np.uint16),
            "value": raw["value"].astype(np.float64),
        }
    )
    frame = frame[frame["flag"].isin(_NEEDED_GP_FLAGS)]
    if frame.empty:
        return pd.DataFrame()

    frame["date"] = pd.to_datetime(frame["date"].astype(str), format="%Y%m%d", errors="coerce")
    frame = frame.dropna(subset=["date"])
    if frame.empty:
        return pd.DataFrame()
    frame = frame.sort_values(["date"])
    frame = frame.drop_duplicates(subset=["date", "flag"], keep="last")
    pivot = frame.pivot(index="date", columns="flag", values="value").sort_index()

    holder_primary = _series_from_pivot(pivot, _GP_FLAG_HOLDER_A)
    holder_backup = _series_from_pivot(pivot, _GP_FLAG_HOLDER_B)
    holder = holder_primary.combine_first(holder_backup)

    result = pd.DataFrame(index=pivot.index)
    result["holder_count_raw"] = holder
    result["block_trade_net_raw"] = _series_from_pivot(pivot, _GP_FLAG_BLOCK_NET)
    result["financing_balance_raw"] = _series_from_pivot(pivot, _GP_FLAG_FINANCING)
    result["debt_ratio_raw"] = _series_from_pivot(pivot, _GP_FLAG_DEBT_RATIO)
    result["roe_raw"] = _series_from_pivot(pivot, _GP_FLAG_ROE)
    result["northbound_net_raw"] = _series_from_pivot(pivot, _GP_FLAG_NORTHBOUND)
    dragon = _series_from_pivot(pivot, _GP_FLAG_DRAGON_TIGER)
    result["dragon_tiger_raw"] = dragon.where(dragon.notna(), 0.0)
    return result


def _series_from_pivot(pivot: pd.DataFrame, flag: int) -> pd.Series:
    if flag not in pivot.columns:
        return pd.Series(index=pivot.index, dtype=float)
    return pd.to_numeric(pivot[flag], errors="coerce")


def _prepare_share_frame(
    *,
    bars: pd.DataFrame,
    share_history: pd.DataFrame | None,
    spot_snapshot: dict[str, object] | None,
) -> pd.DataFrame:
    frame = pd.DataFrame(index=bars.index)
    history = share_history.copy() if isinstance(share_history, pd.DataFrame) else pd.DataFrame()
    if not history.empty:
        if not isinstance(history.index, pd.DatetimeIndex):
            history.index = pd.to_datetime(history.index, errors="coerce")
        history = history[history.index.notna()].sort_index()
    if not history.empty:
        for column in ("float_shares", "total_shares"):
            if column in history.columns:
                frame[column] = pd.to_numeric(history[column], errors="coerce")

    snapshot = dict(spot_snapshot) if isinstance(spot_snapshot, dict) else {}
    snapshot_date = bars.index.max()
    if isinstance(snapshot_date, pd.Timestamp) and not snapshot_date.tzinfo:
        if snapshot:
            snapshot_row = pd.DataFrame(
                {
                    "float_shares": [pd.to_numeric(snapshot.get("float_shares"), errors="coerce")],
                    "total_shares": [pd.to_numeric(snapshot.get("total_shares"), errors="coerce")],
                },
                index=pd.DatetimeIndex([snapshot_date]),
            )
            frame = pd.concat([frame, snapshot_row], axis=0)
            frame = frame[~frame.index.duplicated(keep="last")]

    if frame.empty:
        return frame
    frame = frame.sort_index()
    for column in ("float_shares", "total_shares"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").ffill().bfill()
    return frame.reindex(bars.index).ffill().bfill()


def _load_share_history_from_tdxfin(
    *,
    fin_zip_path: Path,
    symbols: set[str],
) -> tuple[dict[str, pd.DataFrame], dict[str, object]]:
    if not fin_zip_path.exists():
        return {}, {"exists": False}

    per_symbol_rows: dict[str, list[tuple[pd.Timestamp, float, float]]] = defaultdict(list)
    parsed_reports = 0
    with zipfile.ZipFile(fin_zip_path) as zf:
        entries = sorted(
            (
                entry
                for entry in zf.infolist()
                if re.match(r"gpcw\d{8}\.dat$", entry.filename.lower()) and entry.file_size > 20
            ),
            key=lambda entry: entry.filename.lower(),
        )
        for entry in entries:
            raw = zf.read(entry.filename)
            parsed = _parse_gpcw_dat(raw)
            if parsed.empty:
                continue
            if _TDXFIN_TOTAL_SHARES_COLUMN not in parsed.columns:
                continue
            total = pd.to_numeric(parsed[_TDXFIN_TOTAL_SHARES_COLUMN], errors="coerce")
            if _TDXFIN_FLOAT_SHARES_COLUMN in parsed.columns:
                float_shares = pd.to_numeric(parsed[_TDXFIN_FLOAT_SHARES_COLUMN], errors="coerce")
            else:
                float_shares = pd.Series(np.nan, index=parsed.index, dtype=float)
            report_date = pd.to_datetime(
                parsed["report_date"].astype(str),
                format="%Y%m%d",
                errors="coerce",
            )
            subset = pd.DataFrame(
                {
                    "code": parsed["code"].astype(str).str.strip(),
                    "report_date": report_date,
                    "total_shares": total,
                    "float_shares": float_shares,
                }
            )
            subset = subset.dropna(subset=["report_date"])
            subset = subset[subset["code"].isin(symbols)]
            if subset.empty:
                continue
            parsed_reports += 1
            for row in subset.itertuples(index=False):
                code = str(row.code).strip()
                if not code:
                    continue
                total_value = (
                    float(row.total_shares) if pd.notna(row.total_shares) else float("nan")
                )
                float_value = (
                    float(row.float_shares) if pd.notna(row.float_shares) else float("nan")
                )
                per_symbol_rows[code].append(
                    (pd.Timestamp(row.report_date), total_value, float_value)
                )

    share_history: dict[str, pd.DataFrame] = {}
    for symbol, items in per_symbol_rows.items():
        frame = pd.DataFrame(items, columns=["report_date", "total_shares", "float_shares"])
        frame = frame.dropna(subset=["report_date"])
        if frame.empty:
            continue
        frame["total_shares"] = pd.to_numeric(frame["total_shares"], errors="coerce")
        frame["float_shares"] = pd.to_numeric(frame["float_shares"], errors="coerce")
        frame["float_shares"] = frame["float_shares"].where(
            frame["float_shares"] > 0.0,
            frame["total_shares"],
        )
        frame = frame.sort_values("report_date").drop_duplicates(
            subset=["report_date"],
            keep="last",
        )
        frame = frame.set_index("report_date")
        share_history[symbol] = frame

    return share_history, {
        "exists": True,
        "reports_parsed": parsed_reports,
        "symbols_with_share_history": len(share_history),
        "total_symbol_rows": sum(len(frame) for frame in share_history.values()),
        "source_columns": {
            "total_shares": _TDXFIN_TOTAL_SHARES_COLUMN,
            "float_shares": _TDXFIN_FLOAT_SHARES_COLUMN,
        },
    }


def _fetch_tencent_spot_snapshots(
    symbols: list[str],
) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    snapshots: dict[str, dict[str, object]] = {}
    normalized_symbols = [
        item for item in {_normalize_symbol_text(symbol) for symbol in symbols} if item
    ]
    if not normalized_symbols:
        return snapshots, {"requested": 0, "received": 0, "failed_batches": 0}

    failed_batches = 0
    batch_size = 80
    for offset in range(0, len(normalized_symbols), batch_size):
        batch = normalized_symbols[offset : offset + batch_size]
        url_symbols = ",".join(
            _to_tencent_symbol(symbol)
            for symbol in batch
            if _to_tencent_symbol(symbol)
        )
        if not url_symbols:
            continue
        url = f"https://qt.gtimg.cn/q={url_symbols}"
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                text = response.read().decode("gbk", errors="ignore")
        except Exception:
            failed_batches += 1
            continue
        for line in text.split(";"):
            parsed = _parse_tencent_snapshot_line(line)
            if not parsed:
                continue
            snapshots[str(parsed["symbol"])] = parsed
        time.sleep(0.05)

    return snapshots, {
        "requested": len(normalized_symbols),
        "received": len(snapshots),
        "failed_batches": failed_batches,
    }


def _parse_tencent_snapshot_line(line: str) -> dict[str, object]:
    text = line.strip()
    if not text or '="' not in text:
        return {}
    try:
        payload = text.split('="', 1)[1].rsplit('"', 1)[0]
    except Exception:
        return {}
    parts = payload.split("~")
    if len(parts) < 74:
        return {}
    symbol = _normalize_symbol_text(parts[2])
    if not symbol:
        return {}
    price = _to_float(parts[3])
    float_shares = _to_float(parts[72])
    total_shares = _to_float(parts[73])
    float_market_cap = _to_float(parts[44]) * 100_000_000.0
    total_market_cap = _to_float(parts[45]) * 100_000_000.0
    if float_market_cap <= 0.0 and price > 0.0 and float_shares > 0.0:
        float_market_cap = price * float_shares
    if total_market_cap <= 0.0 and price > 0.0 and total_shares > 0.0:
        total_market_cap = price * total_shares
    return {
        "symbol": symbol,
        "name": parts[1].strip(),
        "price": price,
        "turnover_rate": _to_float(parts[38]) / 100.0,
        "float_market_cap": float_market_cap,
        "total_market_cap": total_market_cap,
        "float_shares": float_shares,
        "total_shares": total_shares,
        "trade_timestamp": parts[30].strip(),
    }


def _build_output_frame(
    symbol: str,
    bars: pd.DataFrame,
    gp_features: pd.DataFrame,
    stock_name: str,
    share_history: pd.DataFrame | None,
    spot_snapshot: dict[str, object] | None,
) -> pd.DataFrame:
    data = bars.copy()
    if not gp_features.empty:
        data = data.join(gp_features, how="left")
        data[gp_features.columns] = data[gp_features.columns].ffill()

    share_frame = _prepare_share_frame(
        bars=bars,
        share_history=share_history,
        spot_snapshot=spot_snapshot,
    )
    if not share_frame.empty:
        data = data.join(share_frame, how="left")
        for column in ("float_shares", "total_shares"):
            if column in data.columns:
                data[column] = pd.to_numeric(data[column], errors="coerce").ffill().bfill()

    holder_count = _numeric_column(
        data=data,
        column="holder_count_raw",
    ).fillna(DEFAULT_HOLDER_COUNT)
    block_trade_net = _numeric_column(data=data, column="block_trade_net_raw").fillna(0.0)
    financing_balance = _numeric_column(data=data, column="financing_balance_raw").fillna(
        DEFAULT_FINANCING_BALANCE
    )
    northbound_net = _numeric_column(data=data, column="northbound_net_raw").fillna(0.0)
    dragon_tiger_flag = (
        _numeric_column(data=data, column="dragon_tiger_raw").fillna(0.0).abs() > 0
    ).astype(float)

    roe_raw = _numeric_column(data=data, column="roe_raw")
    debt_raw = _numeric_column(data=data, column="debt_ratio_raw")
    roe = _normalize_ratio_series(roe_raw).fillna(DEFAULT_ROE)
    debt_ratio = (
        _normalize_ratio_series(debt_raw)
        .clip(lower=0.0, upper=1.5)
        .fillna(DEFAULT_DEBT_RATIO)
    )

    roe_missing = roe_raw.isna()
    debt_missing = debt_raw.isna()
    missing_fields = _missing_field_series(roe_missing=roe_missing, debt_missing=debt_missing)

    report_date = ""
    if not gp_features.empty:
        metric_mask = gp_features["roe_raw"].notna() | gp_features["debt_ratio_raw"].notna()
        metric_dates = gp_features.index[metric_mask]
        if len(metric_dates) > 0:
            report_date = metric_dates.max().strftime("%Y-%m-%d")

    snapshot_name = ""
    if isinstance(spot_snapshot, dict):
        snapshot_name = str(spot_snapshot.get("name", "")).strip()
    name_clean = stock_name.strip() or snapshot_name
    is_st = _contains_st(name_clean)
    is_delisting = _contains_delisting_risk(name_clean)
    board = _infer_board(symbol)
    float_shares = _numeric_column(data=data, column="float_shares")
    total_shares = _numeric_column(data=data, column="total_shares")
    share_basis = float_shares.where(
        float_shares > 0.0,
        total_shares.where(total_shares > 0.0, np.nan),
    )
    float_market_cap = (
        pd.to_numeric(data["close"], errors="coerce").fillna(0.0) * share_basis
    ).replace([np.inf, -np.inf], np.nan)
    float_market_cap = float_market_cap.fillna(DEFAULT_FLOAT_MARKET_CAP)

    output = pd.DataFrame(index=data.index)
    output["open"] = pd.to_numeric(data["open"], errors="coerce").fillna(0.0)
    output["high"] = pd.to_numeric(data["high"], errors="coerce").fillna(0.0)
    output["low"] = pd.to_numeric(data["low"], errors="coerce").fillna(0.0)
    output["close"] = pd.to_numeric(data["close"], errors="coerce").fillna(0.0)
    output["volume"] = pd.to_numeric(data["volume"], errors="coerce").fillna(0.0)
    output["turnover"] = pd.to_numeric(data["turnover"], errors="coerce").fillna(0.0)
    output["float_market_cap"] = float_market_cap
    output["suspended"] = False
    output["name"] = name_clean
    output["is_st"] = is_st
    output["is_delisting_risk"] = is_delisting
    output["roe"] = roe
    output["debt_ratio"] = debt_ratio
    output["financial_data_complete"] = True
    output["financial_missing_fields"] = missing_fields
    output["financial_source"] = np.where(
        roe_missing | debt_missing,
        "tdxgp_heuristic+default",
        "tdxgp_heuristic",
    )
    output["financial_report_date"] = report_date
    output["holder_count"] = holder_count
    output["block_trade_net"] = block_trade_net
    output["financing_balance"] = financing_balance
    output["margin_financing_balance"] = financing_balance
    output["northbound_net"] = northbound_net
    output["dragon_tiger_flag"] = dragon_tiger_flag
    output["background_data_source"] = np.where(
        gp_features.empty,
        "tdx_default",
        "tdxgp_heuristic",
    )
    output["background_data_complete"] = True
    output["board"] = board

    output = output.sort_index()
    output = output[~output.index.duplicated(keep="last")]
    output.index.name = "date"
    return output[OUTPUT_COLUMNS]


def _normalize_ratio_series(series: pd.Series | None) -> pd.Series:
    if not isinstance(series, pd.Series):
        return pd.Series(dtype=float)
    values = pd.to_numeric(series, errors="coerce")
    normalized = values.copy()
    mask = normalized.abs() > 1.0
    normalized.loc[mask] = normalized.loc[mask] / 100.0
    return normalized


def _numeric_column(data: pd.DataFrame, column: str) -> pd.Series:
    if column not in data.columns:
        return pd.Series(np.nan, index=data.index, dtype=float)
    return pd.to_numeric(data[column], errors="coerce")


def _missing_field_series(roe_missing: pd.Series, debt_missing: pd.Series) -> pd.Series:
    output = pd.Series("", index=roe_missing.index, dtype=object)
    both = roe_missing & debt_missing
    only_roe = roe_missing & (~debt_missing)
    only_debt = (~roe_missing) & debt_missing
    output.loc[both] = "roe,debt_ratio"
    output.loc[only_roe] = "roe"
    output.loc[only_debt] = "debt_ratio"
    return output


def _contains_st(name: str) -> bool:
    text = name.strip().upper()
    return text.startswith("ST") or "*ST" in text


def _normalize_symbol_text(symbol: str) -> str:
    text = str(symbol).strip().upper()
    for suffix in (".SH", ".SZ", ".BJ"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return text if re.fullmatch(r"\d{6}", text) else ""


def _to_tencent_symbol(symbol: str) -> str:
    normalized = _normalize_symbol_text(symbol)
    if not normalized:
        return ""
    if normalized.startswith(("5", "6", "9")):
        return f"sh{normalized}"
    return f"sz{normalized}"


def _to_float(value: object) -> float:
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0
    if not np.isfinite(result):
        return 0.0
    return result


def _contains_delisting_risk(name: str) -> bool:
    text = name.strip().upper()
    return "退" in text or "DELIST" in text


def _infer_board(symbol: str) -> str:
    text = symbol.strip()
    if text.startswith("688"):
        return "star"
    if text.startswith("300") or text.startswith("301"):
        return "gem"
    if text.startswith("8") or text.startswith("4"):
        return "bj"
    return "main"


def _read_tdxfin_summary(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"exists": False}

    with zipfile.ZipFile(path) as zf:
        dates: list[str] = []
        dates_with_data: list[str] = []
        for entry in zf.infolist():
            match = re.match(r"gpcw(\d{8})\.(dat|zip)$", entry.filename.lower())
            if not match:
                continue
            report_date = match.group(1)
            dates.append(report_date)
            if entry.filename.lower().endswith(".dat") and entry.file_size > 20:
                dates_with_data.append(report_date)
    unique = sorted(set(dates))
    unique_with_data = sorted(set(dates_with_data))
    return {
        "exists": True,
        "report_dates_total": len(unique),
        "report_date_min": unique[0] if unique else "",
        "report_date_max": unique[-1] if unique else "",
        "non_placeholder_total": len(unique_with_data),
        "non_placeholder_min": unique_with_data[0] if unique_with_data else "",
        "non_placeholder_max": unique_with_data[-1] if unique_with_data else "",
    }


def _export_latest_tdxfin_raw(fin_zip_path: Path, output_root: Path) -> dict[str, object]:
    if not fin_zip_path.exists():
        return {"exported": False, "reason": "tdxfin.zip not found"}

    dat_candidates: list[tuple[str, int, str]] = []
    with zipfile.ZipFile(fin_zip_path) as zf:
        for entry in zf.infolist():
            match = re.match(r"gpcw(\d{8})\.dat$", entry.filename.lower())
            if not match:
                continue
            report_date = match.group(1)
            if entry.file_size <= 20:
                continue
            dat_candidates.append((report_date, entry.file_size, entry.filename))
        if not dat_candidates:
            return {"exported": False, "reason": "no non-placeholder gpcw*.dat in zip"}
        dat_candidates.sort()
        latest_item = dat_candidates[-1]
        large_candidates = [item for item in dat_candidates if item[1] >= 1_000_000]
        latest_large_item = large_candidates[-1] if large_candidates else latest_item
        targets = [latest_item]
        if latest_large_item[2] != latest_item[2]:
            targets.append(latest_large_item)

        out_dir = output_root / "financial_raw"
        out_dir.mkdir(parents=True, exist_ok=True)
        exports: list[dict[str, object]] = []
        for report_date, _, entry_name in targets:
            raw = zf.read(entry_name)
            parsed = _parse_gpcw_dat(raw)
            if parsed.empty:
                continue
            out_path = out_dir / f"gpcw_{report_date}.csv"
            parsed.to_csv(out_path, index=False)
            exports.append(
                {
                    "report_date": report_date,
                    "rows": int(len(parsed)),
                    "columns": int(len(parsed.columns)),
                    "path": str(out_path),
                }
            )
    if not exports:
        return {"exported": False, "reason": "failed to parse selected gpcw dat files"}

    return {
        "exported": True,
        "files": exports,
    }


def _parse_gpcw_dat(raw: bytes) -> pd.DataFrame:
    if not raw:
        return pd.DataFrame()

    header_fmt = "<1hI1H3L"
    stock_item_fmt = "<6s1c1L"
    header_size = calcsize(header_fmt)
    stock_item_size = calcsize(stock_item_fmt)
    if len(raw) < header_size:
        return pd.DataFrame()

    try:
        header = unpack(header_fmt, raw[:header_size])
    except Exception:  # noqa: BLE001
        return pd.DataFrame()

    report_date = int(header[1])
    max_count = int(header[2])
    report_size = int(header[4])
    if max_count <= 0 or report_size <= 0:
        return pd.DataFrame()

    report_fields_count = report_size // 4
    report_fmt = f"<{report_fields_count}f"
    expected_min = header_size + max_count * stock_item_size
    if len(raw) < expected_min:
        return pd.DataFrame()

    rows: list[list[object]] = []
    for idx in range(max_count):
        base = header_size + idx * stock_item_size
        item = raw[base : base + stock_item_size]
        if len(item) != stock_item_size:
            break
        try:
            code_bytes, _market_flag, foa = unpack(stock_item_fmt, item)
        except Exception:  # noqa: BLE001
            continue
        code = code_bytes.decode("utf-8", errors="ignore").strip("\x00")
        offset = int(foa)
        if not code or offset < 0 or offset + report_size > len(raw):
            continue
        content = raw[offset : offset + report_size]
        try:
            values = unpack(report_fmt, content)
        except Exception:  # noqa: BLE001
            continue
        rows.append([code, report_date, *values])

    if not rows:
        return pd.DataFrame()
    columns = ["code", "report_date"] + [f"col{i}" for i in range(1, report_fields_count + 1)]
    return pd.DataFrame(rows, columns=columns)


if __name__ == "__main__":
    main()
