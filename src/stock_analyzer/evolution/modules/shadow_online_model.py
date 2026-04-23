"""Shadow online learner for off-hours evolution reporting."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass(slots=True)
class ShadowOnlineMetrics:
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


@dataclass(slots=True)
class ShadowOnlineResult:
    status: str
    engine: str
    shadow_mode: bool
    affects_main_model: bool
    samples_considered: int
    samples_used: int
    metrics: ShadowOnlineMetrics
    reasons: list[str]
    preview: list[dict[str, object]]
    state: dict[str, object]


def run_shadow_online_model(
    *,
    records: Sequence[Mapping[str, object]],
    now: datetime,
    previous_state: Mapping[str, object] | None = None,
    max_samples: int,
    min_samples: int,
    learning_rate: float,
    preview_limit: int = 5,
) -> ShadowOnlineResult:
    mature_records = _resolve_mature_records(records=records, now=now)
    samples_considered = len(mature_records)
    reasons: list[str] = []
    if samples_considered == 0:
        reasons.append("no_matured_samples")
        return ShadowOnlineResult(
            status="idle",
            engine="river_compatible_stub_logistic_v1",
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
        return ShadowOnlineResult(
            status="insufficient_samples",
            engine="river_compatible_stub_logistic_v1",
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
    raw_weights = state.get("weights", {})
    weights = (
        {
            key: float(value)
            for key, value in raw_weights.items()
            if isinstance(key, str) and isinstance(value, (int, float))
        }
        if isinstance(raw_weights, Mapping)
        else {}
    )
    bias = _as_float(state.get("bias"), default=0.0)
    valid_samples = 0
    shadow_losses: list[float] = []
    baseline_losses: list[float] = []
    shadow_squared: list[float] = []
    baseline_squared: list[float] = []
    shadow_hits = 0
    baseline_hits = 0
    shadow_probabilities: list[float] = []
    baseline_probabilities: list[float] = []
    preview: list[dict[str, object]] = []

    for item in usable_records:
        features = _extract_features(item)
        label = _extract_label(item)
        if features is None or label is None:
            continue
        valid_samples += 1
        baseline_probability = _baseline_probability(item)
        shadow_probability = _sigmoid(
            bias + sum(weights.get(name, 0.0) * value for name, value in features.items())
        )
        error = float(label) - shadow_probability
        for name, value in features.items():
            weights[name] = weights.get(name, 0.0) + learning_rate * error * value
        bias += learning_rate * error

        shadow_losses.append(_logloss(label, shadow_probability))
        baseline_losses.append(_logloss(label, baseline_probability))
        shadow_squared.append((shadow_probability - float(label)) ** 2)
        baseline_squared.append((baseline_probability - float(label)) ** 2)
        shadow_hits += int((shadow_probability >= 0.5) == bool(label))
        baseline_hits += int((baseline_probability >= 0.5) == bool(label))
        shadow_probabilities.append(shadow_probability)
        baseline_probabilities.append(baseline_probability)
        if len(preview) < max(1, int(preview_limit)):
            preview.append(
                {
                    "symbol": str(item.get("symbol", "")).strip(),
                    "label": label,
                    "baseline_probability": round(baseline_probability, 6),
                    "shadow_probability": round(shadow_probability, 6),
                    "delta_probability": round(shadow_probability - baseline_probability, 6),
                }
            )

    if valid_samples < max(1, int(min_samples)):
        reasons.append("valid_samples_below_threshold")
        return ShadowOnlineResult(
            status="insufficient_valid_samples",
            engine="river_compatible_stub_logistic_v1",
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
    metrics = ShadowOnlineMetrics(
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
    )
    comparison = _comparison_label(metrics)
    reasons.append(f"comparison:{comparison}")
    return ShadowOnlineResult(
        status="updated",
        engine="river_compatible_stub_logistic_v1",
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


def shadow_online_result_to_dict(result: ShadowOnlineResult) -> dict[str, object]:
    payload = asdict(result)
    payload["metrics"] = asdict(result.metrics)
    return payload


def _resolve_mature_records(
    *,
    records: Sequence[Mapping[str, object]],
    now: datetime,
) -> list[Mapping[str, object]]:
    ordered: list[tuple[str, str, str, Mapping[str, object]]] = []
    for item in records:
        symbol = str(item.get("symbol", "")).strip()
        if not symbol:
            continue
        label = _extract_label(item)
        if label is None:
            continue
        label_mature_time = str(item.get("label_mature_time", "")).strip()
        if label_mature_time:
            mature_dt = _parse_datetime(label_mature_time)
            if mature_dt is not None and mature_dt > _normalize_datetime(now):
                continue
        trade_date = str(item.get("trade_date", item.get("date", ""))).strip()
        ordered.append((label_mature_time, trade_date, symbol, item))
    ordered.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[-1] for item in ordered]


def _extract_features(record: Mapping[str, object]) -> dict[str, float] | None:
    open_px = _as_float(record.get("open"), default=0.0)
    high_px = _as_float(record.get("high"), default=0.0)
    low_px = _as_float(record.get("low"), default=0.0)
    close_px = _as_float(record.get("close"), default=0.0)
    volume = max(0.0, _as_float(record.get("volume"), default=0.0))
    if open_px <= 0 or close_px <= 0:
        return None
    candle_range = max(high_px - low_px, 1e-6)
    close_location = ((close_px - low_px) / candle_range) - 0.5 if high_px > low_px else 0.0
    return {
        "ret_oc": close_px / open_px - 1.0,
        "range_pct": candle_range / max(open_px, 1e-6),
        "close_location": close_location,
        "volume_log": math.log1p(volume) / 20.0,
        "baseline_gap": _baseline_probability(record) - 0.5,
    }


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
    direct = _as_float(record.get("p_meta"), default=-1.0)
    if 0.0 <= direct <= 1.0:
        return direct
    probs = [
        _as_float(record.get(key), default=-1.0)
        for key in ("p_lgbm", "p_xgb", "p_meta")
        if 0.0 <= _as_float(record.get(key), default=-1.0) <= 1.0
    ]
    if probs:
        return sum(probs) / len(probs)
    return 0.5


def _comparison_label(metrics: ShadowOnlineMetrics) -> str:
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
    }


def _normalize_state(previous_state: Mapping[str, object] | None) -> dict[str, object]:
    state = dict(previous_state or {})
    weights = state.get("weights")
    if not isinstance(weights, Mapping):
        state["weights"] = {}
    return state


def _empty_metrics(valid_samples: int = 0) -> ShadowOnlineMetrics:
    return ShadowOnlineMetrics(
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
