"""trend 尾盘策略契约的工程验收用例（改进计划 §4「工程验收」逐条覆盖）。

覆盖：时区跨日、节假日、尾盘窗口、确认后成交、涨跌停、停牌、T+1、跳空止损、
双触发、未成熟退出、顺延退出、最低佣金、按日期冻结成本、特征缺失、模型身份异常，
以及"线上路径与历史路径在同一输入下判定必须一致"。
"""

from __future__ import annotations

import pathlib
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from stock_analyzer.config import (
    AsofBacktestConfig,
    BacktestMatcherConfig,
    CostScheduleEntry,
    LabelsConfig,
    LimitRuleConfig,
    SoupStrategyConfig,
    TrendStrategyConfig,
)
from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    NET_PROFIT_PROBABILITY_FIELD,
    STATUS_FILLED,
    STATUS_NOT_FILLED,
    STATUS_UNCERTAIN,
    ModelIdentity,
    TrendContractError,
    TrendStrategyContract,
    audit_strategy_contract_conflicts,
    buy_lot_quantity,
    completed_bars,
    contract_from_config,
    evaluate_tail_entry,
    label_maturity_time,
    rank_final_recommendations,
    simulate_tail_exit,
    summarize_tail_trade,
)
from stock_analyzer.execution.engine import ExecutionEngine
from stock_analyzer.market_calendar import is_a_share_trading_day

TRADE_DAY = date(2026, 10, 8)
NEXT_DAY = date(2026, 10, 9)

CONTRACT = DEFAULT_TREND_CONTRACT


def _bars(prices: dict[str, float], *, day: date = TRADE_DAY, **extra) -> list:
    """按契约约定：元组里的 datetime 是这根 1 分钟 bar 的**完成**时刻。"""
    return [
        (datetime(day.year, day.month, day.day, int(hhmm[:2]), int(hhmm[3:])),
         dict({"open": price, "high": price, "low": price, "close": price,
               "trade_status": "normal", **extra}))
        for hhmm, price in prices.items()
    ]


def _always_confirm(context) -> tuple[bool, str]:
    return True, ""


def _never_confirm(context) -> tuple[bool, str]:
    return False, "momentum_faded"


def _daily(day: date, *, open_, high, low, close, **extra) -> tuple:
    return (day, {"open": open_, "high": high, "low": low, "close": close,
                  "trade_status": "normal", **extra})


# ---------------------------------------------------------------------------
# 契约自身：口径固定 + fail-closed
# ---------------------------------------------------------------------------


def test_contract_pins_first_round_business_terms() -> None:
    assert CONTRACT.take_profit_pct == pytest.approx(0.08)
    assert CONTRACT.stop_loss_pct == pytest.approx(0.05)
    assert CONTRACT.holding_days == 5
    assert CONTRACT.entry_day_counts_as_holding_day is True
    assert CONTRACT.reference_notional == pytest.approx(10_000.0)
    assert CONTRACT.max_final_recommendations == 3
    assert CONTRACT.min_net_profit_probability == pytest.approx(0.60)
    assert CONTRACT.strategy == "trend"
    assert NET_PROFIT_PROBABILITY_FIELD == "p_net_profit_5d_tail"


def test_tail_window_slots_start_inclusive_end_exclusive() -> None:
    assert CONTRACT.confirmation_slots == ("14:30", "14:35", "14:40", "14:45")
    assert CONTRACT.latest_confirmation_clock == "14:45"


def test_contract_rejects_monster_qfq_and_open_entry_semantics() -> None:
    with pytest.raises(TrendContractError):
        TrendStrategyContract(strategy="monster")
    with pytest.raises(TrendContractError):
        TrendStrategyContract(execution_price_basis="qfq")
    with pytest.raises(TrendContractError):
        TrendStrategyContract(same_bar_conflict_policy="take_profit_first")
    with pytest.raises(TrendContractError):
        TrendStrategyContract(holding_days=1)
    with pytest.raises(TrendContractError):
        TrendStrategyContract(entry_window_start="14:50", entry_window_end="14:50")


def test_contract_digest_is_stable_and_semantic() -> None:
    assert CONTRACT.digest() == TrendStrategyContract().digest()
    assert CONTRACT.digest() != TrendStrategyContract(holding_days=10).digest()


def test_contract_from_config_requires_the_block() -> None:
    with pytest.raises(TrendContractError):
        contract_from_config(SimpleNamespace())
    built = contract_from_config(SimpleNamespace(trend_strategy=TrendStrategyConfig()))
    assert built.digest() == CONTRACT.digest()


# ---------------------------------------------------------------------------
# 时区 / 交易日 / 节假日
# ---------------------------------------------------------------------------


def test_confirmation_datetimes_are_local_to_the_trading_day() -> None:
    slots = CONTRACT.confirmation_datetimes(TRADE_DAY)
    assert slots[0] == datetime(2026, 10, 8, 14, 30)
    assert slots[-1] == datetime(2026, 10, 8, 14, 45)
    assert all(slot.tzinfo is None for slot in slots)


def test_holiday_and_non_trading_day_are_not_tail_days() -> None:
    # 2026-10-10 是周六；2026-10-01 是国庆休市日 —— 都不该跑尾盘确认。
    assert is_a_share_trading_day(date(2026, 10, 10)) is False
    assert is_a_share_trading_day(date(2026, 10, 1)) is False
    assert is_a_share_trading_day(TRADE_DAY) is True


def test_cross_day_bars_do_not_leak_into_the_tail_window() -> None:
    """当日窗口结束后（含跨到次日）的 bar 不得成为当日成交来源。"""
    bars = _bars({"14:30": 10.0, "14:31": 10.05}, up_limit=11.0) + _bars(
        {"09:31": 20.0}, day=NEXT_DAY, up_limit=22.0
    )
    decision = evaluate_tail_entry(
        symbol="600000.SH",
        trading_day=TRADE_DAY,
        minute_bars=bars,
        confirmation=_always_confirm,
        quote_as_of=datetime(2026, 10, 8, 14, 30),
    )
    assert decision.confirmed is True
    assert decision.no_fill_reason == "no_completed_bar_after_confirmation"
    assert decision.fill_price_raw is None


# ---------------------------------------------------------------------------
# 确认 → 成交：只读已完成 bar，成交用之后那一根
# ---------------------------------------------------------------------------


def test_fill_uses_the_next_completed_bar_after_confirmation_not_the_slot_bar() -> None:
    bars = _bars({"14:25": 9.9, "14:30": 10.0, "14:31": 10.2}, up_limit=11.0)
    decision = evaluate_tail_entry(
        symbol="600000.SH",
        trading_day=TRADE_DAY,
        minute_bars=bars,
        confirmation=_always_confirm,
    )
    assert decision.confirmed and decision.filled
    assert decision.confirmation_slot == datetime(2026, 10, 8, 14, 30)
    assert decision.fill_time == datetime(2026, 10, 8, 14, 31)
    # 成交价来自 14:31 那根，不是确认时点 14:30 的 close（那是自成交 look-ahead）。
    assert decision.fill_price_raw == pytest.approx(10.2)
    assert decision.quantity == 900  # 10000 / 10.2 = 980 → 整手 900
    assert decision.entry_amount == pytest.approx(900 * 10.2)


def test_confirmation_never_reads_an_unfinished_bar() -> None:
    seen: list[int] = []

    def spy(context):
        seen.append(len(context.completed))
        assert all(end <= context.confirmation_slot for end, _ in context.completed)
        return False, "not_strength"

    evaluate_tail_entry(
        symbol="600000.SH",
        trading_day=TRADE_DAY,
        minute_bars=_bars({"14:29": 10.0, "14:30": 10.0, "14:44": 10.1, "14:46": 10.3}),
        confirmation=spy,
    )
    assert seen == [2, 2, 2, 3]


def test_not_confirmed_is_reported_as_not_filled_not_as_a_loss() -> None:
    decision = evaluate_tail_entry(
        symbol="600000.SH",
        trading_day=TRADE_DAY,
        minute_bars=_bars({"14:30": 10.0, "14:31": 10.0}),
        confirmation=_never_confirm,
    )
    summary = summarize_tail_trade(entry=decision, exit_result=None)
    assert summary["status"] == STATUS_NOT_FILLED
    assert summary["net_profit"] is None
    assert summary["reason"] == "momentum_faded"


def test_live_and_history_paths_agree_on_identical_input() -> None:
    """计划 §4：相同输入下线上与历史路径必须给出一致的筛选与交易判定。"""
    bars = _bars({"14:25": 9.8, "14:30": 10.0, "14:31": 10.05, "14:35": 10.1,
                  "14:36": 10.2, "14:40": 10.3, "14:41": 10.4}, up_limit=11.0)
    live = evaluate_tail_entry(
        symbol="600000.SH", trading_day=TRADE_DAY, minute_bars=bars,
        confirmation=_always_confirm,
        quote_as_of=datetime(2026, 10, 8, 14, 41),
    )
    history = evaluate_tail_entry(
        symbol="600000.SH", trading_day=TRADE_DAY, minute_bars=bars,
        confirmation=_always_confirm,
    )
    assert (live.confirmed, live.fill_time, live.fill_price_raw, live.quantity) == (
        history.confirmed, history.fill_time, history.fill_price_raw, history.quantity
    )


def test_stale_live_quote_blocks_confirmation_instead_of_using_old_bar() -> None:
    decision = evaluate_tail_entry(
        symbol="600000.SH",
        trading_day=TRADE_DAY,
        minute_bars=_bars({"14:30": 10.0}),
        confirmation=_always_confirm,
        quote_as_of=datetime(2026, 10, 8, 14, 45),
    )
    assert decision.confirmed is False
    assert decision.reason == "realtime_snapshot_stale"


# ---------------------------------------------------------------------------
# 交易资格硬门：停牌 / 涨跌停 / 缺数据
# ---------------------------------------------------------------------------


def test_suspended_fill_bar_is_no_fill_but_not_a_loss() -> None:
    decision = evaluate_tail_entry(
        symbol="600000.SH", trading_day=TRADE_DAY,
        minute_bars=_bars({"14:30": 10.0, "14:31": 10.05}, suspended=True),
        confirmation=_always_confirm,
    )
    assert decision.confirmed and decision.no_fill_reason == "suspended"
    assert decision.quantity == 0


def test_limit_up_locked_fill_is_no_fill() -> None:
    decision = evaluate_tail_entry(
        symbol="600000.SH", trading_day=TRADE_DAY,
        minute_bars=_bars({"14:30": 10.0, "14:31": 11.0}, up_limit=11.0),
        confirmation=_always_confirm,
    )
    assert decision.no_fill_reason == "limit_up_locked"


def test_missing_price_or_limit_data_fails_closed_without_guessing() -> None:
    decision = evaluate_tail_entry(
        symbol="600000.SH", trading_day=TRADE_DAY,
        minute_bars=_bars({"14:30": 10.0, "14:31": 10.05}),
        confirmation=_always_confirm,
    )
    assert decision.no_fill_reason == "no_valid_price_data"


def test_missing_bar_is_not_reported_as_suspension() -> None:
    decision = evaluate_tail_entry(
        symbol="600000.SH", trading_day=TRADE_DAY,
        minute_bars=_bars({"14:30": 10.0}),
        confirmation=_always_confirm,
    )
    assert decision.no_fill_reason == "no_completed_bar_after_confirmation"


def test_reference_notional_below_one_lot_is_no_fill() -> None:
    decision = evaluate_tail_entry(
        symbol="600000.SH", trading_day=TRADE_DAY,
        minute_bars=_bars({"14:30": 10.0, "14:31": 120.0}, up_limit=132.0),
        confirmation=_always_confirm,
        reference_notional=10_000.0,
    )
    assert decision.no_fill_reason == "below_lot_size"
    assert buy_lot_quantity(120.0, 10_000.0) == 0



def test_live_path_never_evaluates_a_slot_that_has_not_arrived_yet() -> None:
    """线上到 14:31 时，不能替还没发生的 14:35/14:40/14:45 做判断。"""
    seen: list[str] = []

    def spy(context):
        seen.append(context.confirmation_slot.strftime("%H:%M"))
        return False, "not_strength"

    evaluate_tail_entry(
        symbol="600000.SH",
        trading_day=TRADE_DAY,
        minute_bars=_bars({"14:30": 10.0, "14:31": 10.05, "14:35": 10.1,
                           "14:36": 10.2}, up_limit=11.0),
        confirmation=spy,
        quote_as_of=datetime(2026, 10, 8, 14, 31),
    )
    assert seen == ["14:30"]


# ---------------------------------------------------------------------------
# 出场：T+1 / 跳空 / 双触发 / 计划退出 / 顺延 / 未成熟
# ---------------------------------------------------------------------------


def _entry(quantity: int = 1000, price: float = 10.0) -> dict:
    return {"entry_price": price, "quantity": quantity, "buy_cost": 5.0}


def test_no_tp_or_sl_can_trigger_on_the_entry_day_because_of_t_plus_1() -> None:
    """入场日 bar 里 price 早已越过止盈档，也不得当日卖出。"""
    result = simulate_tail_exit(
        symbol="600000.SH", entry_date=TRADE_DAY,
        daily_bars=[
            _daily(TRADE_DAY, open_=10.0, high=11.0, low=9.6, close=10.8),
            _daily(NEXT_DAY, open_=10.4, high=10.5, low=10.3, close=10.45),
            _daily(date(2026, 10, 12), open_=10.45, high=10.5, low=10.4, close=10.45),
            _daily(date(2026, 10, 13), open_=10.45, high=10.5, low=10.4, close=10.45),
            _daily(date(2026, 10, 14), open_=10.45, high=10.5, low=10.4, close=10.45),
        ],
        **_entry(),
    )
    assert result.status == STATUS_FILLED
    assert result.exit_date == datetime(2026, 10, 14)  # 第 5 个交易日
    assert result.sessions_held == 5
    assert result.take_profit_hit is False


def test_take_profit_triggers_at_the_level_price() -> None:
    result = simulate_tail_exit(
        symbol="600000.SH", entry_date=TRADE_DAY,
        daily_bars=[
            _daily(TRADE_DAY, open_=10.0, high=10.2, low=9.9, close=10.1),
            _daily(NEXT_DAY, open_=10.1, high=10.9, low=10.0, close=10.5),
        ],
        **_entry(),
    )
    assert result.take_profit_hit and result.exit_price == pytest.approx(10.8)
    assert result.net_profit is True


def test_gap_down_stop_exit_uses_the_open_not_the_stop_level() -> None:
    """跳空止损：开盘已低于止损档 → 以开盘价成交，不得粉饰成 -5%。"""
    result = simulate_tail_exit(
        symbol="600000.SH", entry_date=TRADE_DAY,
        daily_bars=[
            _daily(TRADE_DAY, open_=10.0, high=10.1, low=9.95, close=10.0),
            _daily(NEXT_DAY, open_=9.0, high=9.2, low=8.9, close=9.1),
        ],
        **_entry(),
    )
    assert result.stop_loss_hit and result.gap_exit
    assert result.exit_price == pytest.approx(9.0)
    assert result.net_return < -0.09  # 毛 -10% 再扣成本
    assert result.net_profit is False


def test_double_trigger_same_bar_resolves_to_stop_loss_first() -> None:
    result = simulate_tail_exit(
        symbol="600000.SH", entry_date=TRADE_DAY,
        daily_bars=[
            _daily(TRADE_DAY, open_=10.0, high=10.1, low=9.9, close=10.0),
            _daily(NEXT_DAY, open_=10.0, high=11.0, low=9.0, close=10.5),
        ],
        **_entry(),
    )
    assert result.ambiguous_same_bar is True
    assert result.stop_loss_hit is True
    assert result.take_profit_hit is False
    assert result.exit_price == pytest.approx(9.5)


def test_plan_exit_on_fifth_day_at_close_when_neither_level_hit() -> None:
    bars = [_daily(TRADE_DAY, open_=10.0, high=10.1, low=9.9, close=10.0)]
    for day in [NEXT_DAY, date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)]:
        bars.append(_daily(day, open_=10.2, high=10.3, low=10.1, close=10.25))
    bars.append(_daily(date(2026, 10, 15), open_=10.25, high=10.3, low=10.2, close=10.28))
    result = simulate_tail_exit(symbol="600000.SH", entry_date=TRADE_DAY,
                                daily_bars=bars, **_entry())
    assert result.exit_date == datetime(2026, 10, 14)
    assert result.exit_price == pytest.approx(10.25)


def test_unsellable_exit_day_defers_and_maturity_is_the_actual_exit() -> None:
    """第 5 日封跌停卖不掉 → 顺延，成熟时间以真正能成交那天为准。"""
    down_locked = _daily(date(2026, 10, 14), open_=10.2, high=10.3, low=10.15,
                         close=10.15, down_limit=10.15)
    result = simulate_tail_exit(
        symbol="600000.SH", entry_date=TRADE_DAY,
        daily_bars=[
            _daily(TRADE_DAY, open_=10.0, high=10.1, low=9.9, close=10.0),
            _daily(NEXT_DAY, open_=10.2, high=10.3, low=10.1, close=10.25),
            _daily(date(2026, 10, 12), open_=10.2, high=10.3, low=10.1, close=10.25),
            _daily(date(2026, 10, 13), open_=10.2, high=10.3, low=10.1, close=10.25),
            down_locked,
            _daily(date(2026, 10, 15), open_=10.2, high=10.3, low=10.1, close=10.22),
        ],
        **_entry(),
    )
    assert result.status == STATUS_FILLED
    assert result.deferred_sessions >= 1
    assert result.exit_date == datetime(2026, 10, 15)
    assert label_maturity_time(result) == datetime(2026, 10, 15)


def test_series_end_before_plan_exit_is_uncertain_not_realized_profit() -> None:
    result = simulate_tail_exit(
        symbol="600000.SH", entry_date=TRADE_DAY,
        daily_bars=[
            _daily(TRADE_DAY, open_=10.0, high=10.1, low=9.9, close=10.05),
            _daily(NEXT_DAY, open_=10.05, high=10.4, low=10.0, close=10.3),
        ],
        **_entry(),
    )
    assert result.status == STATUS_UNCERTAIN
    assert result.realized is False
    assert result.net_profit is False
    assert result.reason == "insufficient_data_at_series_end"
    assert label_maturity_time(result) is None


def test_undeclared_trade_status_is_uncertain_fail_closed() -> None:
    bar = dict(_daily(NEXT_DAY, open_=10.0, high=10.2, low=9.9, close=10.1)[1])
    bar.pop("trade_status")
    result = simulate_tail_exit(
        symbol="600000.SH", entry_date=TRADE_DAY,
        daily_bars=[_daily(TRADE_DAY, open_=10.0, high=10.1, low=9.9, close=10.0),
                    (NEXT_DAY, bar)],
        **_entry(),
    )
    assert result.status == STATUS_UNCERTAIN
    assert result.reason == "unknown_trade_status"


def test_uncalculable_corporate_action_is_uncertain() -> None:
    result = simulate_tail_exit(
        symbol="600000.SH", entry_date=TRADE_DAY,
        daily_bars=[
            _daily(TRADE_DAY, open_=10.0, high=10.1, low=9.9, close=10.0),
            _daily(NEXT_DAY, open_=10.0, high=10.2, low=9.9, close=10.1,
                   corporate_action_uncertain=True),
        ],
        **_entry(),
    )
    assert result.status == STATUS_UNCERTAIN
    assert result.corporate_action_uncertain is True


def test_defer_window_exhaustion_does_not_fake_a_successful_exit() -> None:
    bars = [
        _daily(TRADE_DAY, open_=10.0, high=10.1, low=9.9, close=10.0),
        _daily(NEXT_DAY, open_=10.2, high=10.3, low=10.1, close=10.25),
    ]
    cursor = date(2026, 10, 12)
    locked = 0
    while locked < 8:
        bars.append(_daily(cursor, open_=10.2, high=10.3, low=10.15, close=10.15,
                           down_limit=10.15))
        cursor = date(cursor.year, cursor.month, cursor.day + 1)
        locked += 1
    result = simulate_tail_exit(symbol="600000.SH", entry_date=TRADE_DAY,
                                daily_bars=bars,
                                contract=TrendStrategyContract(exit_defer_max_sessions=2),
                                **_entry())
    assert result.status == STATUS_UNCERTAIN
    assert result.reason == "exit_not_executable"


def test_exit_requires_positive_entry_price_and_quantity() -> None:
    with pytest.raises(TrendContractError):
        simulate_tail_exit(symbol="600000.SH", entry_date=TRADE_DAY,
                           daily_bars=[], entry_price=0.0, quantity=100, buy_cost=0.0)


# ---------------------------------------------------------------------------
# 成本：最低佣金、整手、按日期冻结
# ---------------------------------------------------------------------------


def test_minimum_commission_applies_on_a_10k_notional_order() -> None:
    engine = ExecutionEngine(BacktestMatcherConfig())
    # 10000 元额：0.03% = 3 元 < 最低佣金 5 元 → 收 5 元
    cost = engine.estimate_cost("buy", 10.0, 1000, trade_date=TRADE_DAY)
    assert cost == pytest.approx(5.0 + 10_000 * 0.00001)
    assert engine.cost_profile(TRADE_DAY).source == "static_matcher"


def test_cost_schedule_is_date_versioned_and_reports_provenance() -> None:
    limit_rule = LimitRuleConfig(
        cost_schedule_by_date=[
            CostScheduleEntry(**{"from": "2015-01-01", "stamp_tax_rate": 0.001}),
            CostScheduleEntry(**{"from": "2023-08-28", "stamp_tax_rate": 0.0005,
                                 "commission_rate": 0.00025}),
        ]
    )
    engine = ExecutionEngine(BacktestMatcherConfig(), limit_rule)
    before = engine.cost_profile(date(2023, 8, 27))
    after = engine.cost_profile(date(2023, 8, 28))
    assert before.stamp_tax_rate == pytest.approx(0.001)
    assert after.stamp_tax_rate == pytest.approx(0.0005)
    assert after.commission_rate == pytest.approx(0.00025)
    assert before.min_commission_per_order == pytest.approx(5.0)
    assert after.overridden == frozenset({"stamp_tax_rate", "commission_rate"})
    assert after.source == "cost_schedule"
    sell_before = engine.estimate_cost("sell", 10.0, 1000, trade_date=date(2023, 8, 21))
    sell_after = engine.estimate_cost("sell", 10.0, 1000, trade_date=date(2023, 8, 28))
    assert sell_before > sell_after


def test_net_return_charges_both_sides_of_cost() -> None:
    def estimator(side: str, price: float, quantity: int, when: datetime) -> float:
        return 5.0 + price * quantity * 0.0005 if side == "sell" else 5.0

    result = simulate_tail_exit(
        symbol="600000.SH", entry_date=TRADE_DAY,
        daily_bars=[
            _daily(TRADE_DAY, open_=10.0, high=10.1, low=9.9, close=10.0),
            _daily(NEXT_DAY, open_=10.0, high=10.02, low=9.99, close=10.01),
        ],
        entry_price=10.0, quantity=1000, buy_cost=5.0,
        cost_estimator=estimator,
    )
    assert result.status == STATUS_UNCERTAIN  # 只有 2 天，未到期
    full = simulate_tail_exit(
        symbol="600000.SH", entry_date=TRADE_DAY,
        daily_bars=[_daily(TRADE_DAY, open_=10.0, high=10.1, low=9.9, close=10.0)]
        + [_daily(date(2026, 10, d), open_=10.01, high=10.05, low=9.99, close=10.02)
           for d in (9, 12, 13, 14)],
        entry_price=10.0, quantity=1000, buy_cost=5.0,
        cost_estimator=estimator,
    )
    assert full.sell_cost == pytest.approx(5.0 + 1000 * 10.02 * 0.0005)
    assert full.net_return < full.gross_return


# ---------------------------------------------------------------------------
# 最终推荐准入：阈值 / 上限 / 同分 / 去重 / 身份 / 缺失
# ---------------------------------------------------------------------------


def _identity(**overrides) -> ModelIdentity:
    base = {
        "model_id": "trend-tail-lr-2026q4",
        "artifact_content_hash": "sha256:abc",
        "training_commit": "dd6d24b",
        "runtime_commit": "dd6d24b",
        "feature_compute_version": 5,
        "label_policy_id": "net_profit_5d_tail_v1",
        "contract_digest": CONTRACT.digest(),
    }
    base.update(overrides)
    return ModelIdentity(**base)


def _rows() -> list[dict]:
    return [
        {"symbol": "600001.SH", NET_PROFIT_PROBABILITY_FIELD: 0.72},
        {"symbol": "600002.SH", NET_PROFIT_PROBABILITY_FIELD: 0.65},
        {"symbol": "600003.SH", NET_PROFIT_PROBABILITY_FIELD: 0.61},
        {"symbol": "600004.SH", NET_PROFIT_PROBABILITY_FIELD: 0.60},
        {"symbol": "600005.SH", NET_PROFIT_PROBABILITY_FIELD: 0.59},
    ]


def test_ranking_uses_threshold_desc_and_symbol_tiebreak() -> None:
    rows = _rows() + [{"symbol": "600009.SH", NET_PROFIT_PROBABILITY_FIELD: 0.65}]
    outcome = rank_final_recommendations(
        trade_date=TRADE_DAY, rows=rows, model_identity=_identity()
    )
    assert outcome.symbols == ("600001.SH", "600002.SH", "600009.SH")
    rejected = {item.symbol: item.reason for item in outcome.rejected}
    assert rejected["600005.SH"] == "below_threshold"
    assert rejected["600003.SH"] == "cap_exceeded"
    assert rejected["600004.SH"] == "cap_exceeded"


def test_empty_recommendation_is_allowed_and_never_backfilled() -> None:
    rows = [{"symbol": "600005.SH", NET_PROFIT_PROBABILITY_FIELD: 0.2}]
    outcome = rank_final_recommendations(
        trade_date=TRADE_DAY, rows=rows, model_identity=_identity()
    )
    assert outcome.selected == ()
    assert outcome.blocking_reason == ""
    assert outcome.counts["selected"] == 0


def test_old_composite_score_and_grade_do_not_grant_eligibility() -> None:
    """只有旧综合分/等级、没有新概率的行 → 一律 missing_probability，不参与资格。"""
    rows = [{"symbol": "600007.SH", "score": 99.0, "grade": "S", "p_meta": 0.99}]
    outcome = rank_final_recommendations(
        trade_date=TRADE_DAY, rows=rows, model_identity=_identity()
    )
    assert outcome.selected == ()
    assert outcome.rejected[0].reason == "missing_probability"


def test_malformed_probability_is_dropped_not_coerced_to_zero() -> None:
    rows = [{"symbol": "600008.SH", NET_PROFIT_PROBABILITY_FIELD: "not-a-number"},
            {"symbol": "600010.SH", NET_PROFIT_PROBABILITY_FIELD: 1.7}]
    outcome = rank_final_recommendations(
        trade_date=TRADE_DAY, rows=rows, model_identity=_identity()
    )
    assert {item.reason for item in outcome.rejected} == {"missing_probability"}


def test_risk_and_tradeability_reject_before_ranking() -> None:
    rows = [
        {"symbol": "600011.SH", NET_PROFIT_PROBABILITY_FIELD: 0.9, "risk_state": "blocked"},
        {"symbol": "600012.SH", NET_PROFIT_PROBABILITY_FIELD: 0.9,
         "tradeable": False, "not_tradeable_reason": "limit_up_locked"},
    ]
    outcome = rank_final_recommendations(
        trade_date=TRADE_DAY, rows=rows, model_identity=_identity()
    )
    reasons = {item.symbol: item.reason for item in outcome.rejected}
    assert reasons["600011.SH"] == "risk_blocked"
    assert reasons["600012.SH"] == "limit_up_locked"


def test_duplicate_symbols_are_deduped_keeping_the_best_probability() -> None:
    rows = [{"symbol": "600013.SH", NET_PROFIT_PROBABILITY_FIELD: 0.62},
            {"symbol": "600013.SH", NET_PROFIT_PROBABILITY_FIELD: 0.81}]
    outcome = rank_final_recommendations(
        trade_date=TRADE_DAY, rows=rows, model_identity=_identity()
    )
    assert outcome.symbols == ("600013.SH",)
    assert outcome.selected[0].probability == pytest.approx(0.81)
    assert outcome.counts["duplicates_dropped"] == 1


@pytest.mark.parametrize(
    "broken,expected",
    [
        ({"model_id": ""}, "model_id_missing"),
        ({"artifact_content_hash": ""}, "artifact_content_hash_missing"),
        ({"training_commit": ""}, "training_commit_unknown"),
        ({"training_commit": "aaaa", "runtime_commit": "bbbb"},
         "training_runtime_commit_mismatch"),
        ({"feature_compute_version": 0}, "feature_compute_version_invalid"),
        ({"label_policy_id": ""}, "label_policy_id_missing"),
        ({"contract_digest": "stale"}, "strategy_contract_digest_mismatch"),
    ],
)
def test_model_identity_violations_produce_zero_recommendations(broken, expected) -> None:
    outcome = rank_final_recommendations(
        trade_date=TRADE_DAY, rows=_rows(), model_identity=_identity(**broken)
    )
    assert outcome.selected == ()
    assert outcome.blocking_reason == expected
    assert all(item.reason == expected for item in outcome.rejected)


def test_missing_model_identity_fails_closed() -> None:
    outcome = rank_final_recommendations(
        trade_date=TRADE_DAY, rows=_rows(), model_identity=None
    )
    assert outcome.selected == ()
    assert outcome.blocking_reason == "model_identity_missing"


# ---------------------------------------------------------------------------
# 配置冲突审计：现存"声明 vs 开盘入口"不一致必须可见
# ---------------------------------------------------------------------------


def _multi_namespace_config() -> SimpleNamespace:
    return SimpleNamespace(
        trend_strategy=TrendStrategyConfig(),
        labels=LabelsConfig(),
        # max_holdings=1 与 config/default.yaml 现状一致（YAML 里就是 1）。
        soup_strategy=SoupStrategyConfig(max_holdings=1),
        asof_backtest=AsofBacktestConfig(),
        backtest_matcher=BacktestMatcherConfig(),
    )


def test_audit_surfaces_the_real_conflicting_declarations() -> None:
    findings = audit_strategy_contract_conflicts(_multi_namespace_config())
    sites = {item["site"] for item in findings}
    assert "labels" in sites
    assert "labels.pnl_price_basis" in sites
    assert "asof_backtest" in sites
    label_gap = next(item for item in findings if item["site"] == "labels")
    assert label_gap["declares"]["horizon_days"] == 10
    soup_gap = next(item for item in findings if item["site"] == "soup_strategy")
    assert soup_gap["declares"]["max_hold_days"] == 10
    assert soup_gap["declares"]["max_holdings"] == 1
    asof_gap = next(item for item in findings if item["site"] == "asof_backtest")
    assert asof_gap["declares"]["default_horizon_days"] == 10


def test_audit_empty_when_every_block_matches_the_contract() -> None:
    config = SimpleNamespace(
        trend_strategy=TrendStrategyConfig(),
        labels=LabelsConfig(take_profit_pct=0.08, stop_loss_pct=0.05, horizon_days=5,
                            primary="net_profit_5d_tail_v1",
                            pnl_price_basis="tail_confirm_next_bar"),
        soup_strategy=SoupStrategyConfig(stop_loss=5.0, max_hold_days=5, max_holdings=3,
                                         entry_window=["14:30", "14:50"]),
        asof_backtest=AsofBacktestConfig(take_profit_pct=0.08, stop_loss_pct=0.05,
                                         default_horizon_days=5),
        backtest_matcher=BacktestMatcherConfig(max_exit_carry_days=5),
    )
    assert audit_strategy_contract_conflicts(config) == []


def test_audit_reports_a_broken_contract_block_instead_of_defaulting() -> None:
    findings = audit_strategy_contract_conflicts(
        SimpleNamespace(trend_strategy=TrendStrategyConfig(take_profit_pct=0.0))
    )
    assert findings[0]["site"] == "config.trend_strategy"


def test_shipped_yaml_declares_exactly_the_frozen_contract() -> None:
    """config/default.yaml 的 trend_strategy 必须逐字等于冻结契约。

    直接解析 YAML 而不是 load_config()，避免本机 .env 差异影响这条不变量。
    """
    import yaml

    raw = yaml.safe_load(
        (pathlib.Path(__file__).resolve().parents[1] / "config" / "default.yaml")
        .read_text(encoding="utf-8")
    )
    block = TrendStrategyConfig(**raw["trend_strategy"])
    assert block.model_dump() == TrendStrategyConfig().model_dump()
    built = contract_from_config(SimpleNamespace(trend_strategy=block))
    assert built.digest() == CONTRACT.digest()


def test_completed_bars_helper_respects_the_completion_boundary() -> None:
    bars = _bars({"14:29": 10.0, "14:30": 10.1, "14:31": 10.2})
    kept = completed_bars(bars, as_of=datetime(2026, 10, 8, 14, 30))
    assert [end for end, _ in kept] == [datetime(2026, 10, 8, 14, 29),
                                        datetime(2026, 10, 8, 14, 30)]
