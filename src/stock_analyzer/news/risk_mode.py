"""news_risk_mode 渐进启用框架（PLAN P2-3 / LLM Shadow 渐进启用）。

模式：
- ``off``：不启用 LLM 新闻风险（默认，保持现状确定性新闻规则）；
- ``shadow``：只记录 Shadow 评估（费用/延迟/错误率/置信度/人工复核一致率），
  不改变选股结果（首次部署固定为 shadow）；
- ``penalty``：negative 且置信度 >= 0.6 时强扣分；
- ``conditional_veto``：仅在置信度 >= 0.8、命中监管立案/处罚/欺诈/退市风险
  白名单、且来自官方来源或两个独立来源时，才允许硬否决。

LLM 不可用时保留确定性新闻规则并记录降级状态，不阻断整个扫描任务。
Shadow 至少运行 10 个交易日且累计 200 个有效事件，达到成功率 95%、
人工一致率 80%、严重负面召回率 90% 后才允许切换 penalty。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

VALID_MODES = ("off", "shadow", "penalty", "conditional_veto")

# 监管白名单（conditional_veto 允许硬否决的事件类型）
REGULATORY_WHITELIST_EVENTS = frozenset(
    {"立案", "处罚", "欺诈", "退市风险", "立案调查", "行政处罚", "退市"}
)

# 官方来源关键词（conditional_veto 来源要求：官方或两个独立来源）
OFFICIAL_SOURCE_HINTS = frozenset(
    {"交易所", "证监会", "上交所", "深交所", "北交所", "证券时报", "新华社", "人民日报"}
)

# Shadow 启用门槛（至少 10 个交易日且 200 个有效事件）
SHADOW_MIN_TRADE_DAYS = 10
SHADOW_MIN_EVENTS = 200
SHADOW_SUCCESS_RATE = 0.95
SHADOW_HUMAN_AGREEMENT_RATE = 0.80
SHADOW_NEGATIVE_RECALL = 0.90

PENALTY_MIN_CONFIDENCE = 0.6
VETO_MIN_CONFIDENCE = 0.8


@dataclass(slots=True)
class NewsRiskModeDecision:
    mode: str
    applied: bool
    action: str  # none | shadow_record | penalty | veto
    penalty_amount: float
    hard_veto: bool
    degraded: bool
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ShadowStats:
    trade_days: int
    valid_events: int
    success_rate: float
    human_agreement_rate: float
    negative_recall: float
    cost: float
    avg_latency_ms: float
    error_rate: float
    avg_confidence: float

    def ready_for_penalty(self) -> bool:
        """PLAN 门槛：10 交易日 + 200 事件 + 成功率 95% + 人工一致率 80% + 严重负面召回 90%。"""
        if self.trade_days < SHADOW_MIN_TRADE_DAYS:
            return False
        if self.valid_events < SHADOW_MIN_EVENTS:
            return False
        if self.success_rate < SHADOW_SUCCESS_RATE:
            return False
        if self.human_agreement_rate < SHADOW_HUMAN_AGREEMENT_RATE:
            return False
        if self.negative_recall < SHADOW_NEGATIVE_RECALL:
            return False
        return True


def _mode_from(value: object) -> str:
    mode = str(value or "").strip().lower() or "off"
    return mode if mode in VALID_MODES else "off"


def _as_float(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def evaluate_news_risk_mode(
    *,
    mode_value: object,
    decision: Mapping[str, Any],
    shadow_stats: Mapping[str, Any] | None,
    llm_available: bool,
) -> NewsRiskModeDecision:
    """按模式消费 NewsRiskDecision，产出执行动作。

    ``decision`` 来自结构化新闻风险（score/available_for_symbol/hard_veto/
    max_negative_confidence/matched_event_ids/reasons）。
    """
    mode = _mode_from(mode_value)
    reasons: list[str] = [f"mode:{mode}"]
    if not llm_available:
        # LLM 不可用 → 降级为确定性规则（本框架只记录降级，不阻断扫描）。
        return NewsRiskModeDecision(
            mode=mode,
            applied=False,
            action="none",
            penalty_amount=0.0,
            hard_veto=False,
            degraded=True,
            reasons=reasons + ["llm_unavailable_degraded"],
        )
    if not bool(decision.get("available_for_symbol", False)):
        return NewsRiskModeDecision(
            mode=mode,
            applied=False,
            action="none",
            penalty_amount=0.0,
            hard_veto=False,
            degraded=False,
            reasons=reasons + ["no_symbol_news_evidence"],
        )

    score = _as_float(decision.get("score"), default=0.5)
    max_negative_confidence = _as_float(
        decision.get("max_negative_confidence"), default=0.0
    )
    hard_veto_evidence = bool(decision.get("hard_veto", False))
    is_negative = score < 0.45
    reasons.append("negative_news" if is_negative else "positive_news")

    if mode == "off":
        return NewsRiskModeDecision(
            mode=mode,
            applied=False,
            action="none",
            penalty_amount=0.0,
            hard_veto=False,
            degraded=False,
            reasons=reasons,
        )

    if mode == "shadow":
        return NewsRiskModeDecision(
            mode=mode,
            applied=False,
            action="shadow_record",
            penalty_amount=0.0,
            hard_veto=False,
            degraded=False,
            reasons=reasons + ["shadow_only"],
        )

    if mode == "penalty":
        if is_negative and max_negative_confidence >= PENALTY_MIN_CONFIDENCE:
            # negative 且置信度 >= 0.6 时强扣分（score 0→0.45 映射扣分比例）。
            penalty = (0.5 - score) * 0.6 if score < 0.5 else 0.0
            return NewsRiskModeDecision(
                mode=mode,
                applied=True,
                action="penalty",
                penalty_amount=round(max(0.0, penalty), 4),
                hard_veto=False,
                degraded=False,
                reasons=reasons + [f"penalty_confidence_{max_negative_confidence:.2f}"],
            )
        return NewsRiskModeDecision(
            mode=mode,
            applied=False,
            action="none",
            penalty_amount=0.0,
            hard_veto=False,
            degraded=False,
            reasons=reasons + ["below_penalty_confidence"],
        )

    # conditional_veto
    if not is_negative:
        return NewsRiskModeDecision(
            mode=mode,
            applied=False,
            action="none",
            penalty_amount=0.0,
            hard_veto=False,
            degraded=False,
            reasons=reasons + ["not_negative"],
        )
    if max_negative_confidence < VETO_MIN_CONFIDENCE:
        return NewsRiskModeDecision(
            mode=mode,
            applied=False,
            action="none",
            penalty_amount=0.0,
            hard_veto=False,
            degraded=False,
            reasons=reasons + [f"confidence_below_veto_{VETO_MIN_CONFIDENCE}"],
        )
    event_types = [
        str(item)
        for item in (decision.get("event_types") or decision.get("reasons") or [])
        if str(item).strip()
    ]
    # 白名单 = 监管事件类型命中（立案/处罚/欺诈/退市风险），event_id 仅是
    # 证据标识，不构成事件类型白名单。
    whitelisted = bool({event for event in event_types} & REGULATORY_WHITELIST_EVENTS)
    if not whitelisted:
        return NewsRiskModeDecision(
            mode=mode,
            applied=False,
            action="none",
            penalty_amount=0.0,
            hard_veto=False,
            degraded=False,
            reasons=reasons + ["not_whitelisted"],
        )
    sources = [
        str(item) for item in (decision.get("sources") or []) if str(item).strip()
    ]
    official = bool({src for src in sources} & OFFICIAL_SOURCE_HINTS)
    independent_count = len({src for src in sources if src})
    if not (official or independent_count >= 2):
        return NewsRiskModeDecision(
            mode=mode,
            applied=False,
            action="none",
            penalty_amount=0.0,
            hard_veto=False,
            degraded=False,
            reasons=reasons + ["source_not_sufficient"],
        )
    if not hard_veto_evidence:
        # LLM 不得单独执行硬否决：需要 hard_veto 证据（negative 高置信度）
        # 与监管白名单双重确认。
        return NewsRiskModeDecision(
            mode=mode,
            applied=False,
            action="none",
            penalty_amount=0.0,
            hard_veto=False,
            degraded=False,
            reasons=reasons + ["hard_veto_evidence_missing"],
        )
    return NewsRiskModeDecision(
        mode=mode,
        applied=True,
        action="veto",
        penalty_amount=1.0,
        hard_veto=True,
        degraded=False,
        reasons=reasons + ["conditional_veto_applied"],
    )


def shadow_stats_ready(
    stats: ShadowStats | Mapping[str, Any],
) -> dict[str, object]:
    """Shadow 统计 → 是否达到切换 penalty 门槛（结构化输出）。"""
    if isinstance(stats, Mapping):
        stats = ShadowStats(
            trade_days=int(stats.get("trade_days", 0)),
            valid_events=int(stats.get("valid_events", 0)),
            success_rate=_as_float(stats.get("success_rate"), 0.0),
            human_agreement_rate=_as_float(stats.get("human_agreement_rate"), 0.0),
            negative_recall=_as_float(stats.get("negative_recall"), 0.0),
            cost=_as_float(stats.get("cost"), 0.0),
            avg_latency_ms=_as_float(stats.get("avg_latency_ms"), 0.0),
            error_rate=_as_float(stats.get("error_rate"), 0.0),
            avg_confidence=_as_float(stats.get("avg_confidence"), 0.0),
        )
    return {
        "ready_for_penalty": stats.ready_for_penalty(),
        "trade_days": stats.trade_days,
        "valid_events": stats.valid_events,
        "success_rate": stats.success_rate,
        "human_agreement_rate": stats.human_agreement_rate,
        "negative_recall": stats.negative_recall,
        "required": {
            "trade_days": SHADOW_MIN_TRADE_DAYS,
            "valid_events": SHADOW_MIN_EVENTS,
            "success_rate": SHADOW_SUCCESS_RATE,
            "human_agreement_rate": SHADOW_HUMAN_AGREEMENT_RATE,
            "negative_recall": SHADOW_NEGATIVE_RECALL,
        },
    }
