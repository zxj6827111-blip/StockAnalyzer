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


def main() -> None:
    interval_sec = _poll_interval()
    service: StockAnalyzerService | None = None
    consecutive_failures = 0
    while True:
        now = datetime.now()
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
