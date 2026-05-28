"""Security hardening tests for P0/P1 fixes.

Covers:
1. Unified API auth on dangerous POST endpoints (dynamic discovery)
2. Weak command-channel secret rejection
3. WeCom/Feishu allowed_users deny-by-default
4. Feishu verification_token empty rejection
5. docker-compose.runtime.yml default auto_promotion
"""

from __future__ import annotations

import os
import re
import time as _time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

import stock_analyzer.main as main_module
from stock_analyzer.command.channel import (
    CommandEnvelope,
    RuntimeState,
    SignedCommandProcessor,
)
from stock_analyzer.command.feishu_interaction import verify_feishu_token
from stock_analyzer.config import (
    CommandChannelConfig,
    SecurityConfig,
    load_config,
)
from stock_analyzer.infra.cache import InMemoryCache
from stock_analyzer.main import (
    _feishu_user_allowed,
    _wecom_user_allowed,
    app,
)

# ---------------------------------------------------------------------------
# 1. Unified API auth — dynamic endpoint discovery
# ---------------------------------------------------------------------------

# Endpoints with their own auth mechanism — must NOT get the unified auth.
_OWN_AUTH_PATHS: frozenset[str] = frozenset({
    "/wecom/callback",
    "/feishu/callback",
    "/command/execute",
    "/dashboard/ops/toggle",
    "/dashboard/command/quick",
    "/dashboard/reconcile/quick",
})


def _discover_protected_post_paths() -> list[str]:
    """Return every @app.post path that should carry unified auth.

    Reads the source file directly so the list is always in sync with main.py.
    """
    main_py = Path(__file__).resolve().parents[1] / "src" / "stock_analyzer" / "main.py"
    text = main_py.read_text(encoding="utf-8")
    all_post_paths = re.findall(r'@app\.post\("([^"]+)"\)', text)
    return [p for p in all_post_paths if p not in _OWN_AUTH_PATHS]


_PROTECTED_POST_PATHS: list[str] = _discover_protected_post_paths()


class _FakeAuthConfig:
    """Context manager that swaps the global security config for tests."""

    def __init__(
        self,
        enabled: bool = True,
        token: str = "test-token-12345",
    ) -> None:
        self._security = SecurityConfig(api_auth_enabled=enabled, api_token=token)
        self._orig = main_module._config.security

    def __enter__(self) -> _FakeAuthConfig:
        main_module._config.security = self._security
        return self

    def __exit__(self, *_: object) -> None:
        main_module._config.security = self._orig


def test_discovered_protected_post_count() -> None:
    """Sanity check: we must have a meaningful number of protected endpoints."""
    assert len(_PROTECTED_POST_PATHS) >= 70, (
        f"Expected >=70 protected POST endpoints, found {len(_PROTECTED_POST_PATHS)}. "
        "Check _OWN_AUTH_PATHS or the source scan logic."
    )


def test_unauthenticated_protected_post_returns_error() -> None:
    """Every protected POST endpoint must reject unauthenticated requests."""
    client = TestClient(app, raise_server_exceptions=False)
    with _FakeAuthConfig():
        for path in _PROTECTED_POST_PATHS:
            response = client.post(path, json={})
            assert response.status_code in (401, 403, 422), (
                f"POST {path} without token returned {response.status_code}; "
                "expected 401/403/422"
            )


def test_wrong_token_returns_403() -> None:
    """Wrong token must return 403 on a representative sample."""
    client = TestClient(app, raise_server_exceptions=False)
    with _FakeAuthConfig():
        for path in _PROTECTED_POST_PATHS[:5]:
            response = client.post(
                path,
                json={},
                headers={"Authorization": "Bearer wrong-token"},
            )
            assert response.status_code == 403, (
                f"Expected 403 for wrong token on POST {path}, got {response.status_code}"
            )


def test_valid_bearer_token_passes_auth() -> None:
    """Valid Bearer token should pass auth (may fail at business layer, that's OK)."""
    client = TestClient(app, raise_server_exceptions=False)
    with _FakeAuthConfig():
        response = client.post(
            "/notify/test",
            json={"title": "test", "content": "test"},
            headers={"Authorization": "Bearer test-token-12345"},
        )
        assert response.status_code == 200


def test_valid_x_sa_api_key_passes_auth() -> None:
    """Valid X-SA-API-Key header should pass auth."""
    client = TestClient(app, raise_server_exceptions=False)
    with _FakeAuthConfig():
        response = client.post(
            "/notify/test",
            json={"title": "test", "content": "test"},
            headers={"X-SA-API-Key": "test-token-12345"},
        )
        assert response.status_code == 200


def test_auth_disabled_allows_all() -> None:
    """When api_auth_enabled=false, endpoints should be accessible."""
    client = TestClient(app, raise_server_exceptions=False)
    with _FakeAuthConfig(enabled=False):
        response = client.post("/notify/test", json={"title": "t", "content": "c"})
        assert response.status_code == 200


def test_auth_enabled_empty_token_fails_closed() -> None:
    """When api_auth_enabled=true but api_token is empty, every request must be
    rejected (fail-closed), not silently allowed through."""
    client = TestClient(app, raise_server_exceptions=False)
    with _FakeAuthConfig(enabled=True, token=""):
        # Pick a few representative endpoints — all must get 500 (misconfigured)
        for path in ["/notify/test", "/scheduler/run_due", "/train/models"]:
            response = client.post(path, json={})
            assert response.status_code == 500, (
                f"POST {path} with auth enabled but empty token returned "
                f"{response.status_code}; expected 500 (fail-closed)"
            )
            assert "api_token is empty" in response.json().get("detail", "")


def test_health_endpoint_not_protected() -> None:
    """GET /health should always be accessible without auth."""
    client = TestClient(app, raise_server_exceptions=False)
    with _FakeAuthConfig():
        response = client.get("/health")
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# 2. Weak command-channel secret
# ---------------------------------------------------------------------------

_WEAK_SECRETS = [
    "", "change-me", "replace_with_strong_secret",
    "secret", "password", "default", "test",
]


@pytest.mark.parametrize("weak_secret", _WEAK_SECRETS)
def test_weak_secret_rejects_commands(weak_secret: str) -> None:
    """When secret_key is weak, all signed commands must be rejected."""
    config = CommandChannelConfig(enabled=True, secret_key=weak_secret)
    cache = InMemoryCache()
    state = RuntimeState()
    processor = SignedCommandProcessor(config=config, cache=cache, state=state)

    envelope = CommandEnvelope(
        command_id="test-001",
        timestamp=int(_time.time()),
        action="PAUSE_NEW_BUY",
        payload={},
        signature="does-not-matter",
    )
    result = processor.execute(envelope)
    assert result.accepted is False
    assert result.code == "weak_secret"


def test_strong_secret_allows_commands() -> None:
    """With a strong secret, properly signed commands should be accepted."""
    secret = "my-super-strong-secret-key-2026"
    config = CommandChannelConfig(enabled=True, secret_key=secret)
    cache = InMemoryCache()
    state = RuntimeState()
    processor = SignedCommandProcessor(config=config, cache=cache, state=state)

    ts = int(_time.time())
    sig = SignedCommandProcessor.build_signature(
        secret_key=secret,
        command_id="test-002",
        timestamp=ts,
        action="PAUSE_NEW_BUY",
        payload={},
    )
    envelope = CommandEnvelope(
        command_id="test-002",
        timestamp=ts,
        action="PAUSE_NEW_BUY",
        payload={},
        signature=sig,
    )
    result = processor.execute(envelope)
    assert result.accepted is True
    assert result.code == "ok"


def test_is_secret_weak_property() -> None:
    """CommandChannelConfig.is_secret_weak should identify weak secrets."""
    assert CommandChannelConfig(secret_key="change-me").is_secret_weak is True
    assert CommandChannelConfig(secret_key="").is_secret_weak is True
    assert CommandChannelConfig(secret_key="  ").is_secret_weak is True
    assert CommandChannelConfig(secret_key="strong-secret-abc123").is_secret_weak is False


# ---------------------------------------------------------------------------
# 3. WeCom/Feishu allowed_users deny-by-default
# ---------------------------------------------------------------------------


def _make_feishu_event(
    open_id: str = "ou_abc",
    user_id: str = "uid_123",
) -> object:
    from stock_analyzer.command.feishu_interaction import FeishuMessageEvent
    return FeishuMessageEvent(
        event_id="ev1",
        event_type="im.message.receive_v1",
        message_id="msg1",
        chat_id="chat1",
        chat_type="p2p",
        message_type="text",
        text="hello",
        open_id=open_id,
        user_id=user_id,
        union_id="",
        sender_type="user",
    )


def test_wecom_empty_users_deny_by_default(monkeypatch: MonkeyPatch) -> None:
    """When allowed_users is empty and allow_all_users_for_local_dev is False,
    all users must be denied."""
    monkeypatch.setattr(
        main_module._config.wecom_interaction, "allowed_users", [],
    )
    monkeypatch.setattr(
        main_module._config.wecom_interaction,
        "allow_all_users_for_local_dev",
        False,
    )
    assert _wecom_user_allowed("any_user") is False


def test_wecom_empty_users_allow_with_local_dev_flag(
    monkeypatch: MonkeyPatch,
) -> None:
    """When allow_all_users_for_local_dev=True, empty allowed_users allows all."""
    monkeypatch.setattr(
        main_module._config.wecom_interaction, "allowed_users", [],
    )
    monkeypatch.setattr(
        main_module._config.wecom_interaction,
        "allow_all_users_for_local_dev",
        True,
    )
    assert _wecom_user_allowed("any_user") is True


def test_wecom_configured_users_allows_whitelisted(
    monkeypatch: MonkeyPatch,
) -> None:
    """Configured allowed_users should allow listed users."""
    monkeypatch.setattr(
        main_module._config.wecom_interaction,
        "allowed_users",
        ["user_a", "user_b"],
    )
    monkeypatch.setattr(
        main_module._config.wecom_interaction,
        "allow_all_users_for_local_dev",
        False,
    )
    assert _wecom_user_allowed("user_a") is True
    assert _wecom_user_allowed("user_b") is True
    assert _wecom_user_allowed("user_c") is False


def test_feishu_empty_users_deny_by_default(monkeypatch: MonkeyPatch) -> None:
    """When allowed_users is empty and allow_all_users_for_local_dev is False,
    all users must be denied."""
    monkeypatch.setattr(
        main_module._config.feishu_interaction, "allowed_users", [],
    )
    monkeypatch.setattr(
        main_module._config.feishu_interaction,
        "allow_all_users_for_local_dev",
        False,
    )
    event = _make_feishu_event(open_id="ou_xxx", user_id="uid_yyy")
    assert _feishu_user_allowed(event) is False


def test_feishu_empty_users_allow_with_local_dev_flag(
    monkeypatch: MonkeyPatch,
) -> None:
    """When allow_all_users_for_local_dev=True, empty allowed_users allows all."""
    monkeypatch.setattr(
        main_module._config.feishu_interaction, "allowed_users", [],
    )
    monkeypatch.setattr(
        main_module._config.feishu_interaction,
        "allow_all_users_for_local_dev",
        True,
    )
    event = _make_feishu_event(open_id="ou_xxx", user_id="uid_yyy")
    assert _feishu_user_allowed(event) is True


def test_feishu_configured_users_allows_whitelisted(
    monkeypatch: MonkeyPatch,
) -> None:
    """Configured allowed_users should allow users matching
    open_id/user_id/union_id."""
    monkeypatch.setattr(
        main_module._config.feishu_interaction, "allowed_users", ["ou_xxx"],
    )
    monkeypatch.setattr(
        main_module._config.feishu_interaction,
        "allow_all_users_for_local_dev",
        False,
    )
    event = _make_feishu_event(open_id="ou_xxx", user_id="uid_yyy")
    assert _feishu_user_allowed(event) is True
    event2 = _make_feishu_event(open_id="ou_other", user_id="uid_yyy")
    assert _feishu_user_allowed(event2) is False


# ---------------------------------------------------------------------------
# 4. Feishu verification_token empty rejection
# ---------------------------------------------------------------------------


def test_feishu_empty_expected_token_rejects_all() -> None:
    """When verification_token is empty, all payloads should be rejected."""
    payload = {"token": "some_token", "type": "event_callback"}
    assert verify_feishu_token(payload, "") is False
    assert verify_feishu_token(payload, "   ") is False


def test_feishu_valid_token_accepts_matching() -> None:
    """Matching token should be accepted."""
    payload = {"token": "my_token", "type": "event_callback"}
    assert verify_feishu_token(payload, "my_token") is True


def test_feishu_valid_token_rejects_mismatch() -> None:
    """Mismatched token should be rejected."""
    payload = {"token": "wrong_token", "type": "event_callback"}
    assert verify_feishu_token(payload, "my_token") is False


def test_feishu_token_from_header() -> None:
    """Token in header.token should also be checked."""
    payload = {
        "header": {"token": "header_token"},
        "type": "event_callback",
    }
    assert verify_feishu_token(payload, "header_token") is True


# ---------------------------------------------------------------------------
# 5. docker-compose.runtime.yml auto_promotion default
# ---------------------------------------------------------------------------


def test_docker_compose_runtime_auto_promotion_defaults_to_false() -> None:
    """docker-compose.runtime.yml should default auto_promotion to false."""
    compose_path = (
        Path(__file__).resolve().parents[1] / "docker-compose.runtime.yml"
    )
    content = compose_path.read_text(encoding="utf-8")
    assert "${SA__AUTO_PROMOTION__ENABLED:-false}" in content, (
        "docker-compose.runtime.yml should default "
        "SA__AUTO_PROMOTION__ENABLED to false"
    )


def test_docker_compose_runtime_api_auth_defaults_to_fail_closed() -> None:
    """Runtime compose should require API auth by default for dangerous POST APIs."""
    compose_path = (
        Path(__file__).resolve().parents[1] / "docker-compose.runtime.yml"
    )
    content = compose_path.read_text(encoding="utf-8")
    assert "${SA__SECURITY__API_AUTH_ENABLED:-true}" in content
    assert "${SA__SECURITY__API_TOKEN:-}" in content


# ---------------------------------------------------------------------------
# 6. SecurityConfig integration
# ---------------------------------------------------------------------------


def test_security_config_defaults() -> None:
    """SecurityConfig should default to disabled."""
    cfg = SecurityConfig()
    assert cfg.api_auth_enabled is False
    assert cfg.api_token == ""


def test_config_loads_security_section(monkeypatch: MonkeyPatch) -> None:
    """config/default.yaml security section should be loaded."""
    for key in list(os.environ.keys()):
        if key.startswith("SA__"):
            monkeypatch.delenv(key, raising=False)
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    assert config.security.api_auth_enabled is False
    assert config.security.api_token == ""


def test_security_env_override(monkeypatch: MonkeyPatch) -> None:
    """SA__SECURITY__* env vars should override config."""
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("SA__SECURITY__API_AUTH_ENABLED", "true")
    monkeypatch.setenv("SA__SECURITY__API_TOKEN", "my-secret-token")
    config = load_config(root / "config" / "default.yaml")
    assert config.security.api_auth_enabled is True
    assert config.security.api_token == "my-secret-token"
