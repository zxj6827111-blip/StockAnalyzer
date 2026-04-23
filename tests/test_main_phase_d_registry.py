from __future__ import annotations

from fastapi.testclient import TestClient

import stock_analyzer.main as main_module


class _FakePhaseDRegistryService:
    def generate_phase_d6_registry_report(
        self,
        *,
        output_path: str | None = None,
    ) -> dict[str, object]:
        return {
            "scope": "phase_d6",
            "status": "completed_research_registry",
            "output_path": output_path or "",
        }


def test_main_phase_d6_registry_endpoint_returns_report(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "_service", _FakePhaseDRegistryService())
    client = TestClient(main_module.app)

    response = client.get("/research/d6/registry")

    assert response.status_code == 200
    payload = response.json()
    assert payload["scope"] == "phase_d6"
    assert payload["status"] == "completed_research_registry"
