"""Core analyzer pipeline that glues data, feature, signal, and risk modules."""

import hashlib
import json
import math
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Protocol
from uuid import uuid4

import numpy as np
import pandas as pd

from stock_analyzer.config import LiquidityFilterConfig, StockAnalyzerConfig
from stock_analyzer.data.provider import DataSourceError, MarketDataProvider
from stock_analyzer.data.provider_factory import build_runtime_provider
from stock_analyzer.feature.engineer import FeatureEngineer
from stock_analyzer.feature.market_context import build_market_relative_frame
from stock_analyzer.filter.financial import FinancialRiskFilter
from stock_analyzer.learning.feedback_features import ensure_feedback_feature_frame
from stock_analyzer.learning.feature_schema_registry import FeatureSchemaRegistry
from stock_analyzer.learning.label_policy_registry import LabelPolicyRegistry
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    MaturityStatus,
    OutcomeRecord,
    SignalSnapshot,
)
from stock_analyzer.learning.sample_store import SampleStore
from stock_analyzer.models.predictor import SignalPredictor
from stock_analyzer.models.registry import ModelRegistry
from stock_analyzer.monitor.health import DataHealthMonitor
from stock_analyzer.risk.controls import RiskController
from stock_analyzer.signal.cross_review import evaluate_cross_review
from stock_analyzer.signal.scoring import ScoreEngine
from stock_analyzer.strategy.soup import SoupStrategy
from stock_analyzer.time_semantics import apply_time_invariants_to_frame
from stock_analyzer.types import PipelineReport, PipelineSignal, ScoredSignal
from stock_analyzer.week6.engines import MainForceTracker


class NewsSignalProvider(Protocol):
    def score(
        self,
        *,
        symbol: str,
        bars: pd.DataFrame,
        features: pd.DataFrame,
        strategy: str,
    ) -> float:
        """Return normalized news component score in [0, 1]."""


class NeutralNewsSignalProvider:
    def score(
        self,
        *,
        symbol: str,
        bars: pd.DataFrame,
        features: pd.DataFrame,
        strategy: str,
    ) -> float:
        _ = symbol, bars, features, strategy
        return 0.50


class AnalyzerPipeline:
    """Run one pass of signal generation for symbols."""

    def __init__(
        self,
        config: StockAnalyzerConfig,
        provider: MarketDataProvider | None = None,
        news_provider: NewsSignalProvider | None = None,
        sample_store: SampleStore | None = None,
        feature_schema_registry: FeatureSchemaRegistry | None = None,
        label_policy_registry: LabelPolicyRegistry | None = None,
    ) -> None:
        self._config = config
        self._provider = (
            provider
            if provider is not None
            else build_runtime_provider(config.data_source, synthetic_seed=2026)
        )
        self._feature_engineer = FeatureEngineer()
        self._health_monitor = DataHealthMonitor(config.health_check)
        self._score_engine = ScoreEngine(config)
        self._risk_controller = RiskController(config)
        self._strategy = SoupStrategy(config.soup_strategy)
        self._main_force_tracker = MainForceTracker(config.week6.main_force)
        self._financial_filter = FinancialRiskFilter(config=config.financial_filter)
        self._predictor, self._predictor_status = _load_predictor(config.training.artifact_path)
        self._news_provider = (
            news_provider if news_provider is not None else NeutralNewsSignalProvider()
        )
        self._sample_store = sample_store
        self._feature_schema_registry = feature_schema_registry
        self._label_policy_registry = label_policy_registry
        self._model_registry: ModelRegistry | None = None
        self._runtime_config_hash = _stable_config_hash(config)
        self._latest_report: PipelineReport | None = None
        self._evolution_controls: dict[str, object] = {}
        self._news_preview_cache: dict[tuple[str, str], tuple[float, dict[str, object]]] = {}
        self._news_preview_cache_ttl_sec = 60.0
        self._settlement_lag_days = max(
            0,
            int(getattr(config.evolution.execution_spec, "settlement_lag", 1)),
        )
        self._min_history_days = max(
            1,
            int(getattr(config.evolution.universe_spec, "min_list_days", 60)),
        )
        signal_analysis_lookback_days = max(
            1,
            int(getattr(config.evolution.universe_spec, "signal_analysis_lookback_days", 250)),
        )
        signal_fetch_lookback_days = max(
            1,
            int(getattr(config.evolution.universe_spec, "signal_fetch_lookback_days", 500)),
        )
        self._signal_analysis_lookback_days = max(
            120,
            self._min_history_days + 60,
            signal_analysis_lookback_days,
        )
        self._signal_fetch_lookback_days = max(
            self._signal_analysis_lookback_days,
            signal_fetch_lookback_days,
        )

    def run_once(
        self,
        symbols: list[str],
        strategy: str = "trend",
        current_equity: float = 1.0,
    ) -> PipelineReport:
        trace_id = uuid4().hex[:16]
        signals: list[PipelineSignal] = []

        for symbol in symbols:
            signal = self._process_symbol(
                symbol=symbol, strategy=strategy, current_equity=current_equity
            )
            signals.append(signal)

        provider_status = self.provider_status()
        self._risk_controller.update_degraded_mode(
            hard_degraded_mode=bool(provider_status.get("hard_degraded_mode", False)),
            soft_degraded_mode=bool(provider_status.get("soft_degraded_mode", False)),
        )
        risk_status = self._risk_controller.evaluate(current_equity=current_equity)

        report = PipelineReport(
            trace_id=trace_id,
            timestamp=datetime.now(),
            degraded_mode=risk_status.degraded_mode,
            risk=risk_status,
            signals=signals,
        )
        self._latest_report = report
        return report

    def provider_status(self) -> dict[str, object]:
        health_status = self._health_monitor.snapshot()
        predictor_status = dict(self._predictor_status)
        predictor_degrade_reason = str(predictor_status.get("degraded_reason", "")).strip()
        evolution_controls = dict(self._evolution_controls)
        soft_degraded_mode = _soft_degraded(evolution_controls)
        soft_degraded_reason = _soft_degraded_reason(evolution_controls)
        status_method = getattr(self._provider, "status", None)
        if callable(status_method):
            payload = status_method()
            if isinstance(payload, dict):
                hard_degraded_mode = _hard_degraded(payload, health_status)
                hard_degraded_reason = _hard_degraded_reason(payload, health_status)
                degrade_reason = (
                    hard_degraded_reason or soft_degraded_reason or predictor_degrade_reason
                )
                return {
                    **payload,
                    "health": health_status,
                    "evolution": evolution_controls,
                    "hard_degraded_mode": hard_degraded_mode,
                    "soft_degraded_mode": soft_degraded_mode,
                    "degraded_mode": hard_degraded_mode or soft_degraded_mode,
                    "hard_degraded_reason": hard_degraded_reason,
                    "soft_degraded_reason": soft_degraded_reason,
                    "degrade_reason": degrade_reason,
                    "model_loaded": self._predictor is not None,
                    **predictor_status,
                }
        hard_degraded_mode = bool(health_status.get("degraded_mode", False))
        hard_degraded_reason = _hard_degraded_reason({}, health_status)
        return {
            "hard_degraded_mode": hard_degraded_mode,
            "soft_degraded_mode": soft_degraded_mode,
            "degraded_mode": hard_degraded_mode or soft_degraded_mode,
            "health": health_status,
            "evolution": evolution_controls,
            "hard_degraded_reason": hard_degraded_reason,
            "soft_degraded_reason": soft_degraded_reason,
            "degrade_reason": (
                hard_degraded_reason or soft_degraded_reason or predictor_degrade_reason
            ),
            "model_loaded": self._predictor is not None,
            **predictor_status,
        }

    def _insufficient_history_reasons(self, history_days: int) -> list[str]:
        return [f"insufficient_history_days:{history_days}<{self._min_history_days}"]

    def latest_report(self) -> PipelineReport | None:
        return self._latest_report

    def configure_learning_protocol(
        self,
        *,
        sample_store: SampleStore | None,
        feature_schema_registry: FeatureSchemaRegistry | None,
        label_policy_registry: LabelPolicyRegistry | None,
        model_registry: ModelRegistry | None = None,
    ) -> None:
        """Attach learning-protocol dependencies after pipeline construction."""

        self._sample_store = sample_store
        self._feature_schema_registry = feature_schema_registry
        self._label_policy_registry = label_policy_registry
        self._model_registry = model_registry

    def set_evolution_controls(self, controls: Mapping[str, object] | None) -> None:
        if controls is None:
            self._evolution_controls = {}
            return
        self._evolution_controls = {
            str(key): value for key, value in controls.items() if isinstance(key, str)
        }

    def preview_news_component(
        self,
        *,
        symbol: str,
        strategy: str = "trend",
    ) -> dict[str, object]:
        normalized_symbol = symbol.strip()
        normalized_strategy = strategy.strip().lower() or "trend"
        fallback_payload: dict[str, object] = {
            "symbol": normalized_symbol,
            "strategy": normalized_strategy,
            "news_component": 0.50,
        }
        if not normalized_symbol:
            return {
                **fallback_payload,
                "status": "invalid_symbol",
                "reasons": ["invalid_symbol"],
            }
        cache_key = (normalized_symbol, normalized_strategy)
        now_perf = perf_counter()
        cached = self._news_preview_cache.get(cache_key)
        if cached is not None:
            cached_at, cached_payload = cached
            if now_perf - cached_at <= self._news_preview_cache_ttl_sec:
                return deepcopy(cached_payload)
        started = perf_counter()
        try:
            bars = self._provider.fetch_daily_bars(
                symbol=normalized_symbol,
                lookback_days=self._signal_fetch_lookback_days,
            )
            self._health_monitor.record(success=True, latency_sec=perf_counter() - started)
        except DataSourceError as exc:
            self._health_monitor.record(success=False, latency_sec=perf_counter() - started)
            return {
                **fallback_payload,
                "status": "data_source_error",
                "reasons": [f"data_source:{exc}"],
            }
        bars, bars_time_gate = apply_time_invariants_to_frame(
            bars,
            decision_time=datetime.now(),
            timezone=str(self._config.app.timezone).strip() or "Asia/Shanghai",
            holding_horizon_days=self._config.labels.horizon_days,
            settlement_lag_days=self._settlement_lag_days,
            require_mature_label=False,
        )
        if bars.empty:
            payload = {
                **fallback_payload,
                "status": "time_invariant_violation",
                "reasons": ["time_invariant_violation"],
            }
            self._news_preview_cache[cache_key] = (now_perf, deepcopy(payload))
            return payload
        if len(bars) < self._min_history_days:
            payload = {
                **fallback_payload,
                "status": "insufficient_history",
                "reasons": self._insufficient_history_reasons(len(bars)),
            }
            self._news_preview_cache[cache_key] = (now_perf, deepcopy(payload))
            return payload
        analysis_bars = self._clip_signal_analysis_bars(bars)
        intraday_1m, intraday_5m = self._fetch_intraday_summaries(
            symbol=normalized_symbol,
            lookback_days=max(120, len(analysis_bars) + 5),
        )
        market_index = self._maybe_build_market_index(analysis_bars)
        features = self._feature_engineer.transform(
            analysis_bars,
            intraday_1m=intraday_1m,
            intraday_5m=intraday_5m,
            market_index=market_index,
        )
        if features.empty:
            payload = {
                **fallback_payload,
                "status": "feature_empty",
                "reasons": ["feature_empty"],
            }
            self._news_preview_cache[cache_key] = (now_perf, deepcopy(payload))
            return payload
        features, time_gate = apply_time_invariants_to_frame(
            features,
            decision_time=datetime.now(),
            timezone=str(self._config.app.timezone).strip() or "Asia/Shanghai",
            holding_horizon_days=self._config.labels.horizon_days,
            settlement_lag_days=self._settlement_lag_days,
            require_mature_label=False,
        )
        if features.empty:
            payload = {
                **fallback_payload,
                "status": "time_invariant_violation",
                "reasons": ["time_invariant_violation"],
            }
            self._news_preview_cache[cache_key] = (now_perf, deepcopy(payload))
            return payload
        news_component = self._score_news_component(
            symbol=normalized_symbol,
            bars=analysis_bars,
            features=features,
            strategy=normalized_strategy,
        )
        reasons: list[str] = []
        dropped_rows = _as_int(time_gate.get("dropped_rows"), default=0)
        if dropped_rows > 0:
            reasons.append(f"time_gate_dropped_rows:{dropped_rows}")
        bars_dropped_rows = _as_int(bars_time_gate.get("dropped_rows"), default=0)
        if bars_dropped_rows > 0:
            reasons.append(f"bars_time_gate_dropped_rows:{bars_dropped_rows}")
        reasons.append(f"news_component:{news_component:.3f}")
        payload = {
            "symbol": normalized_symbol,
            "strategy": normalized_strategy,
            "news_component": round(news_component, 4),
            "status": "ok",
            "reasons": reasons,
        }
        self._news_preview_cache[cache_key] = (now_perf, deepcopy(payload))
        return payload

    def preview_news_components(
        self,
        symbols: list[str],
        strategy: str = "trend",
    ) -> dict[str, object]:
        normalized_strategy = strategy.strip().lower() or "trend"
        if not symbols:
            return {
                "strategy": normalized_strategy,
                "records": 0,
                "ok_records": 0,
                "average_news_component": 0.50,
                "items": [],
                "status": "empty",
            }
        items = [
            self.preview_news_component(symbol=symbol, strategy=normalized_strategy)
            for symbol in symbols
        ]
        sorted_items = sorted(
            items,
            key=lambda item: _as_float(item.get("news_component"), default=0.50),
            reverse=True,
        )
        ok_items = [item for item in sorted_items if item.get("status") == "ok"]
        if ok_items:
            total = 0.0
            for item in ok_items:
                total += _as_float(item.get("news_component"), default=0.50)
            average_news_component = round(total / len(ok_items), 4)
        else:
            average_news_component = 0.50
        return {
            "strategy": normalized_strategy,
            "records": len(sorted_items),
            "ok_records": len(ok_items),
            "average_news_component": average_news_component,
            "items": sorted_items,
            "status": "ok",
        }

    def news_preview_cache_state(self) -> dict[str, object]:
        return {
            "entries": len(self._news_preview_cache),
            "ttl_sec": self._news_preview_cache_ttl_sec,
        }

    def clear_news_preview_cache(
        self,
        symbol: str = "",
        strategy: str = "",
    ) -> dict[str, object]:
        normalized_symbol = symbol.strip()
        normalized_strategy = strategy.strip().lower()
        before = len(self._news_preview_cache)
        if not normalized_symbol and not normalized_strategy:
            self._news_preview_cache.clear()
            return {
                "cleared": before,
                "remaining": 0,
                "symbol": "",
                "strategy": "",
            }
        remaining: dict[tuple[str, str], tuple[float, dict[str, object]]] = {}
        cleared = 0
        for key, value in self._news_preview_cache.items():
            key_symbol, key_strategy = key
            symbol_hit = not normalized_symbol or key_symbol == normalized_symbol
            strategy_hit = not normalized_strategy or key_strategy == normalized_strategy
            if symbol_hit and strategy_hit:
                cleared += 1
                continue
            remaining[key] = value
        self._news_preview_cache = remaining
        return {
            "cleared": cleared,
            "remaining": len(self._news_preview_cache),
            "symbol": normalized_symbol,
            "strategy": normalized_strategy,
        }

    def reload_predictor(self, artifact_path: str | None = None) -> bool:
        path = artifact_path or self._config.training.artifact_path
        self._predictor, self._predictor_status = _load_predictor(path)
        return self._predictor is not None

    def _fetch_intraday_summaries(
        self,
        *,
        symbol: str,
        lookback_days: int,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        return (
            self._safe_fetch_intraday_summary(
                symbol=symbol,
                interval="1m",
                lookback_days=lookback_days,
            ),
            self._safe_fetch_intraday_summary(
                symbol=symbol,
                interval="5m",
                lookback_days=lookback_days,
            ),
        )

    def _maybe_build_market_index(self, bars: pd.DataFrame) -> pd.DataFrame | None:
        if not bool(self._config.market_relative_feature.enabled):
            return None
        return build_market_relative_frame(
            self._provider,
            bars=bars,
            config=self._config.market_relative_feature,
        )

    def _active_champion_auc(self) -> float | None:
        if self._model_registry is None:
            return None
        champion = self._model_registry.active_champion(suppress_read_errors=True)
        if champion is None:
            return None
        metrics_summary = getattr(champion, "metrics_summary", {})
        if not isinstance(metrics_summary, Mapping):
            return None
        try:
            return float(metrics_summary.get("auc"))
        except (TypeError, ValueError):
            return None

    def _safe_fetch_intraday_summary(
        self,
        *,
        symbol: str,
        interval: str,
        lookback_days: int,
    ) -> pd.DataFrame:
        try:
            frame = self._provider.fetch_intraday_summary(
                symbol=symbol,
                interval=interval,
                lookback_days=max(1, int(lookback_days)),
            )
        except Exception:
            return pd.DataFrame()
        if frame.empty:
            return frame
        if not isinstance(frame.index, pd.DatetimeIndex):
            frame = frame.copy()
            frame.index = pd.to_datetime(frame.index, errors="coerce")
        frame = frame[frame.index.notna()]
        return frame.sort_index()

    def _process_symbol(self, symbol: str, strategy: str, current_equity: float) -> PipelineSignal:
        decision_time = datetime.now()
        start = perf_counter()
        try:
            bars = self._provider.fetch_daily_bars(
                symbol=symbol,
                lookback_days=self._signal_fetch_lookback_days,
            )
            self._health_monitor.record(success=True, latency_sec=perf_counter() - start)
        except DataSourceError as exc:
            self._health_monitor.record(success=False, latency_sec=perf_counter() - start)
            return PipelineSignal(
                symbol=symbol,
                strategy=strategy,
                score=0.0,
                grade="C",
                action="hold",
                target_position=0.0,
                probabilities={"lgbm": 0.0, "xgb": 0.0, "meta": 0.0},
                reasons=[f"data_source:{exc}"],
            )
        bars, bars_time_gate = apply_time_invariants_to_frame(
            bars,
            decision_time=decision_time,
            timezone=str(self._config.app.timezone).strip() or "Asia/Shanghai",
            holding_horizon_days=self._config.labels.horizon_days,
            settlement_lag_days=self._settlement_lag_days,
            require_mature_label=False,
        )
        if bars.empty:
            return PipelineSignal(
                symbol=symbol,
                strategy=strategy,
                score=0.0,
                grade="C",
                action="hold",
                target_position=0.0,
                probabilities={"lgbm": 0.0, "xgb": 0.0, "meta": 0.0},
                reasons=["time_invariant_violation"],
            )
        if len(bars) < self._min_history_days:
            return PipelineSignal(
                symbol=symbol,
                strategy=strategy,
                score=0.0,
                grade="C",
                action="hold",
                target_position=0.0,
                probabilities={"lgbm": 0.0, "xgb": 0.0, "meta": 0.0},
                reasons=self._insufficient_history_reasons(len(bars)),
            )
        analysis_bars = self._clip_signal_analysis_bars(bars)

        intraday_1m, intraday_5m = self._fetch_intraday_summaries(
            symbol=symbol,
            lookback_days=max(120, len(analysis_bars) + 5),
        )
        market_index = self._maybe_build_market_index(analysis_bars)
        features = self._feature_engineer.transform(
            analysis_bars,
            intraday_1m=intraday_1m,
            intraday_5m=intraday_5m,
            market_index=market_index,
        )
        if features.empty:
            return PipelineSignal(
                symbol=symbol,
                strategy=strategy,
                score=0.0,
                grade="C",
                action="hold",
                target_position=0.0,
                probabilities={"lgbm": 0.0, "xgb": 0.0, "meta": 0.0},
                reasons=["feature_empty"],
            )
        features, time_gate = apply_time_invariants_to_frame(
            features,
            decision_time=decision_time,
            timezone=str(self._config.app.timezone).strip() or "Asia/Shanghai",
            holding_horizon_days=self._config.labels.horizon_days,
            settlement_lag_days=self._settlement_lag_days,
            require_mature_label=False,
        )
        if features.empty:
            return PipelineSignal(
                symbol=symbol,
                strategy=strategy,
                score=0.0,
                grade="C",
                action="hold",
                target_position=0.0,
                probabilities={"lgbm": 0.0, "xgb": 0.0, "meta": 0.0},
                reasons=["time_invariant_violation"],
            )
        features = ensure_feedback_feature_frame(features)

        latest_features = features.iloc[-1]
        probabilities = self._infer_probabilities(latest_features)
        champion_auc = self._active_champion_auc()
        cross_review = evaluate_cross_review(
            lgbm_prob=probabilities["lgbm"],
            xgb_prob=probabilities["xgb"],
            meta_prob=probabilities["meta"],
            config=self._config.models.cross_review,
            champion_auc=champion_auc,
        )
        news_component = self._score_news_component(
            symbol=symbol,
            bars=analysis_bars,
            features=features,
            strategy=strategy,
        )
        board_component = self._score_board_component(symbol=symbol, bars=analysis_bars)
        completion_component = self._score_completion_component(
            bars=analysis_bars,
            latest_features=latest_features,
        )

        components = {
            "lgbm": probabilities["lgbm"],
            "xgb": probabilities["xgb"],
            "meta": probabilities["meta"],
            "news": news_component,
            "board": board_component,
            "completion": completion_component,
        }
        bar_t1 = bars.iloc[-2] if len(bars) >= 2 else bars.iloc[-1]
        scored = self._score_engine.score(components=components, strategy=strategy)
        financial_decision = self._financial_filter.evaluate(
            symbol=symbol,
            strategy=strategy,
            snapshot=_financial_snapshot(bar=bar_t1, symbol=symbol),
        )
        if financial_decision.penalty_score > 0:
            adjusted_score = max(0.0, scored.total_score - financial_decision.penalty_score)
            scored = ScoredSignal(
                total_score=adjusted_score,
                grade=_grade_by_strategy(
                    score=adjusted_score,
                    strategy=strategy,
                    config=self._config,
                ),
                components=scored.components,
            )

        liquidity_config = (
            self._config.liquidity_filter_monster
            if strategy == "monster"
            else self._config.liquidity_filter_trend
        )
        liquidity_pass = _liquidity_check(bar_t1, liquidity_config)

        provider_status = self.provider_status()
        self._risk_controller.update_degraded_mode(
            hard_degraded_mode=bool(provider_status.get("hard_degraded_mode", False)),
            soft_degraded_mode=bool(provider_status.get("soft_degraded_mode", False)),
        )
        risk_status = self._risk_controller.evaluate(current_equity=current_equity)

        decision = self._strategy.recommend(
            scored=scored,
            latest_features=latest_features,
            can_open_new_position=risk_status.can_open_new_position,
            liquidity_pass=liquidity_pass,
            cross_review_pass=cross_review.passed,
        )
        strategy_decision_action = decision.action
        strategy_decision_reason = decision.reason
        if not financial_decision.allowed:
            decision.action = "hold"
            decision.target_position = 0.0
            decision.reason = "financial_filter_block"

        reasons = list(cross_review.reasons)
        dropped_rows = _as_int(time_gate.get("dropped_rows"), default=0)
        if dropped_rows > 0:
            reasons.append(f"time_gate_dropped_rows:{dropped_rows}")
        bars_dropped_rows = _as_int(bars_time_gate.get("dropped_rows"), default=0)
        if bars_dropped_rows > 0:
            reasons.append(f"bars_time_gate_dropped_rows:{bars_dropped_rows}")
        if not liquidity_pass:
            reasons.append("liquidity_failed")
        reasons.extend(financial_decision.reasons)
        reasons.append(f"news_component:{news_component:.3f}")
        reasons.append(f"board_component:{board_component:.3f}")
        reasons.append(f"completion_component:{completion_component:.3f}")
        predictor_mode = str(self._predictor_status.get("predictor_mode", "")).strip()
        if predictor_mode and predictor_mode != "artifact_loaded":
            reasons.append(f"predictor_mode:{predictor_mode}")
        predictor_reason = str(self._predictor_status.get("reason", "")).strip()
        if predictor_reason:
            reasons.append(f"predictor_reason:{predictor_reason}")
        reasons.append(decision.reason)

        decision_trace = {
            "provider": {
                "degraded_mode": bool(provider_status.get("degraded_mode", False)),
                "hard_degraded_mode": bool(provider_status.get("hard_degraded_mode", False)),
                "soft_degraded_mode": bool(provider_status.get("soft_degraded_mode", False)),
                "degrade_reason": str(provider_status.get("degrade_reason", "")).strip(),
                "hard_degraded_reason": str(
                    provider_status.get("hard_degraded_reason", "")
                ).strip(),
                "soft_degraded_reason": str(
                    provider_status.get("soft_degraded_reason", "")
                ).strip(),
            },
            "score": {
                "score": round(scored.total_score, 2),
                "grade": scored.grade,
                "components": {key: round(value, 4) for key, value in components.items()},
                "probabilities": {key: round(value, 4) for key, value in probabilities.items()},
            },
            "risk_gate": {
                "passed": bool(risk_status.can_open_new_position),
                "action": risk_status.action,
                "reason": risk_status.reason,
                "degraded_mode": bool(risk_status.degraded_mode),
                "hard_degraded_mode": bool(risk_status.hard_degraded_mode),
                "soft_degraded_mode": bool(risk_status.soft_degraded_mode),
            },
            "liquidity_gate": _build_liquidity_gate_trace(
                bar_t1=bar_t1,
                config=liquidity_config,
                passed=liquidity_pass,
            ),
            "cross_review_gate": {
                "passed": bool(cross_review.passed),
                "merged_probability": round(cross_review.merged_probability, 4),
                "champion_auc": round(champion_auc, 4) if champion_auc is not None else None,
                "reasons": list(cross_review.reasons),
            },
            "financial_gate": {
                "allowed": bool(financial_decision.allowed),
                "penalty_score": round(float(financial_decision.penalty_score), 2),
                "reasons": list(financial_decision.reasons),
            },
            "strategy_decision": {
                "action": strategy_decision_action,
                "reason": strategy_decision_reason,
            },
            "final_decision": {
                "action": decision.action,
                "reason": decision.reason,
                "target_position": round(decision.target_position, 4),
            },
        }

        learning_protocol_ref = self._persist_learning_snapshot(
            symbol=symbol,
            strategy=strategy,
            decision_time=decision_time,
            features=features,
            probabilities=probabilities,
            components=components,
            risk_status=risk_status,
            decision_trace=decision_trace,
        )
        if learning_protocol_ref:
            decision_trace["learning_protocol"] = learning_protocol_ref

        return PipelineSignal(
            symbol=symbol,
            strategy=strategy,
            score=round(scored.total_score, 2),
            grade=scored.grade,
            action=decision.action,
            target_position=round(decision.target_position, 4),
            probabilities={key: round(value, 4) for key, value in probabilities.items()},
            reasons=reasons,
            decision_trace=decision_trace,
        )

    def _persist_learning_snapshot(
        self,
        *,
        symbol: str,
        strategy: str,
        decision_time: datetime,
        features: pd.DataFrame,
        probabilities: Mapping[str, float],
        components: Mapping[str, float],
        risk_status: object,
        decision_trace: Mapping[str, object],
    ) -> dict[str, object] | None:
        if (
            self._sample_store is None
            or self._feature_schema_registry is None
            or self._label_policy_registry is None
            or features.empty
        ):
            return None

        try:
            features = ensure_feedback_feature_frame(features)
            feature_schema = self._feature_schema_registry.register_from_frame(
                features,
                feature_engineer_name=type(self._feature_engineer).__name__,
                feature_engineer_version="transform_t1_v1",
                code_version=str(self._config.evolution.code_commit_id).strip() or "unknown",
                fillna_policy="fill_zero_after_shift",
                normalization_hint="t1_shifted",
            )
            label_policy = self._label_policy_registry.register_from_config(self._config.labels)
            latest_features = features.iloc[-1]
            snapshot_id = f"snap_{decision_time.strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}"
            snapshot = SignalSnapshot(
                snapshot_id=snapshot_id,
                schema_version="1",
                code_version=str(self._config.evolution.code_commit_id).strip() or "unknown",
                symbol=symbol,
                strategy=strategy,
                decision_time=decision_time,
                feature_vector={
                    str(key): float(value)
                    for key, value in latest_features.to_dict().items()
                },
                feature_schema_id=feature_schema.feature_schema_id,
                feature_schema_hash=feature_schema.feature_schema_hash,
                feature_observed_at=decision_time,
                model_outputs={
                    str(key): float(value)
                    for key, value in probabilities.items()
                },
                score_breakdown={
                    str(key): float(value)
                    for key, value in components.items()
                },
                risk_context=_json_safe_mapping(
                    {
                        "can_open_new_position": getattr(
                            risk_status, "can_open_new_position", False
                        ),
                        "degraded_mode": getattr(risk_status, "degraded_mode", False),
                        "reason": getattr(risk_status, "reason", ""),
                    }
                ),
                news_context={
                    "news_component": float(components.get("news", 0.5)),
                    "provider": type(self._news_provider).__name__,
                },
                regime_context={},
                watchlist_source="pipeline_run_once",
                data_quality_score=_estimate_data_quality_score(features),
                sample_weight=1.0,
                runtime_config_hash=self._runtime_config_hash,
                label_policy_id=label_policy.label_policy_id,
                label_policy_hash=label_policy.label_policy_hash,
            )
            self._sample_store.write_snapshot(snapshot)
            self._sample_store.upsert_outcome(
                OutcomeRecord(
                    snapshot_id=snapshot_id,
                    maturity_status=MaturityStatus.PENDING,
                    backfill_fidelity_tier=BackfillFidelityTier.GOLD,
                    backfill_source="runtime_observed",
                )
            )
            return {
                "snapshot_id": snapshot_id,
                "feature_schema_id": feature_schema.feature_schema_id,
                "feature_schema_hash": feature_schema.feature_schema_hash,
                "label_policy_id": label_policy.label_policy_id,
                "label_policy_hash": label_policy.label_policy_hash,
            }
        except Exception:
            return None

    def _clip_signal_analysis_bars(self, bars: pd.DataFrame) -> pd.DataFrame:
        if bars.empty:
            return bars
        if len(bars) <= self._signal_analysis_lookback_days:
            return bars.copy()
        return bars.tail(self._signal_analysis_lookback_days).copy()

    def _infer_probabilities(self, feature_row: pd.Series) -> dict[str, float]:
        if self._predictor is not None:
            return self._predictor.predict_row(feature_row)
        return _controlled_heuristic_probabilities(feature_row)

    def _score_board_component(self, *, symbol: str, bars: pd.DataFrame) -> float:
        if bars.empty:
            return 0.5
        weights: list[float] = []
        values: list[float] = []
        try:
            main_force = self._main_force_tracker.analyze_symbol(symbol, bars)
            values.append(_clip_component(_as_float(main_force.get("score"), default=50.0) / 100.0))
            weights.append(0.55)
            values.append(
                _clip_component(_as_float(main_force.get("completion_score"), default=50.0) / 100.0)
            )
            weights.append(0.15)
        except Exception:
            pass

        northbound = pd.to_numeric(
            bars.get("northbound_net", pd.Series(0.0, index=bars.index, dtype=float)),
            errors="coerce",
        ).fillna(0.0)
        block_trade = pd.to_numeric(
            bars.get("block_trade_net", pd.Series(0.0, index=bars.index, dtype=float)),
            errors="coerce",
        ).fillna(0.0)
        flow_signal = northbound.tail(5).sum() + block_trade.tail(10).sum()
        flow_scale = max(
            float(northbound.abs().tail(20).mean()) + float(block_trade.abs().tail(20).mean()), 1.0
        )
        values.append(_clip_component(0.5 + 0.5 * flow_signal / (flow_scale * 10.0)))
        weights.append(0.20)

        close_series = bars.get("close", pd.Series(dtype=float))
        close = pd.to_numeric(close_series, errors="coerce").dropna()
        if not close.empty:
            ma20 = float(close.tail(min(20, len(close))).mean())
            latest_close = float(close.iloc[-1])
            momentum = latest_close / ma20 - 1.0 if ma20 > 0 else 0.0
            values.append(_clip_component((momentum + 0.08) / 0.16))
            weights.append(0.10)

        return _blend_available_components(values=values, weights=weights, default=0.5)

    def _score_completion_component(
        self,
        *,
        bars: pd.DataFrame,
        latest_features: pd.Series,
    ) -> float:
        if bars.empty:
            return 0.5
        latest_bar = bars.iloc[-1]
        values: list[float] = []
        weights: list[float] = []

        background_fields = [
            "holder_count",
            "block_trade_net",
            "financing_balance",
            "margin_financing_balance",
            "northbound_net",
            "dragon_tiger_flag",
        ]
        values.append(_field_presence_score(latest_bar, background_fields))
        weights.append(0.35)

        financial_fields = ["roe", "debt_ratio", "financial_data_complete"]
        values.append(_field_presence_score(latest_bar, financial_fields))
        weights.append(0.25)

        intraday_feature_names = [
            name for name in latest_features.index if name.startswith(("i1m_", "i5m_"))
        ]
        if intraday_feature_names:
            intraday_values = [
                1.0 if float(latest_features.get(name, 0.0)) != 0.0 else 0.0
                for name in intraday_feature_names
            ]
            values.append(float(np.mean(intraday_values)))
            weights.append(0.20)

        background_completion = _optional_bool_score(latest_bar.get("background_data_complete"))
        if background_completion is not None:
            values.append(background_completion)
            weights.append(0.20)

        return _blend_available_components(values=values, weights=weights, default=0.5)

    def _liquidity_pass(self, bar_t1: pd.Series, strategy: str) -> bool:
        config = (
            self._config.liquidity_filter_monster
            if strategy == "monster"
            else self._config.liquidity_filter_trend
        )
        return _liquidity_check(bar_t1, config)

    def _score_news_component(
        self,
        *,
        symbol: str,
        bars: pd.DataFrame,
        features: pd.DataFrame,
        strategy: str,
    ) -> float:
        try:
            raw_value = self._news_provider.score(
                symbol=symbol,
                bars=bars,
                features=features,
                strategy=strategy,
            )
            value = float(raw_value)
        except Exception:
            return 0.50
        if not math.isfinite(value):
            return 0.50
        return max(0.0, min(1.0, value))


def _liquidity_check(bar_t1: pd.Series, config: LiquidityFilterConfig) -> bool:
    metrics = _liquidity_metrics(bar_t1)
    return (
        metrics["turnover"] >= config.min_daily_turnover
        and metrics["float_market_cap"] >= config.min_float_market_cap
        and metrics["turnover_rate"] <= config.max_turnover_rate
    )


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def _any_degraded(
    provider_status: dict[str, object],
    health_status: dict[str, object],
    evolution_controls: Mapping[str, object] | None = None,
) -> bool:
    return _hard_degraded(provider_status, health_status) or _soft_degraded(evolution_controls)


def _hard_degraded(
    provider_status: dict[str, object],
    health_status: dict[str, object],
) -> bool:
    if bool(provider_status.get("hard_degraded_mode", False)):
        return True
    return bool(provider_status.get("degraded_mode", False)) or bool(
        health_status.get("degraded_mode", False)
    )


def _hard_degraded_reason(
    provider_status: Mapping[str, object],
    health_status: Mapping[str, object],
) -> str:
    provider_reason = str(provider_status.get("degrade_reason", "")).strip()
    if provider_reason:
        return provider_reason
    return str(health_status.get("degrade_reason", "")).strip()


def _soft_degraded(evolution_controls: Mapping[str, object] | None = None) -> bool:
    controls = evolution_controls or {}
    return bool(controls.get("soft_degraded_mode", False)) or bool(
        controls.get("degraded_mode", False)
    ) or bool(controls.get("conservative_mode", False))


def _soft_degraded_reason(evolution_controls: Mapping[str, object] | None = None) -> str:
    controls = evolution_controls or {}
    if not _soft_degraded(controls):
        return ""
    reason = str(controls.get("soft_degraded_reason", "")).strip()
    if reason:
        return reason
    degraded_reason = str(controls.get("degraded_reason", "")).strip()
    if degraded_reason:
        return degraded_reason
    reasons = controls.get("reasons", [])
    if isinstance(reasons, list):
        for item in reasons:
            text = str(item).strip()
            if text:
                return text
    return "evolution_controls"


def _liquidity_metrics(bar_t1: pd.Series) -> dict[str, float]:
    turnover = float(bar_t1.get("turnover", 0.0))
    float_market_cap = float(bar_t1.get("float_market_cap", 0.0))
    turnover_rate = turnover / float_market_cap if float_market_cap > 0 else math.inf
    return {
        "turnover": turnover,
        "float_market_cap": float_market_cap,
        "turnover_rate": turnover_rate,
    }


def _build_liquidity_gate_trace(
    *,
    bar_t1: pd.Series,
    config: LiquidityFilterConfig,
    passed: bool,
) -> dict[str, object]:
    metrics = _liquidity_metrics(bar_t1)
    return {
        "passed": bool(passed),
        "turnover": round(metrics["turnover"], 2),
        "float_market_cap": round(metrics["float_market_cap"], 2),
        "turnover_rate": round(metrics["turnover_rate"], 6),
        "min_daily_turnover": float(config.min_daily_turnover),
        "min_float_market_cap": float(config.min_float_market_cap),
        "max_turnover_rate": float(config.max_turnover_rate),
    }


def _load_predictor(path: str) -> tuple[SignalPredictor | None, dict[str, object]]:
    artifact_path = Path(path)
    status_timestamp = datetime.now().isoformat()
    if not artifact_path.exists():
        return None, {
            "predictor_mode": "controlled_heuristic",
            "reason": "artifact_missing",
            "artifact_path": str(artifact_path),
            "degraded_model_mode": True,
            "degraded_reason_at": status_timestamp,
            "status_timestamp": status_timestamp,
        }
    try:
        predictor = SignalPredictor.load(artifact_path)
        details = predictor.mode_details()
        details["artifact_path"] = str(artifact_path)
        return predictor, details
    except Exception as exc:
        return None, {
            "predictor_mode": "controlled_heuristic",
            "reason": f"artifact_load_failed:{exc.__class__.__name__}",
            "artifact_path": str(artifact_path),
            "degraded_model_mode": True,
            "degraded_reason_at": status_timestamp,
            "status_timestamp": status_timestamp,
        }


def _financial_snapshot(bar: pd.Series, symbol: str) -> dict[str, object]:
    roe = _optional_float(bar.get("roe"))
    debt_ratio = _optional_float(bar.get("debt_ratio"))
    raw_complete = bar.get("financial_data_complete")
    if isinstance(raw_complete, bool):
        complete = raw_complete
    else:
        complete = roe is not None and debt_ratio is not None
    return {
        "symbol": symbol,
        "name": str(bar.get("name", "")),
        "is_st": bool(bar.get("is_st", False)),
        "is_delisting_risk": bool(
            bar.get("is_delisting_risk", False) or bar.get("delisting_risk", False)
        ),
        "roe": roe,
        "debt_ratio": debt_ratio,
        "financial_data_complete": complete,
        "financial_missing_fields": str(bar.get("financial_missing_fields", "")),
        "financial_source": str(bar.get("financial_source", "")),
        "financial_report_date": str(bar.get("financial_report_date", "")),
    }


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = float(text)
            return parsed if math.isfinite(parsed) else None
        except ValueError:
            return None
    return None


def _as_float(value: object, default: float) -> float:
    parsed = _optional_float(value)
    if parsed is None:
        return default
    return parsed


def _clip_component(value: float) -> float:
    return max(0.0, min(1.0, value))


def _blend_available_components(
    *,
    values: list[float],
    weights: list[float],
    default: float,
) -> float:
    usable = [
        (value, weight)
        for value, weight in zip(values, weights, strict=True)
        if math.isfinite(value) and weight > 0
    ]
    if not usable:
        return default
    total_weight = sum(weight for _, weight in usable)
    if total_weight <= 0:
        return default
    return _clip_component(sum(value * weight for value, weight in usable) / total_weight)


def _field_presence_score(row: pd.Series, fields: list[str]) -> float:
    marks: list[float] = []
    for field in fields:
        if field not in row.index:
            continue
        value = row.get(field)
        if isinstance(value, bool):
            marks.append(1.0 if value else 0.0)
            continue
        if value is None:
            marks.append(0.0)
            continue
        if isinstance(value, (int, float)):
            marks.append(1.0 if math.isfinite(float(value)) else 0.0)
            continue
        text = str(value).strip()
        marks.append(1.0 if text else 0.0)
    if not marks:
        return 0.5
    return float(np.mean(marks))


def _optional_bool_score(value: object) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return 1.0 if float(value) > 0 else 0.0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if not normalized:
            return None
        return 1.0 if normalized in {"1", "true", "yes", "y"} else 0.0
    return None


def _controlled_heuristic_probabilities(feature_row: pd.Series) -> dict[str, float]:
    momentum = float(feature_row.get("ret_1d", 0.0) * 12 + feature_row.get("ma_gap", 0.0) * 8)
    volume_signal = float((feature_row.get("volume_ratio", 1.0) - 1.0) * 1.8)
    volatility_penalty = float(feature_row.get("atr_ratio", 0.02) * 4.0)
    completion_boost = float(feature_row.get("background_completion_score", 0.5) - 0.5)
    lgbm = _sigmoid(momentum + volume_signal - volatility_penalty + completion_boost * 0.6)
    xgb = _sigmoid(
        momentum * 0.8 + volume_signal * 0.6 - volatility_penalty * 0.8 + completion_boost * 0.4
    )
    meta = 0.5 * lgbm + 0.5 * xgb
    return {"lgbm": lgbm, "xgb": xgb, "meta": _clip_component(meta)}


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return int(float(text))
        except ValueError:
            return default
    return default


def _grade_by_strategy(score: float, strategy: str, config: StockAnalyzerConfig) -> str:
    normalized = strategy.strip().lower()
    if normalized in config.strategy_scores:
        thresholds = config.strategy_scores[normalized].thresholds
    else:
        thresholds = config.score.thresholds
    if score >= thresholds.s:
        return "S"
    if score >= thresholds.a:
        return "A"
    if score >= thresholds.b:
        return "B"
    return "C"


def _estimate_data_quality_score(features: pd.DataFrame) -> float:
    if features.empty:
        return 0.0
    latest = features.iloc[-1]
    total = max(len(latest.index), 1)
    non_zero = 0
    for value in latest.to_list():
        try:
            if float(value) != 0.0:
                non_zero += 1
        except (TypeError, ValueError):
            continue
    return round(non_zero / total, 6)


def _json_safe_mapping(payload: Mapping[str, object]) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key, value in payload.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            normalized[str(key)] = value
            continue
        normalized[str(key)] = str(value)
    return normalized


def _stable_config_hash(config: StockAnalyzerConfig) -> str:
    payload = config.model_dump(mode="json")
    serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
