"""Execution window sensitivity shadow checks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(slots=True)
class ExecutionSensitivityResult:
    sensitivity_threshold_bp: float
    sensitivity_days: int
    max_diff_bp: float
    mean_diff_bp: float
    breached_symbols: list[str]
    worst_symbol: str
    run_breach: bool
    consecutive_breach_days: int
    execution_sensitivity_alert: bool
    breach_history: list[bool]


def evaluate_execution_sensitivity(
    *,
    records: Sequence[Mapping[str, object]],
    sensitivity_threshold_bp: float,
    sensitivity_days: int,
    previous_breach_history: Sequence[bool] | None = None,
) -> ExecutionSensitivityResult:
    threshold = max(0.0, float(sensitivity_threshold_bp))
    trigger_days = max(1, int(sensitivity_days))
    diffs: list[tuple[str, float]] = []
    for item in records:
        symbol = str(item.get("symbol", "")).strip() or "UNKNOWN"
        open_proxy = _as_price(
            item.get("vwap_proxy_open", item.get("open")),
        )
        day_proxy = _as_price(
            item.get("vwap_proxy_day", item.get("vwap", item.get("close"))),
        )
        if open_proxy <= 0.0 or day_proxy <= 0.0:
            continue
        diff_bp = abs(open_proxy - day_proxy) / day_proxy * 10_000.0
        diffs.append((symbol, diff_bp))

    breached_symbols = [symbol for symbol, diff in diffs if diff > threshold]
    run_breach = len(breached_symbols) > 0
    max_diff_bp = max((diff for _, diff in diffs), default=0.0)
    mean_diff_bp = (
        sum(diff for _, diff in diffs) / len(diffs)
        if diffs
        else 0.0
    )
    worst_symbol = ""
    if diffs:
        worst_symbol = max(diffs, key=lambda item: item[1])[0]

    history = [bool(item) for item in (previous_breach_history or [])][-29:]
    history.append(run_breach)
    consecutive = 0
    for breached in reversed(history):
        if not breached:
            break
        consecutive += 1
    alert = consecutive >= trigger_days

    return ExecutionSensitivityResult(
        sensitivity_threshold_bp=threshold,
        sensitivity_days=trigger_days,
        max_diff_bp=max_diff_bp,
        mean_diff_bp=mean_diff_bp,
        breached_symbols=breached_symbols,
        worst_symbol=worst_symbol,
        run_breach=run_breach,
        consecutive_breach_days=consecutive,
        execution_sensitivity_alert=alert,
        breach_history=history,
    )


def _as_price(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        try:
            parsed = float(text)
        except ValueError:
            return 0.0
        return max(0.0, parsed)
    return 0.0
