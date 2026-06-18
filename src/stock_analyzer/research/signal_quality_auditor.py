"""Structured diagnostics for blocked recommendation signals."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from typing import Any

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
        execution_risk = _execution_risk_context(signals)
        signal_loss_funnel = self._signal_loss_funnel(
            signals=signals,
            audit_events=audit_events or [],
        )
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
            "signal_loss_funnel": signal_loss_funnel,
            "execution_risk_context": execution_risk,
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
                execution_risk=execution_risk,
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

    def _signal_loss_funnel(
        self,
        *,
        signals: list[dict[str, object]],
        audit_events: list[dict[str, object]],
    ) -> dict[str, object]:
        model_threshold = (
            _float(getattr(self._config.walk_forward, "decision_threshold", 0.0)) or 0.0
        )
        signal_stages = {
            "raw_candidates": len(signals),
            "model_output_available": 0,
            "model_threshold_pass": 0,
            "score_buy_threshold_pass": 0,
            "cross_review_pass": 0,
            "financial_gate_pass": 0,
            "liquidity_gate_pass": 0,
            "risk_gate_pass": 0,
            "actionable_buy": 0,
            "positive_target_position": 0,
        }
        unknown = Counter()
        losses: Counter[str] = Counter()
        examples: dict[str, list[dict[str, object]]] = {}

        for signal in signals:
            if _probability_available(signal):
                signal_stages["model_output_available"] += 1
            else:
                _add_loss_example("model_output_missing", signal, losses, examples, self)

            meta_prob = _float(_mapping(signal.get("probabilities")).get("meta"))
            if meta_prob is None:
                unknown["model_threshold_pass"] += 1
            elif meta_prob >= model_threshold:
                signal_stages["model_threshold_pass"] += 1
            else:
                _add_loss_example("model_threshold_block", signal, losses, examples, self)

            score_gap = _score_gap_to_buy(signal, self._config)
            if score_gap <= 0:
                signal_stages["score_buy_threshold_pass"] += 1
            else:
                _add_loss_example("score_buy_threshold_block", signal, losses, examples, self)

            gate_values = {
                "cross_review_pass": _gate_passed(signal, "cross_review_gate"),
                "financial_gate_pass": _gate_passed(signal, "financial_gate"),
                "liquidity_gate_pass": _gate_passed(signal, "liquidity_gate"),
                "risk_gate_pass": _gate_passed(signal, "risk_gate"),
            }
            for stage_name, passed in gate_values.items():
                if passed is True:
                    signal_stages[stage_name] += 1
                elif passed is False:
                    _add_loss_example(
                        stage_name.replace("_pass", "_block"),
                        signal,
                        losses,
                        examples,
                        self,
                    )
                else:
                    unknown[stage_name] += 1

            if _lower_str(signal.get("action")) == "buy":
                signal_stages["actionable_buy"] += 1
            else:
                _add_loss_example("non_buy_final_action", signal, losses, examples, self)
            if (_float(signal.get("target_position")) or 0.0) > 0:
                signal_stages["positive_target_position"] += 1
            elif _lower_str(signal.get("action")) == "buy":
                _add_loss_example("zero_target_position", signal, losses, examples, self)

        execution = _execution_funnel_from_events(audit_events)
        symbol_ledger = _symbol_funnel_ledger(
            signals=signals,
            execution_records=execution["records"],
            config=self._config,
        )
        top_loss_drivers = [
            {"code": code, "count": int(count)}
            for code, count in (losses + Counter(execution.get("loss_counts", {}))).most_common(12)
            if int(count) > 0
        ]
        return {
            "status": "ok" if signals or execution.get("event_count", 0) else "empty",
            "model_threshold": model_threshold,
            "signal_stages": _stage_payload(signal_stages, denominator=max(1, len(signals))),
            "unknown_stage_counts": dict(unknown),
            "loss_counts": dict(losses),
            "loss_examples": examples,
            "execution_stages": execution["stages"],
            "execution_attempts": execution["attempts"],
            "advisory_attempts": execution["advisory_attempts"],
            "dry_run_attempts": execution["dry_run_attempts"],
            "execution_status_breakdown": execution["status_breakdown"],
            "execution_reason_breakdown": execution["reason_breakdown"],
            "outcome_observation": execution["outcome_observation"],
            "symbol_ledger": symbol_ledger,
            "top_loss_drivers": top_loss_drivers,
            "data_gaps": _funnel_data_gaps(signals=signals, execution=execution),
        }

    def _blocked_gates(self, signal: Mapping[str, object]) -> list[str]:
        return _blocked_gates(signal, self._config)

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
        event_counts = Counter(
            str(item.get("event_type", "")).strip() or "unknown" for item in events
        )
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
        execution_risk: Mapping[str, object],
    ) -> list[dict[str, object]]:
        counts = _mapping(gate_attribution.get("counts"))
        repair = _mapping(learning_governance.get("active_champion_repair"))
        actions: list[dict[str, object]] = []
        if repair.get("required") is True:
            actions.append(
                {
                    "priority": "P0",
                    "code": "repair_active_champion",
                    "reason": (
                        "自学习治理找不到 active champion，"
                        "challenger 无法形成 proposal/ticket。"
                    ),
                    "endpoint": "POST /models/registry/bootstrap-active-champion",
                }
            )
        if _int(counts.get("cross_review")) > 0:
            actions.append(
                {
                    "priority": "P0",
                    "code": "review_cross_review_thresholds",
                    "reason": (
                        "候选集中存在 cross_review 阻断，"
                        "需要检查 XGB/meta 概率校准和模型分歧阈值。"
                    ),
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
        if bool(execution_risk.get("artifact_unavailable", False)):
            actions.append(
                {
                    "priority": "P1",
                    "code": "train_execution_risk_artifact",
                    "reason": (
                        "execution-risk artifact is unavailable; "
                        "week5 falls back to shortlist order."
                    ),
                    "endpoint": "POST /train/execution-risk/run",
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


def _blocked_gates(signal: Mapping[str, object], config: StockAnalyzerConfig) -> list[str]:
    reasons = [_lower_str(item) for item in _list(signal.get("reasons"))]
    decision_trace = _mapping(signal.get("decision_trace"))
    blocked: list[str] = []
    cross_review_gate = _mapping(decision_trace.get("cross_review_gate"))
    if (
        cross_review_gate.get("passed") is False
        or "cross_review" in reasons
        or _cross_review_threshold_miss(signal, config)
    ) and "model_disagreement_probe" not in reasons:
        blocked.append("cross_review")
    for gate_name in ("risk_gate", "financial_gate", "liquidity_gate", "execution_risk_gate"):
        gate = _mapping(decision_trace.get(gate_name))
        if gate.get("passed") is False or gate.get("allowed") is False:
            blocked.append(gate_name)
    provider = _mapping(decision_trace.get("provider"))
    if provider.get("soft_degraded_mode") is True or provider.get("hard_degraded_mode") is True:
        blocked.append("provider_degraded")
    if "low_roe" in " ".join(reasons):
        blocked.append("financial_low_roe")
    return _dedupe_preserve_order(blocked)


def _cross_review_gap(
    signal: Mapping[str, object],
    config: StockAnalyzerConfig,
) -> dict[str, float]:
    probabilities = _mapping(signal.get("probabilities"))
    p_lgbm = _float(probabilities.get("lgbm"))
    p_xgb = _float(probabilities.get("xgb"))
    p_meta = _float(probabilities.get("meta"))
    review = config.models.cross_review
    diff = (
        abs((p_lgbm or 0.0) - (p_xgb or 0.0))
        if p_lgbm is not None and p_xgb is not None
        else None
    )
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
    threshold = _score_threshold_for_signal(signal, config)
    return round(max(0.0, threshold - score), 4)


def _score_threshold_for_signal(signal: Mapping[str, object], config: StockAnalyzerConfig) -> float:
    strategy = str(signal.get("strategy", "")).strip()
    if strategy and strategy in config.strategy_scores:
        return float(config.strategy_scores[strategy].thresholds.a)
    return float(config.score.thresholds.a)


def _execution_risk_context(signals: list[dict[str, object]]) -> dict[str, object]:
    reason_counts: Counter[str] = Counter()
    applied = 0
    for signal in signals:
        if bool(signal.get("execution_rerank_applied", False)):
            applied += 1
        reason = str(signal.get("execution_rerank_reason", "")).strip()
        if reason:
            reason_counts[reason] += 1
    artifact_unavailable = any(
        reason in {
            "execution_risk_artifact_unavailable",
            "execution_risk_artifact_missing",
        }
        for reason in reason_counts
    )
    return {
        "candidate_count": len(signals),
        "rerank_applied_count": applied,
        "reason_counts": dict(reason_counts),
        "artifact_unavailable": artifact_unavailable,
    }


def _execution_funnel_from_events(events: list[dict[str, object]]) -> dict[str, object]:
    attempts: Counter[str] = Counter()
    dry_run_attempts: Counter[str] = Counter()
    advisory_attempts: Counter[str] = Counter()
    dry_run_status_breakdown: Counter[str] = Counter()
    dry_run_reason_breakdown: Counter[str] = Counter()
    status_breakdown: Counter[str] = Counter()
    reason_breakdown: Counter[str] = Counter()
    realized_returns: list[float] = []
    execution_records_by_symbol: dict[str, list[dict[str, object]]] = {}
    blocked_event_records_by_symbol: dict[str, list[dict[str, object]]] = {}
    blocked_attempt_totals: Counter[str] = Counter()
    blocked_detail_totals: Counter[str] = Counter()
    event_count = 0
    execution_records = 0

    for event in events:
        event_type = _lower_str(event.get("event_type"))
        payload = _mapping(event.get("payload"))
        if event_type == "pipeline_run":
            event_count += 1
            execution_mode = _lower_str(payload.get("execution_mode"))
            portfolio_update = _mapping(payload.get("portfolio_update"))
            is_dry_run = (
                execution_mode == "portfolio_auto_apply_dry_run"
                or "dry_run" in execution_mode
                or bool(payload.get("dry_run_execution", False))
                or bool(portfolio_update.get("dry_run", False))
            )
            if execution_mode == "advisory_only":
                target_attempts = advisory_attempts
                raw_attempts = _mapping(portfolio_update.get("advisory_attempts")) or _mapping(
                    portfolio_update.get("execution_attempts")
                )
            elif is_dry_run:
                target_attempts = dry_run_attempts
                raw_attempts = _mapping(portfolio_update.get("dry_run_attempts")) or _mapping(
                    portfolio_update.get("execution_attempts")
                )
            else:
                target_attempts = attempts
                raw_attempts = _mapping(portfolio_update.get("execution_attempts"))
            for key, value in raw_attempts.items():
                normalized_key = str(key)
                count = _int(value)
                target_attempts[normalized_key] += count
                if (
                    execution_mode != "advisory_only"
                    and not is_dry_run
                    and normalized_key in {"pre_trade_blocked", "risk_gate_blocked"}
                ):
                    blocked_attempt_totals[normalized_key] += count
            raw_executions = portfolio_update.get("executions")
            if isinstance(raw_executions, list):
                for item in raw_executions:
                    if not isinstance(item, Mapping):
                        continue
                    if is_dry_run or execution_mode == "advisory_only":
                        status = _lower_str(item.get("status")) or "unknown"
                        reason = str(item.get("reason", "")).strip() or "unknown"
                        if is_dry_run:
                            dry_run_status_breakdown[status] += 1
                            dry_run_reason_breakdown[reason] += 1
                        continue
                    execution_records += 1
                    status = _lower_str(item.get("status")) or "unknown"
                    reason = str(item.get("reason", "")).strip() or "unknown"
                    status_breakdown[status] += 1
                    reason_breakdown[reason] += 1
                    normalized_execution = _execution_record_summary(item)
                    symbol = str(normalized_execution.get("symbol", "")).strip()
                    if symbol:
                        execution_records_by_symbol.setdefault(symbol, []).append(
                            normalized_execution
                        )
                    block_category = _lower_str(normalized_execution.get("block_category"))
                    counted_block_category = _blocked_category_for_record(normalized_execution)
                    if counted_block_category:
                        blocked_detail_totals[counted_block_category] += 1
                    if block_category:
                        status_breakdown[f"block_category:{block_category}"] += 1
                    realized = _float(item.get("realized_return_pct"))
                    if realized is not None:
                        realized_returns.append(realized)
            continue

        if event_type in {"pre_trade_blocked", "risk_gate_blocked"}:
            event_count += 1
            blocked_payload = _mapping(event.get("payload"))
            reason = str(blocked_payload.get("reason", "")).strip() or "unknown"
            symbol = str(blocked_payload.get("symbol", "")).strip()
            if symbol:
                blocked_event_records_by_symbol.setdefault(symbol, []).append(
                    {
                        "symbol": symbol,
                        "status": event_type,
                        "block_category": event_type,
                        "reason": reason,
                        "quantity": _int(blocked_payload.get("quantity")),
                        "target_position": _float(blocked_payload.get("target_position")),
                        "event_id": str(event.get("event_id", "")).strip(),
                        "trace_id": str(event.get("trace_id", "")).strip(),
                        "timestamp": str(event.get("timestamp", "")).strip(),
                    }
                )

    aggregate_backfilled_totals: Counter[str] = Counter()
    for symbol, records in blocked_event_records_by_symbol.items():
        existing = list(execution_records_by_symbol.get(symbol, []))
        matched_existing_record_indices: set[int] = set()
        for record in records:
            match_index = _matching_block_record_index(
                record,
                existing,
                matched_existing_record_indices,
            )
            if match_index is not None:
                matched_existing_record_indices.add(match_index)
                continue
            block_category = _blocked_category_for_record(record)
            aggregate_missing = max(
                0,
                blocked_attempt_totals.get(block_category, 0)
                - blocked_detail_totals.get(block_category, 0),
            )
            aggregate_backfill = (
                bool(block_category)
                and aggregate_backfilled_totals.get(block_category, 0) < aggregate_missing
            )
            if aggregate_backfill:
                aggregate_backfilled_totals[block_category] += 1
            elif block_category:
                attempts[block_category] += 1
            status = str(record.get("status", "")).strip()
            if status:
                status_breakdown[status] += 1
            reason = str(record.get("reason", "")).strip() or "unknown"
            reason_breakdown[reason] += 1
            execution_records_by_symbol.setdefault(symbol, []).append(record)
            execution_records += 1

    buy_signals = attempts.get("buy_signals", 0)
    buy_attempted = attempts.get("buy_new_attempted", 0)
    buy_filled = attempts.get("buy_new_filled", 0)
    buy_rejected = attempts.get("buy_new_rejected", 0)
    loss_counts = Counter(
        {
            "pre_trade_blocked": attempts.get("pre_trade_blocked", 0),
            "risk_gate_blocked": attempts.get("risk_gate_blocked", 0),
            "buy_rejected": buy_rejected,
            "entry_no_fill": attempts.get("entry_no_fill_count", 0),
        }
    )
    loss_counts += Counter(
        {
            f"execution_status:{status}": count
            for status, count in status_breakdown.items()
            if status not in {
                "opened",
                "adjusted",
                "closed",
                "trimmed",
                "pre_trade_blocked",
                "risk_gate_blocked",
            }
            and not status.startswith("block_category:")
        }
    )
    profitable = sum(1 for item in realized_returns if item > 0)
    outcome_status = "observed" if realized_returns else "unknown"
    return {
        "event_count": event_count,
        "attempts": dict(attempts),
        "dry_run_attempts": dict(dry_run_attempts),
        "advisory_attempts": dict(advisory_attempts),
        "status_breakdown": dict(status_breakdown),
        "reason_breakdown": dict(reason_breakdown.most_common(12)),
        "dry_run_status_breakdown": dict(dry_run_status_breakdown),
        "dry_run_reason_breakdown": dict(dry_run_reason_breakdown.most_common(12)),
        "loss_counts": dict(loss_counts),
        "stages": {
            "buy_signals": buy_signals,
            "buy_new_attempted": buy_attempted,
            "buy_new_filled": buy_filled,
            "buy_new_rejected": buy_rejected,
            "pre_trade_blocked": attempts.get("pre_trade_blocked", 0),
            "risk_gate_blocked": attempts.get("risk_gate_blocked", 0),
            "sell_executed": attempts.get("sell_executed", 0),
            "profitable_observed": profitable,
        },
        "outcome_observation": {
            "status": outcome_status,
            "observed_returns": len(realized_returns),
            "profitable_count": profitable,
            "avg_realized_return_pct": (
                round(sum(realized_returns) / len(realized_returns), 6)
                if realized_returns
                else None
            ),
            "note": (
                "no mature realized return found in supplied audit events"
                if not realized_returns
                else "realized return observed from execution records"
            ),
        },
        "execution_records": execution_records,
        "records": execution_records_by_symbol,
    }


def _symbol_funnel_ledger(
    *,
    signals: list[dict[str, object]],
    execution_records: Mapping[str, object],
    config: StockAnalyzerConfig,
) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    symbols = {
        str(signal.get("symbol", "")).strip()
        for signal in signals
        if str(signal.get("symbol", "")).strip()
    }
    symbols.update(str(symbol).strip() for symbol in execution_records if str(symbol).strip())
    signal_by_symbol = {
        str(signal.get("symbol", "")).strip(): signal
        for signal in signals
        if str(signal.get("symbol", "")).strip()
    }
    for symbol in sorted(symbols):
        signal = signal_by_symbol.get(symbol, {})
        executions = (
            _list(execution_records.get(symbol))
            if isinstance(execution_records, Mapping)
            else []
        )
        normalized_executions = [
            dict(item) for item in executions if isinstance(item, Mapping)
        ]
        blockers = _symbol_blockers(signal, normalized_executions, config)
        rows.append(
            {
                "symbol": symbol,
                "strategy": str(signal.get("strategy", "")).strip(),
                "action": _lower_str(signal.get("action")) if signal else "execution_only",
                "score": _float(signal.get("score")),
                "grade": str(signal.get("grade", "")).strip(),
                "target_position": _float(signal.get("target_position")),
                "probabilities": _mapping(signal.get("probabilities")),
                "stage": _symbol_stage(signal, normalized_executions, blockers),
                "blockers": blockers,
                "blocked_gate_count": len(blockers),
                "execution_statuses": [
                    str(item.get("status", "")).strip() for item in normalized_executions
                ],
                "block_categories": _dedupe_preserve_order(
                    [
                        _lower_str(item.get("block_category"))
                        for item in normalized_executions
                        if _lower_str(item.get("block_category"))
                    ]
                ),
                "execution_reasons": _dedupe_preserve_order(
                    [
                        str(item.get("reason", "")).strip()
                        for item in normalized_executions
                        if str(item.get("reason", "")).strip()
                    ]
                ),
                "filled": any(
                    _lower_str(item.get("status")) in {"opened", "adjusted", "closed", "trimmed"}
                    for item in normalized_executions
                ),
                "outcome_status": _symbol_outcome_status(normalized_executions),
            }
        )
    blocker_counts = Counter(
        blocker
        for row in rows
        for blocker in _list(row.get("blockers"))
        if isinstance(blocker, str)
    )
    return {
        "records": len(rows),
        "items": rows[:200],
        "truncated": len(rows) > 200,
        "blocker_counts": dict(blocker_counts.most_common(20)),
    }


def _symbol_blockers(
    signal: Mapping[str, object],
    executions: list[dict[str, object]],
    config: StockAnalyzerConfig,
) -> list[str]:
    blockers: list[str] = []
    if signal:
        if not _probability_available(signal):
            blockers.append("model_output_missing")
        meta_prob = _float(_mapping(signal.get("probabilities")).get("meta"))
        model_threshold = _float(getattr(config.walk_forward, "decision_threshold", 0.0)) or 0.0
        if meta_prob is not None and meta_prob < model_threshold:
            blockers.append("model_threshold_block")
        if _score_gap_to_buy(signal, config) > 0:
            blockers.append("score_buy_threshold_block")
        blockers.extend(gate.replace("_gate", "") for gate in _blocked_gates(signal, config))
        if _lower_str(signal.get("action")) != "buy":
            blockers.append("non_buy_final_action")
        elif (_float(signal.get("target_position")) or 0.0) <= 0:
            blockers.append("zero_target_position")
    else:
        blockers.append("missing_signal_snapshot")

    for execution in executions:
        block_category = _lower_str(execution.get("block_category"))
        status = _lower_str(execution.get("status"))
        if block_category in {"pre_trade_blocked", "risk_gate_blocked"}:
            blockers.append(block_category)
        elif status in {"pre_trade_blocked", "risk_gate_blocked"}:
            blockers.append(status)
        elif status.startswith("rejected"):
            blockers.append(f"execution_status:{status}")
    return _dedupe_preserve_order(blockers)


def _symbol_stage(
    signal: Mapping[str, object],
    executions: list[dict[str, object]],
    blockers: list[str],
) -> str:
    if any(
        _lower_str(item.get("status")) in {"opened", "adjusted", "closed", "trimmed"}
        for item in executions
    ):
        return "filled"
    if any(_lower_str(item.get("block_category")) == "pre_trade_blocked" for item in executions):
        return "pre_trade_blocked"
    if any(_lower_str(item.get("block_category")) == "risk_gate_blocked" for item in executions):
        return "risk_gate_blocked"
    if any(_lower_str(item.get("status")).startswith("rejected") for item in executions):
        return "execution_rejected"
    if not signal:
        return "execution_without_signal"
    if _lower_str(signal.get("action")) == "buy":
        return "buy_execution_evidence_missing"
    if blockers:
        return blockers[0]
    return "observed_no_blocker"


def _symbol_outcome_status(executions: list[dict[str, object]]) -> dict[str, object]:
    returns = [
        _float(item.get("realized_return_pct"))
        for item in executions
        if _float(item.get("realized_return_pct")) is not None
    ]
    realized = [float(item) for item in returns if item is not None]
    if not realized:
        return {"status": "unknown", "profitable": None, "realized_return_pct": None}
    avg_return = round(sum(realized) / len(realized), 6)
    return {
        "status": "observed",
        "profitable": avg_return > 0,
        "realized_return_pct": avg_return,
    }


def _execution_record_summary(item: Mapping[str, object]) -> dict[str, object]:
    fields = (
        "symbol",
        "status",
        "block_category",
        "reason",
        "side",
        "strategy",
        "target_position",
        "quantity",
        "amount",
        "price",
        "recommendation_id",
        "snapshot_id",
        "realized_return_pct",
    )
    return {field: item[field] for field in fields if field in item}


def _blocked_category_for_record(record: Mapping[str, object]) -> str:
    block_category = _lower_str(record.get("block_category"))
    status = _lower_str(record.get("status"))
    reason = _lower_str(record.get("reason"))
    if block_category in {"pre_trade_blocked", "risk_gate_blocked"}:
        return block_category
    if status in {"pre_trade_blocked", "risk_gate_blocked"}:
        return status
    if status.startswith("rejected"):
        if reason in {
            "auto_simulated_buy_no_cash",
            "auto_simulated_buy_no_cash_after_fee",
            "auto_simulated_buy_quantity_zero",
        }:
            return "pre_trade_blocked"
        if reason in {
            "auto_simulated_buy_max_holdings",
            "auto_simulated_buy_same_sector",
            "max_position_limit_reached",
            "max_holdings_reached",
        }:
            return "risk_gate_blocked"
    return ""


def _matching_block_record_index(
    candidate: Mapping[str, object],
    existing_records: list[dict[str, object]],
    consumed_indices: set[int],
) -> int | None:
    candidate_symbol = str(candidate.get("symbol", "")).strip()
    candidate_block = _lower_str(candidate.get("block_category"))
    candidate_status = _lower_str(candidate.get("status"))
    candidate_reason = str(candidate.get("reason", "")).strip()
    for index, item in enumerate(existing_records):
        if index in consumed_indices:
            continue
        if str(item.get("symbol", "")).strip() != candidate_symbol:
            continue
        item_block = _lower_str(item.get("block_category"))
        item_status = _lower_str(item.get("status"))
        item_reason = str(item.get("reason", "")).strip()
        same_reason = not candidate_reason or not item_reason or item_reason == candidate_reason
        same_block = (
            item_block == candidate_block
            or item_status == candidate_status
            or (
                same_reason
                and candidate_block in {"pre_trade_blocked", "risk_gate_blocked"}
                and item_status.startswith("rejected")
            )
        )
        if same_block and same_reason:
            return index
    return None


def _stage_payload(stages: Mapping[str, int], denominator: int) -> dict[str, object]:
    return {
        key: {
            "count": int(value),
            "rate": round(int(value) / denominator, 6) if denominator > 0 else 0.0,
        }
        for key, value in stages.items()
    }


def _funnel_data_gaps(
    *,
    signals: list[dict[str, object]],
    execution: Mapping[str, object],
) -> list[str]:
    gaps: list[str] = []
    if not signals:
        gaps.append("latest_signals_empty")
    if _int(execution.get("event_count")) <= 0:
        gaps.append("pipeline_audit_events_missing")
    if _int(execution.get("execution_records")) <= 0:
        gaps.append("portfolio_execution_records_missing")
    outcome = _mapping(execution.get("outcome_observation"))
    if outcome.get("status") != "observed":
        gaps.append("mature_profit_outcomes_missing")
    return gaps


def _add_loss_example(
    code: str,
    signal: Mapping[str, object],
    losses: Counter[str],
    examples: dict[str, list[dict[str, object]]],
    auditor: SignalQualityAuditor,
) -> None:
    losses[code] += 1
    examples.setdefault(code, [])
    if len(examples[code]) < 5:
        examples[code].append(auditor._candidate_summary(signal))


def _probability_available(signal: Mapping[str, object]) -> bool:
    probabilities = _mapping(signal.get("probabilities"))
    return any(_float(probabilities.get(key)) is not None for key in ("lgbm", "xgb", "meta"))


def _gate_passed(signal: Mapping[str, object], gate_name: str) -> bool | None:
    trace = _mapping(signal.get("decision_trace"))
    gate = _mapping(trace.get(gate_name))
    if not gate:
        return None
    if "passed" in gate:
        return bool(gate.get("passed"))
    if "allowed" in gate:
        return bool(gate.get("allowed"))
    return None


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
