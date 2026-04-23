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


def _as_text(value: object) -> str:
    assert isinstance(value, str)
    return value


def _as_text_list(value: object) -> list[str]:
    assert isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    items = [_as_text(item) for item in value]
    assert len(items) == len(value)
    return items


def _as_float(value: object) -> float:
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    return float(value)


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
    config.week6.auto_notify = False
    config.week5.auto_notify = False

    config.score.thresholds.s = 0.0
    config.score.thresholds.a = 0.0
    config.score.thresholds.b = 0.0
    if "trend" in config.strategy_scores:
        config.strategy_scores["trend"].thresholds.s = 0.0
        config.strategy_scores["trend"].thresholds.a = 0.0
        config.strategy_scores["trend"].thresholds.b = 0.0
    return config


def test_multi_strategy_pipeline_returns_weighted_signals() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)

    payload = service.run_pipeline(
        symbols=["600000", "000001"],
        strategy="multi",
        current_equity=1.0,
    )
    signals = _as_mapping_list(payload["signals"])
    assert len(signals) == 2
    assert all(_as_text(item["strategy"]) == "multi" for item in signals)

    week6 = _as_mapping(payload["week6_execution"])
    assert _as_text(week6["strategy"]) == "multi"
    weights = _as_mapping(week6["allocation_weights"])
    assert "trend" in weights
    assert "summary" in week6
    assert "multi_run" in week6


def test_multi_strategy_pipeline_respects_regulatory_exclude() -> None:
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    _ = service.set_regulatory_watchlist(
        entries=[{"symbol": "600000", "tag": "inquiry", "note": "exclude in multi"}]
    )

    payload = service.run_pipeline(symbols=["600000"], strategy="multi", current_equity=1.0)
    signal = _as_mapping_list(payload["signals"])[0]
    assert _as_text(signal["action"]) == "hold"
    assert _as_float(signal["target_position"]) == 0.0
    assert "regulatory_exclude" in _as_text_list(signal["reasons"])
