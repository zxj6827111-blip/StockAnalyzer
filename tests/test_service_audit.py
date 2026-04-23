from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from stock_analyzer.command.channel import CommandEnvelope, SignedCommandProcessor
from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.runtime.service import StockAnalyzerService


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    raise AssertionError(f"Expected mapping, got {type(value).__name__}")


def _as_mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        raise AssertionError(f"Expected list, got {type(value).__name__}")
    return [cast(Mapping[str, object], item) for item in value if isinstance(item, Mapping)]


def _as_int(value: object) -> int:
    if isinstance(value, int):
        return value
    raise AssertionError(f"Expected int, got {value!r}")


def _load_test_config() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.notifications.primary = "console"
    config.notifications.backup = "console"
    config.models.cross_review.p_lgbm_min = 0.0
    config.models.cross_review.p_xgb_min = 0.0
    config.models.cross_review.p_meta_min = 0.0
    config.models.cross_review.max_diff = 1.0
    config.liquidity_filter_trend.min_daily_turnover = 0.0
    config.liquidity_filter_trend.min_float_market_cap = 0.0
    config.liquidity_filter_trend.max_turnover_rate = 1.0
    config.training.artifact_path = str(root / "artifacts" / "nonexistent_test_model.json")
    config.notification_filter.min_score = 0.0
    config.notification_filter.quiet_windows = []
    config.command_channel.secret_key = "test-secret"
    return config


def _sign(
    action: str,
    command_id: str,
    payload: dict[str, object],
    secret: str,
) -> CommandEnvelope:
    ts = int(time.time())
    signature = SignedCommandProcessor.build_signature(
        secret_key=secret,
        command_id=command_id,
        timestamp=ts,
        action=action,
        payload=payload,
    )
    return CommandEnvelope(
        command_id=command_id,
        timestamp=ts,
        action=action,
        payload=payload,
        signature=signature,
    )


def test_service_trace_replay_contains_pipeline_and_notification() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    report = service.run_pipeline(symbols=["600000"], strategy="trend", current_equity=1.0)
    trace_id = str(report["trace_id"])
    _ = service.notify(
        title="trace-test",
        content="pipeline-linked notification",
        level="info",
        trace_id=trace_id,
    )

    events_payload = _as_mapping(service.audit_events(limit=100, trace_id=trace_id))
    event_types = {str(item["event_type"]) for item in _as_mapping_list(events_payload["events"])}
    assert "pipeline_run" in event_types
    assert "notification" in event_types

    replay = _as_mapping(service.trace_replay(trace_id=trace_id))
    summary = _as_mapping(replay["summary"])
    event_type_summary = _as_mapping(summary["event_types"])
    assert _as_int(replay["records"]) >= 2
    assert _as_int(event_type_summary["pipeline_run"]) >= 1
    assert _as_int(event_type_summary["notification"]) >= 1


def test_service_notify_respects_quiet_window_and_keeps_audit() -> None:
    config = _load_test_config()
    config.notification_filter.quiet_windows = ["00:00-23:59"]
    service = StockAnalyzerService(config=config)

    payload = _as_mapping(
        service.notify(
            title="quiet-test",
            content="should be suppressed",
            level="warn",
            trace_id="quiet-window-trace",
        )
    )

    assert payload["channel"] == "quiet_window"
    assert payload["suppressed"] is True

    events_payload = _as_mapping(service.audit_events(limit=20, trace_id="quiet-window-trace"))
    event_types = {str(item["event_type"]) for item in _as_mapping_list(events_payload["events"])}
    assert "notification" in event_types


def test_service_audit_records_rejected_command() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    command = _sign(
        action="SET_EQUITY",
        command_id="cmd-audit-reject",
        payload={"current_equity": 0.98},
        secret="wrong-secret",
    )
    result = service.execute_command(command)
    assert result["accepted"] is False

    events = _as_mapping(service.audit_events(limit=20, event_type="command_rejected"))
    assert _as_int(events["records"]) >= 1
    latest = _as_mapping(_as_mapping_list(events["events"])[-1])
    assert latest["trace_id"] == "cmd-audit-reject"
    assert _as_mapping(latest["payload"])["action"] == "SET_EQUITY"
