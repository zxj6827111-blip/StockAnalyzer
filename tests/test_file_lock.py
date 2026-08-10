from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from stock_analyzer.ops.file_lock import DEFAULT_STALE_AFTER_SEC, DistributedFileLock


def _read_payload(lock_path: Path) -> dict[str, object]:
    return json.loads(lock_path.read_text(encoding="utf-8"))


def test_acquire_creates_lock_with_owner_payload(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler_leader.lock"
    lock = DistributedFileLock(lock_path, stale_after_sec=60, owner_token="owner-1")

    assert lock.acquire() is True
    assert lock.is_held() is True
    assert lock_path.exists()

    payload = _read_payload(lock_path)
    assert payload["owner_token"] == "owner-1"
    assert payload["stale_after_sec"] == 60
    assert isinstance(payload["pid"], int) and payload["pid"] > 0
    assert isinstance(payload["created_at"], str)

    lock.release()
    assert lock.is_held() is False
    assert not lock_path.exists()


def test_second_lock_cannot_acquire_while_held(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler_leader.lock"
    first = DistributedFileLock(lock_path)
    second = DistributedFileLock(lock_path)

    assert first.acquire() is True
    try:
        assert second.acquire() is False
        assert second.is_held() is False
        assert first.is_held() is True
        assert lock_path.exists()
    finally:
        first.release()


def test_release_allows_reacquire(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler_leader.lock"
    first = DistributedFileLock(lock_path)
    second = DistributedFileLock(lock_path)

    assert first.acquire() is True
    first.release()
    assert not lock_path.exists()

    assert second.acquire() is True
    second.release()
    assert not lock_path.exists()


def test_release_refused_for_foreign_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler_leader.lock"
    owner = DistributedFileLock(lock_path, owner_token="owner-a")
    intruder = DistributedFileLock(lock_path, owner_token="owner-b")

    assert owner.acquire() is True
    try:
        intruder.release()
        assert lock_path.exists()
        assert _read_payload(lock_path)["owner_token"] == "owner-a"
    finally:
        owner.release()


def test_renew_refreshes_mtime(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler_leader.lock"
    lock = DistributedFileLock(lock_path, stale_after_sec=60)

    assert lock.acquire() is True
    try:
        mtime_before = lock_path.stat().st_mtime
        time.sleep(0.05)
        assert lock.renew() is True
        assert lock_path.stat().st_mtime > mtime_before
    finally:
        lock.release()


def test_renew_refused_for_foreign_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler_leader.lock"
    owner = DistributedFileLock(lock_path, owner_token="owner-a")
    intruder = DistributedFileLock(lock_path, owner_token="owner-b")

    assert owner.acquire() is True
    try:
        assert intruder.renew() is False
    finally:
        owner.release()


def test_stale_lock_can_be_taken_over(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler_leader.lock"
    first = DistributedFileLock(lock_path, stale_after_sec=1)
    second = DistributedFileLock(lock_path, stale_after_sec=1)

    assert first.acquire() is True
    try:
        time.sleep(1.2)
        assert first.is_stale() is True
        assert second.status()["is_stale"] is True

        assert second.acquire() is True
        assert _read_payload(lock_path)["owner_token"] == second.owner_token
    finally:
        first.release()
        second.release()


def test_stale_lock_is_never_reported_when_file_missing(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler_leader.lock"
    lock = DistributedFileLock(lock_path)
    assert lock.is_stale() is False
    assert lock.status()["exists"] is False
    assert lock.status()["is_stale"] is False


def test_heartbeat_keeps_lock_fresh(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler_leader.lock"
    holder = DistributedFileLock(lock_path, stale_after_sec=2, heartbeat_interval_sec=0.3)
    contender = DistributedFileLock(lock_path, stale_after_sec=2)

    assert holder.acquire() is True
    try:
        time.sleep(2.5)
        assert holder.is_stale() is False
        assert contender.acquire() is False
    finally:
        holder.release()


def test_lock_goes_stale_without_heartbeat(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler_leader.lock"
    lock = DistributedFileLock(lock_path, stale_after_sec=1, heartbeat_interval_sec=10.0)

    assert lock.acquire() is True
    try:
        time.sleep(1.2)
        assert lock.is_stale() is True
    finally:
        lock.release()


def test_status_reports_lock_state(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler_leader.lock"
    lock = DistributedFileLock(lock_path, stale_after_sec=60, owner_token="owner-1")

    assert lock.acquire() is True
    try:
        status = lock.status()
        assert status["exists"] is True
        assert status["owner_token"] == "owner-1"
        assert status["held_by_me"] is True
        assert status["is_stale"] is False
        assert status["age_sec"] >= 0
        assert status["stale_after_sec"] == 60
    finally:
        lock.release()

    assert lock.status()["exists"] is False


def test_default_stale_timeout_is_300_seconds() -> None:
    assert DEFAULT_STALE_AFTER_SEC == 300


def test_concurrent_acquire_mutual_exclusion(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler_leader.lock"
    worker_count = 8
    barrier = threading.Barrier(worker_count)
    guard = threading.Lock()
    active = 0
    max_active = 0

    def _worker(index: int) -> None:
        nonlocal active, max_active
        lock = DistributedFileLock(lock_path, owner_token=f"worker-{index}")
        barrier.wait()
        if not lock.acquire():
            return
        try:
            with guard:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.2)
        finally:
            with guard:
                active -= 1
            lock.release()

    threads = [threading.Thread(target=_worker, args=(index,)) for index in range(worker_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert max_active == 1
    assert not lock_path.exists()


def test_concurrent_acquire_exactly_one_wins(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler_leader.lock"
    winners: list[str] = []

    def _try_acquire(index: int) -> bool:
        lock = DistributedFileLock(lock_path, owner_token=f"thread-{index}")
        if lock.acquire():
            winners.append(lock.owner_token)
            time.sleep(0.1)
            lock.release()
            return True
        return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_try_acquire, range(8)))

    assert sum(1 for result in results if result) == 1
    assert len(winners) == 1


def test_stale_takeover_owner_verification(tmp_path: Path) -> None:
    """A takeover must win the lock exclusively and protect it from the
    previous owner (owner-token verification on acquire/release)."""
    lock_path = tmp_path / "scheduler_leader.lock"
    first = DistributedFileLock(lock_path, stale_after_sec=1, owner_token="owner-1")
    second = DistributedFileLock(lock_path, stale_after_sec=1, owner_token="owner-2")

    assert first.acquire() is True
    first.release()  # simulate crash: no release would happen, but the file remains

    # Simulate a stale lock left behind by a dead holder.
    lock_path.touch()
    old = time.time() - 10
    import os

    os.utime(lock_path, (old, old))
    assert first.is_stale() is True

    # Second process takes over the stale lock.
    assert second.acquire() is True
    assert _read_payload(lock_path)["owner_token"] == "owner-2"

    # First process must not re-acquire while the new lock is fresh, and must
    # not remove the foreign lock on release.
    assert first.acquire() is False
    first.release()
    assert lock_path.exists()
    assert _read_payload(lock_path)["owner_token"] == "owner-2"
    assert second.is_held() is True

    second.release()
    assert not lock_path.exists()


def test_release_after_foreign_takeover_resets_held(tmp_path: Path) -> None:
    """release() after the lock was taken over must reset _held so a later
    acquire() does not falsely report success."""
    lock_path = tmp_path / "scheduler_leader.lock"
    first = DistributedFileLock(lock_path, stale_after_sec=1, owner_token="owner-1")
    second = DistributedFileLock(lock_path, stale_after_sec=1, owner_token="owner-2")

    assert first.acquire() is True
    # Stop the heartbeat so the lock goes stale naturally.
    first._stop_heartbeat()  # noqa: SLF001
    lock_path.touch()
    old = time.time() - 10
    import os

    os.utime(lock_path, (old, old))

    assert second.acquire() is True  # takeover
    assert first.is_held() is True  # first still believes it holds the lock

    first.release()  # must reset held even though the file is foreign
    assert first.is_held() is False

    # A subsequent acquire must actually try the filesystem again.
    second.release()
    assert first.acquire() is True
    assert _read_payload(lock_path)["owner_token"] == "owner-1"
    first.release()
