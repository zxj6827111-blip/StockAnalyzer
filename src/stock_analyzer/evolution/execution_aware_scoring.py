"""Shared execution-aware scoring helpers for reports and runtime reranking."""

from __future__ import annotations

from collections.abc import Mapping

from stock_analyzer.learning.execution_risk_labels import ExecutionRiskTarget

_SHORTLIST_WEIGHT = 0.70
_EXECUTION_AWARE_WEIGHT = 0.30
_HIGH_RISK_PENALTY = 0.08
_MODEL_OUTPUT_ALIASES = {
    "lgbm": "p_lgbm",
    "p_lgbm": "lgbm",
    "xgb": "p_xgb",
    "p_xgb": "xgb",
    "meta": "p_meta",
    "p_meta": "meta",
}


def execution_aware_score(
    *,
    base_probability: float,
    risk: Mapping[str, float],
) -> float:
    """Blend base win probability with ex-ante execution-risk probabilities."""

    can_fill = _clamp_prob(risk.get(ExecutionRiskTarget.CAN_FILL.value, 0.5))
    slippage_risk = _clamp_prob(risk.get(ExecutionRiskTarget.LIKELY_SLIPPAGE_HIGH.value, 0.5))
    divergence_risk = _clamp_prob(
        risk.get(ExecutionRiskTarget.SIM_BROKER_DIVERGENCE_RISK.value, 0.5)
    )
    return round(
        _clamp_prob(base_probability) * can_fill * (1.0 - slippage_risk) * (1.0 - divergence_risk),
        6,
    )


def is_high_execution_risk(risk: Mapping[str, float]) -> bool:
    return bool(
        _clamp_prob(risk.get(ExecutionRiskTarget.CAN_FILL.value, 0.5)) < 0.5
        or _clamp_prob(risk.get(ExecutionRiskTarget.LIKELY_SLIPPAGE_HIGH.value, 0.0)) >= 0.5
        or _clamp_prob(
            risk.get(ExecutionRiskTarget.SIM_BROKER_DIVERGENCE_RISK.value, 0.0)
        )
        >= 0.5
    )


def combine_execution_reranked_score(
    *,
    shortlist_score: float,
    execution_aware_score_value: float,
    high_execution_risk: bool,
) -> float:
    """Rescore one Week5 candidate while preserving shortlist context."""

    blended = (
        _SHORTLIST_WEIGHT * _clamp_prob(float(shortlist_score) / 100.0)
        + _EXECUTION_AWARE_WEIGHT * _clamp_prob(execution_aware_score_value)
        - (_HIGH_RISK_PENALTY if high_execution_risk else 0.0)
    )
    return round(100.0 * _clamp_prob(blended), 2)


def normalize_execution_risk_payload(payload: Mapping[str, float]) -> dict[str, float]:
    return {
        str(key): round(_clamp_prob(value), 6)
        for key, value in payload.items()
        if str(key).strip()
    }


def normalize_execution_model_outputs(
    model_outputs: Mapping[str, object] | None,
) -> dict[str, float]:
    """Return one numeric model-output mapping with common key aliases expanded."""

    normalized: dict[str, float] = {}
    if not isinstance(model_outputs, Mapping):
        return normalized
    for raw_key, raw_value in model_outputs.items():
        key = str(raw_key).strip()
        value = _as_float_or_none(raw_value)
        if not key or value is None:
            continue
        normalized[key] = value
        alias = _MODEL_OUTPUT_ALIASES.get(key.lower())
        if alias and alias not in normalized:
            normalized[alias] = value
    return normalized


def _as_float_or_none(value: object) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _clamp_prob(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
