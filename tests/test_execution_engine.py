"""Unit tests for the shared execution engine (backtest/live common rules)."""

from __future__ import annotations

from datetime import datetime

import pytest

from stock_analyzer.backtest.matcher import ExecutionMatcher
from stock_analyzer.config import BacktestMatcherConfig, LimitRuleConfig
from stock_analyzer.execution.bar_adapter import market_payload_to_bar
from stock_analyzer.execution.engine import ExecutionEngine


def _default_engine() -> ExecutionEngine:
    return ExecutionEngine(BacktestMatcherConfig())


def test_engine_dynamic_slippage_uses_max_of_static_and_dynamic() -> None:
    engine = ExecutionEngine(
        BacktestMatcherConfig(
            slippage_by_strategy={"trend": 0.002},
            max_dynamic_slippage_ratio=0.012,
        )
    )
    ratio = engine.dynamic_slippage_ratio(
        strategy="trend",
        atr14=0.15,
        close=10.0,
        volume_ratio=2.0,
    )
    assert ratio > 0.002
    assert engine.should_downgrade_by_slippage(ratio) is False


def test_engine_dynamic_slippage_degrades_to_static_base_without_market_inputs() -> None:
    engine = _default_engine()
    base = engine.static_slippage_ratio(strategy="trend")
    assert base == pytest.approx(0.0015)
    degraded = engine.dynamic_slippage_ratio(
        strategy="trend",
        atr14=0.0,
        close=0.0,
        volume_ratio=1.0,
    )
    assert degraded == base
    assert engine.static_slippage_ratio(strategy="unknown_strategy") == 0.0


def test_engine_apply_slippage_respects_exchange_tick_rule() -> None:
    engine = ExecutionEngine(BacktestMatcherConfig(price_tick_rule="exchange_tick"))
    buy_fill = engine.apply_slippage(price=10.001, side="buy", slippage_ratio=0.0001)
    sell_fill = engine.apply_slippage(price=10.009, side="sell", slippage_ratio=0.0001)
    assert buy_fill == 10.01
    assert sell_fill == 10.0


def test_engine_apply_price_tick_ceil_buy_floor_sell() -> None:
    engine = _default_engine()
    assert engine.apply_price_tick(10.12525, side="buy") == 10.13
    assert engine.apply_price_tick(10.534175, side="sell") == 10.53
    assert engine.apply_price_tick(10.0, side="buy") == 10.0


def test_engine_plan_order_applies_rounding_and_residual_policy() -> None:
    engine = ExecutionEngine(
        BacktestMatcherConfig(
            share_rounding_rule="lot_down_100",
            residual_order_policy="day_cancel_then_recalc",
            min_notional_per_order=0.0,
        )
    )
    plan = engine.plan_order(side="buy", price=10.03, requested_quantity=1055)
    assert plan.executable is True
    assert plan.quantity == 1000
    assert plan.requested_quantity == 1055
    assert plan.residual_quantity == 55
    assert plan.residual_action == "day_cancel_then_recalc"


def test_engine_plan_order_blocks_when_below_min_notional() -> None:
    engine = ExecutionEngine(
        BacktestMatcherConfig(
            min_notional_per_order=6000.0,
            share_rounding_rule="lot_down_100",
        )
    )
    plan = engine.plan_order(side="buy", price=10.0, requested_quantity=500)
    assert plan.executable is False
    assert plan.trim_reason == "min_notional"
    assert plan.quantity == 0


def test_engine_share_rounding_unit_follows_config_rule() -> None:
    assert _default_engine().share_rounding_unit() == 100
    assert (
        ExecutionEngine(BacktestMatcherConfig(share_rounding_rule="none")).share_rounding_unit()
        == 1
    )


def test_engine_can_sell_enforces_t_plus_one() -> None:
    engine = _default_engine()
    bar = {"close": 10.0, "down_limit": 9.0, "suspended": False}
    buy_date = datetime.fromisoformat("2026-03-01T10:00:00")
    same_day = datetime.fromisoformat("2026-03-01T14:00:00")
    decision = engine.can_sell(bar=bar, last_buy_date=buy_date, current_date=same_day)
    assert decision.executable is False
    assert decision.reason == "t_plus_1_block"


def test_engine_can_sell_allows_next_day() -> None:
    engine = _default_engine()
    bar = {"close": 10.0, "down_limit": 9.0, "suspended": False}
    buy_date = datetime.fromisoformat("2026-03-01T10:00:00")
    next_day = datetime.fromisoformat("2026-03-02T14:00:00")
    decision = engine.can_sell(bar=bar, last_buy_date=buy_date, current_date=next_day)
    assert decision.executable is True
    assert decision.reason == "ok"


def test_engine_estimate_cost_contains_stamp_tax_on_sell() -> None:
    engine = _default_engine()
    buy_cost = engine.estimate_cost(side="buy", price=10.0, quantity=10000)
    sell_cost = engine.estimate_cost(side="sell", price=10.0, quantity=10000)
    assert sell_cost > buy_cost


def test_engine_estimate_cost_respects_date_versioned_stamp_tax() -> None:
    payload: dict[str, object] = {
        "cost_schedule_by_date": [
            {"from": "2015-01-01", "stamp_tax_rate": 0.0010},
            {"from": "2023-08-28", "stamp_tax_rate": 0.0005},
        ]
    }
    limit_rule = LimitRuleConfig.model_validate(payload)
    engine = ExecutionEngine(BacktestMatcherConfig(), limit_rule=limit_rule)
    pre_cut = engine.estimate_cost(
        side="sell",
        price=10.0,
        quantity=1000,
        trade_date=datetime.fromisoformat("2023-08-01T14:30:00"),
    )
    post_cut = engine.estimate_cost(
        side="sell",
        price=10.0,
        quantity=1000,
        trade_date=datetime.fromisoformat("2023-09-01T14:30:00"),
    )
    assert pre_cut > post_cut


def test_engine_rejects_invalid_matcher_config() -> None:
    with pytest.raises(ValueError, match="commission_rate"):
        ExecutionEngine(BacktestMatcherConfig(commission_rate=0.0))
    with pytest.raises(ValueError, match="stamp_tax_rate"):
        ExecutionEngine(BacktestMatcherConfig(stamp_tax_rate=-0.1))
    with pytest.raises(ValueError, match="transfer_fee_rate"):
        ExecutionEngine(BacktestMatcherConfig(transfer_fee_rate=-0.1))
    with pytest.raises(ValueError, match="min_commission_per_order"):
        ExecutionEngine(BacktestMatcherConfig(min_commission_per_order=1.0))
    with pytest.raises(ValueError, match="stamp_tax_apply_on"):
        ExecutionEngine(BacktestMatcherConfig(stamp_tax_apply_on="buy_only"))


def test_thin_shell_matches_engine_for_shared_rules() -> None:
    """ExecutionMatcher (backtest shell) must produce identical shared-rule outputs."""
    config = BacktestMatcherConfig()
    engine = ExecutionEngine(config)
    matcher = ExecutionMatcher(config)
    bar = {"close": 10.0, "down_limit": 9.0, "up_limit": 11.0, "suspended": False}
    buy_date = datetime.fromisoformat("2026-03-01T10:00:00")

    assert engine.can_sell(bar=bar, last_buy_date=buy_date, current_date=buy_date).reason == (
        matcher.can_sell(bar=bar, last_buy_date=buy_date, current_date=buy_date).reason
    )
    assert engine.dynamic_slippage_ratio("trend", 0.2, 10.0, 1.5) == matcher.dynamic_slippage_ratio(
        "trend", 0.2, 10.0, 1.5
    )
    assert engine.apply_slippage(10.1, "buy", 0.0015) == matcher.apply_slippage(10.1, "buy", 0.0015)
    engine_plan = engine.plan_order(side="buy", price=10.03, requested_quantity=1055)
    matcher_plan = matcher.plan_order(side="buy", price=10.03, requested_quantity=1055)
    assert (engine_plan.executable, engine_plan.quantity, engine_plan.residual_quantity) == (
        matcher_plan.executable,
        matcher_plan.quantity,
        matcher_plan.residual_quantity,
    )
    assert engine.estimate_cost(side="sell", price=10.0, quantity=1000) == matcher.estimate_cost(
        side="sell", price=10.0, quantity=1000
    )


def test_bar_adapter_maps_payload_to_bar_view() -> None:
    payload: dict[str, object] = {
        "name": "平安银行",
        "last_price": 10.08,
        "open_price": 10.0,
        "prev_close": 9.9,
        "latest_time": "2026-03-11T09:35:00+08:00",
        "bid_levels": [{"level": 1, "price": 10.0}],
        "ask_levels": [{"level": 1, "price": 10.1}],
    }
    bar = market_payload_to_bar(payload, symbol="000001")
    assert bar["close"] == 10.08
    assert bar["pre_close"] == 9.9
    assert bar["suspended"] is False
    assert bar["symbol"] == "000001"
    assert bar["up_limit"] is not None
    assert bar["down_limit"] is not None


def test_bar_adapter_close_fallback_chain() -> None:
    assert market_payload_to_bar({"open_price": 9.5, "prev_close": 9.4})["close"] == 9.5
    assert market_payload_to_bar({"prev_close": 9.4})["close"] == 9.4
    assert market_payload_to_bar({})["close"] == 0.0


def test_bar_adapter_computes_chi_next_board_20pct_limit() -> None:
    payload: dict[str, object] = {"last_price": 12.0, "prev_close": 10.0}
    bar = market_payload_to_bar(payload, symbol="300001")
    assert bar["up_limit"] == pytest.approx(12.0)
    assert bar["down_limit"] == pytest.approx(8.0)


def test_bar_adapter_computes_st_5pct_limit_via_name() -> None:
    payload: dict[str, object] = {"last_price": 10.5, "prev_close": 10.0, "name": "ST测试"}
    bar = market_payload_to_bar(payload, symbol="000001")
    assert bar["up_limit"] == pytest.approx(10.5)
    assert bar["down_limit"] == pytest.approx(9.5)


# ---------------------------------------------------------------------------
# P0 统一执行层风险门：结构化拒绝原因 + 无有效价格数据 fail-closed
# ---------------------------------------------------------------------------


def test_engine_can_buy_rejects_suspended_with_structured_details() -> None:
    engine = _default_engine()
    bar = {
        "close": 10.0,
        "up_limit": 11.0,
        "down_limit": 9.0,
        "suspended": True,
    }
    decision = engine.can_buy(bar=bar)
    assert decision.executable is False
    assert decision.reason == "suspended"
    assert decision.details.get("suspended") is True


def test_engine_can_sell_rejects_suspended_with_structured_details() -> None:
    engine = _default_engine()
    bar = {"close": 10.0, "up_limit": 11.0, "down_limit": 9.0, "suspended": True}
    now = datetime.fromisoformat("2026-03-11T14:50:00")
    buy = datetime.fromisoformat("2026-03-10T09:35:00")
    decision = engine.can_sell(bar=bar, last_buy_date=buy, current_date=now)
    assert decision.executable is False
    assert decision.reason == "suspended"
    assert decision.details.get("suspended") is True


def test_engine_can_buy_fails_closed_without_valid_price_data() -> None:
    """无 close / 无涨跌停推导数据时必须 fail-closed，而不是用 close±1 猜测。"""
    engine = _default_engine()
    # 无 pre_close、无 pct_change：build_price_limits 无法推导涨跌停
    decision = engine.can_buy(bar={"close": 10.0})
    assert decision.executable is False
    assert decision.reason == "no_valid_price_data"
    assert decision.details.get("up_limit") is None

    # close 缺失同样拒绝
    decision2 = engine.can_buy(bar={"up_limit": 11.0, "down_limit": 9.0})
    assert decision2.executable is False
    assert decision2.reason == "no_valid_price_data"


def test_engine_can_sell_fails_closed_without_valid_price_data() -> None:
    engine = _default_engine()
    now = datetime.fromisoformat("2026-03-11T14:50:00")
    buy = datetime.fromisoformat("2026-03-10T09:35:00")
    decision = engine.can_sell(bar={"close": 10.0}, last_buy_date=buy, current_date=now)
    assert decision.executable is False
    assert decision.reason == "no_valid_price_data"
    assert decision.details.get("down_limit") is None


def test_engine_can_buy_ok_includes_limit_prices_in_details() -> None:
    engine = _default_engine()
    bar = {"close": 10.0, "pre_close": 10.0, "up_limit": 11.0, "down_limit": 9.0}
    decision = engine.can_buy(bar=bar)
    assert decision.executable is True
    assert decision.reason == "ok"
    assert decision.details.get("close") == 10.0
    assert decision.details.get("up_limit") == 11.0


def test_engine_limit_up_reject_carries_close_and_limit() -> None:
    engine = _default_engine()
    bar = {"close": 11.0, "pre_close": 10.0, "up_limit": 11.0, "down_limit": 9.0}
    decision = engine.can_buy(bar=bar)
    assert decision.executable is False
    assert decision.reason == "limit_up_reject"
    assert decision.details.get("up_limit") == 11.0
    assert decision.details.get("close") == 11.0


def test_engine_limit_down_reject_carries_close_and_limit() -> None:
    engine = _default_engine()
    now = datetime.fromisoformat("2026-03-11T14:50:00")
    buy = datetime.fromisoformat("2026-03-10T09:35:00")
    bar = {"close": 9.0, "pre_close": 10.0, "up_limit": 11.0, "down_limit": 9.0}
    decision = engine.can_sell(bar=bar, last_buy_date=buy, current_date=now)
    assert decision.executable is False
    assert decision.reason == "limit_down_reject"
    assert decision.details.get("down_limit") == 9.0
