"""Shared dataclasses for pipeline outputs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Literal

SignalAction = Literal["buy", "watch", "hold"]


@dataclass(slots=True)
class CrossReviewResult:
    passed: bool
    merged_probability: float
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ScoredSignal:
    total_score: float
    grade: str
    components: dict[str, float]


@dataclass(slots=True)
class TradeDecision:
    action: SignalAction
    target_position: float
    reason: str


@dataclass(slots=True)
class RiskStatus:
    action: str
    drawdown_pct: float
    degraded_mode: bool
    can_open_new_position: bool
    reason: str
    hard_degraded_mode: bool = False
    soft_degraded_mode: bool = False


@dataclass(slots=True)
class PipelineSignal:
    symbol: str
    strategy: str
    score: float
    grade: str
    action: SignalAction
    target_position: float
    probabilities: dict[str, float]
    reasons: list[str] = field(default_factory=list)
    decision_trace: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class PipelineReport:
    trace_id: str
    timestamp: datetime
    degraded_mode: bool
    risk: RiskStatus
    signals: list[PipelineSignal]

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["timestamp"] = self.timestamp.isoformat()
        return payload
