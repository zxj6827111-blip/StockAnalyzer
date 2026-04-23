from __future__ import annotations

from datetime import datetime

from stock_analyzer.backtest.matcher import ExecutionMatcher
from stock_analyzer.config import BacktestMatcherConfig, LimitRuleConfig


def test_matcher_enforces_t_plus_one_on_sell() -> None:
    config = BacktestMatcherConfig()
    matcher = ExecutionMatcher(config)

    bar = {"close": 10.0, "down_limit": 9.0, "suspended": False}
    buy_date = datetime.fromisoformat("2026-03-01T10:00:00")
    same_day = datetime.fromisoformat("2026-03-01T14:00:00")
    decision = matcher.can_sell(bar=bar, last_buy_date=buy_date, current_date=same_day)
    assert decision.executable is False
    assert decision.reason == "t_plus_1_block"


def test_matcher_sell_cost_contains_stamp_tax() -> None:
    config = BacktestMatcherConfig()
    matcher = ExecutionMatcher(config)
    buy_cost = matcher.estimate_cost(side="buy", price=10.0, quantity=10000)
    sell_cost = matcher.estimate_cost(side="sell", price=10.0, quantity=10000)
    assert sell_cost > buy_cost


def test_matcher_dynamic_slippage_uses_max_of_static_and_dynamic() -> None:
    config = BacktestMatcherConfig(
        slippage_by_strategy={"trend": 0.002},
        max_dynamic_slippage_ratio=0.012,
    )
    matcher = ExecutionMatcher(config)
    ratio = matcher.dynamic_slippage_ratio(
        strategy="trend",
        atr14=0.15,
        close=10.0,
        volume_ratio=2.0,
    )
    assert ratio > 0.002
    assert matcher.should_downgrade_by_slippage(ratio) is False


def test_matcher_simulate_exit_defers_stop_until_next_tradable_day() -> None:
    config = BacktestMatcherConfig(reject_limit_down_sell=True)
    matcher = ExecutionMatcher(config)
    entry_date = datetime.fromisoformat("2026-03-01T14:50:00")

    future_bars = [
        (
            datetime.fromisoformat("2026-03-02T15:00:00"),
            {
                "open": 9.2,
                "high": 9.3,
                "low": 8.7,
                "close": 9.0,
                "down_limit": 9.0,
                "suspended": False,
            },
        ),
        (
            datetime.fromisoformat("2026-03-03T15:00:00"),
            {
                "open": 8.8,
                "high": 9.1,
                "low": 8.6,
                "close": 8.9,
                "down_limit": 8.0,
                "suspended": False,
            },
        ),
    ]

    result = matcher.simulate_exit(
        entry_price=10.0,
        entry_date=entry_date,
        future_bars=future_bars,
        take_profit_pct=0.05,
        stop_loss_pct=0.05,
    )
    assert result.executed is True
    assert result.reason == "stop_loss_deferred_fill"
    assert result.exit_price == 8.8
    assert result.deferred_days == 1


def test_matcher_limit_rule_versioning_uses_board_and_date() -> None:
    board_name = "\u521b\u4e1a\u677f"
    payload: dict[str, object] = {
        "use_source_first": False,
        "fallback_by_board": True,
        "rule_version_by_date": [
            {"from": "2015-01-01", "board": board_name, "limit_pct": 0.10},
            {"from": "2020-08-24", "board": board_name, "limit_pct": 0.20},
        ],
    }
    limit_rule = LimitRuleConfig.model_validate(payload)
    matcher = ExecutionMatcher(BacktestMatcherConfig(), limit_rule=limit_rule)

    pre_reform = matcher.can_buy(
        {
            "symbol": "300001",
            "board": board_name,
            "trade_date": "2019-06-03",
            "pre_close": 10.0,
            "close": 11.2,
            "suspended": False,
        }
    )
    post_reform = matcher.can_buy(
        {
            "symbol": "300001",
            "board": board_name,
            "trade_date": "2021-06-03",
            "pre_close": 10.0,
            "close": 11.2,
            "suspended": False,
        }
    )
    assert pre_reform.executable is False
    assert pre_reform.reason == "limit_up_reject"
    assert post_reform.executable is True


def test_matcher_stamp_tax_schedule_by_date() -> None:
    payload: dict[str, object] = {
        "cost_schedule_by_date": [
            {"from": "2015-01-01", "stamp_tax_rate": 0.0010},
            {"from": "2023-08-28", "stamp_tax_rate": 0.0005},
        ]
    }
    limit_rule = LimitRuleConfig.model_validate(payload)
    matcher = ExecutionMatcher(BacktestMatcherConfig(), limit_rule=limit_rule)
    pre_cut = matcher.estimate_cost(
        side="sell",
        price=10.0,
        quantity=1000,
        trade_date=datetime.fromisoformat("2023-08-01T14:30:00"),
    )
    post_cut = matcher.estimate_cost(
        side="sell",
        price=10.0,
        quantity=1000,
        trade_date=datetime.fromisoformat("2023-09-01T14:30:00"),
    )
    assert pre_cut > post_cut


def test_matcher_forced_liquidation_after_max_exit_carry_days() -> None:
    config = BacktestMatcherConfig(
        reject_limit_down_sell=True,
        max_exit_carry_days=2,
        forced_liquidation_discount_bp=50,
    )
    matcher = ExecutionMatcher(config)
    entry_date = datetime.fromisoformat("2026-03-01T14:50:00")

    future_bars = [
        (
            datetime.fromisoformat("2026-03-02T15:00:00"),
            {
                "open": 9.2,
                "high": 9.3,
                "low": 8.7,
                "close": 9.0,
                "down_limit": 9.0,
                "suspended": False,
            },
        ),
        (
            datetime.fromisoformat("2026-03-03T15:00:00"),
            {
                "open": 8.9,
                "high": 9.0,
                "low": 8.5,
                "close": 8.8,
                "down_limit": 8.8,
                "suspended": False,
            },
        ),
        (
            datetime.fromisoformat("2026-03-04T15:00:00"),
            {
                "open": 8.7,
                "high": 8.8,
                "low": 8.4,
                "close": 8.6,
                "down_limit": 8.6,
                "suspended": False,
            },
        ),
    ]
    result = matcher.simulate_exit(
        entry_price=10.0,
        entry_date=entry_date,
        future_bars=future_bars,
        take_profit_pct=0.05,
        stop_loss_pct=0.05,
        horizon_days=1,
    )
    assert result.executed is True
    assert result.reason == "forced_liquidation_max_carry"
    assert result.exit_no_fill is True
    assert result.forced_exit is True
    assert result.deferred_days == 3
    assert abs(result.exit_price - 8.55) < 1e-9
    assert result.forced_exit_close_price == 8.6
    assert result.forced_exit_close_date == datetime.fromisoformat("2026-03-04T15:00:00")


def test_matcher_max_hold_exit_no_fill_can_defer_and_fill() -> None:
    config = BacktestMatcherConfig(
        reject_limit_down_sell=True,
        max_exit_carry_days=3,
    )
    matcher = ExecutionMatcher(config)
    entry_date = datetime.fromisoformat("2026-03-01T14:50:00")

    future_bars = [
        (
            datetime.fromisoformat("2026-03-02T15:00:00"),
            {
                "open": 10.1,
                "high": 10.2,
                "low": 9.9,
                "close": 10.0,
                "down_limit": 9.0,
                "suspended": False,
            },
        ),
        (
            datetime.fromisoformat("2026-03-03T15:00:00"),
            {
                "open": 9.1,
                "high": 9.2,
                "low": 8.9,
                "close": 9.0,
                "down_limit": 9.0,
                "suspended": False,
            },
        ),
        (
            datetime.fromisoformat("2026-03-04T15:00:00"),
            {
                "open": 9.3,
                "high": 9.5,
                "low": 9.2,
                "close": 9.4,
                "down_limit": 8.4,
                "suspended": False,
            },
        ),
    ]
    result = matcher.simulate_exit(
        entry_price=10.0,
        entry_date=entry_date,
        future_bars=future_bars,
        take_profit_pct=0.30,
        stop_loss_pct=0.30,
        horizon_days=2,
    )
    assert result.executed is True
    assert result.reason == "max_hold_deferred_fill"
    assert result.exit_no_fill is True
    assert result.forced_exit is False
    assert result.exit_price == 9.3
    assert result.deferred_days == 1


def test_matcher_plan_order_applies_rounding_and_residual_policy() -> None:
    matcher = ExecutionMatcher(
        BacktestMatcherConfig(
            share_rounding_rule="lot_down_100",
            residual_order_policy="day_cancel_then_recalc",
            min_notional_per_order=0.0,
        )
    )
    plan = matcher.plan_order(side="buy", price=10.03, requested_quantity=1055)
    assert plan.executable is True
    assert plan.quantity == 1000
    assert plan.requested_quantity == 1055
    assert plan.residual_quantity == 55
    assert plan.residual_action == "day_cancel_then_recalc"


def test_matcher_plan_order_blocks_when_below_min_notional() -> None:
    matcher = ExecutionMatcher(
        BacktestMatcherConfig(
            min_notional_per_order=6000.0,
            share_rounding_rule="lot_down_100",
        )
    )
    plan = matcher.plan_order(side="buy", price=10.0, requested_quantity=500)
    assert plan.executable is False
    assert plan.trim_reason == "min_notional"
    assert plan.quantity == 0


def test_matcher_apply_slippage_respects_exchange_tick_rule() -> None:
    matcher = ExecutionMatcher(
        BacktestMatcherConfig(price_tick_rule="exchange_tick")
    )
    buy_fill = matcher.apply_slippage(price=10.001, side="buy", slippage_ratio=0.0001)
    sell_fill = matcher.apply_slippage(price=10.009, side="sell", slippage_ratio=0.0001)
    assert buy_fill == 10.01
    assert sell_fill == 10.0
