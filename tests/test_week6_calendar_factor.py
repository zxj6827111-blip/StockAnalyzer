"""#31 pre-holiday position reduction (CalendarFactorEngine) acceptance tests.

PRD semantics: the last ``pre_holiday_reduce_days`` (default 3) trading days
before a long holiday —— 春节/国庆/五一/清明/中秋 blocks with >= 3 consecutive
closed days —— get ``position_multiplier = max_position_multiplier`` (0.5);
every other date keeps 1.0.

The engine detects long holidays via
``stock_analyzer.market_calendar.is_a_share_trading_day``; the dates asserted
below follow that calendar's 2026 A-share schedule (spring festival closed
2026-02-15..02-23, qingming 04-04..04-06, labor day 05-01..05-05, mid-autumn
09-25..09-27, national day 10-01..10-07).
"""

from __future__ import annotations

from datetime import date
from typing import cast

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
    ("day", "expected_days_until_holiday"),
    [
        # 春节 2026: 放假 02-15..02-23，前最后 3 个交易日为 02-13/02-12/02-11
        (date(2026, 2, 13), 1),
        (date(2026, 2, 12), 2),
        (date(2026, 2, 11), 3),
        # 国庆 2026: 放假 10-01..10-07，前最后 3 个交易日为 09-30/09-29/09-28
        (date(2026, 9, 30), 1),
        (date(2026, 9, 29), 2),
        (date(2026, 9, 28), 3),
        # 清明 2026: 放假 04-04..04-06，前最后 3 个交易日为 04-03/04-02/04-01
        (date(2026, 4, 3), 1),
        (date(2026, 4, 2), 2),
        (date(2026, 4, 1), 3),
        # 五一 2026: 放假 05-01..05-05，前最后 3 个交易日为 04-30/04-29/04-28
        (date(2026, 4, 30), 1),
        (date(2026, 4, 29), 2),
        (date(2026, 4, 28), 3),
        # 中秋 2026: 放假 09-25..09-27（3 天连休，同样视为长假）
        (date(2026, 9, 24), 1),
    ],
)
def test_calendar_factor_long_holiday_last_trading_days_reduce_to_half(
    day: date,
    expected_days_until_holiday: int,
) -> None:
    """长假前第 1/2/3 个交易日 → position_multiplier=0.5。"""
    result = _evaluate(day)

    assert _as_bool(result["pre_holiday_reduce"]) is True
    assert _as_float(result["position_multiplier"]) == 0.5
    assert _as_int(result["days_until_holiday"]) == expected_days_until_holiday


@pytest.mark.parametrize(
    ("day", "expected_days_until_holiday"),
    [
        # 春节前第 4/5 个交易日（及更早）→ 1.0
        (date(2026, 2, 10), 4),
        (date(2026, 2, 9), 5),
        # 国庆前（中间隔着中秋假期）第 4 个交易日及更早 → 1.0
        (date(2026, 9, 21), 4),
        # 清明前第 4 个交易日 → 1.0
        (date(2026, 3, 31), 4),
        # 五一前第 4 个交易日 → 1.0
        (date(2026, 4, 27), 4),
    ],
)
def test_calendar_factor_long_holiday_earlier_trading_days_keep_full(
    day: date,
    expected_days_until_holiday: int,
) -> None:
    """长假前第 4 个交易日及更早 → position_multiplier=1.0。"""
    result = _evaluate(day)

    assert _as_bool(result["pre_holiday_reduce"]) is False
    assert _as_float(result["position_multiplier"]) == 1.0
    assert _as_int(result["days_until_holiday"]) == expected_days_until_holiday


@pytest.mark.parametrize(
    "day",
    [
        date(2026, 3, 2),  # 普通周一
        date(2026, 3, 3),  # 普通周二（旧周内近似下会误降仓）
        date(2026, 3, 4),  # 普通周三
        date(2026, 3, 5),  # 普通周四
        date(2026, 3, 6),  # 普通周五
        date(2026, 8, 10),  # 无长假时段内的普通交易日
    ],
)
def test_calendar_factor_ordinary_week_keeps_full_position(day: date) -> None:
    """普通周（无长假临近）任何交易日 → 1.0。"""
    result = _evaluate(day)

    assert _as_bool(result["pre_holiday_reduce"]) is False
    assert _as_float(result["position_multiplier"]) == 1.0
    assert _as_int(result["days_until_holiday"]) == 0


@pytest.mark.parametrize(
    "day",
    [
        date(2026, 2, 14),  # 春节前最后一个周六（非交易日）
        date(2026, 2, 17),  # 春节假期中
        date(2026, 3, 7),  # 普通周六
        date(2026, 3, 8),  # 普通周日
        date(2026, 10, 1),  # 国庆假期中
    ],
)
def test_calendar_factor_non_trading_days_keep_full_position(day: date) -> None:
    """非交易日不降仓 → 1.0（PRD：其他日期 1.0）。"""
    result = _evaluate(day)

    assert _as_bool(result["pre_holiday_reduce"]) is False
    assert _as_float(result["position_multiplier"]) == 1.0
    assert _as_int(result["days_until_holiday"]) == 0


def test_calendar_factor_reduce_days_zero_triggers_only_on_last_trading_day() -> None:
    """pre_holiday_reduce_days=0 时仅长假前最后 1 个交易日触发（边界语义）。"""
    config = HolidayRiskConfig(pre_holiday_reduce_days=0, max_position_multiplier=0.5)

    last_trading_day = _evaluate(date(2026, 2, 13), config=config)
    second_to_last = _evaluate(date(2026, 2, 12), config=config)
    third_to_last = _evaluate(date(2026, 2, 11), config=config)
    ordinary_friday = _evaluate(date(2026, 3, 6), config=config)

    assert _as_float(last_trading_day["position_multiplier"]) == 0.5
    assert _as_bool(last_trading_day["pre_holiday_reduce"]) is True
    assert _as_float(second_to_last["position_multiplier"]) == 1.0
    assert _as_bool(second_to_last["pre_holiday_reduce"]) is False
    assert _as_float(third_to_last["position_multiplier"]) == 1.0
    assert _as_bool(third_to_last["pre_holiday_reduce"]) is False
    assert _as_float(ordinary_friday["position_multiplier"]) == 1.0
    assert _as_bool(ordinary_friday["pre_holiday_reduce"]) is False


def test_calendar_factor_max_position_multiplier_is_configurable() -> None:
    """max_position_multiplier 可配置：长假前交易日按其值降仓。"""
    config = HolidayRiskConfig(pre_holiday_reduce_days=3, max_position_multiplier=0.4)

    result = _evaluate(date(2026, 2, 13), config=config)
    assert _as_float(result["position_multiplier"]) == 0.4
    assert _as_bool(result["pre_holiday_reduce"]) is True
