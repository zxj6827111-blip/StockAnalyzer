from __future__ import annotations

from datetime import datetime

from stock_analyzer.config import NotificationFilterConfig
from stock_analyzer.infra.cache import InMemoryCache
from stock_analyzer.notify.filter import NotificationFilter
from stock_analyzer.types import PipelineSignal


def _signal() -> PipelineSignal:
    return PipelineSignal(
        symbol="600000",
        strategy="trend",
        score=75.0,
        grade="A",
        action="buy",
        target_position=0.1,
        probabilities={"lgbm": 0.7, "xgb": 0.7, "meta": 0.7},
        reasons=["soup_entry"],
    )


def test_notification_filter_dedups_by_cooldown() -> None:
    config = NotificationFilterConfig(
        enabled=True,
        cooldown_sec=300,
        min_score=65.0,
        allowed_actions=["buy", "watch"],
    )
    filterer = NotificationFilter(config=config, cache=InMemoryCache())

    first = filterer.filter([_signal()])
    second = filterer.filter([_signal()])
    assert len(first) == 1
    assert len(second) == 0


def test_notification_filter_respects_quiet_window() -> None:
    config = NotificationFilterConfig(
        enabled=True,
        cooldown_sec=300,
        min_score=65.0,
        allowed_actions=["buy"],
        max_signals_per_run=3,
        quiet_windows=["09:00-10:00"],
    )
    filterer = NotificationFilter(config=config, cache=InMemoryCache())
    quiet_now = datetime.fromisoformat("2026-03-01T09:30:00")
    accepted = filterer.filter([_signal()], now=quiet_now)
    assert accepted == []


def test_notification_filter_respects_overnight_quiet_window() -> None:
    config = NotificationFilterConfig(
        enabled=True,
        cooldown_sec=300,
        min_score=65.0,
        allowed_actions=["buy"],
        max_signals_per_run=3,
        quiet_windows=["23:00-08:30"],
    )
    filterer = NotificationFilter(config=config, cache=InMemoryCache())
    quiet_now = datetime.fromisoformat("2026-03-02T00:15:00")
    accepted = filterer.filter([_signal()], now=quiet_now)
    assert accepted == []


def test_notification_filter_limits_output_count() -> None:
    config = NotificationFilterConfig(
        enabled=True,
        cooldown_sec=300,
        min_score=65.0,
        allowed_actions=["buy"],
        max_signals_per_run=1,
    )
    filterer = NotificationFilter(config=config, cache=InMemoryCache())
    signals = [
        _signal(),
        PipelineSignal(
            symbol="000001",
            strategy="trend",
            score=80.0,
            grade="S",
            action="buy",
            target_position=0.1,
            probabilities={"lgbm": 0.8, "xgb": 0.8, "meta": 0.8},
            reasons=["soup_entry"],
        ),
    ]
    accepted = filterer.filter(signals)
    assert len(accepted) == 1
    assert accepted[0]["symbol"] == "000001"


def test_notification_filter_mutes_entry_day_take_profit_stop_loss() -> None:
    config = NotificationFilterConfig(
        enabled=True,
        cooldown_sec=300,
        min_score=10.0,
        allowed_actions=["watch"],
        t_day_entry_silence_enabled=True,
        t_day_silence_reason_keywords=["take_profit", "stop_loss"],
    )
    filterer = NotificationFilter(config=config, cache=InMemoryCache())
    signal = PipelineSignal(
        symbol="600000",
        strategy="trend",
        score=85.0,
        grade="S",
        action="watch",
        target_position=0.0,
        probabilities={"lgbm": 0.9, "xgb": 0.9, "meta": 0.9},
        reasons=["take_profit_intraday"],
    )
    accepted = filterer.filter(
        [signal],
        now=datetime.fromisoformat("2026-03-01T14:30:00"),
        entry_day_symbols={"600000"},
    )
    assert accepted == []
