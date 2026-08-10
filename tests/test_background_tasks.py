"""Unit tests for the in-memory background task registry (audit P1-#7)."""

from __future__ import annotations

import threading
from collections.abc import Callable

from stock_analyzer.ops.background_tasks import (
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    BackgroundTaskRegistry,
    run_registered_task,
)


def _as_str(value: object) -> str:
    assert isinstance(value, str)
    return value


def test_submit_creates_queued_entry() -> None:
    reg = BackgroundTaskRegistry()
    task_id = reg.submit(name="run_pipeline")

    entry = reg.get(task_id)
    assert entry is not None
    assert entry["task_id"] == task_id
    assert entry["name"] == "run_pipeline"
    assert entry["status"] == STATUS_QUEUED
    assert entry["started_at"] is None
    assert entry["finished_at"] is None
    assert entry["result"] is None
    assert entry["error"] is None
    assert _as_str(entry["submitted_at"])


def test_state_transition_queued_running_succeeded() -> None:
    reg = BackgroundTaskRegistry()
    task_id = reg.submit(name="train_models")

    assert reg.mark_running(task_id) is True
    entry = reg.get(task_id)
    assert entry is not None
    assert entry["status"] == STATUS_RUNNING
    assert entry["started_at"] is not None

    assert reg.mark_succeeded(task_id, {"ok": True}) is True
    entry = reg.get(task_id)
    assert entry is not None
    assert entry["status"] == STATUS_SUCCEEDED
    assert entry["result"] == {"ok": True}
    assert entry["finished_at"] is not None


def test_failed_task_records_error() -> None:
    reg = BackgroundTaskRegistry()
    task_id = reg.submit(name="phase_d_alphalens")

    assert reg.mark_failed(task_id, "ValueError: boom") is True
    entry = reg.get(task_id)
    assert entry is not None
    assert entry["status"] == STATUS_FAILED
    assert entry["error"] == "ValueError: boom"
    assert entry["result"] is None


def test_run_registered_task_records_success_result() -> None:
    reg = BackgroundTaskRegistry()
    task_id = reg.submit(name="run_pipeline")

    def _work() -> dict[str, object]:
        return {"trace_id": "t1"}

    run_registered_task(task_id, _work, reg)
    entry = reg.get(task_id)
    assert entry is not None
    assert entry["status"] == STATUS_SUCCEEDED
    assert entry["result"] == {"trace_id": "t1"}


def test_run_registered_task_records_failure_without_raising() -> None:
    reg = BackgroundTaskRegistry()
    task_id = reg.submit(name="train_models")

    def _boom() -> dict[str, object]:
        raise ValueError("training failed")

    run_registered_task(task_id, _boom, reg)
    entry = reg.get(task_id)
    assert entry is not None
    assert entry["status"] == STATUS_FAILED
    assert "ValueError" in _as_str(entry["error"])
    assert "training failed" in _as_str(entry["error"])


def test_get_returns_copy_not_shared_reference() -> None:
    reg = BackgroundTaskRegistry()
    task_id = reg.submit(name="x")
    entry = reg.get(task_id)
    assert entry is not None
    entry["name"] = "mutated"
    assert reg.get(task_id)["name"] == "x"


def test_unknown_task_returns_none() -> None:
    reg = BackgroundTaskRegistry()
    assert reg.get("no-such-task") is None


def test_concurrent_submissions_are_all_registered() -> None:
    reg = BackgroundTaskRegistry(max_history=500)
    results: list[str] = []
    barrier = threading.Barrier(8)

    def _submit() -> None:
        barrier.wait()
        results.append(reg.submit(name="run_pipeline"))

    threads = [threading.Thread(target=_submit) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 8
    assert len(set(results)) == 8
    recent = reg.list_recent(limit=100)
    assert len(recent) == 8


def test_concurrent_state_transitions_are_consistent() -> None:
    reg = BackgroundTaskRegistry()
    task_id = reg.submit(name="train_models")
    success_count = 0
    failure_count = 0
    barrier = threading.Barrier(6)

    def _finish(marker: Callable[[str], bool]) -> None:
        nonlocal success_count, failure_count
        barrier.wait()
        if marker(task_id):
            success_count += 1
        else:
            failure_count += 1

    threads = [
        threading.Thread(
            target=_finish,
            args=(lambda tid: reg.mark_succeeded(tid, {"ok": True}),),
        )
        for _ in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert success_count == 1
    assert failure_count == 5
    entry = reg.get(task_id)
    assert entry is not None
    assert entry["status"] == STATUS_SUCCEEDED
    assert entry["finished_at"] is not None


def test_max_history_evicts_oldest_finished_entries() -> None:
    reg = BackgroundTaskRegistry(max_history=3)
    task_ids = [reg.submit(name="run_pipeline") for _ in range(5)]
    for task_id in task_ids:
        reg.mark_succeeded(task_id, {"ok": True})

    remaining = reg.list_recent(limit=10)
    assert len(remaining) == 3
    remaining_ids = {item["task_id"] for item in remaining}
    assert remaining_ids == set(task_ids[2:])


def test_max_history_never_evicts_queued_or_running() -> None:
    reg = BackgroundTaskRegistry(max_history=2)
    first = reg.submit(name="a")
    second = reg.submit(name="b")
    reg.mark_running(first)
    third = reg.submit(name="c")
    reg.mark_running(third)

    # first is running, second is queued, third is running: nothing evictable.
    remaining = reg.list_recent(limit=10)
    assert {item["task_id"] for item in remaining} == {first, second, third}

    reg.mark_succeeded(second, None)
    # second (finished) is evicted on completion, running tasks stay.
    remaining = reg.list_recent(limit=10)
    assert {item["task_id"] for item in remaining} == {first, third}

    # A new queued submission cannot evict running tasks even when over cap.
    fourth = reg.submit(name="d")
    remaining = reg.list_recent(limit=10)
    assert {item["task_id"] for item in remaining} == {first, third, fourth}


def test_list_recent_orders_by_submitted_at_desc() -> None:
    reg = BackgroundTaskRegistry()
    ids = [reg.submit(name="x") for _ in range(4)]
    recent = reg.list_recent(limit=10)
    assert {item["task_id"] for item in recent} == set(ids)
    stamps = [str(item["submitted_at"]) for item in recent]
    assert stamps == sorted(stamps, reverse=True)


def test_submit_task_ids_are_unique() -> None:
    reg = BackgroundTaskRegistry()
    ids = {reg.submit(name="x") for _ in range(50)}
    assert len(ids) == 50
