from __future__ import annotations

from fastapi.testclient import TestClient

import stock_analyzer.main as main_module


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


def test_health_endpoint_exposes_build_identity(monkeypatch) -> None:
    monkeypatch.setenv("STOCK_ANALYZER_BUILD_COMMIT", "abc1234")
    client = TestClient(main_module.app)

    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["build"]["commit"] == "abc1234"
    assert payload["build"]["code_commit_id"] == main_module._config.evolution.code_commit_id


def test_health_endpoint_uses_lightweight_execution_risk_status(monkeypatch) -> None:
    client = TestClient(main_module.app)
    seen: list[bool] = []
    original_execution_risk_status = main_module._service.execution_risk_status

    def _tracking_execution_risk_status(
        *,
        include_artifact_scan: bool = True,
    ) -> dict[str, object]:
        seen.append(include_artifact_scan)
        return original_execution_risk_status(include_artifact_scan=include_artifact_scan)

    monkeypatch.setattr(
        main_module._service,
        "execution_risk_status",
        _tracking_execution_risk_status,
    )

    response = client.get("/health")

    assert response.status_code == 200
    assert seen == [False]


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
