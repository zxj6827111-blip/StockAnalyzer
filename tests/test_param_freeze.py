"""PRD §8.7 parameter-freeze tests: config, core check and endpoint wiring.

The freeze is disabled globally for the test suite (see conftest.py, same
pattern as API auth), so every freeze test here enables it explicitly and
installs a deterministic wall clock.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pytest import MonkeyPatch

import stock_analyzer.main as main_module
import stock_analyzer.param_freeze as param_freeze
from stock_analyzer.command.wecom_interaction import build_wecom_signature, parse_wecom_xml
from stock_analyzer.config import (
    ParamFreezeConfig,
    ParamFreezeWindowConfig,
    load_config,
)
from stock_analyzer.main import app

_ROOT = Path(__file__).resolve().parents[1]

# 2026-08-10 is a Monday and an A-share trading day (see market_calendar.py).
_TRADING_DAY_10AM = datetime(2026, 8, 10, 10, 0, 0)
_TRADING_DAY_0930 = datetime(2026, 8, 10, 9, 30, 0)
_WEEKEND_10AM = datetime(2026, 8, 8, 10, 0, 0)  # Saturday
_HOLIDAY_10AM = datetime(2026, 10, 1, 10, 0, 0)  # National Day


def _enable_freeze(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(main_module._config.param_freeze, "enabled", True)


def _set_clock(monkeypatch: MonkeyPatch, value: datetime) -> None:
    monkeypatch.setattr(
        param_freeze,
        "current_time",
        lambda timezone: value,
    )


def _clear_sa_env(monkeypatch: MonkeyPatch) -> None:
    for key in list(os.environ.keys()):
        if key.startswith("SA__"):
            monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Config: defaults, parsing and validation
# ---------------------------------------------------------------------------


def test_param_freeze_config_defaults() -> None:
    config = ParamFreezeConfig()
    assert config.enabled is True
    assert config.timezone == "Asia/Shanghai"
    assert len(config.freeze_windows) == 1
    window = config.freeze_windows[0]
    assert window.start == "09:15"
    assert window.end == "15:00"
    assert "/week7/kill-switch/reset" in config.frozen_paths
    assert "/models/registry/lifecycle" in config.frozen_paths
    assert "/models/registry/role" in config.frozen_paths
    assert "/models/registry/bootstrap-active-champion" in config.frozen_paths
    assert "/learning/models/release/ticket/execute" in config.frozen_paths
    assert "execution_mode_set" in config.frozen_queries


def test_default_yaml_loads_param_freeze_section(monkeypatch: MonkeyPatch) -> None:
    _clear_sa_env(monkeypatch)
    config = load_config(_ROOT / "config" / "default.yaml")
    assert config.param_freeze.enabled is True
    assert config.param_freeze.timezone == "Asia/Shanghai"
    assert config.param_freeze.freeze_windows[0].start == "09:15"
    assert config.param_freeze.freeze_windows[0].end == "15:00"
    assert "/week7/kill-switch/reset" in config.param_freeze.frozen_paths


def test_param_freeze_env_override(monkeypatch: MonkeyPatch) -> None:
    _clear_sa_env(monkeypatch)
    monkeypatch.setenv("SA__PARAM_FREEZE__ENABLED", "false")
    config = load_config(_ROOT / "config" / "default.yaml")
    assert config.param_freeze.enabled is False


def test_param_freeze_invalid_window_time_rejected() -> None:
    with pytest.raises(ValidationError, match="invalid hhmm time"):
        ParamFreezeWindowConfig(start="25:00", end="15:00")


def test_param_freeze_inverted_window_rejected() -> None:
    with pytest.raises(ValidationError, match="start must be before end"):
        ParamFreezeConfig(
            freeze_windows=[ParamFreezeWindowConfig(start="15:00", end="09:15")]
        )


def test_param_freeze_invalid_timezone_rejected() -> None:
    with pytest.raises(ValidationError, match="invalid param_freeze timezone"):
        ParamFreezeConfig(timezone="Not/AZone")


# ---------------------------------------------------------------------------
# Core check: is_params_frozen
# ---------------------------------------------------------------------------


def test_is_params_frozen_in_window_on_trading_day() -> None:
    assert (
        param_freeze.is_params_frozen(
            config=ParamFreezeConfig(), now=_TRADING_DAY_10AM
        )
        is True
    )


def test_is_params_frozen_window_boundaries() -> None:
    config = ParamFreezeConfig()
    # Half-open [09:15, 15:00): start inclusive, end exclusive.
    assert param_freeze.is_params_frozen(config=config, now=datetime(2026, 8, 10, 9, 15)) is True
    assert param_freeze.is_params_frozen(config=config, now=datetime(2026, 8, 10, 15, 0)) is False
    assert param_freeze.is_params_frozen(config=config, now=datetime(2026, 8, 10, 9, 0)) is False
    assert param_freeze.is_params_frozen(config=config, now=datetime(2026, 8, 10, 15, 30)) is False
    # The 09:15-15:00 window covers the lunch break continuously (PRD §8.7).
    assert param_freeze.is_params_frozen(config=config, now=datetime(2026, 8, 10, 12, 0)) is True


def test_is_params_frozen_tz_aware_now() -> None:
    config = ParamFreezeConfig()
    # 02:00 UTC == 10:00 Asia/Shanghai on the same day.
    utc_now = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    assert param_freeze.is_params_frozen(config=config, now=utc_now) is True
    shanghai_now = datetime(2026, 8, 10, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert param_freeze.is_params_frozen(config=config, now=shanghai_now) is True


def test_is_params_frozen_disabled_allows() -> None:
    config = ParamFreezeConfig(enabled=False)
    assert param_freeze.is_params_frozen(config=config, now=_TRADING_DAY_10AM) is False


def test_is_params_frozen_non_trading_day_ignored() -> None:
    config = ParamFreezeConfig()
    assert param_freeze.is_params_frozen(config=config, now=_WEEKEND_10AM) is False
    assert param_freeze.is_params_frozen(config=config, now=_HOLIDAY_10AM) is False


def test_is_params_frozen_custom_window() -> None:
    config = ParamFreezeConfig(
        freeze_windows=[ParamFreezeWindowConfig(start="10:00", end="11:00")]
    )
    assert param_freeze.is_params_frozen(config=config, now=_TRADING_DAY_10AM) is True
    assert param_freeze.is_params_frozen(config=config, now=_TRADING_DAY_0930) is False


# ---------------------------------------------------------------------------
# Endpoint wiring
# ---------------------------------------------------------------------------


def test_frozen_endpoint_rejected_inside_window(monkeypatch: MonkeyPatch) -> None:
    _enable_freeze(monkeypatch)
    _set_clock(monkeypatch, _TRADING_DAY_10AM)
    client = TestClient(app)

    response = client.post(
        "/week7/kill-switch/reset",
        json={"strategy": "freeze_test", "resume_new_buy": True},
    )
    assert response.status_code == 423
    assert response.json()["detail"] == "params_frozen"

    lifecycle = client.post(
        "/models/registry/lifecycle",
        json={"model_id": "freeze_test", "lifecycle_state": "blocked"},
    )
    assert lifecycle.status_code == 423
    assert lifecycle.json()["detail"] == "params_frozen"

    role = client.post(
        "/models/registry/role",
        json={"model_id": "freeze_test", "role": "champion"},
    )
    assert role.status_code == 423
    assert role.json()["detail"] == "params_frozen"

    approve = client.post(
        "/learning/models/proposal/approval",
        json={"approver": "tester", "approved": True},
    )
    assert approve.status_code == 423
    assert approve.json()["detail"] == "params_frozen"

    blacklist_add = client.post("/settings/blacklist/add", json={"symbol": "600000"})
    assert blacklist_add.status_code == 423
    assert blacklist_add.json()["detail"] == "params_frozen"

    blacklist_remove = client.post("/settings/blacklist/remove", json={"symbol": "600000"})
    assert blacklist_remove.status_code == 423
    assert blacklist_remove.json()["detail"] == "params_frozen"


def test_frozen_endpoint_allowed_outside_window(monkeypatch: MonkeyPatch) -> None:
    _enable_freeze(monkeypatch)
    _set_clock(monkeypatch, datetime(2026, 8, 10, 15, 30))
    client = TestClient(app)

    response = client.post(
        "/week7/kill-switch/reset",
        json={"strategy": "freeze_test_outside", "resume_new_buy": True},
    )
    assert response.status_code == 200
    assert response.json()["accepted"] is True

    blacklist_add = client.post("/settings/blacklist/add", json={"symbol": "600001"})
    assert blacklist_add.status_code == 200
    assert blacklist_add.json()["added"] is True
    removed = client.post("/settings/blacklist/remove", json={"symbol": "600001"})
    assert removed.status_code == 200
    assert removed.json()["removed"] is True


def test_frozen_endpoint_allowed_when_freeze_disabled(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(main_module._config.param_freeze, "enabled", False)
    _set_clock(monkeypatch, _TRADING_DAY_10AM)
    client = TestClient(app)

    response = client.post(
        "/week7/kill-switch/reset",
        json={"strategy": "freeze_test_disabled", "resume_new_buy": True},
    )
    assert response.status_code == 200
    assert response.json()["accepted"] is True


def test_frozen_endpoint_allowed_on_non_trading_day(monkeypatch: MonkeyPatch) -> None:
    _enable_freeze(monkeypatch)
    _set_clock(monkeypatch, _WEEKEND_10AM)
    client = TestClient(app)

    response = client.post(
        "/week7/kill-switch/reset",
        json={"strategy": "freeze_test_weekend", "resume_new_buy": True},
    )
    assert response.status_code == 200
    assert response.json()["accepted"] is True


def test_get_endpoints_not_frozen(monkeypatch: MonkeyPatch) -> None:
    _enable_freeze(monkeypatch)
    _set_clock(monkeypatch, _TRADING_DAY_10AM)
    client = TestClient(app)

    status = client.get("/week7/kill-switch/status")
    assert status.status_code == 200

    registry = client.get("/models/registry", params={"limit": 5})
    assert registry.status_code == 200


def test_ops_and_command_channel_not_frozen(monkeypatch: MonkeyPatch) -> None:
    _enable_freeze(monkeypatch)
    _set_clock(monkeypatch, _TRADING_DAY_10AM)
    client = TestClient(app)

    toggle = client.post("/dashboard/ops/toggle", json={"enabled": True})
    assert toggle.status_code == 200

    command = client.post(
        "/command/execute",
        json={
            "command_id": "freeze-test-cmd",
            "timestamp": 1_700_000_000,
            "action": "PAUSE_NEW_BUY",
            "payload": {},
            "signature": "bad-signature",
        },
    )
    assert command.status_code == 200
    assert command.json().get("accepted") is False


# ---------------------------------------------------------------------------
# Interaction channel: execution_mode_set (advisory_only toggle)
# ---------------------------------------------------------------------------


def _wecom_mode_enabled() -> tuple[str, str]:
    import base64

    raw = b"0123456789abcdef0123456789abcdef"
    return "wx-test-token", base64.b64encode(raw).decode("utf-8").rstrip("=")


def _enable_wecom(monkeypatch: MonkeyPatch) -> None:
    _, aes_key = _wecom_mode_enabled()
    cfg = main_module._config.wecom_interaction
    monkeypatch.setattr(main_module._config.app, "advisory_only", False)
    monkeypatch.setattr(main_module._config.feishu_interaction, "enabled", False)
    monkeypatch.setattr(cfg, "enabled", True)
    monkeypatch.setattr(cfg, "token", "wx-test-token")
    monkeypatch.setattr(cfg, "verify_signature", True)
    monkeypatch.setattr(cfg, "allowed_users", ["user_a"])
    monkeypatch.setattr(cfg, "encoding_aes_key", "")
    monkeypatch.setattr(cfg, "receive_id", "")
    monkeypatch.setattr(cfg, "enforce_receive_id", False)


def _post_wecom_mode_command(client: TestClient, token: str, msg_id: str) -> str:
    inbound_xml = (
        "<xml>"
        "<ToUserName><![CDATA[ww-corp]]></ToUserName>"
        "<FromUserName><![CDATA[user_a]]></FromUserName>"
        "<CreateTime>1700000009</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[mode advisory on]]></Content>"
        f"<MsgId>{msg_id}</MsgId>"
        "</xml>"
    )
    timestamp = "1700000009"
    nonce = "nonce-9"
    signature = build_wecom_signature(token, timestamp, nonce, inbound_xml)
    response = client.post(
        "/wecom/callback",
        params={"timestamp": timestamp, "nonce": nonce, "msg_signature": signature},
        content=inbound_xml,
        headers={"Content-Type": "application/xml"},
    )
    assert response.status_code == 200
    return parse_wecom_xml(response.text)["Content"]


def test_wecom_execution_mode_set_frozen_inside_window(monkeypatch: MonkeyPatch) -> None:
    _enable_wecom(monkeypatch)
    _enable_freeze(monkeypatch)
    _set_clock(monkeypatch, _TRADING_DAY_10AM)
    client = TestClient(main_module.app)

    token, _ = _wecom_mode_enabled()
    content = _post_wecom_mode_command(client, token, "10009")
    assert "params_frozen" in content
    assert main_module._config.app.advisory_only is False


def test_wecom_execution_mode_set_allowed_outside_window(monkeypatch: MonkeyPatch) -> None:
    _enable_wecom(monkeypatch)
    _enable_freeze(monkeypatch)
    _set_clock(monkeypatch, datetime(2026, 8, 10, 15, 30))
    client = TestClient(main_module.app)

    token, _ = _wecom_mode_enabled()
    content = _post_wecom_mode_command(client, token, "10010")
    assert "execution_mode_set advisory_only=true" in content
    assert main_module._config.app.advisory_only is True


# ---------------------------------------------------------------------------
# Config drift guard: every default frozen path must be wired in source
# ---------------------------------------------------------------------------


def test_default_frozen_paths_are_wired_in_router_sources() -> None:
    import re

    src_root = _ROOT / "src" / "stock_analyzer" / "api"
    module_by_path: dict[str, str] = {}
    for module_file in src_root.glob("*.py"):
        text = module_file.read_text(encoding="utf-8")
        for match in re.findall(r'@router\.post\("([^"]+)"\)', text):
            module_by_path[match] = module_file.name
    for path in ParamFreezeConfig().frozen_paths:
        assert path in module_by_path, f"frozen path {path} has no POST route"
        source = (src_root / module_by_path[path]).read_text(encoding="utf-8")
        assert "ensure_params_not_frozen" in source, (
            f"frozen path {path} route module is missing the freeze dependency"
        )
