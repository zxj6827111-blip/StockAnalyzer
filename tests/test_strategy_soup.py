from __future__ import annotations

import pandas as pd

from stock_analyzer.config import SoupStrategyConfig
from stock_analyzer.strategy.soup import SoupStrategy
from stock_analyzer.types import ScoredSignal


def test_soup_strategy_signal_strength_multiplier_increases_position() -> None:
    strategy = SoupStrategy(SoupStrategyConfig())
    features = pd.Series({"atr_ratio": 0.2})
    weak = ScoredSignal(total_score=60.0, grade="A", components={})
    strong = ScoredSignal(total_score=92.0, grade="S", components={})

    weak_decision = strategy.recommend(
        scored=weak,
        latest_features=features,
        can_open_new_position=True,
        liquidity_pass=True,
        cross_review_pass=True,
    )
    strong_decision = strategy.recommend(
        scored=strong,
        latest_features=features,
        can_open_new_position=True,
        liquidity_pass=True,
        cross_review_pass=True,
    )

    assert weak_decision.action == "buy"
    assert strong_decision.action == "buy"
    assert strong_decision.target_position > weak_decision.target_position


def test_soup_strategy_demotes_cross_review_near_miss_to_watch() -> None:
    strategy = SoupStrategy(SoupStrategyConfig())
    features = pd.Series({"atr_ratio": 0.2})
    scored = ScoredSignal(total_score=82.0, grade="A", components={})

    decision = strategy.recommend(
        scored=scored,
        latest_features=features,
        can_open_new_position=True,
        liquidity_pass=True,
        cross_review_pass=False,
    )

    assert decision.action == "watch"
    assert decision.target_position == 0.0
    assert decision.reason == "cross_review_near_miss"


def test_soup_strategy_opens_small_recovery_buy_for_degraded_consensus() -> None:
    strategy = SoupStrategy(SoupStrategyConfig())
    features = pd.Series({"atr_ratio": 0.2})
    scored = ScoredSignal(total_score=52.0, grade="B", components={})

    decision = strategy.recommend(
        scored=scored,
        latest_features=features,
        can_open_new_position=True,
        liquidity_pass=True,
        cross_review_pass=True,
        cross_review_reasons=[
            "xgb<0.55",
            "model_diff>0.18",
            "degraded_consensus_lgbm_saturated",
        ],
    )

    assert decision.action == "buy"
    assert decision.reason == "recovery_degraded_consensus"
    assert 0.0 < decision.target_position <= 0.03
