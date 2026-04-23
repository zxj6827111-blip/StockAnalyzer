"""Richer shadow online learner for protocol-bound shadow datasets."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime

_EFFECTIVE_MATURE_STATUSES = {"reconciled", "fully_matured"}


@dataclass(slots=True)
class ShadowOnlineV2Metrics:
    valid_samples: int
    updates_applied: int
    shadow_logloss: float
    baseline_logloss: float
    delta_logloss: float
    shadow_brier: float
    baseline_brier: float
    delta_brier: float
    shadow_accuracy: float
    baseline_accuracy: float
    avg_shadow_probability: float
    avg_baseline_probability: float
    avg_sample_weight: float
    avg_execution_fill_ratio: float
    avg_realized_slippage_bp: float
    signal_divergence_ratio: float


@dataclass(slots=True)
class ShadowOnlineV2Result:
    status: str
    engine: str
    shadow_mode: bool
    affects_main_model: bool
    samples_considered: int
    samples_used: int
    metrics: ShadowOnlineV2Metrics
    reasons: list[str]
    preview: list[dict[str, object]]
    state: dict[str, object]


def run_shadow_online_model_v2(
    *,
    records: Sequence[Mapping[str, object]],
    now: datetime,
    previous_state: Mapping[str, object] | None = None,
    max_samples: int,
    min_samples: int,
    learning_rate: float,
    preview_limit: int = 5,
    signal_threshold: float = 0.5,
) -> ShadowOnlineV2Result:
    mature_records = _resolve_mature_records(records=records, now=now)
    samples_considered = len(mature_records)
    reasons: list[str] = []
    if samples_considered == 0:
        reasons.append("no_matured_samples")
        return ShadowOnlineV2Result(
            status="idle",
            engine="protocol_shadow_online_v2_lr",
            shadow_mode=True,
            affects_main_model=False,
            samples_considered=0,
            samples_used=0,
            metrics=_empty_metrics(),
            reasons=reasons,
            preview=[],
            state=_normalize_state(previous_state),
        )

    usable_records = mature_records[: max(1, int(max_samples))]
    if len(usable_records) < max(1, int(min_samples)):
        reasons.append("insufficient_matured_samples")
        return ShadowOnlineV2Result(
            status="insufficient_samples",
            engine="protocol_shadow_online_v2_lr",
            shadow_mode=True,
            affects_main_model=False,
            samples_considered=samples_considered,
            samples_used=len(usable_records),
            metrics=_empty_metrics(),
            reasons=reasons,
            preview=[],
            state=_normalize_state(previous_state),
        )

    state = _normalize_state(previous_state)
    weights = _normalize_weights(state.get("weights"))
    bias = _as_float(state.get("bias"), default=0.0)
    threshold = max(0.0, min(1.0, float(signal_threshold)))

    valid_samples = 0
    shadow_losses: list[float] = []
    baseline_losses: list[float] = []
    shadow_squared: list[float] = []
    baseline_squared: list[float] = []
    shadow_hits = 0
    baseline_hits = 0
    shadow_probabilities: list[float] = []
    baseline_probabilities: list[float] = []
    sample_weights_used: list[float] = []
    execution_fill_values: list[float] = []
    slippage_values: list[float] = []
    divergence_flags: list[float] = []
    preview: list[dict[str, object]] = []

    for item in usable_records:
        features = _extract_rich_features(item)
        label = _extract_label(item)
        if features is None or label is None:
            continue

        sample_weight = _resolve_sample_weight(item)
        update_rate = max(0.0, float(learning_rate)) * sample_weight
        baseline_probability = _baseline_probability(item)
        shadow_probability = _sigmoid(
            bias + sum(weights.get(name, 0.0) * value for name, value in features.items())
        )
        error = float(label) - shadow_probability
        for name, value in features.items():
            weights[name] = weights.get(name, 0.0) + update_rate * error * value
        bias += update_rate * error

        valid_samples += 1
        shadow_losses.append(_logloss(label, shadow_probability))
        baseline_losses.append(_logloss(label, baseline_probability))
        shadow_squared.append((shadow_probability - float(label)) ** 2)
        baseline_squared.append((baseline_probability - float(label)) ** 2)
        shadow_hits += int((shadow_probability >= threshold) == bool(label))
        baseline_hits += int((baseline_probability >= threshold) == bool(label))
        shadow_probabilities.append(shadow_probability)
        baseline_probabilities.append(baseline_probability)
        sample_weights_used.append(sample_weight)
        execution_fill_values.append(_execution_fill_ratio(item))
        slippage_values.append(_realized_slippage_bp(item))
        divergence_flags.append(1.0 if _signals_diverged(item) else 0.0)
        if len(preview) < max(1, int(preview_limit)):
            preview.append(
                {
                    "symbol": str(item.get("symbol", "")).strip(),
                    "label": label,
                    "baseline_probability": round(baseline_probability, 6),
                    "shadow_probability": round(shadow_probability, 6),
                    "delta_probability": round(shadow_probability - baseline_probability, 6),
                    "sample_weight": round(sample_weight, 6),
                    "realized_return": round(_realized_return(item), 6),
                    "execution_fill_ratio": round(_execution_fill_ratio(item), 6),
                }
            )

    if valid_samples < max(1, int(min_samples)):
        reasons.append("valid_samples_below_threshold")
        return ShadowOnlineV2Result(
            status="insufficient_valid_samples",
            engine="protocol_shadow_online_v2_lr",
            shadow_mode=True,
            affects_main_model=False,
            samples_considered=samples_considered,
            samples_used=valid_samples,
            metrics=_empty_metrics(valid_samples=valid_samples),
            reasons=reasons,
            preview=preview,
            state=_build_state(
                bias=bias,
                weights=weights,
                previous_state=state,
                updates_applied=valid_samples,
                now=now,
            ),
        )

    shadow_logloss = sum(shadow_losses) / max(valid_samples, 1)
    baseline_logloss = sum(baseline_losses) / max(valid_samples, 1)
    shadow_brier = sum(shadow_squared) / max(valid_samples, 1)
    baseline_brier = sum(baseline_squared) / max(valid_samples, 1)
    metrics = ShadowOnlineV2Metrics(
        valid_samples=valid_samples,
        updates_applied=valid_samples,
        shadow_logloss=round(shadow_logloss, 6),
        baseline_logloss=round(baseline_logloss, 6),
        delta_logloss=round(shadow_logloss - baseline_logloss, 6),
        shadow_brier=round(shadow_brier, 6),
        baseline_brier=round(baseline_brier, 6),
        delta_brier=round(shadow_brier - baseline_brier, 6),
        shadow_accuracy=round(shadow_hits / max(valid_samples, 1), 6),
        baseline_accuracy=round(baseline_hits / max(valid_samples, 1), 6),
        avg_shadow_probability=round(sum(shadow_probabilities) / max(valid_samples, 1), 6),
        avg_baseline_probability=round(sum(baseline_probabilities) / max(valid_samples, 1), 6),
        avg_sample_weight=round(sum(sample_weights_used) / max(valid_samples, 1), 6),
        avg_execution_fill_ratio=round(sum(execution_fill_values) / max(valid_samples, 1), 6),
        avg_realized_slippage_bp=round(sum(slippage_values) / max(valid_samples, 1), 6),
        signal_divergence_ratio=round(sum(divergence_flags) / max(valid_samples, 1), 6),
    )
    reasons.append(f"comparison:{_comparison_label(metrics)}")
    return ShadowOnlineV2Result(
        status="updated",
        engine="protocol_shadow_online_v2_lr",
        shadow_mode=True,
        affects_main_model=False,
        samples_considered=samples_considered,
        samples_used=valid_samples,
        metrics=metrics,
        reasons=reasons,
        preview=preview,
        state=_build_state(
            bias=bias,
            weights=weights,
            previous_state=state,
            updates_applied=valid_samples,
            now=now,
        ),
    )


def shadow_online_v2_result_to_dict(result: ShadowOnlineV2Result) -> dict[str, object]:
    payload = asdict(result)
    payload["metrics"] = asdict(result.metrics)
    return payload


def shadow_online_model_v2_features_from_record(record: Mapping[str, object]) -> dict[str, float]:
    return _extract_rich_features(record) or {}


def score_shadow_online_model_v2_record(
    *,
    record: Mapping[str, object],
    state: Mapping[str, object] | None,
) -> float:
    normalized_state = _normalize_state(state)
    weights = _normalize_weights(normalized_state.get("weights"))
    bias = _as_float(normalized_state.get("bias"), default=0.0)
    features = _extract_rich_features(record)
    if not features:
        return 0.5
    score = bias + sum(weights.get(name, 0.0) * value for name, value in features.items())
    return round(_sigmoid(score), 6)


def _resolve_mature_records(
    *,
    records: Sequence[Mapping[str, object]],
    now: datetime,
) -> list[Mapping[str, object]]:
    ordered: list[tuple[str, str, str, Mapping[str, object]]] = []
    normalized_now = _normalize_datetime(now)
    for item in records:
        symbol = str(item.get("symbol", "")).strip()
        if not symbol:
            continue
        label = _extract_label(item)
        if label is None:
            continue
        label_mature_time = str(item.get("label_mature_time", "")).strip()
        if not _record_is_effectively_mature(record=item, normalized_now=normalized_now):
            continue
        trade_date = str(item.get("trade_date", item.get("date", ""))).strip()
        ordered.append((label_mature_time, trade_date, symbol, item))
    ordered.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[-1] for item in ordered]


def _record_is_effectively_mature(
    *,
    record: Mapping[str, object],
    normalized_now: datetime,
) -> bool:
    maturity_status = str(record.get("maturity_status", "")).strip().lower()
    if maturity_status in _EFFECTIVE_MATURE_STATUSES:
        return True
    label_mature_time = str(record.get("label_mature_time", "")).strip()
    if not label_mature_time:
        return True
    mature_dt = _parse_datetime(label_mature_time)
    if mature_dt is None:
        return True
    return mature_dt <= normalized_now


def _extract_rich_features(record: Mapping[str, object]) -> dict[str, float] | None:
    features: dict[str, float] = {}

    shadow_scores = _score_mapping(record.get("shadow_scores")) or _score_mapping(record)
    champion_scores = _score_mapping(record.get("champion_scores"))
    shadow_meta = float(shadow_scores.get("p_meta", _as_float(record.get("p_meta"), default=0.5)))
    champion_meta = float(champion_scores.get("p_meta", 0.5))
    shadow_lgbm = float(shadow_scores.get("p_lgbm", shadow_meta))
    shadow_xgb = float(shadow_scores.get("p_xgb", shadow_meta))
    champion_lgbm = float(champion_scores.get("p_lgbm", champion_meta))
    champion_xgb = float(champion_scores.get("p_xgb", champion_meta))

    features["shadow_p_meta"] = shadow_meta - 0.5
    features["shadow_confidence"] = abs(shadow_meta - 0.5) * 2.0
    features["shadow_model_disagreement"] = abs(shadow_lgbm - shadow_xgb)
    features["champion_p_meta"] = champion_meta - 0.5
    features["champion_model_disagreement"] = abs(champion_lgbm - champion_xgb)
    features["p_meta_delta"] = shadow_meta - champion_meta
    features["p_lgbm_delta"] = shadow_lgbm - champion_lgbm
    features["p_xgb_delta"] = shadow_xgb - champion_xgb
    features["signal_diverged"] = 1.0 if _signals_diverged(record) else 0.0
    features["sample_weight"] = _resolve_sample_weight(record)
    features["data_quality_score"] = _as_float(record.get("data_quality_score"), default=1.0)
    features["execution_fill_ratio"] = _execution_fill_ratio(record)
    features["realized_slippage_bp"] = _realized_slippage_bp(record) / 50.0

    price_features = _extract_price_shape_features(record)
    features.update(price_features)
    features.update(_flatten_numeric_mapping(prefix="score", value=record.get("score_breakdown"), limit=4))
    features.update(_flatten_numeric_mapping(prefix="risk", value=record.get("risk_context"), limit=4))
    features.update(_flatten_numeric_mapping(prefix="regime", value=record.get("regime_context"), limit=4))

    normalized = {
        key: float(value)
        for key, value in features.items()
        if isinstance(key, str) and key.strip() and not math.isnan(float(value))
    }
    return normalized or None


def _extract_price_shape_features(record: Mapping[str, object]) -> dict[str, float]:
    open_px = _as_float(record.get("open"), default=0.0)
    high_px = _as_float(record.get("high"), default=0.0)
    low_px = _as_float(record.get("low"), default=0.0)
    close_px = _as_float(record.get("close"), default=0.0)
    volume = max(0.0, _as_float(record.get("volume"), default=0.0))
    if open_px <= 0.0 or close_px <= 0.0:
        return {}
    candle_range = max(high_px - low_px, 1e-6)
    close_location = ((close_px - low_px) / candle_range) - 0.5 if high_px > low_px else 0.0
    return {
        "ret_oc": close_px / open_px - 1.0,
        "range_pct": candle_range / max(open_px, 1e-6),
        "close_location": close_location,
        "volume_log": math.log1p(volume) / 20.0,
    }


def _flatten_numeric_mapping(
    *,
    prefix: str,
    value: object,
    limit: int,
) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    flattened: dict[str, float] = {}
    for key in sorted(value.keys(), key=lambda item: str(item))[: max(0, int(limit))]:
        normalized_key = str(key).strip().lower().replace(" ", "_")
        if not normalized_key:
            continue
        parsed = _numeric_mapping_value(value.get(key))
        if parsed is None:
            continue
        flattened[f"{prefix}:{normalized_key}"] = parsed
    return flattened


def _numeric_mapping_value(value: object) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "yes", "y", "on"}:
            return 1.0
        if text in {"false", "no", "n", "off"}:
            return 0.0
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _score_mapping(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    normalized: dict[str, float] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key).strip().lower()
        if key not in {"lgbm", "p_lgbm", "xgb", "p_xgb", "meta", "p_meta"}:
            continue
        parsed = _as_float(raw_value, default=-1.0)
        if 0.0 <= parsed <= 1.0:
            normalized["p_" + key.lstrip("p_")] = parsed
    return normalized


def _extract_label(record: Mapping[str, object]) -> int | None:
    raw = record.get("label")
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, (int, float)):
        return 1 if float(raw) >= 0.5 else 0
    if isinstance(raw, str):
        text = raw.strip()
        if text:
            try:
                return 1 if float(text) >= 0.5 else 0
            except ValueError:
                return None
    open_px = _as_float(record.get("open"), default=0.0)
    close_px = _as_float(record.get("close"), default=0.0)
    if open_px > 0 and close_px > 0:
        return 1 if close_px >= open_px else 0
    return None


def _baseline_probability(record: Mapping[str, object]) -> float:
    shadow_scores = _score_mapping(record.get("shadow_scores"))
    if shadow_scores:
        return float(shadow_scores.get("p_meta", 0.5))
    return _signal_probability(record)


def _signal_probability(record: Mapping[str, object]) -> float:
    direct = _as_float(record.get("p_meta"), default=-1.0)
    if 0.0 <= direct <= 1.0:
        return direct
    scores = _score_mapping(record)
    if scores:
        return float(scores.get("p_meta", 0.5))
    return 0.5


def _signals_diverged(record: Mapping[str, object]) -> bool:
    champion_signal = _as_int(record.get("champion_signal"), default=-1)
    shadow_signal = _as_int(record.get("shadow_signal"), default=-1)
    if champion_signal >= 0 and shadow_signal >= 0:
        return champion_signal != shadow_signal
    return bool(_as_float(record.get("signal_diverged"), default=0.0) >= 0.5)


def _resolve_sample_weight(record: Mapping[str, object]) -> float:
    sample_weight = max(0.0, _as_float(record.get("sample_weight"), default=1.0))
    data_quality = _as_float(record.get("data_quality_score"), default=1.0)
    data_quality = max(0.1, min(1.0, data_quality))
    resolved = sample_weight * data_quality
    return max(0.1, min(3.0, resolved))


def _execution_fill_ratio(record: Mapping[str, object]) -> float:
    return max(0.0, min(1.0, _as_float(record.get("execution_fill_ratio"), default=1.0)))


def _realized_slippage_bp(record: Mapping[str, object]) -> float:
    return max(0.0, _as_float(record.get("realized_slippage_bp"), default=0.0))


def _realized_return(record: Mapping[str, object]) -> float:
    return _as_float(record.get("realized_return"), default=0.0)


def _comparison_label(metrics: ShadowOnlineV2Metrics) -> str:
    if metrics.delta_logloss <= -0.005 and metrics.delta_brier <= -0.002:
        return "shadow_better"
    if metrics.delta_logloss >= 0.005 and metrics.delta_brier >= 0.002:
        return "shadow_worse"
    return "shadow_flat"


def _build_state(
    *,
    bias: float,
    weights: Mapping[str, float],
    previous_state: Mapping[str, object],
    updates_applied: int,
    now: datetime,
) -> dict[str, object]:
    previous_updates = _as_int(previous_state.get("cumulative_updates"), default=0)
    return {
        "bias": round(bias, 8),
        "weights": {key: round(float(value), 8) for key, value in sorted(weights.items())},
        "cumulative_updates": previous_updates + max(0, int(updates_applied)),
        "last_updated_at": now.isoformat(),
        "feature_names": sorted(weights),
        "engine": "protocol_shadow_online_v2_lr",
    }


def _normalize_state(previous_state: Mapping[str, object] | None) -> dict[str, object]:
    state = dict(previous_state or {})
    state["weights"] = _normalize_weights(state.get("weights"))
    return state


def _normalize_weights(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): float(item)
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, (int, float))
    }


def _empty_metrics(valid_samples: int = 0) -> ShadowOnlineV2Metrics:
    return ShadowOnlineV2Metrics(
        valid_samples=valid_samples,
        updates_applied=0,
        shadow_logloss=0.0,
        baseline_logloss=0.0,
        delta_logloss=0.0,
        shadow_brier=0.0,
        baseline_brier=0.0,
        delta_brier=0.0,
        shadow_accuracy=0.0,
        baseline_accuracy=0.0,
        avg_shadow_probability=0.0,
        avg_baseline_probability=0.0,
        avg_sample_weight=0.0,
        avg_execution_fill_ratio=0.0,
        avg_realized_slippage_bp=0.0,
        signal_divergence_ratio=0.0,
    )


def _sigmoid(value: float) -> float:
    clipped = max(-30.0, min(30.0, value))
    return 1.0 / (1.0 + math.exp(-clipped))


def _logloss(label: int, probability: float) -> float:
    clipped = max(1e-6, min(1.0 - 1e-6, probability))
    if label >= 1:
        return -math.log(clipped)
    return -math.log(1.0 - clipped)


def _parse_datetime(raw: str) -> datetime | None:
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _normalize_datetime(parsed)


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(value.tzinfo).replace(tzinfo=None)


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return int(float(text))
        except ValueError:
            return default
    return default


def _as_float(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return float(text)
        except ValueError:
            return default
    return default
