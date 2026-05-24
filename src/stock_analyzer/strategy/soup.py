"""Soup strategy decision logic."""

from __future__ import annotations

from collections.abc import Sequence

import pandas as pd

from stock_analyzer.config import SoupStrategyConfig
from stock_analyzer.types import ScoredSignal, TradeDecision


class SoupStrategy:
    """Apply execution gating to scored signals."""

    def __init__(self, config: SoupStrategyConfig) -> None:
        self._config = config

    def recommend(
        self,
        scored: ScoredSignal,
        latest_features: pd.Series,
        can_open_new_position: bool,
        liquidity_pass: bool,
        cross_review_pass: bool,
        cross_review_reasons: Sequence[str] | None = None,
    ) -> TradeDecision:
        if not can_open_new_position:
            return TradeDecision(action="hold", target_position=0.0, reason="risk_gate")
        if not liquidity_pass:
            return TradeDecision(action="hold", target_position=0.0, reason="liquidity_filter")
        if not cross_review_pass:
            if scored.grade in {"S", "A", "B"}:
                return TradeDecision(
                    action="watch",
                    target_position=0.0,
                    reason="cross_review_near_miss",
                )
            return TradeDecision(action="hold", target_position=0.0, reason="cross_review")

        if self._is_recovery_buy(scored=scored, cross_review_reasons=cross_review_reasons):
            atr_ratio = float(latest_features.get("atr_ratio", 0.02))
            position = self._dynamic_position(
                atr_ratio,
                signal_score=float(scored.total_score),
                grade=scored.grade,
            )
            position = min(position, float(self._config.recovery_max_position))
            return TradeDecision(
                action="buy",
                target_position=max(0.01, position),
                reason="recovery_degraded_consensus",
            )

        if scored.grade in {"S", "A"}:
            atr_ratio = float(latest_features.get("atr_ratio", 0.02))
            position = self._dynamic_position(
                atr_ratio,
                signal_score=float(scored.total_score),
                grade=scored.grade,
            )
            return TradeDecision(action="buy", target_position=position, reason="soup_entry")
        if scored.grade == "B":
            return TradeDecision(action="watch", target_position=0.0, reason="watchlist")
        return TradeDecision(action="hold", target_position=0.0, reason="score_too_low")

    def _is_recovery_buy(
        self,
        *,
        scored: ScoredSignal,
        cross_review_reasons: Sequence[str] | None,
    ) -> bool:
        if not bool(self._config.recovery_buy_enabled):
            return False
        reasons = {str(reason) for reason in (cross_review_reasons or [])}
        if "degraded_consensus_lgbm_saturated" not in reasons:
            return False
        if str(scored.grade) not in set(self._config.recovery_allowed_grades):
            return False
        return float(scored.total_score) >= float(self._config.recovery_min_score)

    def _dynamic_position(self, atr_ratio: float, *, signal_score: float, grade: str) -> float:
        safe_atr = max(atr_ratio, 1e-6)
        base_position = 0.02 / safe_atr
        position = base_position * self._signal_strength_multiplier(
            signal_score=signal_score,
            grade=grade,
        )
        return max(0.01, min(position, 0.18))

    def _signal_strength_multiplier(self, *, signal_score: float, grade: str) -> float:
        normalized_score = max(0.0, min((signal_score - 50.0) / 50.0, 1.0))
        multiplier = 0.85 + normalized_score * 0.25
        if grade == "S":
            multiplier += 0.12
        elif grade == "A":
            multiplier += 0.05
        return max(0.85, min(multiplier, 1.30))
