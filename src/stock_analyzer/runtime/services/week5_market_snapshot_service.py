"""Standardized full-market realtime and auction snapshot workflows."""

from __future__ import annotations

import json
import statistics
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from queue import Empty, Queue
from threading import Lock, Thread, current_thread
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd


class Week5MarketSnapshotService:
    """Capture, normalize and replay batch spot snapshots.

    The service intentionally accepts only a generated ``snapshot_id`` for
    replay. Arbitrary server-side paths never enter the public contract.
    """

    def __init__(self, service: Any) -> None:
        self._service = service
        self._fetch_lock = Lock()
        self._fetch_worker: Thread | None = None
        configured_root = str(
            getattr(
                service._config.week5,
                "market_snapshot_root",
                "artifacts/runtime/week5_market_snapshots",
            )
        ).strip()
        self._root = service._resolve_evolution_path(
            configured_root or "artifacts/runtime/week5_market_snapshots"
        )

    def capture(
        self,
        *,
        timestamp: datetime | None = None,
        snapshot_id: str = "",
        frame: pd.DataFrame | None = None,
    ) -> dict[str, object]:
        now = timestamp or _market_now(self._service)
        source = "injected"
        errors: list[str] = []
        if frame is None:
            frame, source, errors = self._fetch_batch_frame_with_timeout()
        normalized = normalize_market_snapshot_frame(frame, timestamp=now, source=source)
        if normalized.empty:
            return {
                "ok": False,
                "status": "unavailable",
                "snapshot_id": "",
                "timestamp": now.isoformat(),
                "source": source,
                "row_count": 0,
                "errors": errors or ["empty_market_snapshot"],
            }
        resolved_id = snapshot_id.strip() or self._new_snapshot_id(now)
        payload = {
            "ok": True,
            "status": "captured",
            "snapshot_id": resolved_id,
            "timestamp": now.isoformat(),
            "source": source,
            "row_count": len(normalized),
            "columns": list(normalized.columns),
            "rows": _frame_rows(normalized),
            "errors": errors,
        }
        self._write_snapshot(resolved_id, payload)
        self._cleanup_snapshots(now=now)
        return payload

    def replay(self, snapshot_id: str) -> dict[str, object]:
        normalized_id = snapshot_id.strip()
        if not normalized_id or "/" in normalized_id or "\\" in normalized_id:
            return {"ok": False, "status": "invalid_snapshot_id", "snapshot_id": normalized_id}
        path = self._root / f"{normalized_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {
                "ok": False,
                "status": "snapshot_not_found",
                "snapshot_id": normalized_id,
            }
        return (
            payload
            if isinstance(payload, dict)
            else {
                "ok": False,
                "status": "snapshot_invalid",
                "snapshot_id": normalized_id,
            }
        )

    def list_recent(self, *, limit: int = 20) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        try:
            paths = sorted(
                self._root.glob("*.json"),
                key=lambda item: (item.stat().st_mtime, item.name),
            )
        except OSError:
            return []
        for path in paths[-max(1, limit) :]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                records.append(payload)
        return records

    def _min_snapshot_rows(self) -> int:
        try:
            return max(
                0,
                int(getattr(self._service._config.week5, "market_snapshot_min_rows", 5000)),
            )
        except (TypeError, ValueError):
            return 5000

    def _fetch_batch_frame(self) -> tuple[pd.DataFrame, str, list[str]]:
        errors: list[str] = []
        # 全市场覆盖率门禁：主源行数低于阈值视为部分数据，必须由备源补齐
        # 或整体拒绝，禁止"仅一只股票也 captured"的伪全市场快照进入下游。
        min_rows = self._min_snapshot_rows()
        primary: pd.DataFrame | None = None
        try:
            import efinance as ef  # type: ignore[import-untyped]

            frame = ef.stock.get_realtime_quotes()
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                # 覆盖率按去重股票数判定（与主备源合并路径同口径）：重复行
                # 不能虚增覆盖，否则 3 行相同股票也会被判为有效全市场快照。
                covered = _coverage_count(frame)
                if min_rows <= 0 or covered >= min_rows:
                    return frame, "efinance", errors
                errors.append(f"efinance_partial_coverage:{covered}")
                primary = frame
            else:
                errors.append("efinance_empty")
        except Exception as exc:
            errors.append(f"efinance:{exc.__class__.__name__}")
        try:
            import akshare as ak  # type: ignore[import-untyped]

            for name in ("stock_zh_a_spot_em", "stock_zh_a_spot"):
                func = getattr(ak, name, None)
                if not callable(func):
                    continue
                frame = func()
                if isinstance(frame, pd.DataFrame) and not frame.empty:
                    if primary is None:
                        covered = _coverage_count(frame)
                        if min_rows <= 0 or covered >= min_rows:
                            return frame, "akshare", errors
                        errors.append(f"{name}_partial_coverage:{covered}")
                        continue
                    # 主源部分数据：用备源补齐缺失股票。两源代码列名异构
                    # （efinance 股票代码 / AkShare 代码），直接 concat 原始帧
                    # 会让后续 normalize 只命中其中一个代码列，另一源整批行
                    # 被当作空代码丢弃（实测只剩 AkShare 股票）。因此先各自
                    # 规范化、再按 [备源, 主源] 顺序合并：capture 阶段
                    # drop_duplicates(keep="last") 以主源（更优先）覆盖重复
                    # symbol，且每行时间戳在各自规范化时已正确落位。
                    fetch_now = _market_now(self._service)
                    normalized_backup = normalize_market_snapshot_frame(
                        frame, timestamp=fetch_now, source=name
                    )
                    normalized_primary = normalize_market_snapshot_frame(
                        primary, timestamp=fetch_now, source="efinance"
                    )
                    if normalized_backup.empty and normalized_primary.empty:
                        errors.append(f"{name}_merge_normalize_failed")
                        continue
                    merged = pd.concat(
                        [normalized_backup, normalized_primary],
                        ignore_index=True,
                    )
                    # 覆盖率按去重股票数判定：两源高度重叠时原始行数会虚增，
                    # 不能作为"全市场覆盖"的依据。
                    covered = _coverage_count(merged)
                    if min_rows <= 0 or covered >= min_rows:
                        return merged, "efinance+akshare", errors
                    errors.append(f"{name}_merge_partial_coverage:{covered}")
                    continue
                errors.append(f"{name}_empty")
        except Exception as exc:
            errors.append(f"akshare:{exc.__class__.__name__}")
        return pd.DataFrame(), "unavailable", errors

    def _fetch_batch_frame_with_timeout(self) -> tuple[pd.DataFrame, str, list[str]]:
        timeout_sec = float(getattr(self._service._config.week5, "market_snapshot_timeout_sec", 10))
        if timeout_sec <= 0:
            return self._fetch_batch_frame()
        with self._fetch_lock:
            if self._fetch_worker is not None and self._fetch_worker.is_alive():
                return (
                    pd.DataFrame(),
                    "unavailable",
                    ["market_snapshot_previous_fetch_in_progress"],
                )
        result_queue: Queue[tuple[pd.DataFrame, str, list[str]]] = Queue(maxsize=1)

        def worker() -> None:
            try:
                result_queue.put(self._fetch_batch_frame())
            except Exception as exc:
                result_queue.put(
                    (pd.DataFrame(), "unavailable", [f"snapshot_worker:{exc.__class__.__name__}"])
                )
            finally:
                with self._fetch_lock:
                    if self._fetch_worker is current_thread():
                        self._fetch_worker = None

        thread = Thread(target=worker, name="week5-market-snapshot", daemon=True)
        with self._fetch_lock:
            if self._fetch_worker is not None and self._fetch_worker.is_alive():
                return (
                    pd.DataFrame(),
                    "unavailable",
                    ["market_snapshot_previous_fetch_in_progress"],
                )
            self._fetch_worker = thread
            thread.start()
        thread.join(timeout=timeout_sec)
        if thread.is_alive():
            return pd.DataFrame(), "unavailable", ["market_snapshot_timeout"]
        try:
            return result_queue.get_nowait()
        except Empty:
            return pd.DataFrame(), "unavailable", ["market_snapshot_empty_result"]

    def _new_snapshot_id(self, now: datetime) -> str:
        return f"{now.strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}"

    def _write_snapshot(self, snapshot_id: str, payload: dict[str, object]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / f"{snapshot_id}.json"
        temp = path.with_name(f"{path.name}.tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
            encoding="utf-8",
        )
        temp.replace(path)

    def _cleanup_snapshots(self, *, now: datetime) -> None:
        retention_hours = float(
            getattr(self._service._config.week5, "market_snapshot_retention_hours", 72.0)
        )
        max_files = max(
            1,
            int(getattr(self._service._config.week5, "market_snapshot_max_files", 500)),
        )
        try:
            paths = sorted(
                self._root.glob("*.json"),
                key=lambda item: (item.stat().st_mtime, item.name),
            )
        except OSError:
            return
        cutoff = None
        if retention_hours > 0:
            current = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
            cutoff = current.astimezone(UTC) - timedelta(hours=retention_hours)
        remaining: list[Path] = []
        for path in paths:
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
                if cutoff is not None and modified < cutoff:
                    path.unlink()
                    continue
                remaining.append(path)
            except OSError:
                continue
        for path in remaining[:-max_files]:
            try:
                path.unlink()
            except OSError:
                continue


def normalize_market_snapshot_frame(
    frame: pd.DataFrame | None,
    *,
    timestamp: datetime,
    source: str,
) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return pd.DataFrame()
    raw = frame.copy()
    columns: dict[str, str] = {_column_token(column): str(column) for column in raw.columns}
    symbol_col = _first(columns, ("代码", "股票代码", "证券代码", "symbol", "code", "stockcode"))
    name_col = _first(columns, ("名称", "股票名称", "name", "stockname"))
    price_col = _first(columns, ("最新价", "现价", "price", "latestprice", "close"))
    prev_close_col = _first(
        columns,
        ("昨收", "昨收价", "昨日收盘", "昨日收盘价", "preclose", "prevclose"),
    )
    change_pct_col = _first(columns, ("涨跌幅", "涨跌幅%", "changepercent", "pctchange"))
    volume_col = _first(columns, ("成交量", "volume", "vol"))
    amount_col = _first(columns, ("成交额", "amount", "turnover"))
    turnover_col = _first(columns, ("换手率", "turnoverrate", "turnover_rate"))
    auction_ratio_col = _first(
        columns,
        ("竞价量比", "竞价量比%", "量比", "量比%", "auctionvolumeratio", "auction_ratio"),
    )
    snapshot_time_col = _first(
        columns,
        (
            "行情时间",
            "更新时间",
            "时间",
            "datetime",
            "timestamp",
            "update_time",
            # 已规范化帧（主备源合并路径）二次规范化时保留每行时间戳。
            "snapshot_time",
        ),
    )
    if symbol_col is None or price_col is None:
        return pd.DataFrame()
    result = pd.DataFrame()
    result["symbol"] = raw[symbol_col].map(_normalize_symbol)
    result["name"] = raw[name_col].astype(str) if name_col is not None else ""
    result["price"] = _numeric(raw[price_col])
    result["prev_close"] = _numeric(raw[prev_close_col]) if prev_close_col is not None else 0.0
    result["change_pct"] = (
        _numeric(raw[change_pct_col]) if change_pct_col is not None else _derive_change_pct(result)
    )
    result["volume"] = _numeric(raw[volume_col]) if volume_col is not None else 0.0
    result["turnover"] = _numeric(raw[amount_col]) if amount_col is not None else 0.0
    result["turnover_rate"] = _numeric(raw[turnover_col]) if turnover_col is not None else 0.0
    result["auction_volume_ratio"] = (
        _numeric(raw[auction_ratio_col]) if auction_ratio_col is not None else float("nan")
    )
    result["gap_pct"] = _derive_change_pct(result)
    result["limit_up_distance"] = [
        _limit_up_distance(
            symbol=str(symbol),
            name=str(name),
            price=_as_float(price),
            prev_close=_as_float(prev_close),
        )
        for symbol, name, price, prev_close in zip(
            result["symbol"].tolist(),
            result["name"].tolist(),
            result["price"].tolist(),
            result["prev_close"].tolist(),
            strict=True,
        )
    ]
    # 备源（如 AkShare spot 接口）不提供行情时间列时，以抓取时刻兜底，
    # 否则整张快照会被判 stale、备源承诺形同虚设。若源提供了时间列但
    # 个别行取值为空，仍按缺失处理（零容忍口径不变）。
    result["snapshot_time"] = (
        [_normalize_snapshot_time(value, timestamp) for value in raw[snapshot_time_col].tolist()]
        if snapshot_time_col is not None
        else [timestamp.isoformat()] * len(result)
    )
    result["source"] = source
    result = result[result["symbol"] != ""].drop_duplicates("symbol", keep="last")
    return result.reset_index(drop=True)


def enrich_auction_metrics(
    frame: pd.DataFrame,
    *,
    baseline: dict[str, list[float]] | None = None,
    now: datetime | None = None,
    max_age_sec: int = 120,
    min_baseline_days: int = 5,
) -> dict[str, object]:
    if frame.empty:
        return {
            "auction_applied": False,
            "status": "unavailable",
            "rows": [],
            "fresh": False,
            "reason": "empty_snapshot",
        }
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    working = frame.copy()
    if "snapshot_time" not in working.columns:
        return {
            "auction_applied": False,
            "status": "stale",
            "rows": _frame_rows(working),
            "fresh": False,
            "reason": "snapshot_time_unavailable",
        }
    timestamps = pd.to_datetime(working["snapshot_time"], errors="coerce", utc=True)
    # 新鲜度取最旧时间戳（最坏情况口径）：任一候选过期即整表视为过期，
    # 防止少数刚更新的行掩盖其余滞后数据。
    oldest = timestamps.min()
    fresh = bool(
        pd.notna(oldest) and abs((current - oldest.to_pydatetime()).total_seconds()) <= max_age_sec
    )
    if not fresh:
        return {
            "auction_applied": False,
            "status": "stale",
            "rows": _frame_rows(working),
            "fresh": False,
            "reason": "snapshot_expired",
        }
    ratio_values = pd.to_numeric(working["auction_volume_ratio"], errors="coerce").tolist()
    filled_from_baseline = 0
    minimum_days = max(5, int(min_baseline_days))
    if baseline:
        for position, (_, row) in enumerate(working.iterrows()):
            if pd.notna(ratio_values[position]):
                continue
            values = [
                _as_float(item)
                for item in baseline.get(str(row.get("symbol", "")), [])
                if _as_float(item) > 0
            ]
            median = statistics.median(values) if len(values) >= minimum_days else 0.0
            volume = _as_float(row.get("volume"))
            if median > 0 and volume > 0:
                ratio_values[position] = volume / median
                filled_from_baseline += 1
    working["auction_volume_ratio"] = ratio_values
    source_values = pd.to_numeric(working["auction_volume_ratio"], errors="coerce")
    missing_mask = source_values.isna()
    missing_ratio_count = int(missing_mask.sum())
    if missing_ratio_count >= len(working):
        return {
            "auction_applied": False,
            "status": "unavailable",
            "rows": _frame_rows(working),
            "fresh": True,
            "reason": "auction_volume_ratio_unavailable",
            "missing_ratio_count": missing_ratio_count,
            "baseline_min_days": minimum_days,
        }
    excluded_missing = 0
    if missing_ratio_count:
        # Plan 验收口径：个别股票缺竞价量比只剔除该股票，不得阻断其余
        # 有效候选进入竞价流程；全部缺失时才整体不可用。
        working = working[~missing_mask].reset_index(drop=True)
        source_values = source_values[~missing_mask]
        excluded_missing = missing_ratio_count
    if filled_from_baseline and source_values.notna().all():
        ratio_source = (
            "historical_09_25_baseline"
            if filled_from_baseline == len(working)
            else "mixed_realtime_and_historical"
        )
    else:
        ratio_source = "realtime_source"
    working["auction_ratio_source"] = ratio_source
    return {
        "auction_applied": True,
        "status": "ok",
        "rows": _frame_rows(working),
        "fresh": True,
        "ratio_source": ratio_source,
        "missing_ratio_excluded": excluded_missing,
        "baseline_min_days": minimum_days,
    }


def _frame_rows(frame: pd.DataFrame) -> list[dict[str, object]]:
    raw_rows = json.loads(frame.to_json(orient="records", date_format="iso"))
    if not isinstance(raw_rows, list):
        return []
    return [dict(item) for item in raw_rows if isinstance(item, dict)]


def _column_token(value: object) -> str:
    return str(value).strip().lower().replace(" ", "").replace("_", "")


def _market_now(service: Any) -> datetime:
    """市场时区当前时间：行情源的墙钟时间（如 09:25:00）按市场本地时间解释。"""
    app_config = getattr(getattr(service, "_config", None), "app", None)
    timezone_name = str(getattr(app_config, "timezone", "Asia/Shanghai")).strip()
    try:
        return datetime.now(ZoneInfo(timezone_name or "Asia/Shanghai"))
    except Exception:
        return datetime.now(UTC)


def _distinct_symbol_count(frame: pd.DataFrame) -> int | None:
    """统计合并帧的去重股票数；识别不了代码列时返回 None（调用方退化为行数）。"""
    for token in ("代码", "股票代码", "证券代码", "symbol", "code", "stockcode"):
        for column in frame.columns:
            if _column_token(column) == token:
                values = frame[column].astype(str).str.extract(r"(\d+)")[0]
                return int(values.dropna().nunique())
    return None


def _coverage_count(frame: pd.DataFrame) -> int:
    """全市场覆盖门禁统一口径：去重股票数；识别不了代码列时退化为行数。"""
    distinct = _distinct_symbol_count(frame)
    return len(frame) if distinct is None else distinct


def _first(columns: Mapping[str, str], aliases: tuple[str, ...]) -> str | None:
    for alias in aliases:
        value = columns.get(_column_token(alias))
        if value is not None:
            return value
    return None


def _numeric(values: pd.Series[Any]) -> pd.Series[Any]:
    return pd.to_numeric(values, errors="coerce")


def _normalize_snapshot_time(value: object, fallback: datetime) -> str:
    # pandas NA 家族（NaN/NaT/<NA>）与空串统一按缺失处理。不使用 pd.isna：
    # 其签名不接受任意 object（strict mypy 阻断），且 str 形式覆盖面等价。
    text = "" if value is None else str(value).strip()
    if not text or text.lower() in {"nan", "nat", "<na>", "none", "null"}:
        return ""
    time_format = "%H:%M:%S" if text.count(":") == 2 else "%H:%M"
    if len(text) in {5, 8} and text[2:3] == ":":
        time_only = pd.to_datetime(text, format=time_format, errors="coerce")
        if pd.notna(time_only):
            local = fallback.replace(
                hour=int(time_only.hour),
                minute=int(time_only.minute),
                second=int(time_only.second),
                microsecond=0,
            )
            return local.isoformat()
    parsed = pd.to_datetime(text, errors="coerce", utc=True)
    return parsed.isoformat() if pd.notna(parsed) else ""


def _limit_up_distance(*, symbol: str, name: str, price: float, prev_close: float) -> float:
    if price <= 0 or prev_close <= 0:
        return 0.0
    normalized_name = name.strip().upper()
    if "ST" in normalized_name or "*ST" in normalized_name:
        limit_pct = 0.05
    elif symbol.startswith(("300", "301", "688", "689")) or symbol.startswith("8"):
        limit_pct = 0.20 if not symbol.startswith("8") else 0.30
    else:
        limit_pct = 0.10
    limit_price = prev_close * (1.0 + limit_pct)
    return round(max(0.0, (limit_price - price) / prev_close * 100.0), 6)


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        number = float(value)
    elif isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return default
    else:
        return default
    return number if pd.notna(number) else default


def _derive_change_pct(frame: pd.DataFrame) -> pd.Series:
    previous = pd.to_numeric(frame["prev_close"], errors="coerce")
    price = pd.to_numeric(frame["price"], errors="coerce")
    return ((price / previous.replace(0, pd.NA)) - 1.0) * 100.0


def _normalize_symbol(value: object) -> str:
    text = str(value).strip()
    digits = "".join(character for character in text if character.isdigit())
    return digits.zfill(6) if digits else ""
