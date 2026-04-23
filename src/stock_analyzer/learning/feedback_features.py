"""Stable lp_* feature columns derived from learning feedback contexts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pandas as pd

LEARNING_PROTOCOL_FEEDBACK_FEATURE_COLUMNS: tuple[str, ...] = (
    "lp_m1_negative_case_applied",
    "lp_m1_negative_case_bucket_mild",
    "lp_m1_negative_case_bucket_medium",
    "lp_m1_negative_case_bucket_severe",
    "lp_m1_negative_case_similarity",
    "lp_m1_negative_case_penalty",
    "lp_m1_negative_case_reason_count",
    "lp_m3_match_score",
    "lp_m3_gate_pass_ratio",
    "lp_m7_effectiveness_score",
    "lp_m7_source_reliability",
    "lp_m7_mean_sentiment",
    "lp_m7_mean_confidence",
    "lp_m7_news_count",
)

_M1_BUCKET_COLUMNS = {
    "mild": "lp_m1_negative_case_bucket_mild",
    "medium": "lp_m1_negative_case_bucket_medium",
    "severe": "lp_m1_negative_case_bucket_severe",
}


def ensure_feedback_feature_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Append the stable lp_* feedback feature columns to one feature frame."""

    working = frame.copy()
    for column in LEARNING_PROTOCOL_FEEDBACK_FEATURE_COLUMNS:
        if column in working.columns:
            working[column] = (
                pd.to_numeric(working[column], errors="coerce").fillna(0.0).astype(float)
            )
            continue
        working[column] = pd.Series(0.0, index=working.index, dtype="float64")
    return working


def build_feedback_feature_vector(
    *,
    risk_context: Mapping[str, object] | None = None,
    news_context: Mapping[str, object] | None = None,
    regime_context: Mapping[str, object] | None = None,
) -> dict[str, float]:
    """Resolve one stable numeric lp_* feature payload from JSON contexts."""

    risk = risk_context or {}
    news = news_context or {}
    regime = regime_context or {}
    feature_vector = {column: 0.0 for column in LEARNING_PROTOCOL_FEEDBACK_FEATURE_COLUMNS}

    bucket = str(
        risk.get("m1_negative_case_bucket", risk.get("negative_case_bucket", ""))
    ).strip().lower()
    if bucket in _M1_BUCKET_COLUMNS:
        feature_vector[_M1_BUCKET_COLUMNS[bucket]] = 1.0

    similarity = _first_float(
        risk.get("m1_negative_case_similarity"),
        risk.get("m1_similarity"),
    )
    penalty = _first_float(risk.get("m1_negative_case_penalty"))
    reason_count = _string_count(
        risk.get("m1_reason_codes", risk.get("reason_codes", []))
    )
    applied = _coerce_bool(risk.get("m1_negative_case_applied")) or any(
        [
            bucket in _M1_BUCKET_COLUMNS,
            similarity is not None and similarity > 0.0,
            penalty is not None and penalty > 0.0,
            reason_count > 0,
        ]
    )
    feature_vector["lp_m1_negative_case_applied"] = 1.0 if applied else 0.0
    feature_vector["lp_m1_negative_case_similarity"] = _clamp(similarity or 0.0, 0.0, 1.0)
    feature_vector["lp_m1_negative_case_penalty"] = max(0.0, penalty or 0.0)
    feature_vector["lp_m1_negative_case_reason_count"] = float(reason_count)

    match_score = _first_float(
        regime.get("m3_match_score"),
        regime.get("m3_similarity"),
        regime.get("pattern_memory_similarity"),
    )
    gate_pass_ratio = _first_float(regime.get("m3_gate_pass_ratio"))
    if gate_pass_ratio is None:
        passed_gates = _first_float(regime.get("m3_passed_gates"))
        gate_total = _first_float(regime.get("m3_gate_total"))
        if passed_gates is not None and gate_total is not None and gate_total > 0:
            gate_pass_ratio = passed_gates / gate_total
    feature_vector["lp_m3_match_score"] = _clamp(match_score or 0.0, 0.0, 1.0)
    feature_vector["lp_m3_gate_pass_ratio"] = _clamp(gate_pass_ratio or 0.0, 0.0, 1.0)

    effectiveness = _first_float(
        news.get("m7_effectiveness_score"),
        news.get("event_effectiveness_score"),
    )
    source_reliability = _first_float(
        news.get("m7_source_reliability"),
        news.get("source_reliability_score"),
    )
    mean_sentiment = _first_float(news.get("m7_mean_sentiment"))
    mean_confidence = _first_float(news.get("m7_mean_confidence"))
    news_count = _first_float(news.get("m7_news_count"))
    feature_vector["lp_m7_effectiveness_score"] = _clamp(effectiveness or 0.0, 0.0, 1.0)
    feature_vector["lp_m7_source_reliability"] = _clamp(source_reliability or 0.0, 0.0, 1.0)
    feature_vector["lp_m7_mean_sentiment"] = _clamp(mean_sentiment or 0.0, -1.0, 1.0)
    feature_vector["lp_m7_mean_confidence"] = _clamp(mean_confidence or 0.0, 0.0, 1.0)
    feature_vector["lp_m7_news_count"] = max(0.0, float(news_count or 0.0))
    return feature_vector


def merge_feedback_feature_vector(
    feature_vector: Mapping[str, object],
    *,
    risk_context: Mapping[str, object] | None = None,
    news_context: Mapping[str, object] | None = None,
    regime_context: Mapping[str, object] | None = None,
    add_missing_columns: bool = True,
) -> dict[str, float]:
    """Merge lp_* feedback features into one existing numeric feature vector."""

    merged = {
        str(key).strip(): float(value)
        for key, value in feature_vector.items()
        if str(key).strip()
    }
    derived = build_feedback_feature_vector(
        risk_context=risk_context,
        news_context=news_context,
        regime_context=regime_context,
    )
    for column, value in derived.items():
        if add_missing_columns or column in merged:
            merged[column] = float(value)
    return merged


def has_feedback_feature_columns(feature_names: Mapping[str, object] | Sequence[str]) -> bool:
    """Return whether one feature contract already includes lp_* columns."""

    if isinstance(feature_names, Mapping):
        available = {str(key).strip() for key in feature_names.keys()}
    else:
        available = {str(item).strip() for item in feature_names}
    return any(column in available for column in LEARNING_PROTOCOL_FEEDBACK_FEATURE_COLUMNS)


def _first_float(*values: object) -> float | None:
    for value in values:
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                continue
            try:
                return float(text)
            except ValueError:
                continue
    return None


def _string_count(value: object) -> int:
    if not isinstance(value, list):
        return 0
    return len([item for item in value if str(item).strip()])


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value) != 0.0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))
