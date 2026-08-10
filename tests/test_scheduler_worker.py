from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, cast

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.ops.file_lock import DistributedFileLock
from stock_analyzer.runtime.scheduler_worker import run_once


class _FakeScheduler:
    def export_state(self) -> dict[str, object]:
        return {"jobs": 0}


class _FakeService:
    def __init__(self) -> None:
        self.run_calls = 0
        self._scheduler = _FakeScheduler()

    def run_due_jobs(
        self,
        now: datetime | None = None,
        only_jobs: list[str] | None = None,
    ) -> list[dict[str, object]]:
        self.run_calls += 1
        return [
            {
                "job": "fake_job",
                "ran": False,
                "success": True,
                "detail": "ok",
                "payload": {},
            }
        ]


def _test_config(lock_path: Path, *, leader_lock_enabled: bool = True) -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.scheduler.leader_lock_enabled = leader_lock_enabled
    config.scheduler.leader_lock_stale_after_sec = 300
    config.scheduler.leader_lock_path = str(lock_path)
    config.command_channel.state_persist_path = str(lock_path.parent / "runtime_state.json")
    return config


def _fake_service() -> _FakeService:
    return cast(Any, _FakeService())


def test_run_once_executes_due_jobs_when_lock_free(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler_leader.lock"
    service = _fake_service()
    now = datetime.fromisoformat("2026-03-02T09:30:00")

    payload = run_once(service=service, config=_test_config(lock_path), now=now)

    assert payload["status"] == "ok"
    assert payload["leader"] is True
    assert service.run_calls == 1
    assert payload["executed"] == []
    assert payload["scheduler_state"] == {"jobs": 0}
    assert not lock_path.exists()


def test_run_once_skips_when_leader_lock_held(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler_leader.lock"
    holder = DistributedFileLock(lock_path)
    assert holder.acquire() is True
    try:
        service = _fake_service()
        payload = run_once(
            service=service,
            config=_test_config(lock_path),
            now=datetime.fromisoformat("2026-03-02T09:30:00"),
        )

        assert payload["status"] == "ok"
        assert payload["leader"] is False
        assert service.run_calls == 0
        assert lock_path.exists()
    finally:
        holder.release()


def test_run_once_releases_lock_after_execution(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler_leader.lock"
    service = _fake_service()

    payload = run_once(
        service=service,
        config=_test_config(lock_path),
        now=datetime.fromisoformat("2026-03-02T09:30:00"),
    )

    assert payload["leader"] is True
    assert not lock_path.exists()

    second = DistributedFileLock(lock_path)
    assert second.acquire() is True
    second.release()


def test_run_once_runs_unconditionally_when_lock_disabled(tmp_path: Path) -> None:
    lock_path = tmp_path / "scheduler_leader.lock"
    holder = DistributedFileLock(lock_path)
    assert holder.acquire() is True
    try:
        service = _fake_service()
        payload = run_once(
            service=service,
            config=_test_config(lock_path, leader_lock_enabled=False),
            now=datetime.fromisoformat("2026-03-02T09:30:00"),
        )

        assert payload["leader"] is True
        assert service.run_calls == 1
        assert lock_path.exists()
    finally:
        holder.release()
