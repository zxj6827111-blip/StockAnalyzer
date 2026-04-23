"""M5 label optimization diagnostics with robust observability fallback."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

_PRIMARY_LABEL_KEYS = ("label", "label_primary", "label_soup_tp_before_sl", "target", "y")


@dataclass(frozen=True, slots=True)
class M5LabelMetrics:
    """Interpretable metrics for label quality and stability."""

    valid_symbols: int
    labeled_samples: int
    label_coverage_ratio: float
    positive_label_ratio: float
    label_balance_score: float
    seed_consistency: float
    seed_observability_ratio: float
    return_alignment: float
    alignment_samples: int


@dataclass(frozen=True, slots=True)
class M5LabelOptimizationResult:
    """M5 output payload."""

    score: float
    status: str
    metrics: M5LabelMetrics


@dataclass(frozen=True, slots=True)
class M5StrategyLinkage:
    """Recommended strategy linkage actions derived from M5 diagnostics."""

    mode: str
    target_strategy: str
    confidence: float
    reason: str
    change_keys: list[str]
    suggested_overrides: dict[str, float | int | str]


def evaluate_m5_label_optimization(
    records: Sequence[Mapping[str, object]],
    label_coverage_floor: float = 0.40,
    positive_ratio_low: float = 0.30,
    positive_ratio_high: float = 0.70,
    seed_consistency_floor: float = 0.70,
    alignment_floor: float = 0.52,
    limited_observability_score: float = 62.0,
) -> M5LabelOptimizationResult:
    """Evaluate M5 dynamic-label quality with optional multi-seed consistency checks.

    The evaluator accepts sparse records and gracefully degrades when labels
    or seed-level diagnostics are partially unavailable.
    """
    valid_symbols = 0
    labeled_samples = 0
    positive_labels = 0
    seed_observed = 0
    seed_consistency_values: list[float] = []
    alignment_hits = 0
    alignment_samples = 0

    for record in records:
        open_px = _as_float(record.get("open"), default=0.0)
        close_px = _as_float(record.get("close"), default=0.0)
        if open_px <= 0.0 or close_px <= 0.0:
            continue
        valid_symbols += 1

        base_label, seed_labels = _extract_labels(record)
        if base_label is None:
            continue

        labeled_samples += 1
        positive_labels += base_label
        if len(seed_labels) >= 2:
            seed_observed += 1
            mean_seed = float(np.mean(np.asarray(seed_labels, dtype=float)))
            seed_consistency_values.append(max(mean_seed, 1.0 - mean_seed))

        is_up = close_px >= open_px
        if (base_label == 1 and is_up) or (base_label == 0 and not is_up):
            alignment_hits += 1
        alignment_samples += 1

    if valid_symbols == 0:
        metrics = M5LabelMetrics(
            valid_symbols=0,
            labeled_samples=0,
            label_coverage_ratio=0.0,
            positive_label_ratio=0.0,
            label_balance_score=0.0,
            seed_consistency=0.0,
            seed_observability_ratio=0.0,
            return_alignment=0.0,
            alignment_samples=0,
        )
        return M5LabelOptimizationResult(score=50.0, status="no_data", metrics=metrics)

    if labeled_samples == 0:
        metrics = M5LabelMetrics(
            valid_symbols=valid_symbols,
            labeled_samples=0,
            label_coverage_ratio=0.0,
            positive_label_ratio=0.0,
            label_balance_score=0.0,
            seed_consistency=0.0,
            seed_observability_ratio=0.0,
            return_alignment=0.0,
            alignment_samples=0,
        )
        return M5LabelOptimizationResult(
            score=_clamp100(limited_observability_score),
            status="limited_observability",
            metrics=metrics,
        )

    label_coverage_ratio = labeled_samples / max(valid_symbols, 1)
    positive_label_ratio = positive_labels / max(labeled_samples, 1)
    label_balance_score = 1.0 - abs(positive_label_ratio - 0.5) * 2.0
    seed_observability_ratio = seed_observed / max(labeled_samples, 1)
    seed_consistency = (
        float(np.mean(np.asarray(seed_consistency_values, dtype=float)))
        if seed_consistency_values
        else 1.0
    )
    return_alignment = alignment_hits / max(alignment_samples, 1)

    out_of_balance_penalty = (
        0.0
        if positive_ratio_low <= positive_label_ratio <= positive_ratio_high
        else 12.0
        + min(
            8.0,
            abs(positive_label_ratio - (positive_ratio_low + positive_ratio_high) * 0.5) * 25.0,
        )
    )
    coverage_penalty = (1.0 - label_coverage_ratio) * 35.0
    balance_penalty = (1.0 - max(0.0, label_balance_score)) * 20.0
    seed_penalty = (1.0 - seed_consistency) * 20.0 * seed_observability_ratio
    alignment_penalty = (1.0 - return_alignment) * 25.0
    score = _clamp100(
        100.0
        - coverage_penalty
        - balance_penalty
        - seed_penalty
        - alignment_penalty
        - out_of_balance_penalty
    )

    seed_ok = seed_observability_ratio < 0.20 or seed_consistency >= seed_consistency_floor
    alignment_ok = alignment_samples == 0 or return_alignment >= alignment_floor
    balance_ok = positive_ratio_low <= positive_label_ratio <= positive_ratio_high
    if label_coverage_ratio < label_coverage_floor * 0.5:
        status = "limited_observability"
    elif positive_label_ratio <= 0.10 or positive_label_ratio >= 0.90:
        status = "degraded"
    elif (
        seed_observability_ratio >= 0.50
        and seed_consistency < seed_consistency_floor * 0.70
        and score < 70.0
    ):
        status = "degraded"
    elif alignment_samples >= 5 and return_alignment < alignment_floor * 0.80 and score < 70.0:
        status = "degraded"
    elif label_coverage_ratio >= label_coverage_floor and balance_ok and seed_ok and alignment_ok:
        status = "optimized" if score >= 75.0 else "watch"
    else:
        status = "watch" if score >= 60.0 else "degraded"

    metrics = M5LabelMetrics(
        valid_symbols=valid_symbols,
        labeled_samples=labeled_samples,
        label_coverage_ratio=label_coverage_ratio,
        positive_label_ratio=positive_label_ratio,
        label_balance_score=max(0.0, min(1.0, label_balance_score)),
        seed_consistency=seed_consistency,
        seed_observability_ratio=seed_observability_ratio,
        return_alignment=return_alignment,
        alignment_samples=alignment_samples,
    )
    return M5LabelOptimizationResult(score=score, status=status, metrics=metrics)


def build_m5_strategy_linkage(
    result: M5LabelOptimizationResult,
    *,
    min_labeled_samples: int = 5,
    target_strategy: str = "soup",
) -> M5StrategyLinkage:
    """Build strategy-linkage recommendations from one M5 result.

    Args:
        result: M5 evaluation output.
        min_labeled_samples: Minimum labeled sample count to allow linkage output.
        target_strategy: Strategy name for downstream proposal context.

    Returns:
        A structured linkage recommendation for orchestrator/governance.
    """
    minimum_samples = max(1, min_labeled_samples)
    metrics = result.metrics
    strategy = target_strategy.strip().lower() or "soup"

    if (
        result.status in {"no_data", "limited_observability"}
        or metrics.labeled_samples < minimum_samples
    ):
        return M5StrategyLinkage(
            mode="observe_only",
            target_strategy=strategy,
            confidence=0.35,
            reason="insufficient labeled samples for safe label tuning linkage",
            change_keys=[],
            suggested_overrides={},
        )

    if result.status == "optimized":
        return M5StrategyLinkage(
            mode="hold",
            target_strategy=strategy,
            confidence=0.80,
            reason="label quality remains balanced and aligned, keep current label policy",
            change_keys=[],
            suggested_overrides={},
        )

    if result.status == "watch":
        return M5StrategyLinkage(
            mode="review_queue",
            target_strategy=strategy,
            confidence=0.55,
            reason="label quality is borderline, queue human review before label-parameter change",
            change_keys=["observation_queue_label_review"],
            suggested_overrides={"label_review_priority": "normal"},
        )

    change_keys: list[str] = []
    overrides: dict[str, float | int | str] = {}
    reason_parts: list[str] = []

    if metrics.positive_label_ratio >= 0.80:
        change_keys.extend(["label_take_profit_pct", "label_horizon_days"])
        overrides["label_take_profit_pct"] = 0.04
        overrides["label_horizon_days"] = 4
        reason_parts.append("positive labels are over-concentrated")
    elif metrics.positive_label_ratio <= 0.20:
        change_keys.extend(["label_stop_loss_pct", "label_horizon_days"])
        overrides["label_stop_loss_pct"] = 0.06
        overrides["label_horizon_days"] = 6
        reason_parts.append("positive labels are under-represented")

    if metrics.seed_observability_ratio >= 0.50 and metrics.seed_consistency < 0.70:
        change_keys.append("label_seed_aggregation_rule")
        overrides["label_seed_aggregation_rule"] = "majority_vote"
        reason_parts.append("seed-level consistency is unstable")

    if (
        metrics.alignment_samples >= max(3, minimum_samples // 2)
        and metrics.return_alignment < 0.50
    ):
        change_keys.append("label_alignment_filter")
        overrides["label_alignment_filter"] = 0.50
        reason_parts.append("label direction has weak next-bar alignment")

    if metrics.label_coverage_ratio < 0.40:
        change_keys.append("label_coverage_gate")
        overrides["label_coverage_gate"] = 0.40
        reason_parts.append("label coverage is below safety floor")

    deduped_keys = _dedupe_keep_order(change_keys)
    if not deduped_keys:
        deduped_keys = ["label_global_review"]
        reason_parts.append("multiple M5 red flags detected")

    confidence = _clamp01(0.60 + max(0.0, 70.0 - result.score) / 100.0)
    reason = "; ".join(reason_parts)
    return M5StrategyLinkage(
        mode="propose_label_tuning",
        target_strategy=strategy,
        confidence=confidence,
        reason=reason,
        change_keys=deduped_keys,
        suggested_overrides=overrides,
    )


def _extract_labels(record: Mapping[str, object]) -> tuple[int | None, list[int]]:
    base_label: int | None = None
    seed_labels: list[int] = []
    fallback_labels: list[int] = []

    for key in _PRIMARY_LABEL_KEYS:
        parsed = _as_binary(record.get(key))
        if parsed is not None:
            base_label = parsed
            break

    for raw_key, raw_value in record.items():
        if not isinstance(raw_key, str):
            continue
        lowered = raw_key.lower()
        if lowered in _PRIMARY_LABEL_KEYS:
            continue
        if "label" not in lowered and lowered not in {"target", "y"}:
            continue
        parsed = _as_binary(raw_value)
        if parsed is None:
            continue
        if "seed" in lowered:
            seed_labels.append(parsed)
        elif base_label is None:
            fallback_labels.append(parsed)

    if base_label is None:
        if fallback_labels:
            base_label = fallback_labels[0]
        elif seed_labels:
            base_label = 1 if float(np.mean(np.asarray(seed_labels, dtype=float))) >= 0.5 else 0

    return base_label, seed_labels


def _as_binary(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "y", "up", "long", "win"}:
            return 1
        if lowered in {"false", "no", "n", "down", "short", "loss"}:
            return 0
        try:
            parsed = float(value)
        except ValueError:
            return None
    else:
        return None

    if 0.0 <= parsed <= 1.0:
        return 1 if parsed >= 0.5 else 0
    if parsed == 2.0:
        return 1
    if parsed == -1.0:
        return 0
    return None


def _as_float(value: object, default: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _clamp100(value: float) -> float:
    return max(0.0, min(100.0, value))


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _dedupe_keep_order(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        stripped = item.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        ordered.append(stripped)
    return ordered
