#!/usr/bin/env python3
"""Collect hash-bound Tushare historical ST snapshots for a frozen decision cohort.

The endpoint is queried once per decision date. A successful, below-limit response is
the complete full-market ST list for that date; it is not a substitute for a full
historical name-change interval export.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

TOKEN_ENV = "SA__MARKET_WAREHOUSE__TUSHARE_TOKEN"
SOURCE = "tushare_stock_st_trade_date"
ROW_LIMIT = 1_000
ST_COLUMNS = ("ts_code", "name", "trade_date", "type", "type_name")
OUTPUT_COLUMNS = (
    "symbol", "ts_code", "trade_date", "name", "type", "type_name", "source",
    "as_of", "response_sha256",
)


class CollectionError(ValueError):
    """The frozen request scope or source response cannot be trusted."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    with tempfile.NamedTemporaryFile(
        mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
    ) as handle:
        temp = Path(handle.name)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def _digest_payload(payload: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "payload_sha256"}
    canonical = json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256_bytes(canonical)


def _normalize_code(value: Any) -> tuple[str, str]:
    code = str(value or "").strip().upper()
    match = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", code)
    if not match:
        raise CollectionError("invalid_or_missing_ts_code")
    return match.group(1), code


def _read_scope(requests_path: Path, calendar_path: Path) -> tuple[list[str], list[str], set[str]]:
    decision_days: set[str] = set()
    symbols: set[str] = set()
    with requests_path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CollectionError("request_file_contains_invalid_json") from exc
            if not isinstance(row, dict):
                raise CollectionError("request_row_is_not_an_object")
            try:
                day = datetime.strptime(str(row.get("decision_date") or ""), "%Y-%m-%d")
                symbol, _code = _normalize_code(row.get("ts_code") or f"{row.get('symbol', '')}.SZ")
            except (ValueError, CollectionError) as exc:
                raise CollectionError("request_row_missing_valid_date_or_symbol") from exc
            if day.strftime("%Y-%m-%d") != row.get("decision_date"):
                raise CollectionError("request_date_not_canonical")
            decision_days.add(day.strftime("%Y%m%d"))
            symbols.add(symbol)

    calendar = pd.read_csv(calendar_path, dtype={"trade_date": "string"}, keep_default_na=False)
    if not {"trade_date", "is_open"} <= set(calendar.columns):
        raise CollectionError("calendar_missing_required_columns")
    if calendar["trade_date"].duplicated().any():
        raise CollectionError("calendar_contains_duplicate_dates")
    open_days: set[str] = set()
    for row in calendar.to_dict("records"):
        text = str(row.get("trade_date") or "").strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            text = text.replace("-", "")
        if not re.fullmatch(r"\d{8}", text):
            raise CollectionError("calendar_contains_invalid_date")
        try:
            datetime.strptime(text, "%Y%m%d")
        except ValueError as exc:
            raise CollectionError("calendar_contains_invalid_date") from exc
        raw_flag = row.get("is_open")
        flag = "" if raw_flag is None else str(raw_flag).strip().lower()
        if flag not in {"0", "1", "false", "true", "f", "t"}:
            raise CollectionError("calendar_contains_invalid_open_flag")
        if flag in {"1", "true", "t"}:
            open_days.add(text)
    if not decision_days or not symbols:
        raise CollectionError("empty_request_scope")
    if not decision_days <= open_days:
        raise CollectionError("decision_date_missing_from_open_calendar")
    return sorted(decision_days), sorted(symbols), open_days


def _load_checkpoint(path: Path, *, day: str, scope_sha256: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectionError("checkpoint_unreadable") from exc
    if payload.get("payload_sha256") != _digest_payload(payload):
        raise CollectionError("checkpoint_hash_mismatch")
    if payload.get("schema") != "tail_st_snapshot_checkpoint.v1":
        raise CollectionError("checkpoint_schema_mismatch")
    if payload.get("trade_date") != day or payload.get("scope_sha256") != scope_sha256:
        raise CollectionError("checkpoint_scope_mismatch")
    if not isinstance(payload.get("rows"), list):
        raise CollectionError("checkpoint_rows_invalid")
    return payload


def _write_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    enriched = dict(payload)
    enriched["payload_sha256"] = _digest_payload(enriched)
    _atomic_json(path, enriched)


def _call_stock_st(pro: Any, day: str, *, retries: int = 3) -> Any:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return pro.stock_st(trade_date=day)
        except Exception as exc:  # noqa: BLE001 - never include provider payloads or secrets
            last_error = exc
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
    raise CollectionError(
        f"stock_st_failed:{type(last_error).__name__ if last_error else 'unknown'}"
    )


def collect(
    pro: Any,
    *,
    requests_path: Path,
    calendar_path: Path,
    out_dir: Path,
    request_interval_sec: float = 1.25,
    checkpoint_dir: Path | None = None,
    retry_incomplete: bool = False,
) -> dict[str, Any]:
    if out_dir.exists():
        raise FileExistsError("output_directory_exists")
    decision_days, symbols, open_days = _read_scope(requests_path, calendar_path)
    requests_sha256 = _sha256_file(requests_path)
    calendar_sha256 = _sha256_file(calendar_path)
    collector_sha256 = _sha256_file(Path(__file__).resolve())
    scope = {
        "schema": "tail_st_snapshot_scope.v1",
        "collector_sha256": collector_sha256,
        "requests_sha256": requests_sha256,
        "calendar_sha256": calendar_sha256,
        "decision_days": decision_days,
        "symbols_sha256": _sha256_bytes("\n".join(symbols).encode("utf-8")),
        "symbols": len(symbols),
        "source": SOURCE,
    }
    scope_sha256 = _sha256_bytes(
        json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )
    if checkpoint_dir is not None:
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        scope_path = checkpoint_dir / "scope.json"
        if scope_path.exists():
            try:
                prior = json.loads(scope_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CollectionError("checkpoint_scope_unreadable") from exc
            if prior != {**scope, "scope_sha256": scope_sha256}:
                raise CollectionError("checkpoint_scope_mismatch")
        elif any(checkpoint_dir.iterdir()):
            raise CollectionError("checkpoint_scope_missing")
        else:
            _atomic_json(scope_path, {**scope, "scope_sha256": scope_sha256})

    output_dir = out_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    checkpoint_rows: dict[str, dict[str, Any]] = {}
    response_errors: dict[str, str] = {}
    resumed_days = 0
    queried_days = 0
    for index, day in enumerate(decision_days, 1):
        checkpoint_path = checkpoint_dir / f"{day}.json" if checkpoint_dir else None
        saved: dict[str, Any] | None = None
        if checkpoint_path is not None and checkpoint_path.exists():
            saved = _load_checkpoint(checkpoint_path, day=day, scope_sha256=scope_sha256)
            if not retry_incomplete or saved.get("complete") is True:
                resumed_days += 1
            else:
                saved = None
        if saved is None:
            queried_days += 1
            as_of = datetime.now().astimezone().isoformat(timespec="seconds")
            normalized_rows: list[dict[str, Any]] = []
            digest = ""
            error = ""
            row_count = 0
            try:
                frame = _call_stock_st(pro, day)
                as_of = datetime.now().astimezone().isoformat(timespec="seconds")
                if not set(ST_COLUMNS) <= set(frame.columns):
                    raise CollectionError("response_missing_columns")
                source = frame.reindex(columns=ST_COLUMNS).copy()
                row_count = len(source)
                digest = _sha256_bytes(
                    source.to_csv(index=False, lineterminator="\n").encode("utf-8")
                )
                if row_count >= ROW_LIMIT:
                    raise CollectionError("provider_row_limit_reached")
                seen: set[str] = set()
                for raw in source.to_dict("records"):
                    symbol, ts_code = _normalize_code(raw.get("ts_code"))
                    row_day = str(raw.get("trade_date") or "").strip()
                    if row_day != day:
                        raise CollectionError("response_contains_wrong_trade_date")
                    if symbol in seen:
                        raise CollectionError("response_contains_duplicate_symbol")
                    seen.add(symbol)
                    normalized_rows.append({
                        "symbol": symbol,
                        "ts_code": ts_code,
                        "trade_date": day,
                        "name": str(raw.get("name") or "") or None,
                        "type": str(raw.get("type") or "") or None,
                        "type_name": str(raw.get("type_name") or "") or None,
                        "source": SOURCE,
                        "as_of": as_of,
                        "response_sha256": digest,
                    })
            except Exception as exc:  # noqa: BLE001 - keep provider exception text out of evidence
                error = str(exc) if isinstance(exc, CollectionError) else type(exc).__name__
                response_errors[day] = error
                normalized_rows = []
            saved = {
                "schema": "tail_st_snapshot_checkpoint.v1",
                "scope_sha256": scope_sha256,
                "trade_date": day,
                "as_of": as_of,
                "rows_returned": row_count,
                "response_sha256": digest,
                "complete": not error,
                "error": error,
                "rows": normalized_rows,
            }
            if checkpoint_path is not None and not error:
                _write_checkpoint(checkpoint_path, saved)
        if saved.get("error"):
            response_errors.setdefault(day, str(saved["error"]))
        checkpoint_rows[day] = saved
        if index % 10 == 0 or index == len(decision_days):
            print(
                f"[{index}/{len(decision_days)}] {day} rows={saved['rows_returned']} "
                f"{'ok' if saved['complete'] else 'FAILED'}",
                flush=True,
            )
        if request_interval_sec > 0 and index < len(decision_days):
            time.sleep(float(request_interval_sec))

    all_rows = [row for day in decision_days for row in checkpoint_rows[day]["rows"]]
    output_frame = pd.DataFrame(all_rows, columns=OUTPUT_COLUMNS)
    rows_path = output_dir / "st_snapshots.csv"
    output_frame.to_csv(rows_path, index=False)
    date_responses = {
        day: {
            "as_of": checkpoint_rows[day]["as_of"],
            "rows_returned": int(checkpoint_rows[day]["rows_returned"]),
            "response_sha256": str(checkpoint_rows[day]["response_sha256"]),
            "complete": bool(checkpoint_rows[day]["complete"]),
            "error": str(checkpoint_rows[day]["error"] or ""),
        }
        for day in decision_days
    }
    report = {
        "schema": "tail_st_snapshot_collection.v1",
        "source": SOURCE,
        "requests_sha256": requests_sha256,
        "calendar_sha256": calendar_sha256,
        "collector_sha256": collector_sha256,
        "decision_days": decision_days,
        "open_calendar_days": len(open_days),
        "requested_symbols": len(symbols),
        "st_rows": len(output_frame),
        "complete_days": sum(item["complete"] for item in date_responses.values()),
        "failed_days": response_errors,
        "coverage_complete": not response_errors
        and all(item["complete"] for item in date_responses.values()),
        "date_responses": date_responses,
        "output_sha256": _sha256_file(rows_path),
        "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "checkpoint": {"enabled": checkpoint_dir is not None,
                       "resumed_days": resumed_days, "queried_days": queried_days},
    }
    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "scope.json").write_text(
        json.dumps({**scope, "scope_sha256": scope_sha256}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=Path, required=True)
    parser.add_argument("--trading-calendar", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--checkpoint-dir", type=Path, default=None)
    parser.add_argument("--request-interval-sec", type=float, default=1.25)
    parser.add_argument("--retry-incomplete", action="store_true")
    args = parser.parse_args(argv)
    token = str(os.environ.get(TOKEN_ENV, "") or "").strip()
    if not token:
        print("collection failed: token_missing", file=sys.stderr)
        return 5
    try:
        import tushare as ts

        report = collect(
            ts.pro_api(token),
            requests_path=args.requests,
            calendar_path=args.trading_calendar,
            out_dir=args.out_dir,
            request_interval_sec=max(0.0, args.request_interval_sec),
            checkpoint_dir=args.checkpoint_dir,
            retry_incomplete=args.retry_incomplete,
        )
    except Exception as exc:  # noqa: BLE001 - exception values may contain private provider data
        print(f"collection failed: {type(exc).__name__}", file=sys.stderr)
        return 5
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["coverage_complete"] else 5


if __name__ == "__main__":
    raise SystemExit(main())
