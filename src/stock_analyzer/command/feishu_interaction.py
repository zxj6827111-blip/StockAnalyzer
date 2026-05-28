"""Feishu callback helpers for event subscription and message parsing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class FeishuUrlVerification:
    challenge: str
    token: str


@dataclass(slots=True)
class FeishuMessageEvent:
    event_id: str
    event_type: str
    message_id: str
    chat_id: str
    chat_type: str
    message_type: str
    text: str
    open_id: str
    user_id: str
    union_id: str
    sender_type: str


def parse_feishu_url_verification(payload: dict[str, Any]) -> FeishuUrlVerification | None:
    if not isinstance(payload, dict):
        return None
    request_type = str(payload.get("type", "")).strip().lower()
    if request_type != "url_verification":
        return None
    return FeishuUrlVerification(
        challenge=str(payload.get("challenge", "")).strip(),
        token=str(payload.get("token", "")).strip(),
    )


def feishu_event_type(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    header = payload.get("header")
    if isinstance(header, dict):
        return str(header.get("event_type", "")).strip()
    return ""


def feishu_payload_is_encrypted(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    encrypted = payload.get("encrypt")
    return isinstance(encrypted, str) and bool(encrypted.strip())


def verify_feishu_token(payload: dict[str, Any], expected_token: str) -> bool:
    normalized_expected = expected_token.strip()
    if not normalized_expected:
        return False

    candidates: list[str] = []
    top_level = str(payload.get("token", "")).strip()
    if top_level:
        candidates.append(top_level)
    header = payload.get("header")
    if isinstance(header, dict):
        header_token = str(header.get("token", "")).strip()
        if header_token:
            candidates.append(header_token)
    return normalized_expected in candidates


def parse_feishu_message_event(payload: dict[str, Any]) -> FeishuMessageEvent:
    event_type = feishu_event_type(payload)
    if event_type != "im.message.receive_v1":
        raise ValueError("unsupported_event_type")

    header = payload.get("header")
    event = payload.get("event")
    if not isinstance(header, dict) or not isinstance(event, dict):
        raise ValueError("invalid_event_payload")

    sender = event.get("sender")
    message = event.get("message")
    if not isinstance(sender, dict) or not isinstance(message, dict):
        raise ValueError("invalid_message_event")

    sender_id = sender.get("sender_id")
    if not isinstance(sender_id, dict):
        sender_id = {}

    message_type = str(message.get("message_type", "")).strip().lower()
    content = str(message.get("content", "")).strip()
    text = _extract_feishu_text_content(content) if message_type == "text" else ""

    return FeishuMessageEvent(
        event_id=str(header.get("event_id", "")).strip(),
        event_type=event_type,
        message_id=str(message.get("message_id", "")).strip(),
        chat_id=str(message.get("chat_id", "")).strip(),
        chat_type=str(message.get("chat_type", "")).strip(),
        message_type=message_type,
        text=text,
        open_id=str(sender_id.get("open_id", "")).strip(),
        user_id=str(sender_id.get("user_id", "")).strip(),
        union_id=str(sender_id.get("union_id", "")).strip(),
        sender_type=str(sender.get("sender_type", "")).strip().lower(),
    )


def build_feishu_message_event_from_sdk(event: Any) -> FeishuMessageEvent:
    header = getattr(event, "header", None)
    payload = getattr(event, "event", None)
    sender = getattr(payload, "sender", None)
    message = getattr(payload, "message", None)
    sender_id = getattr(sender, "sender_id", None)
    message_type = str(getattr(message, "message_type", "") or "").strip().lower()
    content = str(getattr(message, "content", "") or "").strip()
    text = _extract_feishu_text_content(content) if message_type == "text" else ""
    return FeishuMessageEvent(
        event_id=str(getattr(header, "event_id", "") or "").strip(),
        event_type=str(getattr(header, "event_type", "") or "").strip(),
        message_id=str(getattr(message, "message_id", "") or "").strip(),
        chat_id=str(getattr(message, "chat_id", "") or "").strip(),
        chat_type=str(getattr(message, "chat_type", "") or "").strip(),
        message_type=message_type,
        text=text,
        open_id=str(getattr(sender_id, "open_id", "") or "").strip(),
        user_id=str(getattr(sender_id, "user_id", "") or "").strip(),
        union_id=str(getattr(sender_id, "union_id", "") or "").strip(),
        sender_type=str(getattr(sender, "sender_type", "") or "").strip().lower(),
    )


def _extract_feishu_text_content(raw_content: str) -> str:
    if not raw_content:
        return ""
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError:
        return raw_content.strip()
    if not isinstance(parsed, dict):
        return raw_content.strip()
    return str(parsed.get("text", "")).strip()
