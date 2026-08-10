from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from stock_analyzer.evolution.m3_vector_profile import (
    build_default_m3_vector_profile,
    build_m3_vector_from_record,
)
from stock_analyzer.main import app


def _run_task_and_get_result(
    client: TestClient,
    url: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """POST a heavy endpoint (202 + task_id), then fetch the recorded result."""
    response = client.post(url, json=payload)
    assert response.status_code == 202, f"{url} -> {response.status_code}: {response.text}"
    body = response.json()
    assert body["status"] == "queued", url
    task_response = client.get(f"/tasks/{body['task_id']}")
    assert task_response.status_code == 200
    task_payload = task_response.json()
    assert task_payload["status"] == "succeeded", f"{url}: {task_payload.get('error')}"
    return task_payload["result"]


def test_evolution_endpoints_run_latest_and_history() -> None:
    client = TestClient(app)

    run_payload = _run_task_and_get_result(
        client,
        "/evolution/run",
        {
            "symbols": ["600000", "000001"],
            "dry_run": True,
            "now": "2026-03-02T20:40:00",
            "source_trace_id": "api-evo-run",
        },
    )
    assert "proposal" in run_payload
    assert run_payload["dry_run"] is True
    assert "m4" in run_payload["modules"]
    assert "M4" in run_payload["dag"]["module_scores"]
    assert "m5" in run_payload["modules"]
    assert "M5" in run_payload["dag"]["module_scores"]
    assert "m6" in run_payload["modules"]
    assert "M6" in run_payload["dag"]["module_scores"]
    assert "m7" in run_payload["modules"]
    assert "M7" in run_payload["dag"]["module_scores"]
    assert "m8" in run_payload["modules"]
    assert "M8" in run_payload["dag"]["module_scores"]

    latest_response = client.get("/evolution/latest")
    assert latest_response.status_code == 200
    latest_payload = latest_response.json()
    assert "report" in latest_payload

    history_response = client.get("/evolution/history", params={"limit": 10})
    assert history_response.status_code == 200
    history_payload = history_response.json()
    assert history_payload["records"] >= 1


def test_evolution_drill_endpoint() -> None:
    client = TestClient(app)
    response = client.post(
        "/evolution/drill",
        json={"now": "2026-03-02T20:41:00", "source_trace_id": "api-evo-drill"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["dry_run"] is True
    assert "m9" in payload


def test_evolution_preflight_endpoint() -> None:
    client = TestClient(app)
    response = client.get("/evolution/preflight")
    assert response.status_code == 200
    payload = response.json()
    assert "ready" in payload
    assert "dependency" in payload
    assert "path_checks" in payload


def test_evolution_window_report_endpoint() -> None:
    client = TestClient(app)
    response = client.get("/evolution/window_report", params={"days": 10, "min_runs": 1})
    assert response.status_code == 200
    payload = response.json()
    assert "overall" in payload
    assert "checks" in payload


def test_evolution_m3_maintenance_endpoint() -> None:
    client = TestClient(app)
    response = client.post(
        "/evolution/m3/maintenance",
        json={"now": "2026-03-02T21:00:00", "source_trace_id": "api-m3-maintain"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert "purged_count" in payload
    assert "purged" in payload


def test_evolution_m3_search_endpoint() -> None:
    client = TestClient(app)
    seed_payload = _run_task_and_get_result(
        client,
        "/evolution/run",
        {
            "symbols": ["600000", "000001"],
            "dry_run": True,
            "now": "2026-03-02T20:42:00",
            "source_trace_id": "api-evo-seed-for-m3",
        },
    )
    assert seed_payload["dry_run"] is True
    vector = build_m3_vector_from_record(
        {
            "open": 10.0,
            "high": 10.2,
            "low": 9.9,
            "close": 10.1,
            "volume": 14.0,
        },
        vector_profile=build_default_m3_vector_profile(),
        regime_state="range",
    )

    response = client.post(
        "/evolution/m3/search",
        json={
            "vector": vector,
            "top_k": 3,
            "source_trace_id": "api-m3-search",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert "indices" in payload
    assert "scores" in payload


def test_evolution_m8_suggest_endpoint() -> None:
    client = TestClient(app)
    _ = client.post(
        "/evolution/run",
        json={
            "symbols": ["600000"],
            "dry_run": True,
            "now": "2026-03-02T20:43:00",
            "source_trace_id": "api-evo-seed-for-m8",
        },
    )
    response = client.post(
        "/evolution/m8/suggest",
        json={
            "symbols": ["600000"],
            "top_k": 3,
            "now": "2026-03-02T20:44:00",
            "source_trace_id": "api-m8-suggest",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert "summary" in payload
    assert "items" in payload
    assert str(payload.get("artifact_uri", "")).startswith("suggestions/m8/")


def test_evolution_m8_suggest_endpoint_uses_config_default_top_k() -> None:
    client = TestClient(app)
    response = client.post(
        "/evolution/m8/suggest",
        json={
            "symbols": ["600000"],
            "source_trace_id": "api-m8-default-topk",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert "top_k" in payload
    assert int(payload["top_k"]) == 5


def test_evolution_release_gate_endpoints() -> None:
    client = TestClient(app)

    attempt_payload = _run_task_and_get_result(
        client,
        "/evolution/release/attempt",
        {"days": 10, "min_runs": 1},
    )
    assert "accepted" in attempt_payload
    assert "gate" in attempt_payload

    latest_response = client.get("/evolution/release/latest")
    assert latest_response.status_code == 200
    latest_payload = latest_response.json()
    assert "decision" in latest_payload

    history_response = client.get("/evolution/release/history", params={"limit": 10})
    assert history_response.status_code == 200
    history_payload = history_response.json()
    assert history_payload["records"] >= 1


def test_evolution_release_approval_and_ticket_endpoints() -> None:
    client = TestClient(app)
    _ = client.post(
        "/evolution/release/attempt",
        json={"days": 10, "min_runs": 999},
    )

    approval_response = client.post(
        "/evolution/release/approval",
        json={
            "approver": "api-reviewer",
            "approved": False,
            "note": "blocked by gate",
        },
    )
    assert approval_response.status_code == 200
    approval_payload = approval_response.json()
    assert approval_payload["accepted"] is True
    assert "record" in approval_payload

    approval_latest = client.get("/evolution/release/approval/latest")
    assert approval_latest.status_code == 200
    approval_latest_payload = approval_latest.json()
    assert "record" in approval_latest_payload

    approval_history = client.get("/evolution/release/approval/history", params={"limit": 10})
    assert approval_history.status_code == 200
    assert approval_history.json()["records"] >= 1

    ticket_response = client.post(
        "/evolution/release/ticket",
        json={"operator": "api-operator", "note": "try issue"},
    )
    assert ticket_response.status_code == 200
    ticket_payload = ticket_response.json()
    assert ticket_payload["accepted"] is False

    ticket_latest = client.get("/evolution/release/ticket/latest")
    assert ticket_latest.status_code == 200
    ticket_latest_payload = ticket_latest.json()
    assert "ticket" in ticket_latest_payload or "status" in ticket_latest_payload

    ticket_history = client.get("/evolution/release/ticket/history", params={"limit": 10})
    assert ticket_history.status_code == 200
    ticket_history_payload = ticket_history.json()
    assert "records" in ticket_history_payload

    timeline_response = client.get(
        "/evolution/release/ticket/timeline",
        params={"limit": 50, "status": "issued"},
    )
    assert timeline_response.status_code == 200
    timeline_payload = timeline_response.json()
    assert "tickets" in timeline_payload

    execute_payload = _run_task_and_get_result(
        client,
        "/evolution/release/ticket/execute",
        {
            "executor": "api-operator",
            "confirm_window": True,
            "note": "close-out",
        },
    )
    assert "accepted" in execute_payload

    confirm_response = client.post(
        "/evolution/release/ticket/confirm",
        json={
            "confirmer": "api-reviewer",
            "note": "confirm",
        },
    )
    assert confirm_response.status_code == 200
    confirm_payload = confirm_response.json()
    assert "accepted" in confirm_payload

    rollback_response = client.post(
        "/evolution/release/ticket/rollback",
        json={
            "rollback_by": "api-reviewer",
            "note": "rollback",
        },
    )
    assert rollback_response.status_code == 200
    rollback_payload = rollback_response.json()
    assert "accepted" in rollback_payload

    watchdog_response = client.post(
        "/evolution/release/confirmation/watchdog",
        json={},
    )
    assert watchdog_response.status_code == 200
    watchdog_payload = watchdog_response.json()
    assert "checked" in watchdog_payload
    assert "rolled_back" in watchdog_payload
