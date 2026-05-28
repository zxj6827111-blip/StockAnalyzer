from __future__ import annotations

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

import stock_analyzer.main as main_module
from stock_analyzer.config import SecurityConfig


def test_audit_endpoints_return_events_and_trace_replay(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(
        main_module._config,
        "security",
        SecurityConfig(
            api_auth_enabled=True,
            api_token="audit-token-12345",
            notify_test_enabled=True,
        ),
    )
    client = TestClient(main_module.app)
    trace_id = "main-audit-trace"

    notify_response = client.post(
        "/notify/test",
        json={
            "title": "audit-api-test",
            "content": "emit one notification event",
            "level": "info",
            "trace_id": trace_id,
        },
        headers={"Authorization": "Bearer audit-token-12345"},
    )
    assert notify_response.status_code == 200

    events_response = client.get(
        "/audit/events",
        params={"event_type": "notification", "limit": 50},
    )
    assert events_response.status_code == 200
    events_payload = events_response.json()
    assert events_payload["records"] >= 1

    replay_response = client.get(f"/audit/trace/{trace_id}")
    assert replay_response.status_code == 200
    replay_payload = replay_response.json()
    assert replay_payload["records"] >= 1
    assert replay_payload["summary"]["event_types"]["notification"] >= 1
