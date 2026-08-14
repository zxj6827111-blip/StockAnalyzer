"""Host-level watchdog for the scheduler container (P0 ops hardening).

``docker compose`` keeps the scheduler alive via ``restart: unless-stopped``,
but that policy cannot react when the container is running yet wedged (job
loop crashed, heartbeat writer dead) or when the container repeatedly exits
with OOM. This watchdog runs on the NAS host (cron / systemd every 5 min)
and:

* checks the container state (``docker inspect``), the heartbeat file
  ``scheduler_heartbeat.json`` (freshness of ``timestamp``) and the
  ``last_success_at`` of an enabled job inside the heartbeat payload;
* recreates the service with ``docker compose up -d --no-deps <service>``
  when the container is not running or the heartbeat is stale beyond
  ``max_heartbeat_age_sec`` (default 15 min);
* respects a maintenance flag file: when present, only logs and never
  restarts, keeping the manual maintenance safety gate intact;
* guards itself with a stale-aware file lock, a recovery cool-down
  (no repeated restarts within ``cool_down_sec``), result logging and a
  consecutive-failure alarm counter;
* ``--diagnose`` dumps ``docker inspect``, exit code / OOMKilled, host
  memory and docker daemon log excerpts for the NAS pre-acceptance
  evidence (is exit 137 OOM or external SIGKILL?).

Stdlib-only on purpose: the watchdog runs on the host, outside the image,
so it must not depend on the project's Python environment.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

DEFAULT_MAX_HEARTBEAT_AGE_SEC = 900  # 15 minutes
DEFAULT_COOL_DOWN_SEC = 600  # 10 minutes
DEFAULT_POLL_SEC = 300  # 5 minutes between daemon cycles
DEFAULT_CONTAINER_SERVICE = "scheduler"
DEFAULT_MAINTENANCE_FLAG = "artifacts/runtime/SCHEDULER_MAINTENANCE"
DEFAULT_LOG_PATH = "logs/scheduler_watchdog.jsonl"
DEFAULT_STATE_PATH = "logs/scheduler_watchdog_state.json"
DEFAULT_LOCK_PATH = "logs/scheduler_watchdog.lock"

_LOCK_STALE_AFTER_SEC = 3600  # watchdog cycles are short; a lock this old is abandoned


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _as_aware(value: datetime) -> datetime:
    """Normalize a possibly-naive datetime to local-timezone aware."""
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=datetime.now().astimezone().tzinfo)


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat()


def _parse_iso(value: object) -> datetime | None:
    """Parse ISO timestamps with or without timezone info into aware datetime."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # 状态/心跳文件可能由本脚本写入（带本地时区）或由旧版本写入（naive）；
        # naive 统一按本地时区解释，避免与 aware now 相减时报错。
        return parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


class SimpleFileLock:
    """Stale-aware O_EXCL file lock (stdlib, host-side).

    Mirrors the semantics of the in-image ``DistributedFileLock`` (atomic
    creation + mtime-based takeover) so concurrent cron invocations cannot
    double-restart the container.
    """

    def __init__(self, path: str | Path, *, stale_after_sec: float) -> None:
        self._path = Path(path)
        self._stale_after_sec = max(1.0, float(stale_after_sec))
        self._token = f"{os.getpid()}-{uuid4().hex}"

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(3):
            try:
                fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if not self._is_stale():
                    return False
                try:
                    self._path.unlink()
                except OSError:
                    return False
                continue
            except OSError:
                raise
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                json.dump(
                    {
                        "owner": self._token,
                        "pid": os.getpid(),
                        "created_at": _now_iso(),
                    },
                    fp,
                    ensure_ascii=False,
                )
            return True
        return False

    def release(self) -> None:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if str(payload.get("owner", "")) != self._token:
            return
        try:
            self._path.unlink()
        except OSError:
            pass

    def _is_stale(self) -> bool:
        try:
            stat = self._path.stat()
        except OSError:
            return False
        age = max(0.0, time.time() - stat.st_mtime)
        return age >= self._stale_after_sec


class WatchdogState:
    """Persisted recovery cool-down and consecutive-failure counters."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def load(self) -> dict[str, object]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def save(self, data: dict[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_name(f".{self._path.name}.{uuid4().hex}.tmp")
        with temp.open("w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(temp, self._path)

    def last_attempt_ts(self) -> datetime | None:
        return _parse_iso(self.load().get("last_attempt_at"))

    def record_attempt(self, *, ok: bool) -> int:
        """Record a recovery attempt; returns the consecutive-failure count.

        Both successful and failed attempts refresh ``last_attempt_at`` so
        the cool-down also covers failed restarts (no restart storm), while
        only failures increment the consecutive counter.
        """
        data = self.load()
        count = int(data.get("consecutive_failures", 0))
        if ok:
            count = 0
        else:
            count += 1
        data["last_attempt_at"] = _now_iso()
        data["consecutive_failures"] = count
        data["updated_at"] = _now_iso()
        self.save(data)
        return count

    def last_recovery_ts(self) -> datetime | None:
        return self.last_attempt_ts()

    def record_recovery(self) -> None:
        self.record_attempt(ok=True)

    def record_failure(self) -> int:
        return self.record_attempt(ok=False)


def _run_cmd(
    argv: list[str],
    *,
    cwd: str | None = None,
    timeout: float = 30.0,
) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout}s: {' '.join(argv)}"
    except FileNotFoundError:
        return -2, "", f"command not found: {argv[0]}"
    except OSError as exc:
        return -3, "", f"os error running {' '.join(argv)}: {exc}"


def read_heartbeat(path: str | Path) -> dict[str, object] | None:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def heartbeat_age_sec(heartbeat: dict[str, object] | None, *, now: datetime) -> float | None:
    """Age of the heartbeat ``timestamp`` field; None when unreadable/absent."""
    now = _as_aware(now)
    if heartbeat is None:
        return None
    ts = _parse_iso(heartbeat.get("timestamp"))
    if ts is None:
        return None
    return max(0.0, (now - ts).total_seconds())


def job_last_success_age_sec(
    heartbeat: dict[str, object] | None,
    job: str,
    *,
    now: datetime,
) -> float | None:
    """Age of ``scheduler_state.jobs[<job>].last_success_at`` inside the heartbeat."""
    now = _as_aware(now)
    if heartbeat is None:
        return None
    state = heartbeat.get("scheduler_state")
    if not isinstance(state, dict):
        return None
    jobs = state.get("jobs")
    if not isinstance(jobs, dict):
        return None
    runtime = jobs.get(job)
    if not isinstance(runtime, dict):
        return None
    ts = _parse_iso(runtime.get("last_success_at"))
    if ts is None:
        return None
    return max(0.0, (now - ts).total_seconds())


def inspect_container(container: str, *, docker_bin: str) -> dict[str, object]:
    """One-shot docker inspect; fields used by the watchdog and --diagnose."""
    fmt = (
        "{{.State.Running}}|{{.State.ExitCode}}|{{.State.OOMKilled}}"
        "|{{.State.Error}}|{{.State.Status}}|{{.RestartCount}}"
    )
    code, out, err = _run_cmd([docker_bin, "inspect", "-f", fmt, container])
    if code != 0 or not out:
        return {"present": False, "error": err or out, "running": False}
    parts = out.split("|")
    running = parts[0].strip().lower() == "true" if parts else False
    return {
        "present": True,
        "running": running,
        "exit_code": parts[1].strip() if len(parts) > 1 else "",
        "oom_killed": parts[2].strip().lower() == "true" if len(parts) > 2 else False,
        "state_error": parts[3].strip() if len(parts) > 3 else "",
        "state_status": parts[4].strip() if len(parts) > 4 else "",
        "restart_count": parts[5].strip() if len(parts) > 5 else "",
        "error": err,
    }


def recreate_service(
    *,
    compose_dir: str,
    service: str,
    container: str,
    docker_bin: str,
    compose_bin: str,
) -> tuple[bool, str]:
    """Recreate via ``docker compose up -d --no-deps <service>``.

    Falls back to ``docker start`` when compose itself is unavailable but the
    container exists. Returns (recovered, detail).
    """
    code, out, err = _run_cmd(
        [compose_bin, "up", "-d", "--no-deps", service],
        cwd=compose_dir,
        timeout=120.0,
    )
    if code == 0:
        return True, f"compose up ok: {out or err}"
    compose_fallback = _run_cmd(
        [docker_bin, "start", container],
        timeout=120.0,
    )
    if compose_fallback[0] == 0:
        return True, f"compose up failed ({err or out}); docker start ok"
    return False, f"compose up failed ({err or out}); docker start failed ({compose_fallback[2]})"


def _host_memory() -> dict[str, object]:
    code, out, _ = _run_cmd(["free", "-m"])
    if code == 0:
        return {"free_m_output": out}
    return {}


def _docker_daemon_logs(docker_bin: str) -> dict[str, object]:
    code, out, err = _run_cmd(
        [docker_bin, "events", "--since", "1h", "--until", "0s"],
        timeout=10.0,
    )
    if code == 0 and out:
        return {"daemon_events_sample": out.splitlines()[:40]}
    code2, out2, err2 = _run_cmd(["journalctl", "-u", "docker", "-n", "40", "--no-pager"])
    if code2 == 0 and out2:
        return {"journalctl_docker_sample": out2.splitlines()[:40]}
    return {"daemon_log_error": err or err2}


def _service_unhealthy(
    *,
    container_status: dict[str, object],
    heartbeat: dict[str, object] | None,
    heartbeat_age: float | None,
    max_age_sec: float,
) -> tuple[bool, str]:
    if not container_status.get("present"):
        return True, "container_missing"
    if not container_status.get("running"):
        return True, "container_not_running"
    if heartbeat_age is None:
        return True, "heartbeat_missing_or_unparseable"
    if heartbeat_age > max_age_sec:
        return True, f"heartbeat_stale_{int(heartbeat_age)}s"
    return False, ""


def run_once(
    *,
    now: datetime,
    compose_dir: str,
    service: str,
    container: str,
    heartbeat_path: str,
    max_age_sec: float,
    cool_down_sec: float,
    maintenance_flag: str,
    log_path: str,
    state: WatchdogState,
    docker_bin: str,
    compose_bin: str,
    key_job: str = "",
    key_job_max_age_sec: float = 0.0,
) -> dict[str, object]:
    """One watchdog cycle; returns the JSON report for the cycle log."""
    now = _as_aware(now)
    report: dict[str, object] = {
        "ts": now.isoformat(),
        "maintenance": False,
        "action": "noop",
        "container_name": container,
    }
    maintenance = Path(maintenance_flag).exists()
    report["maintenance"] = maintenance
    if maintenance:
        _append_log(log_path, report)
        return report

    heartbeat = read_heartbeat(heartbeat_path)
    heartbeat_age = heartbeat_age_sec(heartbeat, now=now)
    report["heartbeat_age_sec"] = heartbeat_age
    container_status = inspect_container(container, docker_bin=docker_bin)
    report["container_status"] = container_status
    unhealthy, reason = _service_unhealthy(
        container_status=container_status,
        heartbeat=heartbeat,
        heartbeat_age=heartbeat_age,
        max_age_sec=max_age_sec,
    )
    report["unhealthy"] = unhealthy
    report["unhealthy_reason"] = reason

    if key_job:
        job_age = job_last_success_age_sec(heartbeat, key_job, now=now)
        report[f"job_last_success_age_sec.{key_job}"] = job_age
        if job_age is not None and key_job_max_age_sec > 0 and job_age > key_job_max_age_sec:
            # 任务级陈旧只告警，不触发容器重建（重建由容器/心跳决定）。
            report["key_job_stale"] = True
            report["severity"] = "warn"
        else:
            report["key_job_stale"] = False

    if not unhealthy:
        report["severity"] = "info"
        _append_log(log_path, report)
        return report

    # 冷却：恢复尝试（无论成败）之间至少间隔 cool_down_sec，防止重启风暴。
    last_attempt = state.last_attempt_ts()
    if last_attempt is not None and (now - last_attempt).total_seconds() < cool_down_sec:
        report["action"] = "cooldown_skip"
        report["severity"] = "warn"
        _append_log(log_path, report)
        return report

    recovered, detail = recreate_service(
        compose_dir=compose_dir,
        service=service,
        container=container,
        docker_bin=docker_bin,
        compose_bin=compose_bin,
    )
    if recovered:
        state.record_recovery()
        report["action"] = "restart_ok"
        report["severity"] = "warn"
    else:
        failures = state.record_failure()
        report["action"] = "restart_failed"
        report["severity"] = "error"
        report["consecutive_failures"] = failures
    report["recovery_detail"] = detail
    _append_log(log_path, report)
    return report


def _append_log(path: str, payload: dict[str, object]) -> None:
    try:
        log = Path(path)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:  # pragma: no cover - host fs permission edge
        print(f"watchdog log write failed: {exc}", file=sys.stderr)


def diagnose(
    *,
    compose_dir: str,
    service: str,
    container: str,
    heartbeat_path: str,
    state_path: str,
    docker_bin: str,
    key_job: str = "",
) -> dict[str, object]:
    """Collect NAS pre-acceptance evidence (OOM vs external SIGKILL etc.)."""
    now = _as_aware(datetime.now())
    heartbeat = read_heartbeat(heartbeat_path)
    container_status = inspect_container(container, docker_bin=docker_bin)
    out: dict[str, object] = {
        "ts": now.isoformat(),
        "container": container,
        "compose_dir": compose_dir,
        "service": service,
        "heartbeat": heartbeat,
        "heartbeat_age_sec": heartbeat_age_sec(heartbeat, now=now),
        "container_status": container_status,
        "host_memory": _host_memory(),
        "daemon_logs": _docker_daemon_logs(docker_bin),
        "watchdog_state": WatchdogState(state_path).load(),
    }
    if key_job:
        out[f"job_last_success_age_sec.{key_job}"] = job_last_success_age_sec(
            heartbeat, key_job, now=now
        )
    # docker inspect 全量 JSON（exit code / OOMKilled 判定依据）。
    code, raw, err = _run_cmd([docker_bin, "inspect", container], timeout=30.0)
    if code == 0 and raw:
        try:
            out["docker_inspect_json"] = json.loads(raw)
        except json.JSONDecodeError:
            out["docker_inspect_json_error"] = raw[:2000]
    else:
        out["docker_inspect_json_error"] = err or "no inspect output"
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watchdog_scheduler",
        description="Host watchdog for the scheduler container (P0 ops hardening).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run a single check cycle and exit (cron style).",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run in a loop every --poll-sec seconds.",
    )
    parser.add_argument(
        "--poll-sec",
        type=float,
        default=_env_float("WATCHDOG_POLL_SEC", DEFAULT_POLL_SEC),
        help="Daemon cycle interval in seconds.",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Dump diagnostic evidence (docker inspect / OOM / memory / logs) as JSON.",
    )
    parser.add_argument(
        "--compose-dir",
        default=_env_str("WATCHDOG_COMPOSE_DIR", "."),
        help="Directory holding docker-compose.yml.",
    )
    parser.add_argument(
        "--service",
        default=_env_str("WATCHDOG_SERVICE", DEFAULT_CONTAINER_SERVICE),
        help="Compose service name to recreate.",
    )
    parser.add_argument(
        "--container",
        default=_env_str("WATCHDOG_CONTAINER", DEFAULT_CONTAINER_SERVICE),
        help="Docker container name (compose project prefixes usually apply).",
    )
    parser.add_argument(
        "--heartbeat-path",
        default=_env_str("WATCHDOG_HEARTBEAT_PATH", "artifacts/runtime/scheduler_heartbeat.json"),
        help="Path to scheduler_heartbeat.json.",
    )
    parser.add_argument(
        "--max-heartbeat-age-sec",
        type=float,
        default=_env_float("WATCHDOG_MAX_HEARTBEAT_AGE_SEC", DEFAULT_MAX_HEARTBEAT_AGE_SEC),
        help="Stale threshold for the heartbeat timestamp (default 900 = 15 min).",
    )
    parser.add_argument(
        "--cool-down-sec",
        type=float,
        default=_env_float("WATCHDOG_COOL_DOWN_SEC", DEFAULT_COOL_DOWN_SEC),
        help="Minimum seconds between recovery actions (default 600 = 10 min).",
    )
    parser.add_argument(
        "--maintenance-flag",
        default=_env_str("WATCHDOG_MAINTENANCE_FLAG", DEFAULT_MAINTENANCE_FLAG),
        help="Flag file that suppresses auto-restart while present.",
    )
    parser.add_argument(
        "--log-path",
        default=_env_str("WATCHDOG_LOG_PATH", DEFAULT_LOG_PATH),
        help="JSONL log file.",
    )
    parser.add_argument(
        "--state-path",
        default=_env_str("WATCHDOG_STATE_PATH", DEFAULT_STATE_PATH),
        help="Persisted cool-down / failure state file.",
    )
    parser.add_argument(
        "--lock-path",
        default=_env_str("WATCHDOG_LOCK_PATH", DEFAULT_LOCK_PATH),
        help="Watchdog file lock path.",
    )
    parser.add_argument(
        "--docker-bin",
        default=_env_str("WATCHDOG_DOCKER_BIN", "docker"),
        help="Docker binary name/path.",
    )
    parser.add_argument(
        "--compose-bin",
        default=_env_str("WATCHDOG_COMPOSE_BIN", "docker"),
        help="Compose binary (docker for 'docker compose', or docker-compose).",
    )
    parser.add_argument(
        "--key-job",
        default=_env_str("WATCHDOG_KEY_JOB", ""),
        help="Enabled job whose last_success_at is watched (warn only).",
    )
    parser.add_argument(
        "--key-job-max-age-sec",
        type=float,
        default=_env_float("WATCHDOG_KEY_JOB_MAX_AGE_SEC", 0.0),
        help="Stale threshold for the key job last_success_at (0 disables).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    lock = SimpleFileLock(args.lock_path, stale_after_sec=_LOCK_STALE_AFTER_SEC)

    if args.diagnose:
        out = diagnose(
            compose_dir=args.compose_dir,
            service=args.service,
            container=args.container,
            heartbeat_path=args.heartbeat_path,
            state_path=args.state_path,
            docker_bin=args.docker_bin,
            key_job=args.key_job,
        )
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0

    if not args.check and not args.daemon:
        args.check = True  # default to a single cycle

    def _cycle() -> None:
        if not lock.acquire():
            return
        try:
            run_once(
                now=datetime.now(),
                compose_dir=args.compose_dir,
                service=args.service,
                container=args.container,
                heartbeat_path=args.heartbeat_path,
                max_age_sec=args.max_heartbeat_age_sec,
                cool_down_sec=args.cool_down_sec,
                maintenance_flag=args.maintenance_flag,
                log_path=args.log_path,
                state=WatchdogState(args.state_path),
                docker_bin=args.docker_bin,
                compose_bin=args.compose_bin,
                key_job=args.key_job,
                key_job_max_age_sec=args.key_job_max_age_sec,
            )
        finally:
            lock.release()

    if args.check:
        _cycle()
        return 0

    poll = max(5.0, args.poll_sec)
    while True:  # pragma: no cover - daemon loop
        _cycle()
        time.sleep(poll)


if __name__ == "__main__":
    raise SystemExit(main())
