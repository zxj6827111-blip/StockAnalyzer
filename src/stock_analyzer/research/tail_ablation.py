"""Pre-registered Stage 2 trend-tail model and feature arm definitions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from stock_analyzer.feature.trend_candidate_contract import (
    FEATURE_GROUPS,
    GROUP_MARKET_RELATIVE,
    GROUP_TREND_POSITION,
    GROUP_VOLATILITY_OVERHEAT,
    GROUP_VOLUME_LIQUIDITY,
    columns_for_groups,
)
from stock_analyzer.models.tail_net_profit_trainer import KIND_LIGHTGBM, KIND_LOGISTIC

EVENT_FEATURE_COLUMNS = ("moneyflow_net_1d_lag", "top_list_count_1d_lag")
MODEL_KINDS = (KIND_LOGISTIC, KIND_LIGHTGBM)


@dataclass(frozen=True)
class TailAblationConfig:
    config_id: str
    feature_groups: tuple[str, ...]
    extra_features: tuple[str, ...] = ()
    blocked_reason: str = ""

    @property
    def feature_names(self) -> tuple[str, ...]:
        return (*columns_for_groups(self.feature_groups), *self.extra_features)


_ALL_GROUPS = tuple(FEATURE_GROUPS)
TAIL_ABLATION_CONFIGS: tuple[TailAblationConfig, ...] = (
    TailAblationConfig("all_four_price_groups", _ALL_GROUPS),
    TailAblationConfig(
        "drop_market_relative",
        tuple(group for group in _ALL_GROUPS if group != GROUP_MARKET_RELATIVE),
    ),
    TailAblationConfig(
        "drop_trend_position",
        tuple(group for group in _ALL_GROUPS if group != GROUP_TREND_POSITION),
    ),
    TailAblationConfig(
        "drop_volatility_overheat",
        tuple(group for group in _ALL_GROUPS if group != GROUP_VOLATILITY_OVERHEAT),
    ),
    TailAblationConfig("volume_liquidity_only", (GROUP_VOLUME_LIQUIDITY,)),
    TailAblationConfig(
        "all_price_plus_moneyflow_top_list",
        _ALL_GROUPS,
        EVENT_FEATURE_COLUMNS,
        "event_feature_point_in_time_availability_not_proven",
    ),
)


def stage2_arm_registry() -> list[dict[str, Any]]:
    """Return the frozen 6 × 2 matrix, including arms blocked before training."""
    return [
        {
            "arm_id": f"{config.config_id}__{model_kind}",
            "config_id": config.config_id,
            "model_kind": model_kind,
            "feature_groups": list(config.feature_groups),
            "feature_names": list(config.feature_names),
            "status": "blocked" if config.blocked_reason else "registered",
            "blockers": [config.blocked_reason] if config.blocked_reason else [],
        }
        for config in TAIL_ABLATION_CONFIGS
        for model_kind in MODEL_KINDS
    ]


def holm_adjusted_p_values(
    p_values: Mapping[str, float | None],
    *,
    family_size: int,
) -> dict[str, float]:
    """Step-down Holm adjusted p-values over the fixed family size.

    Missing or blocked arms receive p=1 for adjustment. This keeps the registered family
    intact and cannot make a completed arm look more significant because another arm failed.
    """
    if family_size < len(p_values) or family_size < 1:
        raise ValueError("family_size must be positive and at least the number of p-values")
    normalized: dict[str, float] = {}
    for name, value in p_values.items():
        if value is None:
            normalized[name] = 1.0
            continue
        number = float(value)
        if not 0.0 <= number <= 1.0:
            raise ValueError(f"p-value for {name!r} is outside [0, 1]")
        normalized[name] = number
    ordered = sorted(normalized, key=lambda name: (normalized[name], name))
    adjusted: dict[str, float] = {}
    running = 0.0
    for rank, name in enumerate(ordered):
        candidate = min(1.0, (family_size - rank) * normalized[name])
        running = max(running, candidate)
        adjusted[name] = running
    for name in normalized:
        adjusted.setdefault(name, 1.0)
    return adjusted


def select_stage2_candidate(arms: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply the frozen development gates and four-fold OOS return tie-break.

    This is development-only candidate selection. Passing it never opens independent
    confirmation or makes an arm release-eligible.
    """
    eligible: list[dict[str, Any]] = []
    for arm in arms:
        blockers: list[str] = []
        report = arm.get("report") or {}
        quality = report.get("quality") or {}
        folds = report.get("folds") or []
        if arm.get("status") != "completed":
            blockers.append("training_or_walk_forward_not_completed")
        if quality.get("passed") is not True:
            blockers.append("development_quality_gates_not_passed")
        if float(arm.get("holm_adjusted_p", 1.0)) >= 0.05:
            blockers.append("development_holm_adjusted_p_not_below_0_05")
        if report.get("fold_count") != 4 or len(folds) != 4:
            blockers.append("exactly_four_real_test_folds_required")
        if any((fold.get("artifact_roundtrip") or {}).get("status") != "passed" for fold in folds):
            blockers.append("model_artifact_roundtrip_missing_or_failed")
        means: list[float] = []
        feature_counts: list[int] = []
        for fold in folds:
            summary = (fold.get("selection_metrics") or {}).get("p_net_profit_5d_tail") or {}
            mean = summary.get("mean_net_return")
            if mean is None:
                blockers.append("one_or_more_test_folds_has_no_matured_candidate_returns")
            else:
                means.append(float(mean))
            kept = (fold.get("feature_selection") or {}).get("kept_features")
            if not isinstance(kept, list) or not kept:
                blockers.append("fold_feature_selection_identity_missing")
            else:
                feature_counts.append(len(kept))
        arm["selection_eligible"] = not blockers
        arm["selection_blockers"] = sorted(set(blockers))
        if not blockers:
            eligible.append(
                {
                    "arm_id": str(arm.get("arm_id") or ""),
                    "model_kind": str(arm.get("model_kind") or ""),
                    "mean_four_fold_oos_net_return": sum(means) / len(means),
                    "mean_features_per_fold": sum(feature_counts) / len(feature_counts),
                    "holm_adjusted_p": float(arm["holm_adjusted_p"]),
                }
            )
    eligible.sort(
        key=lambda item: (
            -item["mean_four_fold_oos_net_return"],
            item["mean_features_per_fold"],
            item["model_kind"] != KIND_LOGISTIC,
            item["arm_id"],
        )
    )
    return {
        "status": "candidate_selected" if eligible else "no_qualified_candidate",
        "selected_arm_id": eligible[0]["arm_id"] if eligible else None,
        "eligible_arms_ranked": eligible,
        "rule": (
            "all development gates and Holm-adjusted exploratory test pass; then rank by "
            "four-fold mean OOS net return, fewer features, Logistic Regression, arm id"
        ),
        "release_eligible": False,
    }


__all__ = [
    "EVENT_FEATURE_COLUMNS",
    "MODEL_KINDS",
    "TAIL_ABLATION_CONFIGS",
    "TailAblationConfig",
    "holm_adjusted_p_values",
    "select_stage2_candidate",
    "stage2_arm_registry",
]
