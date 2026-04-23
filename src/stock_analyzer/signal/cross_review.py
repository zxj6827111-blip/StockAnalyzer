"""Cross-review gate between LightGBM/XGBoost/Meta scores."""

from __future__ import annotations

from stock_analyzer.config import CrossReviewConfig
from stock_analyzer.types import CrossReviewResult


def _clamp_probability(value: float) -> float:
    return max(0.0, min(1.0, value))


def evaluate_cross_review(
    lgbm_prob: float,
    xgb_prob: float,
    meta_prob: float,
    config: CrossReviewConfig,
    champion_auc: float | None = None,
) -> CrossReviewResult:
    lgbm = _clamp_probability(lgbm_prob)
    xgb = _clamp_probability(xgb_prob)
    meta = _clamp_probability(meta_prob)
    reasons: list[str] = []
    thresholds = _effective_thresholds(config=config, champion_auc=champion_auc)

    if lgbm < thresholds["p_lgbm_min"]:
        reasons.append(f"lgbm<{thresholds['p_lgbm_min']:.2f}")
    if xgb < thresholds["p_xgb_min"]:
        reasons.append(f"xgb<{thresholds['p_xgb_min']:.2f}")
    if abs(lgbm - xgb) > thresholds["max_diff"]:
        reasons.append(f"model_diff>{thresholds['max_diff']:.2f}")
    if meta < thresholds["p_meta_min"]:
        reasons.append(f"meta<{thresholds['p_meta_min']:.2f}")

    merged = (lgbm + xgb + meta) / 3.0
    return CrossReviewResult(passed=not reasons, merged_probability=merged, reasons=reasons)


def _effective_thresholds(
    *,
    config: CrossReviewConfig,
    champion_auc: float | None,
) -> dict[str, float]:
    thresholds = {
        "p_lgbm_min": float(config.p_lgbm_min),
        "p_xgb_min": float(config.p_xgb_min),
        "p_meta_min": float(config.p_meta_min),
        "max_diff": float(config.max_diff),
    }
    if champion_auc is None:
        return thresholds
    if champion_auc < float(config.champion_auc_low):
        delta = float(config.relax_threshold_delta)
        thresholds["p_lgbm_min"] -= delta
        thresholds["p_xgb_min"] -= delta
        thresholds["p_meta_min"] -= delta
        thresholds["max_diff"] += float(config.relax_max_diff_delta)
    elif champion_auc > float(config.champion_auc_high):
        delta = float(config.tighten_threshold_delta)
        thresholds["p_lgbm_min"] += delta
        thresholds["p_xgb_min"] += delta
        thresholds["p_meta_min"] += delta
        thresholds["max_diff"] -= float(config.tighten_max_diff_delta)
    thresholds["p_lgbm_min"] = _clamp_probability(thresholds["p_lgbm_min"])
    thresholds["p_xgb_min"] = _clamp_probability(thresholds["p_xgb_min"])
    thresholds["p_meta_min"] = _clamp_probability(thresholds["p_meta_min"])
    thresholds["max_diff"] = max(0.0, thresholds["max_diff"])
    return thresholds
