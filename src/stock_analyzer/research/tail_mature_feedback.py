"""尾盘净盈利结果的成熟反馈闭环（改进计划 §3.4 末条）。

"成熟结果按模型版本、市场状态和拒绝原因反馈；自动学习只生成 challenger，
正式模型更新仍须经过验证和人工发布。"

两条约束写进代码：

1. 只按**已成熟**（真实可成交退出）的样本算净盈利率；未成交与不确定样本进
   成交率/不确定率，不进盈亏分母。
2. 输出永远是"建议"（challenger 候选 + 复核原因），本模块不含任何晋升动作。
   晋升仍只能走 ``learning_governance_service`` 的两阶段人工票据。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    STATUS_FILLED,
    STATUS_NOT_FILLED,
    STATUS_UNCERTAIN,
    TrendStrategyContract,
)
from stock_analyzer.labels.tail_net_profit import (
    CAPTURE_OBSERVED,
    TailLabelRecord,
)
from stock_analyzer.research.funnel_trace import (
    KIND_HARD_GATE,
    record_stage,
)

#: 自动学习最多走到这一步；再往前是人工票据的事。
STATE_CHALLENGER_SUGGESTED = "challenger_suggested"
STATE_KEEP_OBSERVING = "keep_observing"
STATE_SHADOW_ONLY = "shadow_only"

MIN_FEEDBACK_SAMPLES = 100
MIN_FEEDBACK_DAYS = 20
#: 建议生成 challenger 的最小净盈利率改善（百分点）。低于它就继续观察。
MIN_SUGGESTED_IMPROVEMENT_PP = 0.05


@dataclass(frozen=True)
class FeedbackSlice:
    """一个 (模型版本, 市场状态) 切片的成熟结果。"""

    model_version: str
    contract_digest: str
    market_state: str
    capture_mode: str
    days: int
    candidates: int
    filled: int
    uncertain: int
    realized: int
    net_profits: int
    mean_net_return: float | None
    tail_loss_p05: float | None
    reject_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def fill_rate(self) -> float | None:
        return (self.filled / self.candidates) if self.candidates else None

    @property
    def net_profit_rate(self) -> float | None:
        # 分母只含已实现样本：未成交与不确定既不是盈利也不是亏损。
        return (self.net_profits / self.realized) if self.realized else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version,
            "contract_digest": self.contract_digest,
            "market_state": self.market_state,
            "capture_mode": self.capture_mode,
            "days": self.days,
            "candidates": self.candidates,
            "filled": self.filled,
            "uncertain": self.uncertain,
            "realized": self.realized,
            "net_profits": self.net_profits,
            "fill_rate": self.fill_rate,
            "net_profit_rate": self.net_profit_rate,
            "mean_net_return": self.mean_net_return,
            "tail_loss_p05": self.tail_loss_p05,
            "reject_reasons": dict(self.reject_reasons),
        }


def summarize_mature_feedback(
    records: Sequence[TailLabelRecord],
    *,
    baseline_rate: float | None = None,
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
) -> dict[str, Any]:
    """按模型版本 / 市场状态 / 拒绝原因聚合，并给出**只到 challenger** 的建议。

    ``records`` 上的 ``model_version`` / ``market_state`` 由调用方在打标时写入
    （见 ``build_tail_net_profit_label``）；留空时归入 ``unattributed`` / ``unknown``
    而不是被丢弃，否则身份缺失的股票会悄悄从反馈里消失。
    """
    groups: dict[tuple[str, str, str, str], list[TailLabelRecord]] = {}
    for record in records:
        key = (
            str(record.model_version or "unattributed"),
            str(record.market_state or "unknown"),
            str(record.capture_mode),
            str(record.contract_digest),
        )
        groups.setdefault(key, []).append(record)

    slices: list[FeedbackSlice] = []
    for (model_version, market_state, capture_mode, digest), bucket in sorted(groups.items()):
        realized = [item for item in bucket if item.trainable and item.net_return is not None]
        returns = sorted(
            float(item.net_return) for item in realized if item.net_return is not None
        )
        slices.append(FeedbackSlice(
            model_version=model_version,
            contract_digest=digest,
            market_state=market_state,
            capture_mode=capture_mode,
            days=len({item.decision_date for item in bucket}),
            candidates=len(bucket),
            filled=sum(1 for item in bucket if item.status == STATUS_FILLED),
            uncertain=sum(1 for item in bucket if item.status == STATUS_UNCERTAIN),
            realized=len(realized),
            net_profits=sum(1 for value in returns if value > 0),
            mean_net_return=(sum(returns) / len(returns)) if returns else None,
            tail_loss_p05=_p05(returns),
            reject_reasons=_counts(item.reason for item in bucket if item.reason),
        ))

    observed = [item for item in slices if item.capture_mode == CAPTURE_OBSERVED]
    observed_realized = sum(item.realized for item in observed)
    observed_days = max((item.days for item in observed), default=0)
    overall_rate = (
        sum(item.net_profits for item in observed) / observed_realized
        if observed_realized
        else None
    )
    improvement = (
        overall_rate - float(baseline_rate)
        if (overall_rate is not None and baseline_rate is not None)
        else None
    )

    blockers: list[str] = []
    if not observed:
        blockers.append("no_observed_snapshot_samples")
    if observed_realized < MIN_FEEDBACK_SAMPLES:
        blockers.append(f"realized_observed={observed_realized}<{MIN_FEEDBACK_SAMPLES}")
    if observed_days < MIN_FEEDBACK_DAYS:
        blockers.append(f"decision_days={observed_days}<{MIN_FEEDBACK_DAYS}")
    if improvement is None:
        blockers.append("baseline_rate_not_supplied")
    elif improvement < MIN_SUGGESTED_IMPROVEMENT_PP:
        blockers.append("improvement_below_5pp")

    state = STATE_CHALLENGER_SUGGESTED if not blockers else STATE_KEEP_OBSERVING
    if not observed:
        state = STATE_SHADOW_ONLY
    return {
        "state": state,
        "blockers": blockers,
        "slices": [item.as_dict() for item in slices],
        "observed_overall": {
            "realized": observed_realized,
            "decision_days": observed_days,
            "net_profit_rate": overall_rate,
            "improvement_vs_baseline_pp": improvement,
        },
        "contract_digest": contract.digest(),
        # 这句话是契约的一部分：自动学习到此为止。
        "promotion": "manual_only_via_learning_governance_release_ticket",
    }


def _counts(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        text = str(value).strip()
        if text:
            counts[text] = counts.get(text, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _p05(sorted_returns: Sequence[float]) -> float | None:
    """最近秩 5% 分位（样本少时退化为最差值，不假装精确）。"""
    if not sorted_returns:
        return None
    rank = max(0, min(len(sorted_returns) - 1, int(round(0.05 * (len(sorted_returns) - 1)))))
    return float(sorted_returns[rank])


def reject_reason_feedback(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """被拒原因 × 后续表现：判断"这条规则是否在淘汰本该赚钱的股票"。

    ``rows`` 每行是 ``{"reason": str, "net_return": float|None, "realized": bool}``，
    由留档（``funnel_trace``）与标签（``tail_net_profit``）拼出来。
    """
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("reason") or "unspecified"), []).append(row)
    out: dict[str, Any] = {}
    for reason, bucket in sorted(grouped.items()):
        realized = [
            float(row["net_return"])
            for row in bucket
            if row.get("realized") and row.get("net_return") is not None
        ]
        out[reason] = {
            "n": len(bucket),
            "n_realized": len(realized),
            "net_profit_rate": (
                sum(1 for value in realized if value > 0) / len(realized)
                if realized else None
            ),
            "mean_net_return": sum(realized) / len(realized) if realized else None,
        }
    return out


# ---------------------------------------------------------------------------
# 成交与退出：§2 漏斗最后一层（execution_exit）的证据
# ---------------------------------------------------------------------------

DISPOSITION_PROFIT = "net_profit"
DISPOSITION_LOSS = "net_loss"
DISPOSITION_UNCERTAIN = "uncertain_exit"
DISPOSITION_PENDING = "exit_not_matured"
DISPOSITION_NOT_FILLED = "not_filled"
DISPOSITION_NO_RECORD = "no_label_record"
#: 只有真正落到可归因盈亏的退出才算"走完这一层"；其余都必须留下原因。
REALIZED_DISPOSITIONS = frozenset({DISPOSITION_PROFIT, DISPOSITION_LOSS})


def _disposition_for(row: Mapping[str, Any], record: TailLabelRecord | None) -> dict[str, Any]:
    symbol = str(row.get("symbol"))
    probability = row.get("probability")
    base = {"symbol": symbol, "probability": probability, "net_return": None,
            "exit_date": None, "label_mature_time": None}
    if record is None:
        # 推荐留档里有、标签留档里没有：绝不能当成"没赚没亏"混过去。
        return {**base, "disposition": DISPOSITION_NO_RECORD, "reason": "no_label_record"}
    entry = {
        "net_return": record.net_return,
        "exit_date": (record.label_mature_time.date().isoformat()
                      if record.label_mature_time else None),
        "label_mature_time": (record.label_mature_time.isoformat()
                              if record.label_mature_time else None),
    }
    if record.status == STATUS_NOT_FILLED or not record.filled:
        return {**base, **entry, "disposition": DISPOSITION_NOT_FILLED,
                "reason": record.reason or "not_filled"}
    if record.status == STATUS_UNCERTAIN:
        return {**base, **entry, "disposition": DISPOSITION_UNCERTAIN,
                "reason": record.reason or "uncertain"}
    if record.trainable and record.net_return is not None:
        value = float(record.net_return)
        return {**base, **entry,
                "disposition": DISPOSITION_PROFIT if value > 0.0 else DISPOSITION_LOSS,
                "reason": record.reason or ""}
    return {**base, **entry, "disposition": DISPOSITION_PENDING,
            "reason": record.reason or "holding_not_matured"}


def attach_exit_outcomes(
    *,
    shadow_report: Mapping[str, Any],
    records: Sequence[TailLabelRecord],
    trade_date: date | str | None = None,
) -> dict[str, Any]:
    """把某日的最终推荐与其后的成熟退出对上，产出"成交与退出"层的证据。

    ``trade_date`` 缺省时取留档自己的日期；显式给一个不一致的日期会直接报错——
    把 A 日的推荐接到 B 日的退出上，产出的净盈利率是假的。
    """
    rows = [dict(item) for item in shadow_report.get("final_recommendations") or []]
    archived = str(shadow_report.get("trade_date") or "")
    day = str(trade_date)[:10] if trade_date else archived
    if not day:
        raise ValueError("execution_exit needs a trade_date (report or argument)")
    if archived and day != archived:
        raise ValueError(f"report is dated {archived}, not {day}")
    parsed = date.fromisoformat(day)

    by_symbol = {
        str(record.symbol): record
        for record in records if record.decision_date == parsed
    }
    dispositions = {
        str(row.get("symbol")): _disposition_for(row, by_symbol.get(str(row.get("symbol"))))
        for row in rows
    }
    realized = [
        item for item in dispositions.values()
        if item["disposition"] in REALIZED_DISPOSITIONS
    ]
    profits = [item for item in realized if item["disposition"] == DISPOSITION_PROFIT]
    counts: dict[str, int] = {}
    for item in dispositions.values():
        counts[str(item["disposition"])] = counts.get(str(item["disposition"]), 0) + 1
    identities = [row.get("model_identity") for row in rows if row.get("model_identity")]
    identity = next(
        (item for item in identities if item.get("identity_recorded")),
        {"identity_recorded": False, "reason": "archived_identity_missing"},
    )
    moments = [
        item["label_mature_time"] for item in dispositions.values() if item["label_mature_time"]
    ]
    caveats: list[str] = []
    if not rows:
        caveats.append("no_final_recommendations_archived")
    if counts.get(DISPOSITION_NO_RECORD):
        caveats.append("recommended_symbols_without_label_record")
    if counts.get(DISPOSITION_PENDING):
        caveats.append("exit_not_matured_yet")
    return {
        "trade_date": day,
        "recommended": sorted(dispositions),
        "dispositions": dispositions,
        "counts": dict(sorted(counts.items())),
        "realized": len(realized),
        "net_profits": len(profits),
        "net_profit_rate": (len(profits) / len(realized)) if realized else None,
        "mean_net_return": (
            sum(float(item["net_return"]) for item in realized) / len(realized)
            if realized else None
        ),
        "maturity_pending": sorted(
            symbol for symbol, item in dispositions.items()
            if item["disposition"] in {DISPOSITION_PENDING, DISPOSITION_NO_RECORD}
        ),
        "model_identity": identity,
        "data_as_of": max(moments) if moments else day,
        "contract_digest": str(shadow_report.get("contract_digest") or ""),
        "caveats": caveats,
    }


def execution_exit_stage(
    *,
    exits: Mapping[str, Any],
    contract: TrendStrategyContract = DEFAULT_TREND_CONTRACT,
) -> Any:
    """把上面那份证据落成漏斗的一层（硬门：资金与可成交性，不是预测性规则）。"""
    dispositions = dict(exits.get("dispositions") or {})
    recommended = [str(symbol) for symbol in exits.get("recommended") or []]
    advanced = [
        symbol for symbol in recommended
        if dispositions.get(symbol, {}).get("disposition") in REALIZED_DISPOSITIONS
    ]
    rejected: dict[str, list[str]] = {}
    for symbol in recommended:
        item = dispositions.get(symbol) or {}
        disposition = str(item.get("disposition") or DISPOSITION_NO_RECORD)
        if disposition in REALIZED_DISPOSITIONS:
            continue
        rejected.setdefault(disposition, []).append(symbol)
    probabilities = {
        symbol: float(dispositions[symbol]["probability"])
        for symbol in recommended
        if dispositions.get(symbol, {}).get("probability") is not None
    }
    identity = exits.get("model_identity")
    return record_stage(
        stage="execution_exit",
        kind=KIND_HARD_GATE,
        input_symbols=recommended,
        advanced_symbols=advanced,
        rejected=rejected,
        calibrated_probabilities=probabilities,
        model_identity=identity if identity and identity.get("identity_recorded") else None,
        data_as_of=str(exits.get("data_as_of") or exits.get("trade_date")),
        contract=contract,
        label_policy_id=str((identity or {}).get("label_policy_id") or ""),
        feature_compute_version=int((identity or {}).get("feature_compute_version") or 0),
        notes=(
            "晋级=退出已实现且可归因；未成交/不确定/未成熟都留在拒绝原因里，"
            "净盈利率分母只含已实现样本"
        ),
    )


__all__ = [
    "DISPOSITION_LOSS",
    "DISPOSITION_NO_RECORD",
    "DISPOSITION_NOT_FILLED",
    "DISPOSITION_PENDING",
    "DISPOSITION_PROFIT",
    "DISPOSITION_UNCERTAIN",
    "MIN_FEEDBACK_DAYS",
    "MIN_FEEDBACK_SAMPLES",
    "MIN_SUGGESTED_IMPROVEMENT_PP",
    "REALIZED_DISPOSITIONS",
    "STATE_CHALLENGER_SUGGESTED",
    "STATE_KEEP_OBSERVING",
    "STATE_SHADOW_ONLY",
    "FeedbackSlice",
    "attach_exit_outcomes",
    "execution_exit_stage",
    "reject_reason_feedback",
    "summarize_mature_feedback",
]
