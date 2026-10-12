from __future__ import annotations

import pytest

from stock_analyzer.research.suspension_evidence import (
    TUSHARE_SUSPENSION_SOURCE,
    is_verified_no_trade_adj_factor_gap,
)


@pytest.fixture
def full_day_suspension() -> dict[str, object]:
    return {
        "suspended": True,
        "suspend_type": "S",
        "trade_status": "suspended",
        "coverage_complete": True,
        "as_of": "2026-10-11T00:00:00+00:00",
        "source": TUSHARE_SUSPENSION_SOURCE,
    }


def test_verified_no_trade_factor_gap_requires_all_independent_evidence(
    full_day_suspension: dict[str, object],
) -> None:
    assert is_verified_no_trade_adj_factor_gap(
        full_day_suspension,
        has_raw_daily_bar=False,
        raw_minute_bar_count=0,
        action_coverage_complete=True,
        has_corporate_action=False,
        previous_factor=1.25,
        next_factor=1.25,
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"has_raw_daily_bar": True},
        {"raw_minute_bar_count": 1},
        {"action_coverage_complete": False},
        {"has_corporate_action": True},
        {"next_factor": 1.26},
    ],
)
def test_factor_gap_is_not_exempted_when_any_evidence_conflicts(
    full_day_suspension: dict[str, object],
    overrides: dict[str, object],
) -> None:
    inputs: dict[str, object] = {
        "has_raw_daily_bar": False,
        "raw_minute_bar_count": 0,
        "action_coverage_complete": True,
        "has_corporate_action": False,
        "previous_factor": 1.25,
        "next_factor": 1.25,
    }
    inputs.update(overrides)
    assert not is_verified_no_trade_adj_factor_gap(full_day_suspension, **inputs)


def test_unknown_suspension_does_not_exempt_factor_gap() -> None:
    assert not is_verified_no_trade_adj_factor_gap(
        None,
        has_raw_daily_bar=False,
        raw_minute_bar_count=0,
        action_coverage_complete=True,
        has_corporate_action=False,
        previous_factor=1.25,
        next_factor=1.25,
    )
