"""Primary/backup notification channels."""

from __future__ import annotations

import json
import re
import smtplib
import ssl
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from email.message import EmailMessage
from typing import ClassVar, Protocol
from urllib import parse, request


@dataclass(slots=True)
class NotificationMessage:
    title: str
    content: str
    level: str = "info"
    trace_id: str = ""


@dataclass(slots=True)
class NotificationResult:
    success: bool
    channel: str
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class Notifier(Protocol):
    def send(self, message: NotificationMessage) -> NotificationResult:
        """Send message and return delivery result."""


@dataclass(slots=True)
class BroadcastNotifier:
    """Send the same message to every target and aggregate delivery results."""

    targets: Sequence[tuple[str, Notifier]]
    channel: str = "broadcast"
    missing_targets_error: str = "missing_targets"

    def send(self, message: NotificationMessage) -> NotificationResult:
        if not self.targets:
            return NotificationResult(
                success=False,
                channel=self.channel,
                error=self.missing_targets_error,
            )

        failures: list[dict[str, str]] = []
        for target_name, notifier in self.targets:
            normalized_name = target_name.strip() or "unnamed"
            try:
                result = notifier.send(message)
            except Exception as exc:  # pragma: no cover - defensive for custom notifiers.
                result = NotificationResult(
                    success=False,
                    channel="unknown",
                    error=str(exc),
                )
            if result.success:
                continue
            failures.append(
                {
                    "name": normalized_name,
                    "channel": result.channel,
                    "error": result.error or "send_failed",
                }
            )

        if not failures:
            return NotificationResult(success=True, channel=self.channel)
        return NotificationResult(
            success=False,
            channel=self.channel,
            error=json.dumps(
                {"failures": failures},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )


@dataclass(slots=True)
class RequiredSuccessBroadcastNotifier:
    """Broadcast to all targets, but success depends only on required target names."""

    targets: Sequence[tuple[str, Notifier]]
    required_names: set[str]
    channel: str = "broadcast"
    missing_targets_error: str = "missing_targets"

    def send(self, message: NotificationMessage) -> NotificationResult:
        if not self.targets:
            return NotificationResult(
                success=False,
                channel=self.channel,
                error=self.missing_targets_error,
            )

        failures: list[dict[str, str]] = []
        required_failed = False
        for target_name, notifier in self.targets:
            normalized_name = target_name.strip() or "unnamed"
            try:
                result = notifier.send(message)
            except Exception as exc:  # pragma: no cover - defensive for custom notifiers.
                result = NotificationResult(
                    success=False,
                    channel="unknown",
                    error=str(exc),
                )
            if result.success:
                continue
            if normalized_name in self.required_names:
                required_failed = True
            failures.append(
                {
                    "name": normalized_name,
                    "channel": result.channel,
                    "error": result.error or "send_failed",
                }
            )

        if required_failed:
            return NotificationResult(
                success=False,
                channel=self.channel,
                error=json.dumps(
                    {"failures": failures},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        if failures:
            return NotificationResult(
                success=True,
                channel=self.channel,
                error=json.dumps(
                    {"optional_failures": failures},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        return NotificationResult(success=True, channel=self.channel)


class ConsoleNotifier:
    """Local fallback channel that only prints to stdout."""

    def send(self, message: NotificationMessage) -> NotificationResult:
        print(
            f"[notify][{message.level}] {message.title} trace={message.trace_id} "
            f"content={message.content}"
        )
        return NotificationResult(success=True, channel="console")


@dataclass(slots=True)
class PushPlusNotifier:
    token: str
    timeout_sec: int = 5

    def send(self, message: NotificationMessage) -> NotificationResult:
        if not self.token:
            return NotificationResult(success=False, channel="pushplus", error="missing_token")
        body = {
            "token": self.token,
            "title": message.title,
            "content": message.content,
            "template": "txt",
        }
        return _post_json(
            channel="pushplus",
            url="https://www.pushplus.plus/send",
            body=body,
            timeout_sec=self.timeout_sec,
        )


@dataclass(slots=True)
class WeComNotifier:
    webhook: str
    timeout_sec: int = 5
    title_prefix: str = ""

    def send(self, message: NotificationMessage) -> NotificationResult:
        if not self.webhook:
            return NotificationResult(success=False, channel="wecom", error="missing_webhook")
        title = _apply_title_prefix(
            _format_wecom_title(message.title, message.level),
            self.title_prefix,
        )
        body = {
            "msgtype": "text",
            "text": {"content": f"{title}\n{message.content}"},
        }
        return _post_json(
            channel="wecom", url=self.webhook, body=body, timeout_sec=self.timeout_sec
        )


@dataclass(slots=True)
class FeishuNotifier:
    webhook: str
    timeout_sec: int = 5

    def send(self, message: NotificationMessage) -> NotificationResult:
        if not self.webhook:
            return NotificationResult(success=False, channel="feishu", error="missing_webhook")
        body = {
            "msg_type": "text",
            "content": {"text": _format_feishu_message(message)},
        }
        return _post_json(
            channel="feishu",
            url=self.webhook,
            body=body,
            timeout_sec=self.timeout_sec,
        )


@dataclass(slots=True)
class FeishuAppNotifier:
    app_id: str
    app_secret: str
    receive_id: str
    receive_id_type: str = "open_id"
    timeout_sec: int = 5
    _tenant_access_token: str = field(default="", init=False, repr=False)
    _tenant_access_token_expire_at: float = field(default=0.0, init=False, repr=False)
    _shared_tenant_access_tokens: ClassVar[dict[tuple[str, str], tuple[str, float]]] = {}
    _shared_tenant_access_tokens_lock: ClassVar[threading.Lock] = threading.Lock()

    def send(self, message: NotificationMessage) -> NotificationResult:
        app_id = self.app_id.strip()
        app_secret = self.app_secret.strip()
        receive_id = self.receive_id.strip()
        receive_id_type = self.receive_id_type.strip().lower() or "open_id"
        if not app_id or not app_secret:
            return NotificationResult(
                success=False,
                channel="feishu_app",
                error="missing_app_config",
            )
        if not receive_id:
            return NotificationResult(
                success=False,
                channel="feishu_app",
                error="missing_receive_id",
            )

        access_token_result = self._tenant_access_token_value(
            app_id=app_id,
            app_secret=app_secret,
        )
        if isinstance(access_token_result, NotificationResult):
            return access_token_result

        body = {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps(
                {"text": _format_feishu_message(message)},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
        url = (
            "https://open.feishu.cn/open-apis/im/v1/messages"
            f"?receive_id_type={receive_id_type}"
        )
        return _post_feishu_app_json(
            channel="feishu_app",
            url=url,
            body=body,
            timeout_sec=self.timeout_sec,
            tenant_access_token=access_token_result,
        )

    def reply_text_message(
        self,
        *,
        message_id: str,
        message: NotificationMessage,
    ) -> NotificationResult:
        app_id = self.app_id.strip()
        app_secret = self.app_secret.strip()
        normalized_message_id = message_id.strip()
        if not app_id or not app_secret:
            return NotificationResult(
                success=False,
                channel="feishu_app_reply",
                error="missing_app_config",
            )
        if not normalized_message_id:
            return NotificationResult(
                success=False,
                channel="feishu_app_reply",
                error="missing_message_id",
            )

        access_token_result = self._tenant_access_token_value(
            app_id=app_id,
            app_secret=app_secret,
        )
        if isinstance(access_token_result, NotificationResult):
            return NotificationResult(
                success=False,
                channel="feishu_app_reply",
                error=access_token_result.error or "auth_failed",
            )

        body = {
            "msg_type": "text",
            "content": json.dumps(
                {"text": _format_feishu_message(message)},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
        encoded_message_id = parse.quote(normalized_message_id, safe="")
        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{encoded_message_id}/reply"
        return _post_feishu_app_json(
            channel="feishu_app_reply",
            url=url,
            body=body,
            timeout_sec=self.timeout_sec,
            tenant_access_token=access_token_result,
        )

    @classmethod
    def clear_shared_token_cache(cls) -> None:
        with cls._shared_tenant_access_tokens_lock:
            cls._shared_tenant_access_tokens.clear()

    @classmethod
    def prewarm_tenant_access_token(
        cls,
        *,
        app_id: str,
        app_secret: str,
        timeout_sec: int = 5,
    ) -> NotificationResult:
        notifier = cls(
            app_id=app_id,
            app_secret=app_secret,
            receive_id="prewarm",
            timeout_sec=timeout_sec,
        )
        token_or_result = notifier._tenant_access_token_value(
            app_id=app_id.strip(),
            app_secret=app_secret.strip(),
        )
        if isinstance(token_or_result, NotificationResult):
            return token_or_result
        return NotificationResult(success=True, channel="feishu_app")

    def _tenant_access_token_value(
        self,
        *,
        app_id: str,
        app_secret: str,
    ) -> str | NotificationResult:
        now_ts = time.time()
        shared_token = self._shared_tenant_access_token_value(
            app_id=app_id,
            app_secret=app_secret,
            now_ts=now_ts,
        )
        if shared_token:
            self._tenant_access_token = shared_token
            return shared_token
        if (
            self._tenant_access_token
            and now_ts + 60 < self._tenant_access_token_expire_at
        ):
            return self._tenant_access_token

        req = request.Request(
            url="https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps(
                {"app_id": app_id, "app_secret": app_secret},
                ensure_ascii=False,
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with request.urlopen(req, timeout=self.timeout_sec) as resp:
                if not (200 <= resp.status < 300):
                    return NotificationResult(
                        success=False,
                        channel="feishu_app",
                        error="auth_non_2xx",
                    )
                payload = _read_json_mapping(resp.read())
        except Exception as exc:  # pragma: no cover - network dependent.
            return NotificationResult(success=False, channel="feishu_app", error=str(exc))

        code = _mapping_int(payload, "code", default=0)
        if code != 0:
            return NotificationResult(
                success=False,
                channel="feishu_app",
                error=str(payload.get("msg", "auth_failed")),
            )

        token = str(payload.get("tenant_access_token", "")).strip()
        expire = max(0, _mapping_int(payload, "expire", default=0))
        if not token:
            return NotificationResult(
                success=False,
                channel="feishu_app",
                error="missing_tenant_access_token",
            )

        self._tenant_access_token = token
        ttl_sec = expire if expire > 0 else 3600
        self._tenant_access_token_expire_at = now_ts + max(60, ttl_sec - 60)
        self._write_shared_tenant_access_token(
            app_id=app_id,
            app_secret=app_secret,
            token=token,
            expire_at=self._tenant_access_token_expire_at,
        )
        return token

    @classmethod
    def _shared_tenant_access_token_value(
        cls,
        *,
        app_id: str,
        app_secret: str,
        now_ts: float,
    ) -> str:
        cache_key = (app_id, app_secret)
        with cls._shared_tenant_access_tokens_lock:
            cached = cls._shared_tenant_access_tokens.get(cache_key)
        if cached is None:
            return ""
        token, expire_at = cached
        if not token or now_ts + 60 >= expire_at:
            return ""
        return token

    @classmethod
    def _write_shared_tenant_access_token(
        cls,
        *,
        app_id: str,
        app_secret: str,
        token: str,
        expire_at: float,
    ) -> None:
        cache_key = (app_id, app_secret)
        with cls._shared_tenant_access_tokens_lock:
            cls._shared_tenant_access_tokens[cache_key] = (token, expire_at)


@dataclass(slots=True)
class FeishuEnterpriseBatchNotifier:
    app_id: str
    app_secret: str
    mode: str = "enterprise_department"
    department_ids: Sequence[str] = field(default_factory=list)
    member_ids: Sequence[str] = field(default_factory=list)
    member_id_type: str = "open_id"
    all_department_id: str = "0"
    batch_url: str = "https://open.feishu.cn/open-apis/message/v4/batch_send"
    timeout_sec: int = 5
    _tenant_access_token: str = field(default="", init=False, repr=False)
    _tenant_access_token_expire_at: float = field(default=0.0, init=False, repr=False)
    _shared_tenant_access_tokens: ClassVar[dict[tuple[str, str], tuple[str, float]]] = {}
    _shared_tenant_access_tokens_lock: ClassVar[threading.Lock] = threading.Lock()

    def send(self, message: NotificationMessage) -> NotificationResult:
        app_id = self.app_id.strip()
        app_secret = self.app_secret.strip()
        if not app_id or not app_secret:
            return NotificationResult(
                success=False,
                channel="feishu_enterprise",
                error="missing_app_config",
            )

        targets_or_result = self._target_payload()
        if isinstance(targets_or_result, NotificationResult):
            return targets_or_result

        access_token_result = self._tenant_access_token_value(
            app_id=app_id,
            app_secret=app_secret,
        )
        if isinstance(access_token_result, NotificationResult):
            return access_token_result

        body = {
            **targets_or_result,
            "msg_type": "text",
            "content": {"text": _format_feishu_message(message)},
        }
        url = self.batch_url.strip() or "https://open.feishu.cn/open-apis/message/v4/batch_send"
        return _post_feishu_app_json(
            channel="feishu_enterprise",
            url=url,
            body=body,
            timeout_sec=self.timeout_sec,
            tenant_access_token=access_token_result,
        )

    @classmethod
    def clear_shared_token_cache(cls) -> None:
        with cls._shared_tenant_access_tokens_lock:
            cls._shared_tenant_access_tokens.clear()

    def _target_payload(self) -> dict[str, object] | NotificationResult:
        mode = self.mode.strip().lower() or "enterprise_department"
        if mode == "enterprise_member_list":
            member_ids = _normalize_string_list(self.member_ids)
            if not member_ids:
                return NotificationResult(
                    success=False,
                    channel="feishu_enterprise",
                    error="missing_member_ids",
                )
            member_id_type = self.member_id_type.strip().lower() or "open_id"
            field_by_type = {
                "open_id": "open_ids",
                "user_id": "user_ids",
                "email": "emails",
            }
            field_name = field_by_type.get(member_id_type)
            if field_name is None:
                return NotificationResult(
                    success=False,
                    channel="feishu_enterprise",
                    error=f"unsupported_member_id_type:{member_id_type}",
                )
            return {field_name: member_ids}

        if mode == "enterprise_department":
            department_ids = _normalize_string_list(self.department_ids)
            if not department_ids:
                return NotificationResult(
                    success=False,
                    channel="feishu_enterprise",
                    error="missing_department_ids",
                )
            return {"department_ids": department_ids}

        if mode == "enterprise_all":
            department_id = self.all_department_id.strip() or "0"
            return {"department_ids": [department_id]}

        return NotificationResult(
            success=False,
            channel="feishu_enterprise",
            error=f"unsupported_mode:{mode}",
        )

    def _tenant_access_token_value(
        self,
        *,
        app_id: str,
        app_secret: str,
    ) -> str | NotificationResult:
        now_ts = time.time()
        shared_token = self._shared_tenant_access_token_value(
            app_id=app_id,
            app_secret=app_secret,
            now_ts=now_ts,
        )
        if shared_token:
            self._tenant_access_token = shared_token
            return shared_token
        if (
            self._tenant_access_token
            and now_ts + 60 < self._tenant_access_token_expire_at
        ):
            return self._tenant_access_token

        req = request.Request(
            url="https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps(
                {"app_id": app_id, "app_secret": app_secret},
                ensure_ascii=False,
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with request.urlopen(req, timeout=self.timeout_sec) as resp:
                if not (200 <= resp.status < 300):
                    return NotificationResult(
                        success=False,
                        channel="feishu_enterprise",
                        error="auth_non_2xx",
                    )
                payload = _read_json_mapping(resp.read())
        except Exception as exc:  # pragma: no cover - network dependent.
            return NotificationResult(
                success=False,
                channel="feishu_enterprise",
                error=str(exc),
            )

        code = _mapping_int(payload, "code", default=0)
        if code != 0:
            return NotificationResult(
                success=False,
                channel="feishu_enterprise",
                error=str(payload.get("msg", "auth_failed")),
            )

        token = str(payload.get("tenant_access_token", "")).strip()
        expire = max(0, _mapping_int(payload, "expire", default=0))
        if not token:
            return NotificationResult(
                success=False,
                channel="feishu_enterprise",
                error="missing_tenant_access_token",
            )

        self._tenant_access_token = token
        ttl_sec = expire if expire > 0 else 3600
        self._tenant_access_token_expire_at = now_ts + max(60, ttl_sec - 60)
        self._write_shared_tenant_access_token(
            app_id=app_id,
            app_secret=app_secret,
            token=token,
            expire_at=self._tenant_access_token_expire_at,
        )
        return token

    @classmethod
    def _shared_tenant_access_token_value(
        cls,
        *,
        app_id: str,
        app_secret: str,
        now_ts: float,
    ) -> str:
        cache_key = (app_id, app_secret)
        with cls._shared_tenant_access_tokens_lock:
            cached = cls._shared_tenant_access_tokens.get(cache_key)
        if cached is None:
            return ""
        token, expire_at = cached
        if not token or now_ts + 60 >= expire_at:
            return ""
        return token

    @classmethod
    def _write_shared_tenant_access_token(
        cls,
        *,
        app_id: str,
        app_secret: str,
        token: str,
        expire_at: float,
    ) -> None:
        cache_key = (app_id, app_secret)
        with cls._shared_tenant_access_tokens_lock:
            cls._shared_tenant_access_tokens[cache_key] = (token, expire_at)


@dataclass(slots=True)
class TelegramNotifier:
    bot_token: str
    chat_id: str
    message_thread_id: str = ""
    timeout_sec: int = 5

    def send(self, message: NotificationMessage) -> NotificationResult:
        if not self.bot_token:
            return NotificationResult(success=False, channel="telegram", error="missing_bot_token")
        if not self.chat_id:
            return NotificationResult(success=False, channel="telegram", error="missing_chat_id")
        payload: dict[str, object] = {
            "chat_id": self.chat_id,
            "text": _format_plain_message(message),
            "disable_web_page_preview": True,
        }
        thread_id = self.message_thread_id.strip()
        if thread_id:
            payload["message_thread_id"] = thread_id
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        return _post_telegram_json(
            channel="telegram",
            url=url,
            body=payload,
            timeout_sec=self.timeout_sec,
        )


@dataclass(slots=True)
class EmailNotifier:
    smtp_host: str
    smtp_port: int
    sender: str
    password: str
    receivers: Sequence[str]
    use_ssl: bool = True
    starttls: bool = False
    timeout_sec: int = 8

    def send(self, message: NotificationMessage) -> NotificationResult:
        if not self.smtp_host or not self.sender or not self.password:
            return NotificationResult(
                success=False,
                channel="email",
                error="missing_smtp_config",
            )
        receiver_list = [item.strip() for item in self.receivers if item.strip()]
        if not receiver_list:
            return NotificationResult(success=False, channel="email", error="missing_receivers")

        mail = EmailMessage()
        mail["Subject"] = message.title
        mail["From"] = self.sender
        mail["To"] = ",".join(receiver_list)
        mail.set_content(_format_plain_message(message))

        client: smtplib.SMTP
        try:
            if self.use_ssl:
                client = smtplib.SMTP_SSL(
                    host=self.smtp_host,
                    port=self.smtp_port,
                    timeout=self.timeout_sec,
                )
            else:
                client = smtplib.SMTP(
                    host=self.smtp_host,
                    port=self.smtp_port,
                    timeout=self.timeout_sec,
                )
            if self.starttls and not self.use_ssl:
                client.starttls(context=ssl.create_default_context())
            client.login(self.sender, self.password)
            client.send_message(mail)
            client.quit()
            return NotificationResult(success=True, channel="email")
        except Exception as exc:  # pragma: no cover - network dependent.
            return NotificationResult(success=False, channel="email", error=str(exc))


@dataclass(slots=True)
class CustomWebhookNotifier:
    webhook_url: str
    bearer_token: str = ""
    timeout_sec: int = 5

    def send(self, message: NotificationMessage) -> NotificationResult:
        if not self.webhook_url:
            return NotificationResult(
                success=False,
                channel="custom_webhook",
                error="missing_webhook",
            )
        headers: dict[str, str] = {}
        token = self.bearer_token.strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = {
            "title": message.title,
            "content": message.content,
            "level": message.level,
            "trace_id": message.trace_id,
        }
        return _post_json(
            channel="custom_webhook",
            url=self.webhook_url,
            body=body,
            timeout_sec=self.timeout_sec,
            extra_headers=headers,
        )


@dataclass(slots=True)
class FailoverNotifier:
    """Try primary channel first, then fallback channel."""

    primary: Notifier
    backup: Notifier | None = None

    def send(self, message: NotificationMessage) -> NotificationResult:
        primary_result = self.primary.send(message)
        if primary_result.success:
            return primary_result
        if self.backup is None:
            return primary_result

        backup_result = self.backup.send(message)
        if backup_result.success:
            return backup_result
        combined_error = ";".join([primary_result.error, backup_result.error]).strip(";")
        return NotificationResult(
            success=False,
            channel=f"{primary_result.channel}->{backup_result.channel}",
            error=combined_error,
        )


def _post_json(
    channel: str,
    url: str,
    body: Mapping[str, object],
    timeout_sec: int,
    extra_headers: Mapping[str, str] | None = None,
) -> NotificationResult:
    encoded = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(dict(extra_headers))
    req = request.Request(
        url=url,
        data=encoded,
        method="POST",
        headers=headers,
    )
    try:
        with request.urlopen(req, timeout=timeout_sec) as resp:
            ok = 200 <= resp.status < 300
            return NotificationResult(success=ok, channel=channel, error="" if ok else "non_2xx")
    except Exception as exc:  # pragma: no cover - network dependent.
        return NotificationResult(success=False, channel=channel, error=str(exc))


def _post_telegram_json(
    channel: str,
    url: str,
    body: Mapping[str, object],
    timeout_sec: int,
) -> NotificationResult:
    encoded = json.dumps(body).encode("utf-8")
    req = request.Request(
        url=url,
        data=encoded,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=timeout_sec) as resp:
            if not (200 <= resp.status < 300):
                return NotificationResult(success=False, channel=channel, error="non_2xx")
            raw_payload = resp.read()
            if raw_payload:
                parsed = json.loads(raw_payload.decode("utf-8"))
                if isinstance(parsed, Mapping) and not bool(parsed.get("ok", True)):
                    description = str(parsed.get("description", "telegram_error"))
                    return NotificationResult(success=False, channel=channel, error=description)
            return NotificationResult(success=True, channel=channel)
    except Exception as exc:  # pragma: no cover - network dependent.
        return NotificationResult(success=False, channel=channel, error=str(exc))


def _post_feishu_app_json(
    channel: str,
    url: str,
    body: Mapping[str, object],
    timeout_sec: int,
    tenant_access_token: str,
) -> NotificationResult:
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url=url,
        data=encoded,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {tenant_access_token}",
        },
    )
    try:
        with request.urlopen(req, timeout=timeout_sec) as resp:
            if not (200 <= resp.status < 300):
                return NotificationResult(success=False, channel=channel, error="non_2xx")
            payload = _read_json_mapping(resp.read())
            code = _mapping_int(payload, "code", default=0)
            if code != 0:
                return NotificationResult(
                    success=False,
                    channel=channel,
                    error=str(payload.get("msg", "feishu_app_error")),
                )
            return NotificationResult(success=True, channel=channel)
    except Exception as exc:  # pragma: no cover - network dependent.
        return NotificationResult(success=False, channel=channel, error=str(exc))


def _apply_title_prefix(title: str, prefix: str) -> str:
    normalized_title = title.strip()
    normalized_prefix = prefix.strip()
    if not normalized_prefix:
        return normalized_title
    if normalized_title.startswith(normalized_prefix):
        return normalized_title
    return f"{normalized_prefix}{normalized_title}"


def _strip_priority_badge(title: str) -> str:
    normalized_title = title.strip()
    for badge in ("【紧急】", "【重要】", "【日常】", "【参考】"):
        if normalized_title.startswith(badge):
            return normalized_title[len(badge) :].lstrip()
    return normalized_title


def _split_title_category(title: str) -> tuple[str, str]:
    normalized_title = title.strip()
    if not normalized_title.startswith("【"):
        return "", normalized_title
    closing = normalized_title.find("】")
    if closing <= 1:
        return "", normalized_title
    category = normalized_title[1:closing].strip()
    summary = normalized_title[closing + 1 :].strip()
    return category, summary


def _wecom_category_label(category: str) -> str:
    mapping = {
        "训练": "🎯 训练",
        "收盘": "🌙 收盘",
        "预警": "⚠️ 预警",
        "验收": "🧪 验收",
        "周报": "📰 周报",
        "盘中": "📈 盘中",
        "质量": "📋 质量",
        "配置": "⚙️ 配置",
        "系统": "🖥 系统",
        "盘前": "🌅 盘前",
        "午盘前": "⏰ 午盘前",
        "运维": "🛠 运维",
        "情报": "📡 情报",
        "行动": "⚡ 行动",
        "升级": "🚀 升级",
    }
    normalized = category.strip()
    return mapping.get(normalized, f"🔔 {normalized}" if normalized else "")


def _format_wecom_title(title: str, level: str) -> str:
    level_label = _wecom_level_label(level)
    stripped_title = _strip_priority_badge(title)
    category, summary = _split_title_category(stripped_title)
    if not category:
        return f"[{level_label}] {stripped_title}"
    category_label = _wecom_category_label(category)
    detail = summary or category
    if not category_label:
        return f"[{level_label}] {stripped_title}"
    return f"[{level_label}] [{category_label}] {detail}"


def _format_plain_message(message: NotificationMessage) -> str:
    title = message.title.strip()
    content = message.content.strip()
    if title and content:
        return f"[{message.level.upper()}] {title}\n{content}"
    if content:
        return content
    return f"[{message.level.upper()}] {title}"


_FEISHU_CATEGORY_LABELS = {
    "\u8bad\u7ec3": "\U0001f3af \u8bad\u7ec3\u901a\u77e5",
    "\u6536\u76d8": "\U0001f319 \u6536\u76d8\u63d0\u9192",
    "\u9884\u8b66": "\u26a0\ufe0f \u98ce\u9669\u63d0\u9192",
    "\u9a8c\u6536": "\U0001f9ea \u9a8c\u6536\u901a\u77e5",
    "\u5468\u62a5": "\U0001f4f0 \u5468\u62a5",
    "\u76d8\u4e2d": "\U0001f4c8 \u76d8\u4e2d\u96f7\u8fbe",
    "\u8d28\u91cf": "\U0001f4cb \u6570\u636e\u8d28\u91cf\u63d0\u9192",
    "\u914d\u7f6e": "\u2699\ufe0f \u914d\u7f6e\u66f4\u65b0",
    "\u7cfb\u7edf": "\U0001f5a5 \u7cfb\u7edf\u63d0\u9192",
    "\u76d8\u524d": "\U0001f305 \u76d8\u524d\u7b80\u62a5",
    "\u5348\u76d8\u524d": "\u23f0 \u5348\u76d8\u524d\u7b80\u62a5",
    "\u8fd0\u7ef4": "\U0001f6e0 \u8fd0\u7ef4\u63d0\u9192",
    "\u60c5\u62a5": "\U0001f4e1 \u4ea4\u6613\u60c5\u62a5",
    "\u884c\u52a8": "\u26a1 \u64cd\u4f5c\u63d0\u9192",
    "\u5347\u7ea7": "\U0001f680 \u5347\u7ea7\u901a\u77e5",
}

_FEISHU_KV_LINE_RE = re.compile(
    r"^(?P<key>[A-Za-z0-9_\-/\u4e00-\u9fff ]{1,24})=(?P<value>.+)$"
)


def _format_feishu_message(message: NotificationMessage) -> str:
    title = _format_feishu_title(message.title)
    content = _format_feishu_content(message.content)
    if title and content:
        return f"{title}\n\n{content}"
    if content:
        return content
    return title


def _format_feishu_title(title: str) -> str:
    normalized_title = title.strip()
    if not normalized_title:
        return ""
    stripped_title = _strip_feishu_priority_badge(normalized_title)
    category, summary = _split_feishu_title_category(stripped_title)
    detail = summary or category
    if not category:
        return stripped_title
    category_label = _FEISHU_CATEGORY_LABELS.get(category.strip(), category.strip())
    if not detail or detail == category:
        return category_label
    return f"{category_label}\uff5c{detail}"


def _format_feishu_content(content: str) -> str:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""
    lines = normalized.split("\n")
    rendered: list[str] = []
    last_blank = False
    for raw_line in lines:
        line = _normalize_feishu_content_line(raw_line)
        is_blank = not line
        if is_blank and last_blank:
            continue
        rendered.append(line)
        last_blank = is_blank
    return "\n".join(rendered).strip()


def _normalize_feishu_content_line(line: str) -> str:
    normalized = line.strip()
    if not normalized:
        return ""
    if "://" in normalized or "\uff1a" in normalized:
        return normalized
    matched = _FEISHU_KV_LINE_RE.match(normalized)
    if matched is None:
        return normalized
    key = matched.group("key").strip()
    value = matched.group("value").strip()
    if not key or not value:
        return normalized
    return f"{key}\uff1a{value}"


def _normalize_string_list(values: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_value in values:
        value = str(raw_value).strip()
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return normalized


def _strip_feishu_priority_badge(title: str) -> str:
    normalized = title.strip()
    for badge in (
        "\u3010\u7d27\u6025\u3011",
        "\u3010\u91cd\u8981\u3011",
        "\u3010\u65e5\u5e38\u3011",
        "\u3010\u53c2\u8003\u3011",
        "[\u7d27\u6025]",
        "[\u91cd\u8981]",
        "[\u65e5\u5e38]",
        "[\u53c2\u8003]",
    ):
        if normalized.startswith(badge):
            return normalized[len(badge) :].lstrip()
    return normalized


def _split_feishu_title_category(title: str) -> tuple[str, str]:
    normalized = title.strip()
    if not normalized:
        return "", ""
    if normalized.startswith("\u3010"):
        closing = normalized.find("\u3011")
        if closing > 1:
            return normalized[1:closing].strip(), normalized[closing + 1 :].strip()
    if normalized.startswith("["):
        closing = normalized.find("]")
        if closing > 1:
            return normalized[1:closing].strip(), normalized[closing + 1 :].strip()
    return "", normalized


def _read_json_mapping(raw_payload: bytes) -> dict[str, object]:
    if not raw_payload:
        return {}
    try:
        payload = json.loads(raw_payload.decode("utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, Mapping):
        return {}
    return {str(key): value for key, value in payload.items()}


def _mapping_int(payload: Mapping[str, object], key: str, default: int = 0) -> int:
    raw_value = payload.get(key, default)
    if not isinstance(raw_value, (int, float, str, bytes, bytearray)):
        return default
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return default


def _wecom_level_label(level: str) -> str:
    mapping = {
        "info": "🟡 日常",
        "warn": "🟠 重要",
        "warning": "🟠 重要",
        "error": "🔴 紧急",
    }
    normalized = level.strip().lower()
    return mapping.get(normalized, level)
