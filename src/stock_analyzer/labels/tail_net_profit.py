"""``p_net_profit_5d_tail`` 标签：与选股目标一致的"扣费后净盈利"事件标签。

改进计划 §3.3。这个标签的唯一口径来自
``contracts.trend_strategy`` 的尾盘策略契约——线上推荐、训练标签、历史验证
跑的是同一套入场/成交/出场/成本规则，因此这里**不重复实现任何交易规则**，
只把契约结果整理成可训练、可审计的样本行。

与其它标签的关系：

- 不覆盖 ``labels/soup.py``（TP/SL 路径标签，entry=T+1 开盘）与
  ``labels/return_rank.py``（v3 横截面分位）的任何记录；新 basis、新 label_name、
  新 maturity rule 各成一套，旧分数含义不变。
- 语义是 ``event_probability``：正类 = "按契约成交且扣费扣滑点后净收益 > 0"。
  因此可以和 0/1 标签一起算 Brier / logloss / calibration，这是它区别于
  ``rank_quantile`` 的地方。
- 未成交（确认失败、买不进）与不确定（未到期、数据末尾、未知交易状态、
  公司行动无法核算）**都不生成盈亏标签**，只计入成交率与不确定率。
- 真实观察（``observed_snapshot``）与事后重建（``replayed_recompute``）必须
  分组报告：本项目已实测两类样本的前向结果差一个量级，混算会失真。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    NET_PROFIT_PROBABILITY_FIELD,
    STATUS_FILLED,
    STATUS_NOT_FILLED,
    STATUS_UNCERTAIN,
    TailEntryDecision,
    TailExitResult,
    TrendStrategyContract,
    evaluate_tail_entry,
    simulate_tail_exit,
)
from stock_analyzer.data.limit_rule import resolve_cost_profile
from stock_analyzer.learning.label_policy_registry import (
    LabelPolicyRecord,
    build_label_policy_record,
)

TAIL_NET_PROFIT_BASIS = "net_profit_5d_tail"
TAIL_NET_PROFIT_OUTPUT_FIELD = NET_PROFIT_PROBABILITY_FIELD
TAIL_PRICE_BASIS = "tail_confirm_next_bar"
TAIL_MATURITY_RULE = "label_mature_time_tail_exit_v1"
TAIL_SCHEMA_VERSION = "4"

CAPTURE_OBSERVED = "observed_snapshot"
CAPTURE_REPLAYED = "replayed_recompute"
_VALID_CAPTURE_MODES = frozenset({CAPTURE_OBSERVED, CAPTURE_REPLAYED})

LABEL_PROFIT = 1.0
LABEL_LOSS = 0.0


class TailLabelError(ValueError):
    """标签构造输入违反契约时间语义（例如入场日不晚于决策日）。"""


def tail_label_name(contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT) -> str:
    """label_name 带契约摘要：契约口径变了就是另一个标签，不复用旧 id。"""
    return (
        f"net_profit_{int(contract.holding_days)}d_tail_"
        f"tp{int(round(contract.take_profit_pct * 100))}_"
        f"sl{int(round(contract.stop_loss_pct * 100))}_{contract.digest()}"
    )


def tail_label_policy_record(
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
    *,
    created_at: datetime | None = None,
) -> LabelPolicyRecord:
    """构造该标签的不可变契约记录，供 ``LabelPolicyRegistry.register`` 落库。"""
    return build_label_policy_record(
        label_name=tail_label_name(contract),
        take_profit_pct=float(contract.take_profit_pct),
        stop_loss_pct=float(contract.stop_loss_pct),
        horizon_days=int(contract.holding_days),
        price_basis=TAIL_PRICE_BASIS,
        exclude_untradable=True,
        conflict_policy=contract.same_bar_conflict_policy,
        # 止损优先是硬 0，不是软标签：不存在"半个正类"。
        conflict_soft_label_value=0.0,
        schema_version=TAIL_SCHEMA_VERSION,
        maturity_rule=TAIL_MATURITY_RULE,
        created_at=created_at,
    )


def resolve_tail_slippage_ratio(
    *,
    matcher: Any,
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
    trade_date: date | datetime | None = None,
    limit_rule: Any = None,
) -> float:
    """按日期冻结成本表取滑点：表里配了就用表，否则取该策略的静态档。"""
    profile = resolve_cost_profile(
        limit_rule=limit_rule, matcher=matcher, trade_date=trade_date
    )
    if profile.slippage_ratio is not None:
        return float(profile.slippage_ratio)
    by_strategy = getattr(matcher, "slippage_by_strategy", None) or {}
    return float(dict(by_strategy).get(contract.strategy, 0.0))


@dataclass(frozen=True)
class TailLabelRecord:
    """一行可训练样本的标签侧结果（特征侧由调用方拼接）。"""

    symbol: str
    decision_date: date
    entry_date: date | None
    status: str
    reason: str
    confirmed: bool
    filled: bool
    trainable: bool
    label: float | None
    net_return: float | None
    gross_return: float | None
    capture_mode: str
    contract_version: str
    contract_digest: str
    cost_model_version: str
    price_basis: str
    holding_days: int
    take_profit_pct: float
    stop_loss_pct: float
    reference_notional: float
    label_anchor_time: datetime | None
    label_mature_time: datetime | None
    confirmation_slot: datetime | None
    fill_time: datetime | None
    entry_price: float | None
    quantity: int
    buy_cost: float
    sell_cost: float
    take_profit_hit: bool
    stop_loss_hit: bool
    ambiguous_same_bar: bool
    gap_exit: bool
    deferred_sessions: int
    corporate_action_uncertain: bool
    #: 反馈闭环（§3.4）按这两个维度分组；缺省时归入 unattributed/unknown。
    model_version: str = ""
    market_state: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "decision_date": self.decision_date.isoformat(),
            "entry_date": self.entry_date.isoformat() if self.entry_date else None,
            "status": self.status,
            "reason": self.reason,
            "confirmed": self.confirmed,
            "filled": self.filled,
            "trainable": self.trainable,
            "label": self.label,
            TAIL_NET_PROFIT_OUTPUT_FIELD + "_label": self.label,
            "net_return": self.net_return,
            "gross_return": self.gross_return,
            "capture_mode": self.capture_mode,
            "contract_version": self.contract_version,
            "contract_digest": self.contract_digest,
            "cost_model_version": self.cost_model_version,
            "price_basis": self.price_basis,
            "label_policy_basis": TAIL_NET_PROFIT_BASIS,
            "holding_days": self.holding_days,
            "take_profit_pct": self.take_profit_pct,
            "stop_loss_pct": self.stop_loss_pct,
            "reference_notional": self.reference_notional,
            "label_anchor_time": (
                self.label_anchor_time.isoformat() if self.label_anchor_time else None
            ),
            "label_mature_time": (
                self.label_mature_time.isoformat() if self.label_mature_time else None
            ),
            "confirmation_slot": (
                self.confirmation_slot.isoformat() if self.confirmation_slot else None
            ),
            "fill_time": self.fill_time.isoformat() if self.fill_time else None,
            "entry_price": self.entry_price,
            "quantity": self.quantity,
            "buy_cost": self.buy_cost,
            "sell_cost": self.sell_cost,
            "take_profit_hit": self.take_profit_hit,
            "stop_loss_hit": self.stop_loss_hit,
            "ambiguous_same_bar": self.ambiguous_same_bar,
            "gap_exit": self.gap_exit,
            "deferred_sessions": self.deferred_sessions,
            "corporate_action_uncertain": self.corporate_action_uncertain,
            "model_version": self.model_version,
            "market_state": self.market_state,
            "label_definition": (
                f"按 trend 尾盘契约 {self.contract_version}/{self.contract_digest} 在 "
                f"尾盘窗口确认后以参考金额 {self.reference_notional:.0f} 元成交，"
                f"持有至多 {self.holding_days} 个交易日（入场日为第 1 日）、"
                f"止盈 +{self.take_profit_pct:.0%}/止损 -{self.stop_loss_pct:.0%}"
                "（同日双触发止损优先），扣佣金/最低佣金/过户费/印花税/滑点后"
                "净收益 > 0 记为正类；未成交与不确定样本不生成盈亏标签。"
            ),
        }


def build_tail_net_profit_label(
    *,
    symbol: str,
    decision_date: date | datetime,
    entry_date: date | datetime,
    minute_bars: Sequence[tuple[datetime, Mapping[str, Any]]],
    daily_bars: Sequence[tuple[date | datetime, Mapping[str, Any]]],
    confirmation: Callable[..., tuple[bool, str]],
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
    cost_estimator: Callable[[str, float, int, datetime], float] | None = None,
    price_ticker: Callable[[float, str], float] | None = None,
    slippage_ratio: float = 0.0,
    model_probabilities: Mapping[str, float] | None = None,
    overnight_features: Mapping[str, Any] | None = None,
    capture_mode: str = CAPTURE_REPLAYED,
    quote_as_of: datetime | None = None,
    model_version: str = "",
    market_state: str = "",
) -> TailLabelRecord:
    """观察池决策日 ``decision_date`` 的样本，在次日 ``entry_date`` 尾盘确认并成交。

    ``daily_bars`` 必须含 ``entry_date`` 那根 raw 日线；``minute_bars`` 必须是
    ``entry_date`` 当天的分钟行情（以区间终点标注）。历史重建调用不传 ``quote_as_of``，
    线上实时调用传当前时钟——两条路径共用本函数，判定必然一致。
    """
    decision_day = _to_day(decision_date)
    entry_day = _to_day(entry_date)
    if entry_day <= decision_day:
        raise TailLabelError(
            f"entry_date {entry_day} must be after decision_date {decision_day}: "
            "夜扫在 T 出观察池，确认与成交只能在 T+1 的尾盘窗口"
        )
    if str(capture_mode) not in _VALID_CAPTURE_MODES:
        raise TailLabelError(
            f"unknown capture_mode {capture_mode!r}; observed 与 replayed 必须分开标注"
        )

    entry: TailEntryDecision = evaluate_tail_entry(
        symbol=symbol,
        trading_day=entry_day,
        minute_bars=minute_bars,
        confirmation=confirmation,
        contract=contract,
        model_probabilities=model_probabilities,
        overnight_features=overnight_features,
        quote_as_of=quote_as_of,
        slippage_ratio=slippage_ratio,
        cost_estimator=cost_estimator,
        price_ticker=price_ticker,
    )

    if not entry.filled:
        return _record(
            symbol=symbol, decision_day=decision_day, entry_day=entry_day,
            status=STATUS_NOT_FILLED,
            reason=entry.no_fill_reason if entry.confirmed else entry.reason,
            entry=entry, exit_result=None, label=None, net_return=None,
            gross_return=None, capture_mode=capture_mode, contract=contract,
            trainable=False, model_version=model_version, market_state=market_state,
        )

    exit_result = simulate_tail_exit(
        symbol=symbol,
        entry_date=entry_day,
        entry_price=float(entry.net_fill_price or 0.0),
        quantity=int(entry.quantity),
        buy_cost=float(entry.buy_cost),
        daily_bars=daily_bars,
        contract=contract,
        cost_estimator=cost_estimator,
        price_ticker=price_ticker,
    )

    trainable = bool(exit_result.status == STATUS_FILLED and exit_result.realized)
    label = LABEL_PROFIT if (trainable and exit_result.net_profit) else (
        LABEL_LOSS if trainable else None
    )
    return _record(
        symbol=symbol, decision_day=decision_day, entry_day=entry_day,
        status=exit_result.status, reason=exit_result.reason,
        entry=entry, exit_result=exit_result,
        label=label,
        net_return=exit_result.net_return if trainable else None,
        gross_return=exit_result.gross_return if trainable else None,
        capture_mode=capture_mode, contract=contract, trainable=trainable,
        model_version=model_version, market_state=market_state,
    )


def _to_day(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def _record(
    *,
    symbol: str,
    decision_day: date,
    entry_day: date,
    status: str,
    reason: str,
    entry: TailEntryDecision,
    exit_result: TailExitResult | None,
    label: float | None,
    net_return: float | None,
    gross_return: float | None,
    capture_mode: str,
    contract: TrendStrategyContract,
    trainable: bool,
    model_version: str = "",
    market_state: str = "",
) -> TailLabelRecord:
    return TailLabelRecord(
        symbol=symbol,
        decision_date=decision_day,
        entry_date=entry_day,
        status=status,
        reason=reason,
        confirmed=bool(entry.confirmed),
        filled=bool(entry.filled),
        trainable=trainable,
        label=label,
        net_return=net_return,
        gross_return=gross_return,
        capture_mode=capture_mode,
        contract_version=contract.contract_version,
        contract_digest=contract.digest(),
        cost_model_version=contract.cost_model_version,
        price_basis=TAIL_PRICE_BASIS,
        label_anchor_time=entry.fill_time,
        label_mature_time=(exit_result.exit_date if trainable and exit_result else None),
        confirmation_slot=entry.confirmation_slot,
        fill_time=entry.fill_time,
        holding_days=int(contract.holding_days),
        take_profit_pct=float(contract.take_profit_pct),
        stop_loss_pct=float(contract.stop_loss_pct),
        reference_notional=float(contract.reference_notional),
        entry_price=entry.net_fill_price,
        quantity=int(entry.quantity),
        buy_cost=float(entry.buy_cost),
        sell_cost=float(exit_result.sell_cost) if exit_result else 0.0,
        take_profit_hit=bool(exit_result.take_profit_hit) if exit_result else False,
        stop_loss_hit=bool(exit_result.stop_loss_hit) if exit_result else False,
        ambiguous_same_bar=bool(exit_result.ambiguous_same_bar) if exit_result else False,
        gap_exit=bool(exit_result.gap_exit) if exit_result else False,
        deferred_sessions=int(exit_result.deferred_sessions) if exit_result else 0,
        corporate_action_uncertain=(
            bool(exit_result.corporate_action_uncertain) if exit_result else False
        ),
        details=dict(entry.details),
        model_version=model_version,
        market_state=market_state,
    )


def summarize_tail_labels(records: Sequence[TailLabelRecord]) -> dict[str, Any]:
    """按 capture_mode 分组报告净盈利率 / 净收益 / 成交率 / 尾部亏损 / 不确定率。

    ``net_profit_rate`` 的分母**只有已实现样本**：未成交与不确定样本既不算盈利
    也不算亏损，成交率单独用 ``fill_rate`` 表达（计划 §3.3）。
    """
    groups: dict[str, list[TailLabelRecord]] = {}
    for record in records:
        groups.setdefault(record.capture_mode, []).append(record)

    output: dict[str, Any] = {
        "total_records": len(records),
        "capture_modes_present": sorted(groups),
        "mixed_capture_mode": len(groups) > 1,
        "by_capture_mode": {
            mode: _summarize_group(items) for mode, items in sorted(groups.items())
        },
    }
    digests = {item.contract_digest for item in records}
    output["contract_digests"] = sorted(digests)
    output["single_contract_generation"] = len(digests) <= 1
    return output


def _summarize_group(items: Sequence[TailLabelRecord]) -> dict[str, Any]:
    total = len(items)
    confirmed = [item for item in items if item.confirmed]
    filled = [item for item in items if item.filled]
    realized = [item for item in items if item.trainable and item.net_return is not None]
    uncertain = [item for item in items if item.status == STATUS_UNCERTAIN]
    not_filled = [item for item in items if item.status == STATUS_NOT_FILLED]
    returns = sorted(float(item.net_return) for item in realized)
    profits = [value for value in returns if value > 0]
    return {
        "n_candidates": total,
        "n_confirmed": len(confirmed),
        "n_filled": len(filled),
        "n_realized": len(realized),
        "n_not_filled": len(not_filled),
        "n_uncertain": len(uncertain),
        "confirm_rate": _rate(len(confirmed), total),
        "fill_rate": _rate(len(filled), total),
        "net_profit_rate": _rate(len(profits), len(realized)),
        "mean_net_return": (sum(returns) / len(returns)) if returns else None,
        "median_net_return": returns[len(returns) // 2] if returns else None,
        "tail_loss_p05": _percentile(returns, 0.05),
        "worst_net_return": returns[0] if returns else None,
        "mean_gross_return": (
            sum(float(item.gross_return) for item in realized) / len(realized)
            if realized
            else None
        ),
        "capital_employed": sum(
            float(item.entry_price or 0.0) * item.quantity for item in filled
        ),
        "total_costs": sum(item.buy_cost + item.sell_cost for item in realized),
        "stop_loss_exits": sum(1 for item in realized if item.stop_loss_hit),
        "take_profit_exits": sum(1 for item in realized if item.take_profit_hit),
        "ambiguous_same_bar_exits": sum(
            1 for item in realized if item.ambiguous_same_bar
        ),
        "gap_exits": sum(1 for item in realized if item.gap_exit),
        "deferred_exits": sum(1 for item in realized if item.deferred_sessions > 0),
        "decision_days": sorted({item.decision_date.isoformat() for item in items}),
        "not_filled_reasons": _count_by(item.reason for item in not_filled),
        "uncertain_reasons": _count_by(item.reason for item in uncertain),
        "label_class_counts": _count_by(
            "profit" if item.label == LABEL_PROFIT else "loss" if item.label == LABEL_LOSS
            else "unlabeled"
            for item in items
        ),
    }


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return float(numerator) / float(denominator)


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    index = max(0, min(len(values) - 1, int(round(float(quantile) * (len(values) - 1)))))
    return float(values[index])


def _count_by(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value or "")
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


__all__ = [
    "CAPTURE_OBSERVED",
    "CAPTURE_REPLAYED",
    "LABEL_LOSS",
    "LABEL_PROFIT",
    "TAIL_MATURITY_RULE",
    "TAIL_NET_PROFIT_BASIS",
    "TAIL_NET_PROFIT_OUTPUT_FIELD",
    "TAIL_PRICE_BASIS",
    "TAIL_SCHEMA_VERSION",
    "TailLabelError",
    "TailLabelRecord",
    "build_tail_net_profit_label",
    "resolve_tail_slippage_ratio",
    "summarize_tail_labels",
    "tail_label_name",
    "tail_label_policy_record",
]
