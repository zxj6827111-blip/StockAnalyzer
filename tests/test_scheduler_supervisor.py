from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.runtime.scheduler_supervisor import (
    SchedulerSupervisor,
    scheduler_group_for_job,
    timeout_for_job,
)
from stock_analyzer.runtime.service import StockAnalyzerService


class _FakeService:
    def __init__(self, due: list[str]) -> None:
        self.due = due
        self.calls: list[datetime] = []

    def due_scheduler_jobs(
        self,
        *,
        now: datetime | None = None,
        only_jobs: list[str] | None = None,
    ) -> list[str]:
        _ = only_jobs
        assert now is not None
        self.calls.append(now)
        return list(self.due)


class _FakeProcess:
    _next_pid = 1000

    def __init__(self) -> None:
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: int | None = None) -> int:
        _ = timeout
        assert self.returncode is not None
        return self.returncode


def _config(tmp_path: Path) -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.scheduler.leader_lock_enabled = False
    config.scheduler.critical_max_concurrency = 2
    config.scheduler.heavy_max_concurrency = 1
    config.scheduler.default_critical_timeout_sec = 900
    config.scheduler.default_heavy_timeout_sec = 10800
    config.scheduler.job_timeout_sec = {
        "premarket_scan": 900,
        "week5": 1800,
        "evolution": 10800,
    }
    config.scheduler.process_terminate_grace_sec = 1
    config.command_channel.state_persist_path = str(tmp_path / "runtime_state.json")
    return config


def _service(fake: _FakeService) -> StockAnalyzerService:
    return cast(StockAnalyzerService, cast(Any, fake))


def test_scheduler_group_and_family_timeouts(tmp_path: Path) -> None:
    config = _config(tmp_path)

    assert scheduler_group_for_job("premarket_scan") == "critical"
    assert scheduler_group_for_job("week5_live_runtime_1") == "heavy"
    assert timeout_for_job(config, group="critical", job="premarket_scan") == 900
    assert timeout_for_job(config, group="heavy", job="week5_live_runtime_1") == 1800
    assert timeout_for_job(config, group="heavy", job="evolution_offhours") == 10800


def test_critical_supervisor_launches_only_critical_jobs(tmp_path: Path) -> None:
    config = _config(tmp_path)
    processes: list[_FakeProcess] = []
    commands: list[list[str]] = []

    def _factory(command: list[str], **_: object) -> _FakeProcess:
        commands.append(command)
        process = _FakeProcess()
        processes.append(process)
        return process

    supervisor = SchedulerSupervisor(
        config=config,
        group="critical",
        process_factory=cast(Any, _factory),
        monotonic_fn=lambda: 0.0,
        state_path=tmp_path / "critical-state.json",
        heartbeat_path=tmp_path / "critical-heartbeat.json",
        log_dir=tmp_path / "logs",
        result_dir=tmp_path / "results",
    )
    fake = _FakeService(
        ["premarket_scan", "week5_live_runtime_1", "auction_report", "close_reconcile"]
    )
    payload = supervisor.poll_once(
        service=_service(fake),
        now=datetime.fromisoformat("2026-08-18T09:30:00"),
    )

    assert payload["launched"] == ["premarket_scan", "auction_report"]
    assert len(processes) == 2
    assert all("stock_analyzer.runtime.scheduler_job_worker" in command for command in commands)
    state = json.loads((tmp_path / "critical-state.json").read_text(encoding="utf-8"))
    assert state["jobs"]["premarket_scan"]["status"] == "running"
    assert state["jobs"]["premarket_scan"]["last_attempt_at"] == "2026-08-18T09:30:00"
    assert state["jobs"]["auction_report"]["status"] == "running"
    heartbeat = json.loads((tmp_path / "critical-heartbeat.json").read_text(encoding="utf-8"))
    assert heartbeat["scheduler_state"]["jobs"]["premarket_scan"]["status"] == "running"
    assert payload["scheduler_state"] == heartbeat["scheduler_state"]
    supervisor.shutdown()


def test_heavy_supervisor_times_out_child_and_backs_off(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.scheduler.job_timeout_sec["week5"] = 1
    clock = {"value": 0.0}
    processes: list[_FakeProcess] = []

    def _factory(command: list[str], **_: object) -> _FakeProcess:
        _ = command
        process = _FakeProcess()
        processes.append(process)
        return process

    supervisor = SchedulerSupervisor(
        config=config,
        group="heavy",
        process_factory=cast(Any, _factory),
        monotonic_fn=lambda: clock["value"],
        state_path=tmp_path / "heavy-state.json",
        heartbeat_path=tmp_path / "heavy-heartbeat.json",
        log_dir=tmp_path / "logs",
        result_dir=tmp_path / "results",
    )
    fake = _FakeService(["week5_live_runtime_1"])
    first = supervisor.poll_once(
        service=_service(fake),
        now=datetime.fromisoformat("2026-08-18T10:00:00"),
    )
    assert first["launched"] == ["week5_live_runtime_1"]

    clock["value"] = 2.0
    second = supervisor.poll_once(
        service=_service(fake),
        now=datetime.fromisoformat("2026-08-18T10:00:02"),
    )

    assert processes[0].terminated is True
    assert second["launched"] == []
    assert second["reaped"][0]["status"] == "expired"
    record = supervisor.state["jobs"]["week5_live_runtime_1"]
    assert record["status"] == "expired"
    assert record["last_expired"] == "2026-08-18T10:00:02"
    assert record["consecutive_failures"] == 1
    assert record["next_due_at"] == "2026-08-18T10:01:02"
