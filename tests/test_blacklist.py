"""PRD #19 acceptance tests: blacklist filtering (config + pipeline + API).

Covers:
- config matching: exact symbols and ``prefix*`` wildcards, enabled switch
- pipeline gate: blacklisted symbols yield hold/``blacklist`` with a
  ``decision_trace["blacklist_gate"]`` trace and never touch the provider
- runtime API: GET /settings/blacklist, POST add/remove, unified auth
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

import stock_analyzer.main as main_module
from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.pipeline import AnalyzerPipeline
from tests.test_pipeline import CountingBarsProvider


def _load_default_config() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    return load_config(root / "config" / "default.yaml")


def _blacklist_signal(config: StockAnalyzerConfig, symbol: str, strategy: str = "trend") -> Any:
    pipeline = AnalyzerPipeline(config=config, provider=CountingBarsProvider())
    return pipeline.run_once(symbols=[symbol], strategy=strategy, current_equity=1.0).signals[0]


def test_blacklist_config_defaults_disabled_and_empty() -> None:
    config = _load_default_config()
    assert config.blacklist.enabled is False
    assert config.blacklist.symbols == []
    assert config.blacklist.matches("600000") is None
    assert config.blacklist.is_blacklisted("600000") is False


def test_blacklist_config_exact_and_wildcard_matching() -> None:
    config = _load_default_config()
    config.blacklist.enabled = True
    config.blacklist.symbols = ["600000", "688*"]
    assert config.blacklist.matches("600000") == "600000"
    assert config.blacklist.matches("688981") == "688*"
    assert config.blacklist.matches("000001") is None
    assert config.blacklist.is_blacklisted("600000") is True
    assert config.blacklist.is_blacklisted("688981") is True
    assert config.blacklist.is_blacklisted("000001") is False


def test_blacklist_config_disabled_never_matches() -> None:
    config = _load_default_config()
    config.blacklist.enabled = False
    config.blacklist.symbols = ["600000"]
    assert config.blacklist.is_blacklisted("600000") is False


def test_pipeline_blacklisted_symbol_returns_hold_with_reason() -> None:
    config = _load_default_config()
    config.blacklist.enabled = True
    config.blacklist.symbols = ["600000"]
    pipeline = AnalyzerPipeline(config=config, provider=CountingBarsProvider())
    report = pipeline.run_once(symbols=["600000"], strategy="trend", current_equity=1.0)
    signal = report.signals[0]
    assert signal.action == "hold"
    assert signal.target_position == 0.0
    assert signal.score == 0.0
    assert signal.reasons == ["blacklist"]
    assert signal.decision_trace["blacklist_gate"]["passed"] is False
    assert signal.decision_trace["blacklist_gate"]["enabled"] is True
    assert signal.decision_trace["blacklist_gate"]["matched_pattern"] == "600000"
    assert pipeline._provider.calls == 0  # noqa: SLF001 -- gate fires before any data fetch


def test_pipeline_blacklist_disabled_allows_symbol() -> None:
    config = _load_default_config()
    config.blacklist.enabled = False
    config.blacklist.symbols = ["600000"]
    signal = _blacklist_signal(config, "600000")
    assert "blacklist" not in signal.reasons
    assert "blacklist_gate" not in signal.decision_trace


def test_pipeline_blacklist_empty_symbols_allows_symbol() -> None:
    config = _load_default_config()
    config.blacklist.enabled = True
    config.blacklist.symbols = []
    signal = _blacklist_signal(config, "600000")
    assert "blacklist" not in signal.reasons


def test_pipeline_blacklist_wildcard_prefix_blocks_and_allows_others() -> None:
    config = _load_default_config()
    config.blacklist.enabled = True
    config.blacklist.symbols = ["688*"]
    blocked = _blacklist_signal(config, "688981")
    assert blocked.action == "hold"
    assert blocked.reasons == ["blacklist"]
    assert blocked.decision_trace["blacklist_gate"]["matched_pattern"] == "688*"
    allowed = _blacklist_signal(config, "600000")
    assert "blacklist" not in allowed.reasons


def test_pipeline_blacklist_holds_for_all_strategies() -> None:
    config = _load_default_config()
    config.blacklist.enabled = True
    config.blacklist.symbols = ["600000"]
    for strategy in ("trend", "monster"):
        signal = _blacklist_signal(config, "600000", strategy=strategy)
        assert signal.action == "hold"
        assert signal.reasons == ["blacklist"]
        assert signal.strategy == strategy


def _api_config() -> StockAnalyzerConfig:
    config = _load_default_config()
    config.security.api_auth_enabled = True
    config.security.api_token = "test-blacklist-token"
    return config


def _client_with_config(monkeypatch: Any) -> TestClient:
    monkeypatch.setattr(main_module, "_config", _api_config())
    return TestClient(main_module.app)


def test_settings_blacklist_get_returns_current_state(monkeypatch: Any) -> None:
    client = _client_with_config(monkeypatch)
    response = client.get("/settings/blacklist")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["symbols"] == []
    assert body["count"] == 0


def test_settings_blacklist_add_and_remove(monkeypatch: Any) -> None:
    client = _client_with_config(monkeypatch)
    headers = {"X-SA-API-Key": "test-blacklist-token"}

    added = client.post(
        "/settings/blacklist/add",
        json={"symbol": " 600000 "},
        headers=headers,
    )
    assert added.status_code == 200, added.text
    body = added.json()
    assert body["added"] is True
    assert body["symbols"] == ["600000"]
    assert body["count"] == 1
    assert main_module._config.blacklist.symbols == ["600000"]  # noqa: SLF001

    duplicate = client.post(
        "/settings/blacklist/add",
        json={"symbol": "600000"},
        headers=headers,
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["added"] is False
    assert duplicate.json()["symbols"] == ["600000"]

    wildcard = client.post(
        "/settings/blacklist/add",
        json={"symbol": "688*"},
        headers=headers,
    )
    assert wildcard.status_code == 200
    assert wildcard.json()["symbols"] == ["600000", "688*"]

    removed = client.post(
        "/settings/blacklist/remove",
        json={"symbol": "600000"},
        headers=headers,
    )
    assert removed.status_code == 200
    body = removed.json()
    assert body["removed"] is True
    assert body["symbols"] == ["688*"]

    missing = client.post(
        "/settings/blacklist/remove",
        json={"symbol": "000001"},
        headers=headers,
    )
    assert missing.status_code == 200
    assert missing.json()["removed"] is False

    get_after = client.get("/settings/blacklist")
    assert get_after.json()["symbols"] == ["688*"]


def test_settings_blacklist_add_rejects_empty_symbol(monkeypatch: Any) -> None:
    client = _client_with_config(monkeypatch)
    headers = {"X-SA-API-Key": "test-blacklist-token"}
    response = client.post("/settings/blacklist/add", json={"symbol": "  "}, headers=headers)
    assert response.status_code == 422
    assert response.json()["detail"] == "empty_symbol"


def test_settings_blacklist_management_requires_auth(monkeypatch: Any) -> None:
    client = _client_with_config(monkeypatch)
    no_token = client.post("/settings/blacklist/add", json={"symbol": "600000"})
    assert no_token.status_code == 401
    wrong_token = client.post(
        "/settings/blacklist/remove",
        json={"symbol": "600000"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert wrong_token.status_code == 403
