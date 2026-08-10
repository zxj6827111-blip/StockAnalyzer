"""Online market payload -> unified bar view adapter.

The engine's tradability checks (``can_buy``/``can_sell``) consume a bar view
with ``close``/``pre_close``/``suspended``/``up_limit``/``down_limit``. Online
market payloads (see ``week5_service._build_week5_symbol_market_payload``) do
not carry suspension or limit fields, so this adapter normalizes them and
falls back to ``build_price_limits`` (prev_close based) for limit prices.
Suspension is defaulted to False; callers that need strictness should log a
"no suspended data" audit marker instead of guessing.
"""

from __future__ import annotations

from stock_analyzer.config import LimitRuleConfig
from stock_analyzer.data.limit_rule import build_price_limits


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def market_payload_to_bar(
    payload: dict[str, object],
    *,
    symbol: str = "",
    board: str = "",
    limit_rule: LimitRuleConfig | None = None,
) -> dict[str, object]:
    """Normalize an online market payload into the engine's bar view.

    - ``close``: last_price -> open_price -> prev_close (0.0 if none available)
    - ``pre_close``: prev_close (fallback to close)
    - ``suspended``: always False (payload carries no suspension flag)
    - ``up_limit``/``down_limit``: payload fields if present, otherwise
      recomputed from prev_close via ``build_price_limits`` (ST/创业板/科创板
      differentiation comes from ``LimitRuleConfig`` and symbol-based board
      inference).
    """
    close = _optional_float(payload.get("last_price"))
    if close is None or close <= 0:
        close = _optional_float(payload.get("open_price"))
    if close is None or close <= 0:
        close = _optional_float(payload.get("prev_close"))
    if close is None:
        close = 0.0
    pre_close = _optional_float(payload.get("prev_close"))

    bar: dict[str, object] = {
        "symbol": str(symbol).strip(),
        "board": str(board).strip(),
        "name": str(payload.get("name", "")).strip(),
        "close": close,
        "pre_close": pre_close if pre_close is not None and pre_close > 0 else close,
        "suspended": False,
    }
    trade_date = str(payload.get("latest_time", "")).strip()
    if trade_date:
        bar["trade_date"] = trade_date

    limits = build_price_limits(bar=bar, config=limit_rule or LimitRuleConfig())
    if limits.up_limit is not None:
        bar["up_limit"] = limits.up_limit
    if limits.down_limit is not None:
        bar["down_limit"] = limits.down_limit
    return bar
