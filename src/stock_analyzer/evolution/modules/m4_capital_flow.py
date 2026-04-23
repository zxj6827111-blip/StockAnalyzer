"""M4 capital flow scoring using a lightweight turnover proxy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class M4FlowMetrics:
    """Capital flow diagnostics for one evaluation window."""

    net_flow_ratio: float
    breadth_ratio: float
    concentration: float
    inflow_symbols: int
    outflow_symbols: int
    valid_symbols: int
    estimated_turnover: float
    positive_flow: float
    negative_flow_abs: float


@dataclass(frozen=True, slots=True)
class M4CapitalFlowResult:
    """M4 output summary with score and interpretable status."""

    score: float
    status: str
    metrics: M4FlowMetrics


def evaluate_m4_capital_flow(
    records: Sequence[Mapping[str, object]],
    inflow_ratio_gate: float = 0.02,
    concentration_warn: float = 0.45,
    score_concentration_penalty: float = 12.0,
) -> M4CapitalFlowResult:
    """Evaluate M4 capital flow from bar-like market records.

    A simple proxy is used because L2 order-flow is unavailable in framework phase:
    ``signed_flow = ((close - open) / open) * (close * volume)``.

    Args:
        records: Input records with at least ``open``, ``close`` and ``volume`` fields.
        inflow_ratio_gate: Threshold to classify inflow/outflow dominant regimes.
        concentration_warn: Concentration threshold where score penalty starts.
        score_concentration_penalty: Max score penalty when concentration reaches 1.0.

    Returns:
        M4 result with normalized score and diagnostic metrics.
    """
    flows: list[float] = []
    turnovers: list[float] = []

    for record in records:
        open_px = _as_float(record.get("open"), default=0.0)
        close_px = _as_float(record.get("close"), default=0.0)
        volume = _as_float(record.get("volume"), default=0.0)
        if open_px <= 0.0 or close_px <= 0.0 or volume <= 0.0:
            continue

        turnover = close_px * volume
        signed_return = (close_px - open_px) / max(open_px, 1e-6)
        flows.append(signed_return * turnover)
        turnovers.append(turnover)

    if not flows:
        metrics = M4FlowMetrics(
            net_flow_ratio=0.0,
            breadth_ratio=0.0,
            concentration=0.0,
            inflow_symbols=0,
            outflow_symbols=0,
            valid_symbols=0,
            estimated_turnover=0.0,
            positive_flow=0.0,
            negative_flow_abs=0.0,
        )
        return M4CapitalFlowResult(score=50.0, status="no_data", metrics=metrics)

    positive_flow = sum(value for value in flows if value > 0.0)
    negative_flow_abs = abs(sum(value for value in flows if value < 0.0))
    total_abs = positive_flow + negative_flow_abs
    net_flow_ratio = (positive_flow - negative_flow_abs) / max(total_abs, 1e-6)

    inflow_symbols = sum(1 for value in flows if value > 0.0)
    outflow_symbols = sum(1 for value in flows if value < 0.0)
    valid_symbols = len(flows)
    breadth_ratio = (inflow_symbols - outflow_symbols) / max(valid_symbols, 1)

    concentration = max(abs(value) for value in flows) / max(total_abs, 1e-6)
    concentration_penalty = max(
        0.0, (concentration - concentration_warn) / max(1.0 - concentration_warn, 1e-6)
    ) * score_concentration_penalty
    score = _clamp(50.0 + net_flow_ratio * 35.0 + breadth_ratio * 15.0 - concentration_penalty)

    if net_flow_ratio >= inflow_ratio_gate:
        status = "inflow_dominant"
    elif net_flow_ratio <= -inflow_ratio_gate:
        status = "outflow_dominant"
    else:
        status = "balanced"

    metrics = M4FlowMetrics(
        net_flow_ratio=net_flow_ratio,
        breadth_ratio=breadth_ratio,
        concentration=concentration,
        inflow_symbols=inflow_symbols,
        outflow_symbols=outflow_symbols,
        valid_symbols=valid_symbols,
        estimated_turnover=sum(turnovers),
        positive_flow=positive_flow,
        negative_flow_abs=negative_flow_abs,
    )
    return M4CapitalFlowResult(score=score, status=status, metrics=metrics)


def _as_float(value: object, default: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))
