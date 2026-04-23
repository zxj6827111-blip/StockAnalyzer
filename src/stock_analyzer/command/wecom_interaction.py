"""WeCom callback helpers: parsing, signature verification, and safe-mode crypto."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import struct
import time
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_SYMBOL_PATTERN = re.compile(r"^[0-9A-Za-z._-]{1,16}$")
_PKCS7_BLOCK_SIZE = 32


class WeComCryptoError(ValueError):
    """Raised when WeCom safe-mode encrypt/decrypt fails."""


@dataclass(slots=True)
class WeComParsedCommand:
    kind: str
    action: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    query: str = ""
    error: str = ""


_BUY_COMMANDS = {"买入", "今天买入", "今日买入", "建仓", "调仓", "设仓", "set_position", "set"}
_SELL_COMMANDS = {"卖出", "今天卖出", "今日卖出", "平仓", "close_position", "close"}


def parse_text_command(content: str) -> WeComParsedCommand:
    normalized = _normalize_text(content)
    if not normalized:
        return WeComParsedCommand(kind="invalid", error="empty command")

    lowered = normalized.lower()
    if normalized in {"帮助", "命令", "help", "?"} or lowered == "help":
        return WeComParsedCommand(kind="help")

    if normalized in {"持仓", "仓位", "positions"} or lowered == "positions":
        return WeComParsedCommand(kind="query", query="positions")

    if normalized in {"成交", "最近成交", "trades"} or lowered == "trades":
        return WeComParsedCommand(kind="query", query="trades")

    if lowered in {"mode", "execution_mode", "advisory"}:
        return WeComParsedCommand(kind="query", query="execution_mode_state")

    if normalized in {"全平", "清仓", "close_all"} or lowered in {
        "close_all",
        "close all",
        "flatten",
    }:
        return WeComParsedCommand(kind="execute", action="CLOSE_ALL_POSITIONS", payload={})

    if normalized in {"暂停开仓", "暂停买入", "pause"} or lowered == "pause":
        return WeComParsedCommand(kind="execute", action="PAUSE_NEW_BUY", payload={})

    if normalized in {"恢复开仓", "恢复买入", "resume"} or lowered == "resume":
        return WeComParsedCommand(kind="execute", action="RESUME_NEW_BUY", payload={})

    if normalized in {"对账", "reconcile"} or lowered == "reconcile":
        return WeComParsedCommand(kind="execute", action="RUN_RECONCILE", payload={})

    if normalized in {"确认对账", "ack对账", "ack_reconcile"} or lowered in {
        "ack_reconcile",
        "ack reconcile",
    }:
        return WeComParsedCommand(kind="execute", action="ACK_RECONCILE", payload={})

    tokens = normalized.split()
    if len(tokens) >= 2 and tokens[0].lower() in {"news", "news_score", "score"}:
        symbol = _normalize_symbol(tokens[1])
        if not symbol:
            return WeComParsedCommand(kind="invalid", error="invalid symbol")
        strategy = "trend"
        if len(tokens) >= 3:
            strategy = tokens[2].strip().lower() or "trend"
        return WeComParsedCommand(
            kind="query",
            query="news_score",
            payload={"symbol": symbol, "strategy": strategy},
        )
    if tokens and tokens[0].lower() in {"newslist", "news_watchlist", "news_watch"}:
        limit = 10
        strategy = "trend"
        if len(tokens) >= 2:
            second = tokens[1].strip()
            if second.isdigit():
                parsed_limit = int(second)
                if parsed_limit <= 0:
                    return WeComParsedCommand(kind="invalid", error="invalid limit")
                limit = min(parsed_limit, 50)
            else:
                strategy = second.lower() or "trend"
        if len(tokens) >= 3:
            strategy = tokens[2].strip().lower() or "trend"
        return WeComParsedCommand(
            kind="query",
            query="news_watchlist",
            payload={"limit": limit, "strategy": strategy},
        )
    if tokens and tokens[0].lower() in {"newscache", "news_cache"}:
        op = tokens[1].strip().lower() if len(tokens) >= 2 else "state"
        if op in {"state", "status"}:
            return WeComParsedCommand(
                kind="query",
                query="news_cache_state",
                payload={},
            )
        if op == "clear":
            symbol = ""
            strategy = ""
            if len(tokens) >= 3:
                raw_symbol = _normalize_symbol(tokens[2])
                if raw_symbol:
                    symbol = raw_symbol
                else:
                    strategy = tokens[2].strip().lower()
            if len(tokens) >= 4:
                strategy = tokens[3].strip().lower()
            return WeComParsedCommand(
                kind="query",
                query="news_cache_clear",
                payload={"symbol": symbol, "strategy": strategy},
            )
        return WeComParsedCommand(kind="invalid", error="unsupported news cache command")
    if tokens and tokens[0].lower() in {"newshistory", "news_history"}:
        limit = 10
        symbol = ""
        strategy = ""
        cursor = 1
        if len(tokens) > cursor and tokens[cursor].strip().isdigit():
            parsed_limit = int(tokens[cursor].strip())
            if parsed_limit <= 0:
                return WeComParsedCommand(kind="invalid", error="invalid limit")
            limit = min(parsed_limit, 50)
            cursor += 1
        if len(tokens) > cursor:
            maybe_symbol = _normalize_symbol(tokens[cursor].strip())
            if maybe_symbol:
                symbol = maybe_symbol
                cursor += 1
        if len(tokens) > cursor:
            strategy = tokens[cursor].strip().lower()
        return WeComParsedCommand(
            kind="query",
            query="news_history",
            payload={"limit": limit, "symbol": symbol, "strategy": strategy},
        )
    if tokens and tokens[0].lower() in {"mode", "execution_mode", "advisory"}:
        query_head = tokens[0].strip().lower()
        args = [item.strip().lower() for item in tokens[1:]]
        if not args or args[0] in {"status", "state"}:
            return WeComParsedCommand(kind="query", query="execution_mode_state")
        if query_head in {"mode", "execution_mode"} and args and args[0] == "advisory":
            args = args[1:]
        if not args:
            return WeComParsedCommand(kind="query", query="execution_mode_state")
        token = args[0]
        if token in {"on", "true", "1", "enable"}:
            return WeComParsedCommand(
                kind="query",
                query="execution_mode_set",
                payload={"advisory_only": True},
            )
        if token in {"off", "false", "0", "disable"}:
            return WeComParsedCommand(
                kind="query",
                query="execution_mode_set",
                payload={"advisory_only": False},
            )
        return WeComParsedCommand(kind="invalid", error="unsupported mode command")
    if tokens and tokens[0].lower() in {"lifecycle", "rec", "recommendation", "recommendations"}:
        status = ""
        limit = 10
        statuses = {"recommended", "bought", "watching", "dropped"}
        for raw in tokens[1:]:
            token = raw.strip().lower()
            if token in statuses:
                status = token
                continue
            if token.isdigit():
                parsed_limit = int(token)
                if parsed_limit <= 0:
                    return WeComParsedCommand(kind="invalid", error="invalid limit")
                limit = min(parsed_limit, 50)
        return WeComParsedCommand(
            kind="query",
            query="recommendation_lifecycle",
            payload={"status": status, "limit": limit},
        )
    if tokens and tokens[0].lower() in {"holdingalerts", "holding_alerts", "halerts"}:
        severity = ""
        if len(tokens) >= 2:
            level = tokens[1].strip().lower()
            if level in {"warn", "info"}:
                severity = level
        return WeComParsedCommand(
            kind="query",
            query="holding_alerts",
            payload={"severity": severity},
        )
    if tokens and tokens[0].lower() in {"bias", "execution_bias", "biasreport"}:
        days = 7
        limit = 10
        if len(tokens) >= 2 and tokens[1].strip().isdigit():
            parsed_days = int(tokens[1].strip())
            if parsed_days <= 0:
                return WeComParsedCommand(kind="invalid", error="invalid days")
            days = min(parsed_days, 90)
        if len(tokens) >= 3 and tokens[2].strip().isdigit():
            parsed_limit = int(tokens[2].strip())
            if parsed_limit <= 0:
                return WeComParsedCommand(kind="invalid", error="invalid limit")
            limit = min(parsed_limit, 50)
        return WeComParsedCommand(
            kind="query",
            query="execution_bias",
            payload={"days": days, "limit": limit},
        )
    if len(tokens) >= 3 and tokens[0].lower() in {
        "recstatus",
        "set_rec_status",
        "recommendation_status",
    }:
        symbol = _normalize_symbol(tokens[1])
        if not symbol:
            return WeComParsedCommand(kind="invalid", error="invalid symbol")
        status = tokens[2].strip().lower()
        if status not in {"recommended", "bought", "watching", "dropped"}:
            return WeComParsedCommand(kind="invalid", error="invalid recommendation status")
        note = " ".join(tokens[3:]).strip() if len(tokens) > 3 else ""
        payload: dict[str, Any] = {
            "symbol": symbol,
            "status": status,
            "strategy": "manual",
        }
        if note:
            payload["note"] = note
        return WeComParsedCommand(
            kind="execute",
            action="SET_RECOMMENDATION_STATUS",
            payload=payload,
        )

    trade_command = _parse_trade_command(tokens)
    if trade_command is not None:
        return trade_command

    if normalized.startswith("同步持仓 ") or normalized.startswith("snapshot "):
        raw_list = normalized.split(" ", maxsplit=1)[1]
        positions = _parse_positions(raw_list)
        if not positions:
            return WeComParsedCommand(kind="invalid", error="invalid positions payload")
        return WeComParsedCommand(
            kind="execute",
            action="SET_BROKER_POSITIONS",
            payload={"positions": positions},
        )

    return WeComParsedCommand(kind="invalid", error="unsupported command")


def parse_wecom_command(content: str) -> WeComParsedCommand:
    return parse_text_command(content)


def build_wecom_help_text() -> str:
    return (
        "支持命令:\n"
        "1) 建仓 600000 20%\n"
        "2) 买入 600000 仓位20% 价格9.88 数量800 手续费2.5\n"
        "3) 买入 600000 价格9.88 数量800 总资产100000\n"
        "4) 卖出 600000 价格10.12 数量800 手续费2\n"
        "5) 平仓 600000\n"
        "6) 全平\n"
        "7) 暂停开仓 / 恢复开仓\n"
        "8) 同步持仓 600000:20%,000001:10%\n"
        "9) 对账\n"
        "10) 持仓 / 成交\n"
        "11) news 600000 [trend]\n"
        "12) newslist [limit] [strategy]\n"
        "13) newscache state\n"
        "14) newscache clear [symbol] [strategy]\n"
        "15) newshistory [limit] [symbol] [strategy]\n"
        "16) mode (查看执行模式)\n"
        "17) mode advisory on/off\n"
        "18) lifecycle [status] [limit]\n"
        "19) recstatus 600000 bought [note]\n"
        "20) holdingalerts [warn|info]\n"
        "21) bias [days] [limit]"
    )


def format_positions_text(positions: list[dict[str, object]], max_items: int = 10) -> str:
    if not positions:
        return "当前无持仓"
    lines = [f"当前持仓 {len(positions)} 条"]
    for index, item in enumerate(positions[:max_items], start=1):
        symbol = str(item.get("symbol", "")).strip()
        strategy = str(item.get("strategy", "")).strip() or "manual"
        target = _as_float(item.get("target_position"), default=0.0)
        entry_price = _as_float(item.get("entry_price"), default=0.0)
        quantity = _as_int(item.get("quantity"), default=0)
        parts = [f"{index}. {symbol}", f"仓位 {_format_pct(target)}"]
        if entry_price > 0:
            parts.append(f"成本 {entry_price:g}")
        if quantity > 0:
            parts.append(f"数量 {quantity}")
        if strategy and strategy != "manual":
            parts.append(f"策略 {strategy}")
        lines.append(" | ".join(parts))
    if len(positions) > max_items:
        lines.append(f"... 其余 {len(positions) - max_items} 条省略")
    return "\n".join(lines)


def format_trades_text(trades: list[dict[str, object]], max_items: int = 8) -> str:
    if not trades:
        return "暂无成交记录"
    lines = [f"最近成交 {min(len(trades), max_items)} 条"]
    recent_trades = list(reversed(trades[-max_items:]))
    for index, item in enumerate(recent_trades, start=1):
        side = str(item.get("side", "")).strip().lower()
        symbol = str(item.get("symbol", "")).strip()
        target = _as_float(item.get("target_position"), default=0.0)
        entry_price = _as_float(item.get("entry_price"), default=0.0)
        exit_price = _as_float(item.get("exit_price"), default=0.0)
        quantity = _as_int(item.get("quantity"), default=0)
        exit_quantity = _as_int(item.get("exit_quantity"), default=0)
        timestamp = _format_trade_timestamp(str(item.get("timestamp", "")).strip())
        display_price = exit_price if side == "sell" and exit_price > 0 else entry_price
        display_quantity = exit_quantity if side == "sell" and exit_quantity > 0 else quantity
        parts = [f"{index}. {_side_label(side)} {symbol}", f"仓位 {_format_pct(target)}"]
        if display_price > 0:
            parts.append(f"价格 {display_price:g}")
        if display_quantity > 0:
            parts.append(f"数量 {display_quantity}")
        if timestamp:
            parts.append(timestamp)
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def build_wecom_signature(token: str, timestamp: str, nonce: str, payload: str = "") -> str:
    parts = [token.strip(), str(timestamp).strip(), str(nonce).strip()]
    if payload:
        parts.append(payload.strip())
    raw = "".join(sorted(parts))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def verify_wecom_signature(
    token: str,
    signature: str,
    timestamp: str,
    nonce: str,
    payload: str = "",
) -> bool:
    if not token.strip():
        return True
    normalized = signature.strip().lower()
    if not normalized:
        return False
    candidates = {
        build_wecom_signature(token=token, timestamp=timestamp, nonce=nonce, payload=""),
    }
    if payload:
        candidates.add(
            build_wecom_signature(
                token=token,
                timestamp=timestamp,
                nonce=nonce,
                payload=payload,
            )
        )
    return normalized in {item.lower() for item in candidates}


def parse_wecom_xml(xml_body: str) -> dict[str, str]:
    try:
        root = ElementTree.fromstring(xml_body)
    except ElementTree.ParseError as exc:
        raise ValueError("invalid xml body") from exc
    parsed: dict[str, str] = {}
    for node in list(root):
        parsed[node.tag] = (node.text or "").strip()
    return parsed


def build_text_reply_xml(
    *,
    to_user: str,
    from_user: str,
    content: str,
    create_time: int | None = None,
) -> str:
    ts = create_time if create_time is not None else int(time.time())
    safe_content = content.replace("]]>", "]]]]><![CDATA[>")
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{ts}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{safe_content}]]></Content>"
        "</xml>"
    )


def build_encrypted_reply_xml(
    *,
    token: str,
    encrypt: str,
    timestamp: str | None = None,
    nonce: str | None = None,
) -> str:
    ts = str(timestamp or int(time.time()))
    nonce_value = str(nonce or os.urandom(6).hex())
    signature = build_wecom_signature(
        token=token,
        timestamp=ts,
        nonce=nonce_value,
        payload=encrypt,
    )
    return (
        "<xml>"
        f"<Encrypt><![CDATA[{encrypt}]]></Encrypt>"
        f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
        f"<TimeStamp>{ts}</TimeStamp>"
        f"<Nonce><![CDATA[{nonce_value}]]></Nonce>"
        "</xml>"
    )


def encrypt_wecom_payload(plain_text: str, encoding_aes_key: str, receive_id: str) -> str:
    key = _decode_encoding_aes_key(encoding_aes_key)
    iv = key[:16]
    plain_bytes = plain_text.encode("utf-8")
    receive_id_bytes = receive_id.encode("utf-8")
    packed = os.urandom(16) + struct.pack(">I", len(plain_bytes)) + plain_bytes + receive_id_bytes
    padded = _pkcs7_pad(packed)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("utf-8")


def decrypt_wecom_payload(
    encrypted: str,
    encoding_aes_key: str,
    expected_receive_id: str = "",
    enforce_receive_id: bool = False,
) -> tuple[str, str]:
    key = _decode_encoding_aes_key(encoding_aes_key)
    iv = key[:16]
    try:
        encrypted_bytes = base64.b64decode(encrypted)
    except Exception as exc:  # pragma: no cover - defensive path.
        raise WeComCryptoError("invalid base64 payload") from exc
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(encrypted_bytes) + decryptor.finalize()
    raw = _pkcs7_unpad(padded)
    if len(raw) < 20:
        raise WeComCryptoError("decrypted payload too short")

    msg_len = struct.unpack(">I", raw[16:20])[0]
    if msg_len < 0:
        raise WeComCryptoError("negative message length")
    msg_start = 20
    msg_end = msg_start + msg_len
    if msg_end > len(raw):
        raise WeComCryptoError("invalid message length")
    message = raw[msg_start:msg_end].decode("utf-8")
    receive_id = raw[msg_end:].decode("utf-8")
    normalized_expected = expected_receive_id.strip()
    if (
        enforce_receive_id
        and normalized_expected
        and receive_id.strip()
        and receive_id.strip() != normalized_expected
    ):
        raise WeComCryptoError("receive_id mismatch")
    return message, receive_id


def _normalize_text(raw: str) -> str:
    cleaned = (
        str(raw)
        .strip()
        .replace("，", ",")
        .replace("：", ":")
        .replace("＝", "=")
        .replace("％", "%")
    )
    return " ".join(cleaned.split())


def _normalize_symbol(raw: str) -> str:
    symbol = str(raw).strip().upper()
    if not _SYMBOL_PATTERN.match(symbol):
        return ""
    return symbol


def _parse_position(raw: str, *, allow_zero: bool) -> float | None:
    text = str(raw).strip()
    if not text:
        return None
    is_percent = text.endswith("%")
    if is_percent:
        text = text[:-1].strip()
    try:
        value = float(text)
    except ValueError:
        return None
    normalized = value / 100.0 if is_percent or value > 1.0 else value
    if normalized < 0:
        return None
    if not allow_zero and normalized <= 0:
        return None
    if normalized > 1.0:
        return None
    return round(normalized, 6)


def _parse_positions(raw_list: str) -> list[dict[str, object]]:
    source = str(raw_list).strip()
    if not source:
        return []
    items = [item.strip() for item in source.split(",") if item.strip()]
    positions: list[dict[str, object]] = []
    for item in items:
        separator = ":" if ":" in item else "="
        if separator not in item:
            return []
        symbol_raw, position_raw = item.split(separator, maxsplit=1)
        symbol = _normalize_symbol(symbol_raw)
        if not symbol:
            return []
        target = _parse_position(position_raw, allow_zero=True)
        if target is None:
            return []
        positions.append({"symbol": symbol, "target_position": target})
    return positions


def _parse_trade_command(tokens: list[str]) -> WeComParsedCommand | None:
    if not tokens:
        return None
    head = tokens[0].strip()
    head_lower = head.lower()
    if head in _BUY_COMMANDS or head_lower in _BUY_COMMANDS:
        return _parse_buy_command(tokens)
    if head in _SELL_COMMANDS or head_lower in _SELL_COMMANDS:
        return _parse_sell_command(tokens)
    return None


def _parse_buy_command(tokens: list[str]) -> WeComParsedCommand:
    if len(tokens) < 2:
        return WeComParsedCommand(kind="invalid", error="invalid symbol")
    symbol = _normalize_symbol(tokens[1])
    if not symbol:
        return WeComParsedCommand(kind="invalid", error="invalid symbol")

    payload: dict[str, Any] = {"symbol": symbol, "strategy": "manual"}
    remaining = list(tokens[2:])
    if remaining:
        implied_target = _parse_position(remaining[0], allow_zero=False)
        if implied_target is not None:
            payload["target_position"] = implied_target
            remaining = remaining[1:]

    field_payload, error = _parse_trade_fields(
        remaining,
        field_map={
            "target_position": ("仓位", "target", "position"),
            "entry_price": ("价格", "price", "成交价", "买入价", "entry_price"),
            "quantity": ("数量", "qty", "quantity", "股数"),
            "fee": ("手续费", "fee"),
            "account": ("账户", "account"),
            "trade_time": ("时间", "trade_time", "成交时间"),
            "total_asset": ("总资产", "total_asset", "asset"),
            "note": ("备注", "note"),
        },
    )
    if error:
        return WeComParsedCommand(kind="invalid", error=error)
    payload.update(field_payload)
    return WeComParsedCommand(kind="execute", action="SET_POSITION", payload=payload)


def _parse_sell_command(tokens: list[str]) -> WeComParsedCommand:
    if len(tokens) < 2:
        return WeComParsedCommand(kind="invalid", error="invalid symbol")
    symbol = _normalize_symbol(tokens[1])
    if not symbol:
        return WeComParsedCommand(kind="invalid", error="invalid symbol")

    payload: dict[str, Any] = {"symbol": symbol}
    field_payload, error = _parse_trade_fields(
        tokens[2:],
        field_map={
            "exit_price": ("价格", "price", "成交价", "卖出价", "exit_price"),
            "quantity": ("数量", "qty", "quantity", "股数"),
            "fee": ("手续费", "fee"),
            "account": ("账户", "account"),
            "trade_time": ("时间", "trade_time", "成交时间"),
            "note": ("备注", "note"),
        },
    )
    if error:
        return WeComParsedCommand(kind="invalid", error=error)
    payload.update(field_payload)
    return WeComParsedCommand(kind="execute", action="CLOSE_POSITION", payload=payload)


def _parse_trade_fields(
    tokens: list[str],
    *,
    field_map: dict[str, tuple[str, ...]],
) -> tuple[dict[str, Any], str]:
    parsed: dict[str, Any] = {}
    idx = 0
    while idx < len(tokens):
        token = tokens[idx].strip()
        if not token:
            idx += 1
            continue
        field_name, value_text, consumes_next = _match_labeled_field(
            token=token,
            next_token=tokens[idx + 1].strip() if idx + 1 < len(tokens) else "",
            field_map=field_map,
        )
        if not field_name:
            idx += 1
            continue
        if not value_text:
            return {}, f"missing {field_name}"
        if field_name == "note" and consumes_next:
            value_text = " ".join(item.strip() for item in tokens[idx + 1 :] if item.strip())
        normalized_value = _parse_trade_field_value(field_name=field_name, raw=value_text)
        if normalized_value is None:
            return {}, f"invalid {field_name}"
        parsed[field_name] = normalized_value
        idx += 2 if consumes_next else 1
        if field_name == "note" and consumes_next:
            break
    return parsed, ""


def _match_labeled_field(
    *,
    token: str,
    next_token: str,
    field_map: dict[str, tuple[str, ...]],
) -> tuple[str, str, bool]:
    normalized = token.strip()
    for field_name, aliases in field_map.items():
        for alias in sorted(aliases, key=len, reverse=True):
            if not alias:
                continue
            lowered = normalized.lower()
            alias_lower = alias.lower()
            if lowered == alias_lower:
                return field_name, next_token, True
            for separator in (":", "="):
                prefix = f"{alias_lower}{separator}"
                if lowered.startswith(prefix):
                    return field_name, normalized[len(alias) + 1 :].strip(), False
            if lowered.startswith(alias_lower) and len(normalized) > len(alias):
                return field_name, normalized[len(alias) :].strip(), False
    return "", "", False


def _parse_trade_field_value(field_name: str, raw: str) -> Any | None:
    value = str(raw).strip()
    if not value:
        return None
    if field_name == "target_position":
        return _parse_position(value, allow_zero=False)
    if field_name in {"entry_price", "exit_price", "fee", "total_asset"}:
        return _parse_numeric_value(value, allow_zero=field_name == "fee")
    if field_name == "quantity":
        return _parse_quantity_value(value)
    return value


def _parse_numeric_value(raw: str, *, allow_zero: bool) -> float | None:
    cleaned = str(raw).strip().replace(",", "")
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if value < 0:
        return None
    if not allow_zero and value <= 0:
        return None
    return round(value, 6)


def _parse_quantity_value(raw: str) -> int | None:
    cleaned = str(raw).strip().replace(",", "")
    for suffix in ("股", "shares", "share"):
        if cleaned.lower().endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()
            break
    if not cleaned:
        return None
    try:
        quantity = int(float(cleaned))
    except ValueError:
        return None
    if quantity <= 0:
        return None
    return quantity


def _format_pct(value: float) -> str:
    return f"{value:.2%}"


def _side_label(side: str) -> str:
    if side == "buy":
        return "买入"
    if side == "sell":
        return "卖出"
    return side or "成交"


def _format_trade_timestamp(raw: str) -> str:
    value = str(raw).strip()
    if not value:
        return ""
    try:
        parsed = time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return value.replace("T", " ")[:16]
    return time.strftime("%m-%d %H:%M", parsed)


def _decode_encoding_aes_key(encoding_aes_key: str) -> bytes:
    normalized = encoding_aes_key.strip()
    if not normalized:
        raise WeComCryptoError("missing encoding_aes_key")
    try:
        decoded = base64.b64decode(f"{normalized}=")
    except Exception as exc:  # pragma: no cover - defensive path.
        raise WeComCryptoError("invalid encoding_aes_key") from exc
    if len(decoded) != 32:
        raise WeComCryptoError("encoding_aes_key length must decode to 32 bytes")
    return decoded


def _pkcs7_pad(raw: bytes, block_size: int = _PKCS7_BLOCK_SIZE) -> bytes:
    pad_len = block_size - (len(raw) % block_size)
    if pad_len == 0:
        pad_len = block_size
    return raw + bytes([pad_len]) * pad_len


def _pkcs7_unpad(raw: bytes, block_size: int = _PKCS7_BLOCK_SIZE) -> bytes:
    if not raw:
        raise WeComCryptoError("empty payload")
    pad_len = raw[-1]
    if pad_len < 1 or pad_len > block_size:
        raise WeComCryptoError("invalid padding length")
    if raw[-pad_len:] != bytes([pad_len]) * pad_len:
        raise WeComCryptoError("invalid padding bytes")
    return raw[:-pad_len]


def _as_float(value: object, default: float = 0.0) -> float:
    if not isinstance(value, (int, float, str, bytes, bytearray)):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: object, default: int = 0) -> int:
    if not isinstance(value, (int, float, str, bytes, bytearray)):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default



