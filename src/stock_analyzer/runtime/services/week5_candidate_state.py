"""Persistent candidate state for the Week5 full-market automation chain."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

from stock_analyzer.ops.file_lock import DistributedFileLock

_LOCK_STALE_SEC = 180
_LOCK_WAIT_SEC = 15.0


def default_candidate_state() -> dict[str, object]:
    """Return a schema-stable empty state."""
    return {
        "state_revision": 0,
        "updated_at": "",
        "field_updated_at": {},
        "trade_date": "",
        "data_version": "",
        "fallback": {},
        "night_pool": [],
        "overnight_top5": [],
        "night_pool_updated_at": "",
        "night_pool_trade_date": "",
        "opening_focus": [],
        "radar_continuity": {},
        "dynamic_candidate_pool": [],
        "dynamic_candidate_pool_trade_date": "",
        "live_runtime": [],
        "live_runtime_trade_date": "",
        "candidate_data_gate": {},
        "actionable_data_gate": {},
        "latest_night_scan": {},
        "latest_auction": {},
        "latest_market_radar": {},
        "market_radar_health": {},
        "latest_live_runtime": {},
        "latest_weekend_learning": {},
        "auction_baseline": {
            "dates": [],
            "by_symbol": {},
        },
    }


class CandidateStateStore:
    """File-backed state store with lock, atomic replace and stale-writer guard."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.lock_path = Path(f"{self.path}.lock")

    def load(self) -> dict[str, object]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default_candidate_state()
        if not isinstance(raw, dict):
            return default_candidate_state()
        state = default_candidate_state()
        state.update(deepcopy(raw))
        return state

    def update(
        self,
        patch: Mapping[str, object],
        *,
        updated_at: str | datetime | None = None,
        trade_date: str = "",
        data_version: str = "",
        allow_stale: bool = False,
    ) -> dict[str, object]:
        """Merge a patch and return the committed state.

        Timestamp ordering is enforced per field. This lets an older radar
        worker merge a field that a newer live worker did not touch, while
        still preventing it from overwriting a newer value for the same key.
        """
        incoming_at = _normalize_timestamp(updated_at)
        lock = DistributedFileLock(
            self.lock_path,
            stale_after_sec=_LOCK_STALE_SEC,
            heartbeat_interval_sec=5.0,
        )
        deadline = monotonic() + _LOCK_WAIT_SEC
        while not lock.acquire():
            if monotonic() >= deadline:
                raise TimeoutError(f"candidate state lock timeout: {self.lock_path}")
            sleep(0.05)
        try:
            current = self.load()
            current_at = _parse_timestamp(current.get("updated_at"))
            effective_patch = {str(key): deepcopy(value) for key, value in patch.items()}
            if trade_date.strip():
                effective_patch["trade_date"] = trade_date.strip()
            if data_version.strip():
                effective_patch["data_version"] = data_version.strip()
            field_updated_at = {
                str(key): str(value)
                for key, value in _mapping(current.get("field_updated_at")).items()
            }
            accepted: dict[str, object] = {}
            skipped: list[str] = []
            for key, value in effective_patch.items():
                previous_at = _parse_timestamp(field_updated_at.get(key))
                if (
                    not allow_stale
                    and previous_at is not None
                    and incoming_at is not None
                    and incoming_at < previous_at
                ):
                    skipped.append(key)
                    continue
                accepted[key] = value

            if not accepted:
                current["write_status"] = (
                    "stale_writer_ignored" if skipped else "empty_patch"
                )
                return current

            merged = default_candidate_state()
            merged.update(deepcopy(current))
            for key, value in accepted.items():
                merged[key] = deepcopy(value)
                if incoming_at is not None:
                    field_updated_at[key] = incoming_at.isoformat()
            merged["field_updated_at"] = field_updated_at
            latest_at = incoming_at or datetime.now(UTC)
            if current_at is not None and current_at > latest_at:
                latest_at = current_at
            merged["updated_at"] = latest_at.isoformat()
            merged["state_revision"] = max(0, _as_int(current.get("state_revision"))) + 1
            if skipped:
                merged["write_status"] = "stale_fields_ignored"
            else:
                merged.pop("write_status", None)
            self._atomic_write(merged)
            return merged
        finally:
            lock.release()

    def replace(
        self,
        state: Mapping[str, object],
        *,
        updated_at: str | datetime | None = None,
        allow_stale: bool = False,
    ) -> dict[str, object]:
        """Replace the state while retaining the monotonic revision."""
        payload = default_candidate_state()
        payload.update(deepcopy(dict(state)))
        return self.update(
            payload,
            updated_at=updated_at,
            trade_date=str(payload.get("trade_date", "")),
            data_version=str(payload.get("data_version", "")),
            allow_stale=allow_stale,
        )

    def _atomic_write(self, payload: Mapping[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(f"{self.path.name}.{uuid4().hex}.tmp")
        try:
            with temp_path.open("w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, separators=(",", ":"), default=str)
                fp.write("\n")
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(temp_path, self.path)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def _normalize_timestamp(value: str | datetime | None) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    text = str(value or "").strip()
    if not text:
        return datetime.now(UTC)
    parsed = _parse_timestamp(text)
    return parsed or datetime.now(UTC)


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0
