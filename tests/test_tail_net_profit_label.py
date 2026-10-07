"""``p_net_profit_5d_tail`` 标签层验收（改进计划 §3.3）。

标签本身不重复实现交易规则——规则在 ``contracts/trend_strategy`` 里验收过。
这里验收的是：新 basis 的身份与语义、成交/未成交/不确定的三分类、
"只有已实现样本参与净盈利率"的口径，以及真实观察与重建样本必须分开报告。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from stock_analyzer.config import BacktestMatcherConfig, CostScheduleEntry, LimitRuleConfig
from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    STATUS_FILLED,
    STATUS_NOT_FILLED,
    STATUS_UNCERTAIN,
    TrendStrategyContract,
)
from stock_analyzer.data.limit_rule import resolve_cost_profile
from stock_analyzer.execution.engine import ExecutionEngine
from stock_analyzer.labels.tail_net_profit import (
    CAPTURE_OBSERVED,
    CAPTURE_REPLAYED,
    LABEL_LOSS,
    LABEL_PROFIT,
    TAIL_MATURITY_RULE,
    TAIL_NET_PROFIT_BASIS,
    TAIL_PRICE_BASIS,
    TAIL_SCHEMA_VERSION,
    TailLabelError,
    build_tail_net_profit_label,
    resolve_tail_slippage_ratio,
    summarize_tail_labels,
    tail_label_name,
    tail_label_policy_record,
)
from stock_analyzer.models.output_semantics import (
    OUTPUT_SEMANTICS_EVENT_PROBABILITY,
    output_semantics_for_basis,
)

DECISION_DAY = date(2026, 10, 8)   # 夜扫出观察池
ENTRY_DAY = date(2026, 10, 9)      # 次日尾盘确认并成交
DAYS = [ENTRY_DAY, date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14),
        date(2026, 10, 15)]

CONTRACT = DEFAULT_TREND_CONTRACT


def _confirm(context) -> tuple[bool, str]:
    return True, ""


def _reject(context) -> tuple[bool, str]:
    return False, "tail_strength_faded"


def _minute_bars(price: float = 10.0, *, day: date = ENTRY_DAY) -> list:
    return [
        (datetime(day.year, day.month, day.day, 14, 30),
         {"open": price, "high": price, "low": price, "close": price,
          "up_limit": price * 1.1, "trade_status": "normal"}),
        (datetime(day.year, day.month, day.day, 14, 31),
         {"open": price, "high": price, "low": price, "close": price,
          "up_limit": price * 1.1, "trade_status": "normal"}),
    ]


def _locked_minute_bars(price: float = 10.0, *, day: date = ENTRY_DAY) -> list:
    """开盘价已达到涨停价：买不进（不假设排队成交）。"""
    bar = {"open": price, "high": price, "low": price, "close": price,
           "up_limit": price, "trade_status": "normal"}
    return [
        (datetime(day.year, day.month, day.day, 14, 30), dict(bar)),
        (datetime(day.year, day.month, day.day, 14, 31), dict(bar)),
    ]


def _daily(day: date, *, open_: float, high: float, low: float, close: float,
           **extra) -> tuple:
    return (day, {"open": open_, "high": high, "low": low, "close": close,
                  "trade_status": "normal", **extra})


def _flat_days(price: float = 10.0) -> list:
    return [_daily(day, open_=price, high=price + 0.05, low=price - 0.05, close=price)
            for day in DAYS]


def _build(*, confirmation=_confirm, daily_bars=None, cost_estimator=None,
           capture_mode=CAPTURE_REPLAYED, **overrides) -> object:
    return build_tail_net_profit_label(
        symbol="600000.SH",
        decision_date=DECISION_DAY,
        entry_date=ENTRY_DAY,
        minute_bars=overrides.pop("minute_bars", None) or _minute_bars(),
        daily_bars=daily_bars if daily_bars is not None else _flat_days(),
        confirmation=confirmation,
        cost_estimator=cost_estimator,
        capture_mode=capture_mode,
        **overrides,
    )


# ---------------------------------------------------------------------------
# 身份与语义：新 basis，不改写旧标签含义
# ---------------------------------------------------------------------------


def test_new_basis_is_registered_as_event_probability() -> None:
    assert output_semantics_for_basis(TAIL_NET_PROFIT_BASIS) == (
        OUTPUT_SEMANTICS_EVENT_PROBABILITY
    )


def test_policy_record_carries_tail_contract_identity() -> None:
    record = tail_label_policy_record()
    assert record.schema_version == TAIL_SCHEMA_VERSION == "4"
    assert record.price_basis == TAIL_PRICE_BASIS == "tail_confirm_next_bar"
    assert record.maturity_rule == TAIL_MATURITY_RULE
    assert record.horizon_days == 5
    assert record.take_profit_pct == pytest.approx(0.08)
    assert record.stop_loss_pct == pytest.approx(0.05)
    # 止损优先是硬 0：不存在"半个正类"
    assert record.conflict_policy == "stop_loss_first"
    assert record.conflict_soft_label_value == 0.0
    assert record.label_policy_id.startswith("label_policy_v4_")


def test_policy_identity_changes_when_the_contract_changes() -> None:
    """契约口径变了就是另一个标签，绝不复用旧 label_policy_id。"""
    base = tail_label_policy_record(CONTRACT)
    variant = tail_label_policy_record(TrendStrategyContract(holding_days=10))
    assert base.label_policy_id != variant.label_policy_id
    assert base.label_name == tail_label_name(CONTRACT)
    assert "tp8_sl5" in base.label_name


# ---------------------------------------------------------------------------
# 三分类：已实现 / 未成交 / 不确定
# ---------------------------------------------------------------------------


def test_profitable_exit_is_a_hard_positive_label() -> None:
    tp_day = _daily(DAYS[1], open_=10.0, high=10.9, low=9.95, close=10.6)
    record = _build(daily_bars=[_flat_days()[0], tp_day] + _flat_days()[2:])
    assert record.status == STATUS_FILLED
    assert record.trainable is True
    assert record.label == LABEL_PROFIT
    assert record.take_profit_hit is True
    assert record.label_mature_time == datetime(2026, 10, 12)
    assert record.label_anchor_time == datetime(2026, 10, 9, 14, 31)


def test_stop_loss_exit_is_a_hard_negative_label() -> None:
    sl_day = _daily(DAYS[1], open_=10.0, high=10.05, low=9.4, close=9.5)
    record = _build(daily_bars=[_flat_days()[0], sl_day] + _flat_days()[2:])
    assert record.label == LABEL_LOSS
    assert record.stop_loss_hit is True
    assert record.net_return < 0


def test_confirmation_failure_is_not_filled_and_never_labelled() -> None:
    record = _build(confirmation=_reject)
    assert record.status == STATUS_NOT_FILLED
    assert record.confirmed is False and record.filled is False
    assert record.trainable is False
    assert record.label is None
    assert record.net_return is None
    assert record.reason == "tail_strength_faded"


def test_untradable_entry_is_not_filled_not_a_loss() -> None:
    record = _build(minute_bars=_locked_minute_bars())
    assert record.status == STATUS_NOT_FILLED
    assert record.reason == "limit_up_locked"
    assert record.label is None


def test_immature_exit_stays_uncertain_without_a_label() -> None:
    record = _build(daily_bars=_flat_days()[:2])
    assert record.status == STATUS_UNCERTAIN
    assert record.trainable is False
    assert record.label is None
    assert record.reason == "insufficient_data_at_series_end"


def test_costs_and_slippage_are_inside_the_labelled_return() -> None:
    def estimator(side: str, price: float, quantity: int, when: datetime) -> float:
        return 5.0 + price * quantity * (0.0005 if side == "sell" else 0.0)

    record = _build(cost_estimator=estimator)
    assert record.buy_cost == pytest.approx(5.0)
    assert record.sell_cost > 0
    # 价格没动，但双边成本把它变成净亏损：这正是"净盈利概率"要学的事
    assert record.net_return < record.gross_return
    assert record.gross_return == pytest.approx(0.0, abs=1e-9)
    assert record.label == LABEL_LOSS


# ---------------------------------------------------------------------------
# 时间语义与输入校验：fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry_day", [DECISION_DAY, date(2026, 10, 7)]
)
def test_entry_on_or_before_decision_day_is_rejected(entry_day: date) -> None:
    """夜扫在 T 出池，确认成交只能发生在 T+1 的尾盘窗口。"""
    with pytest.raises(TailLabelError):
        build_tail_net_profit_label(
            symbol="600000.SH", decision_date=DECISION_DAY, entry_date=entry_day,
            minute_bars=_minute_bars(day=entry_day), daily_bars=_flat_days(),
            confirmation=_confirm,
        )


def test_unknown_capture_mode_is_rejected() -> None:
    with pytest.raises(TailLabelError):
        _build(capture_mode="guessed")


def test_record_carries_full_contract_provenance() -> None:
    payload = _build().to_dict()
    assert payload["contract_version"] == CONTRACT.contract_version
    assert payload["contract_digest"] == CONTRACT.digest()
    assert payload["cost_model_version"] == CONTRACT.cost_model_version
    assert payload["price_basis"] == TAIL_PRICE_BASIS
    assert payload["label_policy_basis"] == TAIL_NET_PROFIT_BASIS
    assert payload["holding_days"] == 5
    assert payload["reference_notional"] == pytest.approx(10_000.0)
    assert "未成交与不确定样本不生成盈亏标签" in payload["label_definition"]


# ---------------------------------------------------------------------------
# 分组报告：净盈利率分母只含已实现样本；观察与重建不得混算
# ---------------------------------------------------------------------------


def _next(day: date, offset: int = 1) -> date:
    return day + timedelta(days=offset)


def _synthetic(capture: str, *, profits: int, losses: int, not_filled: int,
               uncertain: int) -> list:
    """造已实现/未成交/不确定三类样本，验证分组报告的分母口径。

    正负样本的价格动作必须发生在**入场日之后**的那根日线（T+1 才可卖），
    否则契约会把入场日跳过——这是契约行为，不是本函数的 bug。
    """
    records: list = []
    index = 0
    for kind, count in (("profit", profits), ("loss", losses),
                        ("not_filled", not_filled), ("uncertain", uncertain)):
        for _ in range(count):
            index += 1
            day = date(2026, 10, 8) + timedelta(days=index)
            entry = _next(day)
            head = [
                _daily(day, open_=10.0, high=10.05, low=9.95, close=10.0),
                _daily(entry, open_=10.0, high=10.05, low=9.95, close=10.0),
            ]
            if kind in {"profit", "loss"}:
                step = 10.9 if kind == "profit" else 9.4
                bars = head + [
                    _daily(_next(day, 2), open_=10.0, high=max(10.05, step),
                           low=min(9.95, step), close=step)
                ] + [
                    _daily(_next(day, offset), open_=10.0, high=10.05, low=9.95,
                           close=10.0)
                    for offset in (3, 4, 5, 6)
                ]
                minute = _minute_bars(day=entry)
                confirmation = _confirm
            elif kind == "not_filled":
                bars = head
                minute = _locked_minute_bars(day=entry)
                confirmation = _confirm
            else:
                bars = head
                minute = _minute_bars(day=entry)
                confirmation = _confirm
            records.append(build_tail_net_profit_label(
                symbol="600000.SH", decision_date=day, entry_date=entry,
                minute_bars=minute, daily_bars=bars,
                confirmation=confirmation, capture_mode=capture,
            ))
    return records


def test_net_profit_rate_denominator_excludes_unfilled_and_uncertain() -> None:
    records = _synthetic(CAPTURE_OBSERVED, profits=6, losses=4, not_filled=3,
                         uncertain=2)
    summary = summarize_tail_labels(records)
    group = summary["by_capture_mode"][CAPTURE_OBSERVED]
    assert group["n_candidates"] == 15
    assert group["n_realized"] == 10
    assert group["n_not_filled"] == 3
    assert group["n_uncertain"] == 2
    assert group["net_profit_rate"] == pytest.approx(0.6)
    # 成交率 ≠ 已实现率：那 2 笔不确定样本确实买到了，只是没能按契约平仓
    assert group["n_filled"] == 12
    assert group["fill_rate"] == pytest.approx(12 / 15)
    assert group["label_class_counts"]["unlabeled"] == 5
    assert summary["mixed_capture_mode"] is False


def test_observed_and_replayed_samples_are_reported_separately() -> None:
    records = _synthetic(CAPTURE_OBSERVED, profits=5, losses=5, not_filled=0,
                         uncertain=0)
    records += _synthetic(CAPTURE_REPLAYED, profits=9, losses=1, not_filled=0,
                          uncertain=0)
    summary = summarize_tail_labels(records)
    assert summary["mixed_capture_mode"] is True
    assert set(summary["by_capture_mode"]) == {CAPTURE_OBSERVED, CAPTURE_REPLAYED}
    assert summary["by_capture_mode"][CAPTURE_OBSERVED][
        "net_profit_rate"] == pytest.approx(0.5)
    assert summary["by_capture_mode"][CAPTURE_REPLAYED][
        "net_profit_rate"] == pytest.approx(0.9)
    assert summary["total_records"] == 20


def test_rejection_reasons_are_counted_for_funnel_diagnosis() -> None:
    records = _synthetic(CAPTURE_OBSERVED, profits=1, losses=0, not_filled=0,
                         uncertain=0)
    group = summarize_tail_labels(records)["by_capture_mode"][CAPTURE_OBSERVED]
    assert group["confirm_rate"] == pytest.approx(1.0)
    assert group["decision_days"]
    assert group["capital_employed"] > 0
    assert group["not_filled_reasons"] == {}


# ---------------------------------------------------------------------------
# 滑点：来自按日期冻结的成本表，策略档位兜底
# ---------------------------------------------------------------------------


def test_slippage_falls_back_to_the_strategy_tier() -> None:
    matcher = BacktestMatcherConfig()
    ratio = resolve_tail_slippage_ratio(matcher=matcher, trade_date=DECISION_DAY)
    assert ratio == pytest.approx(matcher.slippage_by_strategy["trend"])


def test_date_frozen_schedule_overrides_the_strategy_tier() -> None:
    limit_rule = LimitRuleConfig(cost_schedule_by_date=[
        CostScheduleEntry(**{"from": "2026-01-01", "stamp_tax_rate": 0.0005,
                             "slippage_ratio": 0.0008}),
    ])
    matcher = BacktestMatcherConfig()
    assert resolve_tail_slippage_ratio(
        matcher=matcher, limit_rule=limit_rule, trade_date=DECISION_DAY
    ) == pytest.approx(0.0008)
    assert resolve_cost_profile(
        limit_rule=limit_rule, matcher=matcher, trade_date=DECISION_DAY
    ).overridden >= frozenset({"slippage_ratio"})


def test_engine_and_label_share_one_cost_authority() -> None:
    """同一笔下单价：契约侧估的成本必须与执行引擎逐元一致。"""
    engine = ExecutionEngine(BacktestMatcherConfig())
    record = _build(cost_estimator=lambda side, price, quantity, when: engine.estimate_cost(
        side, price, quantity, trade_date=when))
    expected = engine.estimate_cost("buy", float(record.entry_price), record.quantity,
                                    trade_date=ENTRY_DAY)
    assert record.buy_cost == pytest.approx(expected)
