from __future__ import annotations

import time

from stock_analyzer.command.channel import CommandEnvelope, RuntimeState, SignedCommandProcessor
from stock_analyzer.config import CommandChannelConfig
from stock_analyzer.infra.cache import InMemoryCache


def _processor() -> SignedCommandProcessor:
    config = CommandChannelConfig(
        enabled=True,
        secret_key="test-secret",
        dedup_ttl_sec=60,
        max_clock_skew_sec=300,
    )
    return SignedCommandProcessor(config=config, cache=InMemoryCache(), state=RuntimeState())


def test_command_processor_accepts_valid_signed_command() -> None:
    processor = _processor()
    ts = int(time.time())
    payload = {"current_equity": 0.97}
    signature = SignedCommandProcessor.build_signature(
        secret_key="test-secret",
        command_id="cmd-001",
        timestamp=ts,
        action="SET_EQUITY",
        payload=payload,
    )
    result = processor.execute(
        CommandEnvelope(
            command_id="cmd-001",
            timestamp=ts,
            action="SET_EQUITY",
            payload=payload,
            signature=signature,
        )
    )
    assert result.accepted is True
    assert result.state.current_equity == 0.97


def test_command_processor_rejects_duplicate_command_id() -> None:
    processor = _processor()
    ts = int(time.time())
    payload = {"symbol": "600519"}
    signature = SignedCommandProcessor.build_signature(
        secret_key="test-secret",
        command_id="cmd-dup",
        timestamp=ts,
        action="ADD_SYMBOL",
        payload=payload,
    )
    first = processor.execute(
        CommandEnvelope(
            command_id="cmd-dup",
            timestamp=ts,
            action="ADD_SYMBOL",
            payload=payload,
            signature=signature,
        )
    )
    second = processor.execute(
        CommandEnvelope(
            command_id="cmd-dup",
            timestamp=ts,
            action="ADD_SYMBOL",
            payload=payload,
            signature=signature,
        )
    )
    assert first.accepted is True
    assert second.accepted is False
    assert second.code == "duplicate"


def test_command_processor_rejects_bad_signature() -> None:
    processor = _processor()
    ts = int(time.time())
    result = processor.execute(
        CommandEnvelope(
            command_id="cmd-002",
            timestamp=ts,
            action="SET_EQUITY",
            payload={"current_equity": 0.99},
            signature="bad-signature",
        )
    )
    assert result.accepted is False
    assert result.code == "bad_signature"


def test_command_processor_handles_pause_and_resume_new_buy() -> None:
    processor = _processor()
    ts = int(time.time())

    pause_sig = SignedCommandProcessor.build_signature(
        secret_key="test-secret",
        command_id="cmd-pause",
        timestamp=ts,
        action="PAUSE_NEW_BUY",
        payload={},
    )
    pause_result = processor.execute(
        CommandEnvelope(
            command_id="cmd-pause",
            timestamp=ts,
            action="PAUSE_NEW_BUY",
            payload={},
            signature=pause_sig,
        )
    )
    assert pause_result.accepted is True
    assert pause_result.state.pause_new_buy is True

    resume_sig = SignedCommandProcessor.build_signature(
        secret_key="test-secret",
        command_id="cmd-resume",
        timestamp=ts,
        action="RESUME_NEW_BUY",
        payload={},
    )
    resume_result = processor.execute(
        CommandEnvelope(
            command_id="cmd-resume",
            timestamp=ts,
            action="RESUME_NEW_BUY",
            payload={},
            signature=resume_sig,
        )
    )
    assert resume_result.accepted is True
    assert resume_result.state.pause_new_buy is False


def test_command_processor_accepts_broker_positions_command() -> None:
    processor = _processor()
    ts = int(time.time())
    payload = {"positions": [{"symbol": "600000", "target_position": 0.2}]}
    signature = SignedCommandProcessor.build_signature(
        secret_key="test-secret",
        command_id="cmd-broker-pos",
        timestamp=ts,
        action="SET_BROKER_POSITIONS",
        payload=payload,
    )
    result = processor.execute(
        CommandEnvelope(
            command_id="cmd-broker-pos",
            timestamp=ts,
            action="SET_BROKER_POSITIONS",
            payload=payload,
            signature=signature,
        )
    )
    assert result.accepted is True


def test_command_processor_accepts_close_all_positions() -> None:
    processor = _processor()
    ts = int(time.time())
    payload: dict[str, object] = {}
    signature = SignedCommandProcessor.build_signature(
        secret_key="test-secret",
        command_id="cmd-close-all",
        timestamp=ts,
        action="CLOSE_ALL_POSITIONS",
        payload=payload,
    )
    result = processor.execute(
        CommandEnvelope(
            command_id="cmd-close-all",
            timestamp=ts,
            action="CLOSE_ALL_POSITIONS",
            payload=payload,
            signature=signature,
        )
    )
    assert result.accepted is True


def test_command_processor_set_recommendation_status_validation() -> None:
    processor = _processor()
    ts = int(time.time())
    payload = {"symbol": "600000", "status": "watching"}
    signature = SignedCommandProcessor.build_signature(
        secret_key="test-secret",
        command_id="cmd-rec-ok",
        timestamp=ts,
        action="SET_RECOMMENDATION_STATUS",
        payload=payload,
    )
    result = processor.execute(
        CommandEnvelope(
            command_id="cmd-rec-ok",
            timestamp=ts,
            action="SET_RECOMMENDATION_STATUS",
            payload=payload,
            signature=signature,
        )
    )
    assert result.accepted is True

    bad_payload = {"symbol": "600000", "status": "invalid"}
    bad_signature = SignedCommandProcessor.build_signature(
        secret_key="test-secret",
        command_id="cmd-rec-bad",
        timestamp=ts,
        action="SET_RECOMMENDATION_STATUS",
        payload=bad_payload,
    )
    bad = processor.execute(
        CommandEnvelope(
            command_id="cmd-rec-bad",
            timestamp=ts,
            action="SET_RECOMMENDATION_STATUS",
            payload=bad_payload,
            signature=bad_signature,
        )
    )
    assert bad.accepted is False
    assert bad.code == "bad_payload"
