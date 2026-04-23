"""Financial risk filter for ST/delisting/fundamental constraints."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from stock_analyzer.config import FinancialFilterConfig


@dataclass(slots=True)
class FinancialRiskDecision:
    allowed: bool
    penalty_score: float = 0.0
    reasons: list[str] = field(default_factory=list)


class FinancialRiskFilter:
    """Apply strategy-aware financial quality constraints."""

    def __init__(self, config: FinancialFilterConfig) -> None:
        self._config = config

    def evaluate(
        self,
        symbol: str,
        strategy: str,
        snapshot: dict[str, Any] | None,
    ) -> FinancialRiskDecision:
        if not self._config.enabled:
            return FinancialRiskDecision(allowed=True)

        normalized_strategy = strategy.strip().lower()
        apply_to = {item.strip().lower() for item in self._config.apply_to if item.strip()}
        data = snapshot or {}

        breaches = self._collect_breaches(symbol=symbol, snapshot=data)
        if not breaches:
            return FinancialRiskDecision(allowed=True)

        penalty_decision = self._penalty_decision_for_strategy(
            strategy=normalized_strategy,
            breaches=breaches,
        )
        if normalized_strategy in apply_to:
            if penalty_decision is not None:
                return penalty_decision
            reasons = [f"financial_filter:{item}" for item in breaches]
            return FinancialRiskDecision(
                allowed=False,
                reasons=reasons,
            )

        if normalized_strategy == "monster" and penalty_decision is not None:
            return penalty_decision

        return FinancialRiskDecision(allowed=True)

    def _penalty_decision_for_strategy(
        self,
        *,
        strategy: str,
        breaches: list[str],
    ) -> FinancialRiskDecision | None:
        mode = ""
        penalty = 0.0
        if strategy == "trend":
            if any(item not in {"low_roe", "high_debt_ratio"} for item in breaches):
                return None
            mode = self._config.trend_mode
            penalty = self._config.trend_penalty
        elif strategy == "monster":
            mode = self._config.monster_mode
            penalty = self._config.monster_penalty
        else:
            return None

        if mode.strip().lower() != "score_penalty":
            return None

        reasons = [f"financial_penalty:{item}" for item in breaches]
        return FinancialRiskDecision(
            allowed=True,
            penalty_score=max(0.0, float(penalty)),
            reasons=reasons,
        )

    def _collect_breaches(self, symbol: str, snapshot: dict[str, Any]) -> list[str]:
        breaches: list[str] = []
        symbol_name = str(snapshot.get("name", "")).strip().upper()
        is_st = bool(snapshot.get("is_st", False)) or symbol_name.startswith("ST")
        is_delisting = bool(snapshot.get("is_delisting_risk", False))
        roe = _optional_float(snapshot.get("roe"))
        debt_ratio = _optional_float(snapshot.get("debt_ratio"))
        complete_flag = snapshot.get("financial_data_complete")
        missing_policy = self._config.missing_data_policy.strip().lower()

        if self._config.exclude_st and is_st:
            breaches.append("st")
        if self._config.exclude_delisting_risk and is_delisting:
            breaches.append("delisting_risk")

        if complete_flag is False and missing_policy == "reject":
            breaches.append("missing_financial_data")

        if roe is None:
            if missing_policy == "reject":
                breaches.append("missing_roe")
        elif roe < self._config.min_roe:
            breaches.append("low_roe")

        if debt_ratio is None:
            if missing_policy == "reject":
                breaches.append("missing_debt_ratio")
        elif debt_ratio > self._config.max_debt_ratio:
            breaches.append("high_debt_ratio")

        if not symbol.strip():
            breaches.append("invalid_symbol")
        return breaches


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = float(text)
            return parsed if math.isfinite(parsed) else None
        except ValueError:
            return None
    return None
