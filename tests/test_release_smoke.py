from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import stock_analyzer.ops.release_smoke as release_smoke_module
from stock_analyzer.ops.release_smoke import (
    SmokeEndpointSpec,
    default_smoke_endpoints,
    expect_json_keys,
    expect_text_contains,
    run_smoke_api,
)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json({"status": "ok"})
            return
        if self.path == "/ui/":
            self._send_text("<html>/ui/assets/index.js</html>", "text/html")
            return
        if self.path == "/dashboard":
            self._send_json({"summary": {}, "recent_trades": []})
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        if content_length > 0:
            self.rfile.read(content_length)
        if self.path == "/broker":
            self._send_json({"accepted": True})
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    def _send_json(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, body: str, content_type: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def test_release_smoke_runs_against_existing_server() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        report = run_smoke_api(
            base_url=base_url,
            start_server=False,
            specs=[
                SmokeEndpointSpec("health", "GET", "/health", validator=expect_json_keys("status")),
                SmokeEndpointSpec(
                    "ui",
                    "GET",
                    "/ui/",
                    validator=expect_text_contains("/ui/assets/"),
                ),
                SmokeEndpointSpec(
                    "broker",
                    "POST",
                    "/broker",
                    payload={"trace_id": "smoke"},
                    validator=expect_json_keys("accepted"),
                ),
            ],
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert report.ok is True
    assert len(report.failures) == 0
    assert all(item.ok for item in report.checks)


def test_release_smoke_reports_validation_failure() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        report = run_smoke_api(
            base_url=base_url,
            start_server=False,
            specs=[
                SmokeEndpointSpec(
                    "bad-health",
                    "GET",
                    "/health",
                    validator=expect_json_keys("missing"),
                )
            ],
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert report.ok is False
    assert report.failures


def test_default_smoke_endpoints_append_write_cleanup_steps() -> None:
    specs = default_smoke_endpoints(include_ui=False, include_write_checks=True)
    names = [item.name for item in specs]

    assert "broker_snapshot" in names
    assert "broker_snapshot_cleanup" in names
    assert "reconcile_run_cleanup" in names


def test_release_smoke_local_server_uses_safe_notification_env(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class _DummyProcess:
        pid = 43210

        def poll(self) -> int:
            return 0

        def terminate(self) -> None:
            return

        def wait(self, timeout: float | None = None) -> int:
            return 0

    def _fake_popen(*args: object, **kwargs: object) -> _DummyProcess:
        captured["args"] = kwargs.get("args", args[0] if args else [])
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        stdout_handle = kwargs.get("stdout")
        stderr_handle = kwargs.get("stderr")
        if stdout_handle is not None:
            stdout_handle.close()
        if stderr_handle is not None:
            stderr_handle.close()
        return _DummyProcess()

    monkeypatch.setattr(release_smoke_module.subprocess, "Popen", _fake_popen)

    _, stdout_path, stderr_path = release_smoke_module._start_local_server(
        project_root=tmp_path,
        host="127.0.0.1",
        port=8011,
    )

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["SA__NOTIFICATIONS__PRIMARY"] == "console"
    assert env["SA__NOTIFICATIONS__BACKUP"] == "console"
    assert env["SA__FEISHU_INTERACTION__ENABLED"] == "false"
    assert env["SA__WECOM_INTERACTION__ENABLED"] == "false"
    assert stdout_path.endswith(".stdout.log")
    assert stderr_path.endswith(".stderr.log")
