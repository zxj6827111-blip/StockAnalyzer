from __future__ import annotations

from fastapi.testclient import TestClient

from stock_analyzer.main import app


def test_week7_kill_switch_endpoints() -> None:
    client = TestClient(app)
    strategy = "week7_api_strategy"

    _ = client.post(
        "/week7/kill-switch/reset",
        json={"strategy": strategy, "resume_new_buy": True},
    )

    for month in ["2025-10", "2025-11", "2025-12"]:
        response = client.post(
            "/week7/kill-switch/performance",
            json={
                "month": month,
                "strategy": strategy,
                "strategy_return": -0.03,
                "benchmark_return": 0.02,
                "note": "test",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["accepted"] is True

    status_resp = client.get(
        "/week7/kill-switch/status",
        params={"strategy": strategy},
    )
    assert status_resp.status_code == 200
    status_payload = status_resp.json()
    assert status_payload["state"]["triggered"] is True

    history_resp = client.get(
        "/week7/kill-switch/history",
        params={"strategy": strategy, "limit": 10},
    )
    assert history_resp.status_code == 200
    history_payload = history_resp.json()
    assert history_payload["records"] >= 3

    reset_resp = client.post(
        "/week7/kill-switch/reset",
        json={"strategy": strategy, "resume_new_buy": True},
    )
    assert reset_resp.status_code == 200
    reset_payload = reset_resp.json()
    assert reset_payload["accepted"] is True
    assert reset_payload["pause_new_buy"] is False


def test_week7_cloud_backup_endpoints() -> None:
    client = TestClient(app)

    ping_resp = client.post(
        "/week7/cloud-backup/ping",
        json={"source": "api-test"},
    )
    assert ping_resp.status_code == 200
    ping_payload = ping_resp.json()
    assert ping_payload["accepted"] is True
    assert ping_payload["source"] == "api-test"

    status_resp = client.get("/week7/cloud-backup/status")
    assert status_resp.status_code == 200
    status_payload = status_resp.json()
    assert "enabled" in status_payload
    assert "is_offline" in status_payload

    check_resp = client.post("/week7/cloud-backup/check", json={})
    assert check_resp.status_code == 200
    check_payload = check_resp.json()
    assert "status" in check_payload
    assert "alerted" in check_payload


def test_week7_factor_lifecycle_endpoints() -> None:
    client = TestClient(app)
    strategy = "week7_factor_api_strategy"

    _ = client.post(
        "/week7/factor-lifecycle/reset",
        json={"strategy": strategy},
    )

    record_resp = client.post(
        "/week7/factor-lifecycle/record",
        json={
            "month": "2026-01",
            "strategy": strategy,
            "psr": 0.58,
            "ic_mean": 0.01,
            "top_features": [
                {"name": "volume_ratio", "importance": 0.32},
                {"name": "atr14", "importance": 0.21},
            ],
        },
    )
    assert record_resp.status_code == 200
    record_payload = record_resp.json()
    assert record_payload["accepted"] is True

    status_resp = client.get(
        "/week7/factor-lifecycle/status",
        params={"strategy": strategy},
    )
    assert status_resp.status_code == 200
    status_payload = status_resp.json()
    assert status_payload["state"]["records"] >= 1

    history_resp = client.get(
        "/week7/factor-lifecycle/history",
        params={"strategy": strategy, "limit": 10},
    )
    assert history_resp.status_code == 200
    history_payload = history_resp.json()
    assert history_payload["records"] >= 1

    graveyard_resp = client.get(
        "/week7/factor-lifecycle/graveyard",
        params={"strategy": strategy, "limit": 10},
    )
    assert graveyard_resp.status_code == 200
    graveyard_payload = graveyard_resp.json()
    assert "records" in graveyard_payload

    reset_resp = client.post(
        "/week7/factor-lifecycle/reset",
        json={"strategy": strategy},
    )
    assert reset_resp.status_code == 200
    reset_payload = reset_resp.json()
    assert reset_payload["accepted"] is True


def test_week7_sim_broker_endpoints() -> None:
    client = TestClient(app)

    run_resp = client.post(
        "/week7/sim-broker/run",
        json={"days": 7, "notify_enabled": False, "export_enabled": False},
    )
    assert run_resp.status_code == 200
    run_payload = run_resp.json()
    assert "status" in run_payload
    assert "summary" in run_payload
    assert "drilldown" in run_payload
    assert "trend" in run_payload

    latest_resp = client.get("/week7/sim-broker/latest")
    assert latest_resp.status_code == 200
    latest_payload = latest_resp.json()
    assert "report" in latest_payload

    history_resp = client.get("/week7/sim-broker/history", params={"limit": 10})
    assert history_resp.status_code == 200
    history_payload = history_resp.json()
    assert history_payload["records"] >= 1
