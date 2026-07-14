"""Export a support bundle for remote runtime diagnostics."""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CommandRunner = Callable[[Sequence[str]], dict[str, Any]]
HttpGetter = Callable[[str, float], dict[str, Any]]

DEFAULT_API_BASE_URL = "http://127.0.0.1:8001"
DEFAULT_API_CONTAINER = "stock-analyzer-api"
DEFAULT_SCHEDULER_CONTAINER = "stock-analyzer-scheduler"
DEFAULT_REDIS_CONTAINER = "stock-analyzer-redis"
DEFAULT_LOG_TAIL = 120
DEFAULT_TIMEOUT_SEC = 5.0

TRACKED_FILES = (
    ".env",
    "config/default.yaml",
    "docker-compose.yml",
    "docker-compose.runtime.yml",
    "docker-compose.runtime.localvol.yml",
)

HTTP_ENDPOINTS = {
    "health": "/health",
    "runtime_stage": "/runtime/stage",
    "acceptance_week4_latest": "/acceptance/week4/latest",
    "portfolio_reconcile_latest": "/portfolio/reconcile/latest",
}

SENSITIVE_ENV_MARKERS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "WEBHOOK",
    "CHAT_ID",
    "KEY",
)

TRACKED_ENV_PREFIXES = (
    "SA__",
    "STOCK_ANALYZER_",
)

TRACKED_ENV_NAMES = {
    "TZ",
    "SCHEDULER_POLL_SEC",
}


def export_support_bundle(
    *,
    project_root: str | Path | None = None,
    output_path: str | Path | None = None,
    base_url: str = DEFAULT_API_BASE_URL,
    api_container: str = DEFAULT_API_CONTAINER,
    scheduler_container: str = DEFAULT_SCHEDULER_CONTAINER,
    redis_container: str = DEFAULT_REDIS_CONTAINER,
    log_tail: int = DEFAULT_LOG_TAIL,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    mode: str = "host",
) -> Path:
    """Collect support diagnostics and write them to a JSON file."""
    root = _resolve_project_root(project_root)
    output = (
        Path(output_path)
        if output_path is not None
        else root / "artifacts" / "support" / "nas_support_bundle.json"
    )
    bundle = collect_support_bundle(
        project_root=root,
        base_url=base_url,
        api_container=api_container,
        scheduler_container=scheduler_container,
        redis_container=redis_container,
        log_tail=log_tail,
        timeout_sec=timeout_sec,
        mode=mode,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(bundle, indent=2, ensure_ascii=False), encoding="utf-8")
    return output


def collect_support_bundle(
    *,
    project_root: str | Path | None = None,
    base_url: str = DEFAULT_API_BASE_URL,
    api_container: str = DEFAULT_API_CONTAINER,
    scheduler_container: str = DEFAULT_SCHEDULER_CONTAINER,
    redis_container: str = DEFAULT_REDIS_CONTAINER,
    log_tail: int = DEFAULT_LOG_TAIL,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    command_runner: CommandRunner | None = None,
    http_getter: HttpGetter | None = None,
    mode: str = "host",
) -> dict[str, Any]:
    """Collect support diagnostics as a serializable mapping."""
    root = _resolve_project_root(project_root)
    run_command = command_runner or _run_command
    fetch_json = http_getter or _http_get_json
    base_url = base_url.rstrip("/")
    docker_version = _detect_docker_version(run_command)
    docker_available = bool(docker_version)
    normalized_mode = mode.strip().lower()
    if normalized_mode not in {"host", "container"}:
        raise ValueError("mode must be host or container")

    bundle: dict[str, Any] = {
        "schema_version": "nas-support-bundle.v2",
        "mode": normalized_mode,
        "generated_at": datetime.now(UTC).isoformat(),
        "project_root": str(root),
        "base_url": base_url,
        "host": _host_snapshot(root),
        "config_summary": _collect_safe_config_summary(root),
        "build_manifest": _read_json_file(root / "build_manifest.json"),
        "omissions": _mode_omissions(normalized_mode),
        "tracked_files": [
            _describe_file(root / relative_path) for relative_path in TRACKED_FILES
        ],
        "http": _collect_http_snapshots(
            base_url=base_url,
            timeout_sec=timeout_sec,
            fetch_json=fetch_json,
        ),
        "runtime_artifacts": _collect_runtime_artifacts(
            root=root,
            api_container=api_container,
            docker_available=docker_available,
            run_command=run_command,
        ),
        "docker": {
            "available": docker_available,
            "server_version": docker_version,
            "containers": {},
            "recent_logs": {},
            "redis": {},
        },
        "summary": {
            "status": "ok",
            "issues": [],
            "next_actions": [],
        },
    }

    if docker_available:
        containers = {
            "api": api_container,
            "scheduler": scheduler_container,
            "redis": redis_container,
        }
        for role, container_name in containers.items():
            bundle["docker"]["containers"][role] = _inspect_container(
                container_name=container_name,
                run_command=run_command,
            )
        bundle["docker"]["recent_logs"] = {
            "api": _tail_container_logs(
                api_container,
                log_tail=log_tail,
                run_command=run_command,
            ),
            "scheduler": _tail_container_logs(
                scheduler_container,
                log_tail=log_tail,
                run_command=run_command,
            ),
        }
        bundle["docker"]["redis"] = _collect_redis_details(
            redis_container=redis_container,
            run_command=run_command,
        )

    bundle["summary"] = _build_summary(bundle)
    return bundle


def _resolve_project_root(project_root: str | Path | None) -> Path:
    if project_root is not None:
        return Path(project_root)
    return Path(__file__).resolve().parents[3]


def _host_snapshot(root: Path) -> dict[str, Any]:
    usage = shutil.disk_usage(root)
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python_version": sys.version.split()[0],
        "disk": {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "free_ratio": round(usage.free / max(1, usage.total), 6),
        },
    }


def _describe_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "path": str(path),
            "exists": False,
        }
    raw = path.read_bytes()
    return {
        "path": str(path),
        "exists": True,
        "size_bytes": len(raw),
        "modified_at": datetime.fromtimestamp(
            path.stat().st_mtime,
            tz=UTC,
        ).isoformat(),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _collect_http_snapshots(
    *,
    base_url: str,
    timeout_sec: float,
    fetch_json: HttpGetter,
) -> dict[str, Any]:
    snapshots: dict[str, Any] = {}
    for name, route in HTTP_ENDPOINTS.items():
        url = f"{base_url}{route}"
        started = perf_counter()
        try:
            payload = fetch_json(url, timeout_sec)
            snapshots[name] = {
                "ok": True,
                "url": url,
                "latency_ms": round((perf_counter() - started) * 1000, 3),
                "payload": payload,
            }
        except Exception as exc:
            snapshots[name] = {
                "ok": False,
                "url": url,
                "latency_ms": round((perf_counter() - started) * 1000, 3),
                "error": str(exc),
            }
    return snapshots


def _collect_runtime_artifacts(
    *,
    root: Path,
    api_container: str,
    docker_available: bool,
    run_command: CommandRunner,
) -> dict[str, Any]:
    runtime_state_path = root / "artifacts" / "runtime" / "runtime_state.json"
    runtime_state_source = "host_file"
    runtime_state = _read_json_file(runtime_state_path)
    if runtime_state is None and docker_available:
        runtime_state_source = "container_file"
        runtime_state = _read_json_from_container(
            container_name=api_container,
            container_path="/app/artifacts/runtime/runtime_state.json",
            run_command=run_command,
        )

    acceptance_dir = root / "artifacts" / "acceptance"
    acceptance_reports = []
    if acceptance_dir.exists():
        for file_path in sorted(acceptance_dir.glob("*.json")):
            acceptance_reports.append(_describe_file(file_path))

    return {
        "runtime_state": runtime_state,
        "runtime_state_source": runtime_state_source if runtime_state is not None else "missing",
        "acceptance_reports": acceptance_reports,
        "scheduler_heartbeat": _read_json_file(
            root / "artifacts" / "runtime" / "scheduler_heartbeat.json"
        ),
        "runtime_files": _describe_runtime_files(root / "artifacts" / "runtime"),
    }


def _describe_runtime_files(runtime_dir: Path) -> list[dict[str, Any]]:
    if not runtime_dir.exists():
        return []
    return [_describe_file(path) for path in sorted(runtime_dir.glob("*.json*"))[:200]]


def _collect_safe_config_summary(root: Path) -> dict[str, Any]:
    env_path = root / ".env"
    env_keys: list[str] = []
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            env_keys.append(stripped.split("=", 1)[0].strip())
    compose_files = sorted(root.glob("docker-compose*.yml"))
    return {
        "env": {
            "exists": env_path.exists(),
            "key_names": sorted(set(env_keys)),
            "file": _describe_file(env_path),
            "values_included": False,
        },
        "compose": [_describe_file(path) for path in compose_files],
        "secrets_included": False,
    }


def _mode_omissions(mode: str) -> list[dict[str, str]]:
    if mode == "host":
        return []
    return [
        {"item": "host_docker_daemon", "reason": "container namespace may not expose socket"},
        {"item": "host_mount_layout", "reason": "container sees only mounted paths"},
        {"item": "host_disk_capacity", "reason": "reported capacity may be overlay filesystem"},
    ]


def _read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "_error": f"invalid json: {path}",
        }


def _read_json_from_container(
    *,
    container_name: str,
    container_path: str,
    run_command: CommandRunner,
) -> dict[str, Any] | None:
    result = run_command(
        [
            "docker",
            "exec",
            container_name,
            "sh",
            "-lc",
            f"cat {container_path}",
        ]
    )
    if result["returncode"] != 0 or not result["stdout"].strip():
        return None
    try:
        return json.loads(result["stdout"])
    except json.JSONDecodeError:
        return {
            "_error": f"invalid json from {container_name}:{container_path}",
        }


def _detect_docker_version(run_command: CommandRunner) -> str:
    result = run_command(["docker", "version", "--format", "{{.Server.Version}}"])
    if result["returncode"] != 0:
        return ""
    return result["stdout"].strip()


def _inspect_container(
    *,
    container_name: str,
    run_command: CommandRunner,
) -> dict[str, Any]:
    result = run_command(["docker", "inspect", container_name])
    if result["returncode"] != 0:
        return {
            "name": container_name,
            "ok": False,
            "error": _command_error(result),
        }
    try:
        payload = json.loads(result["stdout"])
    except json.JSONDecodeError:
        return {
            "name": container_name,
            "ok": False,
            "error": "docker inspect returned invalid JSON",
        }
    if not payload:
        return {
            "name": container_name,
            "ok": False,
            "error": "docker inspect returned no records",
        }
    record = payload[0]
    state = record.get("State") or {}
    config = record.get("Config") or {}
    mounts = record.get("Mounts") or []
    return {
        "name": container_name,
        "ok": True,
        "image": config.get("Image", ""),
        "image_id": record.get("Image", ""),
        "running": bool(state.get("Running")),
        "status": state.get("Status", ""),
        "started_at": state.get("StartedAt", ""),
        "finished_at": state.get("FinishedAt", ""),
        "restart_count": int(record.get("RestartCount", 0)),
        "mounts": [
            {
                "source": mount.get("Source", ""),
                "destination": mount.get("Destination", ""),
                "mode": mount.get("Mode", ""),
                "rw": bool(mount.get("RW", False)),
                "type": mount.get("Type", ""),
            }
            for mount in mounts
        ],
        "tracked_env": _extract_tracked_env(config.get("Env") or []),
    }


def _extract_tracked_env(env_items: list[str]) -> dict[str, str]:
    tracked: dict[str, str] = {}
    for item in env_items:
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        if not _is_tracked_env_name(name):
            continue
        tracked[name] = _redact_env_value(name=name, value=value)
    return dict(sorted(tracked.items()))


def _is_tracked_env_name(name: str) -> bool:
    if name in TRACKED_ENV_NAMES:
        return True
    return any(name.startswith(prefix) for prefix in TRACKED_ENV_PREFIXES)


def _redact_env_value(*, name: str, value: str) -> str:
    upper_name = name.upper()
    if any(marker in upper_name for marker in SENSITIVE_ENV_MARKERS):
        if not value:
            return ""
        return f"<redacted:{len(value)}>"
    return value


def _tail_container_logs(
    container_name: str,
    *,
    log_tail: int,
    run_command: CommandRunner,
) -> dict[str, Any]:
    result = run_command(
        [
            "docker",
            "logs",
            "--tail",
            str(log_tail),
            container_name,
        ]
    )
    if result["returncode"] != 0:
        return {
            "ok": False,
            "error": _command_error(result),
        }
    return {
        "ok": True,
        "tail": log_tail,
        "lines": result["stdout"].splitlines(),
    }


def _collect_redis_details(
    *,
    redis_container: str,
    run_command: CommandRunner,
) -> dict[str, Any]:
    info_result = run_command(
        [
            "docker",
            "exec",
            redis_container,
            "redis-cli",
            "INFO",
            "keyspace",
        ]
    )
    scan_result = run_command(
        [
            "docker",
            "exec",
            redis_container,
            "redis-cli",
            "--scan",
            "--pattern",
            "runtime_realtime:*",
        ]
    )
    if info_result["returncode"] != 0 and scan_result["returncode"] != 0:
        return {
            "ok": False,
            "error": _command_error(info_result) or _command_error(scan_result),
        }
    realtime_keys = [line for line in scan_result["stdout"].splitlines() if line.strip()]
    return {
        "ok": True,
        "keyspace": info_result["stdout"].strip(),
        "runtime_realtime_key_count": len(realtime_keys),
        "runtime_realtime_sample_keys": realtime_keys[:20],
    }


def _build_summary(bundle: dict[str, Any]) -> dict[str, Any]:
    issues: list[str] = []
    next_actions: list[str] = []

    health = bundle["http"].get("health", {})
    if not health.get("ok"):
        issues.append("health endpoint unavailable")
        next_actions.append("先确认 NAS 上 API 端口映射与容器状态。")

    runtime_stage = bundle["http"].get("runtime_stage", {})
    if not runtime_stage.get("ok"):
        issues.append("runtime stage endpoint unavailable")
        next_actions.append("检查调度容器是否存活，以及 runtime_state 是否可写。")

    acceptance = bundle["http"].get("acceptance_week4_latest", {})
    if acceptance.get("ok"):
        payload = acceptance.get("payload") or {}
        overall = str(payload.get("overall", "")).lower()
        if overall and overall not in {"pass", "passed"}:
            issues.append(f"week4 acceptance overall={payload.get('overall')}")
            next_actions.append(
                "结合 slow-report 与 runtime SLA 定位瓶颈，再决定是否需要发版。"
            )

    docker_snapshot = bundle.get("docker") or {}
    if docker_snapshot.get("available"):
        for role, container in (docker_snapshot.get("containers") or {}).items():
            if not container.get("ok"):
                issues.append(f"{role} container inspect failed")
                next_actions.append(
                    f"补查容器 `{container.get('name', role)}` 是否已创建。"
                )
                continue
            if not container.get("running"):
                issues.append(f"{role} container not running")
                next_actions.append(
                    f"查看 `{container.get('name', role)}` 最近日志并决定是否重启。"
                )
        redis_info = docker_snapshot.get("redis") or {}
        if redis_info.get("ok") and int(redis_info.get("runtime_realtime_key_count", 0)) == 0:
            next_actions.append(
                "若盘中实时链路需要诊断，开盘后再观察 "
                "runtime_realtime Redis key 是否开始滚动。"
            )

    runtime_artifacts = bundle.get("runtime_artifacts") or {}
    if runtime_artifacts.get("runtime_state") is None:
        issues.append("runtime_state.json missing")
        next_actions.append(
            "确认 `/app/artifacts/runtime/runtime_state.json` 是否落在持久卷。"
        )

    status = "ok"
    if issues:
        status = "degraded"
    if any("not running" in issue or "unavailable" in issue for issue in issues):
        status = "error"

    if not next_actions:
        next_actions.append(
            "支持包完整，可直接据此判断是代码问题、配置问题还是运行环境问题。"
        )

    return {
        "status": status,
        "issues": issues,
        "next_actions": next_actions,
    }


def _command_error(result: dict[str, Any]) -> str:
    stdout = str(result.get("stdout", "")).strip()
    stderr = str(result.get("stderr", "")).strip()
    if stderr:
        return stderr
    if stdout:
        return stdout
    return f"exit={result.get('returncode')}"


def _run_command(command: Sequence[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        return {
            "returncode": 127,
            "stdout": "",
            "stderr": str(exc),
        }
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _http_get_json(url: str, timeout_sec: float) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout_sec) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as exc:
        raise RuntimeError(f"{url} -> HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"{url} -> {exc.reason}") from exc
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{url} -> invalid json") from exc
