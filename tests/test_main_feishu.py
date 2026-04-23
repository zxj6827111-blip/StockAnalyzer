from __future__ import annotations

import json

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

import stock_analyzer.main as main_module
from stock_analyzer.notify.channels import NotificationResult

_ACK_TEXT = "\u5df2\u6536\u5230\uff0c\u5904\u7406\u4e2d"


def _enable_feishu(
    monkeypatch: MonkeyPatch,
    *,
    allowed_users: list[str] | None = None,
    subscription_mode: str = "webhook",
    run_final_inline: bool = True,
) -> None:
    cfg = main_module._config.feishu_interaction
    monkeypatch.setattr(main_module._config.app, "advisory_only", False)
    monkeypatch.setattr(cfg, "enabled", True)
    monkeypatch.setattr(cfg, "subscription_mode", subscription_mode)
    monkeypatch.setattr(cfg, "verification_token", "feishu-test-token")
    monkeypatch.setattr(cfg, "allowed_users", allowed_users or [])
    monkeypatch.setattr(cfg, "auto_reconcile_after_broker_snapshot", True)
    monkeypatch.setattr(main_module._config.notifications, "feishu_app_id", "cli_a")
    monkeypatch.setattr(main_module._config.notifications, "feishu_app_secret", "cli_s")
    monkeypatch.setattr(
        main_module.FeishuAppNotifier,
        "prewarm_tenant_access_token",
        classmethod(
            lambda _cls, *, app_id, app_secret, timeout_sec=5: NotificationResult(
                success=True,
                channel="feishu_app",
            )
        ),
    )
    runner = getattr(main_module, "_feishu_long_connection_runner", None)
    if runner is not None and hasattr(runner, "stop"):
        runner.stop()
    monkeypatch.setattr(main_module, "_feishu_long_connection_runner", None)
    main_module._service._cache.delete_prefix("feishu:callback:")
    main_module._service._cache.delete_prefix("feishu:message:")
    main_module._service._cache.delete_prefix("feishu:reply:")
    if run_final_inline:
        monkeypatch.setattr(
            main_module,
            "_launch_feishu_message_final_reply",
            lambda event, *, source, trace_id: main_module._process_feishu_message_event_async(
                event,
                source=source,
                trace_id=trace_id,
            ),
        )


def _patch_feishu_notifier(monkeypatch: MonkeyPatch) -> list[dict[str, object]]:
    sent: list[dict[str, object]] = []

    class _FakeFeishuAppNotifier:
        @classmethod
        def prewarm_tenant_access_token(
            cls,
            *,
            app_id: str,
            app_secret: str,
            timeout_sec: int = 5,
        ) -> NotificationResult:
            _ = app_id, app_secret, timeout_sec, cls
            return NotificationResult(success=True, channel="feishu_app")

        def __init__(
            self,
            app_id: str,
            app_secret: str,
            receive_id: str,
            receive_id_type: str = "open_id",
            timeout_sec: int = 5,
        ) -> None:
            self.app_id = app_id
            self.app_secret = app_secret
            self.receive_id = receive_id
            self.receive_id_type = receive_id_type
            self.timeout_sec = timeout_sec

        def _record(self, *, operation: str, message: object, message_id: str = "") -> None:
            sent.append(
                {
                    "operation": operation,
                    "app_id": self.app_id,
                    "app_secret": self.app_secret,
                    "receive_id": self.receive_id,
                    "receive_id_type": self.receive_id_type,
                    "message_id": message_id,
                    "message": message,
                }
            )

        def send(self, message: object) -> NotificationResult:
            self._record(operation="send", message=message)
            return NotificationResult(success=True, channel="feishu_app")

        def reply_text_message(self, *, message_id: str, message: object) -> NotificationResult:
            self._record(operation="reply", message=message, message_id=message_id)
            return NotificationResult(success=True, channel="feishu_app")

    monkeypatch.setattr(main_module, "FeishuAppNotifier", _FakeFeishuAppNotifier)
    return sent


def _feishu_message_event(
    text: str,
    *,
    token: str = "feishu-test-token",
    event_id: str = "evt-1",
    message_id: str = "om-1",
    chat_id: str = "oc-1",
    open_id: str = "ou-1",
    user_id: str = "u-1",
    message_type: str = "text",
) -> dict[str, object]:
    content = json.dumps({"text": text}, ensure_ascii=False) if message_type == "text" else "{}"
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "token": token,
            "create_time": "1700000000",
            "event_type": "im.message.receive_v1",
            "tenant_key": "tenant-1",
            "app_id": "cli_a",
        },
        "event": {
            "sender": {
                "sender_id": {
                    "open_id": open_id,
                    "user_id": user_id,
                    "union_id": "on-1",
                },
                "sender_type": "user",
                "tenant_key": "tenant-1",
            },
            "message": {
                "message_id": message_id,
                "chat_id": chat_id,
                "chat_type": "p2p",
                "message_type": message_type,
                "content": content,
            },
        },
    }


def _message_content(record: dict[str, object]) -> str:
    return str(getattr(record["message"], "content", ""))


def test_feishu_callback_url_verification(monkeypatch: MonkeyPatch) -> None:
    _enable_feishu(monkeypatch)
    client = TestClient(main_module.app)
    response = client.post(
        "/feishu/callback",
        json={
            "type": "url_verification",
            "token": "feishu-test-token",
            "challenge": "challenge-value",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"challenge": "challenge-value"}


def test_feishu_callback_rejects_bad_token(monkeypatch: MonkeyPatch) -> None:
    _enable_feishu(monkeypatch)
    client = TestClient(main_module.app)
    response = client.post(
        "/feishu/callback",
        json=_feishu_message_event(
            "pause",
            token="bad-token",
            event_id="evt-bad-token",
            message_id="om-bad-token",
        ),
    )
    assert response.status_code == 403
    assert response.json()["msg"] == "invalid_verification_token"


def test_feishu_callback_executes_text_command(monkeypatch: MonkeyPatch) -> None:
    _enable_feishu(monkeypatch)
    sent = _patch_feishu_notifier(monkeypatch)
    main_module._service._state.pause_new_buy = False

    client = TestClient(main_module.app)
    response = client.post(
        "/feishu/callback",
        json=_feishu_message_event(
            "pause",
            event_id="evt-pause",
            message_id="om-pause",
            chat_id="oc-pause",
        ),
    )

    assert response.status_code == 200
    assert response.json()["code"] == 0
    assert bool(main_module._service._state.pause_new_buy) is True
    assert len(sent) == 2
    assert sent[0]["receive_id"] == "oc-pause"
    assert sent[0]["receive_id_type"] == "chat_id"
    assert sent[0]["operation"] == "reply"
    assert sent[0]["message_id"] == "om-pause"
    assert _message_content(sent[0]) == _ACK_TEXT
    assert sent[1]["operation"] == "reply"
    assert sent[1]["message_id"] == "om-pause"
    assert _message_content(sent[1]) != _ACK_TEXT


def test_feishu_callback_registers_buy_with_inferred_position(monkeypatch: MonkeyPatch) -> None:
    _enable_feishu(monkeypatch)
    sent = _patch_feishu_notifier(monkeypatch)
    symbol = "600991"
    client = TestClient(main_module.app)

    response = client.post(
        "/feishu/callback",
        json=_feishu_message_event(
            f"set {symbol} price9.88 quantity800 total_asset100000 fee2.5",
            event_id="evt-buy",
            message_id="om-buy",
            chat_id="oc-buy",
            open_id="ou-buy",
            user_id="u-buy",
        ),
    )

    assert response.status_code == 200
    assert response.json()["code"] == 0
    positions = main_module._service.portfolio_positions()
    row = next(item for item in positions if str(item.get("symbol", "")).strip() == symbol)
    assert abs(float(row.get("target_position", 0.0)) - 0.07904) < 1e-6
    assert len(sent) == 2
    assert sent[0]["operation"] == "reply"
    assert sent[0]["message_id"] == "om-buy"
    assert _message_content(sent[0]) == _ACK_TEXT
    assert sent[1]["operation"] == "reply"
    assert sent[1]["message_id"] == "om-buy"
    assert _message_content(sent[1]) != _ACK_TEXT


def test_feishu_callback_deduplicates_repeated_message(monkeypatch: MonkeyPatch) -> None:
    _enable_feishu(monkeypatch)
    sent = _patch_feishu_notifier(monkeypatch)
    client = TestClient(main_module.app)
    payload = _feishu_message_event(
        "pause",
        event_id="evt-dup",
        message_id="om-dup",
        chat_id="oc-dup",
        open_id="ou-dup",
        user_id="u-dup",
    )

    first = client.post("/feishu/callback", json=payload)
    second = client.post("/feishu/callback", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(sent) == 2
    assert all(str(item["operation"]) == "reply" for item in sent)
    assert _message_content(sent[0]) == _ACK_TEXT


def test_feishu_callback_returns_after_ack_and_defers_final_processing(
    monkeypatch: MonkeyPatch,
) -> None:
    _enable_feishu(monkeypatch, run_final_inline=False)
    sent = _patch_feishu_notifier(monkeypatch)
    queued: list[dict[str, str]] = []
    main_module._service._state.pause_new_buy = False

    monkeypatch.setattr(
        main_module,
        "_launch_feishu_message_final_reply",
        lambda event, *, source, trace_id: queued.append(
            {
                "event_id": event.event_id,
                "message_id": event.message_id,
                "source": source,
                "trace_id": trace_id,
            }
        ),
    )

    client = TestClient(main_module.app)
    response = client.post(
        "/feishu/callback",
        json=_feishu_message_event(
            "pause",
            event_id="evt-async",
            message_id="om-async",
            chat_id="oc-async",
        ),
    )

    assert response.status_code == 200
    assert response.json()["code"] == 0
    assert bool(main_module._service._state.pause_new_buy) is False
    assert len(sent) == 1
    assert sent[0]["operation"] == "reply"
    assert sent[0]["message_id"] == "om-async"
    assert _message_content(sent[0]) == _ACK_TEXT
    assert queued == [
        {
            "event_id": "evt-async",
            "message_id": "om-async",
            "source": "webhook",
            "trace_id": "om-async",
        }
    ]


def test_feishu_callback_disabled_when_long_connection_mode(monkeypatch: MonkeyPatch) -> None:
    _enable_feishu(monkeypatch, subscription_mode="long_connection")

    class _FakeFeishuLongConnectionRunner:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.started = False

        def start(self) -> bool:
            self.started = True
            return True

        def stop(self, timeout_sec: float = 5.0) -> None:
            self.started = False

        def status(self) -> dict[str, object]:
            return {
                "status": "running" if self.started else "stopped",
                "thread_alive": self.started,
                "started_at": "",
                "last_message_at": "",
                "last_error": "",
            }

    monkeypatch.setattr(main_module, "FeishuLongConnectionRunner", _FakeFeishuLongConnectionRunner)

    with TestClient(main_module.app) as client:
        response = client.post(
            "/feishu/callback",
            json=_feishu_message_event(
                "pause",
                event_id="evt-long-disabled",
                message_id="om-long-disabled",
            ),
        )

    assert response.status_code == 403
    assert response.json()["msg"] == "feishu_webhook_mode_disabled"


def test_feishu_long_connection_status_starts_runner(monkeypatch: MonkeyPatch) -> None:
    _enable_feishu(monkeypatch, subscription_mode="long_connection")
    created: list[object] = []

    class _FakeFeishuLongConnectionRunner:
        def __init__(
            self,
            *,
            app_id: str,
            app_secret: str,
            message_handler: object,
            debug: bool = False,
        ) -> None:
            self.app_id = app_id
            self.app_secret = app_secret
            self.message_handler = message_handler
            self.debug = debug
            self.started = False
            self.stopped = False
            created.append(self)

        def start(self) -> bool:
            self.started = True
            return True

        def stop(self, timeout_sec: float = 5.0) -> None:
            self.stopped = True
            self.started = False

        def status(self) -> dict[str, object]:
            return {
                "status": "running" if self.started else "stopped",
                "thread_alive": self.started,
                "started_at": "",
                "last_message_at": "",
                "last_error": "",
            }

    monkeypatch.setattr(main_module, "FeishuLongConnectionRunner", _FakeFeishuLongConnectionRunner)

    with TestClient(main_module.app) as client:
        response = client.get("/feishu/long_connection/status")
        payload = response.json()

    assert response.status_code == 200
    assert payload["enabled"] is True
    assert payload["subscription_mode"] == "long_connection"
    assert payload["credentials_ready"] is True
    assert payload["runner"]["status"] == "running"
    assert len(created) == 1
    assert created[0].app_id == "cli_a"
    assert created[0].app_secret == "cli_s"
    assert created[0].debug is False
    assert created[0].stopped is True
