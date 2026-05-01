"""A-share exchange trading calendar helpers."""

from __future__ import annotations

from datetime import date, datetime


_A_SHARE_2026_CLOSED_RANGES: tuple[tuple[date, date, str], ...] = (
    (date(2026, 1, 1), date(2026, 1, 3), "new_year"),
    (date(2026, 2, 15), date(2026, 2, 23), "spring_festival"),
    (date(2026, 4, 4), date(2026, 4, 6), "qingming"),
    (date(2026, 5, 1), date(2026, 5, 5), "labor_day"),
    (date(2026, 6, 19), date(2026, 6, 21), "dragon_boat"),
    (date(2026, 9, 25), date(2026, 9, 27), "mid_autumn"),
    (date(2026, 10, 1), date(2026, 10, 7), "national_day"),
)


def is_a_share_trading_day(value: date | datetime) -> bool:
    """Return whether SSE/SZSE A-share trading should be active on this date."""
    target = value.date() if isinstance(value, datetime) else value
    return a_share_market_close_reason(target) == ""


def a_share_market_close_reason(value: date | datetime) -> str:
    """Return a stable close reason for known A-share non-trading days."""
    target = value.date() if isinstance(value, datetime) else value
    if target.weekday() >= 5:
        return "weekend"
    for start, end, reason in _A_SHARE_2026_CLOSED_RANGES:
        if start <= target <= end:
            return reason
    return ""
