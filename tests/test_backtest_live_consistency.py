"""验收 #4 / #32 测试落地：回测 vs 线上撮合一致性（P1-#13 剩余项）。

目标（只读+测试，不改 src 实现）：
1. 引擎级一致性：同一输入（价格档位/信号/持仓/配置）下，回测 ExecutionMatcher
   （共享引擎薄壳）与线上 service 撮合路径（_apply_live_auto_portfolio_signals）
   的成交价、手数、费用输出一致。两种 shadow 模式：
   a) apply_dynamic_slippage_live=True：线上成交价 = 回测同 base 滑点规则
      （slippage_by_strategy 静态 base + exchange tick 取整），费用 = 引擎
      estimate_cost（含日期化印花税），price_source 带 "+slip" 标记；
   b) apply_dynamic_slippage_live=False（默认）：线上无滑点（取档位价、无
      "+slip"），手数与回测整手（lot_down_100）口径一致，费用一致。
2. 滑点公式一致性：walk_forward 经 matcher.dynamic_slippage_ratio 的调用链
   （ratio -> should_downgrade -> apply_slippage -> plan_order）与引擎直调输出一致，
   并锁定公式数值（防漂移）。
3. 统一执行层风险门契约（P0）：线上 sell/buy 委托路径与共享引擎
   ExecutionEngine.can_buy/can_sell 一致——涨停买入、跌停卖出、同日 T+1
   卖出均被同一风险门拒绝；回测（ExecutionMatcher）与线上（模拟委托）
   共用同一套可交易性判断，不再存在"线上放行、回测拦截"的静默漂移。
   min_notional 差异契约仍在（线上不执行 plan_order 拒单）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

import pytest

from stock_analyzer.backtest.matcher import ExecutionMatcher
from stock_analyzer.config import BacktestMatcherConfig
from stock_analyzer.execution.bar_adapter import market_payload_to_bar
from stock_analyzer.execution.engine import ExecutionEngine
from stock_analyzer.runtime.service import StockAnalyzerService
from stock_analyzer.types import PipelineSignal
from tests.test_service_portfolio import _load_test_config, _patch_attr

# 线上模拟盘初始资金（_simulation_initial_cash 取 config.dashboard.default_total_asset）
INITIAL_CASH = 100000.0


def _as_mapping_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise AssertionError(f"Expected list, got {type(value).__name__}")
    return [cast(dict[str, object], item) for item in value if isinstance(item, dict)]


def _as_float(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    raise AssertionError(f"Expected numeric value, got {value!r}")


def _as_int(value: object) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    raise AssertionError(f"Expected numeric value, got {value!r}")


def _buy_signal(
    *,
    symbol: str = "600000",
    strategy: str = "monster",
    target_position: float = 0.10,
) -> PipelineSignal:
    return PipelineSignal(
        symbol=symbol,
        strategy=strategy,
        score=86.0,
        grade="S",
        action="buy",
        target_position=target_position,
        probabilities={"lgbm": 0.8, "xgb": 0.8, "meta": 0.8},
        reasons=["soup_entry"],
    )


def _sell_signal(
    *,
    symbol: str = "600000",
    strategy: str = "trend",
) -> PipelineSignal:
    return PipelineSignal(
        symbol=symbol,
        strategy=strategy,
        score=30.0,
        grade="C",
        action=cast(Any, "sell"),
        target_position=0.0,
        probabilities={"lgbm": 0.2, "xgb": 0.2, "meta": 0.2},
        reasons=["sell_signal"],
    )


def _market_payload(
    *,
    ask: float,
    bid: float,
    last: float,
    prev_close: float | None = None,
) -> dict[str, object]:
    return {
        "last_price": last,
        "open_price": ask,
        "prev_close": prev_close if prev_close is not None else last,
        "ask_levels": [{"level": 1, "price": ask, "volume": 5000}],
        "bid_levels": [{"level": 1, "price": bid, "volume": 5000}],
    }


def _patch_market(
    service: StockAnalyzerService,
    payload: dict[str, object],
    symbol: str = "600000",
) -> None:
    _patch_attr(service, "_build_c3_position_management_items", lambda **kwargs: [])
    _patch_attr(
        service,
        "_build_week5_symbol_market_payload",
        lambda **kwargs: dict(payload),
    )
    _patch_attr(
        service,
        "_fetch_market_depth_snapshots",
        lambda **kwargs: {
            symbol: {
                "available": True,
                "ask_levels": payload["ask_levels"],
                "bid_levels": payload["bid_levels"],
            }
        },
    )


# ---------------------------------------------------------------------------
# 1. 引擎级一致性：买入（两种 shadow 模式）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "shadow_enabled,expected_price_source",
    [
        (False, "五档卖1"),       # 默认：线上无滑点，价格取档位价，无 +slip 标记
        (True, "五档卖1+slip"),   # shadow 开：应用共享滑点 + tick 取整
    ],
)
def test_live_buy_price_quantity_fee_match_backtest_both_modes(
    shadow_enabled: bool,
    expected_price_source: str,
) -> None:
    """验收 #4/#32：线上买入（信号撮合全路径）与回测同规则输出一致。

    断言：成交价 = 回测 apply_slippage（同一 base：shadow 开=静态 base，
    shadow 关=0 滑点；均含 exchange tick）；手数 = 回测 plan_order 整手口径
    （同一请求量向下取整到 100 股）；费用 = 引擎 estimate_cost（四舍五入到分）。
    """
    config = _load_test_config()
    config.backtest_matcher.apply_dynamic_slippage_live = shadow_enabled
    service = StockAnalyzerService(config=config)
    engine = service._shared_execution_engine()
    matcher = ExecutionMatcher(config.backtest_matcher, limit_rule=config.limit_rule)

    strategy = "monster"
    ask = 10.1
    target_position = 0.10
    timestamp = datetime.fromisoformat("2026-03-11T09:35:00")

    # 回测同规则参考价：同一 slippage_by_strategy base + tick 取整
    # （matcher 薄壳与 engine 共用同一引擎，base 以引擎为准）
    ratio = engine.static_slippage_ratio(strategy) if shadow_enabled else 0.0
    expected_price = matcher.apply_slippage(price=ask, side="buy", slippage_ratio=ratio)

    _patch_market(service, _market_payload(ask=ask, bid=10.0, last=10.08))
    update = service._apply_live_auto_portfolio_signals(
        trace_id="trace-consistency-buy",
        timestamp=timestamp,
        signals=[_buy_signal(strategy=strategy, target_position=target_position)],
        use_live_runtime=True,
    )

    assert update["status"] == "simulated_auto_applied"
    assert update["opened"] == 1
    executions = _as_mapping_list(update["executions"])
    buy = next(item for item in executions if item["side"] == "buy")
    position = service.portfolio_positions()[0]

    # 成交价：线上 = 回测同滑点规则（同一 base + tick 取整）
    assert _as_float(buy["price"]) == pytest.approx(expected_price)
    assert _as_float(position["entry_price"]) == pytest.approx(expected_price)
    # 线上滑点标记：shadow 开带 +slip，默认模式无
    assert str(buy["price_source"]) == expected_price_source

    # 手数：线上整手口径 = 回测 plan_order（同一请求量、lot_down_100）
    desired_cash = min(INITIAL_CASH * target_position, INITIAL_CASH)
    requested = int(desired_cash // expected_price)
    plan = matcher.plan_order(side="buy", price=expected_price, requested_quantity=requested)
    assert plan.executable is True
    assert _as_int(buy["quantity"]) == plan.quantity
    assert _as_int(buy["quantity"]) % engine.share_rounding_unit() == 0

    # 费用：线上 = 引擎 estimate_cost（回测 matcher 同口径，含日期化印花税）取整到分
    backtest_fee = matcher.estimate_cost(
        side="buy",
        price=expected_price,
        quantity=plan.quantity,
        trade_date=timestamp,
    )
    engine_fee = engine.estimate_cost(
        side="buy",
        price=expected_price,
        quantity=plan.quantity,
        trade_date=timestamp,
    )
    assert _as_float(buy["fee"]) == pytest.approx(round(backtest_fee, 2))
    assert _as_float(buy["fee"]) == pytest.approx(round(engine_fee, 2))


# ---------------------------------------------------------------------------
# 2. 引擎级一致性：卖出（两种 shadow 模式）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "shadow_enabled,expected_price_source",
    [
        (False, "五档买1"),       # 默认：线上无滑点，价格取档位价，无 +slip 标记
        (True, "五档买1+slip"),   # shadow 开：应用共享滑点 + tick 取整
    ],
)
def test_live_sell_price_quantity_fee_match_backtest_both_modes(
    shadow_enabled: bool,
    expected_price_source: str,
) -> None:
    """验收 #4/#32：线上卖出（sell 信号路径）与回测同规则输出一致。

    与买入同口径：成交价（base 滑点 + tick）、手数（整手）、费用（含印花税）。
    """
    config = _load_test_config()
    config.backtest_matcher.apply_dynamic_slippage_live = shadow_enabled
    service = StockAnalyzerService(config=config)
    engine = service._shared_execution_engine()
    matcher = ExecutionMatcher(config.backtest_matcher, limit_rule=config.limit_rule)

    strategy = "trend"
    bid = 10.55
    quantity = 900
    opened_at = datetime.fromisoformat("2026-03-10T09:35:00")
    timestamp = datetime.fromisoformat("2026-03-11T14:50:00")

    service._portfolio.set_manual_position(
        symbol="600000",
        strategy=strategy,
        target_position=0.10,
        timestamp=opened_at,
        trace_id="seed-consistency-sell",
        reason="auto_simulated_buy",
        manual_fill={"entry_price": 10.0, "quantity": quantity},
    )

    # 回测同规则参考价：同一 slippage_by_strategy base + tick 取整
    # （matcher 薄壳与 engine 共用同一引擎，base 以引擎为准）
    ratio = engine.static_slippage_ratio(strategy) if shadow_enabled else 0.0
    expected_price = matcher.apply_slippage(price=bid, side="sell", slippage_ratio=ratio)

    _patch_market(service, _market_payload(ask=10.6, bid=bid, last=10.58))
    update = service._apply_live_auto_portfolio_signals(
        trace_id="trace-consistency-sell",
        timestamp=timestamp,
        signals=[_sell_signal(strategy=strategy)],
        use_live_runtime=True,
    )

    assert update["status"] == "simulated_auto_applied"
    assert update["closed_signals"] == 1
    assert len(service.portfolio_positions()) == 0
    executions = _as_mapping_list(update["executions"])
    sell = next(item for item in executions if item["side"] == "sell")

    # 成交价：线上 = 回测同滑点规则（同一 base + tick 取整）
    assert _as_float(sell["price"]) == pytest.approx(expected_price)
    assert str(sell["price_source"]) == expected_price_source

    # 手数：持仓整手（900）在回测 plan_order 下同样可执行且数量一致
    plan = matcher.plan_order(side="sell", price=expected_price, requested_quantity=quantity)
    assert plan.executable is True
    assert _as_int(sell["quantity"]) == plan.quantity == quantity

    # 费用：线上 = 引擎 estimate_cost（sell 含日期化印花税）取整到分
    backtest_fee = matcher.estimate_cost(
        side="sell",
        price=expected_price,
        quantity=plan.quantity,
        trade_date=timestamp,
    )
    engine_fee = engine.estimate_cost(
        side="sell",
        price=expected_price,
        quantity=plan.quantity,
        trade_date=timestamp,
    )
    assert _as_float(sell["fee"]) == pytest.approx(round(backtest_fee, 2))
    assert _as_float(sell["fee"]) == pytest.approx(round(engine_fee, 2))


# ---------------------------------------------------------------------------
# 3. 线上路径委托共享引擎（滑点/手数/费用）直查
# ---------------------------------------------------------------------------


def test_service_slippage_fee_lot_size_delegate_to_shared_engine() -> None:
    """线上 _apply_shared_slippage / _estimate_simulated_trade_fee / _simulation_lot_size
    的输出与共享引擎直调一致（两种模式），并与回测 matcher 同口径对照。"""
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    matcher = ExecutionMatcher(config.backtest_matcher, limit_rule=config.limit_rule)

    # 默认模式（apply_dynamic_slippage_live=False）：价格原样返回，无 +slip 标记
    price, source = service._apply_shared_slippage(
        side="buy",
        strategy="trend",
        price=10.1,
        price_source="五档卖1",
    )
    assert price == 10.1
    assert source == "五档卖1"

    # shadow 开：= engine.apply_slippage(静态 base) + "+slip" 标记
    config.backtest_matcher.apply_dynamic_slippage_live = True
    service_shadow = StockAnalyzerService(config=config)
    engine_shadow = service_shadow._shared_execution_engine()
    expected = engine_shadow.apply_slippage(
        price=10.1,
        side="buy",
        slippage_ratio=engine_shadow.static_slippage_ratio("monster"),
    )
    price_shadow, source_shadow = service_shadow._apply_shared_slippage(
        side="buy",
        strategy="monster",
        price=10.1,
        price_source="五档卖1",
    )
    assert price_shadow == expected
    assert source_shadow == "五档卖1+slip"
    # 与回测 matcher 同 base 规则一致（base 以共享引擎为准）
    assert expected == matcher.apply_slippage(
        price=10.1,
        side="buy",
        slippage_ratio=matcher.dynamic_slippage_ratio(
            strategy="monster",
            atr14=0.0,
            close=0.0,
            volume_ratio=1.0,
        ),
    )
    assert expected == matcher.apply_slippage(
        price=10.1,
        side="buy",
        slippage_ratio=engine_shadow.static_slippage_ratio("monster"),
    )
    # 非正价格不应用滑点（即使是 shadow 模式）
    price_zero, source_zero = service_shadow._apply_shared_slippage(
        side="sell",
        strategy="trend",
        price=0.0,
        price_source="最新价",
    )
    assert price_zero == 0.0
    assert source_zero == "最新价"

    # 手数：线上 lot size = 共享引擎 share_rounding_unit
    assert service_shadow._simulation_lot_size() == engine_shadow.share_rounding_unit()

    # 费用：= round(引擎 estimate_cost(notional, quantity=1), 2) == 回测同口径取整
    timestamp = datetime.fromisoformat("2026-03-11T14:50:00")
    fee = service_shadow._estimate_simulated_trade_fee(
        side="sell",
        notional=9477.0,
        trade_date=timestamp,
    )
    engine_fee = engine_shadow.estimate_cost(
        side="sell",
        price=9477.0,
        quantity=1,
        trade_date=timestamp,
    )
    matcher_fee = matcher.estimate_cost(
        side="sell",
        price=10.53,
        quantity=900,
        trade_date=timestamp,
    )
    assert fee == pytest.approx(round(engine_fee, 2))
    assert fee == pytest.approx(round(matcher_fee, 2))


# ---------------------------------------------------------------------------
# 4. 滑点公式一致性：walk_forward 调用链 vs 引擎直调
# ---------------------------------------------------------------------------


def test_dynamic_slippage_formula_walk_forward_chain_matches_engine_direct() -> None:
    """walk_forward 的调用链（dynamic_slippage_ratio -> should_downgrade ->
    apply_slippage -> plan_order）与共享引擎直调输出完全一致（同一 bar 数据）。"""
    config = _load_test_config()
    engine = ExecutionEngine(config.backtest_matcher, limit_rule=config.limit_rule)
    matcher = ExecutionMatcher(config.backtest_matcher, limit_rule=config.limit_rule)

    cases = [
        ("trend", 0.15, 10.0, 2.0),    # 动态项 > 静态 base
        ("trend", 0.01, 10.0, 1.0),    # 动态项 < 静态 base -> 取 base
        ("monster", 0.30, 8.5, 0.5),   # volume_ratio < 1 使动态项为负 -> 截断 0
        ("unknown", 0.20, 5.0, 3.0),   # 无配置 base = 0
    ]
    for strategy, atr14, close, volume_ratio in cases:
        ratio_wf = matcher.dynamic_slippage_ratio(
            strategy=strategy,
            atr14=atr14,
            close=close,
            volume_ratio=volume_ratio,
        )
        ratio_direct = engine.dynamic_slippage_ratio(
            strategy=strategy,
            atr14=atr14,
            close=close,
            volume_ratio=volume_ratio,
        )
        assert ratio_wf == pytest.approx(ratio_direct)
        assert matcher.should_downgrade_by_slippage(ratio_wf) == (
            engine.should_downgrade_by_slippage(ratio_direct)
        )
        for side in ("buy", "sell"):
            assert matcher.apply_slippage(price=close, side=side, slippage_ratio=ratio_wf) == (
                engine.apply_slippage(price=close, side=side, slippage_ratio=ratio_direct)
            )
        assert matcher.plan_order(
            side="buy", price=close, requested_quantity=1000
        ).quantity == engine.plan_order(
            side="buy", price=close, requested_quantity=1000
        ).quantity


def test_dynamic_slippage_formula_value_is_locked() -> None:
    """锁定动态滑点公式数值：max(base, (atr14/close)*0.35 + (volume_ratio-1)*0.001)，
    防止公式漂移导致回测/线上收益口径静默变化。"""
    config = BacktestMatcherConfig(slippage_by_strategy={"trend": 0.0015})
    engine = ExecutionEngine(config)
    # (0.15/10)*0.35 + (2-1)*0.001 = 0.00525 + 0.001 = 0.00625 > base
    assert engine.dynamic_slippage_ratio("trend", 0.15, 10.0, 2.0) == pytest.approx(0.00625)
    # 动态项小于静态 base 时退化为 base
    assert engine.dynamic_slippage_ratio("trend", 0.01, 10.0, 1.0) == pytest.approx(0.0015)
    # 无有效市场输入时退化为 base
    assert engine.dynamic_slippage_ratio("trend", 0.0, 0.0, 1.0) == pytest.approx(0.0015)
    assert engine.dynamic_slippage_ratio("trend", 0.15, -1.0, 2.0) == pytest.approx(0.0015)


def test_dynamic_slippage_disabled_degrades_to_static_base_both_sides() -> None:
    """dynamic_slippage_enabled=False 时，回测 matcher 与引擎直调均退化为静态 base。"""
    config = _load_test_config()
    config.backtest_matcher.dynamic_slippage_enabled = False
    engine = ExecutionEngine(config.backtest_matcher, limit_rule=config.limit_rule)
    matcher = ExecutionMatcher(config.backtest_matcher, limit_rule=config.limit_rule)
    for strategy in ("trend", "monster", "unknown"):
        assert matcher.dynamic_slippage_ratio(
            strategy, 0.2, 10.0, 5.0
        ) == pytest.approx(engine.static_slippage_ratio(strategy))
        assert engine.dynamic_slippage_ratio(
            strategy, 0.2, 10.0, 5.0
        ) == pytest.approx(engine.static_slippage_ratio(strategy))


# ---------------------------------------------------------------------------
# 5. 差异契约锁定（P2 未统一项）：断言线上当前不做拦截
# ---------------------------------------------------------------------------


def test_contract_p2_live_blocks_same_day_sell_like_shared_engine() -> None:
    """契约（P0 统一执行层风险门）：线上与共享引擎一致拦截同日 T+1 卖出。

    历史版本锁定过"线上同日卖出不拦截"的现状；P0 统一后线上 sell 路径
    直接经过 ExecutionEngine.can_sell，与回测共享同一风险门。
    """
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    engine = service._shared_execution_engine()
    opened_at = datetime.fromisoformat("2026-03-11T09:35:00")
    close_ts = datetime.fromisoformat("2026-03-11T14:50:00")

    service._portfolio.set_manual_position(
        symbol="600000",
        strategy="trend",
        target_position=0.10,
        timestamp=opened_at,
        trace_id="seed-t1-contract",
        reason="auto_simulated_buy",
        manual_fill={"entry_price": 10.0, "quantity": 900},
    )
    payload = _market_payload(ask=10.0, bid=9.9, last=10.0)
    _patch_market(service, payload)

    update = service._apply_live_auto_portfolio_signals(
        trace_id="trace-t1-contract",
        timestamp=close_ts,
        signals=[_sell_signal()],
        use_live_runtime=True,
    )

    # 线上与共享引擎一致：同日卖出被 T+1 拦截，持仓保留
    assert update["closed_signals"] == 0
    assert len(service.portfolio_positions()) == 1
    assert update["execution_attempts"]["engine_risk_gate_blocked"] == 1

    # 对照：共享引擎对同一输入同样拦截 T+1
    bar = market_payload_to_bar(dict(payload), symbol="600000", limit_rule=config.limit_rule)
    decision = engine.can_sell(bar=bar, last_buy_date=opened_at, current_date=close_ts)
    assert decision.executable is False
    assert decision.reason == "t_plus_1_block"


def test_contract_p2_live_blocks_limit_up_buy_like_shared_engine() -> None:
    """契约（P0 统一执行层风险门）：线上统一拒绝涨停买入。

    历史版本锁定过"允许涨停买入"的现状（TODO P2）；本轮按 PLAN 改为
    正式拒绝契约：price == up_limit 时线上 buy 路径直接拒绝成交。
    """
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    engine = service._shared_execution_engine()
    timestamp = datetime.fromisoformat("2026-03-11T09:35:00")

    # last_price == 涨停价（prev_close 10.0 的 10% 上限 11.0）
    payload = _market_payload(ask=11.0, bid=10.9, last=11.0, prev_close=10.0)
    _patch_market(service, payload)

    update = service._apply_live_auto_portfolio_signals(
        trace_id="trace-limit-up-contract",
        timestamp=timestamp,
        signals=[_buy_signal(strategy="trend", target_position=0.05)],
        use_live_runtime=True,
    )

    # 线上现状：涨停价买入被共享引擎风险门拒绝（limit_up_reject）
    assert update["opened"] == 0
    assert update["execution_attempts"]["engine_risk_gate_blocked"] == 1
    blocked = [
        item
        for item in _as_mapping_list(update["executions"])
        if item["side"] == "buy" and item["status"] == "rejected_execution_gate"
    ]
    assert len(blocked) == 1
    assert "limit_up_reject" in str(blocked[0]["reason"])

    # 对照：共享引擎对同一输入拒绝买入（limit_up_reject）
    bar = market_payload_to_bar(dict(payload), symbol="600000", limit_rule=config.limit_rule)
    decision = engine.can_buy(bar=bar)
    assert decision.executable is False
    assert decision.reason == "limit_up_reject"
    assert decision.details.get("up_limit") == pytest.approx(11.0)


def test_contract_p2_live_blocks_limit_down_sell_like_shared_engine() -> None:
    """契约（P0 统一执行层风险门）：线上统一拒绝跌停卖出。

    历史版本锁定过"线上跌停卖出不拦截"的现状；P0 统一后线上 sell 路径
    经过 ExecutionEngine.can_sell，跌停价卖出被拒绝。
    """
    config = _load_test_config()
    service = StockAnalyzerService(config=config)
    engine = service._shared_execution_engine()
    opened_at = datetime.fromisoformat("2026-03-10T09:35:00")
    close_ts = datetime.fromisoformat("2026-03-11T14:50:00")

    service._portfolio.set_manual_position(
        symbol="600000",
        strategy="trend",
        target_position=0.10,
        timestamp=opened_at,
        trace_id="seed-limit-down-contract",
        reason="auto_simulated_buy",
        manual_fill={"entry_price": 10.0, "quantity": 900},
    )
    # last_price == 跌停价（prev_close 10.0 的 10% 下限 9.0）
    payload = _market_payload(ask=9.1, bid=9.0, last=9.0, prev_close=10.0)
    _patch_market(service, payload)

    update = service._apply_live_auto_portfolio_signals(
        trace_id="trace-limit-down-contract",
        timestamp=close_ts,
        signals=[_sell_signal()],
        use_live_runtime=True,
    )

    # 线上与共享引擎一致：跌停价卖出被拒绝，持仓保留
    assert update["closed_signals"] == 0
    assert len(service.portfolio_positions()) == 1
    assert update["execution_attempts"]["engine_risk_gate_blocked"] == 1

    # 对照：共享引擎对同一输入拒绝卖出（limit_down_reject）
    bar = market_payload_to_bar(dict(payload), symbol="600000", limit_rule=config.limit_rule)
    decision = engine.can_sell(bar=bar, last_buy_date=opened_at, current_date=close_ts)
    assert decision.executable is False
    assert decision.reason == "limit_down_reject"
    assert decision.details.get("down_limit") == pytest.approx(9.0)


def test_contract_p2_live_does_not_enforce_min_notional() -> None:
    """契约锁定（TODO P2）：线上未接入 min_notional 拒单。

    现状：小额买入（notional < min_notional_per_order）线上仍成交；
    共享引擎 plan_order 对同一输入返回 min_notional 拒单。锁定现状防漂移。
    """
    config = _load_test_config()  # min_notional_per_order 默认 5000
    service = StockAnalyzerService(config=config)
    engine = service._shared_execution_engine()
    timestamp = datetime.fromisoformat("2026-03-11T09:35:00")

    # 低价股小额买入：desired_cash=1000 -> 200 股 -> notional 800 < 5000
    payload = _market_payload(ask=4.0, bid=3.98, last=4.0)
    _patch_market(service, payload)

    update = service._apply_live_auto_portfolio_signals(
        trace_id="trace-min-notional-contract",
        timestamp=timestamp,
        signals=[_buy_signal(strategy="trend", target_position=0.01)],
        use_live_runtime=True,
    )

    # 线上现状：低于 min_notional 仍成交
    assert update["opened"] == 1
    buy = next(item for item in _as_mapping_list(update["executions"]) if item["side"] == "buy")
    assert _as_int(buy["quantity"]) == 200
    assert _as_float(buy["price"]) * _as_int(buy["quantity"]) < 5000.0

    # 对照：共享引擎对同一输入按 min_notional 拒单
    plan = engine.plan_order(side="buy", price=4.0, requested_quantity=200)
    assert plan.executable is False
    assert plan.trim_reason == "min_notional"
