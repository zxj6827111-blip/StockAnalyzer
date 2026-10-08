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
from typing import Any

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    STATUS_FILLED,
    STATUS_UNCERTAIN,
    TrendStrategyContract,
)
from stock_analyzer.labels.tail_net_profit import (
    CAPTURE_OBSERVED,
    TailLabelRecord,
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


__all__ = [
    "MIN_FEEDBACK_DAYS",
    "MIN_FEEDBACK_SAMPLES",
    "MIN_SUGGESTED_IMPROVEMENT_PP",
    "STATE_CHALLENGER_SUGGESTED",
    "STATE_KEEP_OBSERVING",
    "STATE_SHADOW_ONLY",
    "FeedbackSlice",
    "reject_reason_feedback",
    "summarize_mature_feedback",
]
