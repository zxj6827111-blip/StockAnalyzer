#!/usr/bin/env python3
"""Collect adjustment factors for a frozen research sample.

The collector supports both per-symbol history requests and the Tushare full-market
``adj_factor(trade_date=...)`` endpoint. Both modes record exact response times and
per-symbol session gaps; neither infers action types nor writes a database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.data.tushare_provider import _to_ts_code  # noqa: E402

SOURCE = "tushare_adj_factor_history"
ROW_CAP = 6_000
FULL_MARKET_MIN_ROWS = 3_000
TOKEN_ENV = "SA__MARKET_WAREHOUSE__TUSHARE_TOKEN"
FACTOR_COLUMNS = ("ts_code", "trade_date", "adj_factor")
COVERAGE_COLUMNS = (
    "symbol",
    "ts_code",
    "source",
    "as_of",
    "requested_sessions",
    "rows_returned",
    "missing_sessions",
    "duplicate_dates",
    "out_of_range_dates",
    "coverage_complete",
    "truncated",
    "error",
    "response_sha256",
)


def _token() -> str:
    value = str(os.environ.get(TOKEN_ENV, "") or "").strip()
    if not value:
        raise RuntimeError(f"missing environment variable {TOKEN_ENV}")
    return value


def _call(pro: Any, method: str, **kwargs: Any) -> Any:
    last: Exception | None = None
    for attempt in range(4):
        try:
            return getattr(pro, method)(**kwargs)
        except Exception as exc:  # noqa: BLE001 - report type, never provider secret text
            last = exc
            if attempt < 3:
                time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(type(last).__name__ if last else "UnknownError")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(
        mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
    ) as handle:
        temp_path = Path(handle.name)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    temp_path.replace(path)


def _checkpoint_digest(payload: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "payload_sha256"}
    encoded = json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _initialize_checkpoint_scope(
    checkpoint_dir: Path,
    *,
    start: str,
    end: str,
    symbols: list[str],
    calendar_sha256: str,
    query_mode: str,
) -> None:
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    if not checkpoint_dir.is_dir():
        raise ValueError("checkpoint_path_must_be_directory")
    scope = {
        "schema": "tail_adj_factor_checkpoint_scope.v1",
        "collector_sha256": _sha256_file(Path(__file__).resolve()),
        "start": start,
        "end": end,
        "symbols_sha256": hashlib.sha256("\n".join(symbols).encode("utf-8")).hexdigest(),
        "symbols": len(symbols),
        "calendar_sha256": calendar_sha256,
        "query_mode": query_mode,
    }
    scope_path = checkpoint_dir / "scope.json"
    if scope_path.exists():
        try:
            existing = json.loads(scope_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("checkpoint_scope_unreadable") from exc
        if existing != scope:
            raise RuntimeError("checkpoint_scope_mismatch")
        return
    if any(checkpoint_dir.iterdir()):
        raise RuntimeError("checkpoint_scope_missing")
    _atomic_json(scope_path, scope)


def _read_symbol_checkpoint(
    path: Path,
    *,
    symbol: str,
    api_symbol: str,
    start: str,
    end: str,
    calendar_sha256: str,
    requested_sessions: int,
) -> dict[str, Any]:
    try:
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("symbol_checkpoint_unreadable") from exc
    if checkpoint.get("payload_sha256") != _checkpoint_digest(checkpoint):
        raise RuntimeError("symbol_checkpoint_hash_mismatch")
    expected_identity = {
        "schema": "tail_adj_factor_symbol_checkpoint.v1",
        "symbol": symbol,
        "ts_code": api_symbol,
        "start": start,
        "end": end,
        "calendar_sha256": calendar_sha256,
    }
    if any(checkpoint.get(key) != value for key, value in expected_identity.items()):
        raise RuntimeError("symbol_checkpoint_scope_mismatch")
    coverage = checkpoint.get("coverage")
    factors = checkpoint.get("factors")
    if (
        not isinstance(coverage, dict)
        or not isinstance(factors, list)
        or coverage.get("symbol") != symbol
        or coverage.get("ts_code") != api_symbol
        or int(coverage.get("requested_sessions", -1)) != requested_sessions
        or coverage.get("error")
    ):
        raise RuntimeError("symbol_checkpoint_invalid")
    for row in factors:
        if (
            not isinstance(row, dict)
            or row.get("symbol") != symbol
            or row.get("ts_code") != api_symbol
            or row.get("source") != SOURCE
            or not start <= str(row.get("trade_date", "")) <= end
        ):
            raise RuntimeError("symbol_checkpoint_invalid_factor_row")
    return checkpoint


def _write_symbol_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    checkpoint = dict(payload)
    checkpoint["payload_sha256"] = _checkpoint_digest(checkpoint)
    _atomic_json(path, checkpoint)


def _read_daily_checkpoint(
    path: Path,
    *,
    trade_day: str,
    start: str,
    end: str,
    calendar_sha256: str,
    symbols_sha256: str,
) -> dict[str, Any]:
    try:
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("daily_checkpoint_unreadable") from exc
    if checkpoint.get("payload_sha256") != _checkpoint_digest(checkpoint):
        raise RuntimeError("daily_checkpoint_hash_mismatch")
    expected = {
        "schema": "tail_adj_factor_daily_checkpoint.v1",
        "trade_date": trade_day,
        "start": start,
        "end": end,
        "calendar_sha256": calendar_sha256,
        "symbols_sha256": symbols_sha256,
    }
    if any(checkpoint.get(key) != value for key, value in expected.items()):
        raise RuntimeError("daily_checkpoint_scope_mismatch")
    if not isinstance(checkpoint.get("factors"), list):
        raise RuntimeError("daily_checkpoint_invalid_factors")
    if not isinstance(checkpoint.get("missing_requested_symbols"), list):
        raise RuntimeError("daily_checkpoint_invalid_missing_symbols")
    for row in checkpoint["factors"]:
        if (
            not isinstance(row, dict)
            or row.get("trade_date") != trade_day
            or row.get("source") != SOURCE
            or not row.get("symbol")
            or not row.get("ts_code")
            or not row.get("as_of")
        ):
            raise RuntimeError("daily_checkpoint_invalid_factor_row")
    return checkpoint


def _write_daily_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    checkpoint = dict(payload)
    checkpoint["payload_sha256"] = _checkpoint_digest(checkpoint)
    _atomic_json(path, checkpoint)


def _collect_by_trade_date(
    pro: Any,
    *,
    start: str,
    end: str,
    requested_symbols: list[str],
    expected_days: list[str],
    calendar_days: int,
    calendar_sha256: str,
    request_interval_sec: float,
    checkpoint_dir: Path | None,
    retry_incomplete: bool,
) -> tuple[Any, Any, dict[str, Any]]:
    import pandas as pd

    api_symbols = {symbol: _to_ts_code(symbol) for symbol in requested_symbols}
    symbol_by_api = {value: key for key, value in api_symbols.items()}
    symbols_sha256 = hashlib.sha256("\n".join(requested_symbols).encode("utf-8")).hexdigest()
    if checkpoint_dir is not None:
        _initialize_checkpoint_scope(
            checkpoint_dir,
            start=start,
            end=end,
            symbols=requested_symbols,
            calendar_sha256=calendar_sha256,
            query_mode="trade_date_market_batch",
        )

    factors_by_symbol: dict[str, list[dict[str, Any]]] = {
        symbol: [] for symbol in requested_symbols
    }
    date_responses: dict[str, dict[str, Any]] = {}
    resumed_days = 0
    queried_days = 0
    failed_days: dict[str, str] = {}
    for index, trade_day in enumerate(expected_days, 1):
        checkpoint_path = (
            checkpoint_dir / "dates" / f"{trade_day}.json" if checkpoint_dir else None
        )
        checkpoint: dict[str, Any] | None = None
        if checkpoint_path is not None and checkpoint_path.exists():
            checkpoint = _read_daily_checkpoint(
                checkpoint_path,
                trade_day=trade_day,
                start=start,
                end=end,
                calendar_sha256=calendar_sha256,
                symbols_sha256=symbols_sha256,
            )
            retry_required = not bool(checkpoint.get("batch_complete")) or bool(
                checkpoint.get("missing_requested_symbols")
            )
            if not retry_incomplete or not retry_required:
                resumed_days += 1
            else:
                checkpoint = None
        if checkpoint is None:
            queried_days += 1
            error = ""
            frame = None
            response_as_of = datetime.now().astimezone().isoformat(timespec="seconds")
            rows: list[dict[str, Any]] = []
            digest = ""
            row_count = 0
            unique_symbols = 0
            truncated = False
            try:
                frame = _call(pro, "adj_factor", trade_date=trade_day)
                response_as_of = datetime.now().astimezone().isoformat(timespec="seconds")
                missing_columns = sorted(set(FACTOR_COLUMNS) - set(frame.columns))
                if missing_columns:
                    raise RuntimeError(f"response_missing_columns:{','.join(missing_columns)}")
                normalized = frame.reindex(columns=FACTOR_COLUMNS).copy()
                normalized["trade_date"] = normalized["trade_date"].astype(str).str[:8]
                row_count = len(normalized)
                truncated = row_count >= ROW_CAP
                digest = hashlib.sha256(
                    normalized.to_csv(index=False, lineterminator="\n").encode("utf-8")
                ).hexdigest()
                if truncated:
                    raise RuntimeError("provider_row_cap_reached")
                malformed: list[str] = []
                requested_rows: list[dict[str, Any]] = []
                seen: set[str] = set()
                for item in normalized.to_dict("records"):
                    code = str(item.get("ts_code") or "").strip().upper()
                    day = str(item.get("trade_date") or "").strip()
                    try:
                        parsed_day = datetime.strptime(day, "%Y%m%d").date()
                        factor = pd.to_numeric(item.get("adj_factor"), errors="coerce")
                    except (TypeError, ValueError):
                        malformed.append("invalid_date_or_factor")
                        continue
                    if parsed_day.strftime("%Y%m%d") != trade_day:
                        malformed.append("out_of_scope_date")
                        continue
                    if (
                        not re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", code)
                        or pd.isna(factor)
                        or not math.isfinite(float(factor))
                        or float(factor) <= 0
                    ):
                        malformed.append("invalid_source_row")
                        continue
                    if code in seen:
                        malformed.append("duplicate_source_symbol")
                        continue
                    seen.add(code)
                    symbol = symbol_by_api.get(code)
                    if symbol is not None:
                        requested_rows.append({
                            "symbol": symbol,
                            "ts_code": code,
                            "trade_date": trade_day,
                            "adj_factor": float(factor),
                            "source": SOURCE,
                            "as_of": response_as_of,
                        })
                unique_symbols = len(seen)
                if unique_symbols < FULL_MARKET_MIN_ROWS:
                    malformed.append("market_batch_below_full_market_threshold")
                if malformed:
                    raise RuntimeError("invalid_market_batch_response")
                rows = sorted(requested_rows, key=lambda row: row["symbol"])
            except Exception as exc:  # noqa: BLE001 - never expose provider response text
                error = (
                    type(exc).__name__
                    if not str(exc).startswith("response_missing")
                    else str(exc)
                )
                failed_days[trade_day] = error
                rows = []
            checkpoint = {
                "schema": "tail_adj_factor_daily_checkpoint.v1",
                "trade_date": trade_day,
                "start": start,
                "end": end,
                "calendar_sha256": calendar_sha256,
                "symbols_sha256": symbols_sha256,
                "as_of": response_as_of,
                "rows_returned": row_count,
                "unique_source_symbols": unique_symbols,
                "source_response_sha256": digest,
                "batch_complete": not error,
                "truncated": truncated,
                "error": error,
                "factors": rows,
                "missing_requested_symbols": sorted(
                    set(requested_symbols) - {row["symbol"] for row in rows}
                ),
            }
            if checkpoint_path is not None and not error:
                _write_daily_checkpoint(checkpoint_path, checkpoint)
        if checkpoint is None:
            raise RuntimeError("daily_collection_checkpoint_missing")
        date_responses[trade_day] = {
            "as_of": checkpoint["as_of"],
            "rows_returned": int(checkpoint["rows_returned"]),
            "unique_source_symbols": int(checkpoint["unique_source_symbols"]),
            "response_sha256": str(checkpoint["source_response_sha256"]),
            "batch_complete": bool(checkpoint["batch_complete"]),
            "truncated": bool(checkpoint["truncated"]),
            "error": str(checkpoint["error"] or ""),
            "missing_requested_symbols": checkpoint["missing_requested_symbols"],
        }
        if checkpoint.get("error"):
            failed_days.setdefault(trade_day, str(checkpoint["error"]))
        for row in checkpoint["factors"]:
            factors_by_symbol[row["symbol"]].append(dict(row))
        if index % 10 == 0 or index == len(expected_days):
            print(f"[{index}/{len(expected_days)}] {trade_day} rows={checkpoint['rows_returned']} "
                  f"{'ok' if checkpoint['batch_complete'] else 'FAILED'}", flush=True)
        if request_interval_sec > 0 and index < len(expected_days):
            time.sleep(float(request_interval_sec))

    factor_rows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []
    incomplete_symbols: dict[str, list[str]] = {}
    for symbol in requested_symbols:
        symbol_rows = sorted(factors_by_symbol[symbol], key=lambda row: row["trade_date"])
        returned_days = {row["trade_date"] for row in symbol_rows}
        missing_days = sorted(set(expected_days) - returned_days)
        complete = not missing_days and not failed_days
        frame = pd.DataFrame(symbol_rows, columns=list(FACTOR_COLUMNS))
        if not frame.empty:
            per_symbol_digest = hashlib.sha256(
                frame.loc[:, list(FACTOR_COLUMNS)].to_csv(
                    index=False, lineterminator="\n"
                ).encode("utf-8")
            ).hexdigest()
        else:
            per_symbol_digest = hashlib.sha256(
                ",".join(FACTOR_COLUMNS).encode("utf-8")
            ).hexdigest()
        latest_as_of = max(
            (str(item["as_of"]) for item in date_responses.values()), default=""
        )
        # This flag belongs to the observation, not the entire symbol/date range.
        # A verified row remains usable when another open session has no source row;
        # the missing session stays absent and the range-level coverage row below
        # remains incomplete.
        for row in symbol_rows:
            factor_rows.append({**row, "coverage_complete": True})
        if not complete:
            incomplete_symbols[symbol] = missing_days
        coverage_rows.append({
            "symbol": symbol,
            "ts_code": api_symbols[symbol],
            "source": SOURCE,
            "as_of": latest_as_of,
            "requested_sessions": len(expected_days),
            "rows_returned": len(symbol_rows),
            "missing_sessions": json.dumps(missing_days, separators=(",", ":")),
            "duplicate_dates": "[]",
            "out_of_range_dates": "[]",
            "coverage_complete": complete,
            "truncated": any(item["truncated"] for item in date_responses.values()),
            "error": ";".join(f"{day}:{error}" for day, error in sorted(failed_days.items())),
            "response_sha256": per_symbol_digest,
        })

    factors_frame = pd.DataFrame(factor_rows, columns=[
        "symbol", "ts_code", "trade_date", "adj_factor", "source", "as_of", "coverage_complete"
    ])
    coverage_frame = pd.DataFrame(coverage_rows, columns=COVERAGE_COLUMNS)
    as_ofs = sorted({str(item["as_of"]) for item in date_responses.values() if item["as_of"]})
    summary = {
        "source": SOURCE,
        "scope": "requested_symbols_only",
        "query_mode": "trade_date_market_batch",
        "requested_symbols": len(requested_symbols),
        "requested_sessions": len(expected_days),
        "trade_calendar": {
            "exchange": "SSE",
            "calendar_days": calendar_days,
            "open_sessions": len(expected_days),
            "normalized_sha256": calendar_sha256,
            "sessions": expected_days,
        },
        "completed_symbols": sum(bool(row["coverage_complete"]) for row in coverage_rows),
        "rows_total": len(factor_rows),
        "failed_days": failed_days,
        "incomplete_symbols": incomplete_symbols,
        "date_responses": date_responses,
        "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "response_as_of_range": {
            "first": as_ofs[0] if as_ofs else None,
            "last": as_ofs[-1] if as_ofs else None,
        },
        "checkpoint": {
            "enabled": checkpoint_dir is not None,
            "resumed_days": resumed_days,
            "queried_days": queried_days,
        },
        "coverage_complete": bool(requested_symbols)
        and not failed_days
        and not incomplete_symbols,
        "as_of": as_ofs[0] if len(as_ofs) == 1 else None,
    }
    return factors_frame, coverage_frame, summary


def collect(
    pro: Any,
    start: str,
    end: str,
    symbols: Iterable[str],
    *,
    request_interval_sec: float = 0.5,
    checkpoint_dir: Path | None = None,
    retry_incomplete: bool = False,
    query_mode: str = "symbol",
) -> tuple[Any, Any, dict[str, Any]]:
    import pandas as pd

    if retry_incomplete and checkpoint_dir is None:
        raise ValueError("retry_incomplete_requires_checkpoint")
    normalized_symbols: set[str] = set()
    for value in symbols:
        raw = str(value).strip().upper()
        if not raw:
            continue
        bare = raw.split(".", 1)[0]
        if not re.fullmatch(r"\d{1,6}", bare):
            raise ValueError("symbols must be numeric exchange codes")
        normalized_symbols.add(bare.zfill(6))
    requested_symbols = sorted(normalized_symbols)
    if not requested_symbols:
        raise ValueError("at least one symbol is required")
    if query_mode not in {"symbol", "trade_date_market_batch"}:
        raise ValueError("unsupported query_mode")
    start_day = datetime.strptime(start, "%Y%m%d").date()
    end_day = datetime.strptime(end, "%Y%m%d").date()
    if end_day < start_day:
        raise ValueError("end must not precede start")
    cal = _call(
        pro,
        "trade_cal",
        exchange="SSE",
        start_date=start,
        end_date=end,
    )
    if not {"cal_date", "is_open"}.issubset(set(cal.columns)):
        raise RuntimeError("calendar_missing_required_columns")
    calendar_state: dict[date, bool] = {}
    for row in cal.to_dict("records"):
        raw_day = str(row.get("cal_date") or "").strip()
        try:
            day = datetime.strptime(raw_day, "%Y%m%d").date()
        except ValueError as exc:
            raise RuntimeError("calendar_has_invalid_date") from exc
        value = row.get("is_open")
        raw_is_open = "" if value is None else str(value).strip().lower()
        if not start_day <= day <= end_day:
            raise RuntimeError("calendar_has_unrequested_date")
        if raw_is_open not in {"0", "1", "false", "true", "f", "t"}:
            raise RuntimeError("calendar_has_invalid_open_flag")
        if day in calendar_state:
            raise RuntimeError("calendar_has_duplicate_dates")
        calendar_state[day] = raw_is_open in {"1", "true", "t"}
    expected_calendar_days = {
        date.fromordinal(ordinal)
        for ordinal in range(start_day.toordinal(), end_day.toordinal() + 1)
    }
    if set(calendar_state) != expected_calendar_days:
        raise RuntimeError("calendar_has_missing_dates")
    parsed_calendar = sorted(day for day, is_open in calendar_state.items() if is_open)
    if not parsed_calendar:
        raise RuntimeError("calendar_empty")
    expected_days = sorted(day.strftime("%Y%m%d") for day in parsed_calendar)
    expected = set(expected_days)
    calendar_payload = json.dumps(
        sorted((day.isoformat(), is_open) for day, is_open in calendar_state.items()),
        separators=(",", ":"),
    ).encode("utf-8")
    calendar_sha256 = hashlib.sha256(calendar_payload).hexdigest()
    if checkpoint_dir is not None:
        checkpoint_dir = checkpoint_dir.expanduser().resolve()
        _initialize_checkpoint_scope(
            checkpoint_dir,
            start=start,
            end=end,
            symbols=requested_symbols,
            calendar_sha256=calendar_sha256,
            query_mode=query_mode,
        )
    if query_mode == "trade_date_market_batch":
        return _collect_by_trade_date(
            pro,
            start=start,
            end=end,
            requested_symbols=requested_symbols,
            expected_days=expected_days,
            calendar_days=len(calendar_state),
            calendar_sha256=calendar_sha256,
            request_interval_sec=request_interval_sec,
            checkpoint_dir=checkpoint_dir,
            retry_incomplete=retry_incomplete,
        )
    factor_rows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []
    failed: dict[str, str] = {}
    truncated_symbols: list[str] = []
    incomplete_symbols: dict[str, list[str]] = {}
    invalid_factor_values: dict[str, list[str]] = {}
    unexpected_source_symbols: dict[str, list[str]] = {}
    resumed_symbols = 0
    queried_symbols = 0
    for index, symbol in enumerate(requested_symbols, 1):
        error = ""
        frame = None
        response_as_of = ""
        api_symbol = _to_ts_code(symbol)
        checkpoint_path = (
            checkpoint_dir / "symbols" / f"{symbol}.json" if checkpoint_dir else None
        )
        if checkpoint_path is not None and checkpoint_path.exists():
            checkpoint = _read_symbol_checkpoint(
                checkpoint_path,
                symbol=symbol,
                api_symbol=api_symbol,
                start=start,
                end=end,
                calendar_sha256=calendar_sha256,
                requested_sessions=len(expected_days),
            )
            checkpoint_coverage = checkpoint["coverage"]
            if not retry_incomplete or bool(checkpoint_coverage["coverage_complete"]):
                factor_rows.extend(checkpoint["factors"])
                coverage_rows.append(checkpoint_coverage)
                resumed_symbols += 1
                if not checkpoint_coverage["coverage_complete"]:
                    incomplete_symbols[symbol] = json.loads(
                        checkpoint_coverage["missing_sessions"]
                    )
                if checkpoint_coverage["truncated"]:
                    truncated_symbols.append(symbol)
                invalid_days = checkpoint.get("invalid_factor_values", [])
                if invalid_days:
                    invalid_factor_values[symbol] = invalid_days
                unexpected = checkpoint.get("unexpected_source_symbols", [])
                if unexpected:
                    unexpected_source_symbols[symbol] = unexpected
                if index % 25 == 0 or index == len(requested_symbols):
                    print(
                        f"[{index}/{len(requested_symbols)}] {symbol} "
                        f"rows={checkpoint_coverage['rows_returned']} RESUMED",
                        flush=True,
                    )
                continue
        queried_symbols += 1
        symbol_factor_start = len(factor_rows)
        try:
            frame = _call(
                pro,
                "adj_factor",
                ts_code=api_symbol,
                start_date=start,
                end_date=end,
            )
            response_as_of = datetime.now().astimezone().isoformat(timespec="seconds")
            missing_columns = sorted(set(FACTOR_COLUMNS) - set(frame.columns))
            if missing_columns:
                raise RuntimeError(f"response_missing_columns:{','.join(missing_columns)}")
        except Exception as exc:  # noqa: BLE001 - no provider messages or token in reports
            response_as_of = datetime.now().astimezone().isoformat(timespec="seconds")
            error = type(exc).__name__ if not str(exc).startswith("response_missing") else str(exc)
            failed[symbol] = error
        count = int(len(frame)) if frame is not None else 0
        truncated = count >= ROW_CAP
        if truncated:
            truncated_symbols.append(symbol)
        digest = ""
        missing_days: list[str] = []
        duplicate_dates: list[str] = []
        out_of_range: list[str] = []
        invalid_values: list[str] = []
        unexpected_symbols: set[str] = set()
        if frame is not None and not error:
            normalized = frame.reindex(columns=FACTOR_COLUMNS).copy()
            normalized["trade_date"] = normalized["trade_date"].astype(str).str[:8]
            digest = hashlib.sha256(
                normalized.to_csv(index=False, lineterminator="\n").encode("utf-8")
            ).hexdigest()
            candidates: list[dict[str, Any]] = []
            returned_days: list[str] = []
            for item in normalized.to_dict("records"):
                source_symbol = str(item.get("ts_code") or "").strip().upper()
                if source_symbol != api_symbol:
                    unexpected_symbols.add(source_symbol or "<missing>")
                    continue
                trade_day = str(item.get("trade_date") or "").strip()
                try:
                    parsed_day = datetime.strptime(trade_day, "%Y%m%d").date()
                except ValueError:
                    invalid_values.append(trade_day or "<invalid-date>")
                    continue
                trade_day = parsed_day.strftime("%Y%m%d")
                if trade_day not in expected:
                    out_of_range.append(trade_day)
                    continue
                factor = pd.to_numeric(item.get("adj_factor"), errors="coerce")
                if pd.isna(factor) or not math.isfinite(float(factor)) or float(factor) <= 0:
                    invalid_values.append(trade_day)
                    continue
                item["trade_date"] = trade_day
                item["adj_factor"] = float(factor)
                candidates.append(item)
                returned_days.append(trade_day)
            duplicate_dates = sorted(day for day, n in Counter(returned_days).items() if n > 1)
            duplicate_set = set(duplicate_dates)
            valid_days = set(returned_days) - duplicate_set
            missing_days = sorted(expected - valid_days)
            invalid_factor_values[symbol] = sorted(set(invalid_values))
            if unexpected_symbols:
                unexpected_source_symbols[symbol] = sorted(unexpected_symbols)
            for item in candidates:
                if item["trade_date"] in duplicate_set:
                    continue
                factor_rows.append(
                    {
                        "symbol": symbol.split(".", 1)[0],
                        **item,
                        "source": SOURCE,
                        "as_of": response_as_of,
                        "coverage_complete": (
                            not truncated
                            and not out_of_range
                            and not duplicate_dates
                            and not invalid_values
                            and not unexpected_symbols
                        ),
                    }
                )
        complete = (
            not error
            and not truncated
            and not missing_days
            and not out_of_range
            and not duplicate_dates
            and not invalid_values
            and not unexpected_symbols
        )
        if not complete:
            incomplete_symbols[symbol] = missing_days
        coverage_row = {
                "symbol": symbol.split(".", 1)[0],
                "ts_code": api_symbol,
                "source": SOURCE,
                "as_of": response_as_of,
                "requested_sessions": len(expected_days),
                "rows_returned": count,
                "missing_sessions": json.dumps(missing_days, separators=(",", ":")),
                "duplicate_dates": json.dumps(duplicate_dates, separators=(",", ":")),
                "out_of_range_dates": json.dumps(out_of_range, separators=(",", ":")),
                "coverage_complete": complete,
                "truncated": truncated,
                "error": error,
                "response_sha256": digest,
            }
        coverage_rows.append(coverage_row)
        if checkpoint_path is not None and not error:
            _write_symbol_checkpoint(
                checkpoint_path,
                {
                    "schema": "tail_adj_factor_symbol_checkpoint.v1",
                    "symbol": symbol,
                    "ts_code": api_symbol,
                    "start": start,
                    "end": end,
                    "calendar_sha256": calendar_sha256,
                    "coverage": coverage_row,
                    "factors": factor_rows[symbol_factor_start:],
                    "invalid_factor_values": invalid_factor_values.get(symbol, []),
                    "unexpected_source_symbols": sorted(unexpected_symbols),
                },
            )
        state = (
            "FAILED" if error else "TRUNCATED" if truncated else "ok" if complete else "INCOMPLETE"
        )
        if index % 25 == 0 or index == len(requested_symbols):
            print(
                f"[{index}/{len(requested_symbols)}] {symbol} rows={count} {state}",
                flush=True,
            )
        if request_interval_sec > 0:
            time.sleep(float(request_interval_sec))
    response_as_ofs = sorted({str(row["as_of"]) for row in coverage_rows if row["as_of"]})
    summary = {
        "source": SOURCE,
        "scope": "requested_symbols_only",
        "requested_symbols": len(requested_symbols),
        "requested_sessions": len(expected_days),
        "trade_calendar": {
            "exchange": "SSE",
            "calendar_days": len(calendar_state),
            "open_sessions": len(expected_days),
            "normalized_sha256": calendar_sha256,
        },
        "completed_symbols": sum(bool(item["coverage_complete"]) for item in coverage_rows),
        "rows_total": len(factor_rows),
        "symbols_failed": failed,
        "symbols_at_or_above_row_cap": truncated_symbols,
        "incomplete_symbols": incomplete_symbols,
        "symbols_with_invalid_factor_values": {
            symbol: days for symbol, days in invalid_factor_values.items() if days
        },
        "symbols_with_unexpected_source_codes": unexpected_source_symbols,
        "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "response_as_of_range": {
            "first": response_as_ofs[0] if response_as_ofs else None,
            "last": response_as_ofs[-1] if response_as_ofs else None,
        },
        "checkpoint": {
            "enabled": checkpoint_dir is not None,
            "resumed_symbols": resumed_symbols,
            "queried_symbols": queried_symbols,
        },
        "coverage_complete": bool(requested_symbols)
        and not failed
        and not truncated_symbols
        and not incomplete_symbols,
        # Retain the legacy single-batch field only when every row shares it.
        # Resumed batches preserve each symbol's original response timestamp.
        "as_of": response_as_ofs[0] if len(response_as_ofs) == 1 else None,
    }
    return pd.DataFrame(factor_rows), pd.DataFrame(coverage_rows), summary


def _read_symbols(path: Path) -> list[str]:
    values = [
        line.strip().split(",", 1)[0] for line in path.read_text(encoding="utf-8").splitlines()
    ]
    return sorted(
        {value for value in values if value and value.lower() not in {"symbol", "ts_code"}}
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="YYYYMMDD")
    parser.add_argument("--end", required=True, help="YYYYMMDD")
    parser.add_argument("--symbols-file", required=True, help="one Tushare ts_code per line")
    parser.add_argument("--factors-out", required=True)
    parser.add_argument("--coverage-out", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--request-interval-sec", type=float, default=0.5)
    parser.add_argument(
        "--query-mode",
        choices=("symbol", "trade_date_market_batch"),
        default="symbol",
        help="query one symbol history at a time or fetch the full market per trade date",
    )
    parser.add_argument("--checkpoint-dir", type=Path, default=None)
    parser.add_argument(
        "--retry-incomplete",
        action="store_true",
        help="re-request symbols whose saved response was not coverage-complete",
    )
    args = parser.parse_args(argv)
    try:
        token = _token()
        import tushare as ts

        symbols = _read_symbols(Path(args.symbols_file))
        pro = ts.pro_api(token)
        output_paths = [
            Path(args.factors_out).expanduser().resolve(),
            Path(args.coverage_out).expanduser().resolve(),
            Path(args.report).expanduser().resolve(),
        ]
        if len(set(output_paths)) != len(output_paths):
            raise ValueError("output_paths_must_be_distinct")
        if any(path.exists() for path in output_paths):
            raise FileExistsError("output_exists; choose fresh output paths")
        for path in output_paths:
            path.parent.mkdir(parents=True, exist_ok=True)
        factors, coverage, summary = collect(
            pro,
            args.start,
            args.end,
            symbols,
            request_interval_sec=max(0.0, args.request_interval_sec),
            checkpoint_dir=args.checkpoint_dir,
            retry_incomplete=args.retry_incomplete,
            query_mode=args.query_mode,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed while omitting token/provider payload
        print(f"collection failed: {type(exc).__name__}")
        return 5
    factors.to_csv(output_paths[0], index=False)
    coverage.reindex(columns=COVERAGE_COLUMNS).to_csv(output_paths[1], index=False)
    with output_paths[2].open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["coverage_complete"] else 5


if __name__ == "__main__":
    raise SystemExit(main())
