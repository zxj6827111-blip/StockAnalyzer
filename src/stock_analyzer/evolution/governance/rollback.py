"""Rollback policy evaluation for challenger promotion."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import numpy as np

from stock_analyzer._pydantic_compat import BaseModel, ConfigDict, Field


class RollbackState(StrEnum):
    """Rollback state for governance workflow."""

    STABLE = "stable"
    SOFT_WARNING = "soft_warning"
    PENDING_CONFIRMATION = "pending_confirmation"
    ROLLED_BACK = "rolled_back"


class RollbackPolicy(BaseModel):
    """Configurable rollback policy."""

    model_config = ConfigDict(extra="forbid")

    observation_window: int = Field(default=10, ge=1)
    min_trades: int = Field(default=15, ge=1)
    max_extension_days: int = Field(default=30, ge=1)
    soft_warning_z: float = -1.5
    soft_warning_days: int = Field(default=3, ge=1)
    hard_trigger_z: float = -2.0
    hard_trigger_days: int = Field(default=5, ge=1)
    pending_confirmation_ttl_days: int = Field(default=3, ge=1)
    post_rollback_actions: list[str] = Field(
        default_factory=lambda: [
            "purge challenger tasks",
            "invalidate score fusion cache",
            "update compliance state to rolled_back",
        ]
    )


class RollbackContext(BaseModel):
    """Runtime context required for rollback evaluation."""

    model_config = ConfigDict(extra="forbid")

    trade_count: int = Field(default=0, ge=0)
    observed_days: int = Field(default=0, ge=0)
    consecutive_soft_days: int = Field(default=0, ge=0)
    consecutive_hard_days: int = Field(default=0, ge=0)
    pending_confirmation_since: datetime | None = None
    manual_confirmation_received: bool = False


class RollbackAssessment(BaseModel):
    """Rollback decision output."""

    model_config = ConfigDict(extra="forbid")

    state: RollbackState
    z_score: float
    std_floor: float
    reason: str
    post_rollback_actions: list[str] = Field(default_factory=list)


def tracking_error_z_score(
    diff_returns: Sequence[float],
    shadow_champion_vol: float,
) -> tuple[float, float]:
    """Compute tracking error Z-score with dynamic std floor.

    Formula:
    ``Z = mean(diff) / max(std(diff), std_floor)``
    with ``std_floor = max(0.001, 0.1 * shadow_champion_vol)``.

    Args:
        diff_returns: Daily return differences (challenger - champion).
        shadow_champion_vol: Champion daily volatility during shadow period.

    Returns:
        Tuple of ``(z_score, std_floor)``.

    Raises:
        ValueError: If diff_returns is empty.
    """
    sample = np.asarray(list(diff_returns), dtype=float)
    if sample.ndim != 1 or sample.size == 0:
        raise ValueError("diff_returns must be a non-empty one-dimensional sequence")
    if not np.isfinite(sample).all():
        raise ValueError("diff_returns must contain only finite values")

    mean_diff = float(np.mean(sample))
    std_diff = float(np.std(sample))
    std_floor = max(0.001, 0.1 * max(float(shadow_champion_vol), 0.0))
    z_score = mean_diff / max(std_diff, std_floor)
    return z_score, std_floor


def evaluate_rollback(
    diff_returns: Sequence[float],
    shadow_champion_vol: float,
    context: RollbackContext,
    policy: RollbackPolicy | None = None,
    now: datetime | None = None,
    hard_drawdown_breach: bool = False,
    tail_loss_triggered: bool = False,
) -> RollbackAssessment:
    """Evaluate rollback state under the configured policy.

    Args:
        diff_returns: Daily return differences (challenger - champion).
        shadow_champion_vol: Champion volatility used for dynamic std floor.
        context: Runtime counters and pending confirmation metadata.
        policy: Optional override policy.
        now: Evaluation timestamp. UTC now is used if omitted.
        hard_drawdown_breach: Whether hard drawdown delta threshold is breached.
        tail_loss_triggered: Whether tail-loss trigger fired.

    Returns:
        Rollback assessment result.
    """
    active_policy = policy or RollbackPolicy()
    ts_now = now or datetime.now(UTC)
    z_score, std_floor = tracking_error_z_score(
        diff_returns=diff_returns,
        shadow_champion_vol=shadow_champion_vol,
    )

    if hard_drawdown_breach or tail_loss_triggered:
        return RollbackAssessment(
            state=RollbackState.ROLLED_BACK,
            z_score=z_score,
            std_floor=std_floor,
            reason="hard_circuit_breaker",
            post_rollback_actions=list(active_policy.post_rollback_actions),
        )

    if (
        context.trade_count < active_policy.min_trades
        and context.observed_days < active_policy.observation_window
        and context.observed_days < active_policy.max_extension_days
    ):
        return RollbackAssessment(
            state=RollbackState.STABLE,
            z_score=z_score,
            std_floor=std_floor,
            reason="low_frequency_extension",
        )

    hard_condition = (
        z_score < active_policy.hard_trigger_z
        and context.consecutive_hard_days >= active_policy.hard_trigger_days
    )
    if hard_condition:
        if context.manual_confirmation_received:
            return RollbackAssessment(
                state=RollbackState.ROLLED_BACK,
                z_score=z_score,
                std_floor=std_floor,
                reason="manual_confirmation",
                post_rollback_actions=list(active_policy.post_rollback_actions),
            )

        if context.pending_confirmation_since is None:
            return RollbackAssessment(
                state=RollbackState.PENDING_CONFIRMATION,
                z_score=z_score,
                std_floor=std_floor,
                reason="hard_trigger_waiting_confirmation",
            )

        ttl = timedelta(days=active_policy.pending_confirmation_ttl_days)
        if ts_now - context.pending_confirmation_since >= ttl:
            return RollbackAssessment(
                state=RollbackState.ROLLED_BACK,
                z_score=z_score,
                std_floor=std_floor,
                reason="timeout_no_ack",
                post_rollback_actions=list(active_policy.post_rollback_actions),
            )
        return RollbackAssessment(
            state=RollbackState.PENDING_CONFIRMATION,
            z_score=z_score,
            std_floor=std_floor,
            reason="pending_confirmation_ttl_active",
        )

    soft_condition = (
        z_score < active_policy.soft_warning_z
        and context.consecutive_soft_days >= active_policy.soft_warning_days
    )
    if soft_condition:
        return RollbackAssessment(
            state=RollbackState.SOFT_WARNING,
            z_score=z_score,
            std_floor=std_floor,
            reason="soft_warning_threshold",
        )

    return RollbackAssessment(
        state=RollbackState.STABLE,
        z_score=z_score,
        std_floor=std_floor,
        reason="within_tolerance",
    )
