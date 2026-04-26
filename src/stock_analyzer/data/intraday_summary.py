"""Build and load daily intraday summary features from TDX/Sina minute bars."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from stock_analyzer.feature.intraday_factors import summarize_intraday_factors

MINUTE_DTYPE = np.dtype(
    [
        ("date", "<u2"),
        ("minutes", "<u2"),
        ("open", "<f4"),
        ("high", "<f4"),
        ("low", "<f4"),
        ("close", "<f4"),
        ("amount", "<f4"),
        ("volume", "<i4"),
        ("reserved", "<i4"),
    ]
)

SUPPORTED_INTERVALS = {"1m": 1, "5m": 5}


@dataclass(slots=True)
class IntradaySyncReport:
    interval: str
    symbols_total: int
    local_source_used: int
    online_delta_used: int
    files_written: int
    skipped: int
    failed: int
    target_end_date: str
    latest_date_max: str
    failed_samples: list[dict[str, str]]

    def to_dict(self) -> dict[str, object]:
        return {
            "interval": self.interval,
            "symbols_total": self.symbols_total,
            "local_source_used": self.local_source_used,
            "online_delta_used": self.online_delta_used,
            "files_written": self.files_written,
            "skipped": self.skipped,
            "failed": self.failed,
            "target_end_date": self.target_end_date,
            "latest_date_max": self.latest_date_max,
            "failed_samples": list(self.failed_samples),
        }


def sync_intraday_summary_bundle(
    *,
    vipdoc_root: str | Path,
    output_root: str | Path,
    symbols: list[str],
    target_end_date: str | date | None = None,
    intervals: tuple[str, ...] = ("1m", "5m"),
    max_workers: int = 8,
    online_delta: bool = True,
) -> dict[str, object]:
    resolved_vipdoc_root = Path(vipdoc_root).expanduser().resolve()
    resolved_output_root = Path(output_root).expanduser().resolve()
    clean_symbols = sorted(
        {_normalize_symbol(symbol) for symbol in symbols if _normalize_symbol(symbol)}
    )
    end_date = _normalize_target_end_date(target_end_date)
    interval_reports: list[dict[str, object]] = []
    interval_state: dict[str, dict[str, object]] = {}
    for interval in intervals:
        if interval not in SUPPORTED_INTERVALS:
            continue
        report = _sync_one_interval(
            vipdoc_root=resolved_vipdoc_root,
            output_root=resolved_output_root,
            symbols=clean_symbols,
            interval=interval,
            target_end_date=end_date,
            max_workers=max(1, int(max_workers)),
            online_delta=online_delta,
        )
        interval_reports.append(report.to_dict())
        interval_state[interval] = {
            "target_end_date": end_date.isoformat(),
            "latest_date_max": report.latest_date_max,
            "symbols_total": report.symbols_total,
            "files_written": report.files_written,
            "failed": report.failed,
        }
    manifest = {
        "generated_at": datetime.now().isoformat(),
        "vipdoc_root": str(resolved_vipdoc_root),
        "output_root": str(resolved_output_root),
        "symbols_total": len(clean_symbols),
        "intervals": interval_state,
        "reports": interval_reports,
    }
    manifest_path = resolved_output_root / "intraday_summary_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "generated_at": manifest["generated_at"],
        "manifest_path": str(manifest_path),
        "intervals": interval_state,
        "reports": interval_reports,
    }


def load_intraday_summary(
    *,
    root: str | Path,
    symbol: str,
    interval: str,
    lookback_days: int = 120,
) -> pd.DataFrame:
    normalized_symbol = _normalize_symbol(symbol)
    if not normalized_symbol:
        return pd.DataFrame()
    target = _resolve_summary_path(Path(root).expanduser(), normalized_symbol, interval)
    if target is None:
        return pd.DataFrame()
    frame = pd.read_csv(target, compression="infer")
    if frame.empty:
        return frame
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"])
    frame = frame.set_index("date").sort_index()
    numeric_columns = [col for col in frame.columns if col != "symbol"]
    for column in numeric_columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.tail(max(1, int(lookback_days))).copy()


def fetch_sina_minute_bars(
    *,
    symbol: str,
    interval: str,
    timeout_sec: int = 30,
) -> pd.DataFrame:
    normalized_symbol = _normalize_symbol(symbol)
    market_symbol = _to_sina_symbol(normalized_symbol)
    if not market_symbol:
        return pd.DataFrame()
    interval_minutes = SUPPORTED_INTERVALS.get(interval)
    if interval_minutes is None:
        raise ValueError(f"unsupported interval: {interval}")
    params = {
        "symbol": market_symbol,
        "scale": str(interval_minutes),
        "ma": "no",
        "datalen": "1970",
    }
    url = (
        "https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData?"
        + urllib.parse.urlencode(params)
    )
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn/",
        },
    )
    with urllib.request.urlopen(request, timeout=max(5, int(timeout_sec))) as response:
        text = response.read().decode("utf-8", errors="ignore")
    if "=(" not in text:
        return pd.DataFrame()
    payload = json.loads(text.split("=(", 1)[1].split(");", 1)[0])
    if not isinstance(payload, list) or not payload:
        return pd.DataFrame()
    frame = pd.DataFrame(payload).iloc[:, :7].copy()
    rename_map = {
        "day": "datetime",
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
        "amount": "amount",
    }
    frame = frame.rename(columns=rename_map)
    frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce")
    frame = frame.dropna(subset=["datetime"])
    for column in ("open", "high", "low", "close", "volume", "amount"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    frame = frame.set_index("datetime").sort_index()
    frame.index.name = "datetime"
    return frame


def summarize_minute_bars(
    frame: pd.DataFrame,
    *,
    interval: str,
) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    if interval not in SUPPORTED_INTERVALS:
        raise ValueError(f"unsupported interval: {interval}")
    ordered = frame.sort_index().copy()
    ordered = ordered[~ordered.index.duplicated(keep="last")]
    return summarize_intraday_factors(ordered, interval=interval)


def read_tdx_minute_bars(
    *,
    vipdoc_root: str | Path,
    symbol: str,
    interval: str,
) -> pd.DataFrame:
    path = resolve_tdx_minute_path(vipdoc_root=vipdoc_root, symbol=symbol, interval=interval)
    if path is None or not path.exists():
        return pd.DataFrame()
    raw = np.fromfile(path, dtype=MINUTE_DTYPE)
    if raw.size == 0:
        return pd.DataFrame()
    date_raw = raw["date"].astype(np.int32)
    date_part = date_raw % 2048
    year = date_raw // 2048 + 2004
    month = date_part // 100
    day = date_part % 100
    minutes_raw = raw["minutes"].astype(np.int32)
    hour = minutes_raw // 60
    minute = minutes_raw % 60
    frame = pd.DataFrame(
        {
            "datetime": pd.to_datetime(
                {
                    "year": year,
                    "month": month,
                    "day": day,
                    "hour": hour,
                    "minute": minute,
                },
                errors="coerce",
            ),
            "open": raw["open"].astype(float),
            "high": raw["high"].astype(float),
            "low": raw["low"].astype(float),
            "close": raw["close"].astype(float),
            "volume": raw["volume"].astype(float),
            "amount": raw["amount"].astype(float),
        }
    )
    frame = frame.dropna(subset=["datetime"])
    frame = frame.set_index("datetime").sort_index()
    frame.index.name = "datetime"
    return frame


def resolve_tdx_minute_path(
    *,
    vipdoc_root: str | Path,
    symbol: str,
    interval: str,
) -> Path | None:
    normalized_symbol = _normalize_symbol(symbol)
    if not normalized_symbol:
        return None
    interval = interval.strip().lower()
    if interval == "1m":
        folder = "minline"
        suffix = ".lc1"
    elif interval == "5m":
        folder = "fzline"
        suffix = ".lc5"
    else:
        return None
    market = _market_for_symbol(normalized_symbol)
    if not market:
        return None
    candidate = (
        Path(vipdoc_root).expanduser() / market / folder / f"{market}{normalized_symbol}{suffix}"
    )
    return candidate if candidate.exists() else None


def read_intraday_summary_manifest(output_root: str | Path) -> dict[str, object]:
    manifest_path = Path(output_root).expanduser() / "intraday_summary_manifest.json"
    if not manifest_path.exists():
        return {"exists": False, "manifest_path": str(manifest_path)}
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {"exists": False, "manifest_path": str(manifest_path)}
    payload["exists"] = True
    payload["manifest_path"] = str(manifest_path)
    return payload


def _sync_one_interval(
    *,
    vipdoc_root: Path,
    output_root: Path,
    symbols: list[str],
    interval: str,
    target_end_date: date,
    max_workers: int,
    online_delta: bool,
) -> IntradaySyncReport:
    summary_root = output_root / "intraday_summary" / interval
    summary_root.mkdir(parents=True, exist_ok=True)
    files_written = 0
    skipped = 0
    failed = 0
    local_source_used = 0
    online_delta_used = 0
    latest_dates: list[str] = []
    failed_samples: list[dict[str, str]] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                _sync_one_symbol_interval,
                vipdoc_root=vipdoc_root,
                summary_root=summary_root,
                symbol=symbol,
                interval=interval,
                target_end_date=target_end_date,
                online_delta=online_delta,
            ): symbol
            for symbol in symbols
        }
        for future in as_completed(future_map):
            symbol = future_map[future]
            try:
                result = future.result()
            except Exception as exc:
                failed += 1
                if len(failed_samples) < 20:
                    failed_samples.append(
                        {"symbol": symbol, "reason": f"{exc.__class__.__name__}:{exc}"}
                    )
                continue
            if result["status"] == "written":
                files_written += 1
            elif result["status"] == "skipped":
                skipped += 1
            elif result["status"] == "failed":
                failed += 1
                if len(failed_samples) < 20:
                    failed_samples.append(
                        {"symbol": symbol, "reason": str(result.get("reason", "unknown"))}
                    )
            if bool(result.get("used_local_source", False)):
                local_source_used += 1
            if bool(result.get("used_online_delta", False)):
                online_delta_used += 1
            latest_date = str(result.get("latest_date", "")).strip()
            if latest_date:
                latest_dates.append(latest_date)

    latest_date_max = max(latest_dates) if latest_dates else ""
    return IntradaySyncReport(
        interval=interval,
        symbols_total=len(symbols),
        local_source_used=local_source_used,
        online_delta_used=online_delta_used,
        files_written=files_written,
        skipped=skipped,
        failed=failed,
        target_end_date=target_end_date.isoformat(),
        latest_date_max=latest_date_max,
        failed_samples=failed_samples,
    )


def _sync_one_symbol_interval(
    *,
    vipdoc_root: Path,
    summary_root: Path,
    symbol: str,
    interval: str,
    target_end_date: date,
    online_delta: bool,
) -> dict[str, object]:
    summary_path = summary_root / f"{symbol}.csv.gz"
    existing = load_intraday_summary(root=summary_root.parent, symbol=symbol, interval=interval)
    existing_latest = _frame_latest_date(existing)
    local_latest = _local_tdx_latest_date(vipdoc_root=vipdoc_root, symbol=symbol, interval=interval)
    need_local_rebuild = existing.empty or (
        local_latest is not None and (existing_latest is None or local_latest > existing_latest)
    )

    working = existing
    used_local_source = False
    if need_local_rebuild:
        local_frame = read_tdx_minute_bars(
            vipdoc_root=vipdoc_root, symbol=symbol, interval=interval
        )
        working = summarize_minute_bars(local_frame, interval=interval)
        used_local_source = not working.empty
        existing_latest = _frame_latest_date(working)

    used_online_delta = False
    if online_delta and (existing_latest is None or existing_latest < target_end_date):
        online_frame = fetch_sina_minute_bars(symbol=symbol, interval=interval)
        if not online_frame.empty:
            online_summary = summarize_minute_bars(online_frame, interval=interval)
            if not online_summary.empty:
                if existing_latest is not None:
                    online_index = pd.DatetimeIndex(online_summary.index)
                    online_summary = online_summary.loc[online_index.date >= existing_latest]
                if working.empty:
                    working = online_summary
                else:
                    working = pd.concat([working, online_summary], axis=0)
                    working = working[~working.index.duplicated(keep="last")].sort_index()
                used_online_delta = not online_summary.empty

    if working.empty:
        return {
            "status": "failed",
            "reason": "no_intraday_summary",
            "used_local_source": used_local_source,
            "used_online_delta": used_online_delta,
            "latest_date": "",
        }

    latest_date = _frame_latest_date(working)
    changed = need_local_rebuild or used_online_delta or not summary_path.exists()
    if not changed:
        return {
            "status": "skipped",
            "used_local_source": used_local_source,
            "used_online_delta": used_online_delta,
            "latest_date": latest_date.isoformat() if latest_date is not None else "",
        }

    payload = working.reset_index()
    payload.insert(0, "symbol", symbol)
    payload.to_csv(summary_path, index=False, compression="gzip")
    return {
        "status": "written",
        "used_local_source": used_local_source,
        "used_online_delta": used_online_delta,
        "latest_date": latest_date.isoformat() if latest_date is not None else "",
    }


def _resolve_summary_path(root: Path, symbol: str, interval: str) -> Path | None:
    candidates = (
        root / "intraday_summary" / interval / f"{symbol}.csv.gz",
        root / "intraday_summary" / interval / f"{symbol}.csv",
    )
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _frame_latest_date(frame: pd.DataFrame) -> date | None:
    if frame.empty:
        return None
    return pd.Timestamp(frame.index.max()).date()


def _local_tdx_latest_date(*, vipdoc_root: Path, symbol: str, interval: str) -> date | None:
    path = resolve_tdx_minute_path(vipdoc_root=vipdoc_root, symbol=symbol, interval=interval)
    if path is None or not path.exists():
        return None
    with path.open("rb") as fp:
        try:
            fp.seek(-32, 2)
        except OSError:
            return None
        raw = fp.read(32)
    if len(raw) != 32:
        return None
    tail = np.frombuffer(raw, dtype=MINUTE_DTYPE, count=1)
    if tail.size == 0:
        return None
    raw_date = int(tail["date"][0])
    date_part = raw_date % 2048
    year = raw_date // 2048 + 2004
    month = date_part // 100
    day = date_part % 100
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _normalize_target_end_date(value: str | date | None) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        return datetime.fromisoformat(value.strip()).date()
    return datetime.now().date()


def _market_for_symbol(symbol: str) -> str:
    if symbol.startswith(("6", "5", "9")):
        return "sh"
    if symbol.startswith(("0", "1", "2", "3")):
        return "sz"
    if symbol.startswith(("4", "8")):
        return "bj"
    return ""


def _to_sina_symbol(symbol: str) -> str:
    market = _market_for_symbol(symbol)
    if market in {"sh", "sz"}:
        return f"{market}{symbol}"
    return ""


def _normalize_symbol(symbol: str) -> str:
    text = str(symbol).strip().upper()
    for suffix in (".SH", ".SZ", ".BJ"):
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text
