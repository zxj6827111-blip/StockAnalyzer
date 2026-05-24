"""Structured diagnostics for blocked recommendation signals."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Mapping

from stock_analyzer.config import StockAnalyzerConfig


class SignalQualityAuditor:
    """Summarise why scanned candidates did or did not become actionable."""

    def __init__(self, config: StockAnalyzerConfig) -> None:
        self._config = config

    def build_report(
        self,
        *,
        latest_signals: list[dict[str, object]],
        audit_events: list[dict[str, object]] | None = None,
        notification_filter_diagnostics: dict[str, object] | None = None,
        provider_status: dict[str, object] | None = None,
        week5_report: dict[str, object] | None = None,
        learning_governance: dict[str, object] | None = None,
        generated_at: datetime | None = None,
    ) -> dict[str, object]:
        signals = [item for item in latest_signals if isinstance(item, dict)]
        action_counts = Counter(_lower_str(item.get("action")) for item in signals)
        grade_counts = Counter(str(item.get("grade", "")).strip() or "unknown" for item in signals)
        scores = [_float(item.get("score")) for item in signals]
        scores = [item for item in scores if item is not None]
        gate_attribution = self._gate_attribution(signals)
        top_candidates = sorted(
            (self._candidate_summary(item) for item in signals),
            key=lambda item: float(item.get("score", 0.0)),
            reverse=True,
        )[:10]
        near_misses = [
            self._near_miss(item)
            for item in sorted(
                signals,
                key=lambda row: float(_float(row.get("score")) or 0.0),
                reverse=True,
            )
            if _lower_str(item.get("action")) != "buy"
        ][:10]
        return {
            "status": "ok" if signals else "empty",
            "generated_at": (generated_at or datetime.now()).isoformat(),
            "summary": {
                "signal_count": len(signals),
                "action_breakdown": dict(action_counts),
                "grade_breakdown": dict(grade_counts),
                "max_score": max(scores) if scores else None,
                "avg_score": round(sum(scores) / len(scores), 4) if scores else None,
            },
            "gate_attribution": gate_attribution,
            "top_candidates": top_candidates,
            "near_misses": near_misses,
            "notification_filter": notification_filter_diagnostics or {},
            "runtime_context": self._runtime_context(provider_status or {}, week5_report or {}),
            "learning_context": self._learning_context(learning_governance or {}),
            "audit_event_summary": self._audit_event_summary(audit_events or []),
            "recommended_next_actions": self._recommended_next_actions(
                signal_count=len(signals),
                gate_attribution=gate_attribution,
                notification_filter=notification_filter_diagnostics or {},
                learning_governance=learning_governance or {},
                provider_status=provider_status or {},
            ),
        }

    def _gate_attribution(self, signals: list[dict[str, object]]) -> dict[str, object]:
        counts: Counter[str] = Counter()
        examples: dict[str, list[dict[str, object]]] = {}
        for signal in signals:
            blocked = self._blocked_gates(signal)
            for gate in blocked:
                counts[gate] += 1
                examples.setdefault(gate, [])
                if len(examples[gate]) < 5:
                    examples[gate].append(self._candidate_summary(signal))
        return {
            "counts": dict(counts),
            "examples": examples,
        }

    def _blocked_gates(self, signal: Mapping[str, object]) -> list[str]:
        reasons = [_lower_str(item) for item in _list(signal.get("reasons"))]
        decision_trace = _mapping(signal.get("decision_trace"))
        blocked: list[str] = []
        cross_review_gate = _mapping(decision_trace.get("cross_review_gate"))
        if (
            cross_review_gate.get("passed") is False
            or "cross_review" in reasons
            or _cross_review_threshold_miss(signal, self._config)
        ):
            blocked.append("cross_review")
        for gate_name in ("risk_gate", "financial_gate", "liquidity_gate", "execution_risk_gate"):
            gate = _mapping(decision_trace.get(gate_name))
            if gate.get("passed") is False:
                blocked.append(gate_name)
        provider = _mapping(decision_trace.get("provider"))
        if provider.get("soft_degraded_mode") is True or provider.get("hard_degraded_mode") is True:
            blocked.append("provider_degraded")
        if "low_roe" in " ".join(reasons):
            blocked.append("financial_low_roe")
        return _dedupe_preserve_order(blocked)

    def _candidate_summary(self, signal: Mapping[str, object]) -> dict[str, object]:
        return {
            "symbol": str(signal.get("symbol", "")).strip(),
            "action": _lower_str(signal.get("action")),
            "score": _float(signal.get("score")),
            "grade": str(signal.get("grade", "")).strip(),
            "reasons": _list(signal.get("reasons"))[:5],
            "probabilities": _mapping(signal.get("probabilities")),
        }

    def _near_miss(self, signal: Mapping[str, object]) -> dict[str, object]:
        item = self._candidate_summary(signal)
        item["blocked_gates"] = self._blocked_gates(signal)
        item["cross_review_gap"] = _cross_review_gap(signal, self._config)
        item["score_gap_to_buy"] = _score_gap_to_buy(signal, self._config)
        return item

    def _runtime_context(
        self,
        provider_status: Mapping[str, object],
        week5_report: Mapping[str, object],
    ) -> dict[str, object]:
        empty_signal = _mapping(week5_report.get("empty_signal"))
        signal_pool = _mapping(week5_report.get("signal_pool"))
        return {
            "provider_soft_degraded": bool(provider_status.get("soft_degraded_mode", False)),
            "provider_hard_degraded": bool(provider_status.get("hard_degraded_mode", False)),
            "provider_degrade_reason": str(provider_status.get("degrade_reason", "")).strip(),
            "week5_empty_signal_triggered": bool(empty_signal.get("triggered", False)),
            "week5_empty_signal_reasons": _list(empty_signal.get("reasons")),
            "week5_candidate_count": _int(signal_pool.get("candidate_count")),
            "week5_buy_signals": _int(week5_report.get("buy_signals")),
        }

    def _learning_context(self, learning_governance: Mapping[str, object]) -> dict[str, object]:
        active_champion = learning_governance.get("active_champion")
        repair = _mapping(learning_governance.get("active_champion_repair"))
        config = _mapping(learning_governance.get("config"))
        return {
            "active_champion_present": isinstance(active_champion, dict) and bool(active_champion),
            "configured_active_champion_id": str(config.get("active_champion_id", "")).strip(),
            "repair_required": bool(repair.get("required", False)),
            "repair_reason": str(repair.get("reason", "")).strip(),
        }

    def _audit_event_summary(self, events: list[dict[str, object]]) -> dict[str, object]:
        event_counts = Counter(str(item.get("event_type", "")).strip() or "unknown" for item in events)
        level_counts = Counter(str(item.get("level", "")).strip() or "unknown" for item in events)
        return {
            "event_count": len(events),
            "event_type_breakdown": dict(event_counts.most_common(10)),
            "level_breakdown": dict(level_counts),
        }

    def _recommended_next_actions(
        self,
        *,
        signal_count: int,
        gate_attribution: Mapping[str, object],
        notification_filter: Mapping[str, object],
        learning_governance: Mapping[str, object],
        provider_status: Mapping[str, object],
    ) -> list[dict[str, object]]:
        counts = _mapping(gate_attribution.get("counts"))
        repair = _mapping(learning_governance.get("active_champion_repair"))
        actions: list[dict[str, object]] = []
        if repair.get("required") is True:
            actions.append(
                {
                    "priority": "P0",
                    "code": "repair_active_champion",
                    "reason": "自学习治理找不到 active champion，challenger 无法形成 proposal/ticket。",
                    "endpoint": "POST /models/registry/bootstrap-active-champion",
                }
            )
        if _int(counts.get("cross_review")) > 0:
            actions.append(
                {
                    "priority": "P0",
                    "code": "review_cross_review_thresholds",
                    "reason": "候选集中存在 cross_review 阻断，需要检查 XGB/meta 概率校准和模型分歧阈值。",
                }
            )
        if _int(notification_filter.get("rejected_by_score")) > 0:
            actions.append(
                {
                    "priority": "P1",
                    "code": "split_notification_threshold",
                    "reason": "通知层仍有分数拦截，需确认 buy/watch 分层阈值是否符合预期。",
                }
            )
        if bool(provider_status.get("soft_degraded_mode", False)):
            actions.append(
                {
                    "priority": "P1",
                    "code": "inspect_provider_degraded_mode",
                    "reason": "provider 软降级会影响风险与模型复核结果。",
                }
            )
        if signal_count == 0:
            actions.append(
                {
                    "priority": "P0",
                    "code": "restore_signal_materialization",
                    "reason": "latest signals 为空，先恢复扫描结果落地，再判断策略质量。",
                }
            )
        return actions[:8]


def _cross_review_threshold_miss(signal: Mapping[str, object], config: StockAnalyzerConfig) -> bool:
    gap = _cross_review_gap(signal, config)
    return any(float(value) > 0 for value in gap.values())


def _cross_review_gap(signal: Mapping[str, object], config: StockAnalyzerConfig) -> dict[str, float]:
    probabilities = _mapping(signal.get("probabilities"))
    p_lgbm = _float(probabilities.get("lgbm"))
    p_xgb = _float(probabilities.get("xgb"))
    p_meta = _float(probabilities.get("meta"))
    review = config.models.cross_review
    diff = abs((p_lgbm or 0.0) - (p_xgb or 0.0)) if p_lgbm is not None and p_xgb is not None else None
    return {
        "lgbm_below_min": _positive_gap(review.p_lgbm_min, p_lgbm),
        "xgb_below_min": _positive_gap(review.p_xgb_min, p_xgb),
        "meta_below_min": _positive_gap(review.p_meta_min, p_meta),
        "model_diff_excess": _positive_gap(diff, review.max_diff) if diff is not None else 0.0,
    }


def _score_gap_to_buy(signal: Mapping[str, object], config: StockAnalyzerConfig) -> float:
    score = _float(signal.get("score"))
    if score is None:
        return 0.0
    return round(max(0.0, float(config.score.thresholds.a) - score), 4)


def _positive_gap(threshold: float | None, value: float | None) -> float:
    if threshold is None or value is None:
        return 0.0
    return round(max(0.0, float(threshold) - float(value)), 4)


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _lower_str(value: object) -> str:
    return str(value or "").strip().lower()


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
