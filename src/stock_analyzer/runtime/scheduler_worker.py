"""Polling worker that executes due scheduler jobs continuously.

Only one scheduler instance runs ``run_due_jobs`` at a time: every poll
cycle first tries to take the scheduler-leader file lock (shared artifacts
volume) and skips the cycle when another replica holds it (audit P2-#20).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from stock_analyzer.build_identity import get_build_manifest
from stock_analyzer.config import StockAnalyzerConfig, get_config
from stock_analyzer.ops.file_lock import DistributedFileLock
from stock_analyzer.runtime.service import StockAnalyzerService

NOW_OVERRIDE_ENV = "SCHEDULER_NOW_OVERRIDE"


def _parse_now_override(raw: str) -> datetime:
    """Parse the override into a *naive* datetime (may raise ``ValueError``).

    ``datetime.fromisoformat`` returns an aware datetime whenever the input
    carries an offset (``2026-08-28T21:45:00+08:00``), while the default
    branch of :func:`_resolve_now` returns naive ``datetime.now()``. Feeding
    an aware value downstream would break comparisons against the naive
    datetimes ``DailyScheduler`` / ``TimeGuard`` already work with
    (``TypeError: can't compare offset-naive and offset-aware datetimes``),
    so an offset-carrying override is converted to local wall clock and
    stripped — the same normalization ``time_guard._now_in_market_timezone``
    applies for the identical reason.
    """
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def validate_now_override() -> None:
    """Fail fast on a malformed override, before entering any polling loop.

    Must be called *outside* the long-running ``while True`` loops. Those
    loops call :func:`_resolve_now` at the top of every iteration but
    **outside** their ``try`` block, so letting a ``ValueError`` escape from
    there would kill the process on every single poll: under the containers'
    ``restart: unless-stopped`` policy that degrades into an endless restart
    loop whose only symptom is a bare traceback repeating in the logs, with
    nothing pointing at the real cause (a typo in one environment variable).
    Validating once at startup turns that into a single actionable message.
    """
    raw = os.getenv(NOW_OVERRIDE_ENV, "").strip()
    if not raw:
        return
    try:
        parsed = _parse_now_override(raw)
    except ValueError as exc:
        raise SystemExit(
            f"{NOW_OVERRIDE_ENV}={raw!r} is not a valid ISO 8601 datetime ({exc}). "
            f"Unset it to use the real clock, or fix the format, "
            f"e.g. {NOW_OVERRIDE_ENV}=2026-08-28T21:45:00"
        ) from exc
    # 时钟被钉死是极不寻常的运行状态，必须在日志里显著可见：否则运维看到
    # "所有 job 都不触发" 或 "同一个 job 每轮重复执行" 时，很难联想到是这个
    # 环境变量还留着没清掉（例如从测试配置复制粘贴过来的 compose 覆盖）。
    print(
        json.dumps(
            {
                "level": "WARNING",
                "event": "scheduler_now_override_active",
                "override": parsed.isoformat(),
                "message": (
                    f"{NOW_OVERRIDE_ENV} is set: the scheduler clock is pinned and will "
                    "not advance. Intended for deterministic replay/tests only."
                ),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def _resolve_now() -> datetime:
    """Current wall clock, or a fixed override for deterministic replay/tests.

    Long-running worker loops (this ``main()`` and
    ``scheduler_supervisor.run_supervisor``) previously called
    ``datetime.now()`` directly on every poll iteration with no way to pin
    the clock from outside the process — unlike ``DailyScheduler.run_due``/
    ``due_job_names`` and the ``/scheduler/run_due`` API/CLI, which already
    accept an explicit ``now``. ``SCHEDULER_NOW_OVERRIDE`` (ISO 8601) closes
    that gap for the long-lived loop entrypoints without touching production
    behavior: unset (the default) keeps using the real clock.

    Never raises: callers invoke this outside their ``try`` block, so a
    malformed value must not be allowed to terminate the loop (see
    :func:`validate_now_override`, which is what actually reports the typo at
    startup). Reaching the fallback here means the value changed after that
    startup check, which should not happen for a container's fixed env.
    """
    raw = os.getenv(NOW_OVERRIDE_ENV, "").strip()
    if not raw:
        return datetime.now()
    try:
        return _parse_now_override(raw)
    except ValueError:
        return datetime.now()


def main() -> None:
    # 在任何长驻循环之前校验一次：两条分支（本函数的 while 与 run_supervisor
    # 的 while）都在 try 之外调用 _resolve_now()，把格式错误留到循环里会变成
    # 无限重启循环。这里是生产的唯一入口，校验一次即覆盖两条路径。
    validate_now_override()
    group = os.getenv("SCHEDULER_GROUP", "").strip().lower()
    if group in {"critical", "heavy"}:
        from stock_analyzer.runtime.scheduler_supervisor import run_supervisor

        run_supervisor(group)
        return
    interval_sec = _poll_interval()
    service: StockAnalyzerService | None = None
    consecutive_failures = 0
    while True:
        now = _resolve_now()
        try:
            config = get_config()
            if service is None:
                service = StockAnalyzerService(config=config)
            payload = run_once(service=service, config=config, now=now)
            consecutive_failures = 0
            _write_heartbeat(payload)
            executed = payload.get("executed")
            if isinstance(executed, list) and executed:
                print(json.dumps(payload, ensure_ascii=False))
            time.sleep(interval_sec)
        except Exception as exc:
            consecutive_failures += 1
            service = None
            backoff_sec = min(300, max(interval_sec, 2 ** min(consecutive_failures, 8)))
            payload = {
                "timestamp": now.isoformat(),
                "status": "error",
                "error_type": exc.__class__.__name__,
                "error": str(exc),
                "consecutive_failures": consecutive_failures,
                "retry_in_sec": backoff_sec,
                "build": get_build_manifest(),
            }
            _write_heartbeat(payload)
            print(json.dumps(payload, ensure_ascii=False), flush=True)
            time.sleep(backoff_sec)


def run_once(
    *,
    service: StockAnalyzerService,
    config: StockAnalyzerConfig,
    now: datetime,
) -> dict[str, object]:
    """Run one poll cycle: take the leader lock, run due jobs, release.

    Returns the heartbeat payload for the cycle.  When another scheduler
    instance holds the leader lock the cycle is skipped (``leader`` False,
    ``run_due_jobs`` never invoked) instead of executing in parallel.
    """
    lock = _build_leader_lock(config)
    if lock is not None and not lock.acquire():
        return {
            "timestamp": now.isoformat(),
            "status": "ok",
            "leader": False,
            "executed": [],
            "build": get_build_manifest(),
        }
    try:
        results = service.run_due_jobs(now=now)
        executed = [item for item in results if bool(item.get("ran", False))]
        return {
            "timestamp": now.isoformat(),
            "status": "ok",
            "leader": True,
            "executed": executed,
            "scheduler_state": service._scheduler.export_state(),
            "build": get_build_manifest(),
        }
    finally:
        if lock is not None:
            lock.release()


def _build_leader_lock(config: StockAnalyzerConfig) -> DistributedFileLock | None:
    scheduler_config = config.scheduler
    if not scheduler_config.leader_lock_enabled:
        return None
    return DistributedFileLock(
        scheduler_config.leader_lock_path,
        stale_after_sec=scheduler_config.leader_lock_stale_after_sec,
    )


def _write_heartbeat(payload: dict[str, object]) -> None:
    path = Path(
        os.getenv(
            "SCHEDULER_HEARTBEAT_PATH",
            "artifacts/runtime/scheduler_heartbeat.json",
        )
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temp.open("w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, separators=(",", ":"), default=str)
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(temp, path)


def _poll_interval() -> int:
    raw = os.getenv("SCHEDULER_POLL_SEC", "30").strip()
    try:
        return max(5, int(raw))
    except ValueError:
        return 30


if __name__ == "__main__":
    main()
