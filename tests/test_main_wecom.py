from __future__ import annotations

import base64

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

import stock_analyzer.main as main_module
from stock_analyzer.command.wecom_interaction import (
    build_wecom_signature,
    decrypt_wecom_payload,
    encrypt_wecom_payload,
    parse_wecom_xml,
)


def _aes_key() -> str:
    raw = b"0123456789abcdef0123456789abcdef"
    return base64.b64encode(raw).decode("utf-8").rstrip("=")


def _enable_wecom(monkeypatch: MonkeyPatch, *, safe_mode: bool = False) -> None:
    cfg = main_module._config.wecom_interaction
    monkeypatch.setattr(main_module._config.app, "advisory_only", False)
    monkeypatch.setattr(main_module._config.feishu_interaction, "enabled", False)
    monkeypatch.setattr(cfg, "enabled", True)
    monkeypatch.setattr(cfg, "token", "wx-test-token")
    monkeypatch.setattr(cfg, "verify_signature", True)
    monkeypatch.setattr(cfg, "allowed_users", ["user_a"])
    monkeypatch.setattr(cfg, "auto_reconcile_after_broker_snapshot", True)
    if safe_mode:
        monkeypatch.setattr(cfg, "encoding_aes_key", _aes_key())
        monkeypatch.setattr(cfg, "receive_id", "ww-corp")
        monkeypatch.setattr(cfg, "enforce_receive_id", True)
    else:
        monkeypatch.setattr(cfg, "encoding_aes_key", "")
        monkeypatch.setattr(cfg, "receive_id", "")
        monkeypatch.setattr(cfg, "enforce_receive_id", False)


def test_wecom_callback_verify_endpoint_plain(monkeypatch: MonkeyPatch) -> None:
    _enable_wecom(monkeypatch, safe_mode=False)
    client = TestClient(main_module.app)
    timestamp = "1700000000"
    nonce = "nonce-1"
    echostr = "hello-wecom"
    signature = build_wecom_signature("wx-test-token", timestamp, nonce, echostr)
    response = client.get(
        "/wecom/callback",
        params={
            "timestamp": timestamp,
            "nonce": nonce,
            "msg_signature": signature,
            "echostr": echostr,
        },
    )
    assert response.status_code == 200
    assert response.text == echostr


def test_wecom_callback_verify_endpoint_safe_mode(monkeypatch: MonkeyPatch) -> None:
    _enable_wecom(monkeypatch, safe_mode=True)
    client = TestClient(main_module.app)
    timestamp = "1700000000"
    nonce = "nonce-safe-1"
    echostr_plain = "verify-ok"
    echostr = encrypt_wecom_payload(
        plain_text=echostr_plain,
        encoding_aes_key=_aes_key(),
        receive_id="ww-corp",
    )
    signature = build_wecom_signature("wx-test-token", timestamp, nonce, echostr)
    response = client.get(
        "/wecom/callback",
        params={
            "timestamp": timestamp,
            "nonce": nonce,
            "msg_signature": signature,
            "echostr": echostr,
        },
    )
    assert response.status_code == 200
    assert response.text == echostr_plain


def test_wecom_callback_executes_text_command_plain(monkeypatch: MonkeyPatch) -> None:
    _enable_wecom(monkeypatch, safe_mode=False)
    client = TestClient(main_module.app)
    timestamp = "1700000001"
    nonce = "nonce-2"
    inbound_xml = (
        "<xml>"
        "<ToUserName><![CDATA[ww-corp]]></ToUserName>"
        "<FromUserName><![CDATA[user_a]]></FromUserName>"
        "<CreateTime>1700000001</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[pause]]></Content>"
        "<MsgId>10001</MsgId>"
        "</xml>"
    )
    signature = build_wecom_signature("wx-test-token", timestamp, nonce, inbound_xml)
    response = client.post(
        "/wecom/callback",
        params={"timestamp": timestamp, "nonce": nonce, "msg_signature": signature},
        content=inbound_xml,
        headers={"Content-Type": "application/xml"},
    )
    assert response.status_code == 200
    payload = parse_wecom_xml(response.text)
    assert payload["MsgType"] == "text"
    assert "已暂停开仓" in payload["Content"]

    state_response = client.get("/command/state")
    assert state_response.status_code == 200
    state = state_response.json().get("state", {})
    assert bool(state.get("pause_new_buy", False)) is True


def test_wecom_callback_executes_text_command_safe_mode(monkeypatch: MonkeyPatch) -> None:
    _enable_wecom(monkeypatch, safe_mode=True)
    client = TestClient(main_module.app)
    timestamp = "1700000002"
    nonce = "nonce-safe-2"
    inner_xml = (
        "<xml>"
        "<ToUserName><![CDATA[ww-corp]]></ToUserName>"
        "<FromUserName><![CDATA[user_a]]></FromUserName>"
        "<CreateTime>1700000002</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[pause]]></Content>"
        "<MsgId>10002</MsgId>"
        "</xml>"
    )
    encrypted = encrypt_wecom_payload(
        plain_text=inner_xml,
        encoding_aes_key=_aes_key(),
        receive_id="ww-corp",
    )
    outer_xml = (
        "<xml>"
        "<ToUserName><![CDATA[ww-corp]]></ToUserName>"
        f"<Encrypt><![CDATA[{encrypted}]]></Encrypt>"
        "</xml>"
    )
    signature = build_wecom_signature("wx-test-token", timestamp, nonce, encrypted)
    response = client.post(
        "/wecom/callback",
        params={"timestamp": timestamp, "nonce": nonce, "msg_signature": signature},
        content=outer_xml,
        headers={"Content-Type": "application/xml"},
    )
    assert response.status_code == 200

    outer_reply = parse_wecom_xml(response.text)
    encrypted_reply = outer_reply.get("Encrypt", "")
    assert encrypted_reply
    expected_reply_signature = build_wecom_signature(
        "wx-test-token",
        outer_reply["TimeStamp"],
        outer_reply["Nonce"],
        encrypted_reply,
    )
    assert outer_reply["MsgSignature"] == expected_reply_signature

    decrypted_reply, receive_id = decrypt_wecom_payload(
        encrypted=encrypted_reply,
        encoding_aes_key=_aes_key(),
        expected_receive_id="ww-corp",
        enforce_receive_id=True,
    )
    inner_reply = parse_wecom_xml(decrypted_reply)
    assert inner_reply["MsgType"] == "text"
    assert "已暂停开仓" in inner_reply["Content"]
    assert receive_id == "ww-corp"


def test_wecom_callback_rejects_bad_signature(monkeypatch: MonkeyPatch) -> None:
    _enable_wecom(monkeypatch, safe_mode=False)
    client = TestClient(main_module.app)
    inbound_xml = (
        "<xml>"
        "<ToUserName><![CDATA[ww-corp]]></ToUserName>"
        "<FromUserName><![CDATA[user_a]]></FromUserName>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[positions]]></Content>"
        "</xml>"
    )
    response = client.post(
        "/wecom/callback",
        params={
            "timestamp": "1700000003",
            "nonce": "nonce-3",
            "msg_signature": "bad-signature",
        },
        content=inbound_xml,
        headers={"Content-Type": "application/xml"},
    )
    assert response.status_code == 403


def test_wecom_callback_supports_news_score_query(monkeypatch: MonkeyPatch) -> None:
    _enable_wecom(monkeypatch, safe_mode=False)
    client = TestClient(main_module.app)
    timestamp = "1700000004"
    nonce = "nonce-4"
    inbound_xml = (
        "<xml>"
        "<ToUserName><![CDATA[ww-corp]]></ToUserName>"
        "<FromUserName><![CDATA[user_a]]></FromUserName>"
        "<CreateTime>1700000004</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[news 600000 trend]]></Content>"
        "<MsgId>10004</MsgId>"
        "</xml>"
    )
    signature = build_wecom_signature("wx-test-token", timestamp, nonce, inbound_xml)
    response = client.post(
        "/wecom/callback",
        params={"timestamp": timestamp, "nonce": nonce, "msg_signature": signature},
        content=inbound_xml,
        headers={"Content-Type": "application/xml"},
    )
    assert response.status_code == 200
    payload = parse_wecom_xml(response.text)
    assert payload["MsgType"] == "text"
    assert "news_score 600000 trend=" in payload["Content"]

def test_wecom_callback_supports_news_watchlist_query(monkeypatch: MonkeyPatch) -> None:
    _enable_wecom(monkeypatch, safe_mode=False)
    client = TestClient(main_module.app)
    timestamp = "1700000005"
    nonce = "nonce-5"
    inbound_xml = (
        "<xml>"
        "<ToUserName><![CDATA[ww-corp]]></ToUserName>"
        "<FromUserName><![CDATA[user_a]]></FromUserName>"
        "<CreateTime>1700000005</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[newslist 3 trend]]></Content>"
        "<MsgId>10005</MsgId>"
        "</xml>"
    )
    signature = build_wecom_signature("wx-test-token", timestamp, nonce, inbound_xml)
    response = client.post(
        "/wecom/callback",
        params={"timestamp": timestamp, "nonce": nonce, "msg_signature": signature},
        content=inbound_xml,
        headers={"Content-Type": "application/xml"},
    )
    assert response.status_code == 200
    payload = parse_wecom_xml(response.text)
    assert payload["MsgType"] == "text"
    assert "news_watchlist strategy=trend" in payload["Content"]


def test_wecom_callback_supports_news_cache_state_query(monkeypatch: MonkeyPatch) -> None:
    _enable_wecom(monkeypatch, safe_mode=False)
    client = TestClient(main_module.app)
    timestamp = "1700000006"
    nonce = "nonce-6"
    inbound_xml = (
        "<xml>"
        "<ToUserName><![CDATA[ww-corp]]></ToUserName>"
        "<FromUserName><![CDATA[user_a]]></FromUserName>"
        "<CreateTime>1700000006</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[newscache state]]></Content>"
        "<MsgId>10006</MsgId>"
        "</xml>"
    )
    signature = build_wecom_signature("wx-test-token", timestamp, nonce, inbound_xml)
    response = client.post(
        "/wecom/callback",
        params={"timestamp": timestamp, "nonce": nonce, "msg_signature": signature},
        content=inbound_xml,
        headers={"Content-Type": "application/xml"},
    )
    assert response.status_code == 200
    payload = parse_wecom_xml(response.text)
    assert payload["MsgType"] == "text"
    assert "news_cache entries=" in payload["Content"]


def test_wecom_callback_supports_news_cache_clear_query(monkeypatch: MonkeyPatch) -> None:
    _enable_wecom(monkeypatch, safe_mode=False)
    client = TestClient(main_module.app)
    timestamp = "1700000007"
    nonce = "nonce-7"
    inbound_xml = (
        "<xml>"
        "<ToUserName><![CDATA[ww-corp]]></ToUserName>"
        "<FromUserName><![CDATA[user_a]]></FromUserName>"
        "<CreateTime>1700000007</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[newscache clear]]></Content>"
        "<MsgId>10007</MsgId>"
        "</xml>"
    )
    signature = build_wecom_signature("wx-test-token", timestamp, nonce, inbound_xml)
    response = client.post(
        "/wecom/callback",
        params={"timestamp": timestamp, "nonce": nonce, "msg_signature": signature},
        content=inbound_xml,
        headers={"Content-Type": "application/xml"},
    )
    assert response.status_code == 200
    payload = parse_wecom_xml(response.text)
    assert payload["MsgType"] == "text"
    assert "news_cache_clear" in payload["Content"]


def test_wecom_callback_supports_news_history_query(monkeypatch: MonkeyPatch) -> None:
    _enable_wecom(monkeypatch, safe_mode=False)
    client = TestClient(main_module.app)
    timestamp = "1700000008"
    nonce = "nonce-8"
    inbound_xml = (
        "<xml>"
        "<ToUserName><![CDATA[ww-corp]]></ToUserName>"
        "<FromUserName><![CDATA[user_a]]></FromUserName>"
        "<CreateTime>1700000008</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[newshistory 3 600000 trend]]></Content>"
        "<MsgId>10008</MsgId>"
        "</xml>"
    )
    signature = build_wecom_signature("wx-test-token", timestamp, nonce, inbound_xml)
    response = client.post(
        "/wecom/callback",
        params={"timestamp": timestamp, "nonce": nonce, "msg_signature": signature},
        content=inbound_xml,
        headers={"Content-Type": "application/xml"},
    )
    assert response.status_code == 200
    payload = parse_wecom_xml(response.text)
    assert payload["MsgType"] == "text"
    assert "news_history records=" in payload["Content"]


def test_wecom_callback_can_switch_execution_mode(monkeypatch: MonkeyPatch) -> None:
    _enable_wecom(monkeypatch, safe_mode=False)
    client = TestClient(main_module.app)

    ts_on = "1700000009"
    nonce_on = "nonce-9"
    inbound_on = (
        "<xml>"
        "<ToUserName><![CDATA[ww-corp]]></ToUserName>"
        "<FromUserName><![CDATA[user_a]]></FromUserName>"
        "<CreateTime>1700000009</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[mode advisory on]]></Content>"
        "<MsgId>10009</MsgId>"
        "</xml>"
    )
    sig_on = build_wecom_signature("wx-test-token", ts_on, nonce_on, inbound_on)
    resp_on = client.post(
        "/wecom/callback",
        params={"timestamp": ts_on, "nonce": nonce_on, "msg_signature": sig_on},
        content=inbound_on,
        headers={"Content-Type": "application/xml"},
    )
    assert resp_on.status_code == 200
    payload_on = parse_wecom_xml(resp_on.text)
    assert "execution_mode_set advisory_only=true" in payload_on["Content"]

    ts_state = "1700000010"
    nonce_state = "nonce-10"
    inbound_state = (
        "<xml>"
        "<ToUserName><![CDATA[ww-corp]]></ToUserName>"
        "<FromUserName><![CDATA[user_a]]></FromUserName>"
        "<CreateTime>1700000010</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[mode]]></Content>"
        "<MsgId>10010</MsgId>"
        "</xml>"
    )
    sig_state = build_wecom_signature("wx-test-token", ts_state, nonce_state, inbound_state)
    resp_state = client.post(
        "/wecom/callback",
        params={"timestamp": ts_state, "nonce": nonce_state, "msg_signature": sig_state},
        content=inbound_state,
        headers={"Content-Type": "application/xml"},
    )
    assert resp_state.status_code == 200
    payload_state = parse_wecom_xml(resp_state.text)
    assert "execution_mode=advisory_only advisory_only=true" in payload_state["Content"]
