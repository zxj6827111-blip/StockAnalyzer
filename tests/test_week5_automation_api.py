from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi.testclient import TestClient

import stock_analyzer.main as main_module


class _FakeAutomationApiService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run_week5_night_scan(
        self, *, timestamp: datetime | None, notify_enabled: bool, sync_watchlist: bool
    ) -> dict[str, object]:
        self.calls.append(
            (
                "night",
                {
                    "timestamp": timestamp,
                    "notify_enabled": notify_enabled,
                    "sync_watchlist": sync_watchlist,
                },
            )
        )
        return {"route": "night"}

    def run_week5_auction(
        self, *, timestamp: datetime | None, snapshot_id: str, notify_enabled: bool
    ) -> dict[str, object]:
        self.calls.append(
            (
                "auction",
                {
                    "timestamp": timestamp,
                    "snapshot_id": snapshot_id,
                    "notify_enabled": notify_enabled,
                },
            )
        )
        return {"route": "auction"}

    def run_week5_automation_market_radar(
        self, *, timestamp: datetime | None, snapshot_id: str, notify_enabled: bool
    ) -> dict[str, object]:
        self.calls.append(
            (
                "radar",
                {
                    "timestamp": timestamp,
                    "snapshot_id": snapshot_id,
                    "notify_enabled": notify_enabled,
                },
            )
        )
        return {"route": "radar"}

    def run_week5_automation_live_runtime(
        self, *, timestamp: datetime | None, notify_enabled: bool
    ) -> dict[str, object]:
        self.calls.append(("live", {"timestamp": timestamp, "notify_enabled": notify_enabled}))
        return {"route": "live"}

    def run_week5_weekend_learning(self, *, timestamp: datetime | None) -> dict[str, object]:
        self.calls.append(("weekend", {"timestamp": timestamp}))
        return {"route": "weekend"}


def _client(monkeypatch: Any) -> tuple[TestClient, _FakeAutomationApiService]:
    config = main_module._config.model_copy(deep=True)
    config.security.api_auth_enabled = True
    config.security.api_token = "week5-test-token"
    service = _FakeAutomationApiService()
    monkeypatch.setattr(main_module, "_config", config)
    monkeypatch.setattr(main_module, "_service", service)
    return TestClient(main_module.app), service


def test_week5_automation_post_requires_auth(monkeypatch: Any) -> None:
    client, service = _client(monkeypatch)
    response = client.post("/week5/night-scan/run", json={})
    assert response.status_code == 401
    assert service.calls == []


def test_week5_automation_posts_delegate_and_force_signal_only(monkeypatch: Any) -> None:
    client, service = _client(monkeypatch)
    headers = {"X-SA-API-Key": "week5-test-token"}
    payload = {
        "now": "2026-08-24T09:25:00+00:00",
        "snapshot_id": "snapshot-1",
        "notify_enabled": True,
        "sync_watchlist": False,
    }
    responses = [
        client.post("/week5/night-scan/run", json=payload, headers=headers),
        client.post("/week5/auction/run", json=payload, headers=headers),
        client.post("/week5/market-radar/run", json=payload, headers=headers),
        client.post("/week5/live-runtime/run", json=payload, headers=headers),
        client.post(
            "/learning/weekend/run", json={"now": "2026-08-29T12:00:00+00:00"}, headers=headers
        ),
    ]
    assert [response.status_code for response in responses] == [200] * 5
    assert [route for route, _ in service.calls] == ["night", "auction", "radar", "live", "weekend"]
    assert service.calls[0][1]["notify_enabled"] is True
    assert service.calls[1][1]["notify_enabled"] is True
    assert service.calls[2][1]["notify_enabled"] is True
    assert service.calls[3][1]["notify_enabled"] is True


def test_week5_automation_latest_and_state_gets_require_auth(monkeypatch: Any) -> None:
    client, service = _client(monkeypatch)

    paths = [
        "/week5/night-scan/latest",
        "/week5/auction/latest",
        "/week5/market-radar/latest",
        "/week5/live-runtime/latest",
        "/week5/candidate-state",
        "/learning/weekend/latest",
    ]

    responses = [client.get(path) for path in paths]

    assert [response.status_code for response in responses] == [401] * len(paths)
    assert service.calls == []