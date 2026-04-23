"""M6 counterparty pressure estimation from daily bar structure."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class M6CounterpartyMetrics:
    """M6 diagnostics describing intraday counterparty pressure."""

    pressure_index: float
    bearish_ratio: float
    rejection_ratio: float
    close_near_low_ratio: float
    valid_symbols: int


@dataclass(frozen=True, slots=True)
class M6CounterpartyResult:
    """M6 output summary with score and status."""

    score: float
    status: str
    metrics: M6CounterpartyMetrics


def evaluate_m6_counterparty(
    records: Sequence[Mapping[str, object]],
    sell_pressure_gate: float = 0.58,
    bearish_ratio_gate: float = 0.55,
    rejection_shadow_gate: float = 0.55,
) -> M6CounterpartyResult:
    """Evaluate M6 counterparty pressure based on OHLCV candles.

    Args:
        records: Input records with ``open/high/low/close`` fields.
        sell_pressure_gate: Pressure threshold to classify heavy sell pressure.
        bearish_ratio_gate: Bearish-candle ratio threshold for warning state.
        rejection_shadow_gate: Upper-shadow ratio threshold to count rejections.

    Returns:
        Counterparty result with score and metrics.
    """
    pressure_values: list[float] = []
    bearish_count = 0
    rejection_count = 0
    close_near_low_count = 0

    for record in records:
        open_px = _as_float(record.get("open"), default=0.0)
        high = _as_float(record.get("high"), default=0.0)
        low = _as_float(record.get("low"), default=0.0)
        close = _as_float(record.get("close"), default=0.0)
        candle_range = high - low
        if open_px <= 0.0 or close <= 0.0 or candle_range <= 1e-6:
            continue

        upper_shadow = max(0.0, high - max(open_px, close))
        upper_shadow_ratio = upper_shadow / candle_range
        close_position = (close - low) / candle_range

        pressure = 0.65 * upper_shadow_ratio + 0.35 * (1.0 - close_position)
        pressure_values.append(_clamp01(pressure))

        if close < open_px:
            bearish_count += 1
        if upper_shadow_ratio >= rejection_shadow_gate:
            rejection_count += 1
        if close_position <= 0.35:
            close_near_low_count += 1

    if not pressure_values:
        metrics = M6CounterpartyMetrics(
            pressure_index=0.0,
            bearish_ratio=0.0,
            rejection_ratio=0.0,
            close_near_low_ratio=0.0,
            valid_symbols=0,
        )
        return M6CounterpartyResult(score=50.0, status="no_data", metrics=metrics)

    valid = len(pressure_values)
    pressure_index = sum(pressure_values) / valid
    bearish_ratio = bearish_count / valid
    rejection_ratio = rejection_count / valid
    close_near_low_ratio = close_near_low_count / valid

    score = _clamp100(
        95.0
        - pressure_index * 40.0
        - bearish_ratio * 20.0
        - rejection_ratio * 15.0
        - close_near_low_ratio * 20.0
    )

    if pressure_index >= sell_pressure_gate and bearish_ratio >= bearish_ratio_gate:
        status = "heavy_sell_pressure"
    elif pressure_index <= max(0.0, sell_pressure_gate - 0.25) and bearish_ratio < 0.45:
        status = "favorable"
    else:
        status = "balanced"

    metrics = M6CounterpartyMetrics(
        pressure_index=pressure_index,
        bearish_ratio=bearish_ratio,
        rejection_ratio=rejection_ratio,
        close_near_low_ratio=close_near_low_ratio,
        valid_symbols=valid,
    )
    return M6CounterpartyResult(score=score, status=status, metrics=metrics)


def _as_float(value: object, default: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _clamp100(value: float) -> float:
    return max(0.0, min(100.0, value))
