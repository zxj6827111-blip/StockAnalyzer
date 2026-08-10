from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Literal
from urllib import parse, request

from _pytest.monkeypatch import MonkeyPatch

from stock_analyzer.config import load_config
from stock_analyzer.notify.channels import (
    DingTalkNotifier,
    HttpSmsGateway,
    NotificationMessage,
    NotificationResult,
    SmsGateway,
    SmsNotifier,
)
from stock_analyzer.runtime.notifier_factory import build_channel


class _FakeHttpResponse:
    def __init__(self, status: int = 200, payload: dict[str, object] | None = None) -> None:
        self.status = status
        self.payload = payload

    def __enter__(self) -> _FakeHttpResponse:
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        tb: object,
    ) -> Literal[False]:
        return False

    def read(self) -> bytes:
        if self.payload is None:
            return b""
        return json.dumps(self.payload).encode("utf-8")


def _expected_dingtalk_sign(secret: str, timestamp_ms: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp_ms}\n{secret}".encode(),
        digestmod=hashlib.sha256,
    ).digest()
    return parse.quote_plus(base64.b64encode(digest))


def test_dingtalk_notifier_reports_missing_webhook() -> None:
    result = DingTalkNotifier(webhook="").send(
        NotificationMessage(title="x", content="y")
    )
    assert result.success is False
    assert result.channel == "dingtalk"
    assert result.error == "missing_webhook"


def test_dingtalk_notifier_sends_markdown_without_secret(monkeypatch: MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _fake_urlopen(req: object, timeout: int) -> _FakeHttpResponse:
        assert isinstance(req, request.Request)
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _FakeHttpResponse(status=200, payload={"errcode": 0, "errmsg": "ok"})

    monkeypatch.setattr(request, "urlopen", _fake_urlopen)

    result = DingTalkNotifier(webhook="https://oapi.dingtalk.com/robot/send?access_token=abc").send(
        NotificationMessage(title="【运维】同步异常", content="正文", level="warn")
    )

    assert result.success is True
    assert captured["timeout"] == 5
    assert captured["url"] == "https://oapi.dingtalk.com/robot/send?access_token=abc"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["msgtype"] == "markdown"
    markdown = payload["markdown"]
    assert isinstance(markdown, dict)
    assert markdown["title"] == "【运维】同步异常"
    assert markdown["text"] == "## 【运维】同步异常\n\n正文"


def test_dingtalk_notifier_appends_signed_query_when_secret_set(
    monkeypatch: MonkeyPatch,
) -> None:
    captured_url: list[str] = []
    monkeypatch.setattr(time, "time", lambda: 1700000000.123)

    def _fake_urlopen(req: object, timeout: int) -> _FakeHttpResponse:
        assert isinstance(req, request.Request)
        _ = timeout
        captured_url.append(req.full_url)
        return _FakeHttpResponse(status=200, payload={"errcode": 0, "errmsg": "ok"})

    monkeypatch.setattr(request, "urlopen", _fake_urlopen)

    result = DingTalkNotifier(
        webhook="https://oapi.dingtalk.com/robot/send?access_token=abc",
        secret="SEC-test-secret",
    ).send(NotificationMessage(title="x", content="y"))

    assert result.success is True
    expected_sign = _expected_dingtalk_sign("SEC-test-secret", "1700000000123")
    assert captured_url == [
        "https://oapi.dingtalk.com/robot/send"
        "?access_token=abc&timestamp=1700000000123&sign=" + expected_sign
    ]


def test_dingtalk_notifier_fails_on_nonzero_errcode(monkeypatch: MonkeyPatch) -> None:
    def _fake_urlopen(req: object, timeout: int) -> _FakeHttpResponse:
        _ = req, timeout
        return _FakeHttpResponse(
            status=200,
            payload={"errcode": 310000, "errmsg": "keywords not in content"},
        )

    monkeypatch.setattr(request, "urlopen", _fake_urlopen)

    result = DingTalkNotifier(webhook="https://example.com/dingtalk").send(
        NotificationMessage(title="x", content="y")
    )
    assert result.success is False
    assert result.channel == "dingtalk"
    assert result.error == "keywords not in content"


def test_dingtalk_notifier_survives_bad_webhook_url(monkeypatch: MonkeyPatch) -> None:
    def _fake_urlopen(req: object, timeout: int) -> _FakeHttpResponse:
        _ = req, timeout
        raise ValueError("unknown url type: not-a-url")

    monkeypatch.setattr(request, "urlopen", _fake_urlopen)

    result = DingTalkNotifier(webhook="not-a-url").send(
        NotificationMessage(title="x", content="y")
    )
    assert result.success is False
    assert "unknown url type" in result.error


def test_sms_notifier_reports_missing_phone_numbers() -> None:
    result = SmsNotifier(url="https://example.com/sms").send(
        NotificationMessage(title="x", content="y")
    )
    assert result.success is False
    assert result.channel == "sms"
    assert result.error == "missing_phone_numbers"


def test_sms_notifier_reports_missing_url() -> None:
    result = SmsNotifier(url="", phone_numbers=["13800000000"]).send(
        NotificationMessage(title="x", content="y")
    )
    assert result.success is False
    assert result.error == "missing_url"


def test_sms_notifier_sends_via_http_gateway(monkeypatch: MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _fake_urlopen(req: object, timeout: int) -> _FakeHttpResponse:
        assert isinstance(req, request.Request)
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _FakeHttpResponse(status=200)

    monkeypatch.setattr(request, "urlopen", _fake_urlopen)

    notifier = SmsNotifier(
        url="https://example.com/sms/send",
        app_key="ak",
        app_secret="sk",
        sign_name="测试签名",
        template_id="SMS_123",
        phone_numbers=["13800000000", "13900000000", " "],
    )
    result = notifier.send(
        NotificationMessage(title="预警", content="系统异常", level="error")
    )

    assert result.success is True
    assert captured["url"] == "https://example.com/sms/send"
    assert captured["timeout"] == 5
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["app_key"] == "ak"
    assert payload["app_secret"] == "sk"
    assert payload["sign_name"] == "测试签名"
    assert payload["template_id"] == "SMS_123"
    assert payload["phones"] == ["13800000000", "13900000000"]
    assert payload["content"] == "[ERROR] 预警\n系统异常"


class _RecordingSmsGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, object], int]] = []

    def send(self, body: dict[str, object], timeout_sec: int) -> NotificationResult:
        self.calls.append((body, timeout_sec))
        return NotificationResult(success=True, channel="sms")


def test_sms_notifier_uses_injected_gateway() -> None:
    gateway = _RecordingSmsGateway()
    notifier = SmsNotifier(
        url="https://example.com/sms",
        phone_numbers=["13800000000"],
        gateway=gateway,  # type: ignore[arg-type]
    )
    result = notifier.send(NotificationMessage(title="x", content="y"))

    assert result.success is True
    assert len(gateway.calls) == 1
    body, timeout_sec = gateway.calls[0]
    assert timeout_sec == 5
    assert body["phones"] == ["13800000000"]
    assert body["content"] == "[INFO] x\ny"


def test_sms_notifier_survives_gateway_error(monkeypatch: MonkeyPatch) -> None:
    def _fake_urlopen(req: object, timeout: int) -> _FakeHttpResponse:
        _ = req, timeout
        raise TimeoutError("timed out")

    monkeypatch.setattr(request, "urlopen", _fake_urlopen)

    result = SmsNotifier(
        url="https://example.com/sms",
        phone_numbers=["13800000000"],
    ).send(NotificationMessage(title="x", content="y"))

    assert result.success is False
    assert result.error == "timed out"


def test_sms_gateway_protocol_is_satisfied_by_http_gateway() -> None:
    def _accept(gateway: SmsGateway) -> SmsGateway:
        return gateway

    http_gateway = HttpSmsGateway(url="https://example.com/sms")
    assert _accept(http_gateway) is http_gateway


def test_build_channel_supports_dingtalk_and_sms() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml").model_copy(deep=True)
    config.notifications.dingtalk_webhook = "https://example.com/dingtalk"
    config.notifications.sms_url = "https://example.com/sms"
    config.notifications.sms_phone_numbers = ["13800000000"]

    assert isinstance(build_channel(config, "dingtalk"), DingTalkNotifier)
    assert isinstance(build_channel(config, "sms"), SmsNotifier)


def test_config_env_supports_dingtalk_and_sms_channels(monkeypatch: MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("SA__NOTIFICATIONS__PRIMARY", "dingtalk")
    monkeypatch.setenv("SA__NOTIFICATIONS__BACKUP", "sms")
    monkeypatch.setenv("SA__NOTIFICATIONS__DINGTALK_WEBHOOK", "https://example.com/dingtalk")
    monkeypatch.setenv("SA__NOTIFICATIONS__DINGTALK_SECRET", "SEC-test")
    monkeypatch.setenv("SA__NOTIFICATIONS__SMS_URL", "https://example.com/sms")
    monkeypatch.setenv("SA__NOTIFICATIONS__SMS_PHONE_NUMBERS", '["13800000000"]')

    config = load_config(root / "config" / "default.yaml")

    assert config.notifications.primary == "dingtalk"
    assert config.notifications.backup == "sms"
    assert config.notifications.dingtalk_webhook == "https://example.com/dingtalk"
    assert config.notifications.dingtalk_secret == "SEC-test"
    assert config.notifications.sms_url == "https://example.com/sms"
    assert config.notifications.sms_phone_numbers == ["13800000000"]
