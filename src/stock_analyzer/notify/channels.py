"""Primary/backup notification channels."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import smtplib
import ssl
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from email.message import EmailMessage
from typing import Any, ClassVar, Protocol
from urllib import parse, request

_logger = logging.getLogger(__name__)


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


# 逐目标交付结果（正式晚报专用）。与 NotificationResult 并存：旧 ``send()`` 的
# 返回字段与语义保持不变，白天通知链路不受影响。
OUTCOME_ACCEPTED = "accepted"
OUTCOME_FAILED = "failed"
OUTCOME_UNKNOWN = "unknown"


@dataclass(slots=True)
class TargetDeliveryOutcome:
    """单个接收目标的一次投递结果。

    ``accepted`` 只表示**目标 API 明确接受**（可解析响应 + 明确业务成功），
    既不表示用户读过，也不表示内容正确。``unknown`` 表示"可能已接受但本地
    无法确认"（超时、连接中断、响应无法解析），调用方必须按不确定处理：
    可确认幂等时用同一 uuid 重试，否则停手交人工。

    ``retryable`` 只表达"重试有可能变好"。明确失败（认证/目标不存在/权限/
    参数错误）一律 False，避免高频重试刷屏并可能触发平台限频。
    """

    target_key: str
    outcome: str
    message_id: str = ""
    error_code: str = ""
    error_message: str = ""
    accepted_at: str = ""
    retryable: bool = False
    http_status: int = 0

    @property
    def delivered(self) -> bool:
        return self.outcome == OUTCOME_ACCEPTED

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

    def send_explicit(
        self,
        message: NotificationMessage,
        *,
        request_uuid: str = "",
        target_key: str = "console",
    ) -> TargetDeliveryOutcome:
        """console 输出**永远不算送达**：它只证明"日志写出去了"。

        2026-09-16 取证：主渠道失败后 FailoverNotifier 回退到 console，最终返回
        ``success=True, channel=console``，于是"夜扫成功"与"用户收到结果"被混为一谈。
        """
        _ = message, request_uuid
        return TargetDeliveryOutcome(
            target_key=target_key,
            outcome=OUTCOME_FAILED,
            error_code="console_not_a_delivery",
            error_message="console 输出不能作为送达依据",
            retryable=False,
        )


@dataclass(slots=True)
class DingTalkNotifier:
    """DingTalk custom robot webhook with optional HMAC-SHA256 signing."""

    webhook: str
    secret: str = ""
    timeout_sec: int = 5

    def send(self, message: NotificationMessage) -> NotificationResult:
        webhook = self.webhook.strip()
        if not webhook:
            _logger.warning("dingtalk notifier missing webhook; skipping send")
            return NotificationResult(success=False, channel="dingtalk", error="missing_webhook")
        body = {
            "msgtype": "markdown",
            "markdown": {
                "title": message.title.strip() or "StockAnalyzer 通知",
                "text": _format_dingtalk_message(message),
            },
        }
        url = webhook
        secret = self.secret.strip()
        if secret:
            timestamp_ms = str(int(time.time() * 1000))
            sign = _dingtalk_signature(secret=secret, timestamp_ms=timestamp_ms)
            separator = "&" if "?" in webhook else "?"
            url = f"{webhook}{separator}timestamp={timestamp_ms}&sign={sign}"
        return _post_dingtalk_json(
            channel="dingtalk",
            url=url,
            body=body,
            timeout_sec=self.timeout_sec,
        )


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

    def send_explicit(
        self,
        message: NotificationMessage,
        *,
        request_uuid: str = "",
        target_key: str = "feishu",
    ) -> TargetDeliveryOutcome:
        """严格版发送：HTTP 2xx **不足以**算成功，必须解析出明确的业务状态。

        旧 ``send()`` 走 ``_post_json`` 只看状态码，飞书自定义机器人在签名/token
        错误时会返回 HTTP 200 + ``{"code": 9499,...}``，被误判为成功。新接口只
        影响晚报交付链，不改白天通知的既有语义。webhook 无幂等参数，故不传 uuid。
        """
        _ = request_uuid
        if not self.webhook:
            return TargetDeliveryOutcome(
                target_key=target_key,
                outcome=OUTCOME_FAILED,
                error_code="missing_webhook",
                error_message="未配置 webhook",
            )
        body = {
            "msg_type": "text",
            "content": {"text": _format_feishu_message(message)},
        }
        return _post_json_strict(
            target_key=target_key,
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
        url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
        return _post_feishu_app_json(
            channel="feishu_app",
            url=url,
            body=body,
            timeout_sec=self.timeout_sec,
            tenant_access_token=access_token_result,
        )

    def send_explicit(
        self,
        message: NotificationMessage,
        *,
        request_uuid: str = "",
        target_key: str = "feishu_app",
    ) -> TargetDeliveryOutcome:
        """严格版发送，带稳定 ``uuid`` 与 ``message_id`` 回执。

        ``uuid`` 的语义（飞书官方文档：同一 uuid 在 1 小时内至多成功执行一次）
        只用于**降低**重复推送概率，不能替代本地持久化交付记录——它只覆盖 1 小时，
        而"这条今天到底发没发成功"要记一整晚并且要能跨重启追溯。故 uuid 必须由
        调用方在**首次尝试前**生成并冻结，重试沿用同一个。
        """
        app_id = self.app_id.strip()
        app_secret = self.app_secret.strip()
        receive_id = self.receive_id.strip()
        receive_id_type = self.receive_id_type.strip().lower() or "open_id"
        if not app_id or not app_secret:
            return TargetDeliveryOutcome(
                target_key=target_key,
                outcome=OUTCOME_FAILED,
                error_code="missing_app_config",
                error_message="缺少飞书应用凭据",
            )
        if not receive_id:
            return TargetDeliveryOutcome(
                target_key=target_key,
                outcome=OUTCOME_FAILED,
                error_code="missing_receive_id",
                error_message="缺少接收方配置",
            )

        access_token_result = self._tenant_access_token_value(
            app_id=app_id,
            app_secret=app_secret,
        )
        if isinstance(access_token_result, NotificationResult):
            # 取 token 失败：可能是网络抖动（unknown，值得重试），也可能是凭据错误
            # （failed，重试无意义）。按错误文本里是否含鉴权字样区分，宁可保守。
            error_text = access_token_result.error or "auth_failed"
            permanent = "missing" in error_text or "invalid" in error_text
            return TargetDeliveryOutcome(
                target_key=target_key,
                outcome=OUTCOME_FAILED if permanent else OUTCOME_UNKNOWN,
                error_code="tenant_access_token_failed",
                error_message=error_text[:200],
                retryable=not permanent,
            )

        body: dict[str, object] = {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps(
                {"text": _format_feishu_message(message)},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
        normalized_uuid = request_uuid.strip()
        if normalized_uuid:
            body["uuid"] = normalized_uuid
        url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
        return _post_feishu_app_delivery(
            target_key=target_key,
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
        if self._tenant_access_token and now_ts + 60 < self._tenant_access_token_expire_at:
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

    def send_explicit(
        self,
        message: NotificationMessage,
        *,
        request_uuid: str = "",
        target_key: str = "feishu_enterprise",
    ) -> TargetDeliveryOutcome:
        """企业分发目标（可选目标）的严格版发送。

        ``uuid`` 被忽略：批量发送接口**没有已核实的幂等参数**。因此调用方对
        ``unknown`` 结果不得自动重试（可选渠道没有幂等能力时重试就是重复推送），
        这一点由交付服务负责，不在这里假装支持。
        """
        _ = request_uuid
        app_id = self.app_id.strip()
        app_secret = self.app_secret.strip()
        if not app_id or not app_secret:
            return TargetDeliveryOutcome(
                target_key=target_key,
                outcome=OUTCOME_FAILED,
                error_code="missing_app_config",
                error_message="缺少飞书应用凭据",
            )
        targets_or_result = self._target_payload()
        if isinstance(targets_or_result, NotificationResult):
            return TargetDeliveryOutcome(
                target_key=target_key,
                outcome=OUTCOME_FAILED,
                error_code=targets_or_result.error or "invalid_target",
                error_message="企业分发目标配置无效",
            )
        access_token_result = self._tenant_access_token_value(
            app_id=app_id,
            app_secret=app_secret,
        )
        if isinstance(access_token_result, NotificationResult):
            error_text = access_token_result.error or "auth_failed"
            permanent = "missing" in error_text or "invalid" in error_text
            return TargetDeliveryOutcome(
                target_key=target_key,
                outcome=OUTCOME_FAILED if permanent else OUTCOME_UNKNOWN,
                error_code="tenant_access_token_failed",
                error_message=error_text[:200],
                retryable=not permanent,
            )
        body = {
            **targets_or_result,
            "msg_type": "text",
            "content": {"text": _format_feishu_message(message)},
        }
        url = self.batch_url.strip() or "https://open.feishu.cn/open-apis/message/v4/batch_send"
        return _post_feishu_app_delivery(
            target_key=target_key,
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
        if self._tenant_access_token and now_ts + 60 < self._tenant_access_token_expire_at:
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


class SmsGateway(Protocol):
    def send(self, body: Mapping[str, object], timeout_sec: int) -> NotificationResult:
        """Send SMS payload to the gateway and return delivery result."""


@dataclass(slots=True)
class HttpSmsGateway:
    """Generic HTTP JSON SMS gateway sender."""

    url: str
    app_key: str = ""
    app_secret: str = ""

    def send(self, body: Mapping[str, object], timeout_sec: int) -> NotificationResult:
        if not self.url.strip():
            return NotificationResult(success=False, channel="sms", error="missing_url")
        return _post_json(
            channel="sms",
            url=self.url,
            body=body,
            timeout_sec=timeout_sec,
        )


@dataclass(slots=True)
class SmsNotifier:
    """SMS channel built on an injectable SmsGateway (default: HttpSmsGateway)."""

    url: str = ""
    app_key: str = ""
    app_secret: str = ""
    sign_name: str = ""
    template_id: str = ""
    phone_numbers: Sequence[str] = field(default_factory=list)
    timeout_sec: int = 5
    gateway: SmsGateway | None = None

    def send(self, message: NotificationMessage) -> NotificationResult:
        phones = _normalize_string_list(self.phone_numbers)
        if not phones:
            _logger.warning("sms notifier missing phone numbers; skipping send")
            return NotificationResult(
                success=False,
                channel="sms",
                error="missing_phone_numbers",
            )
        if not self.url.strip():
            _logger.warning("sms notifier missing gateway url; skipping send")
            return NotificationResult(success=False, channel="sms", error="missing_url")
        gateway = self.gateway or HttpSmsGateway(
            url=self.url,
            app_key=self.app_key,
            app_secret=self.app_secret,
        )
        body: dict[str, object] = {
            "app_key": self.app_key,
            "app_secret": self.app_secret,
            "sign_name": self.sign_name,
            "template_id": self.template_id,
            "phones": phones,
            "content": _format_plain_message(message),
        }
        return gateway.send(body=body, timeout_sec=self.timeout_sec)


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


def send_explicit(
    notifier: object,
    message: NotificationMessage,
    *,
    request_uuid: str = "",
    target_key: str = "",
) -> TargetDeliveryOutcome:
    """逐目标显式交付入口：把"发送过一次"变成"这个目标到底收没收到"。

    永远不抛异常——交付服务要能把结果落盘成记录，抛出去就只剩一条日志。
    走不到严格实现时退回旧 ``send()`` 映射，但**console 一律不算送达**：旧链路
    的 failover 会把 console 的 ``success=True`` 当成整体成功（2026-09-16 实据），
    那正是"调度 green、用户没收到"的来源。
    """
    key = target_key.strip() or getattr(notifier, "channel", "") or "unknown"
    explicit = getattr(notifier, "send_explicit", None)
    if callable(explicit):
        try:
            outcome = explicit(message, request_uuid=request_uuid, target_key=key)
        except Exception as exc:  # noqa: BLE001 - 任何异常都降级为不确定结果
            return TargetDeliveryOutcome(
                target_key=key,
                outcome=OUTCOME_UNKNOWN,
                error_code="send_explicit_raised",
                error_message=f"{exc.__class__.__name__}: {exc}"[:200],
                retryable=True,
            )
        if isinstance(outcome, TargetDeliveryOutcome):
            return outcome
        return TargetDeliveryOutcome(
            target_key=key,
            outcome=OUTCOME_UNKNOWN,
            error_code="invalid_explicit_outcome",
            error_message="send_explicit 返回值类型不符",
            retryable=True,
        )
    return _legacy_send_explicit(notifier=notifier, message=message, target_key=key)


def _legacy_send_explicit(
    *,
    notifier: object,
    message: NotificationMessage,
    target_key: str,
) -> TargetDeliveryOutcome:
    sender = getattr(notifier, "send", None)
    if not callable(sender):
        return TargetDeliveryOutcome(
            target_key=target_key,
            outcome=OUTCOME_FAILED,
            error_code="notifier_has_no_send",
            error_message="目标对象既无 send_explicit 也无 send",
        )
    try:
        result = sender(message)
    except Exception as exc:  # noqa: BLE001
        return TargetDeliveryOutcome(
            target_key=target_key,
            outcome=OUTCOME_UNKNOWN,
            error_code="send_raised",
            error_message=f"{exc.__class__.__name__}: {exc}"[:200],
            retryable=True,
        )
    if not isinstance(result, NotificationResult):
        return TargetDeliveryOutcome(
            target_key=target_key,
            outcome=OUTCOME_UNKNOWN,
            error_code="invalid_send_result",
            error_message="send 返回值类型不符",
            retryable=True,
        )
    channel = str(result.channel).lower()
    if channel == "console":
        return TargetDeliveryOutcome(
            target_key=target_key,
            outcome=OUTCOME_FAILED,
            error_code="console_not_a_delivery",
            error_message="console 输出不能作为送达依据",
        )
    if result.success:
        return TargetDeliveryOutcome(target_key=target_key, outcome=OUTCOME_ACCEPTED)
    return TargetDeliveryOutcome(
        target_key=target_key,
        outcome=OUTCOME_FAILED,
        error_code="send_failed",
        error_message=str(result.error)[:200],
        retryable=True,
    )


def _post_json_strict(
    *,
    target_key: str,
    url: str,
    body: Mapping[str, object],
    timeout_sec: int,
) -> TargetDeliveryOutcome:
    """严格版 webhook 投递：HTTP 2xx 不足以算成功，必须解析出明确业务状态。"""
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url=url,
        data=encoded,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=timeout_sec) as resp:
            status = int(resp.status)
            raw_payload = resp.read()
    except Exception as exc:  # noqa: BLE001 - 网络异常按不确定处理
        return TargetDeliveryOutcome(
            target_key=target_key,
            outcome=OUTCOME_UNKNOWN,
            error_code="request_exception",
            error_message=f"{exc.__class__.__name__}: {exc}"[:200],
            retryable=True,
        )
    if status >= 500 or status == 429:
        return TargetDeliveryOutcome(
            target_key=target_key,
            outcome=OUTCOME_FAILED,
            error_code=f"http_{status}",
            error_message="服务端临时错误",
            retryable=True,
            http_status=status,
        )
    if not (200 <= status < 300):
        return TargetDeliveryOutcome(
            target_key=target_key,
            outcome=OUTCOME_FAILED,
            error_code=f"http_{status}",
            error_message="请求被拒绝（认证/权限/参数）",
            http_status=status,
        )
    payload = _read_json_mapping(raw_payload)
    if not payload or "code" not in payload:
        return TargetDeliveryOutcome(
            target_key=target_key,
            outcome=OUTCOME_UNKNOWN,
            error_code="unparseable_response",
            error_message="HTTP 2xx 但响应缺少明确业务状态",
            retryable=True,
            http_status=status,
        )
    code = _mapping_int(payload, "code", default=-1)
    if code != 0:
        return TargetDeliveryOutcome(
            target_key=target_key,
            outcome=OUTCOME_FAILED,
            error_code=str(code),
            error_message=str(payload.get("msg", ""))[:200],
            http_status=status,
        )
    return TargetDeliveryOutcome(
        target_key=target_key,
        outcome=OUTCOME_ACCEPTED,
        accepted_at=datetime.now().isoformat(),
        http_status=status,
    )


def _post_feishu_app_delivery(
    *,
    target_key: str,
    url: str,
    body: Mapping[str, object],
    timeout_sec: int,
    tenant_access_token: str,
) -> TargetDeliveryOutcome:
    """飞书应用消息的严格版投递（正式晚报的主/可选目标都走这里）。

    判定阶梯：HTTP 2xx → 响应可解析 → ``code`` 存在且为 0 → 才算 accepted。
    中间任何一步不满足都不是成功：飞书在业务失败时同样返回 HTTP 200。
    """
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
            status = int(resp.status)
            raw_payload = resp.read()
    except Exception as exc:  # noqa: BLE001
        # 超时/连接重置：请求可能已经到达服务端。按**不确定**处理，绝不当成
        # "没发出去"直接重发——那是重复推送的来源。
        return TargetDeliveryOutcome(
            target_key=target_key,
            outcome=OUTCOME_UNKNOWN,
            error_code="request_exception",
            error_message=f"{exc.__class__.__name__}: {exc}"[:200],
            retryable=True,
        )
    if status >= 500 or status == 429:
        return TargetDeliveryOutcome(
            target_key=target_key,
            outcome=OUTCOME_FAILED,
            error_code=f"http_{status}",
            error_message="飞书服务端临时错误",
            retryable=True,
            http_status=status,
        )
    if not (200 <= status < 300):
        # 4xx：认证、权限、目标不存在、参数错误——重试不会变好，记录为需处理状态。
        return TargetDeliveryOutcome(
            target_key=target_key,
            outcome=OUTCOME_FAILED,
            error_code=f"http_{status}",
            error_message="请求被拒绝（认证/权限/目标/参数）",
            http_status=status,
        )
    payload = _read_json_mapping(raw_payload)
    if not payload or "code" not in payload:
        return TargetDeliveryOutcome(
            target_key=target_key,
            outcome=OUTCOME_UNKNOWN,
            error_code="unparseable_response",
            error_message="HTTP 2xx 但响应缺少明确业务状态",
            retryable=True,
            http_status=status,
        )
    code = _mapping_int(payload, "code", default=-1)
    if code != 0:
        # 业务错误码同样返回 HTTP 200。这里不猜哪些码可重试：一律记为明确失败，
        # 交给人工核对，避免对着认证/权限类错误高频重试。
        return TargetDeliveryOutcome(
            target_key=target_key,
            outcome=OUTCOME_FAILED,
            error_code=str(code),
            error_message=str(payload.get("msg", ""))[:200],
            http_status=status,
        )
    return TargetDeliveryOutcome(
        target_key=target_key,
        outcome=OUTCOME_ACCEPTED,
        message_id=_extract_message_id(payload),
        accepted_at=datetime.now().isoformat(),
        http_status=status,
    )


def _extract_message_id(payload: Mapping[str, Any]) -> str:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return ""
    return str(data.get("message_id", "")).strip()


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
    try:
        req = request.Request(
            url=url,
            data=encoded,
            method="POST",
            headers=headers,
        )
        with request.urlopen(req, timeout=timeout_sec) as resp:
            ok = 200 <= resp.status < 300
            return NotificationResult(success=ok, channel=channel, error="" if ok else "non_2xx")
    except Exception as exc:  # pragma: no cover - network dependent.
        return NotificationResult(success=False, channel=channel, error=str(exc))


def _post_dingtalk_json(
    channel: str,
    url: str,
    body: Mapping[str, object],
    timeout_sec: int,
) -> NotificationResult:
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        req = request.Request(
            url=url,
            data=encoded,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with request.urlopen(req, timeout=timeout_sec) as resp:
            if not (200 <= resp.status < 300):
                return NotificationResult(success=False, channel=channel, error="non_2xx")
            payload = _read_json_mapping(resp.read())
            errcode = _mapping_int(payload, "errcode", default=0)
            if errcode != 0:
                return NotificationResult(
                    success=False,
                    channel=channel,
                    error=str(payload.get("errmsg", "dingtalk_error")),
                )
            return NotificationResult(success=True, channel=channel)
    except Exception as exc:  # pragma: no cover - network dependent.
        return NotificationResult(success=False, channel=channel, error=str(exc))


def _dingtalk_signature(*, secret: str, timestamp_ms: str) -> str:
    secret_enc = secret.encode("utf-8")
    string_to_sign = f"{timestamp_ms}\n{secret}".encode()
    digest = hmac.new(secret_enc, string_to_sign, digestmod=hashlib.sha256).digest()
    return parse.quote_plus(base64.b64encode(digest))


def _format_dingtalk_message(message: NotificationMessage) -> str:
    title = message.title.strip()
    content = message.content.strip()
    if title and content:
        return f"## {title}\n\n{content}"
    if content:
        return content
    if title:
        return f"## {title}"
    return f"[{message.level.upper()}]"


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

_FEISHU_KV_LINE_RE = re.compile(r"^(?P<key>[A-Za-z0-9_\-/\u4e00-\u9fff ]{1,24})=(?P<value>.+)$")


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
