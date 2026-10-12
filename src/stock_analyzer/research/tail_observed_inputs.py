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

    payload = {
        "schema": SCHEMA,
        "capture_mode": "observed_snapshot",
        "capture_scope": "tail_shadow_watch_pool_and_minute_inputs_only",
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
        "limitations": [
            "does_not_include_the_full_market_candidate_universe",
            "does_not_attest_per_field_source_availability_or_imputation_history",
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
