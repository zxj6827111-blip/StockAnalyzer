"""RAW 分钟退出、T+1 可卖性与公司行动账本的边界测试。"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    STATUS_FILLED,
    STATUS_UNCERTAIN,
    simulate_tail_exit_raw_minutes,
)

ENTRY = date(2026, 3, 9)
DAY_1 = date(2026, 3, 10)
DAY_2 = date(2026, 3, 11)
DAY_3 = date(2026, 3, 12)
DAY_4 = date(2026, 3, 13)


def _session(day: date, **extra) -> tuple[date, dict]:
    return day, {
        "trade_status": "normal",
        "exact_limits_complete": True,
        "minute_session_complete": True,
        "down_limit": 8.0,
        **extra,
    }


def _minute(day: date, minute: int, *, open_: float, high: float,
            low: float, close: float, **extra) -> tuple[datetime, dict]:
    return datetime(day.year, day.month, day.day, 9, minute), {
        "open": open_, "high": high, "low": low, "close": close,
        "down_limit": 8.0, "trade_status": "normal", **extra,
    }


def _coverage(*days: date) -> dict[str, dict[str, bool]]:
    return {day.isoformat(): {"coverage_complete": True} for day in days}


def _simulate(*, sessions, minutes, actions=(), coverage=None, quantity=1000,
              contract=DEFAULT_TREND_CONTRACT):
    return simulate_tail_exit_raw_minutes(
        symbol="600000.SH", entry_date=ENTRY, entry_price=10.0,
        quantity=quantity, buy_cost=5.0, minute_bars=minutes,
        session_bars=sessions, corporate_actions=actions,
        corporate_action_coverage=coverage or _coverage(*(day for day, _ in sessions[1:])),
        contract=contract,
    )


def test_take_profit_uses_minute_trigger_and_level_price() -> None:
    result = _simulate(
        sessions=[_session(ENTRY), _session(DAY_1)],
        minutes=[_minute(DAY_1, 31, open_=10.70, high=10.90, low=10.65, close=10.85)],
    )
    assert result.status == STATUS_FILLED
    assert result.take_profit_hit is True
    assert result.exit_date == datetime(2026, 3, 10, 9, 31)
    assert result.exit_price == pytest.approx(10.80)
    assert result.details["exit_price_basis"] == "raw_1m"


def test_same_minute_double_trigger_resolves_to_stop_loss() -> None:
    result = _simulate(
        sessions=[_session(ENTRY), _session(DAY_1)],
        minutes=[_minute(DAY_1, 31, open_=10.0, high=11.0, low=9.0, close=10.1)],
    )
    assert result.stop_loss_hit is True
    assert result.take_profit_hit is False
    assert result.ambiguous_same_bar is True
    assert result.exit_price == pytest.approx(9.50)


def test_cash_dividend_receivable_adjusts_raw_price_exit_threshold() -> None:
    action = {
        "action_id": "cash-1", "ex_date": DAY_1.isoformat(),
        "record_date": ENTRY.isoformat(), "pay_date": DAY_4.isoformat(),
        "implementation_status": "实施",
        "cash_dividend_per_share_after_tax": 0.50,
        "cash_dividend_per_share_before_tax": 0.625,
        "stock_distribution_per_share": 0.0,
        "bonus_shares_per_share": 0.0,
        "transfer_shares_per_share": 0.0,
    }
    result = _simulate(
        sessions=[_session(ENTRY), _session(DAY_1)],
        minutes=[_minute(DAY_1, 31, open_=9.50, high=10.35, low=9.40, close=10.30)],
        actions=[action],
    )
    assert result.status == STATUS_FILLED
    assert result.take_profit_hit is True
    assert result.exit_price == pytest.approx(10.30)
    assert result.gross_return == pytest.approx(0.08)
    assert result.details["cash_dividend_receivable"] == pytest.approx(500.0)
    assert result.details["cash_dividend_unpaid_receivable"] == pytest.approx(500.0)
    assert result.details["company_action_ledger"][0]["action_id"] == "cash-1"


def test_bonus_shares_cannot_be_sold_before_listing_date() -> None:
    action = {
        "action_id": "stock-1", "ex_date": DAY_1.isoformat(),
        "record_date": ENTRY.isoformat(), "stock_listing_date": DAY_3.isoformat(),
        "implementation_status": "实施",
        "cash_dividend_per_share_after_tax": 0.0,
        "cash_dividend_per_share_before_tax": 0.0,
        "stock_distribution_per_share": 0.10,
        "bonus_shares_per_share": 0.05,
        "transfer_shares_per_share": 0.05,
    }
    result = _simulate(
        sessions=[_session(ENTRY), _session(DAY_1), _session(DAY_2), _session(DAY_3)],
        minutes=[
            _minute(DAY_1, 31, open_=9.90, high=10.0, low=9.85, close=9.95),
            _minute(DAY_2, 31, open_=9.95, high=10.0, low=9.90, close=9.95),
            _minute(DAY_3, 31, open_=9.95, high=10.0, low=9.90, close=9.95),
        ],
        actions=[action],
    )
    assert result.status == STATUS_FILLED
    assert result.take_profit_hit is True
    assert result.exit_date == datetime(2026, 3, 12, 9, 31)
    assert result.quantity == 1100
    assert result.details["company_action_ledger"][0]["shares_added"] == 100
    assert result.deferred_sessions == 2


@pytest.mark.parametrize("mutation,reason", [
    ("missing_coverage", "corporate_action_coverage_incomplete"),
    ("missing_listing_date", "corporate_action_stock_listing_date_unknown"),
    ("ratio_mismatch", "corporate_action_share_ratio_mismatch"),
])
def test_unproven_corporate_action_inputs_are_uncertain(mutation, reason) -> None:
    action = {
        "action_id": "stock-1", "ex_date": DAY_1.isoformat(),
        "record_date": ENTRY.isoformat(), "stock_listing_date": DAY_2.isoformat(),
        "implementation_status": "实施",
        "cash_dividend_per_share_after_tax": 0.0,
        "cash_dividend_per_share_before_tax": 0.0,
        "stock_distribution_per_share": 0.10,
        "bonus_shares_per_share": 0.05,
        "transfer_shares_per_share": 0.05,
    }
    coverage = _coverage(DAY_1, DAY_2)
    if mutation == "missing_coverage":
        coverage.pop(DAY_1.isoformat())
    elif mutation == "missing_listing_date":
        action.pop("stock_listing_date")
    elif mutation == "ratio_mismatch":
        action["transfer_shares_per_share"] = 0.02
    result = _simulate(
        sessions=[_session(ENTRY), _session(DAY_1), _session(DAY_2)],
        minutes=[
            _minute(DAY_1, 31, open_=9.10, high=9.20, low=9.00, close=9.10),
            _minute(DAY_2, 31, open_=9.10, high=9.20, low=9.00, close=9.10),
        ],
        actions=[action], coverage=coverage,
    )
    assert result.status == STATUS_UNCERTAIN
    assert result.reason == reason
    assert result.corporate_action_uncertain is True


def test_missing_raw_minute_coverage_blocks_exit_even_with_daily_session() -> None:
    sessions = [_session(ENTRY), _session(DAY_1, minute_session_complete=False)]
    result = _simulate(
        sessions=sessions,
        minutes=[_minute(DAY_1, 31, open_=10.0, high=10.1, low=9.9, close=10.0)],
    )
    assert result.status == STATUS_UNCERTAIN
    assert result.reason == "raw_minute_session_incomplete"


def test_official_suspension_ignores_only_explicit_zero_volume_placeholders() -> None:
    sessions = [
        _session(ENTRY),
        _session(DAY_1, suspended=True, trade_status="suspended",
                 minute_session_complete=False),
        _session(DAY_2),
        _session(DAY_3),
        _session(DAY_4),
    ]
    minutes = [
        _minute(DAY_1, 31, open_=10.0, high=10.0, low=10.0, close=10.0, volume=0),
        _minute(DAY_2, 31, open_=10.0, high=10.1, low=9.9, close=10.0, volume=100),
        _minute(DAY_3, 31, open_=10.0, high=10.1, low=9.9, close=10.0, volume=100),
        _minute(DAY_4, 31, open_=10.0, high=10.1, low=9.9, close=10.0, volume=100),
    ]

    result = _simulate(sessions=sessions, minutes=minutes)

    assert result.status == STATUS_FILLED
    assert result.exit_date == datetime(2026, 3, 13, 9, 31)
    assert result.details["suspended_zero_volume_placeholder_days"] == [
        DAY_1.isoformat()
    ]


@pytest.mark.parametrize("volume", [1, None, "invalid"])
def test_suspended_minute_rows_with_nonzero_or_unknown_volume_still_block(volume) -> None:
    result = _simulate(
        sessions=[
            _session(ENTRY),
            _session(DAY_1, suspended=True, trade_status="suspended"),
        ],
        minutes=[_minute(
            DAY_1, 31, open_=10.0, high=10.1, low=9.9, close=10.0, volume=volume
        )],
    )

    assert result.status == STATUS_UNCERTAIN
    assert result.reason == "suspension_minute_bar_conflict"
