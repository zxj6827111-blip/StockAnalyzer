"""Board-aware consecutive limit-up risk (PLAN P1-2).

``build_price_limits`` is reused (source first, then board/ST/date rules) so
the streak matches the execution layer's tradability view instead of a fixed
9.5% threshold. A close reaching the up-limit price within ``tolerance``
(default 0.1%) counts as a limit-up day. When the consecutive streak reaches
``consecutive_limit_up_reject`` (default 3), the trend track rejects new
buys (``reject_new_buy=True``); the same symbol may stay on the monster
watchlist but must not enter the default final signals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from stock_analyzer.config import BoardRiskConfig, LimitRuleConfig
from stock_analyzer.data.limit_rule import build_price_limits


@dataclass(slots=True)
class BoardRiskDecision:
    consecutive_limit_up: int
    current_limit_state: str  # none | limit_up | limit_down
    board: str
    reject_new_buy: bool
    reasons: list[str] = field(default_factory=list)


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _date_str(index_value: object) -> str:
    try:
        return str(index_value.date())
    except (AttributeError, TypeError):
        return ""


def _row_pre_close(row: pd.Series, closes: pd.Series, index: int) -> float:
    raw = _optional_float(row.get("pre_close"))
    if raw is not None and raw > 0:
        return raw
    if index > 0:
        previous = closes.iloc[index - 1]
        if pd.notna(previous) and float(previous) > 0:
            return float(previous)
    return 0.0


def consecutive_limit_up_count(
    bars: pd.DataFrame,
    *,
    symbol: str,
    limit_rule: LimitRuleConfig,
    tolerance: float,
) -> int:
    """Consecutive limit-up days counting back from the latest bar."""
    ordered = bars if bars.index.is_monotonic_increasing else bars.sort_index()
    if len(ordered) == 0:
        return 0
    closes = pd.to_numeric(ordered.get("close"), errors="coerce")
    if closes is None or closes.empty:
        return 0
    streak = 0
    for index in range(len(ordered) - 1, -1, -1):
        row = ordered.iloc[index]
        close = pd.to_numeric(row.get("close"), errors="coerce")
        if pd.isna(close) or float(close) <= 0:
            break
        bar = {
            "close": float(close),
            "pre_close": _row_pre_close(row, closes, index),
            "board": str(row.get("board", "") or ""),
            "is_st": bool(row.get("is_st", False)),
            "name": str(row.get("name", "") or ""),
            "trade_date": _date_str(ordered.index[index]),
            "symbol": symbol,
        }
        limits = build_price_limits(bar=bar, config=limit_rule)
        if limits.up_limit is None:
            break
        if float(close) >= limits.up_limit * (1.0 - max(0.0, float(tolerance))):
            streak += 1
        else:
            break
    return streak


def evaluate_board_risk(
    bars: pd.DataFrame,
    *,
    symbol: str,
    limit_rule: LimitRuleConfig,
    board_risk_config: BoardRiskConfig,
) -> BoardRiskDecision:
    """Board-aware limit-up streak + current limit state for one symbol.

    ``bars`` must be sorted by date; the latest row drives the current state.
    """
    ordered = bars if bars.index.is_monotonic_increasing else bars.sort_index()
    streak = consecutive_limit_up_count(
        bars=ordered,
        symbol=symbol,
        limit_rule=limit_rule,
        tolerance=float(board_risk_config.limit_up_tolerance),
    )
    reject_threshold = max(1, int(board_risk_config.consecutive_limit_up_reject))

    current_limit_state = "none"
    reasons: list[str] = []
    if len(ordered) > 0:
        row = ordered.iloc[-1]
        close = _optional_float(row.get("close"))
        closes = pd.to_numeric(ordered.get("close"), errors="coerce")
        if close is not None and close > 0:
            bar = {
                "close": close,
                "pre_close": _row_pre_close(row, closes, len(ordered) - 1),
                "board": str(row.get("board", "") or ""),
                "is_st": bool(row.get("is_st", False)),
                "name": str(row.get("name", "") or ""),
                "trade_date": _date_str(ordered.index[-1]),
                "symbol": symbol,
            }
            limits = build_price_limits(bar=bar, config=limit_rule)
            tolerance = max(0.0, float(board_risk_config.limit_up_tolerance))
            if limits.up_limit is not None and close >= limits.up_limit * (1.0 - tolerance):
                current_limit_state = "limit_up"
            elif (
                limits.down_limit is not None
                and close <= limits.down_limit * (1.0 + tolerance)
            ):
                current_limit_state = "limit_down"

    reject_new_buy = streak >= reject_threshold
    if reject_new_buy:
        reasons.append(f"consecutive_limit_up_{streak}")
    if current_limit_state == "limit_up" and streak == 0:
        reasons.append("limit_up_today")
    board = str(ordered.attrs.get("board", "")) or ""
    if not board and len(ordered) > 0:
        board = str(ordered.iloc[-1].get("board", "") or "")
    return BoardRiskDecision(
        consecutive_limit_up=streak,
        current_limit_state=current_limit_state,
        board=board,
        reject_new_buy=reject_new_buy,
        reasons=reasons,
    )


def board_decision_to_dict(decision: BoardRiskDecision) -> dict[str, Any]:
    """Serializable snapshot of the decision for scan audit artifacts."""
    return {
        "consecutive_limit_up": decision.consecutive_limit_up,
        "current_limit_state": decision.current_limit_state,
        "board": decision.board,
        "reject_new_buy": decision.reject_new_buy,
        "reasons": list(decision.reasons),
    }
