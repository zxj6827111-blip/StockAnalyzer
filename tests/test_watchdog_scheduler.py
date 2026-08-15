"""Tests for the host-level scheduler watchdog (P0 ops hardening)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from scripts.watchdog_scheduler import (
    SimpleFileLock,
    WatchdogState,
    heartbeat_age_sec,
    job_last_success_age_sec,
    run_once,
)


def _heartbeat(ts: str, *, job_last_success_at: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {"timestamp": ts, "status": "ok", "leader": True}
    jobs: dict[str, object] = {}
    if job_last_success_at is not None:
        jobs["premarket_scan"] = {"last_success_at": job_last_success_at}
    payload["scheduler_state"] = {"jobs": jobs}
    return payload


def _container_status(
    *,
    running: bool = True,
    present: bool = True,
) -> dict[str, object]:
    return {
        "present": present,
        "running": running,
        "exit_code": "0" if running else "137",
        "oom_killed": False,
        "state_error": "",
        "state_status": "running" if running else "exited",
        "restart_count": "0",
        "error": "",
    }


def _default_kwargs(
    tmp_path: Path,
    *,
    heartbeat: dict[str, object] | None,
    now: datetime,
    container: dict[str, object] | None = None,
) -> dict[str, object]:
    hb_path = tmp_path / "scheduler_heartbeat.json"
    if heartbeat is not None:
        hb_path.write_text(json.dumps(heartbeat), encoding="utf-8")
    return {
        "now": now,
        "compose_dir": str(tmp_path),
        "service": "scheduler",
        "container": "scheduler",
        "heartbeat_path": str(hb_path),
        "max_age_sec": 900.0,
        "cool_down_sec": 600.0,
        "maintenance_flag": str(tmp_path / "SCHEDULER_MAINTENANCE"),
        "log_path": str(tmp_path / "watchdog.jsonl"),
        "state": WatchdogState(str(tmp_path / "state.json")),
        "docker_bin": "docker",
        "compose_bin": "docker",
        "key_job": "",
        "key_job_max_age_sec": 0.0,
    }


def test_heartbeat_age_parses_and_ages(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, 10, 0, 0)
    hb = _heartbeat("2026-08-14T09:45:00")
    assert heartbeat_age_sec(hb, now=now) == pytest.approx(900.0)
    assert heartbeat_age_sec(None, now=now) is None
    assert heartbeat_age_sec({"timestamp": "garbage"}, now=now) is None
    assert heartbeat_age_sec({}, now=now) is None


def test_job_last_success_age_sec_reads_heartbeat_jobs(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, 10, 0, 0)
    hb = _heartbeat("2026-08-14T09:58:00", job_last_success_at="2026-08-14T08:30:00")
    assert job_last_success_age_sec(hb, "premarket_scan", now=now) == pytest.approx(5400.0)
    assert job_last_success_age_sec(hb, "missing_job", now=now) is None
    assert job_last_success_age_sec(None, "premarket_scan", now=now) is None


def test_healthy_cycle_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 8, 14, 10, 0, 0)
    kwargs = _default_kwargs(
        tmp_path,
        heartbeat=_heartbeat("2026-08-14T09:59:00"),
        now=now,
    )
    monkeypatch.setattr(
        "scripts.watchdog_scheduler.inspect_container",
        lambda *a, **kw: _container_status(),
    )
    report = run_once(**kwargs)  # type: ignore[arg-type]
    assert report["unhealthy"] is False
    assert report["action"] == "noop"
    assert (tmp_path / "watchdog.jsonl").exists()


def test_maintenance_flag_suppresses_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 14, 10, 0, 0)
    (tmp_path / "SCHEDULER_MAINTENANCE").write_text("manual maintenance", encoding="utf-8")
    kwargs = _default_kwargs(
        tmp_path,
        heartbeat=_heartbeat("2026-08-14T09:00:00"),  # stale
        now=now,
    )
    restarts: list[str] = []

    def _fake_recreate(**kw: object) -> tuple[bool, str]:
        restarts.append("called")
        return True, "ok"

    monkeypatch.setattr("scripts.watchdog_scheduler.recreate_service", _fake_recreate)
    report = run_once(**kwargs)  # type: ignore[arg-type]
    assert report["maintenance"] is True
    assert report["action"] == "noop"
    assert restarts == []


def test_stale_heartbeat_triggers_restart_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 14, 10, 0, 0)
    kwargs = _default_kwargs(
        tmp_path,
        heartbeat=_heartbeat("2026-08-14T09:30:00"),  # 30 min stale > 15 min
        now=now,
    )
    monkeypatch.setattr(
        "scripts.watchdog_scheduler.inspect_container",
        lambda *a, **kw: _container_status(running=True),
    )
    monkeypatch.setattr(
        "scripts.watchdog_scheduler.recreate_service",
        lambda **kw: (True, "compose up ok"),
    )
    report = run_once(**kwargs)  # type: ignore[arg-type]
    assert report["unhealthy"] is True
    assert report["unhealthy_reason"].startswith("heartbeat_stale")
    assert report["action"] == "restart_ok"
    state = WatchdogState(str(tmp_path / "state.json")).load()
    assert state["consecutive_failures"] == 0
    assert "last_attempt_at" in state


def test_container_not_running_triggers_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 14, 10, 0, 0)
    kwargs = _default_kwargs(
        tmp_path,
        heartbeat=_heartbeat("2026-08-14T09:59:00"),  # fresh heartbeat but container dead
        now=now,
    )
    monkeypatch.setattr(
        "scripts.watchdog_scheduler.inspect_container",
        lambda *a, **kw: _container_status(running=False),
    )
    monkeypatch.setattr(
        "scripts.watchdog_scheduler.recreate_service",
        lambda **kw: (True, "compose up ok"),
    )
    report = run_once(**kwargs)  # type: ignore[arg-type]
    assert report["unhealthy"] is True
    assert report["unhealthy_reason"] == "container_not_running"
    assert report["action"] == "restart_ok"


def test_cool_down_skips_repeat_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 14, 10, 0, 0)
    state = WatchdogState(str(tmp_path / "state.json"))
    state.save({"last_attempt_at": (now - timedelta(seconds=120)).isoformat()})
    kwargs = _default_kwargs(
        tmp_path,
        heartbeat=_heartbeat("2026-08-14T09:30:00"),
        now=now,
    )
    kwargs["state"] = state
    restarts: list[str] = []
    monkeypatch.setattr(
        "scripts.watchdog_scheduler.inspect_container",
        lambda *a, **kw: _container_status(running=False),
    )
    monkeypatch.setattr(
        "scripts.watchdog_scheduler.recreate_service",
        lambda **kw: restarts.append("called") or (True, "ok"),
    )
    report = run_once(**kwargs)  # type: ignore[arg-type]
    assert report["action"] == "cooldown_skip"
    assert restarts == []


def test_restart_failure_increments_counter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 14, 10, 0, 0)
    kwargs = _default_kwargs(
        tmp_path,
        heartbeat=_heartbeat("2026-08-14T09:30:00"),
        now=now,
    )
    monkeypatch.setattr(
        "scripts.watchdog_scheduler.inspect_container",
        lambda *a, **kw: _container_status(running=False),
    )
    monkeypatch.setattr(
        "scripts.watchdog_scheduler.recreate_service",
        lambda **kw: (False, "compose up failed"),
    )
    report = run_once(**kwargs)  # type: ignore[arg-type]
    assert report["action"] == "restart_failed"
    assert report["consecutive_failures"] == 1
    report2 = run_once(**kwargs)  # type: ignore[arg-type]
    # 冷却期内第二次直接 cooldown_skip，连续失败计数保持不变
    assert report2["action"] == "cooldown_skip"


def test_key_job_stale_warns_but_does_not_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 14, 10, 0, 0)
    kwargs = _default_kwargs(
        tmp_path,
        heartbeat=_heartbeat("2026-08-14T09:59:00", job_last_success_at="2026-08-12T08:30:00"),
        now=now,
    )
    kwargs["key_job"] = "premarket_scan"
    kwargs["key_job_max_age_sec"] = 86400.0  # > 2 days stale
    restarts: list[str] = []
    monkeypatch.setattr(
        "scripts.watchdog_scheduler.inspect_container",
        lambda *a, **kw: _container_status(running=True),
    )
    monkeypatch.setattr(
        "scripts.watchdog_scheduler.recreate_service",
        lambda **kw: restarts.append("called") or (True, "ok"),
    )
    report = run_once(**kwargs)  # type: ignore[arg-type]
    assert report["unhealthy"] is False  # 心跳新鲜，不触发重建
    assert report["key_job_stale"] is True
    assert report["action"] == "noop"
    assert restarts == []


def test_simple_file_lock_excludes_concurrent_holder(tmp_path: Path) -> None:
    lock_path = tmp_path / "watchdog.lock"
    first = SimpleFileLock(lock_path, stale_after_sec=3600.0)
    second = SimpleFileLock(lock_path, stale_after_sec=3600.0)
    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


def test_simple_file_lock_takes_over_stale(tmp_path: Path) -> None:
    lock_path = tmp_path / "watchdog.lock"
    first = SimpleFileLock(lock_path, stale_after_sec=0.5)
    assert first.acquire() is True
    first.release()
    # 模拟遗留陈旧锁：直接创建文件并回拨 mtime
    lock_path.write_text("{}", encoding="utf-8")
    old = datetime.now().timestamp() - 3600.0
    import os

    os.utime(lock_path, (old, old))
    second = SimpleFileLock(lock_path, stale_after_sec=1.0)
    assert second.acquire() is True
    second.release()
