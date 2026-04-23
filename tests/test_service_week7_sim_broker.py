from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from stock_analyzer.command.channel import CommandEnvelope, SignedCommandProcessor
from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.runtime.service import StockAnalyzerService


def _as_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def _as_sequence(value: object) -> Sequence[object]:
    assert isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    return value


def _as_int(value: object) -> int:
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


def _as_text(value: object) -> str:
    assert isinstance(value, str)
    return value


def _load_test_config() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.notifications.primary = "console"
    config.notifications.backup = "console"
    config.command_channel.secret_key = "test-secret"
    config.notification_filter.enabled = False
    config.week5.auto_notify = False
    config.week6.auto_notify = False
    config.sim_broker_weekly.auto_notify = False
    config.sim_broker_weekly.export_enabled = False
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


def test_week7_sim_broker_weekly_report_contains_attribution() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    set_cmd = _sign(
        action="SET_POSITION",
        command_id="cmd-week7-sim-broker-set",
        payload={"symbol": "600000", "strategy": "manual", "target_position": 0.2},
        secret=config.command_channel.secret_key,
    )
    set_result = service.execute_command(set_cmd)
    assert set_result["accepted"] is True

    _ = service.update_broker_snapshot(
        positions=[{"symbol": "600000", "target_position": 0.05}],
        source_trace_id="week7-sim-broker-test",
    )
    _ = service.run_reconciliation(
        timestamp=datetime.fromisoformat("2026-03-01T15:30:00"),
        trace_id="week7-sim-broker-reconcile",
    )

    report = service.run_week7_sim_broker_weekly(
        days=7,
        timestamp=datetime.fromisoformat("2026-03-01T20:30:00"),
        export_enabled=False,
        notify_enabled=False,
    )
    assert _as_text(report["status"]) in {"healthy", "watch", "action_required"}
    summary = _as_mapping(report["summary"])
    assert _as_int(summary["mismatch_records"]) >= 1
    assert "attribution" in report
    assert "drilldown" in report
    assert "trend" in report
    drilldown = _as_mapping(report["drilldown"])
    assert len(_as_sequence(drilldown["accounts"])) >= 1
    assert len(_as_sequence(drilldown["strategies"])) >= 1
    trend = _as_mapping(report["trend"])
    assert _as_int(trend["records"]) >= 1
    assert len(_as_sequence(report["recommendations"])) >= 1
    latest = service.latest_week7_sim_broker_report()
    assert latest is not None
    assert latest["timestamp"] == report["timestamp"]


def test_week7_sim_broker_weekly_history_appends_reports() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    first = service.run_week7_sim_broker_weekly(
        days=7,
        timestamp=datetime.fromisoformat("2026-03-01T20:30:00"),
        export_enabled=False,
        notify_enabled=False,
    )
    second = service.run_week7_sim_broker_weekly(
        days=5,
        timestamp=datetime.fromisoformat("2026-03-02T20:30:00"),
        export_enabled=False,
        notify_enabled=False,
    )
    assert _as_int(first["days"]) == 7
    assert _as_int(second["days"]) == 5
    second_trend = _as_mapping(second["trend"])
    assert _as_int(second_trend["records"]) >= 2
    history = service.week7_sim_broker_history(limit=10)
    assert _as_int(history["records"]) >= 2


def test_week7_sim_broker_weekly_disabled_returns_code() -> None:
    config = _load_test_config()
    config.sim_broker_weekly.enabled = False
    service = StockAnalyzerService(config=config)
    report = service.run_week7_sim_broker_weekly(days=7)
    assert report["accepted"] is False
    assert _as_text(report["code"]) == "disabled"
