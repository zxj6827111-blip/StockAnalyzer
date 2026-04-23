from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.runtime.service import StockAnalyzerService


def _patch_attr(target: object, name: str, value: object) -> None:
    object.__setattr__(target, name, value)


def _as_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def _as_int(value: object) -> int:
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


def _load_test_config() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.notifications.primary = "console"
    config.notifications.backup = "console"
    config.cloud_backup.enabled = True
    config.cloud_backup.alert_after_offline_min = 1
    config.cloud_backup.notify_recovery = False
    config.notification_filter.enabled = False
    config.week5.auto_notify = False
    config.week6.auto_notify = False
    return config


def test_cloud_backup_status_ping_and_offline_alert() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    base = datetime.fromisoformat("2026-03-01T09:00:00")
    ping = service.cloud_backup_ping(source="test", timestamp=base)
    assert ping["accepted"] is True
    assert ping["is_offline"] is False
    assert ping["source"] == "test"

    check_ok = service.run_cloud_backup_check(now=base + timedelta(seconds=30))
    assert check_ok["alerted"] is False
    assert _as_mapping(check_ok["status"])["is_offline"] is False

    check_offline = service.run_cloud_backup_check(now=base + timedelta(minutes=2))
    assert check_offline["alerted"] is True
    assert _as_mapping(check_offline["status"])["is_offline"] is True
    assert _as_int(_as_mapping(check_offline["snapshot"])["open_positions"]) >= 0

    check_again = service.run_cloud_backup_check(now=base + timedelta(minutes=3))
    assert check_again["alerted"] is False
    assert _as_mapping(check_again["status"])["alert_active"] is True

    events = service.audit_events(limit=50, event_type="week7_cloud_backup_offline_alert")
    assert _as_int(events["records"]) >= 1


def test_cloud_backup_ping_clears_active_alert() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    base = datetime.fromisoformat("2026-03-01T10:00:00")
    _ = service.cloud_backup_ping(source="boot", timestamp=base)
    _ = service.run_cloud_backup_check(now=base + timedelta(minutes=2))
    status_before = service.cloud_backup_status(now=base + timedelta(minutes=2))
    assert status_before["alert_active"] is True

    ping_back = service.cloud_backup_ping(
        source="recover",
        timestamp=base + timedelta(minutes=3),
    )
    assert ping_back["accepted"] is True
    assert ping_back["recovered"] is True
    assert ping_back["alert_active"] is False


def test_cloud_backup_notifications_use_structured_template() -> None:
    config = _load_test_config()
    config.cloud_backup.notify_recovery = True
    service = StockAnalyzerService(config=config)
    notifications: list[dict[str, str]] = []

    def _fake_notify(
        title: str,
        content: str,
        level: str = "info",
        trace_id: str = "",
    ) -> dict[str, object]:
        notifications.append(
            {
                "title": title,
                "content": content,
                "level": level,
                "trace_id": trace_id,
            }
        )
        return {"ok": True}

    _patch_attr(service, "notify", _fake_notify)

    base = datetime.fromisoformat("2026-03-01T10:00:00")
    _ = service.cloud_backup_ping(source="boot", timestamp=base)
    _ = service.run_cloud_backup_check(now=base + timedelta(minutes=2))
    _ = service.cloud_backup_ping(source="recover", timestamp=base + timedelta(minutes=3))

    assert any("云备份离线" in item["title"] for item in notifications)
    assert any("无人值守的风险窗口" in item["content"] for item in notifications)
    assert any("云备份已恢复" in item["title"] for item in notifications)
    assert any("心跳来源：recover" in item["content"] for item in notifications)
