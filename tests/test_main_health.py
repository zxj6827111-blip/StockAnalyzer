from __future__ import annotations

import stock_analyzer.main as main_module
from fastapi.testclient import TestClient

def test_health_endpoint_exposes_provider_backend_visibility() -> None:
    client = TestClient(main_module.app)

    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    provider = payload["provider"]
    assert "predictor_mode" in provider
    assert "lgbm_backend" in provider
    assert "xgb_backend" in provider
    assert "status_timestamp" in provider


def test_health_endpoint_skips_learning_governance_when_registry_status_is_unavailable(
    monkeypatch,
) -> None:
    client = TestClient(main_module.app)
    governance_called = False

    def _fail_governance() -> dict[str, object]:
        nonlocal governance_called
        governance_called = True
        raise RuntimeError(
            'Could not set lock on file "/app/artifacts/training/learning_protocol.duckdb"'
        )

    monkeypatch.setattr(main_module._service, "learning_model_governance_status", _fail_governance)

    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert governance_called is False
    assert payload["runtime"]["learning_governance"]["status"] == "skipped"
    assert payload["runtime"]["learning_governance"]["reason"] == "omitted_for_lightweight_health"
