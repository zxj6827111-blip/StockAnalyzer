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


def _scored_signal(symbol: str, score: float, action: str = "buy") -> PipelineSignal:
    return PipelineSignal(
        symbol=symbol,
        strategy="trend",
        score=score,
        grade="A",
        action=action,
        target_position=0.1 if action == "buy" else 0.0,
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
    diagnostics = filterer.latest_diagnostics()
    assert diagnostics is not None
    assert diagnostics["rejected_by_cooldown"] == 1


def test_notification_filter_disabled_passthrough_does_not_parse_quiet_windows() -> None:
    config = NotificationFilterConfig(
        enabled=False,
        cooldown_sec=300,
        min_score=65.0,
        allowed_actions=["buy"],
        quiet_windows=["bad-window"],
    )
    filterer = NotificationFilter(config=config, cache=InMemoryCache())

    accepted = filterer.filter([_signal()])
    diagnostics = filterer.latest_diagnostics()

    assert len(accepted) == 1
    assert diagnostics is not None
    assert diagnostics["status"] == "disabled_passthrough"
    assert diagnostics["quiet_window_hit"] is False


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
    diagnostics = filterer.latest_diagnostics()
    assert diagnostics is not None
    assert diagnostics["quiet_window_hit"] is True
    assert diagnostics["rejected_by_quiet_window"] == 1


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


def test_notification_filter_accepts_score_equal_to_min_score_and_reports_limit() -> None:
    config = NotificationFilterConfig(
        enabled=True,
        cooldown_sec=300,
        min_score=60.0,
        allowed_actions=["buy"],
        max_signals_per_run=5,
    )
    filterer = NotificationFilter(config=config, cache=InMemoryCache())
    signals = [_scored_signal(f"6000{idx:02d}", 70.0 - idx) for idx in range(10)]
    signals.append(_scored_signal("000060", 60.0))

    accepted = filterer.filter(signals, trace_id="trace-filter-limit")
    diagnostics = filterer.latest_diagnostics()

    assert len(accepted) == 5
    assert accepted[0]["score"] == 70.0
    assert accepted[-1]["score"] == 66.0
    assert diagnostics is not None
    assert diagnostics["trace_id"] == "trace-filter-limit"
    assert diagnostics["input_count"] == 11
    assert diagnostics["accepted_count"] == 5
    assert diagnostics["rejected_by_max_signals_per_run"] == 6
    assert diagnostics["min_score"] == 60.0


def test_notification_filter_uses_action_specific_min_score() -> None:
    config = NotificationFilterConfig(
        enabled=True,
        cooldown_sec=300,
        min_score=65.0,
        min_score_by_action={"watch": 50.0},
        allowed_actions=["buy", "watch"],
        max_signals_per_run=5,
    )
    filterer = NotificationFilter(config=config, cache=InMemoryCache())

    accepted = filterer.filter(
        [
            _scored_signal("600010", 57.0, action="watch"),
            _scored_signal("600011", 64.0, action="buy"),
        ],
        trace_id="trace-action-threshold",
    )
    diagnostics = filterer.latest_diagnostics()

    assert [item["symbol"] for item in accepted] == ["600010"]
    assert diagnostics is not None
    assert diagnostics["rejected_by_score"] == 1
    assert diagnostics["min_score"] == 65.0
    assert diagnostics["min_score_by_action"] == {"watch": 50.0}
    assert diagnostics["effective_min_score_by_action"] == {"buy": 65.0, "watch": 50.0}


def test_notification_filter_diagnostics_counts_action_score_and_t_day_rejections() -> None:
    config = NotificationFilterConfig(
        enabled=True,
        cooldown_sec=300,
        min_score=60.0,
        allowed_actions=["buy"],
        t_day_entry_silence_enabled=True,
        t_day_silence_reason_keywords=["take_profit", "stop_loss"],
    )
    filterer = NotificationFilter(config=config, cache=InMemoryCache())
    rejected_t_day = _scored_signal("600001", 72.0, action="buy")
    rejected_t_day.reasons.append("stop_loss_intraday")

    accepted = filterer.filter(
        [
            _scored_signal("600000", 65.0, action="hold"),
            _scored_signal("600002", 59.99, action="buy"),
            rejected_t_day,
            _scored_signal("600003", 60.0, action="buy"),
        ],
        entry_day_symbols={"600001"},
    )
    diagnostics = filterer.latest_diagnostics()

    assert [item["symbol"] for item in accepted] == ["600003"]
    assert diagnostics is not None
    assert diagnostics["rejected_by_action"] == 1
    assert diagnostics["rejected_by_score"] == 1
    assert diagnostics["rejected_by_t_day_silence"] == 1
    assert diagnostics["accepted_count"] == 1


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
