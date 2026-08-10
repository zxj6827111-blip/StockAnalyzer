from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

import stock_analyzer.main as main_module
from stock_analyzer.ops.file_lock import DistributedFileLock


class _FakeService:
    def __init__(self) -> None:
        self.run_calls = 0

    def run_due_jobs(
        self,
        now: Any = None,
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


def _build_client(monkeypatch: Any, tmp_path: Path) -> tuple[TestClient, _FakeService]:
    config = main_module._config.model_copy(deep=True)
    config.scheduler.leader_lock_enabled = True
    config.scheduler.leader_lock_stale_after_sec = 300
    config.scheduler.leader_lock_path = str(tmp_path / "scheduler_leader.lock")
    monkeypatch.setattr(main_module, "_config", config)
    service = _FakeService()
    monkeypatch.setattr(main_module, "_service", service)
    return TestClient(main_module.app), service


def test_run_due_returns_409_when_leader_lock_held(monkeypatch: Any, tmp_path: Path) -> None:
    client, service = _build_client(monkeypatch, tmp_path)
    holder = DistributedFileLock(tmp_path / "scheduler_leader.lock")
    assert holder.acquire() is True
    try:
        response = client.post("/scheduler/run_due", json={})

        assert response.status_code == 409
        assert "another scheduler instance" in response.json()["detail"]
        assert service.run_calls == 0
    finally:
        holder.release()


def test_run_due_returns_results_when_lock_free(monkeypatch: Any, tmp_path: Path) -> None:
    client, service = _build_client(monkeypatch, tmp_path)

    response = client.post("/scheduler/run_due", json={})

    assert response.status_code == 200
    assert response.json() == {
        "results": [
            {
                "job": "fake_job",
                "ran": False,
                "success": True,
                "detail": "ok",
                "payload": {},
            }
        ]
    }
    assert service.run_calls == 1
    assert not (tmp_path / "scheduler_leader.lock").exists()


def test_run_due_runs_unconditionally_when_lock_disabled(monkeypatch: Any, tmp_path: Path) -> None:
    client, service = _build_client(monkeypatch, tmp_path)
    main_module._config.scheduler.leader_lock_enabled = False
    holder = DistributedFileLock(tmp_path / "scheduler_leader.lock")
    assert holder.acquire() is True
    try:
        response = client.post("/scheduler/run_due", json={})

        assert response.status_code == 200
        assert service.run_calls == 1
    finally:
        holder.release()
