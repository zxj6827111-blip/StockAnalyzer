"""Thread-safe in-memory background task registry (audit P1-#7).

FastAPI ``BackgroundTasks`` only schedules work after the response is sent;
this module tracks the submitted task lifecycle (``queued`` -> ``running`` ->
``succeeded``/``failed``) so callers can poll ``GET /tasks/{task_id}``. The
registry is size-capped to bound memory usage, and failed tasks keep their
error message.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import BackgroundTasks

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

_DEFAULT_MAX_HISTORY = 200


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class BackgroundTaskRegistry:
    """Size-capped, lock-protected registry of background task entries."""

    def __init__(self, max_history: int = _DEFAULT_MAX_HISTORY) -> None:
        self._max_history = max(1, int(max_history))
        self._entries: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def submit(self, *, name: str) -> str:
        """Create a ``queued`` entry and return its unique task id."""
        task_id = uuid.uuid4().hex
        entry: dict[str, Any] = {
            "task_id": task_id,
            "name": name,
            "status": STATUS_QUEUED,
            "submitted_at": _now_iso(),
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
        }
        with self._lock:
            self._entries[task_id] = entry
            self._evict_locked()
        return task_id

    def mark_running(self, task_id: str) -> bool:
        with self._lock:
            entry = self._entries.get(task_id)
            if entry is None or entry["status"] != STATUS_QUEUED:
                return False
            entry["status"] = STATUS_RUNNING
            entry["started_at"] = _now_iso()
        return True

    def mark_succeeded(self, task_id: str, result: Any) -> bool:
        with self._lock:
            entry = self._entries.get(task_id)
            if entry is None or entry["status"] not in (STATUS_QUEUED, STATUS_RUNNING):
                return False
            entry["status"] = STATUS_SUCCEEDED
            entry["result"] = result
            entry["finished_at"] = _now_iso()
            self._evict_locked()
        return True

    def mark_failed(self, task_id: str, error: str) -> bool:
        with self._lock:
            entry = self._entries.get(task_id)
            if entry is None or entry["status"] not in (STATUS_QUEUED, STATUS_RUNNING):
                return False
            entry["status"] = STATUS_FAILED
            entry["error"] = error
            entry["finished_at"] = _now_iso()
            self._evict_locked()
        return True

    def get(self, task_id: str) -> dict[str, Any] | None:
        """Return a copy of the entry, or ``None`` for an unknown task id."""
        with self._lock:
            entry = self._entries.get(task_id)
            return dict(entry) if entry is not None else None

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return copies of the most recently submitted entries, newest first."""
        with self._lock:
            items = list(self._entries.values())
        items.sort(key=lambda item: str(item["submitted_at"]), reverse=True)
        return [dict(item) for item in items[: max(1, limit)]]

    def _evict_locked(self) -> None:
        """Drop the oldest finished entries once the history cap is exceeded.

        Queued/running entries are never evicted.
        """
        if len(self._entries) <= self._max_history:
            return
        finished_ids = [
            task_id
            for task_id, entry in self._entries.items()
            if entry["status"] in (STATUS_SUCCEEDED, STATUS_FAILED)
        ]
        overflow = len(self._entries) - self._max_history
        for task_id in finished_ids[:overflow]:
            del self._entries[task_id]


registry = BackgroundTaskRegistry()


def run_registered_task(
    task_id: str,
    fn: Callable[[], Any],
    registry_obj: BackgroundTaskRegistry | None = None,
) -> None:
    """Execute ``fn`` and record the outcome into the registry.

    Any exception raised by ``fn`` is captured as a ``failed`` entry with the
    error message instead of propagating into the request lifecycle.
    """
    reg = registry_obj if registry_obj is not None else registry
    if not reg.mark_running(task_id):
        return
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 - any failure must be recorded
        reg.mark_failed(task_id, f"{type(exc).__name__}: {exc}")
        return
    reg.mark_succeeded(task_id, result)


def submit_background_task(
    background_tasks: BackgroundTasks,
    *,
    name: str,
    fn: Callable[[], Any],
    registry_obj: BackgroundTaskRegistry | None = None,
) -> dict[str, object]:
    """Register ``fn`` for post-response execution and return the 202 payload.

    The callable is executed in FastAPI's background-task machinery (a worker
    thread) right after the response is sent; its result or error lands in the
    registry entry tracked by the returned ``task_id``.
    """
    reg = registry_obj if registry_obj is not None else registry
    task_id = reg.submit(name=name)
    background_tasks.add_task(run_registered_task, task_id, fn, reg)
    entry = reg.get(task_id) or {}
    return {
        "task_id": task_id,
        "name": name,
        "status": STATUS_QUEUED,
        "submitted_at": entry.get("submitted_at", ""),
    }
