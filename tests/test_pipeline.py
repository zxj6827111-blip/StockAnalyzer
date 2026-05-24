from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import cast

import pandas as pd

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.data.provider import DataSourceError, SyntheticProvider
from stock_analyzer.data.resilient_provider import ResilientProvider
from stock_analyzer.pipeline import AnalyzerPipeline


class AlwaysFailProvider:
    def fetch_daily_bars(self, symbol: str, lookback_days: int = 120) -> pd.DataFrame:
        raise DataSourceError(f"boom:{symbol}:{lookback_days}")

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        raise DataSourceError(f"boom:{symbol}:{interval}:{lookback_days}")


class MinimalBarsProvider:
    def fetch_daily_bars(self, symbol: str, lookback_days: int = 120) -> pd.DataFrame:
        dates = pd.bdate_range(end=datetime.now().date(), periods=lookback_days)
        record_count = len(dates)
        close = pd.Series(range(record_count), index=dates, dtype=float) * 0.05 + 10.0
        frame = pd.DataFrame(
            {
                "open": close * 0.99,
                "high": close * 1.01,
                "low": close * 0.98,
                "close": close,
                "volume": 2_000_000.0,
                "turnover": close * 2_000_000.0,
                "float_market_cap": 10_000_000_000.0,
                "suspended": False,
            },
            index=dates,
        )
        frame.index.name = "date"
        return frame

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        _ = symbol, interval, lookback_days
        return pd.DataFrame()


class FutureAvailableBarsProvider(MinimalBarsProvider):
    def fetch_daily_bars(self, symbol: str, lookback_days: int = 120) -> pd.DataFrame:
        frame = super().fetch_daily_bars(symbol=symbol, lookback_days=lookback_days)
        frame["event_time"] = [
            ts.to_pydatetime().strftime("%Y-%m-%dT09:30:00+08:00") for ts in frame.index
        ]
        frame["available_time"] = "2099-01-01T09:30:00+08:00"
        return frame


class CountingBarsProvider(MinimalBarsProvider):
    def __init__(self) -> None:
        self.calls = 0

    def fetch_daily_bars(self, symbol: str, lookback_days: int = 120) -> pd.DataFrame:
        self.calls += 1
        return super().fetch_daily_bars(symbol=symbol, lookback_days=lookback_days)


class RecordingBarsProvider(MinimalBarsProvider):
    def __init__(self) -> None:
        self.lookback_requests: list[tuple[str, int]] = []

    def fetch_daily_bars(self, symbol: str, lookback_days: int = 120) -> pd.DataFrame:
        self.lookback_requests.append((symbol, lookback_days))
        return super().fetch_daily_bars(symbol=symbol, lookback_days=lookback_days)


class WeakFundamentalsBarsProvider(MinimalBarsProvider):
    def fetch_daily_bars(self, symbol: str, lookback_days: int = 120) -> pd.DataFrame:
        frame = super().fetch_daily_bars(symbol=symbol, lookback_days=lookback_days)
        frame["roe"] = 0.01
        frame["debt_ratio"] = 0.90
        frame["financial_data_complete"] = True
        return frame


class ConstantNewsProvider:
    def __init__(self, value: float) -> None:
        self._value = value

    def score(
        self,
        *,
        symbol: str,
        bars: pd.DataFrame,
        features: pd.DataFrame,
        strategy: str,
    ) -> float:
        _ = symbol, bars, features, strategy
        return self._value


class FixedProbabilityPredictor:
    def __init__(self, probabilities: dict[str, float]) -> None:
        self._probabilities = probabilities

    def predict_row(self, feature_row: pd.Series) -> dict[str, float]:
        _ = feature_row
        return dict(self._probabilities)


class ErrorNewsProvider:
    def score(
        self,
        *,
        symbol: str,
        bars: pd.DataFrame,
        features: pd.DataFrame,
        strategy: str,
    ) -> float:
        _ = symbol, bars, features, strategy
        raise RuntimeError("boom")


class SymbolMappedNewsProvider:
    def __init__(self, mapping: dict[str, float]) -> None:
        self._mapping = mapping

    def score(
        self,
        *,
        symbol: str,
        bars: pd.DataFrame,
        features: pd.DataFrame,
        strategy: str,
    ) -> float:
        _ = bars, features, strategy
        return self._mapping.get(symbol, 0.5)


class CapturingNewsProvider(ConstantNewsProvider):
    def __init__(self, value: float) -> None:
        super().__init__(value)
        self.last_bars_count = 0
        self.last_features_count = 0

    def score(
        self,
        *,
        symbol: str,
        bars: pd.DataFrame,
        features: pd.DataFrame,
        strategy: str,
    ) -> float:
        self.last_bars_count = len(bars)
        self.last_features_count = len(features)
        return super().score(symbol=symbol, bars=bars, features=features, strategy=strategy)


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


def _load_default_config() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    return load_config(root / "config" / "default.yaml")


def test_pipeline_generates_report_with_signals() -> None:
    config = _load_default_config()
    pipeline = AnalyzerPipeline(config=config, provider=SyntheticProvider(seed_offset=77))
    report = pipeline.run_once(symbols=["600000", "000001"], strategy="trend", current_equity=1.0)
    assert len(report.signals) == 2
    assert report.trace_id
    assert report.timestamp is not None


def test_pipeline_stops_new_buy_under_degraded_mode() -> None:
    config = _load_default_config()
    config.data_source.switch_after_failures = 1
    provider = ResilientProvider(
        primary=AlwaysFailProvider(),
        backup=SyntheticProvider(seed_offset=88),
        config=config.data_source,
    )
    pipeline = AnalyzerPipeline(config=config, provider=provider)
    report = pipeline.run_once(symbols=["600000"], strategy="trend", current_equity=1.0)
    assert report.degraded_mode is True
    assert report.risk.can_open_new_position is False
    assert report.signals[0].action == "hold"


def test_pipeline_keeps_new_buy_open_under_evolution_soft_degraded_mode() -> None:
    config = _load_default_config()
    config.models.cross_review.p_lgbm_min = 0.0
    config.models.cross_review.p_xgb_min = 0.0
    config.models.cross_review.p_meta_min = 0.0
    config.models.cross_review.max_diff = 1.0
    config.financial_filter.enabled = False
    config.liquidity_filter_trend.min_daily_turnover = 0.0
    config.liquidity_filter_trend.min_float_market_cap = 0.0
    config.liquidity_filter_trend.max_turnover_rate = 1.0
    config.strategy_scores["trend"].thresholds.s = 0.0
    config.strategy_scores["trend"].thresholds.a = 0.0
    config.strategy_scores["trend"].thresholds.b = 0.0
    pipeline = AnalyzerPipeline(config=config, provider=SyntheticProvider(seed_offset=188))
    pipeline.set_evolution_controls(
        {
            "degraded_mode": True,
            "degraded_reason": "m10_degraded",
            "source": "evolution",
        }
    )
    report = pipeline.run_once(symbols=["600000"], strategy="trend", current_equity=1.0)
    assert report.degraded_mode is True
    assert report.risk.hard_degraded_mode is False
    assert report.risk.soft_degraded_mode is True
    assert report.risk.can_open_new_position is True
    assert report.risk.reason == "soft_degraded_monitoring"
    assert report.signals[0].action == "buy"
    assert report.signals[0].decision_trace["risk_gate"]["passed"] is True


def test_pipeline_blocks_when_financial_snapshot_missing_under_reject_policy() -> None:
    config = _load_default_config()
    config.models.cross_review.p_lgbm_min = 0.0
    config.models.cross_review.p_xgb_min = 0.0
    config.models.cross_review.p_meta_min = 0.0
    config.models.cross_review.max_diff = 1.0
    config.financial_filter.enabled = True
    config.financial_filter.missing_data_policy = "reject"
    config.financial_filter.apply_to = ["trend"]
    config.financial_filter.trend_mode = "block"
    config.liquidity_filter_trend.min_daily_turnover = 0.0
    config.liquidity_filter_trend.min_float_market_cap = 0.0
    config.liquidity_filter_trend.max_turnover_rate = 1.0

    pipeline = AnalyzerPipeline(config=config, provider=MinimalBarsProvider())
    report = pipeline.run_once(symbols=["600000"], strategy="trend", current_equity=1.0)
    signal = report.signals[0]
    assert signal.action == "hold"
    assert any(reason.startswith("financial_filter:") for reason in signal.reasons)


def test_pipeline_penalizes_trend_instead_of_blocking_under_score_penalty_mode() -> None:
    base_config = _load_default_config()
    penalized_config = _load_default_config()
    for config in (base_config, penalized_config):
        config.models.cross_review.p_lgbm_min = 0.0
        config.models.cross_review.p_xgb_min = 0.0
        config.models.cross_review.p_meta_min = 0.0
        config.models.cross_review.max_diff = 1.0
        config.liquidity_filter_trend.min_daily_turnover = 0.0
        config.liquidity_filter_trend.min_float_market_cap = 0.0
        config.liquidity_filter_trend.max_turnover_rate = 1.0
        config.strategy_scores["trend"].thresholds.s = 0.0
        config.strategy_scores["trend"].thresholds.a = 0.0
        config.strategy_scores["trend"].thresholds.b = 0.0
    base_config.financial_filter.enabled = False
    penalized_config.financial_filter.enabled = True
    penalized_config.financial_filter.apply_to = ["trend"]
    penalized_config.financial_filter.trend_mode = "score_penalty"
    penalized_config.financial_filter.trend_penalty = 6.0

    baseline_signal = AnalyzerPipeline(
        config=base_config,
        provider=WeakFundamentalsBarsProvider(),
    ).run_once(symbols=["600000"], strategy="trend", current_equity=1.0).signals[0]
    penalized_signal = AnalyzerPipeline(
        config=penalized_config,
        provider=WeakFundamentalsBarsProvider(),
    ).run_once(symbols=["600000"], strategy="trend", current_equity=1.0).signals[0]

    assert penalized_signal.action == "buy"
    assert penalized_signal.score < baseline_signal.score
    assert any(reason.startswith("financial_penalty:") for reason in penalized_signal.reasons)
    assert "financial_filter_block" not in penalized_signal.reasons


def test_pipeline_opens_model_disagreement_probe_from_raw_score() -> None:
    config = _load_default_config()
    config.financial_filter.enabled = True
    config.financial_filter.apply_to = ["trend"]
    config.financial_filter.trend_mode = "score_penalty"
    config.financial_filter.trend_penalty = 15.0
    config.liquidity_filter_trend.min_daily_turnover = 0.0
    config.liquidity_filter_trend.min_float_market_cap = 0.0
    config.liquidity_filter_trend.max_turnover_rate = 1.0
    config.strategy_scores["trend"].weights = {
        "lgbm": 0.50,
        "xgb": 0.15,
        "meta": 0.20,
        "news": 0.05,
        "board": 0.05,
        "completion": 0.05,
    }
    pipeline = AnalyzerPipeline(
        config=config,
        provider=WeakFundamentalsBarsProvider(),
        news_provider=ConstantNewsProvider(0.5),
    )
    pipeline._predictor = FixedProbabilityPredictor(  # noqa: SLF001
        {"lgbm": 1.0, "xgb": 0.2694, "meta": 0.4970}
    )
    pipeline._predictor_status = {"predictor_mode": "artifact_loaded"}  # noqa: SLF001

    report = pipeline.run_once(symbols=["600000"], strategy="trend", current_equity=1.0)
    signal = report.signals[0]

    assert signal.action == "buy"
    assert signal.target_position == 0.01
    assert "model_disagreement_probe" in signal.reasons
    assert signal.score < signal.decision_trace["score"]["raw_score"]
    assert signal.decision_trace["cross_review_gate"]["passed"] is False


def test_pipeline_blocks_symbol_with_future_available_time() -> None:
    config = _load_default_config()
    pipeline = AnalyzerPipeline(config=config, provider=FutureAvailableBarsProvider())
    report = pipeline.run_once(symbols=["600000"], strategy="trend", current_equity=1.0)
    signal = report.signals[0]
    assert signal.action == "hold"
    assert "time_invariant_violation" in signal.reasons


def test_pipeline_news_component_is_injected_into_score() -> None:
    config = _load_default_config()
    provider = MinimalBarsProvider()
    high_news_pipeline = AnalyzerPipeline(
        config=config,
        provider=provider,
        news_provider=ConstantNewsProvider(1.0),
    )
    low_news_pipeline = AnalyzerPipeline(
        config=config,
        provider=provider,
        news_provider=ConstantNewsProvider(0.0),
    )
    high_report = high_news_pipeline.run_once(
        symbols=["600000"], strategy="trend", current_equity=1.0
    )
    low_report = low_news_pipeline.run_once(
        symbols=["600000"], strategy="trend", current_equity=1.0
    )
    high_score = high_report.signals[0].score
    low_score = low_report.signals[0].score
    assert high_score > low_score
    assert any(
        reason.startswith("news_component:") for reason in high_report.signals[0].reasons
    )


def test_pipeline_news_provider_failure_falls_back_to_neutral() -> None:
    config = _load_default_config()
    provider = MinimalBarsProvider()
    neutral_pipeline = AnalyzerPipeline(
        config=config,
        provider=provider,
        news_provider=ConstantNewsProvider(0.5),
    )
    error_pipeline = AnalyzerPipeline(
        config=config,
        provider=provider,
        news_provider=ErrorNewsProvider(),
    )
    neutral_score = neutral_pipeline.run_once(
        symbols=["600000"], strategy="trend", current_equity=1.0
    ).signals[0].score
    error_score = error_pipeline.run_once(
        symbols=["600000"], strategy="trend", current_equity=1.0
    ).signals[0].score
    assert error_score == neutral_score


def test_pipeline_uses_configured_fetch_and_analysis_lookbacks() -> None:
    config = _load_default_config()
    provider = RecordingBarsProvider()
    news_provider = CapturingNewsProvider(0.5)
    pipeline = AnalyzerPipeline(
        config=config,
        provider=provider,
        news_provider=news_provider,
    )
    _ = pipeline.run_once(symbols=["600000"], strategy="trend", current_equity=1.0)
    assert provider.lookback_requests == [
        ("600000", config.evolution.universe_spec.signal_fetch_lookback_days)
    ]
    assert (
        news_provider.last_bars_count
        == config.evolution.universe_spec.signal_analysis_lookback_days
    )
    assert (
        news_provider.last_features_count
        == config.evolution.universe_spec.signal_analysis_lookback_days
    )


def test_pipeline_news_preview_returns_component_payload() -> None:
    config = _load_default_config()
    pipeline = AnalyzerPipeline(
        config=config,
        provider=MinimalBarsProvider(),
        news_provider=ConstantNewsProvider(0.75),
    )
    payload = pipeline.preview_news_component(symbol="600000", strategy="trend")
    payload_view = _as_mapping(payload)
    assert payload_view["status"] == "ok"
    assert payload_view["symbol"] == "600000"
    assert payload_view["strategy"] == "trend"
    assert payload_view["news_component"] == 0.75
    reasons = payload_view["reasons"]
    assert isinstance(reasons, list)
    assert any(str(reason).startswith("news_component:") for reason in reasons)


def test_pipeline_news_preview_falls_back_on_data_source_error() -> None:
    config = _load_default_config()
    pipeline = AnalyzerPipeline(
        config=config,
        provider=AlwaysFailProvider(),
        news_provider=ConstantNewsProvider(1.0),
    )
    payload = pipeline.preview_news_component(symbol="600000", strategy="trend")
    payload_view = _as_mapping(payload)
    assert payload_view["status"] == "data_source_error"
    assert payload_view["news_component"] == 0.5


def test_pipeline_news_preview_batch_returns_sorted_items() -> None:
    config = _load_default_config()
    pipeline = AnalyzerPipeline(
        config=config,
        provider=MinimalBarsProvider(),
        news_provider=SymbolMappedNewsProvider({"600000": 0.8, "000001": 0.2}),
    )
    payload = pipeline.preview_news_components(
        symbols=["000001", "600000"],
        strategy="trend",
    )
    payload_view = _as_mapping(payload)
    assert payload_view["status"] == "ok"
    assert payload_view["records"] == 2
    assert payload_view["ok_records"] == 2
    items = _as_mapping_list(payload_view["items"])
    assert items[0]["symbol"] == "600000"
    assert items[1]["symbol"] == "000001"


def test_pipeline_news_preview_batch_returns_empty_payload() -> None:
    config = _load_default_config()
    pipeline = AnalyzerPipeline(config=config, provider=MinimalBarsProvider())
    payload = _as_mapping(pipeline.preview_news_components(symbols=[], strategy="trend"))
    assert payload["status"] == "empty"
    assert payload["records"] == 0
    assert payload["items"] == []


def test_pipeline_news_preview_uses_short_ttl_cache() -> None:
    config = _load_default_config()
    provider = CountingBarsProvider()
    pipeline = AnalyzerPipeline(
        config=config,
        provider=provider,
        news_provider=ConstantNewsProvider(0.63),
    )
    first = pipeline.preview_news_component(symbol="600000", strategy="trend")
    second = pipeline.preview_news_component(symbol="600000", strategy="trend")
    first_view = _as_mapping(first)
    second_view = _as_mapping(second)
    assert first_view["status"] == "ok"
    assert second_view["status"] == "ok"
    assert provider.calls == 1


def test_pipeline_news_preview_cache_state_and_clear() -> None:
    config = _load_default_config()
    pipeline = AnalyzerPipeline(
        config=config,
        provider=MinimalBarsProvider(),
        news_provider=ConstantNewsProvider(0.5),
    )
    _ = pipeline.preview_news_component(symbol="600000", strategy="trend")
    state = _as_mapping(pipeline.news_preview_cache_state())
    assert _as_int(state["entries"]) >= 1
    clear_payload = _as_mapping(
        pipeline.clear_news_preview_cache(symbol="600000", strategy="trend")
    )
    assert _as_int(clear_payload["cleared"]) >= 1
    state_after = _as_mapping(pipeline.news_preview_cache_state())
    assert _as_int(state_after["entries"]) == 0
