"""Release smoke API runner."""

# mypy: disable-error-code=misc

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


JsonValidator = Callable[[object], tuple[bool, str]]


@dataclass(frozen=True)
class SmokeEndpointSpec:
    """One smoke endpoint definition."""

    name: str
    method: str
    path: str
    payload: dict[str, object] | None = None
    validator: JsonValidator | None = None


class SmokeCheckResult(_StrictModel):
    """Execution result for one smoke endpoint."""

    name: str
    method: str
    path: str
    ok: bool
    status_code: int
    duration_ms: int
    detail: str
    keys: list[str] = Field(default_factory=list)


class SmokeApiReport(_StrictModel):
    """Structured smoke-run report."""

    ok: bool
    started_at: datetime
    finished_at: datetime
    base_url: str
    started_local_server: bool
    process_id: int | None = None
    stdout_path: str = ""
    stderr_path: str = ""
    checks: list[SmokeCheckResult] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)


_SAFE_LOCAL_SERVER_ENV = {
    "SA__NOTIFICATIONS__PRIMARY": "console",
    "SA__NOTIFICATIONS__BACKUP": "console",
    "SA__NOTIFICATIONS__PUSHPLUS_TOKEN": "",
    "SA__NOTIFICATIONS__WECOM_WEBHOOK": "",
    "SA__NOTIFICATIONS__FEISHU_WEBHOOK": "",
    "SA__NOTIFICATIONS__FEISHU_APP_ID": "",
    "SA__NOTIFICATIONS__FEISHU_APP_SECRET": "",
    "SA__NOTIFICATIONS__FEISHU_APP_RECEIVE_ID": "",
    "SA__NOTIFICATIONS__FEISHU_APP_RECEIVE_ID_TYPE": "open_id",
    "SA__WECOM_INTERACTION__ENABLED": "false",
    "SA__FEISHU_INTERACTION__ENABLED": "false",
}


def run_smoke_api(
    *,
    base_url: str | None = None,
    project_root: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8011,
    start_server: bool = True,
    include_ui: bool = True,
    include_write_checks: bool = True,
    request_timeout_sec: float = 30.0,
    startup_timeout_sec: float = 45.0,
    specs: list[SmokeEndpointSpec] | None = None,
) -> SmokeApiReport:
    """Run release smoke endpoints against a live or spawned API server."""
    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parents[3]
    active_base_url = base_url or f"http://{host}:{port}"
    started_at = datetime.now()
    process: subprocess.Popen[str] | None = None
    stdout_path = ""
    stderr_path = ""
    if start_server:
        process, stdout_path, stderr_path = _start_local_server(
            project_root=root,
            host=host,
            port=port,
        )
        try:
            _wait_for_health(active_base_url, timeout_sec=startup_timeout_sec)
        except Exception:
            _stop_local_server(process)
            raise

    checks: list[SmokeCheckResult] = []
    failures: list[str] = []
    try:
        endpoint_specs = specs or default_smoke_endpoints(
            include_ui=include_ui,
            include_write_checks=include_write_checks,
        )
        for spec in endpoint_specs:
            result = _run_endpoint(
                base_url=active_base_url,
                spec=spec,
                timeout_sec=request_timeout_sec,
            )
            checks.append(result)
            if not result.ok:
                failures.append(f"{spec.name}: {result.detail}")
    finally:
        if process is not None:
            _stop_local_server(process)

    finished_at = datetime.now()
    return SmokeApiReport(
        ok=not failures,
        started_at=started_at,
        finished_at=finished_at,
        base_url=active_base_url,
        started_local_server=start_server,
        process_id=process.pid if process is not None else None,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        checks=checks,
        failures=failures,
    )


def default_smoke_endpoints(
    *,
    include_ui: bool,
    include_write_checks: bool,
) -> list[SmokeEndpointSpec]:
    """Build the default release smoke endpoint list."""
    trace_id = f"release-smoke-{int(time.time())}"
    specs = [
        SmokeEndpointSpec(
            name="health",
            method="GET",
            path="/health",
            validator=expect_json_keys("status"),
        ),
        SmokeEndpointSpec(
            name="dashboard_portfolio",
            method="GET",
            path="/dashboard/portfolio?days=7&trade_limit=20",
            validator=expect_json_keys("summary", "positions_panel", "recent_trades"),
        ),
        SmokeEndpointSpec(
            name="week5_latest",
            method="GET",
            path="/week5/scan/latest",
            validator=expect_json_any_keys("report", "status"),
        ),
        SmokeEndpointSpec(
            name="week6_latest",
            method="GET",
            path="/week6/latest",
            validator=expect_json_any_keys("report", "status"),
        ),
        SmokeEndpointSpec(
            name="reconcile_latest",
            method="GET",
            path="/portfolio/reconcile/latest",
            validator=expect_json_any_keys("report", "status"),
        ),
        SmokeEndpointSpec(
            name="week7_latest",
            method="GET",
            path="/week7/sim-broker/latest",
            validator=expect_json_any_keys("report", "status"),
        ),
    ]
    if include_ui:
        specs.insert(
            1,
            SmokeEndpointSpec(
                name="ui_index",
                method="GET",
                path="/ui/",
                validator=expect_text_contains("/ui/assets/"),
            ),
        )
    if include_write_checks:
        specs.extend(
            [
                SmokeEndpointSpec(
                    name="broker_snapshot",
                    method="POST",
                    path="/portfolio/broker_snapshot",
                    payload={
                        "positions": [
                            {
                                "symbol": "600000",
                                "target_position": 0.2,
                                "quantity": 200,
                                "account": "STAGING",
                            }
                        ],
                        "source_trace_id": trace_id,
                    },
                    validator=expect_json_object(),
                ),
                SmokeEndpointSpec(
                    name="reconcile_run",
                    method="POST",
                    path="/portfolio/reconcile/run",
                    payload={"now": "2026-03-13T04:10:00"},
                    validator=expect_json_keys("report"),
                ),
                SmokeEndpointSpec(
                    name="week7_run",
                    method="POST",
                    path="/week7/sim-broker/run",
                    payload={
                        "days": 7,
                        "notify_enabled": False,
                        "export_enabled": False,
                        "source_trace_id": f"{trace_id}-week7",
                    },
                    validator=expect_json_keys("status", "summary", "drilldown", "trend"),
                ),
                SmokeEndpointSpec(
                    name="broker_snapshot_cleanup",
                    method="POST",
                    path="/portfolio/broker_snapshot",
                    payload={
                        "positions": [],
                        "source_trace_id": f"{trace_id}-cleanup",
                    },
                    validator=expect_json_object(),
                ),
                SmokeEndpointSpec(
                    name="reconcile_run_cleanup",
                    method="POST",
                    path="/portfolio/reconcile/run",
                    payload={"now": "2026-03-13T04:11:00"},
                    validator=expect_json_keys("report"),
                ),
            ]
        )
    return specs


def expect_json_object() -> JsonValidator:
    """Validate that the response is a JSON object."""

    def _validator(value: object) -> tuple[bool, str]:
        if isinstance(value, dict):
            return True, "json object"
        return False, f"expected JSON object, got {type(value).__name__}"

    return _validator


def expect_json_keys(*keys: str) -> JsonValidator:
    """Validate that the response JSON contains all keys."""

    def _validator(value: object) -> tuple[bool, str]:
        if not isinstance(value, dict):
            return False, f"expected JSON object, got {type(value).__name__}"
        missing = [key for key in keys if key not in value]
        if missing:
            return False, f"missing keys {missing}"
        return True, "keys present"

    return _validator


def expect_json_any_keys(*keys: str) -> JsonValidator:
    """Validate that the response JSON contains at least one key."""

    def _validator(value: object) -> tuple[bool, str]:
        if not isinstance(value, dict):
            return False, f"expected JSON object, got {type(value).__name__}"
        if any(key in value for key in keys):
            return True, "any key present"
        return False, f"expected one of keys {list(keys)}"

    return _validator


def expect_text_contains(fragment: str) -> JsonValidator:
    """Validate that the response text contains the given fragment."""

    def _validator(value: object) -> tuple[bool, str]:
        if not isinstance(value, str):
            return False, f"expected text body, got {type(value).__name__}"
        if fragment in value:
            return True, "text fragment present"
        return False, f"expected fragment {fragment!r}"

    return _validator


def _start_local_server(
    *,
    project_root: Path,
    host: str,
    port: int,
) -> tuple[subprocess.Popen[str], str, str]:
    smoke_dir = project_root / "artifacts" / "release" / "smoke"
    smoke_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stdout_path = smoke_dir / f"uvicorn_{stamp}.stdout.log"
    stderr_path = smoke_dir / f"uvicorn_{stamp}.stderr.log"
    env = dict(os.environ)
    env.update(_SAFE_LOCAL_SERVER_ENV)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "stock_analyzer.main:app",
            "--app-dir",
            "src",
            "--host",
            host,
            "--port",
            str(port),
        ],
        cwd=project_root,
        stdout=stdout_path.open("w", encoding="utf-8"),
        stderr=stderr_path.open("w", encoding="utf-8"),
        text=True,
        env=env,
    )
    return process, str(stdout_path), str(stderr_path)


def _wait_for_health(base_url: str, *, timeout_sec: float) -> None:
    deadline = time.perf_counter() + timeout_sec
    last_error = ""
    while time.perf_counter() < deadline:
        try:
            request = urllib.request.Request(f"{base_url}/health", method="GET")
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status == 200:
                    return
        except Exception as exc:
            last_error = str(exc)
        time.sleep(1)
    raise RuntimeError(f"smoke startup timeout: {last_error}")


def _stop_local_server(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _run_endpoint(
    *,
    base_url: str,
    spec: SmokeEndpointSpec,
    timeout_sec: float,
) -> SmokeCheckResult:
    started = time.perf_counter()
    status_code = 0
    detail = "ok"
    keys: list[str] = []
    try:
        payload_bytes = (
            json.dumps(spec.payload, ensure_ascii=False).encode("utf-8")
            if spec.payload is not None
            else None
        )
        headers = {"Content-Type": "application/json"} if spec.payload is not None else {}
        request = urllib.request.Request(
            urllib.parse.urljoin(base_url, spec.path),
            data=payload_bytes,
            headers=headers,
            method=spec.method,
        )
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            status_code = response.status
            body = response.read()
            content_type = response.headers.get("Content-Type", "")
        parsed_body = _parse_response_body(body=body, content_type=content_type)
        if isinstance(parsed_body, dict):
            keys = sorted(str(key) for key in parsed_body.keys())
        validator = spec.validator
        if validator is not None:
            ok, detail = validator(parsed_body)
        else:
            ok, detail = True, "no validator configured"
    except urllib.error.HTTPError as exc:
        ok = False
        status_code = exc.code
        detail = f"http error {exc.code}"
    except Exception as exc:
        ok = False
        detail = str(exc)
    duration_ms = int((time.perf_counter() - started) * 1000)
    return SmokeCheckResult(
        name=spec.name,
        method=spec.method,
        path=spec.path,
        ok=ok,
        status_code=status_code,
        duration_ms=duration_ms,
        detail=detail,
        keys=keys,
    )


def _parse_response_body(*, body: bytes, content_type: str) -> object:
    text = body.decode("utf-8", errors="replace")
    if "application/json" not in content_type.lower():
        return text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text
