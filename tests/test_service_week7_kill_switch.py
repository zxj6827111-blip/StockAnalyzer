from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.runtime.service import StockAnalyzerService


def _as_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def _as_mapping_list(value: object) -> list[Mapping[str, object]]:
    assert isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    items = [_as_mapping(item) for item in value]
    assert len(items) == len(value)
    return items


def _as_bool(value: object) -> bool:
    assert isinstance(value, bool)
    return value


def _as_int(value: object) -> int:
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


def _as_text(value: object) -> str:
    assert isinstance(value, str)
    return value


def _as_text_list(value: object) -> list[str]:
    assert isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    items = [_as_text(item) for item in value]
    assert len(items) == len(value)
    return items


def _patch_attr(target: object, name: str, value: object) -> None:
    object.__setattr__(target, name, value)


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
    config.week5.auto_notify = False
    config.week6.auto_notify = False
    config.strategy_kill_switch.underperform_months = 3

    if "trend" in config.strategy_scores:
        config.strategy_scores["trend"].thresholds.s = 0.0
        config.strategy_scores["trend"].thresholds.a = 0.0
        config.strategy_scores["trend"].thresholds.b = 0.0
    return config


def test_strategy_kill_switch_triggers_after_consecutive_underperformance() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    months = ["2025-10", "2025-11", "2025-12"]
    for month in months:
        result = _as_mapping(
            service.record_strategy_performance(
                month=month,
                strategy="trend",
                strategy_return=-0.03,
                benchmark_return=0.01,
            )
        )
        assert _as_bool(result["accepted"]) is True

    status = _as_mapping(service.strategy_kill_switch_status(strategy="trend"))
    state = _as_mapping(status["state"])
    assert _as_bool(state["triggered"]) is True
    assert _as_int(state["consecutive_underperform"]) >= 3
    assert service.state.pause_new_buy is True


def test_strategy_kill_switch_emits_trigger_and_reset_notifications() -> None:
    config = _load_test_config()
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

    for month in ["2025-10", "2025-11", "2025-12"]:
        result = _as_mapping(
            service.record_strategy_performance(
                month=month,
                strategy="trend",
                strategy_return=-0.03,
                benchmark_return=0.01,
            )
        )
        assert _as_bool(result["accepted"]) is True

    reset = _as_mapping(service.reset_strategy_kill_switch(strategy="trend", resume_new_buy=True))
    assert _as_bool(reset["accepted"]) is True

    assert any("策略自毁预警" in item["title"] for item in notifications)
    assert any("连续 3 个月跑输基准" in item["content"] for item in notifications)
    assert any("策略自毁已解除" in item["title"] for item in notifications)
    assert any("解除策略：趋势策略" in item["content"] for item in notifications)


def test_week7_kill_switch_surfaces_triggered_strategy_in_pipeline_summary() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    baseline = _as_mapping(
        service.run_pipeline(symbols=["600000"], strategy="trend", current_equity=1.0)
    )
    baseline_summary = _as_mapping(baseline["strategy_kill_switch"])
    assert _as_bool(baseline_summary["pause_new_buy"]) is False
    assert _as_text_list(baseline_summary["triggered_strategies"]) == []

    for month in ["2025-10", "2025-11", "2025-12"]:
        service.record_strategy_performance(
            month=month,
            strategy="trend",
            strategy_return=-0.04,
            benchmark_return=0.02,
        )

    payload = _as_mapping(
        service.run_pipeline(symbols=["600000"], strategy="trend", current_equity=1.0)
    )
    summary = _as_mapping(payload["strategy_kill_switch"])
    assert _as_bool(summary["pause_new_buy"]) is True
    assert "trend" in _as_text_list(summary["triggered_strategies"])


def test_week7_kill_switch_reset_clears_state_and_history() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    for month in ["2025-10", "2025-11", "2025-12"]:
        service.record_strategy_performance(
            month=month,
            strategy="trend",
            strategy_return=-0.02,
            benchmark_return=0.01,
        )
    assert service.state.pause_new_buy is True

    reset = _as_mapping(
        service.reset_strategy_kill_switch(
            strategy="trend",
            resume_new_buy=True,
        )
    )
    assert _as_bool(reset["accepted"]) is True
    assert _as_int(reset["removed"]) >= 1
    assert _as_int(reset["removed_history"]) >= 3
    assert service.state.pause_new_buy is False

    status = _as_mapping(service.strategy_kill_switch_status(strategy="trend"))
    state = _as_mapping(status["state"])
    assert _as_bool(state["triggered"]) is False
