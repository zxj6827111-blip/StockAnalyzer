from __future__ import annotations

import json
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Literal
from urllib import request

from _pytest.monkeypatch import MonkeyPatch

from stock_analyzer.config import FeishuAppTargetConfig, load_config
from stock_analyzer.notify.channels import (
    BroadcastNotifier,
    ConsoleNotifier,
    CustomWebhookNotifier,
    EmailNotifier,
    FailoverNotifier,
    FeishuAppNotifier,
    FeishuNotifier,
    NotificationMessage,
    NotificationResult,
    TelegramNotifier,
    WeComNotifier,
)
from stock_analyzer.runtime.notifier_factory import build_channel, build_notifier


@dataclass(slots=True)
class _FakeNotifier:
    ok: bool
    name: str

    def send(self, message: NotificationMessage) -> NotificationResult:
        assert message.title
        return NotificationResult(
            success=self.ok,
            channel=self.name,
            error="" if self.ok else "failed",
        )


@dataclass(slots=True)
class _RecordingNotifier:
    ok: bool
    name: str
    calls: list[str]

    def send(self, message: NotificationMessage) -> NotificationResult:
        self.calls.append(self.name)
        return NotificationResult(
            success=self.ok,
            channel=self.name,
            error="" if self.ok else f"{self.name}_failed",
        )


@dataclass(slots=True)
class _FakeHttpResponse:
    status: int = 200
    payload: dict[str, object] | None = None

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


def test_failover_notifier_uses_backup_when_primary_fails() -> None:
    notifier = FailoverNotifier(
        primary=_FakeNotifier(ok=False, name="p"), backup=_FakeNotifier(ok=True, name="b")
    )
    result = notifier.send(NotificationMessage(title="x", content="y"))
    assert result.success is True
    assert result.channel == "b"


def test_broadcast_notifier_calls_all_targets_and_succeeds_when_all_successful() -> None:
    calls: list[str] = []
    notifier = BroadcastNotifier(
        targets=[
            ("personal", _RecordingNotifier(ok=True, name="first", calls=calls)),
            ("company", _RecordingNotifier(ok=True, name="second", calls=calls)),
        ],
        channel="feishu_app_broadcast",
    )

    result = notifier.send(NotificationMessage(title="x", content="y"))

    assert calls == ["first", "second"]
    assert result.success is True
    assert result.channel == "feishu_app_broadcast"
    assert result.error == ""


def test_broadcast_notifier_reports_failures_after_calling_every_target() -> None:
    calls: list[str] = []
    notifier = BroadcastNotifier(
        targets=[
            ("personal", _RecordingNotifier(ok=True, name="first", calls=calls)),
            ("company", _RecordingNotifier(ok=False, name="second", calls=calls)),
            ("backup_personal", _RecordingNotifier(ok=False, name="third", calls=calls)),
        ],
        channel="feishu_app_broadcast",
    )

    result = notifier.send(NotificationMessage(title="x", content="y"))

    assert calls == ["first", "second", "third"]
    assert result.success is False
    assert result.channel == "feishu_app_broadcast"
    error_payload = json.loads(result.error)
    assert error_payload == {
        "failures": [
            {
                "name": "company",
                "channel": "second",
                "error": "second_failed",
            },
            {
                "name": "backup_personal",
                "channel": "third",
                "error": "third_failed",
            },
        ]
    }


def test_broadcast_notifier_reports_missing_targets() -> None:
    notifier = BroadcastNotifier(
        targets=[],
        channel="feishu_app_broadcast",
        missing_targets_error="missing_feishu_apps",
    )

    result = notifier.send(NotificationMessage(title="x", content="y"))

    assert result.success is False
    assert result.channel == "feishu_app_broadcast"
    assert result.error == "missing_feishu_apps"


def test_new_notifiers_report_missing_configuration() -> None:
    message = NotificationMessage(title="x", content="y")

    assert FeishuNotifier(webhook="").send(message).error == "missing_webhook"
    assert (
        FeishuAppNotifier(app_id="", app_secret="", receive_id="").send(message).error
        == "missing_app_config"
    )
    assert (
        FeishuAppNotifier(app_id="cli_a", app_secret="cli_s", receive_id="").send(message).error
        == "missing_receive_id"
    )
    assert TelegramNotifier(bot_token="", chat_id="").send(message).error == "missing_bot_token"
    assert TelegramNotifier(bot_token="token", chat_id="").send(message).error == "missing_chat_id"
    assert (
        EmailNotifier(
            smtp_host="",
            smtp_port=465,
            sender="",
            password="",
            receivers=[],
        ).send(message).error
        == "missing_smtp_config"
    )
    assert CustomWebhookNotifier(webhook_url="").send(message).error == "missing_webhook"


def test_telegram_notifier_uses_api_result(monkeypatch: MonkeyPatch) -> None:
    def _fake_urlopen(req: object, timeout: int) -> _FakeHttpResponse:
        _ = req, timeout
        return _FakeHttpResponse(status=200, payload={"ok": False, "description": "chat not found"})

    monkeypatch.setattr(request, "urlopen", _fake_urlopen)

    result = TelegramNotifier(bot_token="token", chat_id="10086").send(
        NotificationMessage(title="x", content="y")
    )
    assert result.success is False
    assert result.error == "chat not found"


def test_feishu_app_notifier_fetches_token_and_sends_message(monkeypatch: MonkeyPatch) -> None:
    requests_seen: list[dict[str, object]] = []
    FeishuAppNotifier.clear_shared_token_cache()

    def _fake_urlopen(req: object, timeout: int) -> _FakeHttpResponse:
        assert isinstance(req, request.Request)
        requests_seen.append(
            {
                "url": req.full_url,
                "timeout": timeout,
                "headers": dict(req.header_items()),
                "payload": json.loads(req.data.decode("utf-8")),
            }
        )
        if req.full_url.endswith("/tenant_access_token/internal"):
            return _FakeHttpResponse(
                status=200,
                payload={
                    "code": 0,
                    "msg": "success",
                    "tenant_access_token": "t-123",
                    "expire": 7200,
                },
            )
        return _FakeHttpResponse(status=200, payload={"code": 0, "msg": "success"})

    monkeypatch.setattr(request, "urlopen", _fake_urlopen)

    notifier = FeishuAppNotifier(
        app_id="cli_a",
        app_secret="cli_s",
        receive_id="ou_xxx",
        receive_id_type="open_id",
    )
    result = notifier.send(NotificationMessage(title="日报", content="系统运行正常", level="info"))

    assert result.success is True
    assert len(requests_seen) == 2
    assert requests_seen[0]["url"] == "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    assert requests_seen[1]["url"] == (
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
    )
    send_headers = requests_seen[1]["headers"]
    assert send_headers["Authorization"] == "Bearer t-123"
    send_payload = requests_seen[1]["payload"]
    assert send_payload["receive_id"] == "ou_xxx"
    assert send_payload["msg_type"] == "text"
    assert isinstance(send_payload["content"], str)
    content_payload = json.loads(send_payload["content"])
    assert content_payload["text"] == "日报\n\n系统运行正常"


def test_feishu_app_notifier_replies_to_message(monkeypatch: MonkeyPatch) -> None:
    requests_seen: list[dict[str, object]] = []
    FeishuAppNotifier.clear_shared_token_cache()

    def _fake_urlopen(req: object, timeout: int) -> _FakeHttpResponse:
        assert isinstance(req, request.Request)
        requests_seen.append(
            {
                "url": req.full_url,
                "timeout": timeout,
                "headers": dict(req.header_items()),
                "payload": json.loads(req.data.decode("utf-8")),
            }
        )
        if req.full_url.endswith("/tenant_access_token/internal"):
            return _FakeHttpResponse(
                status=200,
                payload={
                    "code": 0,
                    "msg": "success",
                    "tenant_access_token": "t-123",
                    "expire": 7200,
                },
            )
        return _FakeHttpResponse(status=200, payload={"code": 0, "msg": "success"})

    monkeypatch.setattr(request, "urlopen", _fake_urlopen)

    notifier = FeishuAppNotifier(
        app_id="cli_a",
        app_secret="cli_s",
        receive_id="oc_xxx",
        receive_id_type="chat_id",
    )
    result = notifier.reply_text_message(
        message_id="om_xxx",
        message=NotificationMessage(title="", content="ack"),
    )

    assert result.success is True
    assert len(requests_seen) == 2
    assert requests_seen[1]["url"] == "https://open.feishu.cn/open-apis/im/v1/messages/om_xxx/reply"
    reply_headers = requests_seen[1]["headers"]
    assert reply_headers["Authorization"] == "Bearer t-123"
    reply_payload = requests_seen[1]["payload"]
    assert reply_payload["msg_type"] == "text"
    assert isinstance(reply_payload["content"], str)
    reply_content = json.loads(reply_payload["content"])
    assert reply_content["text"] == "ack"


def test_feishu_app_notifier_formats_structured_title_and_key_value_lines(
    monkeypatch: MonkeyPatch,
) -> None:
    requests_seen: list[dict[str, object]] = []
    FeishuAppNotifier.clear_shared_token_cache()

    def _fake_urlopen(req: object, timeout: int) -> _FakeHttpResponse:
        assert isinstance(req, request.Request)
        requests_seen.append(
            {
                "url": req.full_url,
                "timeout": timeout,
                "headers": dict(req.header_items()),
                "payload": json.loads(req.data.decode("utf-8")),
            }
        )
        if req.full_url.endswith("/tenant_access_token/internal"):
            return _FakeHttpResponse(
                status=200,
                payload={
                    "code": 0,
                    "msg": "success",
                    "tenant_access_token": "t-123",
                    "expire": 7200,
                },
            )
        return _FakeHttpResponse(status=200, payload={"code": 0, "msg": "success"})

    monkeypatch.setattr(request, "urlopen", _fake_urlopen)

    notifier = FeishuAppNotifier(
        app_id="cli_a",
        app_secret="cli_s",
        receive_id="ou_xxx",
        receive_id_type="open_id",
    )
    result = notifier.send(
        NotificationMessage(
            title="【日常】【盘前】每日策略简报",
            content="时间=2026-04-04T23:10:00\n状态=已生成",
            level="info",
        )
    )

    assert result.success is True
    assert len(requests_seen) == 2
    send_payload = requests_seen[1]["payload"]
    content_payload = json.loads(str(send_payload["content"]))
    assert content_payload["text"] == (
        "🌅 盘前简报｜每日策略简报\n\n"
        "时间：2026-04-04T23:10:00\n"
        "状态：已生成"
    )


def test_feishu_app_notifier_reuses_cached_token(monkeypatch: MonkeyPatch) -> None:
    request_urls: list[str] = []
    FeishuAppNotifier.clear_shared_token_cache()

    def _fake_urlopen(req: object, timeout: int) -> _FakeHttpResponse:
        assert isinstance(req, request.Request)
        _ = timeout
        request_urls.append(req.full_url)
        if req.full_url.endswith("/tenant_access_token/internal"):
            return _FakeHttpResponse(
                status=200,
                payload={"code": 0, "tenant_access_token": "t-123", "expire": 7200},
            )
        return _FakeHttpResponse(status=200, payload={"code": 0, "msg": "success"})

    monkeypatch.setattr(request, "urlopen", _fake_urlopen)

    notifier = FeishuAppNotifier(
        app_id="cli_a",
        app_secret="cli_s",
        receive_id="ou_xxx",
    )
    assert notifier.send(NotificationMessage(title="x", content="y")).success is True
    assert notifier.send(NotificationMessage(title="x2", content="y2")).success is True
    assert request_urls.count(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    ) == 1


def test_feishu_app_notifier_reuses_shared_token_across_instances(
    monkeypatch: MonkeyPatch,
) -> None:
    request_urls: list[str] = []
    FeishuAppNotifier.clear_shared_token_cache()

    def _fake_urlopen(req: object, timeout: int) -> _FakeHttpResponse:
        assert isinstance(req, request.Request)
        _ = timeout
        request_urls.append(req.full_url)
        if req.full_url.endswith("/tenant_access_token/internal"):
            return _FakeHttpResponse(
                status=200,
                payload={"code": 0, "tenant_access_token": "t-123", "expire": 7200},
            )
        return _FakeHttpResponse(status=200, payload={"code": 0, "msg": "success"})

    monkeypatch.setattr(request, "urlopen", _fake_urlopen)

    first = FeishuAppNotifier(
        app_id="cli_a",
        app_secret="cli_s",
        receive_id="ou_xxx",
    )
    second = FeishuAppNotifier(
        app_id="cli_a",
        app_secret="cli_s",
        receive_id="ou_yyy",
    )
    assert first.send(NotificationMessage(title="x", content="y")).success is True
    assert second.send(NotificationMessage(title="x2", content="y2")).success is True
    assert request_urls.count(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    ) == 1


def test_feishu_app_notifier_prewarm_populates_shared_cache(monkeypatch: MonkeyPatch) -> None:
    request_urls: list[str] = []
    FeishuAppNotifier.clear_shared_token_cache()

    def _fake_urlopen(req: object, timeout: int) -> _FakeHttpResponse:
        assert isinstance(req, request.Request)
        _ = timeout
        request_urls.append(req.full_url)
        if req.full_url.endswith("/tenant_access_token/internal"):
            return _FakeHttpResponse(
                status=200,
                payload={"code": 0, "tenant_access_token": "t-123", "expire": 7200},
            )
        return _FakeHttpResponse(status=200, payload={"code": 0, "msg": "success"})

    monkeypatch.setattr(request, "urlopen", _fake_urlopen)

    prewarm = FeishuAppNotifier.prewarm_tenant_access_token(
        app_id="cli_a",
        app_secret="cli_s",
    )
    assert prewarm.success is True

    notifier = FeishuAppNotifier(
        app_id="cli_a",
        app_secret="cli_s",
        receive_id="ou_xxx",
    )
    assert notifier.send(NotificationMessage(title="x", content="y")).success is True
    assert request_urls.count(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    ) == 1


def test_wecom_notifier_keeps_level_prefix_and_strips_duplicate_priority_badge(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_urlopen(req: object, timeout: int) -> _FakeHttpResponse:
        assert isinstance(req, request.Request)
        captured["timeout"] = timeout
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _FakeHttpResponse(status=200)

    monkeypatch.setattr(request, "urlopen", _fake_urlopen)

    result = WeComNotifier(webhook="https://example.com/wecom").send(
        NotificationMessage(
            title="【重要】【运维】基础数据库同步异常",
            content="正文",
            level="warn",
        )
    )

    assert result.success is True
    assert captured["timeout"] == 5
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["text"] == {"content": "[🟠 重要] [🛠 运维] 基础数据库同步异常\n正文"}


def test_wecom_notifier_formats_info_title_with_category_badge(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_urlopen(req: object, timeout: int) -> _FakeHttpResponse:
        assert isinstance(req, request.Request)
        captured["timeout"] = timeout
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _FakeHttpResponse(status=200)

    monkeypatch.setattr(request, "urlopen", _fake_urlopen)

    result = WeComNotifier(webhook="https://example.com/wecom").send(
        NotificationMessage(title="【日常】【盘前】盘前简报", content="正文", level="info")
    )

    assert result.success is True
    assert captured["timeout"] == 5
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["text"] == {"content": "[🟡 日常] [🌅 盘前] 盘前简报\n正文"}


def test_email_notifier_sends_message_via_ssl_client(monkeypatch: MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    class _FakeSMTP:
        def __init__(self, host: str, port: int, timeout: int) -> None:
            calls["host"] = host
            calls["port"] = port
            calls["timeout"] = timeout

        def login(self, sender: str, password: str) -> None:
            calls["sender"] = sender
            calls["password"] = password

        def send_message(self, message: EmailMessage) -> None:
            calls["to"] = str(message["To"])

        def quit(self) -> None:
            calls["quit"] = True

    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTP)

    notifier = EmailNotifier(
        smtp_host="smtp.test.local",
        smtp_port=465,
        sender="robot@test.local",
        password="secret",
        receivers=["alpha@test.local", "beta@test.local"],
        use_ssl=True,
        starttls=False,
    )
    result = notifier.send(NotificationMessage(title="daily", content="report"))
    assert result.success is True
    assert calls["host"] == "smtp.test.local"
    assert calls["port"] == 465
    assert calls["sender"] == "robot@test.local"
    assert calls["quit"] is True


def test_build_channel_supports_new_notification_types() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml").model_copy(deep=True)

    config.notifications.feishu_webhook = "https://example.com/feishu"
    config.notifications.feishu_app_id = "cli_a"
    config.notifications.feishu_app_secret = "cli_s"
    config.notifications.feishu_app_receive_id = "ou_xxx"
    config.notifications.telegram_bot_token = "tg-token"
    config.notifications.telegram_chat_id = "10086"
    config.notifications.email_smtp_host = "smtp.test.local"
    config.notifications.email_sender = "robot@test.local"
    config.notifications.email_password = "secret"
    config.notifications.email_receivers = ["ops@test.local"]
    config.notifications.custom_webhook_url = "https://example.com/hook"

    assert isinstance(build_channel(config, "feishu"), FeishuNotifier)
    assert isinstance(build_channel(config, "feishu_app"), FeishuAppNotifier)
    assert isinstance(build_channel(config, "telegram"), TelegramNotifier)
    assert isinstance(build_channel(config, "email"), EmailNotifier)
    assert isinstance(build_channel(config, "custom_webhook"), CustomWebhookNotifier)


def test_build_channel_supports_feishu_app_broadcast() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml").model_copy(deep=True)
    config.notifications.feishu_apps = [
        FeishuAppTargetConfig(
            name="personal",
            app_id="cli_a",
            app_secret="secret_a",
            receive_id="oc_a",
            receive_id_type="chat_id",
        ),
        FeishuAppTargetConfig(
            name="company",
            app_id="cli_b",
            app_secret="secret_b",
            receive_id="oc_b",
            receive_id_type="chat_id",
        ),
    ]

    notifier = build_channel(config, "feishu_app_broadcast")

    assert isinstance(notifier, BroadcastNotifier)
    assert notifier.channel == "feishu_app_broadcast"
    assert len(notifier.targets) == 2
    assert [name for name, _ in notifier.targets] == ["personal", "company"]
    assert all(isinstance(target, FeishuAppNotifier) for _, target in notifier.targets)


def test_build_notifier_forces_console_in_pytest(monkeypatch: MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml").model_copy(deep=True)
    config.notifications.primary = "wecom"
    config.notifications.backup = "feishu"
    config.notifications.wecom_webhook = "https://example.com/wecom"
    config.notifications.feishu_webhook = "https://example.com/feishu"

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_notifier.py::test_build_notifier")
    notifier = build_notifier(config)
    assert isinstance(notifier, FailoverNotifier)
    assert isinstance(notifier.primary, ConsoleNotifier)
    assert isinstance(notifier.backup, ConsoleNotifier)


def test_build_notifier_forces_console_during_pytest_collection(
    monkeypatch: MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml").model_copy(deep=True)
    config.notifications.primary = "feishu_app"
    config.notifications.backup = "wecom"
    config.notifications.feishu_app_id = "cli_a"
    config.notifications.feishu_app_secret = "cli_s"
    config.notifications.feishu_app_receive_id = "ou_xxx"
    config.notifications.wecom_webhook = "https://example.com/wecom"

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    notifier = build_notifier(config)

    assert isinstance(notifier, FailoverNotifier)
    assert isinstance(notifier.primary, ConsoleNotifier)
    assert isinstance(notifier.backup, ConsoleNotifier)


def test_build_notifier_forces_console_when_env_flag_enabled(monkeypatch: MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml").model_copy(deep=True)
    config.notifications.primary = "wecom"
    config.notifications.backup = "telegram"
    config.notifications.wecom_webhook = "https://example.com/wecom"
    config.notifications.telegram_bot_token = "token"
    config.notifications.telegram_chat_id = "10086"

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("SA_DISABLE_EXTERNAL_NOTIFICATIONS", "true")
    notifier = build_notifier(config)
    assert isinstance(notifier, FailoverNotifier)
    assert isinstance(notifier.primary, ConsoleNotifier)
    assert isinstance(notifier.backup, ConsoleNotifier)


def test_build_notifier_forces_console_with_explicit_console_flag(
    monkeypatch: MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml").model_copy(deep=True)
    config.notifications.primary = "feishu_app"
    config.notifications.backup = "wecom"
    config.notifications.feishu_app_id = "cli_a"
    config.notifications.feishu_app_secret = "cli_s"
    config.notifications.feishu_app_receive_id = "ou_xxx"
    config.notifications.wecom_webhook = "https://example.com/wecom"

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("SA_FORCE_CONSOLE_NOTIFIER", "true")
    notifier = build_notifier(config)

    assert isinstance(notifier, FailoverNotifier)
    assert isinstance(notifier.primary, ConsoleNotifier)
    assert isinstance(notifier.backup, ConsoleNotifier)


def test_build_channel_does_not_add_test_prefix_for_wecom_by_default() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml").model_copy(deep=True)
    config.app.mode = "simulation"
    config.notifications.wecom_webhook = "https://example.com/wecom"

    notifier = build_channel(config, "wecom")
    assert isinstance(notifier, WeComNotifier)
    assert notifier.title_prefix == ""


def test_build_channel_uses_explicit_test_prefix_for_wecom(monkeypatch: MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml").model_copy(deep=True)
    config.app.mode = "simulation"
    config.notifications.wecom_webhook = "https://example.com/wecom"
    monkeypatch.setenv("SA_WECOM_TEST_PREFIX", "[测试]")

    notifier = build_channel(config, "wecom")
    assert isinstance(notifier, WeComNotifier)
    assert notifier.title_prefix == "[测试]"
