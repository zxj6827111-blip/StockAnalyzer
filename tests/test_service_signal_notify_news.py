from __future__ import annotations

from pathlib import Path

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.runtime.service import (
    StockAnalyzerService,
    _extract_news_component,
    _news_component_sentiment,
)


def _load_test_config() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.notifications.primary = "console"
    config.notifications.backup = "console"
    config.training.artifact_path = str(root / "artifacts" / "nonexistent_test_model.json")
    config.notification_filter.enabled = False
    return config


def test_extract_news_component_and_sentiment() -> None:
    value = _extract_news_component(["soup_entry", "news_component:0.84"])
    assert value == 0.84
    assert _news_component_sentiment(value) == "positive"
    assert _news_component_sentiment(0.50) == "neutral"
    assert _news_component_sentiment(0.20) == "negative"


def test_notify_actionable_buy_message_contains_news_component_line() -> None:
    service = StockAnalyzerService(config=_load_test_config())
    captured: list[dict[str, object]] = []

    def _fake_notify(
        title: str,
        content: str,
        level: str = "info",
        trace_id: str = "",
    ) -> dict[str, object]:
        captured.append(
            {
                "title": title,
                "content": content,
                "level": level,
                "trace_id": trace_id,
            }
        )
        return {"accepted": True}

    service.notify = _fake_notify  # type: ignore[method-assign]
    service._notify_actionable_signals(
        report={
            "actionable_signals": [
                {
                    "symbol": "600000",
                    "action": "buy",
                    "target_position": 0.2,
                    "strategy": "trend",
                    "reasons": ["news_component:0.83", "soup_entry"],
                    "score": 88.0,
                    "grade": "A",
                }
            ]
        },
        trace_id="trace-news-buy",
        title_prefix="intraday",
    )
    assert len(captured) == 1
    content = str(captured[0]["content"])
    assert "所属策略：趋势策略" in content
    assert "新闻因子：0.830（情绪=偏多）" in content


def test_notify_actionable_buy_uses_strategy_a_threshold() -> None:
    service = StockAnalyzerService(config=_load_test_config())
    captured: list[dict[str, object]] = []

    def _fake_notify(
        title: str,
        content: str,
        level: str = "info",
        trace_id: str = "",
    ) -> dict[str, object]:
        captured.append(
            {
                "title": title,
                "content": content,
                "level": level,
                "trace_id": trace_id,
            }
        )
        return {"accepted": True}

    service.notify = _fake_notify  # type: ignore[method-assign]
    service._notify_actionable_signals(
        report={
            "actionable_signals": [
                {
                    "symbol": "600000",
                    "action": "buy",
                    "target_position": 0.2,
                    "strategy": "trend",
                    "reasons": ["soup_entry"],
                    "score": 70.0,
                    "grade": "A",
                }
            ]
        },
        trace_id="trace-buy-a-threshold",
        title_prefix="intraday",
    )
    assert len(captured) == 1


def test_notify_actionable_buy_uses_lowered_monster_strategy_threshold() -> None:
    service = StockAnalyzerService(config=_load_test_config())
    captured: list[dict[str, object]] = []

    def _fake_notify(
        title: str,
        content: str,
        level: str = "info",
        trace_id: str = "",
    ) -> dict[str, object]:
        captured.append(
            {
                "title": title,
                "content": content,
                "level": level,
                "trace_id": trace_id,
            }
        )
        return {"accepted": True}

    service.notify = _fake_notify  # type: ignore[method-assign]
    service._notify_actionable_signals(
        report={
            "actionable_signals": [
                {
                    "symbol": "600000",
                    "action": "buy",
                    "target_position": 0.2,
                    "strategy": "monster",
                    "reasons": ["soup_entry"],
                    "score": 60.0,
                    "grade": "A",
                }
            ]
        },
        trace_id="trace-buy-monster-threshold",
        title_prefix="intraday",
    )
    assert len(captured) == 1


def test_notify_actionable_sell_message_contains_news_component_line() -> None:
    service = StockAnalyzerService(config=_load_test_config())
    captured: list[dict[str, object]] = []

    def _fake_notify(
        title: str,
        content: str,
        level: str = "info",
        trace_id: str = "",
    ) -> dict[str, object]:
        captured.append(
            {
                "title": title,
                "content": content,
                "level": level,
                "trace_id": trace_id,
            }
        )
        return {"accepted": True}

    service.notify = _fake_notify  # type: ignore[method-assign]
    service._notify_actionable_signals(
        report={
            "actionable_signals": [
                {
                    "symbol": "600000",
                    "action": "sell",
                    "target_position": 0.0,
                    "strategy": "trend",
                    "reasons": ["news_component:0.12", "stop_loss_limit"],
                }
            ]
        },
        trace_id="trace-news-sell",
        title_prefix="intraday",
    )
    assert len(captured) == 1
    content = str(captured[0]["content"])
    assert "所属策略：趋势策略" in content
    assert "新闻因子：0.120（情绪=偏空）" in content
