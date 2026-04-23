"""M11 shadow-portfolio redline guard with lightweight attribution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from stock_analyzer.evolution.modules.m11_shadow_loader import (
    M11ShadowObservation,
    parse_m11_shadow_records,
)


@dataclass(frozen=True, slots=True)
class M11ShadowMetrics:
    """M11 summary metrics."""

    valid_samples: int
    champion_cum_return: float
    challenger_cum_return: float
    champion_max_drawdown: float
    challenger_max_drawdown: float
    drawdown_delta: float
    tail_loss_delta: float
    execution_divergence_ratio: float
    champion_win_rate: float
    challenger_win_rate: float
    mean_return_diff: float


@dataclass(frozen=True, slots=True)
class M11AttributionItem:
    """One M11 attribution item."""

    name: str
    value: float
    threshold: float
    breached: bool
    impact: float


@dataclass(frozen=True, slots=True)
class M11ShadowResult:
    """M11 result payload."""

    score: float
    status: str
    redlines: dict[str, bool]
    metrics: M11ShadowMetrics
    attribution: list[M11AttributionItem]


def evaluate_m11_shadow_portfolio(
    records: Sequence[Mapping[str, object]] | None = None,
    *,
    shadow_observations: Sequence[M11ShadowObservation] | None = None,
    drawdown_delta_limit: float = 0.05,
    tail_loss_delta_limit: float = 0.03,
    execution_divergence_limit: float = 0.35,
) -> M11ShadowResult:
    """Evaluate M11 redline state from shadow return/signal observations.

    Data can come from either:
    - ``shadow_observations`` from independent shadow-result artifacts, or
    - ``records`` that carry inline shadow fields.

    Args:
        records: Optional raw records with inline shadow fields.
        shadow_observations: Optional preloaded normalized shadow observations.
        drawdown_delta_limit: Drawdown delta redline threshold.
        tail_loss_delta_limit: Tail-loss delta redline threshold.
        execution_divergence_limit: Execution divergence redline threshold.

    Returns:
        M11 redline assessment result.
    """
    raw_records: Sequence[Mapping[str, object]] = records if records is not None else []
    observations = (
        list(shadow_observations)
        if shadow_observations is not None
        else parse_m11_shadow_records(records=raw_records)
    )
    champion_returns: list[float] = []
    challenger_returns: list[float] = []
    signal_divergence_flags: list[int] = []

    for observation in observations:
        champion_returns.append(observation.champion_shadow_return)
        challenger_returns.append(observation.challenger_shadow_return)
        champion_signal = observation.champion_signal
        challenger_signal = observation.challenger_signal
        if champion_signal is not None and challenger_signal is not None:
            signal_divergence_flags.append(1 if champion_signal != challenger_signal else 0)

    valid_samples = min(len(champion_returns), len(challenger_returns))
    if valid_samples == 0:
        metrics = M11ShadowMetrics(
            valid_samples=0,
            champion_cum_return=0.0,
            challenger_cum_return=0.0,
            champion_max_drawdown=0.0,
            challenger_max_drawdown=0.0,
            drawdown_delta=0.0,
            tail_loss_delta=0.0,
            execution_divergence_ratio=0.0,
            champion_win_rate=0.0,
            challenger_win_rate=0.0,
            mean_return_diff=0.0,
        )
        return M11ShadowResult(
            score=50.0,
            status="no_data",
            redlines={
                "drawdown_delta": False,
                "tail_loss_delta": False,
                "execution_divergence": False,
            },
            metrics=metrics,
            attribution=[],
        )

    champion_arr = np.asarray(champion_returns, dtype=float)
    challenger_arr = np.asarray(challenger_returns, dtype=float)
    diff_arr = challenger_arr - champion_arr

    champion_cum_return = float(np.prod(1.0 + champion_arr) - 1.0)
    challenger_cum_return = float(np.prod(1.0 + challenger_arr) - 1.0)
    champion_drawdown = _max_drawdown(champion_arr)
    challenger_drawdown = _max_drawdown(challenger_arr)
    drawdown_delta = max(0.0, challenger_drawdown - champion_drawdown)
    champion_tail = abs(float(np.percentile(champion_arr, 10)))
    challenger_tail = abs(float(np.percentile(challenger_arr, 10)))
    tail_loss_delta = max(0.0, challenger_tail - champion_tail)
    execution_divergence_ratio = (
        float(np.mean(np.asarray(signal_divergence_flags, dtype=float)))
        if signal_divergence_flags
        else 0.0
    )
    champion_win_rate = float(np.mean((champion_arr > 0).astype(float)))
    challenger_win_rate = float(np.mean((challenger_arr > 0).astype(float)))
    mean_return_diff = float(np.mean(diff_arr))

    redlines = {
        "drawdown_delta": drawdown_delta > max(drawdown_delta_limit, 1e-9),
        "tail_loss_delta": tail_loss_delta > max(tail_loss_delta_limit, 1e-9),
        "execution_divergence": execution_divergence_ratio > max(execution_divergence_limit, 1e-9),
    }
    any_redline = any(redlines.values())

    drawdown_penalty = min(35.0, drawdown_delta / max(drawdown_delta_limit, 1e-9) * 25.0)
    tail_penalty = min(30.0, tail_loss_delta / max(tail_loss_delta_limit, 1e-9) * 20.0)
    divergence_penalty = min(
        35.0,
        execution_divergence_ratio / max(execution_divergence_limit, 1e-9) * 25.0,
    )
    score = _clamp100(92.0 - drawdown_penalty - tail_penalty - divergence_penalty)
    status = "redline_breach" if any_redline else "stable"

    metrics = M11ShadowMetrics(
        valid_samples=valid_samples,
        champion_cum_return=champion_cum_return,
        challenger_cum_return=challenger_cum_return,
        champion_max_drawdown=champion_drawdown,
        challenger_max_drawdown=challenger_drawdown,
        drawdown_delta=drawdown_delta,
        tail_loss_delta=tail_loss_delta,
        execution_divergence_ratio=execution_divergence_ratio,
        champion_win_rate=champion_win_rate,
        challenger_win_rate=challenger_win_rate,
        mean_return_diff=mean_return_diff,
    )
    attribution = [
        M11AttributionItem(
            name="drawdown_delta",
            value=drawdown_delta,
            threshold=drawdown_delta_limit,
            breached=redlines["drawdown_delta"],
            impact=min(1.0, drawdown_delta / max(drawdown_delta_limit, 1e-9)),
        ),
        M11AttributionItem(
            name="tail_loss_delta",
            value=tail_loss_delta,
            threshold=tail_loss_delta_limit,
            breached=redlines["tail_loss_delta"],
            impact=min(1.0, tail_loss_delta / max(tail_loss_delta_limit, 1e-9)),
        ),
        M11AttributionItem(
            name="execution_divergence",
            value=execution_divergence_ratio,
            threshold=execution_divergence_limit,
            breached=redlines["execution_divergence"],
            impact=min(1.0, execution_divergence_ratio / max(execution_divergence_limit, 1e-9)),
        ),
    ]
    return M11ShadowResult(
        score=score,
        status=status,
        redlines=redlines,
        metrics=metrics,
        attribution=attribution,
    )

def _max_drawdown(returns: NDArray[np.float64]) -> float:
    if returns.size == 0:
        return 0.0
    equity = np.cumprod(1.0 + returns)
    running_max = np.maximum.accumulate(equity)
    drawdowns = 1.0 - equity / np.maximum(running_max, 1e-12)
    return float(np.max(drawdowns))


def _clamp100(value: float) -> float:
    return max(0.0, min(100.0, value))
