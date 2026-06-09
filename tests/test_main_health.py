from __future__ import annotations

from fastapi.testclient import TestClient

import stock_analyzer.main as main_module


def test_health_endpoint_exposes_provider_backend_visibility() -> None:
    client = TestClient(main_module.app)

    response = client.get("/health/deep")

    assert response.status_code == 200
    payload = response.json()
    provider = payload["provider"]
    assert "predictor_mode" in provider
    assert "lgbm_backend" in provider
    assert "xgb_backend" in provider
    assert "status_timestamp" in provider


def test_health_endpoint_exposes_build_identity(monkeypatch) -> None:
    monkeypatch.setenv("STOCK_ANALYZER_BUILD_COMMIT", "abc1234")
    client = TestClient(main_module.app)

    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["health_type"] == "lightweight"
    assert payload["build"]["commit"] == "abc1234"
    assert payload["build"]["code_commit_id"] == main_module._config.evolution.code_commit_id


def test_health_endpoint_does_not_call_deep_runtime_dependencies(monkeypatch) -> None:
    client = TestClient(main_module.app)

    def _fail_provider_status() -> dict[str, object]:
        raise AssertionError("/health must not call provider_status")

    def _fail_runtime_status(*, include_learning_governance: bool = True) -> dict[str, object]:
        raise AssertionError("/health must not call runtime_status")

    monkeypatch.setattr(main_module._service, "provider_status", _fail_provider_status)
    monkeypatch.setattr(main_module._service, "runtime_status", _fail_runtime_status)

    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["health_type"] == "lightweight"
    assert payload["runtime"]["scheduler_enabled"] == main_module._config.scheduler.enabled


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

    response = client.get("/health/deep")

    assert response.status_code == 200
    payload = response.json()
    assert governance_called is False
    assert payload["runtime"]["learning_governance"]["status"] == "skipped"
    assert payload["runtime"]["learning_governance"]["reason"] == "omitted_for_lightweight_health"
