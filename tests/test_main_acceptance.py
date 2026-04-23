from __future__ import annotations

from fastapi.testclient import TestClient

from stock_analyzer.main import app


def test_acceptance_week4_endpoints_run_latest_and_history() -> None:
    client = TestClient(app)

    run_response = client.post("/acceptance/week4/run", json={"sla_recent_runs": 50})
    assert run_response.status_code == 200
    run_payload = run_response.json()
    assert "overall" in run_payload
    assert "summary" in run_payload
    assert "artifact" in run_payload

    latest_response = client.get("/acceptance/week4/latest")
    assert latest_response.status_code == 200
    latest_payload = latest_response.json()
    assert "report" in latest_payload

    history_response = client.get("/acceptance/week4/history", params={"limit": 10})
    assert history_response.status_code == 200
    history_payload = history_response.json()
    assert history_payload["records"] >= 1


def test_acceptance_v13_bundle_endpoint_generates_artifacts() -> None:
    client = TestClient(app)

    response = client.post(
        "/acceptance/v13/bundle",
        json={
            "symbol": "600000",
            "lookback_days": 320,
            "run_week5_scan": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert "baseline" in payload
    assert "phase_checkpoints" in payload
    assert "portfolio_execution" in payload
    assert "label_conflict_shadow" in payload
    assert "v13_acceptance" in payload
