"""API tests for the background-task asyncization (audit P1-#7).

Heavy endpoints must return 202 + ``task_id`` and track their outcome in the
task registry; light endpoints stay synchronous. Under TestClient the
background task runs to completion before ``client.post`` returns, so tests
poll ``GET /tasks/{task_id}`` afterwards to assert the recorded outcome.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

import stock_analyzer.main as main_module


class _StubService:
    """Minimal service stub exposing only the asyncized methods under test."""

    def run_pipeline(self, **kwargs: object) -> dict[str, object]:
        return {"trace_id": "t-pipeline", "status": "ok", "echo": kwargs}

    def train_models(self, **kwargs: object) -> dict[str, object]:
        return {"mode": "single_symbol", "status": "trained", "echo": kwargs}

    def train_execution_risk_model(self, **kwargs: object) -> dict[str, object]:
        return {"mode": "execution_risk_training", "status": "trained", "echo": kwargs}

    def train_learning_manifest(self, **kwargs: object) -> dict[str, object]:
        return {"mode": "dataset_manifest", "ok": True, "echo": kwargs}

    def run_learning_manifest_shadow_validation(self, **kwargs: object) -> dict[str, object]:
        return {"mode": "learning_manifest_shadow_validation", "ok": True, "echo": kwargs}

    def run_learning_manifest_shadow_promotion_gate(self, **kwargs: object) -> dict[str, object]:
        return {"mode": "learning_manifest_shadow_promotion_gate", "ok": True, "echo": kwargs}

    def run_learning_manifest_shadow_proposal(self, **kwargs: object) -> dict[str, object]:
        return {"mode": "learning_manifest_shadow_proposal", "ok": True, "echo": kwargs}

    def run_signal_quality_audit(self, **kwargs: object) -> dict[str, object]:
        return {"status": "ok", "echo": kwargs}

    def build_phase_d_alphalens_report(self, **kwargs: object) -> dict[str, object]:
        return {"research_id": "alphalens_sidecar", "status": "ok", "echo": kwargs}

    def build_phase_d_shap_report(self, **kwargs: object) -> dict[str, object]:
        return {"research_id": "shap_sidecar", "status": "ok", "echo": kwargs}

    def build_phase_d_catboost_shadow_report(self, **kwargs: object) -> dict[str, object]:
        return {"research_id": "catboost_shadow", "status": "ok", "echo": kwargs}

    def build_phase_d_finbert_report(self, **kwargs: object) -> dict[str, object]:
        return {"research_id": "finbert_sidecar", "status": "ok", "echo": kwargs}

    def build_phase_d_qlib_bridge_report(self, **kwargs: object) -> dict[str, object]:
        return {"research_id": "qlib_bridge", "status": "ok", "echo": kwargs}

    def build_phase_d_tabular_deep_report(self, **kwargs: object) -> dict[str, object]:
        return {"research_id": "tabnet_ft_transformer", "status": "ok", "echo": kwargs}

    def build_phase_d_tft_report(self, **kwargs: object) -> dict[str, object]:
        return {"research_id": "tft_sidecar", "status": "ok", "echo": kwargs}

    def build_phase_d_finrl_report(self, **kwargs: object) -> dict[str, object]:
        return {"research_id": "finrl_sidecar", "status": "ok", "echo": kwargs}

    def build_phase_d_heavy_ts_report(self, **kwargs: object) -> dict[str, object]:
        return {"research_id": "heavy_ts_shadow", "status": "ok", "echo": kwargs}

    def run_tdx_offline_sync(self, **kwargs: object) -> dict[str, object]:
        return {"status": "ok", "echo": kwargs}

    def run_market_warehouse_sync(self, **kwargs: object) -> dict[str, object]:
        return {"status": "ok", "synced_symbols": 2, "echo": kwargs}

    def run_evolution_offhours(self, **kwargs: object) -> dict[str, object]:
        return {"status": "ok", "dry_run": True, "echo": kwargs}

    def attempt_evolution_release(self, **kwargs: object) -> dict[str, object]:
        return {"accepted": True, "gate": {}, "echo": kwargs}

    def execute_evolution_release_ticket(self, **kwargs: object) -> dict[str, object]:
        return {"accepted": True, "echo": kwargs}

    def latest_report(self) -> None:
        return None


class _FailingService(_StubService):
    def train_models(self, **kwargs: object) -> dict[str, object]:
        raise ValueError("training exploded")


def _wait_task(client: TestClient, task_id: str) -> dict[str, Any]:
    response = client.get(f"/tasks/{task_id}")
    assert response.status_code == 200, response.text
    return response.json()


def test_heavy_endpoint_returns_202_with_queued_task(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "_service", _StubService())
    client = TestClient(main_module.app)

    response = client.post(
        "/run/pipeline",
        json={"symbols": ["600000", "000001"], "strategy": "trend"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["task_id"]
    assert body["name"] == "run_pipeline"
    assert body["submitted_at"]

    entry = _wait_task(client, body["task_id"])
    assert entry["status"] == "succeeded"
    assert entry["result"]["trace_id"] == "t-pipeline"
    assert entry["error"] is None
    assert entry["started_at"] is not None
    assert entry["finished_at"] is not None


def test_all_asyncized_endpoints_return_202_and_complete(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "_service", _StubService())
    client = TestClient(main_module.app)

    cases = [
        ("/run/pipeline", {"symbols": ["600000"]}, "run_pipeline"),
        ("/train/models", {"symbol": "600000"}, "train_models"),
        ("/train/execution-risk", {}, "train_execution_risk"),
        ("/train/learning-manifest", {}, "train_learning_manifest"),
        ("/train/learning-manifest/shadow-validate", {}, "train_learning_manifest_shadow_validate"),
        ("/train/learning-manifest/shadow-promote", {}, "train_learning_manifest_shadow_promote"),
        ("/train/learning-manifest/shadow-proposal", {}, "train_learning_manifest_shadow_proposal"),
        ("/research/signal-quality/run", {}, "signal_quality_audit"),
        ("/research/alphalens/report", {}, "phase_d_alphalens"),
        ("/research/shap/report", {}, "phase_d_shap"),
        ("/research/catboost-shadow/report", {}, "phase_d_catboost_shadow"),
        ("/research/finbert/report", {"records": []}, "phase_d_finbert"),
        ("/research/qlib-bridge/report", {}, "phase_d_qlib_bridge"),
        ("/research/tabular-deep/report", {}, "phase_d_tabular_deep"),
        ("/research/tft/report", {}, "phase_d_tft"),
        ("/research/finrl/report", {}, "phase_d_finrl"),
        ("/research/heavy-ts/report", {}, "phase_d_heavy_ts"),
        ("/tdx/sync/run", {}, "tdx_sync_run"),
        ("/warehouse/sync/run", {}, "warehouse_sync_run"),
        ("/evolution/run", {}, "evolution_run"),
        ("/evolution/release/attempt", {}, "evolution_release_attempt"),
        (
            "/evolution/release/ticket/execute",
            {"executor": "test-operator"},
            "evolution_release_ticket_execute",
        ),
    ]
    for path, payload, expected_name in cases:
        response = client.post(path, json=payload)
        assert response.status_code == 202, f"{path} -> {response.status_code}"
        body = response.json()
        assert body["status"] == "queued", path
        assert body["name"] == expected_name, path
        entry = _wait_task(client, body["task_id"])
        assert entry["status"] == "succeeded", f"{path}: {entry.get('error')}"


def test_failing_background_task_is_recorded_as_failed(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "_service", _FailingService())
    client = TestClient(main_module.app)

    response = client.post("/train/models", json={"symbol": "600000"})
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"

    entry = _wait_task(client, body["task_id"])
    assert entry["status"] == "failed"
    assert "ValueError" in entry["error"]
    assert "training exploded" in entry["error"]
    assert entry["finished_at"] is not None


def test_unknown_task_returns_404(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "_service", _StubService())
    client = TestClient(main_module.app)

    response = client.get("/tasks/does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"] == "task_not_found"


def test_tasks_list_endpoint_returns_recent_tasks(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "_service", _StubService())
    client = TestClient(main_module.app)

    before = client.get("/tasks", params={"limit": 100}).json()["count"]
    client.post("/run/pipeline", json={"symbols": ["600000"]})
    after_payload = client.get("/tasks", params={"limit": 100}).json()

    assert after_payload["count"] == before + 1
    assert after_payload["tasks"][0]["status"] == "succeeded"
    assert after_payload["tasks"][0]["name"] == "run_pipeline"


def test_light_endpoints_stay_synchronous(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "_service", _StubService())
    client = TestClient(main_module.app)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["health_type"] == "lightweight"

    risk = client.get("/risk/status")
    assert risk.status_code == 200
    assert risk.json() == {"status": "no_run"}
