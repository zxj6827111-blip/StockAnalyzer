"""保存尾盘影子决策点实际收到的输入快照。

该快照留存运行时输入本身，便于后续补齐逐字段来源与可用时钟；它不等同于
``tail_field_pit_evidence``，也不会单独让历史/PIT 审计通过。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from enum import Enum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCHEMA = "tail_observed_input_snapshot.v1"
RUNTIME_CONTEXT_SCHEMA = "tail_observed_runtime_context_snapshot.v1"


def _json_value(value: Any) -> Any:
    """把运行时对象转换成标准 JSON 值，NaN/Inf 显式落成 null。"""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, set):
        return sorted((_json_value(item) for item in value), key=repr)
    scalar = getattr(value, "item", None)
    if callable(scalar):
        try:
            return _json_value(scalar())
        except (TypeError, ValueError):
            pass
    return str(value)


def write_tail_observed_input_snapshot(
    root: str | Path,
    *,
    timestamp: datetime | None,
    trade_date: date,
    contract: Any,
    watch_pool: Sequence[Mapping[str, Any]],
    minute_bars: Mapping[str, Any],
    minute_bar_sources: Mapping[str, Any] | None = None,
    runtime_context: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """在最后一个确认时点写一次不可覆盖的输入快照。

    其他运行时刻（包括收盘后的 14:55 轮次）返回 ``None``。时间戳必须带时区，
    且必须与策略时区、最后确认槽和交易日一致。
    """
    if timestamp is None or timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return None
    try:
        zone = ZoneInfo(str(contract.timezone))
        local = timestamp.astimezone(zone)
    except (KeyError, ValueError):
        return None
    if local.date() != trade_date or local.strftime("%H:%M") != contract.latest_confirmation_clock:
        return None

    symbols = sorted({
        str(row.get("symbol", "")).strip()
        for row in watch_pool
        if str(row.get("symbol", "")).strip()
    })
    normalized_bars: dict[str, list[dict[str, Any]]] = {}
    for symbol, bars in minute_bars.items():
        normalized_bars[str(symbol)] = [
            {"bar_time": _json_value(bar_time), "bar": _json_value(bar)}
            for bar_time, bar in bars
        ]

    capture_scope = (
        "tail_shadow_watch_pool_minute_and_runtime_context"
        if runtime_context is not None
        else "tail_shadow_watch_pool_and_minute_inputs_only"
    )
    payload = {
        "schema": SCHEMA,
        "capture_mode": "observed_snapshot",
        "capture_scope": capture_scope,
        "full_chain_pit_evidence": False,
        "trade_date": trade_date.isoformat(),
        "decision_at": local.isoformat(timespec="microseconds"),
        "captured_at": local.isoformat(timespec="microseconds"),
        "capture_clock_source": "runtime_observation_capture_clock",
        "contract": {
            "version": str(contract.contract_version),
            "digest": str(contract.digest()),
            "timezone": str(contract.timezone),
            "entry_window": [str(contract.entry_window_start), str(contract.entry_window_end)],
            "decision_slot": str(contract.latest_confirmation_clock),
        },
        "input_symbol_count": len(symbols),
        "symbols": symbols,
        "watch_pool": _json_value(watch_pool),
        "minute_bars": normalized_bars,
        "minute_bar_sources": _json_value(minute_bar_sources or {}),
        "runtime_context": _json_value(runtime_context) if runtime_context is not None else None,
        "limitations": [
            "does_not_include_the_full_market_candidate_universe",
            "does_not_attest_per_field_source_availability_or_imputation_history",
            "runtime_context_is_not_per_field_pit_evidence",
            "does_not_pass_full_chain_field_pit_audit_by_itself",
        ],
    }
    encoded = (json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ) + "\n").encode("utf-8")
    target_root = Path(root) / "observed_inputs" / trade_date.isoformat()
    target_root.mkdir(parents=True, exist_ok=True)
    slot_key = str(contract.latest_confirmation_clock).replace(":", "")
    filename = f"tail_observed_input_{slot_key}.json"
    target = target_root / filename
    fd, temporary_name = tempfile.mkstemp(prefix=".tail-input-", suffix=".tmp", dir=target_root)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            return {"status": "blocked", "reason": "snapshot_already_exists"}
    finally:
        temporary.unlink(missing_ok=True)

    directory_fd = os.open(target_root, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return {
        "status": "captured",
        "schema": SCHEMA,
        "capture_scope": payload["capture_scope"],
        "path": str(target.resolve()),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "trade_date": trade_date.isoformat(),
        "decision_at": payload["decision_at"],
        "input_symbol_count": len(symbols),
        "minute_bar_symbol_count": len(normalized_bars),
        "full_chain_pit_evidence": False,
    }


def write_tail_observed_runtime_context_snapshot(
    root: str | Path,
    *,
    timestamp: datetime | None,
    trade_date: date,
    contract: Any,
    input_snapshot: Mapping[str, Any],
    runtime_context: Mapping[str, Any],
) -> dict[str, Any]:
    """单独保存按时完成的候选 pipeline 上下文，并绑定预先落盘的原始输入快照。"""
    if timestamp is None or timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return {"status": "not_persisted", "reason": "runtime_context_clock_invalid"}
    try:
        zone = ZoneInfo(str(contract.timezone))
        local = timestamp.astimezone(zone)
    except (KeyError, ValueError):
        return {"status": "not_persisted", "reason": "runtime_context_timezone_invalid"}
    if local.date() != trade_date or local.strftime("%H:%M") != contract.latest_confirmation_clock:
        return {
            "status": "not_persisted",
            "reason": "pipeline_completed_outside_decision_minute",
        }
    if input_snapshot.get("status") != "captured":
        return {"status": "not_persisted", "reason": "input_snapshot_not_captured"}
    if (
        input_snapshot.get("schema") != SCHEMA
        or input_snapshot.get("trade_date") != trade_date.isoformat()
    ):
        return {"status": "not_persisted", "reason": "input_snapshot_metadata_invalid"}

    root_path = Path(root).resolve()
    input_path_value = input_snapshot.get("path")
    expected_input_sha = input_snapshot.get("sha256")
    if not isinstance(input_path_value, str) or not isinstance(expected_input_sha, str):
        return {"status": "not_persisted", "reason": "input_snapshot_reference_invalid"}
    input_path = Path(input_path_value).resolve()
    if not input_path.is_relative_to(root_path) or not input_path.is_file():
        return {"status": "not_persisted", "reason": "input_snapshot_path_invalid"}
    try:
        input_bytes = input_path.read_bytes()
    except OSError:
        return {"status": "not_persisted", "reason": "input_snapshot_unreadable"}
    if hashlib.sha256(input_bytes).hexdigest() != expected_input_sha:
        return {"status": "not_persisted", "reason": "input_snapshot_hash_mismatch"}
    try:
        input_payload = json.loads(input_bytes)
        if not isinstance(input_payload, Mapping):
            raise ValueError("input snapshot payload is not an object")
        input_clock = datetime.fromisoformat(str(input_payload["decision_at"]))
        if (
            input_payload.get("schema") != SCHEMA
            or input_payload.get("trade_date") != trade_date.isoformat()
            or input_snapshot.get("decision_at") != input_payload.get("decision_at")
            or input_clock.tzinfo is None
            or input_clock.astimezone(zone).date() != trade_date
            or input_clock.astimezone(zone).strftime("%H:%M")
            != contract.latest_confirmation_clock
            or input_clock.astimezone(zone) > local
        ):
            raise ValueError("input snapshot clock or metadata mismatch")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return {"status": "not_persisted", "reason": "input_snapshot_clock_invalid"}

    relative_input_path = input_path.relative_to(root_path).as_posix()
    payload = {
        "schema": RUNTIME_CONTEXT_SCHEMA,
        "capture_mode": "observed_snapshot",
        "capture_scope": "candidate_pipeline_runtime_context_only",
        "full_chain_pit_evidence": False,
        "trade_date": trade_date.isoformat(),
        "decision_at": local.isoformat(timespec="microseconds"),
        "captured_at": local.isoformat(timespec="microseconds"),
        "capture_clock_source": "runtime_pipeline_completion_clock",
        "input_snapshot": {
            "path": relative_input_path,
            "sha256": expected_input_sha,
        },
        "runtime_context": _json_value(runtime_context),
        "limitations": [
            "covers_only_the_live_runtime_candidate_pool",
            "does_not_attest_per_field_source_availability_or_imputation_history",
            "does_not_pass_full_chain_field_pit_audit_by_itself",
        ],
    }
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    target_root = root_path / "observed_inputs" / trade_date.isoformat()
    target_root.mkdir(parents=True, exist_ok=True)
    slot_key = str(contract.latest_confirmation_clock).replace(":", "")
    target = target_root / f"tail_runtime_context_{slot_key}.json"
    fd, temporary_name = tempfile.mkstemp(prefix=".tail-context-", suffix=".tmp", dir=target_root)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            return {"status": "blocked", "reason": "runtime_context_snapshot_already_exists"}
    finally:
        temporary.unlink(missing_ok=True)

    directory_fd = os.open(target_root, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return {
        "status": "captured",
        "schema": RUNTIME_CONTEXT_SCHEMA,
        "path": str(target.resolve()),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "input_snapshot_path": relative_input_path,
        "input_snapshot_sha256": expected_input_sha,
        "trade_date": trade_date.isoformat(),
        "decision_at": payload["decision_at"],
        "full_chain_pit_evidence": False,
    }
