"""Deterministic ordering and hash for online update samples."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class OnlineSampleAudit:
    online_samples_used: int
    online_samples_used_hash: str
    deterministic_order_fields: list[str]
    deterministic_order_applied: bool
    skipped_not_matured: int
    skipped_invalid: int


def build_online_sample_audit(
    *,
    records: Sequence[Mapping[str, object]],
    now: datetime,
) -> OnlineSampleAudit:
    now_cmp = _normalize_datetime(now)
    ordered: list[tuple[str, str, str]] = []
    skipped_not_matured = 0
    skipped_invalid = 0
    for item in records:
        symbol = str(item.get("symbol", "")).strip()
        trade_date = str(item.get("trade_date", item.get("date", ""))).strip()
        if not symbol:
            skipped_invalid += 1
            continue
        raw_mature = str(item.get("label_mature_time", "")).strip()
        if raw_mature:
            mature_dt = _parse_datetime(raw_mature)
            if mature_dt is not None and mature_dt > now_cmp:
                skipped_not_matured += 1
                continue
        ordered.append((raw_mature, trade_date, symbol))
    ordered.sort(key=lambda item: (item[0], item[1], item[2]))
    serialized = [
        {
            "label_mature_time": mature,
            "trade_date": trade_date,
            "symbol": symbol,
        }
        for mature, trade_date, symbol in ordered
    ]
    payload = json.dumps(serialized, ensure_ascii=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return OnlineSampleAudit(
        online_samples_used=len(serialized),
        online_samples_used_hash=digest,
        deterministic_order_fields=["label_mature_time", "trade_date", "symbol"],
        deterministic_order_applied=True,
        skipped_not_matured=skipped_not_matured,
        skipped_invalid=skipped_invalid,
    )


def _parse_datetime(raw: str) -> datetime | None:
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _normalize_datetime(parsed)


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(value.tzinfo).replace(tzinfo=None)
