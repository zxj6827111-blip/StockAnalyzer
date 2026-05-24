from __future__ import annotations

from stock_analyzer.config import CrossReviewConfig
from stock_analyzer.signal.cross_review import evaluate_cross_review


def test_cross_review_passes_when_all_thresholds_met() -> None:
    config = CrossReviewConfig(p_lgbm_min=0.62, p_xgb_min=0.60, max_diff=0.12, p_meta_min=0.58)
    result = evaluate_cross_review(0.70, 0.68, 0.66, config)
    assert result.passed is True
    assert result.reasons == []


def test_cross_review_fails_when_diff_or_meta_not_met() -> None:
    config = CrossReviewConfig(p_lgbm_min=0.62, p_xgb_min=0.60, max_diff=0.12, p_meta_min=0.58)
    result = evaluate_cross_review(0.80, 0.55, 0.50, config)
    assert result.passed is False
    assert "xgb<0.60" in result.reasons
    assert "meta<0.58" in result.reasons


def test_cross_review_allows_degraded_consensus_when_lgbm_saturates() -> None:
    config = CrossReviewConfig(p_lgbm_min=0.60, p_xgb_min=0.55, max_diff=0.18, p_meta_min=0.54)
    result = evaluate_cross_review(1.0, 0.39, 0.58, config)
    assert result.passed is True
    assert result.mode == "degraded_consensus"
    assert result.degraded_consensus is True
    assert "xgb<0.55" in result.reasons
    assert "model_diff>0.18" in result.reasons
    assert "degraded_consensus_lgbm_saturated" in result.reasons


def test_cross_review_relaxes_thresholds_when_active_champion_is_weak() -> None:
    config = CrossReviewConfig(
        p_lgbm_min=0.62,
        p_xgb_min=0.60,
        max_diff=0.12,
        p_meta_min=0.58,
        champion_auc_low=0.55,
        champion_auc_high=0.62,
        relax_threshold_delta=0.02,
        relax_max_diff_delta=0.03,
    )
    result = evaluate_cross_review(0.60, 0.58, 0.56, config, champion_auc=0.52)
    assert result.passed is True
    assert result.reasons == []


def test_cross_review_tightens_thresholds_when_active_champion_is_strong() -> None:
    config = CrossReviewConfig(
        p_lgbm_min=0.62,
        p_xgb_min=0.60,
        max_diff=0.12,
        p_meta_min=0.58,
        champion_auc_low=0.55,
        champion_auc_high=0.62,
        tighten_threshold_delta=0.01,
        tighten_max_diff_delta=0.02,
    )
    result = evaluate_cross_review(0.62, 0.61, 0.58, config, champion_auc=0.68)
    assert result.passed is False
    assert "lgbm<0.63" in result.reasons
    assert "meta<0.59" in result.reasons
