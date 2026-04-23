"""Learning-protocol sample weighting from M1/M3/M7 feedback signals."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from stock_analyzer.learning.sample_schema import OutcomeRecord, SignalSnapshot


@dataclass(frozen=True, slots=True)
class FeedbackWeightResult:
    """Resolved per-sample weight with module-level attribution."""

    snapshot_id: str
    base_weight: float
    module_weights: dict[str, float]
    final_weight: float
    reason_codes: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FeedbackWeightSummary:
    """Aggregate summary used in training metadata and diagnostics."""

    row_count: int
    applied_rows: int
    mean_weight: float
    min_weight: float
    max_weight: float
    module_active_rows: dict[str, int]
    module_mean_weight: dict[str, float]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def build_feedback_weight(
    *,
    snapshot: SignalSnapshot,
    outcome: OutcomeRecord,
    apply_feedback: bool = True,
    clip_low: float = 0.35,
    clip_high: float = 3.0,
) -> FeedbackWeightResult:
    """Build one feedback-aware weight for manifest-driven training."""

    base_weight = max(1e-6, float(snapshot.sample_weight))
    if not apply_feedback:
        return FeedbackWeightResult(
            snapshot_id=snapshot.snapshot_id,
            base_weight=base_weight,
            module_weights={"M1": 1.0, "M3": 1.0, "M7": 1.0},
            final_weight=base_weight,
            reason_codes=[],
        )

    reason_codes: list[str] = []
    module_weights = {
        "M1": _m1_weight(snapshot=snapshot, outcome=outcome, reason_codes=reason_codes),
        "M3": _m3_weight(snapshot=snapshot, reason_codes=reason_codes),
        "M7": _m7_weight(snapshot=snapshot, reason_codes=reason_codes),
    }
    final_weight = base_weight
    for weight in module_weights.values():
        final_weight *= max(0.05, float(weight))
    clipped_weight = _clamp(final_weight, clip_low, clip_high)
    if clipped_weight != final_weight:
        reason_codes.append("feedback_weight_clipped")
    return FeedbackWeightResult(
        snapshot_id=snapshot.snapshot_id,
        base_weight=base_weight,
        module_weights=module_weights,
        final_weight=clipped_weight,
        reason_codes=_dedupe(reason_codes),
    )


def summarize_feedback_weights(
    rows: list[FeedbackWeightResult],
) -> FeedbackWeightSummary:
    """Aggregate sample-weight results for artifact metadata."""

    if not rows:
        return FeedbackWeightSummary(
            row_count=0,
            applied_rows=0,
            mean_weight=1.0,
            min_weight=1.0,
            max_weight=1.0,
            module_active_rows={"M1": 0, "M3": 0, "M7": 0},
            module_mean_weight={"M1": 1.0, "M3": 1.0, "M7": 1.0},
        )

    module_active_rows = {"M1": 0, "M3": 0, "M7": 0}
    module_totals = {"M1": 0.0, "M3": 0.0, "M7": 0.0}
    applied_rows = 0
    for row in rows:
        if abs(row.final_weight - row.base_weight) > 1e-9:
            applied_rows += 1
        for module_name, weight in row.module_weights.items():
            if module_name not in module_active_rows:
                continue
            module_totals[module_name] += float(weight)
            if abs(float(weight) - 1.0) > 1e-9:
                module_active_rows[module_name] += 1
    final_weights = [float(item.final_weight) for item in rows]
    module_mean_weight = {
        module_name: module_totals[module_name] / max(len(rows), 1)
        for module_name in module_totals
    }
    return FeedbackWeightSummary(
        row_count=len(rows),
        applied_rows=applied_rows,
        mean_weight=sum(final_weights) / len(final_weights),
        min_weight=min(final_weights),
        max_weight=max(final_weights),
        module_active_rows=module_active_rows,
        module_mean_weight=module_mean_weight,
    )


def _m1_weight(
    *,
    snapshot: SignalSnapshot,
    outcome: OutcomeRecord,
    reason_codes: list[str],
) -> float:
    risk_context = snapshot.risk_context
    explicit_bucket = str(
        risk_context.get("m1_negative_case_bucket", risk_context.get("negative_case_bucket", ""))
    ).strip().lower()
    bucket_weight = {
        "mild": 1.10,
        "medium": 1.25,
        "severe": 1.50,
    }.get(explicit_bucket, 1.0)
    if bucket_weight > 1.0:
        reason_codes.append(f"m1_bucket:{explicit_bucket}")

    if explicit_bucket == "":
        derived_bucket = _derive_m1_bucket_from_outcome(outcome=outcome)
        bucket_weight = {
            "mild": 1.05,
            "medium": 1.15,
            "severe": 1.30,
        }.get(derived_bucket, bucket_weight)
        if derived_bucket:
            reason_codes.append(f"m1_outcome_bucket:{derived_bucket}")

    similarity = _first_float(
        risk_context.get("m1_similarity"),
        risk_context.get("m1_negative_case_similarity"),
    )
    similarity_weight = 1.0
    if similarity is not None and similarity > 0.0:
        similarity_weight = 1.0 + 0.15 * _clamp(similarity, 0.0, 1.0)
        reason_codes.append("m1_similarity")

    raw_reason_codes = risk_context.get("m1_reason_codes", risk_context.get("reason_codes", []))
    explicit_reason_count = (
        len([item for item in raw_reason_codes if isinstance(item, str) and item.strip()])
        if isinstance(raw_reason_codes, list)
        else 0
    )
    reason_weight = 1.05 if explicit_reason_count > 0 else 1.0
    if reason_weight > 1.0:
        reason_codes.append("m1_reason_codes")

    poison_flag = bool(risk_context.get("m1_poison_flag", risk_context.get("leak_flag", False)))
    poison_weight = 0.40 if poison_flag else 1.0
    if poison_flag:
        reason_codes.append("m1_poison_flag")

    return bucket_weight * similarity_weight * reason_weight * poison_weight


def _m3_weight(
    *,
    snapshot: SignalSnapshot,
    reason_codes: list[str],
) -> float:
    regime_context = snapshot.regime_context
    match_score = _first_float(
        regime_context.get("m3_match_score"),
        regime_context.get("m3_similarity"),
        regime_context.get("pattern_memory_similarity"),
    )
    if match_score is None or match_score <= 0.0:
        return 1.0
    normalized = _clamp(match_score, 0.0, 1.0)
    reason_codes.append("m3_pattern_memory")
    return 1.0 + 0.25 * normalized


def _m7_weight(
    *,
    snapshot: SignalSnapshot,
    reason_codes: list[str],
) -> float:
    news_context = snapshot.news_context
    effectiveness = _first_float(
        news_context.get("m7_effectiveness_score"),
        news_context.get("event_effectiveness_score"),
    )
    source_reliability = _first_float(
        news_context.get("m7_source_reliability"),
        news_context.get("source_reliability_score"),
    )
    news_component = _first_float(news_context.get("news_component"))

    weight = 1.0
    if effectiveness is not None:
        weight *= 0.90 + 0.40 * _clamp(effectiveness, 0.0, 1.0)
        reason_codes.append("m7_effectiveness")
    if source_reliability is not None:
        weight *= 0.95 + 0.20 * _clamp(source_reliability, 0.0, 1.0)
        reason_codes.append("m7_source_reliability")
    elif news_component is not None:
        strength = abs(_clamp(news_component, 0.0, 1.0) - 0.5) * 2.0
        weight *= 1.0 + 0.15 * strength
        if strength > 0.0:
            reason_codes.append("m7_news_component")
    return weight


def _derive_m1_bucket_from_outcome(*, outcome: OutcomeRecord) -> str:
    realized_return = float(outcome.realized_return) if outcome.realized_return is not None else 0.0
    adverse = (
        float(outcome.max_adverse_excursion)
        if outcome.max_adverse_excursion is not None
        else realized_return
    )
    stress = min(realized_return, adverse)
    if stress <= -0.10:
        return "severe"
    if stress <= -0.05:
        return "medium"
    if stress < 0.0:
        return "mild"
    return ""


def _first_float(*values: object) -> float | None:
    for value in values:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    return ordered


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))
