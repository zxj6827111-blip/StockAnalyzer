"""Latency SLO evaluator with max-latency semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class LatencyAction:
    state: str
    block_online_update: bool
    raise_u_threshold_bp: int
    force_champion_only: bool


@dataclass(frozen=True, slots=True)
class LatencySLOResult:
    data_latency_sec: float
    max_data_latency_sec: float
    latency_by_input: dict[str, float]
    latency_worst_input: str
    breach: bool
    breach_ratio_20d: float
    latency_watch_flag: bool
    limited_observability: bool
    action: LatencyAction
    breach_history: list[bool]


def evaluate_latency_slo(
    records: Sequence[Mapping[str, object]],
    *,
    now: datetime,
    required_inputs: Sequence[str],
    max_data_latency_sec: float,
    previous_breach_history: Sequence[bool] | None = None,
) -> LatencySLOResult:
    """Evaluate latency SLO using max-latency formula."""
    now_utc = _to_utc(now)
    threshold = max(1.0, float(max_data_latency_sec))
    latency_by_input: dict[str, float] = {}

    for input_name in required_inputs:
        available = _resolve_input_available_time(
            records=records,
            input_name=str(input_name).strip(),
            now=now_utc,
        )
        latency = max(0.0, (now_utc - available).total_seconds())
        latency_by_input[str(input_name).strip()] = round(float(latency), 6)

    if not latency_by_input:
        latency_by_input["market_price"] = 0.0

    worst_input = max(latency_by_input, key=lambda key: latency_by_input[key])
    data_latency_sec = float(latency_by_input[worst_input])
    breach = data_latency_sec > threshold

    history = list(previous_breach_history or [])
    history.append(breach)
    history = history[-20:]
    breach_ratio = (
        sum(1.0 for item in history if item) / float(len(history)) if history else 0.0
    )

    latency_watch = threshold < data_latency_sec <= (2.0 * threshold)
    severe_latency = data_latency_sec > (2.0 * threshold)
    sustained_latency = len(history) >= 20 and breach_ratio > 0.15
    limited_observability = severe_latency or sustained_latency

    if limited_observability:
        action = LatencyAction(
            state="limited_observability",
            block_online_update=True,
            raise_u_threshold_bp=30,
            force_champion_only=sustained_latency,
        )
    elif latency_watch:
        action = LatencyAction(
            state="latency_watch",
            block_online_update=True,
            raise_u_threshold_bp=10,
            force_champion_only=False,
        )
    else:
        action = LatencyAction(
            state="healthy",
            block_online_update=False,
            raise_u_threshold_bp=0,
            force_champion_only=False,
        )

    return LatencySLOResult(
        data_latency_sec=round(data_latency_sec, 6),
        max_data_latency_sec=round(threshold, 6),
        latency_by_input=latency_by_input,
        latency_worst_input=worst_input,
        breach=breach,
        breach_ratio_20d=round(breach_ratio, 6),
        latency_watch_flag=latency_watch,
        limited_observability=limited_observability,
        action=action,
        breach_history=history,
    )


def _resolve_input_available_time(
    *,
    records: Sequence[Mapping[str, object]],
    input_name: str,
    now: datetime,
) -> datetime:
    if not input_name:
        return now
    candidates: list[datetime] = []
    for record in records:
        direct = _extract_record_timestamp(record, key=f"{input_name}_available_time")
        if direct is not None:
            candidates.append(direct)
            continue
        alias = _extract_record_timestamp(record, key=f"{input_name}_available_at")
        if alias is not None:
            candidates.append(alias)
            continue
        generic = _extract_record_timestamp(record, key="available_time")
        if generic is not None:
            candidates.append(generic)
    if not candidates:
        return now
    return min(candidates)


def _extract_record_timestamp(record: Mapping[str, object], *, key: str) -> datetime | None:
    raw = record.get(key)
    if isinstance(raw, datetime):
        return _to_utc(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            return _to_utc(datetime.fromisoformat(text))
        except ValueError:
            return None
    return None


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
