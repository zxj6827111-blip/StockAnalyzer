"""M2 regime adaptation with four-state confidence/cooldown and Optuna-like tuning."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

_VALID_REGIME_STATES = {"trend_up", "trend_down", "range", "extreme"}


@dataclass(frozen=True, slots=True)
class RegimeObservation:
    """Features used for regime inference."""

    atr_ratio: float
    sector_dispersion: float
    turnover_zscore: float


@dataclass(frozen=True, slots=True)
class RegimeModelParams:
    """Tunable threshold bundle for four-state inference and cooldown switch."""

    extreme_atr_gate: float = 0.05
    extreme_turnover_z_gate: float = 2.5
    trend_turnover_z_gate: float = 0.8
    trend_dispersion_up_gate: float = 0.30
    trend_dispersion_down_gate: float = 0.25
    range_atr_scale: float = 0.06
    range_turnover_scale: float = 3.0
    switch_confidence_gate: float = 0.70
    switch_confirm_days: int = 2

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
        *,
        default: RegimeModelParams | None = None,
    ) -> RegimeModelParams:
        """Build model params from persisted payload.

        Args:
            payload: Raw mapping payload.
            default: Optional fallback params for missing values.

        Returns:
            Parsed and normalized parameter bundle.
        """
        base = default or cls()
        switch_days = _as_int(payload.get("switch_confirm_days"), default=base.switch_confirm_days)
        return cls(
            extreme_atr_gate=_as_float(
                payload.get("extreme_atr_gate"),
                default=base.extreme_atr_gate,
            ),
            extreme_turnover_z_gate=_as_float(
                payload.get("extreme_turnover_z_gate"),
                default=base.extreme_turnover_z_gate,
            ),
            trend_turnover_z_gate=_as_float(
                payload.get("trend_turnover_z_gate"),
                default=base.trend_turnover_z_gate,
            ),
            trend_dispersion_up_gate=_as_float(
                payload.get("trend_dispersion_up_gate"),
                default=base.trend_dispersion_up_gate,
            ),
            trend_dispersion_down_gate=_as_float(
                payload.get("trend_dispersion_down_gate"),
                default=base.trend_dispersion_down_gate,
            ),
            range_atr_scale=max(
                1e-6,
                _as_float(payload.get("range_atr_scale"), default=base.range_atr_scale),
            ),
            range_turnover_scale=max(
                1e-6,
                _as_float(payload.get("range_turnover_scale"), default=base.range_turnover_scale),
            ),
            switch_confidence_gate=_clamp(
                _as_float(
                    payload.get("switch_confidence_gate"),
                    default=base.switch_confidence_gate,
                ),
                0.0,
                1.0,
            ),
            switch_confirm_days=max(1, switch_days),
        )

    def to_dict(self) -> dict[str, object]:
        """Serialize parameter bundle to JSON-compatible mapping."""
        return {
            "extreme_atr_gate": self.extreme_atr_gate,
            "extreme_turnover_z_gate": self.extreme_turnover_z_gate,
            "trend_turnover_z_gate": self.trend_turnover_z_gate,
            "trend_dispersion_up_gate": self.trend_dispersion_up_gate,
            "trend_dispersion_down_gate": self.trend_dispersion_down_gate,
            "range_atr_scale": self.range_atr_scale,
            "range_turnover_scale": self.range_turnover_scale,
            "switch_confidence_gate": self.switch_confidence_gate,
            "switch_confirm_days": self.switch_confirm_days,
        }


@dataclass(frozen=True, slots=True)
class M2OptunaLikeConfig:
    """Configuration for lightweight Optuna-like random-search tuning."""

    n_trials: int = 48
    min_samples: int = 20
    min_improvement: float = 0.01
    random_seed: int = 42


@dataclass(frozen=True, slots=True)
class M2OptunaLikeResult:
    """Result payload for one Optuna-like tuning cycle."""

    backend: str
    tuned: bool
    reason: str
    sample_count: int
    trials: int
    baseline_score: float
    objective_score: float
    improvement: float
    params: RegimeModelParams
    tuned_at: str | None

    def to_dict(self) -> dict[str, object]:
        """Serialize tuning result into JSON-compatible mapping."""
        return {
            "backend": self.backend,
            "tuned": self.tuned,
            "reason": self.reason,
            "sample_count": self.sample_count,
            "trials": self.trials,
            "baseline_score": self.baseline_score,
            "objective_score": self.objective_score,
            "improvement": self.improvement,
            "params": self.params.to_dict(),
            "tuned_at": self.tuned_at,
        }


@dataclass(frozen=True, slots=True)
class RegimeInference:
    """Inference result for one observation."""

    state: str
    confidence: float
    confidence_tier: str


@dataclass(frozen=True, slots=True)
class RegimeSnapshot:
    """Controller snapshot after one update."""

    active_state: str
    switched: bool
    pending_state: str | None
    pending_days: int
    confidence: float
    confidence_tier: str


@dataclass(frozen=True, slots=True)
class M2RegimeResult:
    """M2 output summary with score."""

    score: float
    snapshot: RegimeSnapshot


class RegimeStateController:
    """State controller with confidence gate and cooldown switching.

    Switching rule:
    - Candidate switch is considered only when confidence >= ``switch_confidence_gate``.
    - The same candidate state must persist for ``switch_confirm_days`` consecutive updates.
    """

    def __init__(
        self,
        active_state: str = "range",
        *,
        params: RegimeModelParams | None = None,
    ) -> None:
        if active_state not in _VALID_REGIME_STATES:
            raise ValueError(f"unsupported active_state: {active_state}")
        self._active_state = active_state
        self._pending_state: str | None = None
        self._pending_days = 0
        self._params = params or RegimeModelParams()

    @property
    def params(self) -> RegimeModelParams:
        """Return active parameter bundle."""
        return self._params

    def set_params(self, params: RegimeModelParams) -> None:
        """Replace active parameter bundle."""
        self._params = params

    def update(self, observation: RegimeObservation) -> RegimeSnapshot:
        """Update controller with one observation and return state snapshot."""
        inference = infer_regime(observation=observation, params=self._params)
        switched = False
        confidence = inference.confidence

        if inference.state == self._active_state:
            self._pending_state = None
            self._pending_days = 0
        elif confidence >= self._params.switch_confidence_gate:
            if self._pending_state == inference.state:
                self._pending_days += 1
            else:
                self._pending_state = inference.state
                self._pending_days = 1

            if self._pending_days >= self._params.switch_confirm_days:
                self._active_state = inference.state
                self._pending_state = None
                self._pending_days = 0
                switched = True
        else:
            self._pending_state = None
            self._pending_days = 0

        return RegimeSnapshot(
            active_state=self._active_state,
            switched=switched,
            pending_state=self._pending_state,
            pending_days=self._pending_days,
            confidence=inference.confidence,
            confidence_tier=inference.confidence_tier,
        )

    def dump_state(self) -> dict[str, object]:
        """Serialize controller state for cross-process persistence.

        Returns:
            JSON-serializable controller state.
        """
        return {
            "active_state": self._active_state,
            "pending_state": self._pending_state,
            "pending_days": self._pending_days,
        }

    def dump_params(self) -> dict[str, object]:
        """Serialize active parameter bundle."""
        return self._params.to_dict()

    def load_state(self, payload: Mapping[str, object]) -> None:
        """Load controller state from persisted payload.

        Args:
            payload: Serialized state from ``dump_state``.

        Raises:
            ValueError: If payload contains invalid values.
        """
        raw_active = payload.get("active_state")
        active = raw_active if isinstance(raw_active, str) else self._active_state
        if active not in _VALID_REGIME_STATES:
            raise ValueError(f"unsupported active_state: {active}")

        raw_pending = payload.get("pending_state")
        pending: str | None
        if raw_pending is None:
            pending = None
        elif isinstance(raw_pending, str):
            if raw_pending not in _VALID_REGIME_STATES:
                raise ValueError(f"unsupported pending_state: {raw_pending}")
            pending = raw_pending
        else:
            raise ValueError("pending_state must be str | None")

        raw_pending_days = payload.get("pending_days", 0)
        if not isinstance(raw_pending_days, int):
            raise ValueError("pending_days must be int")
        if raw_pending_days < 0:
            raise ValueError("pending_days must be >= 0")

        self._active_state = active
        self._pending_state = pending
        self._pending_days = raw_pending_days

    def load_params(self, payload: Mapping[str, object]) -> None:
        """Load active parameters from persisted payload."""
        self._params = RegimeModelParams.from_mapping(payload, default=self._params)


def infer_regime(
    observation: RegimeObservation,
    *,
    params: RegimeModelParams | None = None,
) -> RegimeInference:
    """Infer one of four regime states from observation features.

    Args:
        observation: M2 input observation.
        params: Optional threshold bundle.

    Returns:
        Inferred regime state and confidence tier.
    """
    cfg = params or RegimeModelParams()
    atr = max(0.0, observation.atr_ratio)
    dispersion = max(0.0, observation.sector_dispersion)
    turnover_z = observation.turnover_zscore

    if atr > cfg.extreme_atr_gate or abs(turnover_z) > cfg.extreme_turnover_z_gate:
        state = "extreme"
        atr_scale = max(cfg.extreme_atr_gate * 1.6, 1e-6)
        turnover_scale = max(cfg.extreme_turnover_z_gate * 1.6, 1e-6)
        confidence = _clamp(max(atr / atr_scale, abs(turnover_z) / turnover_scale), 0.0, 1.0)
    elif turnover_z > cfg.trend_turnover_z_gate and dispersion < cfg.trend_dispersion_up_gate:
        state = "trend_up"
        turnover_component = (
            turnover_z - cfg.trend_turnover_z_gate * 0.75
        ) / max(cfg.trend_turnover_z_gate * 1.8, 1e-6)
        dispersion_component = cfg.trend_dispersion_up_gate - dispersion
        confidence = _clamp(turnover_component + dispersion_component, 0.0, 1.0)
    elif turnover_z < -cfg.trend_turnover_z_gate and dispersion > cfg.trend_dispersion_down_gate:
        state = "trend_down"
        turnover_component = (
            abs(turnover_z) - cfg.trend_turnover_z_gate * 0.75
        ) / max(cfg.trend_turnover_z_gate * 1.8, 1e-6)
        dispersion_component = dispersion - cfg.trend_dispersion_down_gate
        confidence = _clamp(turnover_component + dispersion_component, 0.0, 1.0)
    else:
        state = "range"
        confidence = _clamp(
            1.0 - atr / max(cfg.range_atr_scale, 1e-6) - abs(turnover_z) / cfg.range_turnover_scale,
            0.0,
            1.0,
        )

    return RegimeInference(
        state=state,
        confidence=confidence,
        confidence_tier=_confidence_tier(confidence),
    )


def tune_regime_with_optuna_like_search(
    observations: Sequence[RegimeObservation],
    *,
    baseline_params: RegimeModelParams | None = None,
    config: M2OptunaLikeConfig | None = None,
    now: datetime | None = None,
) -> M2OptunaLikeResult:
    """Tune M2 thresholds with Optuna-like random search.

    This function intentionally avoids heavy dependencies while keeping an
    Optuna-compatible workflow shape: trial sampling, objective evaluation,
    best-parameter selection, and minimum-improvement gating.

    Args:
        observations: Observation history used as objective sample.
        baseline_params: Optional current parameter bundle.
        config: Optional tuning config.
        now: Optional timestamp override.

    Returns:
        Tuning result payload with selected parameters and objective metrics.
    """
    active_config = config or M2OptunaLikeConfig()
    base = baseline_params or RegimeModelParams()
    sample_count = len(observations)
    baseline_score = _regime_objective_score(observations=observations, params=base)
    tuned_at = (now or datetime.now(UTC)).isoformat()

    if sample_count < active_config.min_samples:
        return M2OptunaLikeResult(
            backend="optuna_like_random_search",
            tuned=False,
            reason="insufficient_samples",
            sample_count=sample_count,
            trials=0,
            baseline_score=baseline_score,
            objective_score=baseline_score,
            improvement=0.0,
            params=base,
            tuned_at=None,
        )

    rng = np.random.default_rng(active_config.random_seed)
    best_params = base
    best_score = baseline_score

    for _ in range(max(1, active_config.n_trials)):
        sampled = _sample_trial_params(rng=rng, anchor=base)
        score = _regime_objective_score(observations=observations, params=sampled)
        if score > best_score:
            best_score = score
            best_params = sampled

    improvement = best_score - baseline_score
    tuned = improvement >= active_config.min_improvement
    selected = best_params if tuned else base
    reason = "tuned" if tuned else "no_material_improvement"
    return M2OptunaLikeResult(
        backend="optuna_like_random_search",
        tuned=tuned,
        reason=reason,
        sample_count=sample_count,
        trials=max(1, active_config.n_trials),
        baseline_score=baseline_score,
        objective_score=best_score,
        improvement=improvement,
        params=selected,
        tuned_at=tuned_at if tuned else None,
    )


def evaluate_m2_regime(
    controller: RegimeStateController,
    observation: RegimeObservation,
) -> M2RegimeResult:
    """Evaluate M2 state and convert it to module score."""
    snapshot = controller.update(observation=observation)
    base = {
        "trend_up": 78.0,
        "range": 68.0,
        "trend_down": 56.0,
        "extreme": 42.0,
    }.get(snapshot.active_state, 60.0)
    score = _clamp(base * (0.5 + 0.5 * snapshot.confidence), 0.0, 100.0)
    return M2RegimeResult(score=score, snapshot=snapshot)


def _regime_objective_score(
    observations: Sequence[RegimeObservation],
    params: RegimeModelParams,
) -> float:
    if not observations:
        return 0.0

    states: list[str] = []
    confidences: list[float] = []
    switch_count = 0
    controller = RegimeStateController(active_state="range", params=params)
    for observation in observations:
        inference = infer_regime(observation=observation, params=params)
        states.append(inference.state)
        confidences.append(inference.confidence)
        snapshot = controller.update(observation=observation)
        if snapshot.switched:
            switch_count += 1

    n_samples = len(observations)
    mean_confidence = float(np.mean(np.asarray(confidences, dtype=float)))
    confidence_array = np.asarray(confidences, dtype=float)
    low_confidence_ratio = float(np.mean((confidence_array < 0.40).astype(float)))
    switch_ratio = switch_count / max(n_samples - 1, 1)

    fractions = []
    for state in sorted(_VALID_REGIME_STATES):
        fractions.append(states.count(state) / n_samples)
    concentration = float(np.sum(np.square(np.asarray(fractions, dtype=float))))
    diversity = 1.0 - concentration
    extreme_ratio = states.count("extreme") / n_samples
    extreme_balance = 1.0 - min(1.0, abs(extreme_ratio - 0.10) / 0.30)

    objective = (
        0.45 * mean_confidence
        + 0.25 * diversity
        + 0.15 * (1.0 - low_confidence_ratio)
        + 0.10 * extreme_balance
        + 0.05 * (1.0 - switch_ratio)
    )
    return _clamp(objective, 0.0, 1.0)


def _sample_trial_params(
    *,
    rng: np.random.Generator,
    anchor: RegimeModelParams,
) -> RegimeModelParams:
    trend_turnover_gate = float(rng.uniform(0.55, 1.35))
    trend_dispersion_up_gate = float(rng.uniform(0.18, 0.36))
    trend_dispersion_down_gate = max(
        trend_dispersion_up_gate - 0.06,
        float(rng.uniform(0.20, 0.45)),
    )
    return RegimeModelParams(
        extreme_atr_gate=float(rng.uniform(0.035, 0.085)),
        extreme_turnover_z_gate=float(rng.uniform(1.8, 3.8)),
        trend_turnover_z_gate=trend_turnover_gate,
        trend_dispersion_up_gate=trend_dispersion_up_gate,
        trend_dispersion_down_gate=trend_dispersion_down_gate,
        range_atr_scale=float(rng.uniform(0.04, 0.09)),
        range_turnover_scale=float(rng.uniform(2.0, 4.2)),
        switch_confidence_gate=float(rng.uniform(0.60, 0.85)),
        switch_confirm_days=int(rng.integers(2, 4)),
    )


def _confidence_tier(confidence: float) -> str:
    if confidence > 0.70:
        return "high"
    if confidence >= 0.50:
        return "medium"
    if confidence >= 0.40:
        return "low"
    return "very_low"


def _as_float(value: object, default: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _as_int(value: object, default: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))
