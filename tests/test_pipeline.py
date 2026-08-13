from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import cast

import pandas as pd

from stock_analyzer.config import LiquidityFilterConfig, StockAnalyzerConfig, load_config
from stock_analyzer.data.provider import DataSourceError, SyntheticProvider
from stock_analyzer.data.resilient_provider import ResilientProvider
from stock_analyzer.pipeline import AnalyzerPipeline, _liquidity_check


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
        frame["financial_missing_fields"] = ""
        frame["financial_source"] = "unit_test_financials"
        frame["financial_report_date"] = "2026-03-31"
        return frame


class LiquidityOverrideBarsProvider(MinimalBarsProvider):
    """MinimalBarsProvider with explicit liquidity metrics for gate testing."""

    def __init__(
        self,
        *,
        turnover: float | None = None,
        float_market_cap: float | None = None,
    ) -> None:
        self._turnover = turnover
        self._float_market_cap = float_market_cap

    def fetch_daily_bars(self, symbol: str, lookback_days: int = 120) -> pd.DataFrame:
        frame = super().fetch_daily_bars(symbol=symbol, lookback_days=lookback_days)
        if self._turnover is not None:
            frame["turnover"] = float(self._turnover)
        if self._float_market_cap is not None:
            frame["float_market_cap"] = float(self._float_market_cap)
        return frame


class ToggleFailBarsProvider(MinimalBarsProvider):
    """Fails on every provider call while ``fail`` is True, succeeds otherwise.

    The flag controls whole runs (daily + intraday), so degraded mode stays
    set for the duration of a failing run and resets on the next successful one.
    """

    def __init__(self) -> None:
        super().__init__()
        self.fail = False

    def fetch_daily_bars(self, symbol: str, lookback_days: int = 120) -> pd.DataFrame:
        if self.fail:
            raise DataSourceError("forced failure")
        return super().fetch_daily_bars(symbol=symbol, lookback_days=lookback_days)

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        if self.fail:
            raise DataSourceError("forced intraday failure")
        return pd.DataFrame()


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


class DegradedModePredictor:
    def predict_row(self, feature_row: pd.Series) -> dict[str, float]:
        _ = feature_row
        return {"lgbm": 0.1355, "xgb": 0.1355, "meta": 0.1355}

    def mode_details(self) -> dict[str, object]:
        return {"degraded_model_mode": True}


class HealthyModePredictor:
    def predict_row(self, feature_row: pd.Series) -> dict[str, float]:
        _ = feature_row
        return {"lgbm": 0.8, "xgb": 0.8, "meta": 0.8}

    def mode_details(self) -> dict[str, object]:
        return {"degraded_model_mode": False}


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
    financial_gate = _as_mapping(penalized_signal.decision_trace["financial_gate"])
    assert financial_gate["roe"] == 0.01
    assert financial_gate["debt_ratio"] == 0.90
    assert financial_gate["financial_data_complete"] is True
    assert financial_gate["financial_missing_fields"] == ""
    assert financial_gate["financial_source"] == "unit_test_financials"
    assert financial_gate["financial_report_date"] == "2026-03-31"


def test_pipeline_opens_model_disagreement_probe_from_raw_score() -> None:
    config = _load_default_config()
    # C1: transform now emits the full fixed intraday column set; without
    # intraday data the extra zero columns push the feature-quality score
    # below the gate threshold, so disable the gate to keep exercising the
    # model-disagreement probe path this test targets.
    config.models.feature_quality.enabled = False
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


def test_pipeline_news_provider_failure_excludes_news_component() -> None:
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
    neutral_signal = neutral_pipeline.run_once(
        symbols=["600000"], strategy="trend", current_equity=1.0
    ).signals[0]
    error_signal = error_pipeline.run_once(
        symbols=["600000"], strategy="trend", current_equity=1.0
    ).signals[0]
    neutral_components = _as_mapping(neutral_signal.decision_trace["score"]["components"])
    error_components = _as_mapping(error_signal.decision_trace["score"]["components"])
    assert "news" in neutral_components
    assert "news" not in error_components
    assert "news_component_unavailable" in error_signal.reasons
    assert error_signal.score != neutral_signal.score


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


def test_pipeline_records_per_stage_wall_clock_breakdown() -> None:
    config = _load_default_config()
    pipeline = AnalyzerPipeline(config=config, provider=MinimalBarsProvider())
    _ = pipeline.run_once(symbols=["600000", "000001"], strategy="trend", current_equity=1.0)
    stages = pipeline._last_pipeline_stage_ms  # noqa: SLF001
    # 阶段细分计时（检查项 6）：旧三键仍在，新增 5 子阶段 + completed_count。
    assert set(stages.keys()) == {
        "fetch_bars_ms",
        "feature_engine_ms",
        "inference_ms",
        "intraday_ms",
        "market_context_ms",
        "cross_review_ms",
        "score_risk_ms",
        "learning_persist_ms",
        "completed_count",
    }
    for value in stages.values():
        assert value >= 0
    # Two symbols were processed: per-symbol work must be aggregated, not reset.
    assert stages["fetch_bars_ms"] > 0
    assert stages["completed_count"] == 2


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


def test_pipeline_fallback_model_falls_back_to_heuristic() -> None:
    config = _load_default_config()
    # C1: with no intraday data the fixed zero columns lower the
    # feature-quality score below the gate threshold (0.48 < 0.5); disable
    # the gate so the healthy branch still exercises predictor passthrough
    # instead of silently degrading to heuristic probabilities.
    config.models.feature_quality.enabled = False
    provider = MinimalBarsProvider()

    degraded_pipeline = AnalyzerPipeline(config=config, provider=provider)
    degraded_pipeline._predictor = DegradedModePredictor()  # noqa: SLF001
    degraded_pipeline._predictor_status = {"predictor_mode": "artifact_loaded"}  # noqa: SLF001
    degraded_report = degraded_pipeline.run_once(
        symbols=["600000"], strategy="trend", current_equity=1.0
    )
    degraded_signal = degraded_report.signals[0]
    degraded_health = _as_mapping(degraded_signal.decision_trace["probability_health"])
    assert degraded_health.get("predictor_degraded_model_mode") is True
    degraded_probs = _as_mapping(degraded_signal.decision_trace["score"]["probabilities"])
    assert not all(abs(float(value) - 0.1355) < 1e-9 for value in degraded_probs.values())

    healthy_pipeline = AnalyzerPipeline(config=config, provider=provider)
    healthy_pipeline._predictor = HealthyModePredictor()  # noqa: SLF001
    healthy_pipeline._predictor_status = {"predictor_mode": "artifact_loaded"}  # noqa: SLF001
    healthy_report = healthy_pipeline.run_once(
        symbols=["600000"], strategy="trend", current_equity=1.0
    )
    healthy_signal = healthy_report.signals[0]
    healthy_health = _as_mapping(healthy_signal.decision_trace["probability_health"])
    assert healthy_health.get("predictor_degraded_model_mode") is False
    healthy_probs = _as_mapping(healthy_signal.decision_trace["score"]["probabilities"])
    assert abs(float(healthy_probs["lgbm"]) - 0.8) < 1e-9
    assert abs(float(healthy_probs["xgb"]) - 0.8) < 1e-9
    assert abs(float(healthy_probs["meta"]) - 0.8) < 1e-9


def test_pipeline_filters_symbol_below_min_daily_turnover() -> None:
    # MinimalBarsProvider yields turnover ~3e7, below the trend threshold 8e7;
    # keep the default (non-zero) liquidity thresholds so the gate must reject.
    config = _load_default_config()
    pipeline = AnalyzerPipeline(config=config, provider=MinimalBarsProvider())
    report = pipeline.run_once(symbols=["600000"], strategy="trend", current_equity=1.0)
    signal = report.signals[0]
    assert signal.action == "hold"
    assert signal.target_position == 0.0
    assert any(reason == "liquidity_filter" for reason in signal.reasons)
    liquidity_gate = _as_mapping(signal.decision_trace["liquidity_gate"])
    assert liquidity_gate["passed"] is False
    assert liquidity_gate["min_daily_turnover"] == 80_000_000
    assert float(liquidity_gate["turnover"]) < 80_000_000


def test_pipeline_filters_symbol_below_min_float_market_cap() -> None:
    config = _load_default_config()
    provider = LiquidityOverrideBarsProvider(
        turnover=500_000_000.0,
        float_market_cap=5_000_000_000.0,
    )
    pipeline = AnalyzerPipeline(config=config, provider=provider)
    report = pipeline.run_once(symbols=["600000"], strategy="trend", current_equity=1.0)
    signal = report.signals[0]
    assert signal.action == "hold"
    assert any(reason == "liquidity_filter" for reason in signal.reasons)
    liquidity_gate = _as_mapping(signal.decision_trace["liquidity_gate"])
    assert liquidity_gate["passed"] is False
    assert liquidity_gate["min_float_market_cap"] == 8_000_000_000
    # turnover/rate satisfy the other two thresholds: only cap is violated.
    assert float(liquidity_gate["turnover"]) >= liquidity_gate["min_daily_turnover"]
    assert float(liquidity_gate["turnover_rate"]) <= liquidity_gate["max_turnover_rate"]


def test_pipeline_filters_symbol_above_max_turnover_rate() -> None:
    config = _load_default_config()
    provider = LiquidityOverrideBarsProvider(
        turnover=2_000_000_000.0,
        float_market_cap=10_000_000_000.0,
    )
    pipeline = AnalyzerPipeline(config=config, provider=provider)
    report = pipeline.run_once(symbols=["600000"], strategy="trend", current_equity=1.0)
    signal = report.signals[0]
    assert signal.action == "hold"
    assert any(reason == "liquidity_filter" for reason in signal.reasons)
    liquidity_gate = _as_mapping(signal.decision_trace["liquidity_gate"])
    assert liquidity_gate["passed"] is False
    assert liquidity_gate["max_turnover_rate"] == 0.15
    # turnover/cap satisfy the other two thresholds: only rate is violated.
    assert float(liquidity_gate["turnover"]) >= liquidity_gate["min_daily_turnover"]
    assert float(liquidity_gate["float_market_cap"]) >= liquidity_gate["min_float_market_cap"]
    assert float(liquidity_gate["turnover_rate"]) > liquidity_gate["max_turnover_rate"]


def test_liquidity_check_passes_when_turnover_equals_min_threshold() -> None:
    config = LiquidityFilterConfig(
        min_daily_turnover=80_000_000,
        min_float_market_cap=8_000_000_000,
        max_turnover_rate=0.15,
    )
    # turnover == min_daily_turnover exactly; rate = 8e7/8e9 = 0.01 <= 0.15.
    bar = pd.Series({"turnover": 80_000_000.0, "float_market_cap": 8_000_000_000.0})
    assert _liquidity_check(bar, config) is True


def test_pipeline_resumes_new_buy_after_provider_recovers() -> None:
    # First run: every provider call fails -> degraded mode stops new buys.
    # Second run: provider recovers -> degraded mode resets, new buys reopen.
    config = _load_default_config()
    config.data_source.switch_after_failures = 1
    primary = ToggleFailBarsProvider()
    primary.fail = True
    provider = ResilientProvider(
        primary=primary,
        backup=SyntheticProvider(seed_offset=88),
        config=config.data_source,
    )
    pipeline = AnalyzerPipeline(config=config, provider=provider)

    first = pipeline.run_once(symbols=["600000"], strategy="trend", current_equity=1.0)
    assert first.degraded_mode is True
    assert first.risk.hard_degraded_mode is True
    assert first.risk.can_open_new_position is False
    assert first.risk.reason == "degraded_stop_new_buy"
    assert first.signals[0].action == "hold"

    primary.fail = False
    second = pipeline.run_once(symbols=["600000"], strategy="trend", current_equity=1.0)
    assert second.degraded_mode is False
    assert second.risk.hard_degraded_mode is False
    assert second.risk.can_open_new_position is True
    assert second.risk.reason != "degraded_stop_new_buy"
    status = provider.status()
    assert status["degraded_mode"] is False
    assert status["consecutive_failures"] == 0
    assert status["last_error"] == ""
