from __future__ import annotations

from fastapi.testclient import TestClient

from stock_analyzer.main import app


def _set_ops_enabled(client: TestClient, enabled: bool) -> None:
    response = client.post("/dashboard/ops/toggle", json={"enabled": enabled})
    assert response.status_code == 200


def _close_all_positions(client: TestClient) -> None:
    response = client.get("/portfolio/positions")
    assert response.status_code == 200
    positions = response.json().get("positions", [])
    for item in positions:
        symbol = str(item.get("symbol", "")).strip()
        if not symbol:
            continue
        close_response = client.post(
            "/dashboard/command/quick",
            json={"action": "CLOSE_POSITION", "payload": {"symbol": symbol}},
        )
        assert close_response.status_code == 200


def test_dashboard_quick_command_pause_and_resume() -> None:
    client = TestClient(app)
    _set_ops_enabled(client, True)

    pause_response = client.post(
        "/dashboard/command/quick",
        json={"action": "PAUSE_NEW_BUY", "payload": {}},
    )
    assert pause_response.status_code == 200
    pause_payload = pause_response.json()
    assert pause_payload["result"]["accepted"] is True
    assert pause_payload["result"]["state"]["pause_new_buy"] is True

    resume_response = client.post(
        "/dashboard/command/quick",
        json={"action": "RESUME_NEW_BUY", "payload": {}},
    )
    assert resume_response.status_code == 200
    resume_payload = resume_response.json()
    assert resume_payload["result"]["accepted"] is True
    assert resume_payload["result"]["state"]["pause_new_buy"] is False


def test_dashboard_quick_reconcile_with_snapshot() -> None:
    client = TestClient(app)
    _set_ops_enabled(client, True)
    _close_all_positions(client)

    set_response = client.post(
        "/dashboard/command/quick",
        json={
            "action": "SET_POSITION",
            "payload": {"symbol": "605001", "strategy": "manual", "target_position": 0.12},
        },
    )
    assert set_response.status_code == 200
    set_payload = set_response.json()
    assert set_payload["result"]["accepted"] is True

    reconcile_response = client.post(
        "/dashboard/reconcile/quick",
        json={
            "positions": [
                {
                    "symbol": "605001",
                    "target_position": 0.12,
                    "quantity": 1200,
                    "account": "acc-ui",
                }
            ],
            "run_reconcile": True,
        },
    )
    assert reconcile_response.status_code == 200
    reconcile_payload = reconcile_response.json()
    assert reconcile_payload["snapshot"]["broker_positions"] >= 1
    assert reconcile_payload["snapshot"]["quantity_records"] >= 1
    assert reconcile_payload["snapshot"]["account_records"] >= 1
    assert reconcile_payload["report"]["status"] == "ok"


def test_dashboard_quick_command_unknown_action_is_rejected() -> None:
    client = TestClient(app)
    _set_ops_enabled(client, True)
    response = client.post(
        "/dashboard/command/quick",
        json={"action": "NOT_A_REAL_ACTION", "payload": {}},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["result"]["accepted"] is False
    assert payload["result"]["code"] == "unknown_action"


def test_dashboard_quick_command_set_position_with_manual_fill_payload() -> None:
    client = TestClient(app)
    _set_ops_enabled(client, True)
    _close_all_positions(client)

    response = client.post(
        "/dashboard/command/quick",
        json={
            "action": "SET_POSITION",
            "payload": {
                "symbol": "605123",
                "strategy": "manual",
                "target_position": 0.12,
                "entry_price": 9.88,
                "quantity": 800,
                "fee": 2.5,
                "account": "acc-ui",
                "trade_time": "2026-03-01T10:02:03",
                "note": "dashboard fill",
            },
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["result"]["accepted"] is True
    manual_fill = payload["result"]["command_update"]["manual_fill"]
    assert manual_fill["entry_price"] == 9.88
    assert manual_fill["quantity"] == 800
    assert manual_fill["account"] == "acc-ui"
    watchlist_sync = payload["result"]["command_update"]["watchlist_sync"]
    assert watchlist_sync["symbol"] == "605123"
    assert "added" in watchlist_sync

    positions_response = client.get("/portfolio/positions")
    assert positions_response.status_code == 200
    positions = positions_response.json().get("positions", [])
    assert len(positions) >= 1
    target = next(item for item in positions if item.get("symbol") == "605123")
    assert target["entry_price"] == 9.88
    assert target["quantity"] == 800
    assert target["account"] == "acc-ui"


def test_dashboard_quick_command_set_recommendation_status() -> None:
    client = TestClient(app)
    _set_ops_enabled(client, True)

    response = client.post(
        "/dashboard/command/quick",
        json={
            "action": "SET_RECOMMENDATION_STATUS",
            "payload": {"symbol": "605456", "status": "watching", "note": "manual watch"},
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["result"]["accepted"] is True
    command_update = payload["result"]["command_update"]
    assert command_update["status"] == "watching"
    assert command_update["recommendation"]["symbol"] == "605456"

    lifecycle = client.get("/recommendations/lifecycle?status=watching")
    assert lifecycle.status_code == 200
    items = lifecycle.json().get("items", [])
    target = next(item for item in items if item.get("symbol") == "605456")
    assert target["status"] == "watching"
    assert target["note"] == "manual watch"

    bias = client.get("/portfolio/execution_bias?days=30&limit=20")
    assert bias.status_code == 200
    assert "items" in bias.json()

    recommendation_page = client.get("/dashboard/recommendations", follow_redirects=False)
    assert recommendation_page.status_code == 307
    assert recommendation_page.headers["location"] == "/ui/recommendations"


def test_dashboard_quick_command_close_all_positions() -> None:
    client = TestClient(app)
    _set_ops_enabled(client, True)
    _close_all_positions(client)

    set_response = client.post(
        "/dashboard/command/quick",
        json={
            "action": "SET_POSITION",
            "payload": {"symbol": "605002", "strategy": "manual", "target_position": 0.1},
        },
    )
    assert set_response.status_code == 200
    assert set_response.json()["result"]["accepted"] is True

    close_all_response = client.post(
        "/dashboard/command/quick",
        json={"action": "CLOSE_ALL_POSITIONS", "payload": {}},
    )
    assert close_all_response.status_code == 200
    close_payload = close_all_response.json()
    assert close_payload["result"]["accepted"] is True
    assert close_payload["result"]["command_update"]["closed_count"] >= 1


def test_dashboard_quick_command_close_position_with_manual_fill_payload() -> None:
    client = TestClient(app)
    _set_ops_enabled(client, True)
    _close_all_positions(client)

    set_response = client.post(
        "/dashboard/command/quick",
        json={
            "action": "SET_POSITION",
            "payload": {"symbol": "605888", "strategy": "manual", "target_position": 0.1},
        },
    )
    assert set_response.status_code == 200
    assert set_response.json()["result"]["accepted"] is True

    close_response = client.post(
        "/dashboard/command/quick",
        json={
            "action": "CLOSE_POSITION",
            "payload": {
                "symbol": "605888",
                "exit_price": 10.66,
                "quantity": 700,
                "fee": 2.2,
                "account": "acc-ui",
                "trade_time": "2026-03-01T14:59:59",
                "note": "dashboard sell",
            },
        },
    )
    assert close_response.status_code == 200
    close_payload = close_response.json()
    assert close_payload["result"]["accepted"] is True
    close_fill = close_payload["result"]["command_update"]["close_fill"]
    assert close_fill["exit_price"] == 10.66
    assert close_fill["quantity"] == 700
    assert close_fill["account"] == "acc-ui"

    trades_response = client.get("/portfolio/trades?limit=5")
    assert trades_response.status_code == 200
    trades = trades_response.json().get("trades", [])
    target = next(
        item
        for item in trades
        if item.get("symbol") == "605888" and item.get("side") == "sell"
    )
    assert target["exit_price"] == 10.66
    assert target["exit_quantity"] == 700


def test_latest_signals_api_contains_recommendation_id() -> None:
    client = TestClient(app)
    run_response = client.post(
        "/run/pipeline",
        json={"symbols": ["600000", "000001"], "strategy": "trend", "current_equity": 1.0},
    )
    assert run_response.status_code == 200

    latest_response = client.get("/signals/latest")
    assert latest_response.status_code == 200
    payload = latest_response.json()
    signals = payload.get("signals", [])
    assert isinstance(signals, list)
    assert len(signals) >= 1
    assert "recommendation_id" in signals[0]


def test_dashboard_ops_toggle_disables_quick_command() -> None:
    client = TestClient(app)
    _set_ops_enabled(client, False)

    state_response = client.get("/dashboard/ops/state")
    assert state_response.status_code == 200
    assert state_response.json()["enabled"] is False

    quick_response = client.post(
        "/dashboard/command/quick",
        json={"action": "PAUSE_NEW_BUY", "payload": {}},
    )
    assert quick_response.status_code == 200
    quick_payload = quick_response.json()
    assert quick_payload["accepted"] is False
    assert quick_payload["code"] == "disabled"

    _set_ops_enabled(client, True)


def test_dashboard_ops_state_contains_execution_mode_fields() -> None:
    client = TestClient(app)
    response = client.get("/dashboard/ops/state")
    assert response.status_code == 200
    payload = response.json()
    assert "advisory_only" in payload
    assert payload["execution_mode"] in {"advisory_only", "portfolio_auto_apply"}
    assert "market_warehouse" in payload
    assert "lock" in payload["market_warehouse"]
    assert "background_data" in payload["market_warehouse"]
