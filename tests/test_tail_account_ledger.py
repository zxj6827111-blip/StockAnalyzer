from datetime import date

import pytest

from stock_analyzer.research.tail_account_ledger import (
    TailAccountLedgerError,
    simulate_tail_account_ledger,
)

CALENDAR = [date(2026, 1, day) for day in range(5, 10)]


def _order(order_id: str, symbol: str = "600000") -> dict[str, object]:
    return {
        "order_id": order_id,
        "arm_id": "candidate",
        "symbol": symbol,
        "decision_date": "2026-01-05",
        "selected": True,
    }


def _fill(
    order_id: str,
    *,
    price: float = 10.0,
    shares: int = 1000,
    symbol: str = "600000",
    exit_day: str | None = None,
    marks: list[dict[str, object]] | None = None,
    **extra: object,
) -> dict[str, object]:
    if marks is None:
        marks = [
            {"trade_date": f"2026-01-{day:02d}", "raw_close": price, "shares": shares}
            for day in range(6, 10 if exit_day is None else int(exit_day[-2:]) + 1)
        ]
    return {
        "order_id": order_id,
        "symbol": symbol,
        "entry_date": "2026-01-06",
        "entry_price": price,
        "quantity": shares,
        "buy_cost": 10.0,
        "exit_date": exit_day,
        "exit_price": price if exit_day else None,
        "sell_cost": 15.0 if exit_day else 0.0,
        "daily_marks": marks,
        **extra,
    }


def test_ledger_replays_cash_split_dividend_and_exit() -> None:
    marks = [
        {"trade_date": "2026-01-06", "raw_close": 10.0, "shares": 1000},
        {"trade_date": "2026-01-07", "raw_close": 9.5, "shares": 1100},
        {"trade_date": "2026-01-08", "raw_close": 10.0, "shares": 1100},
    ]
    result = simulate_tail_account_ledger(
        trading_calendar=CALENDAR,
        orders=[_order("o1")],
        fills=[
            _fill(
                "o1",
                exit_day="2026-01-08",
                marks=marks,
                share_events=[
                    {
                        "trade_date": "2026-01-07",
                        "shares_delta": 100,
                        "action_id": "split-1",
                        "source": "test_feed",
                        "as_of": "2026-01-07",
                    }
                ],
                dividend_receivables=[
                    {
                        "ex_date": "2026-01-07",
                        "record_date": "2026-01-06",
                        "amount_after_tax": 100.0,
                        "pay_date": "2026-01-08",
                        "action_id": "dividend-1",
                        "source": "test_feed",
                        "as_of": "2026-01-07",
                    }
                ],
            )
        ],
    )

    assert result["fills"] == 1
    assert result["exited"] == 1
    assert result["daily_account"][2]["dividend_receivables"] == 100.0
    assert result["daily_account"][3]["cash"] == 301075.0
    assert result["daily_account"][3]["nav"] == 301075.0


def test_unpaid_dividend_stays_as_receivable_after_calendar_end() -> None:
    result = simulate_tail_account_ledger(
        trading_calendar=CALENDAR,
        orders=[_order("o1")],
        fills=[
            _fill(
                "o1",
                dividend_receivables=[
                    {
                        "ex_date": "2026-01-07",
                        "record_date": "2026-01-06",
                        "amount_after_tax": 25.0,
                        "pay_date": "2026-02-01",
                        "action_id": "dividend-1",
                        "source": "test_feed",
                        "as_of": "2026-01-07",
                    }
                ],
            )
        ],
    )
    assert result["daily_account"][-1]["dividend_receivables"] == 25.0
    assert result["daily_account"][-1]["cash"] == 289990.0


def test_cash_shortfall_does_not_replace_selected_recommendation() -> None:
    marks = [
        {"trade_date": f"2026-01-{day:02d}", "raw_close": 9.0, "shares": 1000}
        for day in range(6, 10)
    ]
    result = simulate_tail_account_ledger(
        trading_calendar=CALENDAR,
        orders=[_order("o1"), _order("o2", "000001")],
        fills=[
            _fill(
                "o1",
                buy_cost=1000.0,
                cash_events=[
                    {
                        "trade_date": "2026-01-07",
                        "amount": 100.0,
                        "action_id": "cash-1",
                        "source": "test_feed",
                        "as_of": "2026-01-07",
                    }
                ],
            ),
            _fill("o2", price=9.0, symbol="000001", marks=marks),
        ],
        initial_cash=10_000.0,
    )
    assert result["order_status"]["o1"] == "unfilled_insufficient_cash"
    assert result["order_status"]["o2"] == "open"
    assert result["daily_account"][1]["fills"] == ["o2"]
    assert result["daily_account"][2]["cash"] == 990.0


def test_company_action_cash_event_updates_filled_position_account() -> None:
    result = simulate_tail_account_ledger(
        trading_calendar=CALENDAR,
        orders=[_order("o1")],
        fills=[
            _fill(
                "o1",
                cash_events=[
                    {
                        "trade_date": "2026-01-07",
                        "amount": 5.0,
                        "action_id": "cash-1",
                        "source": "test_feed",
                        "as_of": "2026-01-07",
                    }
                ],
            )
        ],
    )
    assert result["daily_account"][2]["cash"] == 289995.0


def test_recommendation_cap_is_enforced() -> None:
    with pytest.raises(TailAccountLedgerError, match="recommendation cap exceeded"):
        simulate_tail_account_ledger(
            trading_calendar=CALENDAR,
            orders=[_order(f"o{i}", str(i)) for i in range(4)],
            fills=[],
        )


def test_missing_open_position_mark_fails_closed() -> None:
    marks = [
        {"trade_date": "2026-01-06", "raw_close": 10.0, "shares": 1000},
        {"trade_date": "2026-01-08", "raw_close": 10.0, "shares": 1000},
        {"trade_date": "2026-01-09", "raw_close": 10.0, "shares": 1000},
    ]
    with pytest.raises(TailAccountLedgerError, match="position marks are incomplete"):
        simulate_tail_account_ledger(
            trading_calendar=CALENDAR,
            orders=[_order("o1")],
            fills=[_fill("o1", marks=marks)],
        )


def test_unexplained_share_change_fails_closed() -> None:
    marks = [
        {"trade_date": "2026-01-06", "raw_close": 10.0, "shares": 1000},
        {"trade_date": "2026-01-07", "raw_close": 10.0, "shares": 1100},
        {"trade_date": "2026-01-08", "raw_close": 10.0, "shares": 1100},
        {"trade_date": "2026-01-09", "raw_close": 10.0, "shares": 1100},
    ]
    with pytest.raises(TailAccountLedgerError, match="company-action event"):
        simulate_tail_account_ledger(
            trading_calendar=CALENDAR,
            orders=[_order("o1")],
            fills=[_fill("o1", marks=marks)],
        )


def test_share_event_requires_source_identity_and_integral_shares() -> None:
    marks = [
        {"trade_date": "2026-01-06", "raw_close": 10.0, "shares": 1000},
        {"trade_date": "2026-01-07", "raw_close": 10.0, "shares": 1100},
        {"trade_date": "2026-01-08", "raw_close": 10.0, "shares": 1100},
        {"trade_date": "2026-01-09", "raw_close": 10.0, "shares": 1100},
    ]
    with pytest.raises(TailAccountLedgerError, match="source/as_of"):
        simulate_tail_account_ledger(
            trading_calendar=CALENDAR,
            orders=[_order("o1")],
            fills=[
                _fill(
                    "o1",
                    marks=marks,
                    share_events=[
                        {
                            "trade_date": "2026-01-07",
                            "shares_delta": 100,
                            "action_id": "split-1",
                        }
                    ],
                )
            ],
        )

    with pytest.raises(TailAccountLedgerError, match="shares_delta must be an integer"):
        simulate_tail_account_ledger(
            trading_calendar=CALENDAR,
            orders=[_order("o1")],
            fills=[
                _fill(
                    "o1",
                    marks=marks,
                    share_events=[
                        {
                            "trade_date": "2026-01-07",
                            "shares_delta": 100.5,
                            "action_id": "split-1",
                            "source": "test_feed",
                            "as_of": "2026-01-07",
                        }
                    ],
                )
            ],
        )
