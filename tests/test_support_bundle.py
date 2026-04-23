from __future__ import annotations

import json
from pathlib import Path

from stock_analyzer.ops.support_bundle import collect_support_bundle


def test_collect_support_bundle_uses_host_runtime_state_and_redacts_env(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    runtime_dir = project_root / "artifacts" / "runtime"
    acceptance_dir = project_root / "artifacts" / "acceptance"
    config_dir = project_root / "config"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    acceptance_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)

    (project_root / "config" / "default.yaml").write_text("app: {}\n", encoding="utf-8")
    (project_root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (runtime_dir / "runtime_state.json").write_text(
        '{"system_stage":{"code":"night"},"scheduler_state":{"last_interval_slot":{"week5_live_runtime_1":"2026-03-18T14:35:00"}}}',
        encoding="utf-8",
    )
    (acceptance_dir / "week4_acceptance_latest.json").write_text(
        '{"overall":"pass"}',
        encoding="utf-8",
    )

    def fake_http(url: str, timeout_sec: float) -> dict[str, object]:
        assert timeout_sec == 3.0
        if url.endswith("/health"):
            return {"status": "ok", "provider": {"realtime_monitoring": {"cache_hits": 12}}}
        if url.endswith("/runtime/stage"):
            return {"system_stage": {"code": "night"}}
        if url.endswith("/acceptance/week4/latest"):
            return {"overall": "pass"}
        if url.endswith("/portfolio/reconcile/latest"):
            return {"status": "missing_snapshot"}
        raise AssertionError(url)

    def fake_command(command: list[str]) -> dict[str, object]:
        rendered = " ".join(command)
        if command[:4] == ["docker", "version", "--format", "{{.Server.Version}}"]:
            return {"returncode": 0, "stdout": "27.0.1\n", "stderr": ""}
        if command[:2] == ["docker", "inspect"]:
            env_items = [
                "SA__CACHE__BACKEND=redis",
                "SA__NOTIFICATIONS__WECOM_WEBHOOK=https://secret.example",
                "SCHEDULER_POLL_SEC=30",
            ]
            inspect_payload = [
                {
                    "Config": {
                        "Image": "stock-analyzer:latest",
                        "Env": env_items,
                    },
                    "State": {
                        "Running": True,
                        "Status": "running",
                        "StartedAt": "2026-03-18T10:00:00Z",
                        "FinishedAt": "0001-01-01T00:00:00Z",
                    },
                    "RestartCount": 0,
                    "Image": "sha256:test",
                    "Mounts": [
                        {
                            "Source": "/var/lib/docker/volumes/runtime",
                            "Destination": "/app/artifacts",
                            "Mode": "rw",
                            "RW": True,
                            "Type": "volume",
                        }
                    ],
                }
            ]
            return {
                "returncode": 0,
                "stdout": json.dumps(inspect_payload),
                "stderr": "",
            }
        if command[:4] == ["docker", "logs", "--tail", "50"]:
            return {"returncode": 0, "stdout": "line-1\nline-2\n", "stderr": ""}
        if (
            command[:4] == ["docker", "exec", "stock-analyzer-redis", "redis-cli"]
            and command[4:6] == ["INFO", "keyspace"]
        ):
            return {"returncode": 0, "stdout": "db0:keys=3,expires=3\n", "stderr": ""}
        if (
            command[:4] == ["docker", "exec", "stock-analyzer-redis", "redis-cli"]
            and command[4:6] == ["--scan", "--pattern"]
        ):
            return {
                "returncode": 0,
                "stdout": "runtime_realtime:600001.SH\nruntime_realtime:600519.SH\n",
                "stderr": "",
            }
        if "cat /app/artifacts/runtime/runtime_state.json" in rendered:
            return {"returncode": 1, "stdout": "", "stderr": "should not read container"}
        raise AssertionError(rendered)

    bundle = collect_support_bundle(
        project_root=project_root,
        base_url="http://127.0.0.1:8001",
        api_container="stock-analyzer-api",
        scheduler_container="stock-analyzer-scheduler",
        redis_container="stock-analyzer-redis",
        log_tail=50,
        timeout_sec=3.0,
        command_runner=fake_command,
        http_getter=fake_http,
    )

    assert bundle["summary"]["status"] == "ok"
    assert bundle["runtime_artifacts"]["runtime_state_source"] == "host_file"
    assert bundle["runtime_artifacts"]["runtime_state"]["system_stage"]["code"] == "night"
    assert bundle["docker"]["redis"]["runtime_realtime_key_count"] == 2
    tracked_env = bundle["docker"]["containers"]["api"]["tracked_env"]
    assert tracked_env["SA__CACHE__BACKEND"] == "redis"
    assert tracked_env["SA__NOTIFICATIONS__WECOM_WEBHOOK"] == "<redacted:22>"


def test_collect_support_bundle_marks_unavailable_runtime_endpoint_as_error(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / "config").mkdir(parents=True, exist_ok=True)
    (project_root / "config" / "default.yaml").write_text("app: {}\n", encoding="utf-8")

    def fake_http(url: str, timeout_sec: float) -> dict[str, object]:
        if url.endswith("/runtime/stage"):
            raise RuntimeError("connection refused")
        return {"status": "ok"}

    def fake_command(command: list[str]) -> dict[str, object]:
        if command[:4] == ["docker", "version", "--format", "{{.Server.Version}}"]:
            return {"returncode": 1, "stdout": "", "stderr": "docker missing"}
        raise AssertionError("docker should not be used without availability")

    bundle = collect_support_bundle(
        project_root=project_root,
        command_runner=fake_command,
        http_getter=fake_http,
    )

    assert bundle["docker"]["available"] is False
    assert bundle["summary"]["status"] == "error"
    assert "runtime stage endpoint unavailable" in bundle["summary"]["issues"]
