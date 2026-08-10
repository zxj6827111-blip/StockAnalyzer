"""#31 pre-holiday position reduction (CalendarFactorEngine) acceptance tests.

These tests directly exercise the date-triggered branch of
``CalendarFactorEngine.evaluate``, which the existing week6 execution tests
(test_service_week6_execution.py) only reach indirectly through the
position-multiplier scaling path.

ACTUAL IMPLEMENTATION SEMANTICS (vs PRD):
- PRD (#31): 长假（春节/国庆）前 3 个交易日自动降仓 50%。
- Implementation (week6/engines.py:145-166): 降仓触发条件是
  ``days_to_weekend = max(0, 4 - weekday) <= pre_holiday_reduce_days``，
  即「距本周五的天数 ≤ 3」的周内近似，与真实长假（春节/国庆）日期无关。
  默认配置（pre_holiday_reduce_days=3）下，每周二至周五（以及周末）都会触发
  0.5 倍降仓——比 PRD 的「前 3 个交易日」多覆盖周二（4 个交易日），
  且普通周同样降仓，不感知春节/国庆长假日历。

The assertions below follow the implemented algorithm (per audit instruction);
the divergence from the PRD is intentional and documented in the docstrings.
"""

from __future__ import annotations

from datetime import date
from typing import Any, cast

import pytest

from stock_analyzer.config import HolidayRiskConfig
from stock_analyzer.week6.engines import CalendarFactorEngine


def _as_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    raise AssertionError(f"Expected dict, got {type(value).__name__}")


def _as_float(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    raise AssertionError(f"Expected numeric value, got {value!r}")


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    raise AssertionError(f"Expected bool value, got {value!r}")


def _as_int(value: object) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    raise AssertionError(f"Expected numeric value, got {value!r}")


_DEFAULT_CONFIG = HolidayRiskConfig()  # pre_holiday_reduce_days=3, multiplier=0.5


def _evaluate(day: date, config: HolidayRiskConfig = _DEFAULT_CONFIG) -> dict[str, object]:
    return _as_dict(CalendarFactorEngine(config=config).evaluate(now=day))


@pytest.mark.parametrize(
    ("day", "expected_days_to_weekend"),
    [
        # Tue=3d to weekend, Wed=2d, Thu=1d, Fri=0d - implementation's
        # "pre-holiday window" covers the last 4 trading days of the week
        # (PRD says the last 3 trading days before a long holiday).
        (date(2026, 3, 3), 3),
        (date(2026, 3, 4), 2),
        (date(2026, 3, 5), 1),
        (date(2026, 3, 6), 0),
        # Weekend days are also treated as pre-holiday (not trading days, but
        # the implementation does not exclude them).
        (date(2026, 3, 7), 0),
        (date(2026, 3, 8), 0),
    ],
)
def test_calendar_factor_pre_holiday_window_reduces_position_to_half(
    day: date,
    expected_days_to_weekend: int,
) -> None:
    """Days within `pre_holiday_reduce_days` of the weekend get multiplier 0.5.

    PRD semantic: 长假（春节/国庆）前第 1/2/3 个交易日 → 0.5。
    Actual semantic: 距本周五天数 ≤ 3（每周二至周五+周末）→ 0.5，与真实长假无关。
    """
    result = _evaluate(day)

    assert _as_bool(result["pre_holiday_reduce"]) is True
    assert _as_float(result["position_multiplier"]) == 0.5
    assert _as_int(result["days_to_weekend"]) == expected_days_to_weekend


@pytest.mark.parametrize(
    "day",
    [
        date(2026, 3, 2),  # Monday = 4 trading days before the weekend (4th day or earlier)
        date(2026, 3, 9),  # ordinary Monday
        date(2026, 3, 16),  # ordinary Monday
    ],
)
def test_calendar_factor_outside_pre_holiday_window_keeps_full_position(day: date) -> None:
    """Days with days_to_weekend > pre_holiday_reduce_days keep multiplier 1.0.

    PRD semantic: 长假前第 4 个交易日或更早 → 1.0（不降仓）；普通日期 → 1.0。
    Actual semantic: 周一（距周末 4 天）→ 1.0。
    """
    result = _evaluate(day)

    assert _as_bool(result["pre_holiday_reduce"]) is False
    assert _as_float(result["position_multiplier"]) == 1.0
    assert _as_int(result["days_to_weekend"]) == 4


def test_calendar_factor_reduce_days_zero_triggers_only_on_weekend_eve() -> None:
    """pre_holiday_reduce_days=0 narrows the window to days_to_weekend==0 (Friday).

    Documents the configurable boundary: with a 0-day window only the weekend
    eve (Friday, plus weekends) is treated as pre-holiday.
    """
    config = HolidayRiskConfig(pre_holiday_reduce_days=0, max_position_multiplier=0.5)

    monday = _evaluate(date(2026, 3, 2), config=config)
    thursday = _evaluate(date(2026, 3, 5), config=config)
    friday = _evaluate(date(2026, 3, 6), config=config)

    assert _as_float(monday["position_multiplier"]) == 1.0
    assert _as_bool(monday["pre_holiday_reduce"]) is False
    assert _as_float(thursday["position_multiplier"]) == 1.0
    assert _as_bool(thursday["pre_holiday_reduce"]) is False
    assert _as_float(friday["position_multiplier"]) == 0.5
    assert _as_bool(friday["pre_holiday_reduce"]) is True


def test_calendar_factor_divergence_from_prd_long_holiday_dates() -> None:
    """Documents the divergence between PRD long-holiday semantics and the
    weekday-based approximation (asserts the implementation, not the PRD).

    - 2026-02-10 is the 4th trading day before 春节 (CNY 2026-02-17 is a
      Tuesday; pre-holiday trading days are 02-11/02-12/02-13). PRD says the
      4th trading day must NOT reduce, but the implementation reduces (0.5)
      because it is a Tuesday (days_to_weekend=3).
    - 2026-03-03 is an ordinary Tuesday far away from any long holiday. PRD
      says ordinary dates must keep 1.0, but the implementation reduces (0.5)
      purely based on the weekday.
    """
    pre_cny_4th_trading_day = _evaluate(date(2026, 2, 10))
    ordinary_tuesday = _evaluate(date(2026, 3, 3))

    assert _as_float(pre_cny_4th_trading_day["position_multiplier"]) == 0.5
    assert _as_bool(pre_cny_4th_trading_day["pre_holiday_reduce"]) is True
    assert _as_float(ordinary_tuesday["position_multiplier"]) == 0.5
    assert _as_bool(ordinary_tuesday["pre_holiday_reduce"]) is True
