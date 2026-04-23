from __future__ import annotations

import base64

from stock_analyzer.command.wecom_interaction import (
    build_encrypted_reply_xml,
    build_text_reply_xml,
    build_wecom_help_text,
    build_wecom_signature,
    decrypt_wecom_payload,
    encrypt_wecom_payload,
    format_positions_text,
    parse_wecom_command,
    parse_wecom_xml,
    verify_wecom_signature,
)


def _aes_key() -> str:
    raw = b"0123456789abcdef0123456789abcdef"
    return base64.b64encode(raw).decode("utf-8").rstrip("=")


def test_parse_wecom_command_set_position() -> None:
    parsed = parse_wecom_command("建仓 600000 20%")
    assert parsed.kind == "execute"
    assert parsed.action == "SET_POSITION"
    assert parsed.payload["symbol"] == "600000"
    assert float(parsed.payload["target_position"]) == 0.2


def test_parse_wecom_command_buy_with_labeled_fields() -> None:
    parsed = parse_wecom_command("买入 600000 仓位20% 价格9.88 数量800 手续费2.5")
    assert parsed.kind == "execute"
    assert parsed.action == "SET_POSITION"
    assert parsed.payload["symbol"] == "600000"
    assert float(parsed.payload["target_position"]) == 0.2
    assert float(parsed.payload["entry_price"]) == 9.88
    assert int(parsed.payload["quantity"]) == 800
    assert float(parsed.payload["fee"]) == 2.5


def test_parse_wecom_command_buy_supports_total_asset_inference_fields() -> None:
    parsed = parse_wecom_command("买入 600000 价格9.88 数量800 总资产100000")
    assert parsed.kind == "execute"
    assert parsed.action == "SET_POSITION"
    assert parsed.payload["symbol"] == "600000"
    assert "target_position" not in parsed.payload
    assert float(parsed.payload["entry_price"]) == 9.88
    assert int(parsed.payload["quantity"]) == 800
    assert float(parsed.payload["total_asset"]) == 100000.0


def test_parse_wecom_command_sell_with_labeled_fields() -> None:
    parsed = parse_wecom_command("卖出 600000 价格10.12 数量800 手续费2")
    assert parsed.kind == "execute"
    assert parsed.action == "CLOSE_POSITION"
    assert parsed.payload["symbol"] == "600000"
    assert float(parsed.payload["exit_price"]) == 10.12
    assert int(parsed.payload["quantity"]) == 800
    assert float(parsed.payload["fee"]) == 2.0


def test_parse_wecom_command_sync_snapshot() -> None:
    parsed = parse_wecom_command("同步持仓 600000:20%,000001:10%")
    assert parsed.kind == "execute"
    assert parsed.action == "SET_BROKER_POSITIONS"
    positions = parsed.payload["positions"]
    assert isinstance(positions, list)
    assert len(positions) == 2


def test_parse_wecom_command_unknown_returns_invalid() -> None:
    parsed = parse_wecom_command("unknown-command")
    assert parsed.kind == "invalid"
    assert parsed.error


def test_verify_wecom_signature_supports_plain_payload_modes() -> None:
    token = "test-token"
    timestamp = "1700000000"
    nonce = "n1"
    payload = "echo-value"
    signature = build_wecom_signature(token, timestamp, nonce, payload)
    assert (
        verify_wecom_signature(
            token=token,
            signature=signature,
            timestamp=timestamp,
            nonce=nonce,
            payload=payload,
        )
        is True
    )


def test_parse_wecom_xml_and_build_reply_xml() -> None:
    inbound = (
        "<xml>"
        "<ToUserName><![CDATA[ww-corp]]></ToUserName>"
        "<FromUserName><![CDATA[user_a]]></FromUserName>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[持仓]]></Content>"
        "</xml>"
    )
    parsed = parse_wecom_xml(inbound)
    assert parsed["MsgType"] == "text"
    reply = build_text_reply_xml(
        to_user=parsed["FromUserName"],
        from_user=parsed["ToUserName"],
        content="ok",
        create_time=1700000001,
    )
    assert "<MsgType><![CDATA[text]]></MsgType>" in reply
    assert "<Content><![CDATA[ok]]></Content>" in reply


def test_format_positions_text_empty_and_non_empty() -> None:
    assert format_positions_text([]) == "当前无持仓"
    text = format_positions_text(
        [
            {"symbol": "600000", "strategy": "manual", "target_position": 0.2},
            {"symbol": "000001", "strategy": "trend", "target_position": 0.1},
        ]
    )
    assert "当前持仓 2 条" in text
    assert "600000" in text


def test_encrypt_and_decrypt_wecom_payload_roundtrip() -> None:
    encoding_aes_key = _aes_key()
    plain = "<xml><MsgType><![CDATA[text]]></MsgType></xml>"
    encrypted = encrypt_wecom_payload(
        plain_text=plain,
        encoding_aes_key=encoding_aes_key,
        receive_id="ww-corp",
    )
    decrypted, receive_id = decrypt_wecom_payload(
        encrypted=encrypted,
        encoding_aes_key=encoding_aes_key,
        expected_receive_id="ww-corp",
        enforce_receive_id=True,
    )
    assert decrypted == plain
    assert receive_id == "ww-corp"


def test_build_encrypted_reply_xml_contains_signature() -> None:
    token = "wx-token"
    encrypted = "cipher-value"
    xml = build_encrypted_reply_xml(
        token=token,
        encrypt=encrypted,
        timestamp="1700000010",
        nonce="nonce-x",
    )
    parsed = parse_wecom_xml(xml)
    assert parsed["Encrypt"] == encrypted
    expected = build_wecom_signature(token, "1700000010", "nonce-x", encrypted)
    assert parsed["MsgSignature"] == expected


def test_parse_wecom_command_news_score_query() -> None:
    parsed = parse_wecom_command("news 600000 trend")
    assert parsed.kind == "query"
    assert parsed.query == "news_score"
    assert parsed.payload["symbol"] == "600000"
    assert parsed.payload["strategy"] == "trend"

def test_parse_wecom_command_news_watchlist_query() -> None:
    parsed = parse_wecom_command("newslist 5 trend")
    assert parsed.kind == "query"
    assert parsed.query == "news_watchlist"
    assert parsed.payload["limit"] == 5
    assert parsed.payload["strategy"] == "trend"


def test_parse_wecom_command_news_cache_state_query() -> None:
    parsed = parse_wecom_command("newscache state")
    assert parsed.kind == "query"
    assert parsed.query == "news_cache_state"


def test_parse_wecom_command_news_cache_clear_query() -> None:
    parsed = parse_wecom_command("newscache clear 600000 trend")
    assert parsed.kind == "query"
    assert parsed.query == "news_cache_clear"
    assert parsed.payload["symbol"] == "600000"
    assert parsed.payload["strategy"] == "trend"


def test_parse_wecom_command_news_history_query() -> None:
    parsed = parse_wecom_command("newshistory 5 600000 trend")
    assert parsed.kind == "query"
    assert parsed.query == "news_history"
    assert parsed.payload["limit"] == 5
    assert parsed.payload["symbol"] == "600000"
    assert parsed.payload["strategy"] == "trend"


def test_wecom_help_text_contains_news_cache_and_history_commands() -> None:
    help_text = build_wecom_help_text()
    assert "newscache state" in help_text
    assert "newscache clear" in help_text
    assert "newshistory" in help_text
    assert "mode advisory on/off" in help_text
    assert "买入 600000" in help_text
    assert "卖出 600000" in help_text


def test_parse_wecom_command_execution_mode_query() -> None:
    parsed = parse_wecom_command("mode")
    assert parsed.kind == "query"
    assert parsed.query == "execution_mode_state"


def test_parse_wecom_command_execution_mode_set() -> None:
    parsed = parse_wecom_command("mode advisory on")
    assert parsed.kind == "query"
    assert parsed.query == "execution_mode_set"
    assert parsed.payload["advisory_only"] is True

