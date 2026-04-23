from __future__ import annotations

from fastapi.testclient import TestClient

from stock_analyzer.main import app


def test_idle_queue_run_endpoint_returns_report() -> None:
    client = TestClient(app)
    response = client.post("/idle/run", json={"now": "2026-03-02T20:40:00"})
    assert response.status_code == 200
    payload = response.json()
    assert "status" in payload


def test_idle_queue_latest_and_history_endpoints() -> None:
    client = TestClient(app)

    latest = client.get("/idle/latest")
    assert latest.status_code == 200
    latest_payload = latest.json()
    assert "report" in latest_payload

    history = client.get("/idle/history?limit=20")
    assert history.status_code == 200
    history_payload = history.json()
    assert "records" in history_payload
    assert "items" in history_payload

    state = client.get("/idle/state")
    assert state.status_code == 200
    state_payload = state.json()
    assert "enabled" in state_payload
    assert "blocked_tasks" in state_payload


def test_idle_queue_ack_endpoint_returns_payload() -> None:
    client = TestClient(app)
    response = client.post("/idle/ack", json={"task_id": "WD-P0-01"})
    assert response.status_code == 200
    payload = response.json()
    assert "status" in payload
