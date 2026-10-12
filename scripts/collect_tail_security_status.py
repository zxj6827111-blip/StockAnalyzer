#!/usr/bin/env python3
"""Collect complete PIT listing and name-change history with per-symbol evidence.

This collector uses only the public Tushare API surface.  It stores resumable,
credential-free checkpoint files under an explicit research directory and emits
the payload accepted by ``sync_tail_reference_data.py --security-status-json``.
It never opens or writes a warehouse.
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

TOKEN_ENV = "SA__MARKET_WAREHOUSE__TUSHARE_TOKEN"
BASIC_STATES = ("L", "D", "P")
BASIC_ROW_LIMIT = 6_000
NAMECHANGE_ROW_LIMIT = 10_000
BASIC_FIELDS = "ts_code,name,list_date,delist_date,list_status,market,exchange"
NAMECHANGE_FIELDS = "ts_code,name,start_date,end_date,ann_date,change_reason"
_CODE_RE = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")
_LEGACY_DELISTED_CODE_RE = re.compile(r"^T\d{6}\.SH$")


class CollectionError(ValueError):
    """The source scope or its resumable evidence is not trustworthy."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _records(frame: Any) -> list[dict[str, Any]]:
    if not hasattr(frame, "to_dict") or not hasattr(frame, "columns"):
        raise CollectionError("provider_response_not_a_dataframe")
    return list(frame.to_dict("records"))


def _normalize_code(value: Any, *, allow_legacy_delisted: bool = False) -> str:
    code = str(value or "").strip().upper()
    if _CODE_RE.fullmatch(code):
        return code
    # Tushare stock_basic(list_status="D") includes historical Shanghai
    # securities such as T600018.SH. Preserve that source identity: stripping
    # the T would collide with the current 600018.SH listing.
    if allow_legacy_delisted and _LEGACY_DELISTED_CODE_RE.fullmatch(code):
        return code
    raise CollectionError("invalid_or_missing_ts_code")


def _valid_optional_day(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        try:
            if bool(value != value):
                # pandas represents an open-ended date as NaN in some response frames.
                return True
        except (TypeError, ValueError):
            pass
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "<na>"}:
        return True
    if len(text) == 8 and text.isdigit():
        try:
            datetime.strptime(text, "%Y%m%d")
            return True
        except ValueError:
            return False
    try:
        datetime.fromisoformat(text[:10])
    except ValueError:
        return False
    return True


def _retry_call(
    api: Any,
    method: str,
    kwargs: dict[str, Any],
    *,
    retries: int,
    delay: float,
) -> Any:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            result = getattr(api, method)(**kwargs)
            if delay:
                time.sleep(delay)
            return result
        except Exception as exc:  # noqa: BLE001 - provider payloads may contain sensitive data
            last = exc
            if attempt < retries:
                time.sleep(max(delay, 0.25) * (attempt + 1))
    raise CollectionError(f"{method}_failed:{type(last).__name__ if last else 'unknown'}")


def _load_or_create_basic_scope(api: Any, checkpoint_dir: Path, *, delay: float) -> dict[str, Any]:
    basic_path = checkpoint_dir / "stock_basic_snapshot.json"
    if basic_path.exists():
        payload = json.loads(basic_path.read_text(encoding="utf-8"))
        if payload.get("schema") != "tail_security_stock_basic_snapshot.v1":
            raise CollectionError("unrecognized_stock_basic_checkpoint")
        return payload

    snapshots: dict[str, Any] = {}
    for state in BASIC_STATES:
        frame = _retry_call(
            api,
            "stock_basic",
            {"list_status": state, "fields": BASIC_FIELDS},
            retries=2,
            delay=delay,
        )
        rows = _records(frame)
        columns = set(getattr(frame, "columns", []))
        required = set(BASIC_FIELDS.split(","))
        if not required.issubset(columns):
            raise CollectionError(f"stock_basic_{state}_missing_columns")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in rows:
            code = _normalize_code(
                item.get("ts_code"), allow_legacy_delisted=(state == "D")
            )
            if str(item.get("list_status") or "").strip().upper() != state:
                raise CollectionError(f"stock_basic_{state}_unexpected_status")
            if code in seen:
                raise CollectionError(f"stock_basic_{state}_duplicate_symbol")
            seen.add(code)
            normalized.append({key: item.get(key) for key in BASIC_FIELDS.split(",")})
        if len(rows) >= BASIC_ROW_LIMIT:
            raise CollectionError(f"stock_basic_{state}_at_or_above_row_limit")
        snapshots[state] = {"rows": normalized, "rows_returned": len(rows), "complete": True}
    payload = {
        "schema": "tail_security_stock_basic_snapshot.v1",
        "captured_at": datetime.now().astimezone().isoformat(),
        "states": snapshots,
    }
    _atomic_json(basic_path, payload)
    return payload


def _symbols_from_basic(basic: dict[str, Any]) -> list[str]:
    states = basic.get("states")
    if not isinstance(states, dict) or any(state not in states for state in BASIC_STATES):
        raise CollectionError("stock_basic_scope_incomplete")
    symbols: set[str] = set()
    for state in BASIC_STATES:
        snapshot = states[state]
        if snapshot.get("complete") is not True:
            raise CollectionError("stock_basic_scope_incomplete")
        for row in snapshot.get("rows", []):
            code = _normalize_code(
                row.get("ts_code"), allow_legacy_delisted=(state == "D")
            )
            if code in symbols:
                raise CollectionError("stock_basic_symbol_in_multiple_statuses")
            symbols.add(code)
    if not symbols:
        raise CollectionError("stock_basic_scope_empty")
    return sorted(symbols)


def collect_security_status(
    api: Any,
    *,
    checkpoint_dir: Path,
    output_path: Path,
    request_interval_sec: float = 1.25,
    retries: int = 2,
) -> dict[str, Any]:
    """Collect full stock-basic scope and per-symbol name-change history.

    A query with no name-change rows is still a successful, complete response.
    Failed and row-capped symbols remain explicit in ``namechange_collection`` and
    get a coverage marker when the payload is converted for reference-store import.
    """
    checkpoint_dir = checkpoint_dir.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    basic = _load_or_create_basic_scope(
        api, checkpoint_dir, delay=max(0.0, request_interval_sec)
    )
    symbols = _symbols_from_basic(basic)
    symbols_sha = _sha256_bytes(_canonical_json(symbols))
    basic_sha = _sha256_bytes(_canonical_json(basic))
    manifest_path = checkpoint_dir / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("schema") != "tail_security_status_collection.v1"
            or manifest.get("symbols_sha256") != symbols_sha
            or manifest.get("stock_basic_sha256") != basic_sha
        ):
            raise CollectionError("checkpoint_scope_mismatch")
    else:
        manifest = {
            "schema": "tail_security_status_collection.v1",
            "run_id": _sha256_bytes(
                f"{basic_sha}:{datetime.now().astimezone().isoformat()}".encode()
            )[:24],
            "created_at": datetime.now().astimezone().isoformat(),
            "stock_basic_sha256": basic_sha,
            "symbols_sha256": symbols_sha,
            "requested_symbols": len(symbols),
            "complete_symbols": 0,
            "incomplete_symbols": [],
            "as_of": datetime.now().astimezone().isoformat(),
        }
        _atomic_json(manifest_path, manifest)

    response_dir = checkpoint_dir / "namechange"
    response_dir.mkdir(parents=True, exist_ok=True)
    rows_by_symbol: dict[str, list[dict[str, Any]]] = {}
    coverage_by_symbol: dict[str, bool] = {}
    response_hashes: dict[str, str] = {}
    for index, code in enumerate(symbols, 1):
        response_path = response_dir / f"{code}.json"
        result: dict[str, Any] | None = None
        if response_path.exists():
            cached = json.loads(response_path.read_text(encoding="utf-8"))
            if cached.get("ts_code") != code:
                raise CollectionError("checkpoint_symbol_mismatch")
            if cached.get("coverage_complete") is True:
                result = cached
        if result is None:
            try:
                frame = _retry_call(
                    api,
                    "namechange",
                    {"ts_code": code, "fields": NAMECHANGE_FIELDS},
                    retries=max(0, retries),
                    delay=max(0.0, request_interval_sec),
                )
                records = _records(frame)
                columns = set(getattr(frame, "columns", []))
                required = set(NAMECHANGE_FIELDS.split(","))
                complete = required.issubset(columns) and len(records) < NAMECHANGE_ROW_LIMIT
                normalized: list[dict[str, Any]] = []
                if complete:
                    seen_start_dates: set[str] = set()
                    for row in records:
                        row_code = _normalize_code(
                            row.get("ts_code"),
                            allow_legacy_delisted=code.startswith("T"),
                        )
                        start_date = str(row.get("start_date") or "").strip()
                        if (
                            row_code != code
                            or not start_date
                            or not _valid_optional_day(start_date)
                            or not _valid_optional_day(row.get("end_date"))
                            or start_date in seen_start_dates
                        ):
                            complete = False
                            break
                        seen_start_dates.add(start_date)
                        normalized.append(
                            {key: row.get(key) for key in NAMECHANGE_FIELDS.split(",")}
                        )
                result = {
                    "ts_code": code,
                    "rows": normalized,
                    "rows_returned": len(records),
                    "coverage_complete": complete,
                    "truncated": len(records) >= NAMECHANGE_ROW_LIMIT,
                    "error": "" if complete else "invalid_or_truncated_response",
                }
            except Exception as exc:  # noqa: BLE001 - persist only the exception type
                result = {
                    "ts_code": code,
                    "rows": [],
                    "rows_returned": 0,
                    "coverage_complete": False,
                    "truncated": False,
                    "error": type(exc).__name__,
                }
            _atomic_json(response_path, result)
        rows_by_symbol[code] = list(result.get("rows", []))
        coverage_by_symbol[code] = bool(result.get("coverage_complete"))
        response_hashes[code] = _sha256_bytes(_canonical_json(result))
        if index % 100 == 0 or index == len(symbols):
            manifest["complete_symbols"] = sum(coverage_by_symbol.values())
            manifest["incomplete_symbols"] = [
                symbol for symbol, complete in coverage_by_symbol.items() if not complete
            ]
            manifest["last_completed_symbol"] = code
            _atomic_json(manifest_path, manifest)

    # Include cached responses not visited yet in the summary only when they are valid.
    for code in symbols:
        response_path = response_dir / f"{code}.json"
        if code in coverage_by_symbol or not response_path.exists():
            continue
        result = json.loads(response_path.read_text(encoding="utf-8"))
        rows_by_symbol[code] = list(result.get("rows", []))
        coverage_by_symbol[code] = bool(result.get("coverage_complete"))
        response_hashes[code] = _sha256_bytes(_canonical_json(result))

    namechange_rows = [row for code in symbols for row in rows_by_symbol.get(code, [])]
    collection = {
        "schema": "tushare_namechange_per_symbol.v1",
        "as_of": manifest["as_of"],
        "requested_symbols": len(symbols),
        "complete_symbols": sum(coverage_by_symbol.get(code, False) for code in symbols),
        "incomplete_symbols": [code for code in symbols if not coverage_by_symbol.get(code, False)],
        "coverage_by_symbol": coverage_by_symbol,
        "response_sha256_by_symbol": response_hashes,
        "stock_basic_sha256": basic_sha,
        "symbols_sha256": symbols_sha,
    }
    output = {
        "schema": "tail_security_status_payload.v2",
        "as_of": manifest["as_of"],
        "collection_run_id": manifest["run_id"],
        "basic_L": {"rows": basic["states"]["L"]["rows"]},
        "basic_D": {"rows": basic["states"]["D"]["rows"]},
        "basic_P": {"rows": basic["states"]["P"]["rows"]},
        "stock_basic_collection": {
            "complete": True,
            "rows_by_status": {
                state: basic["states"][state]["rows_returned"] for state in BASIC_STATES
            },
            "sha256": basic_sha,
        },
        "namechange_total": len(namechange_rows),
        "namechange": {"rows": namechange_rows},
        "namechange_collection": collection,
    }
    if output_path.exists():
        existing = json.loads(output_path.read_text(encoding="utf-8"))
        if existing.get("collection_run_id") != manifest["run_id"]:
            raise CollectionError("refusing_to_replace_different_output")
    _atomic_json(output_path, output)
    manifest["complete_symbols"] = collection["complete_symbols"]
    manifest["incomplete_symbols"] = collection["incomplete_symbols"]
    manifest["output_path"] = str(output_path)
    manifest["output_sha256"] = _sha256_bytes(_canonical_json(output))
    _atomic_json(manifest_path, manifest)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint-dir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--request-interval-sec", type=float, default=1.25)
    parser.add_argument("--retries", type=int, default=2)
    args = parser.parse_args(argv)
    token = str(os.environ.get(TOKEN_ENV, "") or "").strip()
    if not token:
        print(f"security status collection failed: missing {TOKEN_ENV}", file=sys.stderr)
        return 5
    try:
        import tushare as ts

        payload = collect_security_status(
            ts.pro_api(token),
            checkpoint_dir=Path(args.checkpoint_dir),
            output_path=Path(args.out),
            request_interval_sec=max(0.0, args.request_interval_sec),
            retries=max(0, args.retries),
        )
    except Exception as exc:  # noqa: BLE001 - provider messages may contain request details
        print(f"security status collection failed: {type(exc).__name__}", file=sys.stderr)
        return 5
    collection = payload["namechange_collection"]
    print(
        json.dumps(
            {
                "output": str(Path(args.out).expanduser().resolve()),
                "requested_symbols": collection["requested_symbols"],
                "complete_symbols": collection["complete_symbols"],
                "incomplete_symbols": len(collection["incomplete_symbols"]),
            },
            ensure_ascii=False,
        )
    )
    return 0 if not collection["incomplete_symbols"] else 5


if __name__ == "__main__":
    raise SystemExit(main())
