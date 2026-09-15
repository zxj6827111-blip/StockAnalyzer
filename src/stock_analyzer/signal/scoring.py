"""Unified scoring engine with strategy-aware thresholds and weights."""

from __future__ import annotations

from stock_analyzer.config import ScoreThresholdConfig, StockAnalyzerConfig
from stock_analyzer.types import ScoredSignal


class ScoreEngine:
    """把有界分量加权成 0-100 分。

    **语义（C2）**：模型分量（``lgbm``/``xgb``/``meta``）是"分数"，其含义由
    工件 label 契约决定（见 ``models/output_semantics.py``）：``event_probability``
    下它近似事件概率，``rank_quantile``（return_rank v3，中间 40% 剔除）下它是
    同日横截面分位归属、``0.5`` 是"上尾 vs 下尾"而不是"涨 vs 跌"。因此这里的
    ``total_score`` 是**排序装置**，``total_score/100`` 不是上涨概率，等级阈值
    也不得解释为概率阈值。
    """

    def __init__(self, config: StockAnalyzerConfig) -> None:
        self._config = config

    def score(self, components: dict[str, float], strategy: str) -> ScoredSignal:
        weights, thresholds = self._resolve_profile(strategy)
        present_weights = {
            name: weight for name, weight in weights.items() if name in components
        }
        if not present_weights:
            present_weights = dict(weights)
        normalized_weights = _normalize_weights(present_weights)

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
