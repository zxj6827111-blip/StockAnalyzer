#!/usr/bin/env python3
"""Collect RAW daily bars, exact limits, and explicit suspension rows for a request set.

The collector writes CSV evidence only. It never opens a warehouse or research database.
Only a positive-volume RAW daily row proves trading; only an explicit ``suspend_d`` row
proves suspension. Dates with neither source remain unresolved in the report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.contracts.trend_strategy import DEFAULT_TREND_CONTRACT  # noqa: E402
from stock_analyzer.data.tushare_provider import _to_ts_code  # noqa: E402
from stock_analyzer.research.tail_rebuild import exit_window_end  # noqa: E402

TOKEN_ENV = "SA__MARKET_WAREHOUSE__TUSHARE_TOKEN"
ROW_CAP = 10_000
DAILY_SOURCE = "tushare_daily_unadjusted"
LIMIT_SOURCE = "tushare_stk_limit"
TRADED_STATUS_SOURCE = "tushare_daily_positive_volume"
SUSPEND_STATUS_SOURCE = "tushare_suspend_d"

DAILY_COLUMNS = (
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "source",
    "as_of",
)
LIMIT_COLUMNS = (
    "symbol",
    "trade_date",
    "up_limit",
    "down_limit",
    "approximated",
    "coverage_complete",
    "source",
    "as_of",
)
STATUS_COLUMNS = (
    "symbol",
    "trade_date",
    "suspended",
    "suspend_type",
    "trade_status",
    "suspend_events_json",
    "coverage_complete",
    "source",
    "as_of",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _day(value: Any) -> date | None:
    text = str(value or "").strip()[:10]
    if len(text) == 8 and text.isdigit():
        try:
            return datetime.strptime(text, "%Y%m%d").date()
        except ValueError:
            return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and math.isnan(float(value)):
        return None
    text = str(value).strip()
    return None if text.lower() in {"", "nan", "none", "nat", "<na>"} else text


def read_request_windows(path: Path) -> tuple[dict[str, tuple[date, date]], str]:
    """Return each requested symbol's first entry through last possible exit session."""
    windows: dict[str, tuple[date, date]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            raw_symbol = str(payload["symbol"]).strip().upper()
            symbol = raw_symbol.split(".", 1)[0]
            if not re.fullmatch(r"\d{1,6}", symbol):
                raise ValueError("symbol must be a numeric exchange code")
            symbol = symbol.zfill(6)
            entry = date.fromisoformat(str(payload["entry_date"])[:10])
            end = exit_window_end(entry, DEFAULT_TREND_CONTRACT)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid request at line {line_number}: {type(exc).__name__}"
            ) from exc
        previous = windows.get(symbol)
        windows[symbol] = (
            min(previous[0], entry) if previous else entry,
            max(previous[1], end) if previous else end,
        )
    if not windows:
        raise ValueError("request file has no valid rows")
    return windows, _sha256(path)


def read_explicit_windows(path: Path) -> tuple[dict[str, tuple[date, date]], str]:
    """Read locally precomputed date windows without interpreting a strategy contract."""
    windows: dict[str, tuple[date, date]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            raw_symbol = str(payload["symbol"]).strip().upper()
            symbol = raw_symbol.split(".", 1)[0]
            if not re.fullmatch(r"\d{1,6}", symbol):
                raise ValueError("symbol must be a numeric exchange code")
            symbol = symbol.zfill(6)
            start = date.fromisoformat(str(payload["start"])[:10])
            end = date.fromisoformat(str(payload["end"])[:10])
            if end < start:
                raise ValueError("end precedes start")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid explicit window at line {line_number}: {type(exc).__name__}"
            ) from exc
        previous = windows.get(symbol)
        windows[symbol] = (
            min(previous[0], start) if previous else start,
            max(previous[1], end) if previous else end,
        )
    if not windows:
        raise ValueError("window file has no valid rows")
    return windows, _sha256(path)


def _api_call(pro: Any, method: str, kwargs: dict[str, Any], *, retries: int, delay: float) -> Any:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            value = getattr(pro, method)(**kwargs)
            time.sleep(delay)
            return value
        except Exception as exc:  # noqa: BLE001 - never expose provider payload or credentials
            last = exc
            if attempt < retries:
                time.sleep(delay * (attempt + 1))
    raise RuntimeError(type(last).__name__ if last else "UnknownError")


def _frame_records(frame: Any) -> list[dict[str, Any]]:
    if hasattr(frame, "to_dict"):
        return list(frame.to_dict("records"))
    raise TypeError("provider_response_not_a_dataframe")


def _clean_api_rows(
    frame: Any,
    *,
    required: tuple[str, ...],
    date_column: str,
    expected: set[date],
    expected_symbol: str,
    allow_duplicate_dates: bool,
    row_cap: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records = _frame_records(frame)
    columns = set(getattr(frame, "columns", []))
    if not set(required).issubset(columns):
        return [], {
            "error": "response_missing_columns",
            "missing_columns": sorted(set(required) - columns),
            "rows_returned": len(records),
            "truncated": False,
            "duplicate_dates": [],
            "out_of_range_dates": [],
            "invalid_dates": 0,
            "unexpected_symbols": [],
        }
    truncated = len(records) >= row_cap
    candidates: list[tuple[date, dict[str, Any]]] = []
    out_of_range: list[str] = []
    unexpected_symbols: set[str] = set()
    invalid_dates = 0
    for row in records:
        source_symbol = str(row.get("ts_code") or "").strip().upper()
        if source_symbol != expected_symbol:
            unexpected_symbols.add(source_symbol or "<missing>")
            continue
        parsed = _day(row.get(date_column))
        if parsed is None:
            invalid_dates += 1
            continue
        if parsed not in expected:
            out_of_range.append(parsed.isoformat())
            continue
        candidates.append((parsed, row))
    counts = Counter(day for day, _ in candidates)
    duplicates = sorted(day.isoformat() for day, count in counts.items() if count > 1)
    duplicate_set = {date.fromisoformat(item) for item in duplicates}
    clean = (
        [row for _day_value, row in candidates]
        if allow_duplicate_dates
        else [row for day, row in candidates if day not in duplicate_set]
    )
    return clean, {
        "error": "invalid_response_date" if invalid_dates else "",
        "rows_returned": len(records),
        "truncated": truncated,
        "duplicate_dates": duplicates,
        "out_of_range_dates": sorted(set(out_of_range)),
        "invalid_dates": invalid_dates,
        "unexpected_symbols": sorted(unexpected_symbols),
    }


def _clean_market_daily_rows(
    frame: Any,
    *,
    day: date,
    expected_symbols: set[str],
    row_cap: int,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Filter one market-wide daily response down to the requested symbols."""
    records = _frame_records(frame)
    required = {"ts_code", "trade_date", "open", "high", "low", "close", "vol"}
    columns = set(getattr(frame, "columns", []))
    if not required.issubset(columns):
        return {}, {
            "error": "response_missing_columns",
            "missing_columns": sorted(required - columns),
            "rows_returned": len(records),
            "truncated": False,
            "duplicate_dates": [],
            "out_of_range_dates": [],
            "invalid_dates": 0,
        }

    candidates: dict[str, list[dict[str, Any]]] = {}
    invalid_dates = 0
    out_of_range: set[str] = set()
    for row in records:
        source_symbol = str(row.get("ts_code") or "").strip().upper()
        if source_symbol not in expected_symbols:
            continue
        parsed = _day(row.get("trade_date"))
        if parsed is None:
            invalid_dates += 1
            continue
        if parsed != day:
            out_of_range.add(parsed.isoformat())
            continue
        candidates.setdefault(source_symbol, []).append(row)

    duplicates = sorted(
        symbol
        for symbol, rows in candidates.items()
        if len(rows) > 1
    )
    return candidates, {
        "error": "invalid_response_date" if invalid_dates else "",
        "rows_returned": len(records),
        "truncated": len(records) >= row_cap,
        "duplicate_dates": duplicates,
        "out_of_range_dates": sorted(out_of_range),
        "invalid_dates": invalid_dates,
    }


def collect(
    pro: Any,
    windows: dict[str, tuple[date, date]],
    *,
    retries: int = 3,
    delay: float = 0.2,
    row_cap: int = ROW_CAP,
    progress: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    symbols = sorted(windows)
    start = min(window[0] for window in windows.values()).strftime("%Y%m%d")
    end = max(window[1] for window in windows.values()).strftime("%Y%m%d")
    calendar = _api_call(
        pro,
        "trade_cal",
        {"exchange": "SSE", "start_date": start, "end_date": end},
        retries=retries,
        delay=delay,
    )
    calendar_rows = _frame_records(calendar)
    if not calendar_rows or not {"cal_date", "is_open"}.issubset(set(calendar.columns)):
        raise RuntimeError("calendar_empty_or_malformed")
    requested_start = datetime.strptime(start, "%Y%m%d").date()
    requested_end = datetime.strptime(end, "%Y%m%d").date()
    calendar_state: dict[date, bool] = {}
    calendar_invalid = False
    for row in calendar_rows:
        day = _day(row.get("cal_date"))
        raw_is_open = str(row.get("is_open", "")).strip().lower()
        if day is None or not requested_start <= day <= requested_end or raw_is_open not in {
            "0", "1", "false", "true", "f", "t",
        }:
            calendar_invalid = True
        else:
            if day in calendar_state:
                calendar_invalid = True
            calendar_state[day] = raw_is_open in {"1", "true", "t"}
    expected_calendar_days = {
        requested_start.fromordinal(ordinal)
        for ordinal in range(requested_start.toordinal(), requested_end.toordinal() + 1)
    }
    if set(calendar_state) != expected_calendar_days:
        calendar_invalid = True
    if calendar_invalid or not any(calendar_state.values()):
        raise RuntimeError("calendar_empty_or_malformed")
    open_days = sorted(day for day, is_open in calendar_state.items() if is_open)
    calendar_payload = json.dumps(
        sorted((day.isoformat(), is_open) for day, is_open in calendar_state.items()),
        separators=(",", ":"),
    ).encode("utf-8")
    calendar_sha256 = hashlib.sha256(calendar_payload).hexdigest()
    as_of = datetime.now().astimezone().isoformat(timespec="seconds")
    daily_rows: list[dict[str, Any]] = []
    limit_rows: list[dict[str, Any]] = []
    status_rows: list[dict[str, Any]] = []
    symbols_report: list[dict[str, Any]] = []

    symbol_codes = {
        symbol: symbol if symbol.endswith((".SH", ".SZ", ".BJ")) else _to_ts_code(symbol)
        for symbol in symbols
    }
    expected_symbols_by_day: dict[date, set[str]] = {}
    for symbol, (symbol_start, symbol_end) in windows.items():
        ts_code = symbol_codes[symbol]
        for day in open_days:
            if symbol_start <= day <= symbol_end:
                expected_symbols_by_day.setdefault(day, set()).add(ts_code)

    daily_source_rows: dict[str, dict[date, dict[str, Any]]] = {
        ts_code: {} for ts_code in symbol_codes.values()
    }
    daily_source_status: dict[str, dict[str, Any]] = {
        ts_code: {
            "error": "",
            "rows_returned": 0,
            "truncated": False,
            "duplicate_dates": [],
            "out_of_range_dates": [],
            "invalid_dates": 0,
            "unexpected_symbols": [],
        }
        for ts_code in symbol_codes.values()
    }
    daily_query_count = 0
    for day, expected_codes in sorted(expected_symbols_by_day.items()):
        daily_query_count += 1
        try:
            frame = _api_call(
                pro,
                "daily",
                {"trade_date": day.strftime("%Y%m%d")},
                retries=retries,
                delay=delay,
            )
            by_code, details = _clean_market_daily_rows(
                frame,
                day=day,
                expected_symbols=expected_codes,
                row_cap=row_cap,
            )
        except Exception as exc:  # noqa: BLE001 - source error class only
            by_code = {}
            details = {
                "error": type(exc).__name__,
                "rows_returned": 0,
                "truncated": False,
                "duplicate_dates": [],
                "out_of_range_dates": [],
                "invalid_dates": 0,
            }

        for ts_code in expected_codes:
            status = daily_source_status[ts_code]
            if details["error"] and not status["error"]:
                status["error"] = details["error"]
            status["truncated"] = bool(status["truncated"] or details["truncated"])
            status["invalid_dates"] += int(details["invalid_dates"])
            if details["out_of_range_dates"]:
                status["out_of_range_dates"].extend(details["out_of_range_dates"])
            if ts_code in details["duplicate_dates"]:
                status["duplicate_dates"].append(day.isoformat())
            rows = by_code.get(ts_code, [])
            status["rows_returned"] += len(rows)
            if len(rows) == 1:
                daily_source_rows[ts_code][day] = rows[0]

    for status in daily_source_status.values():
        status["duplicate_dates"] = sorted(set(status["duplicate_dates"]))
        status["out_of_range_dates"] = sorted(set(status["out_of_range_dates"]))

    for index, symbol in enumerate(symbols, 1):
        symbol_start, symbol_end = windows[symbol]
        expected = {day for day in open_days if symbol_start <= day <= symbol_end}
        ts_code = symbol_codes[symbol]
        params = {
            "ts_code": ts_code,
            "start_date": symbol_start.strftime("%Y%m%d"),
            "end_date": symbol_end.strftime("%Y%m%d"),
        }
        api_status: dict[str, Any] = {}
        normalized: dict[str, list[dict[str, Any]]] = {}
        api_status["daily"] = daily_source_status[ts_code]
        normalized["daily"] = [
            raw for day, raw in daily_source_rows[ts_code].items() if day in expected
        ]
        api_specs = (
            (
                "stk_limit",
                ("ts_code", "trade_date", "up_limit", "down_limit"),
                "trade_date",
                False,
            ),
            (
                "suspend_d",
                ("ts_code", "trade_date", "suspend_type", "suspend_timing"),
                "trade_date",
                True,
            ),
        )
        for method, required, date_column, allow_duplicates in api_specs:
            try:
                frame = _api_call(pro, method, params, retries=retries, delay=delay)
                rows, details = _clean_api_rows(
                    frame,
                    required=required,
                    date_column=date_column,
                    expected=expected,
                    expected_symbol=ts_code,
                    allow_duplicate_dates=allow_duplicates,
                    row_cap=row_cap,
                )
                api_status[method] = details
                normalized[method] = rows
            except Exception as exc:  # noqa: BLE001 - keep the source error class only
                api_status[method] = {
                    "error": type(exc).__name__,
                    "rows_returned": 0,
                    "truncated": False,
                    "duplicate_dates": [],
                    "out_of_range_dates": [],
                }
                normalized[method] = []

        daily_by_day: dict[date, dict[str, Any]] = {}
        for raw in normalized.get("daily", []):
            day = _day(raw.get("trade_date"))
            open_, high, low, close, volume = (
                _number(raw.get(name)) for name in ("open", "high", "low", "close", "vol")
            )
            if (
                day is None
                or None in (open_, high, low, close, volume)
                or min(open_, high, low, close) <= 0
                or volume < 0
                or high < max(open_, close)
                or low > min(open_, close)
                or high < low
            ):
                continue
            daily_by_day[day] = {
                "symbol": symbol,
                "date": day.isoformat(),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "source": DAILY_SOURCE,
                "as_of": as_of,
            }
        daily_rows.extend(daily_by_day.values())

        limits_by_day: dict[date, dict[str, Any]] = {}
        for raw in normalized.get("stk_limit", []):
            day = _day(raw.get("trade_date"))
            up, down = _number(raw.get("up_limit")), _number(raw.get("down_limit"))
            if day is None:
                continue
            complete = up is not None and down is not None and up > 0 and down > 0
            limits_by_day[day] = {
                "symbol": symbol,
                "trade_date": day.isoformat(),
                "up_limit": up,
                "down_limit": down,
                "approximated": False,
                "coverage_complete": complete,
                "source": LIMIT_SOURCE,
                "as_of": as_of,
            }
        limit_rows.extend(limits_by_day.values())

        suspend_events_by_day: dict[date, list[dict[str, str | None]]] = {}
        invalid_suspend_events: list[str] = []
        for raw in normalized.get("suspend_d", []):
            day = _day(raw.get("trade_date"))
            stype = str(raw.get("suspend_type") or "").strip().upper()
            timing = _optional_text(raw.get("suspend_timing"))
            if day is None or stype not in {"S", "R"}:
                invalid_suspend_events.append(day.isoformat() if day else "<invalid-date>")
                continue
            suspend_events_by_day.setdefault(day, []).append({
                "suspend_type": stype,
                "suspend_timing": timing,
            })

        traded_days = {day for day, raw in daily_by_day.items() if raw["volume"] > 0}
        suspended_days: set[date] = set()
        conflicts_set: set[date] = set()
        known_traded_days: set[date] = set()
        for day in expected:
            events = suspend_events_by_day.get(day, [])
            event_types = {str(item["suspend_type"]) for item in events}
            full_suspension = any(
                item["suspend_type"] == "S" and item["suspend_timing"] is None
                for item in events
            )
            intraday_suspension = any(
                item["suspend_type"] == "S" and item["suspend_timing"] is not None
                for item in events
            )
            if day in traded_days:
                if full_suspension:
                    conflicts_set.add(day)
                else:
                    # A positive-volume RAW daily bar proves trading, including a day
                    # with an explicitly time-bounded intraday suspension.
                    known_traded_days.add(day)
            elif full_suspension and event_types == {"S"} and not intraday_suspension:
                suspended_days.add(day)
        conflicts = sorted(day.isoformat() for day in conflicts_set)
        known_days = (known_traded_days | suspended_days) - conflicts_set
        unknown_days = sorted(day.isoformat() for day in expected - known_days)
        trading_without_limits = sorted(
            day.isoformat()
            for day in known_traded_days
            if day not in limits_by_day or not limits_by_day[day]["coverage_complete"]
        )
        for day in sorted(known_days):
            is_suspended = day in suspended_days
            events = suspend_events_by_day.get(day, [])
            status_rows.append(
                {
                    "symbol": symbol,
                    "trade_date": day.isoformat(),
                    "suspended": is_suspended,
                    "suspend_type": "S" if is_suspended else None,
                    "trade_status": "suspended" if is_suspended else "trading",
                    "suspend_events_json": json.dumps(
                        events, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ),
                    "coverage_complete": True,
                    "source": SUSPEND_STATUS_SOURCE if is_suspended else TRADED_STATUS_SOURCE,
                    "as_of": as_of,
                }
            )

        endpoint_errors = {
            name: detail.get("error") for name, detail in api_status.items() if detail.get("error")
        }
        endpoint_anomalies = {
            name: {
                field: detail[field]
                for field in ("duplicate_dates", "out_of_range_dates")
                if detail.get(field) and (field != "duplicate_dates" or name != "suspend_d")
            }
            for name, detail in api_status.items()
            if (
                detail.get("out_of_range_dates")
                or (name != "suspend_d" and detail.get("duplicate_dates"))
            )
        }
        truncated = [name for name, detail in api_status.items() if detail.get("truncated")]
        invalid_response_dates = {
            name: detail.get("invalid_dates", 0)
            for name, detail in api_status.items()
            if detail.get("invalid_dates", 0)
        }
        symbol_complete = (
            not unknown_days
            and not conflicts
            and not trading_without_limits
            and not endpoint_errors
            and not endpoint_anomalies
            and not invalid_response_dates
            and not invalid_suspend_events
            and not truncated
            and not any(detail.get("unexpected_symbols") for detail in api_status.values())
        )
        symbols_report.append(
            {
                "symbol": symbol,
                "ts_code": ts_code,
                "start": symbol_start.isoformat(),
                "end": symbol_end.isoformat(),
                "expected_open_sessions": len(expected),
                "daily_rows": len(daily_by_day),
                "positive_volume_sessions": len(traded_days),
                "suspended_sessions": len(suspended_days),
                "resume_event_sessions": sum(
                    any(event["suspend_type"] == "R" for event in events)
                    for events in suspend_events_by_day.values()
                ),
                "suspend_events": [
                    {"trade_date": day.isoformat(), **event}
                    for day in sorted(suspend_events_by_day)
                    for event in suspend_events_by_day[day]
                ],
                "invalid_suspend_events": invalid_suspend_events,
                "unknown_status_days": unknown_days,
                "trading_sessions_without_exact_limits": trading_without_limits,
                "status_conflict_days": conflicts,
                "endpoint_errors": endpoint_errors,
                "endpoint_anomalies": endpoint_anomalies,
                "invalid_response_dates": invalid_response_dates,
                "unexpected_source_symbols": {
                    name: detail["unexpected_symbols"]
                    for name, detail in api_status.items()
                    if detail.get("unexpected_symbols")
                },
                "truncated_endpoints": truncated,
                "api": api_status,
                "coverage_complete": symbol_complete,
            }
        )
        if progress and (index % 25 == 0 or index == len(symbols)):
            state = "ok" if symbol_complete else "INCOMPLETE"
            print(f"[{index}/{len(symbols)}] {symbol} {state}", file=sys.stderr, flush=True)

    report = {
        "schema": "tail_trade_reference_collection.v2",
        "scope": "requested_symbols_only",
        "as_of": as_of,
        "global_start": start,
        "global_end": end,
        "open_sessions": len(open_days),
        "trade_calendar": {
            "exchange": "SSE",
            "calendar_days": len(calendar_state),
            "open_sessions": len(open_days),
            "normalized_sha256": calendar_sha256,
        },
        "requested_symbols": len(symbols),
        "daily_query_mode": "market_wide_by_trade_date",
        "daily_date_queries": daily_query_count,
        "daily_rows": len(daily_rows),
        "exact_limit_rows": len(limit_rows),
        "known_trade_status_rows": len(status_rows),
        "symbols_complete": sum(bool(item["coverage_complete"]) for item in symbols_report),
        "coverage_complete": bool(symbols_report)
        and all(bool(item["coverage_complete"]) for item in symbols_report),
        "symbols": symbols_report,
        "sources": {
            "daily_bars": DAILY_SOURCE,
            "limit_prices": LIMIT_SOURCE,
            "trade_status": [TRADED_STATUS_SOURCE, SUSPEND_STATUS_SOURCE],
        },
    }
    return daily_rows, limit_rows, status_rows, report


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    import pandas as pd

    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False)


def main(argv: list[str] | None = None) -> int:
    import tushare as ts

    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument(
        "--requests", help="request JSONL; locally derives each symbol's exit window"
    )
    scope.add_argument(
        "--windows-file",
        help="explicit symbol/start/end JSONL; dates are not reinterpreted by this process",
    )
    parser.add_argument("--daily-out", required=True)
    parser.add_argument("--limits-out", required=True)
    parser.add_argument("--status-out", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--request-interval-sec", type=float, default=0.2)
    parser.add_argument("--retries", type=int, default=3)
    args = parser.parse_args(argv)
    scope_path = Path(args.requests or args.windows_file).expanduser()
    try:
        windows, scope_sha = (
            read_request_windows(scope_path) if args.requests else read_explicit_windows(scope_path)
        )
        token = str(os.environ.get(TOKEN_ENV, "") or "").strip()
        if not token:
            raise RuntimeError(f"missing environment variable {TOKEN_ENV}")
        daily, limits, statuses, report = collect(
            ts.pro_api(token),
            windows,
            retries=max(0, args.retries),
            delay=max(0.0, args.request_interval_sec),
        )
    except Exception as exc:  # noqa: BLE001 - no token/provider message in logs
        print(f"trade reference collection failed: {type(exc).__name__}", file=sys.stderr)
        return 5

    targets = {
        "daily_bars": (Path(args.daily_out).expanduser(), DAILY_COLUMNS, daily),
        "limit_prices": (Path(args.limits_out).expanduser(), LIMIT_COLUMNS, limits),
        "trade_status": (Path(args.status_out).expanduser(), STATUS_COLUMNS, statuses),
    }
    for _name, (path, columns, rows) in targets.items():
        _write_csv(path, columns, rows)
    report["scope_input"] = "request_jsonl" if args.requests else "explicit_symbol_windows"
    report["scope_input_path"] = str(scope_path.resolve())
    report["scope_input_sha256"] = scope_sha
    report["outputs"] = {
        name: {"path": str(path.resolve()), "rows": len(rows), "sha256": _sha256(path)}
        for name, (path, _columns, rows) in targets.items()
    }
    report_path = Path(args.report).expanduser()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "schema",
                    "requested_symbols",
                    "daily_rows",
                    "exact_limit_rows",
                    "known_trade_status_rows",
                    "symbols_complete",
                    "coverage_complete",
                    "outputs",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["coverage_complete"] else 5


if __name__ == "__main__":
    raise SystemExit(main())
