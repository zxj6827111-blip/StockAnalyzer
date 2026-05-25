from __future__ import annotations

from datetime import datetime, timedelta

from stock_analyzer.portfolio.book import PortfolioBook
from stock_analyzer.types import PipelineSignal


def _buy_signal(symbol: str, score: float = 80.0) -> PipelineSignal:
    return PipelineSignal(
        symbol=symbol,
        strategy="trend",
        score=score,
        grade="S",
        action="buy",
        target_position=0.1,
        probabilities={"lgbm": 0.8, "xgb": 0.8, "meta": 0.8},
        reasons=["soup_entry"],
    )


def test_portfolio_book_enforces_max_holdings() -> None:
    book = PortfolioBook(max_holdings=1, max_hold_days=5)
    now = datetime.fromisoformat("2026-03-01T10:00:00")
    update = book.apply_signals(
        trace_id="trace-1",
        timestamp=now,
        signals=[_buy_signal("600000"), _buy_signal("000001")],
    )
    assert update["opened"] == 1
    assert update["skipped_max_holdings"] == 1
    assert len(book.positions()) == 1


def test_portfolio_book_closes_expired_positions() -> None:
    book = PortfolioBook(max_holdings=2, max_hold_days=1)
    opened_at = datetime.fromisoformat("2026-03-01T10:00:00")
    book.apply_signals(trace_id="trace-1", timestamp=opened_at, signals=[_buy_signal("600000")])

    next_day = opened_at + timedelta(days=1)
    update = book.apply_signals(trace_id="trace-2", timestamp=next_day, signals=[])
    assert update["closed_expired"] == 1
    assert len(book.positions()) == 0
    trades = book.trades(limit=10)
    assert len(trades) == 2
    assert trades[-1]["side"] == "sell"


def test_portfolio_book_enforces_same_sector_limit() -> None:
    book = PortfolioBook(max_holdings=3, max_hold_days=5, max_same_sector=1)
    now = datetime.fromisoformat("2026-03-01T10:00:00")
    update = book.apply_signals(
        trace_id="trace-sector",
        timestamp=now,
        signals=[_buy_signal("600000"), _buy_signal("600001")],
    )
    assert update["opened"] == 1
    assert update["skipped_same_sector"] == 1
    assert len(book.positions()) == 1


def test_portfolio_book_manual_set_and_close() -> None:
    book = PortfolioBook(max_holdings=2, max_hold_days=5)
    now = datetime.fromisoformat("2026-03-01T10:00:00")

    status = book.set_manual_position(
        symbol="600000",
        strategy="manual",
        target_position=0.2,
        timestamp=now,
        trace_id="cmd-1",
    )
    assert status == "opened"
    assert len(book.positions()) == 1

    closed = book.close_position(
        symbol="600000",
        timestamp=now,
        trace_id="cmd-2",
    )
    assert closed is True
    assert len(book.positions()) == 0


def test_portfolio_book_manual_set_accepts_fill_payload() -> None:
    book = PortfolioBook(max_holdings=2, max_hold_days=5)
    now = datetime.fromisoformat("2026-03-01T10:00:00")

    status = book.set_manual_position(
        symbol="600000",
        strategy="manual",
        target_position=0.2,
        timestamp=now,
        trace_id="cmd-fill",
        manual_fill={
            "entry_price": 10.25,
            "quantity": 1000,
            "fee": 3.2,
            "account": "acc-x",
            "manual_trade_time": "2026-03-01T10:01:00",
            "note": "first buy",
        },
    )
    assert status == "opened"
    pos = book.positions()[0]
    assert pos["entry_price"] == 10.25
    assert pos["quantity"] == 1000
    assert pos["fee"] == 3.2
    assert pos["account"] == "acc-x"
    assert pos["manual_trade_time"] == "2026-03-01T10:01:00"
    assert pos["note"] == "first buy"


def test_portfolio_book_update_target_position_ignores_unchanged_target() -> None:
    book = PortfolioBook(max_holdings=2, max_hold_days=5)
    opened_at = datetime.fromisoformat("2026-03-01T10:00:00")
    updated_at = datetime.fromisoformat("2026-03-01T10:10:00")
    status = book.set_manual_position(
        symbol="600000",
        strategy="manual",
        target_position=0.01,
        timestamp=opened_at,
        trace_id="cmd-open",
    )

    changed = book.update_target_position(
        symbol="600000",
        target_position=0.01,
        timestamp=updated_at,
        reason="auto_simulated_adjust",
    )

    assert status == "opened"
    assert changed is False
    position = book.positions()[0]
    assert position["target_position"] == 0.01
    assert position["updated_at"] == opened_at.isoformat()
    assert position["open_reason"] == "manual_set_position"


def test_portfolio_book_reduce_position_records_partial_sell() -> None:
    book = PortfolioBook(max_holdings=2, max_hold_days=5)
    now = datetime.fromisoformat("2026-03-01T10:00:00")
    status = book.set_manual_position(
        symbol="600000",
        strategy="manual",
        target_position=0.2,
        timestamp=now,
        trace_id="cmd-open",
        manual_fill={
            "entry_price": 10.0,
            "quantity": 900,
            "fee": 2.0,
        },
    )
    assert status == "opened"

    trimmed = book.reduce_position(
        symbol="600000",
        target_position=0.08,
        timestamp=now + timedelta(days=1),
        trace_id="cmd-trim",
        reason="take_profit_stage_1_reached",
        manual_fill={"exit_price": 10.6},
    )
    assert trimmed == "trimmed"
    position = book.positions()[0]
    assert position["target_position"] == 0.08
    assert position["quantity"] == 360
    trade = book.trades(limit=1)[0]
    assert trade["side"] == "sell"
    assert trade["reason"] == "take_profit_stage_1_reached"
    assert trade["target_position"] == 0.08
    assert trade["exit_price"] == 10.6


def test_portfolio_book_manual_close_accepts_fill_payload() -> None:
    book = PortfolioBook(max_holdings=2, max_hold_days=5)
    now = datetime.fromisoformat("2026-03-01T10:00:00")
    _ = book.set_manual_position(
        symbol="600000",
        strategy="manual",
        target_position=0.2,
        timestamp=now,
        trace_id="cmd-fill-open",
    )

    closed = book.close_position(
        symbol="600000",
        timestamp=now,
        trace_id="cmd-fill-close",
        manual_fill={
            "exit_price": 11.11,
            "quantity": 900,
            "fee": 2.1,
            "account": "acc-x",
            "manual_trade_time": "2026-03-01T14:55:00",
            "note": "manual sell",
        },
    )
    assert closed is True
    trade = book.trades(limit=1)[0]
    assert trade["side"] == "sell"
    assert trade["exit_price"] == 11.11
    assert trade["exit_quantity"] == 900
    assert trade["exit_fee"] == 2.1
    assert trade["exit_account"] == "acc-x"
    assert trade["exit_trade_time"] == "2026-03-01T14:55:00"
    assert trade["exit_note"] == "manual sell"
