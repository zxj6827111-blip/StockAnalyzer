"""Critical/heavy scheduler supervisor with isolated child processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4

from stock_analyzer.build_identity import get_build_manifest
from stock_analyzer.config import StockAnalyzerConfig, get_config
from stock_analyzer.ops.file_lock import DistributedFileLock
from stock_analyzer.runtime.service import StockAnalyzerService

CRITICAL_JOBS = frozenset(
    {
        "premarket_scan",
        "auction_report",
        "midday_news_brief",
        "close_reconcile",
    }
)
_VALID_GROUPS = frozenset({"critical", "heavy"})


def scheduler_group_for_job(job: str) -> str:
    return "critical" if str(job).strip() in CRITICAL_JOBS else "heavy"


def timeout_for_job(config: StockAnalyzerConfig, *, group: str, job: str) -> int:
    scheduler = config.scheduler
    configured = scheduler.job_timeout_sec
    if job in configured:
        return max(1, int(configured[job]))
    for family in ("week5", "evolution"):
        if job.startswith(f"{family}_") and family in configured:
            return max(1, int(configured[family]))
    default = (
        scheduler.default_critical_timeout_sec
        if group == "critical"
        else scheduler.default_heavy_timeout_sec
    )
    return max(1, int(default))


@dataclass(slots=True)
class _RunningJob:
    job: str
    run_id: str
    process: subprocess.Popen[Any]
    started_at: datetime
    started_monotonic: float
    timeout_sec: int
    result_path: Path
    log_path: Path
    log_handle: TextIO


class SchedulerSupervisor:
    def __init__(
        self,
        *,
        config: StockAnalyzerConfig,
        group: str,
        process_factory: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
        monotonic_fn: Callable[[], float] = time.monotonic,
        state_path: str | Path | None = None,
        heartbeat_path: str | Path | None = None,
        log_dir: str | Path | None = None,
        result_dir: str | Path | None = None,
    ) -> None:
        normalized_group = str(group).strip().lower()
        if normalized_group not in _VALID_GROUPS:
            raise ValueError(f"unsupported scheduler group: {group}")
        self.config = config
        self.group = normalized_group
        self.process_factory = process_factory
        self.monotonic_fn = monotonic_fn
        runtime_root = Path("artifacts/runtime")
        self.state_path = Path(
            state_path
            or os.getenv("SCHEDULER_STATE_PATH", "").strip()
            or runtime_root / f"scheduler_{self.group}_state.json"
        )
        self.heartbeat_path = Path(
            heartbeat_path
            or os.getenv("SCHEDULER_HEARTBEAT_PATH", "").strip()
            or runtime_root / f"scheduler_{self.group}_heartbeat.json"
        )
        self.log_dir = Path(
            log_dir
            or os.getenv("SCHEDULER_JOB_LOG_DIR", "").strip()
            or runtime_root / "scheduler_job_logs" / self.group
        )
        self.result_dir = Path(
            result_dir
            or os.getenv("SCHEDULER_JOB_RESULT_DIR", "").strip()
            or runtime_root / "scheduler_job_results" / self.group
        )
        self.running: dict[str, _RunningJob] = {}
        self.state = self._load_state()
        self._mark_interrupted_runs()

    @property
    def max_concurrency(self) -> int:
        value = (
            self.config.scheduler.critical_max_concurrency
            if self.group == "critical"
            else self.config.scheduler.heavy_max_concurrency
        )
        return max(1, int(value))

    def poll_once(
        self,
        *,
        service: StockAnalyzerService,
        now: datetime,
    ) -> dict[str, object]:
        reaped = self._reap_jobs(now=now)
        due_jobs: list[str] = []
        launched: list[str] = []
        leader = self._build_leader_lock()
        leader_acquired = leader is None or leader.acquire()
        try:
            if leader_acquired:
                due_jobs = [
                    job
                    for job in service.due_scheduler_jobs(now=now)
                    if scheduler_group_for_job(job) == self.group
                ]
                available = max(0, self.max_concurrency - len(self.running))
                for job in due_jobs:
                    if available <= 0:
                        break
                    if job in self.running or not self._retry_ready(job=job, now=now):
                        continue
                    if self._launch_job(job=job, now=now):
                        launched.append(job)
                        available -= 1
        finally:
            if leader is not None and leader_acquired:
                leader.release()
        heartbeat = {
            "timestamp": now.isoformat(),
            "status": "ok",
            "group": self.group,
            "leader": leader_acquired,
            "due": due_jobs,
            "launched": launched,
            "reaped": reaped,
            "running": [self._running_payload(item) for item in self.running.values()],
            "max_concurrency": self.max_concurrency,
            "scheduler_state": self._scheduler_state_payload(),
            "build": get_build_manifest(),
        }
        self.state["updated_at"] = now.isoformat()
        self.state["group"] = self.group
        self.state["running"] = heartbeat["running"]
        self._write_json_atomic(self.state_path, self.state)
        self._write_json_atomic(self.heartbeat_path, heartbeat)
        return heartbeat

    def shutdown(self) -> None:
        for item in list(self.running.values()):
            self._terminate_process(item)
            self._close_log(item)
        self.running.clear()

    def _build_leader_lock(self) -> DistributedFileLock | None:
        scheduler = self.config.scheduler
        if not scheduler.leader_lock_enabled:
            return None
        return DistributedFileLock(
            scheduler.leader_lock_path,
            stale_after_sec=max(1, int(scheduler.leader_lock_stale_after_sec)),
        )

    def _launch_job(self, *, job: str, now: datetime) -> bool:
        run_id = uuid4().hex
        safe_job = _safe_job_name(job)
        result_path = self.result_dir / f"{safe_job}.{run_id}.json"
        log_path = self.log_dir / f"{safe_job}.log"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("a", encoding="utf-8")
        command = [
            sys.executable,
            "-m",
            "stock_analyzer.runtime.scheduler_job_worker",
            "--job",
            job,
            "--run-id",
            run_id,
            "--now",
            now.isoformat(),
            "--result-path",
            str(result_path),
        ]
        try:
            process = self.process_factory(
                command,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=os.environ.copy(),
            )
        except Exception as exc:
            log_handle.close()
            self._record_launch_failure(job=job, run_id=run_id, now=now, detail=str(exc))
            return False
        timeout_sec = timeout_for_job(self.config, group=self.group, job=job)
        item = _RunningJob(
            job=job,
            run_id=run_id,
            process=process,
            started_at=now,
            started_monotonic=self.monotonic_fn(),
            timeout_sec=timeout_sec,
            result_path=result_path,
            log_path=log_path,
            log_handle=log_handle,
        )
        self.running[job] = item
        record = self._job_record(job)
        record.update(
            {
                "job": job,
                "group": self.group,
                "last_attempt": now.isoformat(),
                "last_attempt_at": now.isoformat(),
                "running_since": now.isoformat(),
                "heartbeat_at": now.isoformat(),
                "run_id": run_id,
                "status": "running",
                "timeout_sec": timeout_sec,
                "pid": process.pid,
                "log_path": str(log_path),
                "result_path": str(result_path),
            }
        )
        return True

    def _reap_jobs(self, *, now: datetime) -> list[dict[str, object]]:
        completed: list[dict[str, object]] = []
        for job, item in list(self.running.items()):
            return_code = item.process.poll()
            timed_out = (
                return_code is None
                and self.monotonic_fn() - item.started_monotonic >= item.timeout_sec
            )
            if timed_out:
                self._terminate_process(item)
                payload = {
                    "job": job,
                    "run_id": item.run_id,
                    "status": "expired",
                    "success": False,
                    "detail": f"timeout after {item.timeout_sec}s",
                    "completed_at": now.isoformat(),
                }
            elif return_code is None:
                self._job_record(job)["heartbeat_at"] = now.isoformat()
                continue
            else:
                payload = self._read_result(item.result_path)
                if not payload:
                    payload = {
                        "job": job,
                        "run_id": item.run_id,
                        "status": "failed" if return_code else "skipped",
                        "success": return_code == 0,
                        "detail": f"child exited with code {return_code} without result",
                        "completed_at": now.isoformat(),
                    }
            self._close_log(item)
            self._complete_job(item=item, payload=payload, now=now)
            self.running.pop(job, None)
            completed.append(payload)
        return completed

    def _terminate_process(self, item: _RunningJob) -> None:
        if item.process.poll() is not None:
            return
        item.process.terminate()
        grace = max(1, int(self.config.scheduler.process_terminate_grace_sec))
        try:
            item.process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            item.process.kill()
            item.process.wait(timeout=grace)

    def _complete_job(
        self,
        *,
        item: _RunningJob,
        payload: dict[str, object],
        now: datetime,
    ) -> None:
        record = self._job_record(item.job)
        status = str(payload.get("status", "failed")).strip() or "failed"
        detail = str(payload.get("detail", "")).strip()
        record.update(
            {
                "running_since": "",
                "heartbeat_at": now.isoformat(),
                "run_id": item.run_id,
                "status": status,
                "pid": None,
                "detail": detail,
                "completed_at": str(payload.get("completed_at", now.isoformat())),
            }
        )
        if status == "success":
            record["last_success"] = now.isoformat()
            record["last_success_at"] = now.isoformat()
            record["last_failure"] = ""
            record["last_failure_at"] = ""
            record["consecutive_failures"] = 0
            record["next_due_at"] = ""
        elif status in {"expired", "failed"}:
            if status == "expired":
                record["last_expired"] = now.isoformat()
            record["last_failure"] = detail
            record["last_failure_at"] = now.isoformat()
            self._schedule_retry(record=record, now=now)
        elif status == "skipped":
            record["next_due_at"] = (now + timedelta(minutes=1)).isoformat()

    def _record_launch_failure(
        self,
        *,
        job: str,
        run_id: str,
        now: datetime,
        detail: str,
    ) -> None:
        record = self._job_record(job)
        record.update(
            {
                "job": job,
                "group": self.group,
                "last_attempt": now.isoformat(),
                "last_attempt_at": now.isoformat(),
                "last_failure": detail,
                "last_failure_at": now.isoformat(),
                "running_since": "",
                "heartbeat_at": now.isoformat(),
                "run_id": run_id,
                "status": "failed",
            }
        )
        self._schedule_retry(record=record, now=now)

    def _retry_ready(self, *, job: str, now: datetime) -> bool:
        raw = str(self._job_record(job).get("next_due_at", "")).strip()
        if not raw:
            return True
        try:
            return now >= datetime.fromisoformat(raw)
        except ValueError:
            return True

    @staticmethod
    def _schedule_retry(*, record: dict[str, object], now: datetime) -> None:
        raw_failures = record.get("consecutive_failures", 0)
        try:
            failures = int(raw_failures) if isinstance(raw_failures, (int, float, str)) else 0
        except ValueError:
            failures = 0
        failures += 1
        backoff_minutes = min(30, 2 ** min(max(failures - 1, 0), 5))
        record["consecutive_failures"] = failures
        record["next_due_at"] = (now + timedelta(minutes=backoff_minutes)).isoformat()

    def _job_record(self, job: str) -> dict[str, object]:
        jobs = self.state.setdefault("jobs", {})
        if not isinstance(jobs, dict):
            jobs = {}
            self.state["jobs"] = jobs
        record = jobs.setdefault(job, {})
        if not isinstance(record, dict):
            record = {}
            jobs[job] = record
        return record

    def _load_state(self) -> dict[str, object]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"group": self.group, "jobs": {}, "running": []}
        if not isinstance(payload, dict):
            return {"group": self.group, "jobs": {}, "running": []}
        return payload

    def _mark_interrupted_runs(self) -> None:
        jobs = self.state.get("jobs")
        if not isinstance(jobs, dict):
            return
        now = datetime.now().isoformat()
        for raw in jobs.values():
            if not isinstance(raw, dict) or raw.get("status") != "running":
                continue
            raw["status"] = "failed"
            raw["last_failure"] = "supervisor_restarted_before_child_recovery"
            raw["last_failure_at"] = now
            raw["running_since"] = ""
            raw["heartbeat_at"] = now
            raw["pid"] = None

    @staticmethod
    def _read_result(path: Path) -> dict[str, object]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        with temp.open("w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, separators=(",", ":"), default=str)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(temp, path)

    @staticmethod
    def _close_log(item: _RunningJob) -> None:
        if not item.log_handle.closed:
            item.log_handle.close()

    def _scheduler_state_payload(self) -> dict[str, object]:
        jobs = self.state.get("jobs")
        if not isinstance(jobs, dict):
            return {"jobs": {}}
        return {
            "jobs": {
                str(name): deepcopy(value)
                for name, value in jobs.items()
                if isinstance(value, dict)
            }
        }

    @staticmethod
    def _running_payload(item: _RunningJob) -> dict[str, object]:
        return {
            "job": item.job,
            "run_id": item.run_id,
            "pid": item.process.pid,
            "started_at": item.started_at.isoformat(),
            "timeout_sec": item.timeout_sec,
            "result_path": str(item.result_path),
            "log_path": str(item.log_path),
        }


def _safe_job_name(job: str) -> str:
    return (
        "".join(
            character if character.isalnum() or character in {"-", "_", "."} else "_"
            for character in str(job).strip()
        )
        or "unnamed"
    )


def _poll_interval() -> int:
    raw = os.getenv("SCHEDULER_POLL_SEC", "30").strip()
    try:
        return max(5, int(raw))
    except ValueError:
        return 30


def run_supervisor(group: str) -> None:
    config = get_config()
    supervisor = SchedulerSupervisor(config=config, group=group)
    service: StockAnalyzerService | None = None
    interval_sec = _poll_interval()
    consecutive_failures = 0
    try:
        while True:
            now = datetime.now()
            try:
                if service is None:
                    service = StockAnalyzerService(config=config)
                payload = supervisor.poll_once(service=service, now=now)
                consecutive_failures = 0
                if payload["launched"] or payload["reaped"]:
                    print(json.dumps(payload, ensure_ascii=False, default=str), flush=True)
                time.sleep(interval_sec)
            except Exception as exc:
                consecutive_failures += 1
                service = None
                backoff = min(300, max(interval_sec, 2 ** min(consecutive_failures, 8)))
                payload = {
                    "timestamp": now.isoformat(),
                    "status": "error",
                    "group": group,
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                    "consecutive_failures": consecutive_failures,
                    "retry_in_sec": backoff,
                    "build": get_build_manifest(),
                }
                supervisor._write_json_atomic(supervisor.heartbeat_path, payload)
                print(json.dumps(payload, ensure_ascii=False, default=str), flush=True)
                time.sleep(backoff)
    finally:
        supervisor.shutdown()


def main() -> None:
    group = os.getenv("SCHEDULER_GROUP", "").strip().lower()
    if group not in _VALID_GROUPS:
        raise SystemExit("SCHEDULER_GROUP must be critical or heavy")
    run_supervisor(group)


if __name__ == "__main__":
    main()
