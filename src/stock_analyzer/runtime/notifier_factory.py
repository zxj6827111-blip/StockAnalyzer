"""Notification channel factory helpers."""

from __future__ import annotations

import os
import sys

from stock_analyzer.config import StockAnalyzerConfig
from stock_analyzer.notify.channels import (
    BroadcastNotifier,
    ConsoleNotifier,
    CustomWebhookNotifier,
    EmailNotifier,
    FailoverNotifier,
    FeishuAppNotifier,
    FeishuNotifier,
    Notifier,
    PushPlusNotifier,
    TelegramNotifier,
    WeComNotifier,
)


def build_notifier(config: StockAnalyzerConfig) -> FailoverNotifier:
    """Build runtime notifier from primary/backup channel config."""
    if _force_console_notifier():
        console = ConsoleNotifier()
        return FailoverNotifier(primary=console, backup=console)
    primary_name = config.notifications.primary.strip().lower()
    backup_name = config.notifications.backup.strip().lower()
    primary = build_channel(config=config, channel_name=primary_name)
    backup = build_channel(config=config, channel_name=backup_name)
    return FailoverNotifier(primary=primary, backup=backup)


def build_channel(config: StockAnalyzerConfig, channel_name: str) -> Notifier:
    """Build single channel notifier by canonical channel name."""
    if channel_name == "pushplus":
        return PushPlusNotifier(
            token=config.notifications.pushplus_token,
            timeout_sec=config.notifications.timeout_sec,
        )
    if channel_name in {"wecom", "wechat"}:
        return WeComNotifier(
            webhook=config.notifications.wecom_webhook,
            timeout_sec=config.notifications.timeout_sec,
            title_prefix=_wecom_title_prefix(config),
        )
    if channel_name in {"feishu", "lark"}:
        return FeishuNotifier(
            webhook=config.notifications.feishu_webhook,
            timeout_sec=config.notifications.timeout_sec,
        )
    if channel_name in {"feishu_app", "lark_app"}:
        return FeishuAppNotifier(
            app_id=config.notifications.feishu_app_id,
            app_secret=config.notifications.feishu_app_secret,
            receive_id=config.notifications.feishu_app_receive_id,
            receive_id_type=config.notifications.feishu_app_receive_id_type,
            timeout_sec=config.notifications.timeout_sec,
        )
    if channel_name in {"feishu_app_broadcast", "lark_app_broadcast"}:
        targets = [
            (
                target.name or target.app_id or f"feishu_app_{index}",
                FeishuAppNotifier(
                    app_id=target.app_id,
                    app_secret=target.app_secret,
                    receive_id=target.receive_id,
                    receive_id_type=target.receive_id_type,
                    timeout_sec=config.notifications.timeout_sec,
                ),
            )
            for index, target in enumerate(config.notifications.feishu_apps, start=1)
        ]
        return BroadcastNotifier(
            targets=targets,
            channel=channel_name,
            missing_targets_error="missing_feishu_apps",
        )
    if channel_name in {"telegram", "tg"}:
        return TelegramNotifier(
            bot_token=config.notifications.telegram_bot_token,
            chat_id=config.notifications.telegram_chat_id,
            message_thread_id=config.notifications.telegram_message_thread_id,
            timeout_sec=config.notifications.timeout_sec,
        )
    if channel_name in {"email", "smtp"}:
        return EmailNotifier(
            smtp_host=config.notifications.email_smtp_host,
            smtp_port=config.notifications.email_smtp_port,
            sender=config.notifications.email_sender,
            password=config.notifications.email_password,
            receivers=config.notifications.email_receivers,
            use_ssl=config.notifications.email_use_ssl,
            starttls=config.notifications.email_starttls,
            timeout_sec=config.notifications.timeout_sec,
        )
    if channel_name in {"custom", "webhook", "custom_webhook"}:
        return CustomWebhookNotifier(
            webhook_url=config.notifications.custom_webhook_url,
            bearer_token=config.notifications.custom_webhook_bearer_token,
            timeout_sec=config.notifications.timeout_sec,
        )
    return ConsoleNotifier()


def _force_console_notifier() -> bool:
    if os.getenv("PYTEST_CURRENT_TEST") or "pytest" in sys.modules:
        return True
    raw_flags = [
        os.getenv("SA_DISABLE_EXTERNAL_NOTIFICATIONS", ""),
        os.getenv("SA_FORCE_CONSOLE_NOTIFIER", ""),
    ]
    return any(item.strip().lower() in {"1", "true", "yes", "on"} for item in raw_flags)


def _wecom_title_prefix(config: StockAnalyzerConfig) -> str:
    _ = config
    forced = os.getenv("SA_WECOM_TEST_PREFIX", "").strip()
    if forced:
        return forced
    return ""
