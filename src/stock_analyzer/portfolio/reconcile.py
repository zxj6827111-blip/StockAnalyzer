"""Simulation-vs-broker position reconciliation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass(slots=True)
class ReconcileDiff:
    symbol: str
    strategy_position: float
    broker_position: float
    diff: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class ReconcileReport:
    timestamp: datetime
    status: str
    matched_count: int
    mismatch_count: int
    missing_in_strategy: list[str]
    missing_in_broker: list[str]
    diffs: list[ReconcileDiff]
    strategy_positions: int
    broker_positions: int
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        payload = {
            "timestamp": self.timestamp.isoformat(),
            "status": self.status,
            "matched_count": self.matched_count,
            "mismatch_count": self.mismatch_count,
            "missing_in_strategy": self.missing_in_strategy,
            "missing_in_broker": self.missing_in_broker,
            "diffs": [item.to_dict() for item in self.diffs],
            "strategy_positions": self.strategy_positions,
            "broker_positions": self.broker_positions,
            "note": self.note,
        }
        return payload


def reconcile_positions(
    strategy_positions: dict[str, float],
    broker_positions: dict[str, float],
    timestamp: datetime,
    tolerance: float,
    note: str = "",
) -> ReconcileReport:
    strategy_symbols = set(strategy_positions.keys())
    broker_symbols = set(broker_positions.keys())

    missing_in_broker = sorted(strategy_symbols - broker_symbols)
    missing_in_strategy = sorted(broker_symbols - strategy_symbols)
    shared_symbols = sorted(strategy_symbols & broker_symbols)

    diffs: list[ReconcileDiff] = []
    matched_count = 0
    for symbol in shared_symbols:
        strategy_value = float(strategy_positions.get(symbol, 0.0))
        broker_value = float(broker_positions.get(symbol, 0.0))
        diff = abs(strategy_value - broker_value)
        if diff <= tolerance:
            matched_count += 1
            continue
        diffs.append(
            ReconcileDiff(
                symbol=symbol,
                strategy_position=strategy_value,
                broker_position=broker_value,
                diff=diff,
            )
        )

    mismatch_count = len(diffs) + len(missing_in_broker) + len(missing_in_strategy)
    status = "ok" if mismatch_count == 0 else "mismatch"

    return ReconcileReport(
        timestamp=timestamp,
        status=status,
        matched_count=matched_count,
        mismatch_count=mismatch_count,
        missing_in_strategy=missing_in_strategy,
        missing_in_broker=missing_in_broker,
        diffs=diffs,
        strategy_positions=len(strategy_positions),
        broker_positions=len(broker_positions),
        note=note,
    )
