from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest

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


def test_resolve_now_uses_real_clock_when_override_unset(monkeypatch: Any) -> None:
    from stock_analyzer.runtime.scheduler_worker import _resolve_now

    monkeypatch.delenv("SCHEDULER_NOW_OVERRIDE", raising=False)
    before = datetime.now()
    resolved = _resolve_now()
    after = datetime.now()

    assert before <= resolved <= after


def test_resolve_now_honors_override_for_deterministic_replay(monkeypatch: Any) -> None:
    from stock_analyzer.runtime.scheduler_worker import _resolve_now

    monkeypatch.setenv("SCHEDULER_NOW_OVERRIDE", "2026-03-02T09:30:00")

    assert _resolve_now() == datetime.fromisoformat("2026-03-02T09:30:00")


def test_resolve_now_never_raises_on_malformed_override(monkeypatch: Any) -> None:
    """格式非法时必须退回真实时钟，绝不能抛错。

    scheduler_worker.main() 与 scheduler_supervisor.run_supervisor() 都在各自
    ``try`` 块**之外**调用 _resolve_now()，一旦这里抛 ValueError，进程会直接
    终止；配合容器的 restart: unless-stopped 就变成无限重启循环，日志里只有
    反复出现的裸栈回溯。真正负责报告这个拼写错误的是 validate_now_override()。
    """
    from stock_analyzer.runtime.scheduler_worker import _resolve_now

    monkeypatch.setenv("SCHEDULER_NOW_OVERRIDE", "not-a-datetime")
    before = datetime.now()
    resolved = _resolve_now()
    after = datetime.now()

    assert before <= resolved <= after


def test_resolve_now_strips_timezone_from_offset_aware_override(monkeypatch: Any) -> None:
    """带时区偏移的 override 必须规范化成 naive，避免下游比较抛 TypeError。

    默认分支返回的是 naive 的 datetime.now()，而 DailyScheduler / TimeGuard
    比较的也都是 naive datetime；混入 aware 值会在比较处直接
    ``TypeError: can't compare offset-naive and offset-aware datetimes``。
    """
    from stock_analyzer.runtime.scheduler_worker import _resolve_now

    monkeypatch.setenv("SCHEDULER_NOW_OVERRIDE", "2026-03-02T09:30:00+08:00")
    resolved = _resolve_now()

    assert resolved.tzinfo is None
    # 换算到本机时区后再去掉 tzinfo，与该时刻表示的绝对时间保持一致
    expected = datetime.fromisoformat("2026-03-02T09:30:00+08:00").astimezone().replace(tzinfo=None)
    assert resolved == expected


def test_validate_now_override_fails_fast_on_malformed_value(monkeypatch: Any) -> None:
    """启动阶段就把拼错的值变成一条可操作的错误信息，而不是留给循环去崩。"""
    from stock_analyzer.runtime.scheduler_worker import validate_now_override

    monkeypatch.setenv("SCHEDULER_NOW_OVERRIDE", "2026-03-02 09:30 CST")

    with pytest.raises(SystemExit) as excinfo:
        validate_now_override()

    message = str(excinfo.value)
    assert "SCHEDULER_NOW_OVERRIDE" in message
    # 错误信息必须包含实际拿到的非法值和一个可照抄的正确格式示例
    assert "2026-03-02 09:30 CST" in message
    assert "2026-08-28T21:45:00" in message


def test_validate_now_override_is_noop_when_unset(monkeypatch: Any, capsys: Any) -> None:
    """默认（未设置）路径必须完全静默，不打任何告警噪音。"""
    from stock_analyzer.runtime.scheduler_worker import validate_now_override

    monkeypatch.delenv("SCHEDULER_NOW_OVERRIDE", raising=False)
    validate_now_override()

    assert capsys.readouterr().out == ""


def test_validate_now_override_warns_loudly_when_clock_is_pinned(
    monkeypatch: Any, capsys: Any
) -> None:
    """时钟被钉死必须在日志里显著可见。

    否则运维看到"所有 job 都不触发"或"同一个 job 每轮重复执行"时，很难
    联想到是这个环境变量还留着没清掉。
    """
    from stock_analyzer.runtime.scheduler_worker import validate_now_override

    monkeypatch.setenv("SCHEDULER_NOW_OVERRIDE", "2026-03-02T09:30:00")
    validate_now_override()

    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "scheduler_now_override_active" in out
    assert "2026-03-02T09:30:00" in out
