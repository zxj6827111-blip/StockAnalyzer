"""Fixed-cash portfolio accounting for common-contract tail strategy comparisons.

The input is deliberately event-shaped: selection, fills, RAW daily marks, exits, and
dividend receivables are separate facts. Missing marks block the account path instead of
being filled with zero or dropped from daily NAV.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date
from typing import Any

from stock_analyzer.contracts.trend_strategy import DEFAULT_TREND_CONTRACT

INITIAL_CASH = 300_000.0
REFERENCE_NOTIONAL = 10_000.0
MAX_RECOMMENDATIONS_PER_DAY = 3
_LOT_SIZE = 100
_MAX_HOLDING_INDEX = (
    DEFAULT_TREND_CONTRACT.holding_days - 1 + DEFAULT_TREND_CONTRACT.exit_defer_max_sessions
)


class TailAccountLedgerError(ValueError):
    """The account input cannot be replayed under the frozen cash and fill contract."""


def _day(value: Any) -> date:
    if value is None:
        raise TailAccountLedgerError("date value is required")
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _finite_positive(value: Any, field: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise TailAccountLedgerError(f"{field} must be finite and > 0")
    return number


def _integral(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise TailAccountLedgerError(f"{field} must be an integer")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TailAccountLedgerError(f"{field} must be an integer") from exc
    if not math.isfinite(number) or not number.is_integer():
        raise TailAccountLedgerError(f"{field} must be an integer")
    return int(number)


def _validate_action_provenance(event: dict[str, Any], order_id: str) -> None:
    if not str(event.get("action_id") or "").strip():
        raise TailAccountLedgerError(f"company-action event lacks action_id: {order_id}")
    if not str(event.get("source") or "").strip() or not str(event.get("as_of") or "").strip():
        raise TailAccountLedgerError(f"company-action event lacks source/as_of: {order_id}")


def simulate_tail_account_ledger(
    *,
    trading_calendar: list[date],
    orders: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    initial_cash: float = INITIAL_CASH,
    reference_notional: float = REFERENCE_NOTIONAL,
    max_recommendations_per_day: int = MAX_RECOMMENDATIONS_PER_DAY,
) -> dict[str, Any]:
    """Replay one arm's cash, positions, dividend receivables, and daily NAV.

    ``orders`` contains every final recommendation, including those that never filled.
    Each selected order has at most one matching row in ``fills``. A fill row contains
    entry/exit facts, one RAW ``daily_marks`` row for every open session, and optional
    per-position ``dividend_receivables``. A selected but unfilled order consumes a slot
    and is never replaced by another stock.

    A daily mark is ``{trade_date, raw_close, shares, suspended}``. When a suspension is
    explicitly declared and has no close, the last declared RAW close is carried forward
    and the report marks that day stale. Other missing marks fail closed.
    """
    days = [_day(value) for value in trading_calendar]
    if not days or days != sorted(set(days)):
        raise TailAccountLedgerError("trading_calendar must be non-empty, unique, and sorted")
    day_index = {day: index for index, day in enumerate(days)}
    if not math.isfinite(float(initial_cash)) or float(initial_cash) <= 0:
        raise TailAccountLedgerError("initial_cash must be finite and > 0")
    if not math.isfinite(float(reference_notional)) or float(reference_notional) <= 0:
        raise TailAccountLedgerError("reference_notional must be finite and > 0")
    if int(max_recommendations_per_day) < 0:
        raise TailAccountLedgerError("max_recommendations_per_day must be >= 0")

    orders_by_id: dict[str, dict[str, Any]] = {}
    selected_by_day: dict[date, int] = defaultdict(int)
    order_keys: set[tuple[str, date, str]] = set()
    for raw in orders:
        order_id = str(raw.get("order_id") or "").strip()
        symbol = str(raw.get("symbol") or "").strip()
        decision_day = _day(raw.get("decision_date"))
        if not order_id or not symbol:
            raise TailAccountLedgerError("every order requires order_id and symbol")
        if order_id in orders_by_id:
            raise TailAccountLedgerError(f"duplicate order_id: {order_id}")
        key = (symbol, decision_day, str(raw.get("arm_id") or ""))
        if key in order_keys:
            raise TailAccountLedgerError(f"duplicate recommendation key: {key!r}")
        order_keys.add(key)
        if decision_day not in day_index:
            raise TailAccountLedgerError(
                f"decision date is outside the trading calendar: {decision_day}"
            )
        selected_value = raw.get("selected")
        if not isinstance(selected_value, bool):
            raise TailAccountLedgerError(f"selected must be a boolean: {order_id}")
        selected = selected_value
        if selected:
            selected_by_day[decision_day] += 1
            if selected_by_day[decision_day] > max_recommendations_per_day:
                raise TailAccountLedgerError(
                    f"recommendation cap exceeded on {decision_day}: "
                    f"{selected_by_day[decision_day]} > {max_recommendations_per_day}"
                )
        orders_by_id[order_id] = {**raw, "_decision_day": decision_day, "_selected": selected}

    fills_by_id: dict[str, dict[str, Any]] = {}
    entry_by_day: dict[date, list[dict[str, Any]]] = defaultdict(list)
    exit_by_day: dict[date, list[dict[str, Any]]] = defaultdict(list)
    mark_by_order: dict[str, dict[date, dict[str, Any]]] = {}
    payment_by_day: dict[date, list[tuple[str, float]]] = defaultdict(list)
    accrual_by_day: dict[date, list[tuple[str, float, date | None]]] = defaultdict(list)
    cash_event_by_day: dict[date, list[tuple[str, float]]] = defaultdict(list)

    for raw in fills:
        order_id = str(raw.get("order_id") or "").strip()
        if order_id not in orders_by_id:
            raise TailAccountLedgerError(
                f"fill has no matching selected recommendation: {order_id!r}"
            )
        order = orders_by_id[order_id]
        if not order["_selected"]:
            raise TailAccountLedgerError(f"unselected order has a fill: {order_id}")
        if order_id in fills_by_id:
            raise TailAccountLedgerError(f"duplicate fill for order_id: {order_id}")
        entry_day = _day(raw.get("entry_date"))
        if (
            entry_day not in day_index
            or order["_decision_day"] not in day_index
            or day_index[entry_day] != day_index[order["_decision_day"]] + 1
        ):
            raise TailAccountLedgerError(
                f"invalid T+1 entry date for order {order_id}: {entry_day}"
            )
        fill_symbol = str(raw.get("symbol") or order.get("symbol") or "").strip()
        if fill_symbol != str(order["symbol"]).strip():
            raise TailAccountLedgerError(f"fill symbol differs from recommendation: {order_id}")
        price = _finite_positive(raw.get("entry_price"), "entry_price")
        quantity = _integral(raw.get("quantity"), "quantity")
        if quantity <= 0 or quantity % _LOT_SIZE:
            raise TailAccountLedgerError(f"quantity must be a positive 100-share lot: {order_id}")
        notional = price * quantity
        if notional > reference_notional + 0.011:
            raise TailAccountLedgerError(
                f"fill exceeds reference notional for order {order_id}: {notional:.4f}"
            )
        buy_cost = float(raw.get("buy_cost") or 0.0)
        if not math.isfinite(buy_cost) or buy_cost < 0:
            raise TailAccountLedgerError(f"buy_cost must be finite and >= 0: {order_id}")

        exit_day_value = raw.get("exit_date")
        exit_day = _day(exit_day_value) if exit_day_value else None
        if exit_day is not None:
            if exit_day not in day_index or day_index[exit_day] <= day_index[entry_day]:
                raise TailAccountLedgerError(f"exit must be a later open session: {order_id}")
            if day_index[exit_day] - day_index[entry_day] > _MAX_HOLDING_INDEX:
                raise TailAccountLedgerError(
                    f"exit exceeds frozen holding/defer window: {order_id}"
                )
            _finite_positive(raw.get("exit_price"), "exit_price")
            sell_cost = float(raw.get("sell_cost") or 0.0)
            if not math.isfinite(sell_cost) or sell_cost < 0:
                raise TailAccountLedgerError(f"sell_cost must be finite and >= 0: {order_id}")
        else:
            sell_cost = 0.0

        marks: dict[date, dict[str, Any]] = {}
        for mark in raw.get("daily_marks") or []:
            mark_day = _day(mark.get("trade_date"))
            if mark_day not in day_index:
                raise TailAccountLedgerError(f"mark date outside calendar for order {order_id}")
            if mark_day in marks:
                raise TailAccountLedgerError(
                    f"duplicate daily mark for order {order_id}/{mark_day}"
                )
            mark_shares = _integral(mark.get("shares"), "mark shares")
            if mark_shares <= 0:
                raise TailAccountLedgerError(f"mark shares must be positive: {order_id}/{mark_day}")
            close_raw = mark.get("raw_close")
            close = None if close_raw is None else _finite_positive(close_raw, "raw_close")
            suspended_value = mark.get("suspended", False)
            if not isinstance(suspended_value, bool):
                raise TailAccountLedgerError(f"suspended must be a boolean: {order_id}/{mark_day}")
            suspended = suspended_value
            if close is None and not suspended:
                raise TailAccountLedgerError(
                    f"non-suspended position has no RAW mark: {order_id}/{mark_day}"
                )
            marks[mark_day] = {
                "shares": mark_shares,
                "raw_close": close,
                "suspended": suspended,
            }
        if entry_day not in marks:
            raise TailAccountLedgerError(f"entry-day mark is missing for order {order_id}")
        if marks[entry_day]["shares"] != quantity:
            raise TailAccountLedgerError(f"entry-day shares differ from fill quantity: {order_id}")
        last_required_day = exit_day or days[-1]
        required_days = [
            day
            for day in days
            if day_index[entry_day] <= day_index[day] <= day_index[last_required_day]
        ]
        missing_marks = [day for day in required_days if day not in marks]
        if missing_marks:
            raise TailAccountLedgerError(
                f"position marks are incomplete for order {order_id}: "
                f"{[day.isoformat() for day in missing_marks[:5]]}"
            )
        share_events: dict[date, int] = {}
        for action in raw.get("share_events") or []:
            _validate_action_provenance(action, order_id)
            event_day = _day(action.get("trade_date"))
            delta = _integral(action.get("shares_delta"), "shares_delta")
            if (
                event_day not in day_index
                or delta == 0
                or event_day < entry_day
                or day_index[event_day] > day_index[last_required_day]
            ):
                raise TailAccountLedgerError(f"invalid share event for order {order_id}")
            if event_day in share_events:
                raise TailAccountLedgerError(
                    f"duplicate share event for order {order_id}/{event_day}"
                )
            share_events[event_day] = delta
        previous_shares = quantity
        for mark_day in required_days:
            current_shares = marks[mark_day]["shares"]
            delta = current_shares - previous_shares
            if delta != share_events.get(mark_day, 0):
                raise TailAccountLedgerError(
                    f"share change lacks a matching company-action event: {order_id}/{mark_day}"
                )
            previous_shares = current_shares
        if exit_day is not None and exit_day not in marks:
            raise TailAccountLedgerError(f"exit-day mark is missing for order {order_id}")

        fill = {
            **raw,
            "order_id": order_id,
            "symbol": str(raw.get("symbol") or order.get("symbol") or ""),
            "entry_date": entry_day,
            "entry_price": price,
            "quantity": quantity,
            "buy_cost": buy_cost,
            "exit_date": exit_day,
            "sell_cost": sell_cost,
        }
        fills_by_id[order_id] = fill
        entry_by_day[entry_day].append(fill)
        if exit_day is not None:
            exit_by_day[exit_day].append(fill)
        mark_by_order[order_id] = marks

        for action in raw.get("dividend_receivables") or []:
            _validate_action_provenance(action, order_id)
            ex_day = _day(action.get("ex_date"))
            record_day = _day(action.get("record_date"))
            amount = float(action.get("amount_after_tax"))
            if (
                ex_day not in day_index
                or ex_day <= entry_day
                or record_day not in day_index
                or record_day < entry_day
                or (exit_day is not None and ex_day > exit_day)
                or not math.isfinite(amount)
                or amount < 0
            ):
                raise TailAccountLedgerError(f"invalid dividend receivable for order {order_id}")
            pay_day_value = action.get("pay_date")
            pay_day = _day(pay_day_value) if pay_day_value else None
            accrual_by_day[ex_day].append((order_id, amount, pay_day))
            if pay_day is not None:
                if pay_day < ex_day:
                    raise TailAccountLedgerError(f"invalid dividend pay date for order {order_id}")
                payment_day = next((day for day in days if day >= pay_day), None)
                if payment_day is not None:
                    payment_by_day[payment_day].append((order_id, amount))

        for event in raw.get("cash_events") or []:
            _validate_action_provenance(event, order_id)
            event_day = _day(event.get("trade_date"))
            amount = float(event.get("amount"))
            if (
                event_day not in day_index
                or event_day <= entry_day
                or (exit_day is not None and event_day > exit_day)
                or not math.isfinite(amount)
            ):
                raise TailAccountLedgerError(f"invalid company-action cash event for {order_id}")
            cash_event_by_day[event_day].append((order_id, amount))

    cash = float(initial_cash)
    positions: dict[str, dict[str, Any]] = {}
    receivables: dict[str, float] = defaultdict(float)
    last_close: dict[str, float] = {}
    stale_marks: list[dict[str, str]] = []
    account_rows: list[dict[str, Any]] = []
    order_status = {
        order_id: "selected_unfilled" if order["_selected"] else "not_selected"
        for order_id, order in orders_by_id.items()
    }

    for day in days:
        day_fills: list[str] = []
        day_exits: list[str] = []
        for order_id, amount, _pay_day in accrual_by_day.get(day, []):
            if order_id not in positions:
                continue
            receivables[order_id] += amount
        for order_id, amount in payment_by_day.get(day, []):
            paid = min(float(amount), float(receivables.get(order_id, 0.0)))
            cash += paid
            receivables[order_id] = max(0.0, receivables.get(order_id, 0.0) - paid)
        for _order_id, amount in cash_event_by_day.get(day, []):
            if _order_id in positions:
                cash += amount

        for fill in sorted(entry_by_day.get(day, []), key=lambda item: item["order_id"]):
            order_id = fill["order_id"]
            debit = fill["entry_price"] * fill["quantity"] + fill["buy_cost"]
            if debit > cash + 1e-8:
                order_status[order_id] = "unfilled_insufficient_cash"
                continue
            cash -= debit
            positions[order_id] = {
                "symbol": fill["symbol"],
                "shares": fill["quantity"],
                "last_close": None,
            }
            order_status[order_id] = "open"
            day_fills.append(order_id)

        market_value = 0.0
        for order_id, position in list(positions.items()):
            mark = mark_by_order[order_id].get(day)
            if mark is None:
                raise TailAccountLedgerError(f"open position has no daily mark: {order_id}/{day}")
            if mark["raw_close"] is None:
                close = position.get("last_close") or last_close.get(position["symbol"])
                if close is None:
                    raise TailAccountLedgerError(
                        f"suspended position has no previous RAW close: {order_id}/{day}"
                    )
                stale_marks.append({"order_id": order_id, "trade_date": day.isoformat()})
            else:
                close = float(mark["raw_close"])
            position["shares"] = int(mark["shares"])
            position["last_close"] = float(close)
            last_close[position["symbol"]] = float(close)
            market_value += int(mark["shares"]) * float(close)

        for fill in sorted(exit_by_day.get(day, []), key=lambda item: item["order_id"]):
            order_id = fill["order_id"]
            position = positions.get(order_id)
            if position is None:
                continue
            exit_quantity = int(position["shares"])
            proceeds = _finite_positive(fill.get("exit_price"), "exit_price") * exit_quantity
            cash += proceeds - float(fill["sell_cost"])
            market_value -= exit_quantity * float(position["last_close"])
            positions.pop(order_id, None)
            order_status[order_id] = "exited"
            day_exits.append(order_id)

        receivable_value = sum(float(value) for value in receivables.values())
        nav = cash + market_value + receivable_value
        previous_nav = account_rows[-1]["nav"] if account_rows else float(initial_cash)
        account_rows.append(
            {
                "trade_date": day.isoformat(),
                "cash": round(cash, 8),
                "position_market_value": round(market_value, 8),
                "dividend_receivables": round(receivable_value, 8),
                "nav": round(nav, 8),
                "daily_return": float(nav / previous_nav - 1.0),
                "open_positions": len(positions),
                "recommendations": selected_by_day.get(day, 0),
                "fills": day_fills,
                "exits": day_exits,
            }
        )

    return {
        "schema": "tail_account_ledger.v1",
        "status": "completed",
        "initial_cash": float(initial_cash),
        "reference_notional": float(reference_notional),
        "max_recommendations_per_day": int(max_recommendations_per_day),
        "trading_days": len(days),
        "selected_recommendations": sum(selected_by_day.values()),
        "fills": sum(1 for value in order_status.values() if value in {"open", "exited"}),
        "exited": sum(value == "exited" for value in order_status.values()),
        "order_status": dict(sorted(order_status.items())),
        "stale_suspended_marks": stale_marks,
        "daily_account": account_rows,
    }


__all__ = [
    "INITIAL_CASH",
    "MAX_RECOMMENDATIONS_PER_DAY",
    "REFERENCE_NOTIONAL",
    "TailAccountLedgerError",
    "simulate_tail_account_ledger",
]
