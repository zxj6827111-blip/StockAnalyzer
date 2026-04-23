"""Unified scoring engine with strategy-aware thresholds and weights."""

from __future__ import annotations

from stock_analyzer.config import ScoreThresholdConfig, StockAnalyzerConfig
from stock_analyzer.types import ScoredSignal


class ScoreEngine:
    """Compute 0-100 score from weighted component probabilities."""

    def __init__(self, config: StockAnalyzerConfig) -> None:
        self._config = config

    def score(self, components: dict[str, float], strategy: str) -> ScoredSignal:
        weights, thresholds = self._resolve_profile(strategy)
        normalized_weights = _normalize_weights(weights)

        total = 0.0
        normalized_components: dict[str, float] = {}
        for name, weight in normalized_weights.items():
            value = _clamp(components.get(name, 0.0))
            normalized_components[name] = value
            total += value * weight

        total_score = total * 100.0
        grade = _grade(total_score, thresholds)
        return ScoredSignal(total_score=total_score, grade=grade, components=normalized_components)

    def _resolve_profile(self, strategy: str) -> tuple[dict[str, float], ScoreThresholdConfig]:
        if strategy in self._config.strategy_scores:
            profile = self._config.strategy_scores[strategy]
            return profile.weights, profile.thresholds
        return self._config.score.weights, self._config.score.thresholds


def _normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    total = sum(weights.values())
    if total <= 0:
        raise ValueError("score weights must sum to a positive value")
    return {key: value / total for key, value in weights.items()}


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _grade(score: float, thresholds: ScoreThresholdConfig) -> str:
    if score >= thresholds.s:
        return "S"
    if score >= thresholds.a:
        return "A"
    if score >= thresholds.b:
        return "B"
    return "C"
