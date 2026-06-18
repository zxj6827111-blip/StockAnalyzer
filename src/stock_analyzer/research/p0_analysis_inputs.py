"""Build read-only P0 analysis inputs from runtime and model artifacts."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from statistics import mean

from stock_analyzer.config import StockAnalyzerConfig
from stock_analyzer.signal.cross_review import evaluate_cross_review

_PROBABILITY_KEYS = ("lgbm", "xgb", "meta")
_MODEL_THRESHOLD_GRID = {
    "xgb_min": (0.25, 0.30, 0.33),
    "meta_min": (0.45, 0.48, 0.50),
    "max_diff": (0.18, 0.25, 0.30),
    "score_min": (40.0, 45.0, 50.0, 55.0),
}
_FEATURE_FAMILIES = {
    "financial_background": (
        "financial",
        "low_roe",
        "roe",
        "debt",
        "st",
        "delisting",
        "bg_",
        "background",
    ),
    "capital_flow": (
        "holder",
        "northbound",
        "financing",
        "block_trade",
        "main_force",
        "fund_flow",
        "capital_flow",
    ),
    "market_relative": (
        "market_relative",
        "benchmark",
        "relative",
        "regime",
        "market",
        "m2_",
    ),
    "liquidity_volume": (
        "liquidity",
        "turnover",
        "volume",
        "float_market_cap",
        "spread",
        "depth",
    ),
    "model_probability": (
        "cross_review",
        "model_diff",
        "xgb<",
        "meta<",
        "lgbm<",
        "model_disagreement",
        "probability",
    ),
    "risk_execution": (
        "risk_gate",
        "execution_risk",
        "pre_trade",
        "no_cash",
        "same_sector",
        "max_holdings",
    ),
    "price_momentum": (
        "momentum",
        "ret_",
        "trend",
        "ma_",
        "ema_",
        "rsi",
        "macd",
        "breakout",
    ),
}


def build_model_diagnosis_final(
    *,
    model_artifact_path: Path,
    learning_manifest_paths: Sequence[Path] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    """Summarize trainability and label health without retraining a model."""
    model = _load_json(model_artifact_path)
    manifests = [_load_json(path) for path in learning_manifest_paths or []]
    manifests = [item for item in manifests if item]
    training_metrics = _mapping(model.get("training_metrics"))
    metadata = _mapping(model.get("metadata"))
    train_samples = _int(metadata.get("train_samples"))
    calibration_samples = _int(
        metadata.get("calibration_samples"), fallback=training_metrics.get("calibration_samples")
    )
    test_samples = _int(metadata.get("test_samples"), fallback=training_metrics.get("test_samples"))
    test_positive_rate = _float(training_metrics.get("positive_rate"))
    test_positive = _positive_count(samples=test_samples, positive_rate=test_positive_rate)
    status = _label_health_status(
        samples=test_samples,
        positive=test_positive,
        rate=test_positive_rate,
    )
    return {
        "report_type": "model_diagnosis_final",
        "generated_at": (generated_at or datetime.now()).isoformat(),
        "status": status,
        "source": {
            "model_artifact_path": str(model_artifact_path),
            "learning_manifest_paths": [str(path) for path in learning_manifest_paths or []],
        },
        "model_artifact": {
            "present": bool(model),
            "version": str(model.get("version", "")).strip(),
            "created_at": str(model.get("created_at", "")).strip()
            or str(metadata.get("artifact_created_at", "")).strip(),
            "feature_schema_id": str(model.get("feature_schema_id", "")).strip(),
            "label_policy_id": str(model.get("label_policy_id", "")).strip(),
            "dataset_manifest_id": str(model.get("dataset_manifest_id", "")).strip(),
            "feature_count": len(_list(model.get("feature_columns"))),
            "training_metrics": training_metrics,
            "metadata": metadata,
            "backend": {
                "lgbm": str(_mapping(model.get("lgbm_model")).get("backend", "")).strip(),
                "xgb": str(_mapping(model.get("xgb_model")).get("backend", "")).strip(),
                "degraded_model_mode": bool(metadata.get("degraded_model_mode", False)),
            },
        },
        "label_distribution": {
            "train": {"samples": train_samples},
            "calibration": {"samples": calibration_samples},
            "test": {
                "samples": test_samples,
                "positive": test_positive,
                "positive_rate": test_positive_rate,
            },
        },
        "probability_summary": {
            "meta_mean_prob": _float(training_metrics.get("meta_mean_prob")),
            "lgbm_mean_prob": _float(training_metrics.get("lgbm_mean_prob")),
            "xgb_mean_prob": _float(training_metrics.get("xgb_mean_prob")),
            "mean_prob_spread": _float(training_metrics.get("mean_prob_spread")),
        },
        "learning_manifest_summary": _manifest_summary(manifests),
        "recommended_next_actions": _model_diagnosis_actions(
            status=status,
            metadata=metadata,
            manifests=manifests,
        ),
    }


def build_cross_review_failure_analysis(
    *,
    signals: Sequence[Mapping[str, object]],
    config: StockAnalyzerConfig,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    """Replay cross-review thresholds against signal snapshots."""
    rows = [_normalize_signal(item) for item in signals]
    rows = [item for item in rows if item.get("symbol")]
    stats = Counter()
    reason_counts: Counter[str] = Counter()
    probability_values: dict[str, list[float]] = {key: [] for key in _PROBABILITY_KEYS}
    near_misses: list[dict[str, object]] = []
    thresholds = {
        "p_lgbm_min": float(config.models.cross_review.p_lgbm_min),
        "p_xgb_min": float(config.models.cross_review.p_xgb_min),
        "p_meta_min": float(config.models.cross_review.p_meta_min),
        "max_diff": float(config.models.cross_review.max_diff),
        "decision_threshold": float(config.walk_forward.decision_threshold),
    }

    for row in rows:
        probabilities = _mapping(row.get("probabilities"))
        parsed = {key: _float(probabilities.get(key)) for key in _PROBABILITY_KEYS}
        if all(value is not None for value in parsed.values()):
            stats["model_output_available"] += 1
            for key, value in parsed.items():
                if value is not None:
                    probability_values[key].append(value)
        else:
            stats["probability_missing"] += 1

        meta = parsed.get("meta")
        if meta is not None and meta >= thresholds["decision_threshold"]:
            stats["total_decision_threshold_pass"] += 1

        if any(value is None for value in parsed.values()):
            continue
        lgbm = float(parsed["lgbm"] or 0.0)
        xgb = float(parsed["xgb"] or 0.0)
        meta = float(parsed["meta"] or 0.0)
        review = evaluate_cross_review(
            lgbm_prob=lgbm,
            xgb_prob=xgb,
            meta_prob=meta,
            config=config.models.cross_review,
        )
        if review.passed:
            stats["total_cross_review_pass"] += 1
        else:
            stats["total_cross_review_fail"] += 1
        for reason in review.reasons:
            reason_counts[reason] += 1

        gaps = _cross_review_gaps(
            lgbm=lgbm,
            xgb=xgb,
            meta=meta,
            thresholds=thresholds,
        )
        if gaps["any_gap"]:
            near_misses.append(
                {
                    "symbol": row["symbol"],
                    "timestamp": row.get("timestamp", ""),
                    "action": row.get("action", ""),
                    "score": _float(row.get("score")),
                    "probabilities": {"lgbm": lgbm, "xgb": xgb, "meta": meta},
                    "model_diff": round(abs(lgbm - xgb), 6),
                    "gap_score": gaps["gap_score"],
                    "reasons": list(review.reasons),
                }
            )

    near_misses = sorted(
        near_misses,
        key=lambda item: (
            float(item.get("gap_score") or 0.0),
            -float(item.get("score") or 0.0),
        ),
    )[:20]
    total = len(rows)
    return {
        "report_type": "p4_cross_review_failure_analysis_v1",
        "generated_at": (generated_at or datetime.now()).isoformat(),
        "status": "ok" if total else "empty",
        "source_scope": _source_scope(rows=rows, audit_events=[]),
        "cross_review_analysis": {
            "thresholds": thresholds,
            "gate_statistics": {
                "total_evaluated_rows": total,
                "model_output_available": int(stats["model_output_available"]),
                "probability_missing": int(stats["probability_missing"]),
                "total_decision_threshold_pass": int(stats["total_decision_threshold_pass"]),
                "total_cross_review_pass": int(stats["total_cross_review_pass"]),
                "total_cross_review_fail": int(stats["total_cross_review_fail"]),
                "total_incremental_cross_review_rejection": int(
                    stats["total_decision_threshold_pass"] - stats["total_cross_review_pass"]
                )
                if stats["total_decision_threshold_pass"] >= stats["total_cross_review_pass"]
                else 0,
            },
            "reason_counts": dict(reason_counts),
            "probability_distribution": {
                key: _distribution(values, pass_threshold=_probability_threshold(key, thresholds))
                for key, values in probability_values.items()
            },
            "near_misses": near_misses,
        },
        "recommended_next_actions": [
            {
                "priority": "P0",
                "code": "repair_label_split_before_threshold_change",
                "detail": "Keep production cross-review unchanged until trainability improves.",
            },
            {
                "priority": "P0",
                "code": "shadow_probability_calibration",
                "detail": "Replay meta/xgb calibration buckets offline before gate tuning.",
            },
        ],
    }


def build_final_report_v3(
    *,
    signals: Sequence[Mapping[str, object]],
    audit_events: Sequence[Mapping[str, object]],
    config: StockAnalyzerConfig,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    """Build a read-only baseline and threshold sweep from captured runtime artifacts."""
    rows = [_normalize_signal(item) for item in signals]
    rows = [item for item in rows if item.get("symbol")]
    execution = _summarize_execution_events(audit_events)
    baseline = _baseline_from_rows(rows=rows, execution=execution, config=config)
    threshold_sweep = _threshold_grid(rows=rows, execution=execution)
    outcome_coverage = _outcome_coverage(execution)
    source_scope = _source_scope(rows=rows, audit_events=audit_events)
    status = "ok"
    if outcome_coverage["status"] != "sufficient":
        status = "outcome_coverage_insufficient"
    return {
        "report_type": "final_report_v3",
        "generated_at": (generated_at or datetime.now()).isoformat(),
        "status": status,
        "production_change_allowed": False,
        "source_scope": source_scope,
        "multisymbol_multiwindow": {
            "status": "runtime_artifact_replay",
            "note": (
                "Built from captured signal/audit artifacts; return fields are populated "
                "only when mature execution outcomes are present."
            ),
            "summaries": {
                "baseline": baseline,
            },
        },
        "threshold_sweep": threshold_sweep,
        "outcome_coverage": outcome_coverage,
        "recommended_next_actions": _final_report_actions(
            threshold_sweep=threshold_sweep,
            outcome_coverage=outcome_coverage,
            source_scope=source_scope,
        ),
    }


def build_feature_family_ablation(
    *,
    signals: Sequence[Mapping[str, object]],
    model_artifact_path: Path,
    config: StockAnalyzerConfig,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    """Estimate feature-family pressure from reasons, traces and model feature schema."""
    rows = [_normalize_signal(item) for item in signals]
    rows = [item for item in rows if item.get("symbol")]
    model = _load_json(model_artifact_path)
    feature_columns = [str(item) for item in _list(model.get("feature_columns"))]
    feature_schema = _feature_schema_summary(feature_columns)
    family_stats = _feature_family_stats(rows, config=config)
    baseline = _feature_baseline(rows)
    ablation_results = []
    for family, stats in sorted(family_stats.items()):
        impact = _family_shadow_impact(family=family, stats=stats, baseline=baseline)
        ablation_results.append(
            {
                "experiment_name": f"shadow_reduce_{family}",
                "family": family,
                "status": "shadow_only",
                "sample_scope": "captured_signal_rows",
                "metrics": {
                    "avg_auc": None,
                    "final_equity": None,
                    "max_drawdown": None,
                    "total_trades": stats["buy_rows"],
                    "win_rate": None,
                    "affected_rows": stats["rows"],
                    "affected_symbols": stats["symbols"],
                    "avg_score": stats["avg_score"],
                    "avg_meta": stats["avg_meta"],
                },
                "impact": impact,
                "evidence": {
                    "reason_counts": stats["reason_counts"],
                    "gate_counts": stats["gate_counts"],
                    "data_quality_counts": stats["data_quality_counts"],
                    "example_symbols": stats["example_symbols"],
                },
                "recommendation": _family_recommendation(family=family, stats=stats),
            }
        )
    financial_quality = _financial_data_quality(rows, config=config)
    return {
        "report_type": "p4_feature_family_ablation_v1",
        "generated_at": (generated_at or datetime.now()).isoformat(),
        "status": "shadow_only_no_production_change",
        "production_change_allowed": False,
        "method": (
            "Artifact replay estimates family-level drag from captured reasons, "
            "decision traces and model feature schema. It does not retrain or alter "
            "production feature weights."
        ),
        "baseline_metrics": baseline,
        "feature_schema": feature_schema,
        "financial_data_quality": financial_quality,
        "feature_families": {
            name: len(values) for name, values in _family_feature_columns(feature_columns).items()
        },
        "ablation_results": ablation_results,
        "summary": {
            "total_families": len(ablation_results),
            "shadow_reduce_candidates": sum(
                1 for item in ablation_results if item["recommendation"] == "shadow_reduce"
            ),
            "keep_candidates": sum(
                1 for item in ablation_results if item["recommendation"] == "keep"
            ),
            "data_repair_candidates": sum(
                1 for item in ablation_results if item["recommendation"] == "repair_data_first"
            ),
        },
    }


def build_position_framework_analysis(
    *,
    audit_events: Sequence[Mapping[str, object]],
    signals: Sequence[Mapping[str, object]],
    config: StockAnalyzerConfig,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    """Analyze position, stop, take-profit and re-entry evidence from artifacts."""
    rows = [_normalize_signal(item) for item in signals]
    execution = _summarize_execution_events(audit_events)
    symbol_paths = _position_symbol_paths(rows=rows, execution=execution)
    return {
        "report_type": "position_framework_analysis",
        "generated_at": (generated_at or datetime.now()).isoformat(),
        "status": "runtime_artifact_replay",
        "production_change_allowed": False,
        "position_controls": _position_controls(config),
        "position_sizing_analysis": _position_sizing_analysis(config),
        "execution_path_summary": execution["summary"],
        "loss_path_analysis": _loss_path_analysis(symbol_paths),
        "symbol_paths": symbol_paths[:200],
        "recommended_shadow": [
            "position_sizing_sensitivity",
            "stop_loss_cooldown_reentry_shadow",
            "take_profit_trailing_ab_test",
            "cash_fragmentation_sensitivity",
        ],
        "guardrails": {
            "do_not_change_production_risk_gates": True,
            "do_not_enable_auto_promotion": True,
            "do_not_execute_real_orders": True,
        },
    }


def collect_signal_rows(paths: Sequence[Path]) -> list[dict[str, object]]:
    """Collect signal-like rows from latest_signals, runtime_state, week5 reports and jsonl."""
    rows: list[dict[str, object]] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for path in paths:
        for payload in _iter_json_payloads(path):
            for row in _extract_signal_rows(payload, source_path=path):
                key = _signal_row_identity(row)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
    return rows


def collect_audit_events(paths: Sequence[Path]) -> list[dict[str, object]]:
    """Collect pipeline/audit events from runtime_state JSON and jsonl sidecars."""
    rows: list[dict[str, object]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for path in paths:
        for payload in _iter_json_payloads(path):
            for event in _extract_audit_events(payload, source_path=path):
                key = (
                    str(event.get("event_id", "")).strip(),
                    str(event.get("timestamp", "")).strip(),
                    str(event.get("trace_id", "")).strip(),
                    str(event.get("event_type", "")).strip(),
                )
                if key in seen:
                    continue
                seen.add(key)
                rows.append(event)
    return rows


def write_p0_analysis_inputs(
    *,
    analysis_dir: Path,
    model_artifact_path: Path,
    learning_manifest_paths: Sequence[Path],
    signal_source_paths: Sequence[Path],
    config: StockAnalyzerConfig,
    audit_event_paths: Sequence[Path] | None = None,
    include_research_completeness_artifacts: bool = True,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    """Write model diagnosis and cross-review inputs for the P0 shadow planner."""
    generated_at = generated_at or datetime.now()
    analysis_dir.mkdir(parents=True, exist_ok=True)
    signals = collect_signal_rows(signal_source_paths)
    audit_events = collect_audit_events(audit_event_paths or [])
    model_diagnosis = build_model_diagnosis_final(
        model_artifact_path=model_artifact_path,
        learning_manifest_paths=learning_manifest_paths,
        generated_at=generated_at,
    )
    cross_review = build_cross_review_failure_analysis(
        signals=signals,
        config=config,
        generated_at=generated_at,
    )
    outputs = {
        "model_diagnosis_final": analysis_dir / "model_diagnosis_final.json",
        "cross_review_failure": analysis_dir / "p4_cross_review_failure_analysis_v1.json",
    }
    if include_research_completeness_artifacts:
        final_report = build_final_report_v3(
            signals=signals,
            audit_events=audit_events,
            config=config,
            generated_at=generated_at,
        )
        feature_ablation = build_feature_family_ablation(
            signals=signals,
            model_artifact_path=model_artifact_path,
            config=config,
            generated_at=generated_at,
        )
        position_framework = build_position_framework_analysis(
            audit_events=audit_events,
            signals=signals,
            config=config,
            generated_at=generated_at,
        )
        outputs.update(
            {
                "final_report_v3": analysis_dir / "final_report_v3.json",
                "feature_family_ablation": analysis_dir / "p4_feature_family_ablation_v1.json",
                "position_framework": analysis_dir
                / "p5_position"
                / "position_framework_analysis.json",
            }
        )
    _write_json(outputs["model_diagnosis_final"], model_diagnosis)
    _write_json(outputs["cross_review_failure"], cross_review)
    if include_research_completeness_artifacts:
        _write_json(outputs["final_report_v3"], final_report)
        _write_json(outputs["feature_family_ablation"], feature_ablation)
        _write_json(outputs["position_framework"], position_framework)
    manifest = {
        "report_type": "p0_analysis_inputs_manifest",
        "generated_at": generated_at.isoformat(),
        "production_change_allowed": False,
        "inputs": {
            "model_artifact_path": str(model_artifact_path),
            "learning_manifest_paths": [str(path) for path in learning_manifest_paths],
            "signal_source_paths": [str(path) for path in signal_source_paths],
            "audit_event_paths": [str(path) for path in audit_event_paths or []],
            "signal_rows": len(signals),
            "audit_events": len(audit_events),
            "source_scope": _source_scope(rows=signals, audit_events=audit_events),
        },
        "outputs": {name: str(path) for name, path in outputs.items()},
        "remaining_expected_inputs": []
        if include_research_completeness_artifacts
        else [
            "final_report_v3.json",
            "p4_feature_family_ablation_v1.json",
            "p5_position/position_framework_analysis.json",
        ],
    }
    manifest_path = analysis_dir / "p0_analysis_inputs_manifest.json"
    _write_json(manifest_path, manifest)
    manifest["outputs"]["manifest"] = str(manifest_path)
    return manifest


def _extract_signal_rows(
    payload: Mapping[str, object],
    *,
    source_path: Path,
) -> list[dict[str, object]]:
    source_path_text = str(source_path)
    timestamp = str(payload.get("timestamp", "")).strip()
    rows: list[dict[str, object]] = []

    for item in _list(payload.get("signals")):
        row = _normalize_signal(_mapping(item))
        row.setdefault("timestamp", timestamp)
        row["source_path"] = source_path_text
        row["source_container"] = "signals"
        rows.append(row)

    latest_signals = _mapping(payload.get("latest_signals"))
    for item in _list(latest_signals.get("signals")):
        row = _normalize_signal(_mapping(item))
        row.setdefault("timestamp", str(latest_signals.get("timestamp", "")).strip() or timestamp)
        row["source_path"] = source_path_text
        row["source_container"] = "latest_signals"
        row["signal_source"] = str(latest_signals.get("source", "")).strip() or "latest_signals"
        row["storage_source"] = str(latest_signals.get("storage_source", "")).strip()
        rows.append(row)

    for report_key in ("week5_scan_latest", "week5_report"):
        week5 = _mapping(payload.get(report_key))
        if week5:
            rows.extend(_extract_week5_rows(week5, source_path=source_path))

    rows.extend(_extract_week5_rows(payload, source_path=source_path))
    return [item for item in rows if item.get("symbol")]


def _extract_week5_rows(
    payload: Mapping[str, object],
    *,
    source_path: Path,
) -> list[dict[str, object]]:
    timestamp = str(payload.get("timestamp", "")).strip()
    source_path_text = str(source_path)
    raw_rows: list[Mapping[str, object]] = []
    first_board = _mapping(payload.get("first_board"))
    raw_rows.extend(_mapping(item) for item in _list(first_board.get("leaders")))
    raw_rows.extend(_mapping(item) for item in _list(first_board.get("candidates")))
    signal_pool = _mapping(payload.get("signal_pool"))
    raw_rows.extend(_mapping(item) for item in _list(signal_pool.get("candidates")))

    rows: list[dict[str, object]] = []
    for item in raw_rows:
        row = _normalize_signal(item)
        row.setdefault("timestamp", timestamp)
        row["source_path"] = source_path_text
        row["source_container"] = "week5_candidates"
        row.setdefault("signal_source", "week5_latest_candidates")
        rows.append(row)
    return rows


def _extract_audit_events(
    payload: Mapping[str, object],
    *,
    source_path: Path,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    source_path_text = str(source_path)
    event_type = str(payload.get("event_type", "")).strip()
    if event_type:
        event = dict(payload)
        event["source_path"] = source_path_text
        rows.append(event)
    for item in _list(payload.get("audit_events")):
        event = _mapping(item)
        if event:
            event["source_path"] = source_path_text
            rows.append(event)
    return rows


def _normalize_signal(signal: Mapping[str, object]) -> dict[str, object]:
    row = dict(signal)
    row["symbol"] = str(row.get("symbol", "")).strip()
    row["action"] = str(row.get("action", "")).strip().lower()
    row["probabilities"] = _mapping(row.get("probabilities"))
    if not row["probabilities"]:
        row["probabilities"] = _mapping(_mapping(row.get("decision_trace")).get("probabilities"))
    if "score" not in row and "shortlist_score" in row:
        row["score"] = row.get("shortlist_score")
    return row


def _iter_json_payloads(path: Path) -> Iterable[Mapping[str, object]]:
    if not path.exists() or not path.is_file():
        return
    if path.suffix.lower() == ".jsonl":
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, Mapping):
                        yield payload
        except OSError:
            return
        return
    payload = _load_json(path)
    if payload:
        yield payload


def _manifest_summary(manifests: Sequence[Mapping[str, object]]) -> dict[str, object]:
    items: list[dict[str, object]] = []
    for manifest in manifests:
        metrics = _mapping(manifest.get("training_metrics"))
        metadata = _mapping(manifest.get("metadata"))
        items.append(
            {
                "created_at": str(manifest.get("created_at", "")).strip()
                or str(metadata.get("artifact_created_at", "")).strip(),
                "dataset_manifest_id": str(manifest.get("dataset_manifest_id", "")).strip(),
                "label_policy_id": str(manifest.get("label_policy_id", "")).strip(),
                "train_samples": _int(metadata.get("train_samples")),
                "calibration_samples": _int(
                    metadata.get("calibration_samples"),
                    fallback=metrics.get("calibration_samples"),
                ),
                "test_samples": _int(
                    metadata.get("test_samples"),
                    fallback=metrics.get("test_samples"),
                ),
                "positive_rate": _float(metrics.get("positive_rate")),
                "auc": _float(metrics.get("auc")),
                "meta_mean_prob": _float(metrics.get("meta_mean_prob")),
                "lgbm_mean_prob": _float(metrics.get("lgbm_mean_prob")),
                "xgb_mean_prob": _float(metrics.get("xgb_mean_prob")),
                "degraded_model_mode": bool(metadata.get("degraded_model_mode", False)),
            }
        )
    return {
        "count": len(items),
        "items": sorted(items, key=lambda item: str(item.get("created_at", ""))),
    }


def _model_diagnosis_actions(
    *,
    status: str,
    metadata: Mapping[str, object],
    manifests: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    if status != "usable":
        actions.append(
            {
                "priority": "P0",
                "code": "label_split_trainability_shadow",
                "detail": "Require enough positive labels in test folds before threshold tuning.",
            }
        )
    if bool(metadata.get("degraded_model_mode", False)):
        actions.append(
            {
                "priority": "P0",
                "code": "model_backend_dependency_check",
                "detail": (
                    "Production artifact used fallback model backends; verify "
                    "LightGBM/XGBoost availability before retraining."
                ),
            }
        )
    if manifests:
        manifest_rates = [
            _float(_mapping(item.get("training_metrics")).get("positive_rate"))
            for item in manifests
        ]
        if all((rate or 0.0) <= 0.0 for rate in manifest_rates):
            actions.append(
                {
                    "priority": "P0",
                    "code": "manifest_positive_label_audit",
                    "detail": "Learning manifests also show no positive test labels.",
                }
            )
    return actions


def _cross_review_gaps(
    *,
    lgbm: float,
    xgb: float,
    meta: float,
    thresholds: Mapping[str, float],
) -> dict[str, object]:
    lgbm_gap = max(0.0, float(thresholds["p_lgbm_min"]) - lgbm)
    xgb_gap = max(0.0, float(thresholds["p_xgb_min"]) - xgb)
    meta_gap = max(0.0, float(thresholds["p_meta_min"]) - meta)
    diff_gap = max(0.0, abs(lgbm - xgb) - float(thresholds["max_diff"]))
    gap_score = round(lgbm_gap + xgb_gap + meta_gap + diff_gap, 6)
    return {
        "lgbm_below_min": round(lgbm_gap, 6),
        "xgb_below_min": round(xgb_gap, 6),
        "meta_below_min": round(meta_gap, 6),
        "model_diff_excess": round(diff_gap, 6),
        "gap_score": gap_score,
        "any_gap": gap_score > 0,
    }


def _distribution(values: Sequence[float], *, pass_threshold: float) -> dict[str, object]:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {"n": 0, "pass": 0}
    return {
        "n": len(ordered),
        "min": round(ordered[0], 6),
        "p10": round(_quantile(ordered, 0.10), 6),
        "p25": round(_quantile(ordered, 0.25), 6),
        "median": round(_quantile(ordered, 0.50), 6),
        "mean": round(mean(ordered), 6),
        "p75": round(_quantile(ordered, 0.75), 6),
        "p90": round(_quantile(ordered, 0.90), 6),
        "max": round(ordered[-1], 6),
        "pass": sum(1 for value in ordered if value >= pass_threshold),
        "pass_threshold": pass_threshold,
    }


def _probability_threshold(key: str, thresholds: Mapping[str, float]) -> float:
    if key == "lgbm":
        return float(thresholds["p_lgbm_min"])
    if key == "xgb":
        return float(thresholds["p_xgb_min"])
    return float(thresholds["p_meta_min"])


def _signal_row_identity(row: Mapping[str, object]) -> tuple[str, str, str, str, str, str]:
    probabilities = _mapping(row.get("probabilities"))
    probability_sig = ",".join(
        f"{key}={_float(probabilities.get(key))}" for key in _PROBABILITY_KEYS
    )
    return (
        str(row.get("symbol", "")).strip(),
        str(row.get("timestamp", "")).strip(),
        str(row.get("source_path", "")).strip(),
        str(row.get("source_container", "")).strip(),
        str(row.get("action", "")).strip(),
        probability_sig,
    )


def _source_scope(
    *,
    rows: Sequence[Mapping[str, object]],
    audit_events: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    row_sources = Counter(str(row.get("source_container", "")).strip() or "unknown" for row in rows)
    signal_sources = Counter(str(row.get("signal_source", "")).strip() or "unknown" for row in rows)
    execution_modes = Counter()
    dry_run_events = 0
    advisory_events = 0
    live_like_events = 0
    for event in audit_events:
        payload = _mapping(event.get("payload"))
        mode = str(payload.get("execution_mode", "")).strip() or "unknown"
        execution_modes[mode] += 1
        portfolio_update = _mapping(payload.get("portfolio_update"))
        is_dry_run = bool(payload.get("dry_run_execution", False)) or bool(
            portfolio_update.get("dry_run", False)
        )
        if mode == "advisory_only":
            advisory_events += 1
        elif is_dry_run or "dry_run" in mode:
            dry_run_events += 1
        elif mode != "unknown":
            live_like_events += 1
    fallback_rows = sum(
        1
        for row in rows
        if str(row.get("source_container", "")).strip() == "week5_candidates"
        or str(row.get("signal_source", "")).strip() == "week5_latest_candidates"
    )
    mixed_or_shadow = bool(fallback_rows or dry_run_events or advisory_events)
    return {
        "row_count": len(rows),
        "row_source_breakdown": dict(row_sources),
        "signal_source_breakdown": dict(signal_sources),
        "audit_event_count": len(audit_events),
        "execution_mode_breakdown": dict(execution_modes),
        "fallback_signal_rows": int(fallback_rows),
        "advisory_events": advisory_events,
        "dry_run_events": dry_run_events,
        "live_like_events": live_like_events,
        "is_production_pure": not mixed_or_shadow and live_like_events > 0,
        "requires_runtime_source_review": mixed_or_shadow,
    }


def _summarize_execution_events(events: Sequence[Mapping[str, object]]) -> dict[str, object]:
    attempts: Counter[str] = Counter()
    dry_run_attempts: Counter[str] = Counter()
    advisory_attempts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    realized_returns: list[float] = []
    records_by_symbol: dict[str, list[dict[str, object]]] = defaultdict(list)
    mode_counts: Counter[str] = Counter()

    for event in events:
        payload = _mapping(event.get("payload"))
        event_type = str(event.get("event_type", "")).strip().lower()
        if event_type in {"pre_trade_blocked", "risk_gate_blocked"}:
            record = {
                "symbol": str(_mapping(event.get("payload")).get("symbol", "")).strip(),
                "status": event_type,
                "block_category": event_type,
                "reason": str(_mapping(event.get("payload")).get("reason", "")).strip()
                or "unknown",
                "timestamp": str(event.get("timestamp", "")).strip(),
                "trace_id": str(event.get("trace_id", "")).strip(),
            }
            if record["symbol"]:
                records_by_symbol[str(record["symbol"])].append(record)
            attempts[event_type] += 1
            status_counts[event_type] += 1
            reason_counts[str(record["reason"])] += 1
            continue
        if event_type != "pipeline_run":
            continue
        mode = str(payload.get("execution_mode", "")).strip().lower() or "unknown"
        mode_counts[mode] += 1
        portfolio_update = _mapping(payload.get("portfolio_update"))
        is_dry_run = (
            bool(payload.get("dry_run_execution", False))
            or bool(portfolio_update.get("dry_run", False))
            or "dry_run" in mode
        )
        raw_attempts = _mapping(portfolio_update.get("execution_attempts"))
        target = (
            advisory_attempts
            if mode == "advisory_only"
            else dry_run_attempts
            if is_dry_run
            else attempts
        )
        for key, value in raw_attempts.items():
            target[str(key)] += _int(value)
        raw_executions = portfolio_update.get("executions")
        if not isinstance(raw_executions, list):
            continue
        for item in raw_executions:
            if not isinstance(item, Mapping):
                continue
            record = _execution_record(item, mode=mode, dry_run=is_dry_run)
            status = str(record.get("status", "")).strip() or "unknown"
            reason = str(record.get("reason", "")).strip() or "unknown"
            status_counts[status] += 1
            reason_counts[reason] += 1
            realized = _float(record.get("realized_return_pct"))
            if realized is not None and mode != "advisory_only" and not is_dry_run:
                realized_returns.append(realized)
            symbol = str(record.get("symbol", "")).strip()
            if symbol:
                records_by_symbol[symbol].append(record)

    return {
        "attempts": dict(attempts),
        "dry_run_attempts": dict(dry_run_attempts),
        "advisory_attempts": dict(advisory_attempts),
        "status_counts": dict(status_counts),
        "reason_counts": dict(reason_counts.most_common(20)),
        "realized_returns": realized_returns,
        "records_by_symbol": dict(records_by_symbol),
        "summary": {
            "execution_modes": dict(mode_counts),
            "live_attempts": sum(attempts.values()),
            "dry_run_attempts": sum(dry_run_attempts.values()),
            "advisory_attempts": sum(advisory_attempts.values()),
            "records": sum(len(items) for items in records_by_symbol.values()),
            "observed_realized_returns": len(realized_returns),
        },
    }


def _execution_record(
    item: Mapping[str, object],
    *,
    mode: str,
    dry_run: bool,
) -> dict[str, object]:
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
        "realized_return_pct",
        "remaining_quantity",
        "remaining_ratio",
    )
    record = {field: item[field] for field in fields if field in item}
    record["execution_mode"] = mode
    record["dry_run"] = dry_run
    return record


def _baseline_from_rows(
    *,
    rows: Sequence[Mapping[str, object]],
    execution: Mapping[str, object],
    config: StockAnalyzerConfig,
) -> dict[str, object]:
    scores = [_float(row.get("score")) for row in rows]
    scores = [value for value in scores if value is not None]
    buy_rows = [row for row in rows if str(row.get("action", "")).lower() == "buy"]
    returns = [float(item) for item in _list(execution.get("realized_returns"))]
    profitable = sum(1 for item in returns if item > 0.0)
    return {
        "status": "return_observed" if returns else "outcome_missing",
        "symbols_tested": len({str(row.get("symbol", "")).strip() for row in rows}),
        "windows_tested": len(
            {str(row.get("timestamp", "")).strip() for row in rows if row.get("timestamp")}
        ),
        "total_experiments": len(rows),
        "avg_final_equity": _equity_from_returns(returns),
        "median_final_equity": _equity_from_returns([_median(returns)]) if returns else None,
        "avg_max_drawdown": _max_drawdown_from_returns(returns),
        "total_trades": len(returns),
        "overall_win_rate": round(profitable / len(returns), 6) if returns else None,
        "losing_symbols": _losing_symbol_count(_mapping(execution.get("records_by_symbol"))),
        "losing_ratio": None,
        "insufficient_sample": len(returns) < 100,
        "candidate_rows": len(rows),
        "buy_rows": len(buy_rows),
        "avg_score": round(mean(scores), 6) if scores else None,
        "score_buy_threshold": _score_threshold(config),
    }


def _threshold_grid(
    rows: Sequence[Mapping[str, object]],
    *,
    execution: Mapping[str, object],
) -> dict[str, object]:
    total = len(rows)
    returns_by_symbol = _returns_by_symbol(_mapping(execution.get("records_by_symbol")))
    results: list[dict[str, object]] = []
    for xgb_min in _MODEL_THRESHOLD_GRID["xgb_min"]:
        for meta_min in _MODEL_THRESHOLD_GRID["meta_min"]:
            for max_diff in _MODEL_THRESHOLD_GRID["max_diff"]:
                for score_min in _MODEL_THRESHOLD_GRID["score_min"]:
                    passed = []
                    for row in rows:
                        probabilities = _mapping(row.get("probabilities"))
                        lgbm = _float(probabilities.get("lgbm"))
                        xgb = _float(probabilities.get("xgb"))
                        meta = _float(probabilities.get("meta"))
                        score = _float(row.get("score"))
                        if any(value is None for value in (lgbm, xgb, meta, score)):
                            continue
                        if (
                            float(xgb) >= xgb_min
                            and float(meta) >= meta_min
                            and abs(float(lgbm) - float(xgb)) <= max_diff
                            and float(score) >= score_min
                        ):
                            passed.append(row)
                    returns = _returns_for_passed_rows(passed, returns_by_symbol)
                    trade_count = len(returns)
                    profitable = sum(1 for item in returns if item > 0.0)
                    results.append(
                        {
                            "xgb_min": xgb_min,
                            "meta_min": meta_min,
                            "max_diff": max_diff,
                            "score_min": score_min,
                            "pass_count": len(passed),
                            "pass_rate": round(len(passed) / total, 6) if total else 0.0,
                            "buy_count": sum(
                                1
                                for item in passed
                                if str(item.get("action", "")).strip().lower() == "buy"
                            ),
                            "observed_trade_count": trade_count,
                            "win_rate": round(profitable / trade_count, 6)
                            if trade_count
                            else None,
                            "avg_realized_return_pct": round(mean(returns), 6)
                            if returns
                            else None,
                            "final_equity": _equity_from_returns(returns),
                            "max_drawdown": _max_drawdown_from_returns(returns),
                            "profitability_status": "observed"
                            if trade_count >= 30
                            else "insufficient_outcomes",
                            "example_symbols": sorted(
                                {str(item.get("symbol", "")).strip() for item in passed}
                            )[:12],
                        }
                    )
    best = sorted(
        results,
        key=lambda item: (
            item.get("final_equity") is None,
            -float(item.get("final_equity") or 0.0),
            -int(item["pass_count"]),
            item["xgb_min"],
        ),
    )[:10]
    effective = any(int(item["pass_count"]) > 0 for item in results)
    return {
        "status": "candidate_generating" if effective else "not_effective",
        "grid": {
            "xgb_min": list(_MODEL_THRESHOLD_GRID["xgb_min"]),
            "meta_min": list(_MODEL_THRESHOLD_GRID["meta_min"]),
            "max_diff": list(_MODEL_THRESHOLD_GRID["max_diff"]),
            "score_min": list(_MODEL_THRESHOLD_GRID["score_min"]),
        },
        "total_rows": total,
        "outcome_linkage": {
            "symbols_with_returns": len(returns_by_symbol),
            "minimum_observed_trades_for_profitability_rank": 30,
            "can_rank_by_profitability": any(
                int(item["observed_trade_count"]) >= 30 for item in results
            ),
        },
        "top_candidate_generating_variants": best,
        "results": results,
        "note": (
            "Profitability fields are populated only when captured execution records "
            "can be linked back to candidate symbols."
        ),
    }


def _returns_by_symbol(records_by_symbol: Mapping[str, object]) -> dict[str, list[float]]:
    results: dict[str, list[float]] = {}
    for symbol, raw_records in records_by_symbol.items():
        symbol_text = str(symbol).strip()
        if not symbol_text:
            continue
        returns = [
            float(value)
            for value in (
                _float(item.get("realized_return_pct"))
                for item in _list(raw_records)
                if isinstance(item, Mapping)
            )
            if value is not None
        ]
        if returns:
            results[symbol_text] = returns
    return results


def _returns_for_passed_rows(
    rows: Sequence[Mapping[str, object]],
    returns_by_symbol: Mapping[str, Sequence[float]],
) -> list[float]:
    returns: list[float] = []
    seen_symbols: set[str] = set()
    for row in rows:
        symbol = str(row.get("symbol", "")).strip()
        if not symbol or symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        returns.extend(float(item) for item in returns_by_symbol.get(symbol, []))
    return returns


def _outcome_coverage(execution: Mapping[str, object]) -> dict[str, object]:
    returns = _list(execution.get("realized_returns"))
    count = len(returns)
    return {
        "status": "sufficient" if count >= 100 else "insufficient",
        "observed_returns": count,
        "minimum_for_profitability_claim": 100,
        "can_claim_profitability": count >= 100,
    }


def _final_report_actions(
    *,
    threshold_sweep: Mapping[str, object],
    outcome_coverage: Mapping[str, object],
    source_scope: Mapping[str, object],
) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    if bool(source_scope.get("requires_runtime_source_review", False)):
        actions.append(
            {
                "priority": "P0",
                "code": "runtime_source_purity_review",
                "detail": "Separate production, advisory, dry-run and week5 fallback samples.",
            }
        )
    if outcome_coverage.get("status") != "sufficient":
        actions.append(
            {
                "priority": "P0",
                "code": "collect_mature_outcomes_before_profit_claim",
                "detail": "Threshold variants can be ranked by count now, not by return.",
            }
        )
    if threshold_sweep.get("status") == "candidate_generating":
        actions.append(
            {
                "priority": "P0",
                "code": "cross_review_threshold_shadow_replay",
                "detail": "Replay candidate-generating variants against mature outcomes.",
            }
        )
    return actions


def _feature_schema_summary(feature_columns: Sequence[str]) -> dict[str, object]:
    families = _family_feature_columns(feature_columns)
    return {
        "total_features": len(feature_columns),
        "families": {
            name: {"count": len(values), "examples": values[:8]}
            for name, values in families.items()
        },
    }


def _family_feature_columns(feature_columns: Sequence[str]) -> dict[str, list[str]]:
    families: dict[str, list[str]] = {name: [] for name in _FEATURE_FAMILIES}
    families["other"] = []
    for column in feature_columns:
        normalized = column.lower()
        matched = False
        for family, tokens in _FEATURE_FAMILIES.items():
            if any(token in normalized for token in tokens):
                families[family].append(column)
                matched = True
                break
        if not matched:
            families["other"].append(column)
    return families


def _feature_family_stats(
    rows: Sequence[Mapping[str, object]],
    *,
    config: StockAnalyzerConfig,
) -> dict[str, dict[str, object]]:
    stats: dict[str, dict[str, object]] = {}
    for family in _FEATURE_FAMILIES:
        stats[family] = {
            "rows": 0,
            "symbols_set": set(),
            "buy_rows": 0,
            "scores": [],
            "metas": [],
            "reason_counter": Counter(),
            "gate_counter": Counter(),
            "data_quality_counter": Counter(),
            "examples": [],
        }
    for row in rows:
        text_parts = _row_reason_text(row)
        family_matches = [
            family
            for family, tokens in _FEATURE_FAMILIES.items()
            if any(token in text_parts for token in tokens)
        ]
        trace = _mapping(row.get("decision_trace"))
        for family in family_matches:
            item = stats[family]
            item["rows"] = int(item["rows"]) + 1
            cast_set = item["symbols_set"]
            if isinstance(cast_set, set):
                cast_set.add(str(row.get("symbol", "")).strip())
            if str(row.get("action", "")).lower() == "buy":
                item["buy_rows"] = int(item["buy_rows"]) + 1
            score = _float(row.get("score"))
            if score is not None:
                _list_mut(item, "scores").append(score)
            meta = _float(_mapping(row.get("probabilities")).get("meta"))
            if meta is not None:
                _list_mut(item, "metas").append(meta)
            for reason in _list(row.get("reasons")):
                _counter_mut(item, "reason_counter")[str(reason)] += 1
            for gate_name in ("financial_gate", "liquidity_gate", "risk_gate", "cross_review_gate"):
                gate = _mapping(trace.get(gate_name))
                if gate.get("passed") is False or gate.get("allowed") is False:
                    _counter_mut(item, "gate_counter")[gate_name] += 1
            if family == "financial_background":
                for code in _financial_data_quality_codes(row, config=config):
                    _counter_mut(item, "data_quality_counter")[code] += 1
            examples = _list_mut(item, "examples")
            if len(examples) < 8:
                examples.append(str(row.get("symbol", "")).strip())
    normalized: dict[str, dict[str, object]] = {}
    for family, item in stats.items():
        scores = [float(value) for value in _list(item.get("scores"))]
        metas = [float(value) for value in _list(item.get("metas"))]
        symbols = sorted(str(value) for value in item.get("symbols_set", set()))
        normalized[family] = {
            "rows": int(item["rows"]),
            "symbols": len(symbols),
            "buy_rows": int(item["buy_rows"]),
            "avg_score": round(mean(scores), 6) if scores else None,
            "avg_meta": round(mean(metas), 6) if metas else None,
            "reason_counts": dict(_counter_mut(item, "reason_counter").most_common(12)),
            "gate_counts": dict(_counter_mut(item, "gate_counter").most_common(12)),
            "data_quality_counts": dict(_counter_mut(item, "data_quality_counter").most_common(12)),
            "example_symbols": symbols[:8],
        }
    return normalized


def _feature_baseline(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    scores = [_float(row.get("score")) for row in rows]
    scores = [value for value in scores if value is not None]
    return {
        "row_count": len(rows),
        "unique_symbols": len({str(row.get("symbol", "")).strip() for row in rows}),
        "buy_rows": sum(1 for row in rows if str(row.get("action", "")).lower() == "buy"),
        "avg_score": round(mean(scores), 6) if scores else None,
        "return_metrics_available": False,
    }


def _family_shadow_impact(
    *,
    family: str,
    stats: Mapping[str, object],
    baseline: Mapping[str, object],
) -> dict[str, object]:
    baseline_rows = max(1, _int(baseline.get("row_count")))
    affected = _int(stats.get("rows"))
    affected_pct = round(100.0 * affected / baseline_rows, 4)
    return {
        "affected_rows_pct": affected_pct,
        "final_equity": {"change_pct": None},
        "avg_auc": {"change_pct": None},
        "interpretation": (
            "financial data repair should precede feature downweighting"
            if family == "financial_background"
            else "shadow-only affected-row pressure; retraining evidence unavailable"
        ),
    }


def _family_recommendation(*, family: str, stats: Mapping[str, object]) -> str:
    rows = _int(stats.get("rows"))
    data_quality_counts = _mapping(stats.get("data_quality_counts"))
    if family == "financial_background" and _int(data_quality_counts.get("missing_financials")):
        return "repair_data_first"
    if rows <= 0:
        return "keep"
    if family in {"model_probability", "financial_background"}:
        return "keep"
    return "shadow_reduce"


def _financial_data_quality(
    rows: Sequence[Mapping[str, object]],
    *,
    config: StockAnalyzerConfig,
) -> dict[str, object]:
    counter: Counter[str] = Counter()
    examples: dict[str, list[str]] = {}
    score_by_code: dict[str, list[float]] = defaultdict(list)
    buy_rows_by_code: Counter[str] = Counter()
    for row in rows:
        for code in _financial_data_quality_codes(row, config=config):
            counter[code] += 1
            examples.setdefault(code, [])
            if len(examples[code]) < 8:
                examples[code].append(str(row.get("symbol", "")).strip())
            score = _float(row.get("score"))
            if score is not None:
                score_by_code[code].append(score)
            if str(row.get("action", "")).strip().lower() == "buy":
                buy_rows_by_code[code] += 1
    unique_symbols = {
        str(row.get("symbol", "")).strip()
        for row in rows
        if _financial_data_quality_codes(row, config=config)
    }
    return {
        "status": "needs_data_quality_review" if counter else "no_financial_penalty_observed",
        "affected_rows": sum(counter.values()),
        "affected_symbols": len(unique_symbols),
        "reason_counts": dict(counter.most_common(20)),
        "examples": examples,
        "classification": _financial_classification(
            counter=counter,
            score_by_code=score_by_code,
            buy_rows_by_code=buy_rows_by_code,
            rows=rows,
            config=config,
        ),
        "current_policy": {
            "enabled": bool(config.financial_filter.enabled),
            "missing_data_policy": str(config.financial_filter.missing_data_policy),
            "trend_mode": str(config.financial_filter.trend_mode),
            "monster_mode": str(config.financial_filter.monster_mode),
            "min_roe": float(config.financial_filter.min_roe),
            "max_debt_ratio": float(config.financial_filter.max_debt_ratio),
        },
        "questions_answered": {
            "true_low_roe_vs_missing_data": (
                "requires source financial fields; rows currently classify observed "
                "penalties and missing flags"
            ),
            "should_relax_filter": "not from this artifact alone; run shadow after data repair",
        },
    }


def _financial_data_quality_codes(
    row: Mapping[str, object],
    *,
    config: StockAnalyzerConfig,
) -> list[str]:
    _ = config
    text = _row_reason_text(row)
    codes: list[str] = []
    if "financial_data_complete_false" in text or "missing_financial" in text:
        codes.append("missing_financials")
    if "missing_financial_data" in text:
        codes.append("missing_financial_data_gate")
    if "stale_financial" in text or "financial_stale" in text:
        codes.append("stale_financials")
    if "default_financial" in text or "default_penalty" in text:
        codes.append("default_financial_penalty")
    if "data_age" in text or "age_days" in text:
        codes.append("financial_age_observed")
    if "low_roe" in text:
        codes.append("low_roe_penalty")
    if "high_debt" in text:
        codes.append("high_debt_penalty")
    if "financial_penalty:st" in text or " st" in f" {text}":
        codes.append("st_penalty")
    trace = _mapping(row.get("decision_trace"))
    financial_gate = _mapping(trace.get("financial_gate"))
    for key in ("data_complete", "financial_data_complete", "complete"):
        if financial_gate.get(key) is False:
            codes.append("missing_financials")
    for key in ("stale", "is_stale"):
        if financial_gate.get(key) is True:
            codes.append("stale_financials")
    if financial_gate.get("default_penalty") is True:
        codes.append("default_financial_penalty")
    if financial_gate.get("allowed") is False or financial_gate.get("passed") is False:
        codes.append("financial_gate_block")
    return _dedupe_preserve_order(codes)


def _financial_classification(
    *,
    counter: Counter[str],
    score_by_code: Mapping[str, Sequence[float]],
    buy_rows_by_code: Mapping[str, int],
    rows: Sequence[Mapping[str, object]],
    config: StockAnalyzerConfig,
) -> dict[str, object]:
    total_rows = max(1, len(rows))
    codes = sorted(counter)
    buckets = []
    for code in codes:
        scores = [float(item) for item in score_by_code.get(code, [])]
        buckets.append(
            {
                "code": code,
                "rows": int(counter[code]),
                "row_pct": round(int(counter[code]) / total_rows, 6),
                "avg_score": round(mean(scores), 6) if scores else None,
                "buy_rows": int(buy_rows_by_code.get(code, 0)),
            }
        )
    true_low_roe_rows = max(
        0,
        counter.get("low_roe_penalty", 0) - counter.get("missing_financials", 0),
    )
    return {
        "buckets": buckets,
        "true_low_roe_evidence_rows": int(true_low_roe_rows),
        "missing_or_default_evidence_rows": int(
            counter.get("missing_financials", 0)
            + counter.get("missing_financial_data_gate", 0)
            + counter.get("default_financial_penalty", 0)
        ),
        "stale_evidence_rows": int(counter.get("stale_financials", 0)),
        "short_term_strength_may_be_overblocked": _short_term_strength_overblocked(
            rows=rows,
            config=config,
        ),
        "recommendation": (
            "repair_or_refresh_financial_data_before_relaxing_filter"
            if counter.get("missing_financials", 0)
            or counter.get("default_financial_penalty", 0)
            or counter.get("stale_financials", 0)
            else "shadow_validate_financial_filter_weight"
        ),
    }


def _short_term_strength_overblocked(
    *,
    rows: Sequence[Mapping[str, object]],
    config: StockAnalyzerConfig,
) -> dict[str, object]:
    threshold = _score_threshold(config)
    blocked_rows = []
    for row in rows:
        codes = _financial_data_quality_codes(row, config=config)
        score = _float(row.get("score"))
        if (
            score is not None
            and score >= threshold
            and any(code in codes for code in ("low_roe_penalty", "financial_gate_block"))
        ):
            blocked_rows.append(row)
    return {
        "rows": len(blocked_rows),
        "score_threshold": threshold,
        "symbols": sorted({str(row.get("symbol", "")).strip() for row in blocked_rows})[:20],
        "interpretation": (
            "high-score rows are still receiving financial penalties; verify in shadow "
            "before changing production financial filtering"
            if blocked_rows
            else "no high-score financial overblock evidence in captured rows"
        ),
    }


def _position_controls(config: StockAnalyzerConfig) -> dict[str, object]:
    return {
        "soup_strategy": {
            "dynamic_position": config.soup_strategy.dynamic_position,
            "max_holdings": config.soup_strategy.max_holdings,
            "max_hold_days": config.soup_strategy.max_hold_days,
            "max_same_sector": config.soup_strategy.max_same_sector,
            "take_profit": list(config.soup_strategy.take_profit),
            "stop_loss": config.soup_strategy.stop_loss,
            "trailing_stop": config.soup_strategy.trailing_stop,
            "recovery_buy_enabled": config.soup_strategy.recovery_buy_enabled,
            "disagreement_probe_enabled": config.soup_strategy.disagreement_probe_enabled,
        },
        "capital_curve": {
            "drawdown_alert": config.capital_curve.drawdown_alert,
            "drawdown_reduce": config.capital_curve.drawdown_reduce,
            "drawdown_freeze": config.capital_curve.drawdown_freeze,
            "protect_line": config.capital_curve.protect_line,
        },
        "monster_risk": {
            "max_total_position": config.monster_risk.max_total_position,
            "max_stock_position": config.monster_risk.max_stock_position,
            "disable_if_sentiment_below": config.monster_risk.disable_if_sentiment_below,
        },
    }


def _position_sizing_analysis(config: StockAnalyzerConfig) -> dict[str, object]:
    scenarios = []
    for atr_ratio in (0.01, 0.02, 0.03, 0.05, 0.08):
        position = min(0.15, 0.02 / atr_ratio)
        scenarios.append(
            {
                "atr_ratio": atr_ratio,
                "calculated_position_pct": round(position * 100.0, 4),
            }
        )
    return {
        "formula": config.soup_strategy.dynamic_position,
        "scenario_grid": scenarios,
        "status": "config_static_analysis",
    }


def _position_symbol_paths(
    *,
    rows: Sequence[Mapping[str, object]],
    execution: Mapping[str, object],
) -> list[dict[str, object]]:
    records_by_symbol = _mapping(execution.get("records_by_symbol"))
    signals_by_symbol: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        symbol = str(row.get("symbol", "")).strip()
        if symbol:
            signals_by_symbol[symbol].append(row)
    symbols = sorted(set(signals_by_symbol) | set(records_by_symbol))
    paths: list[dict[str, object]] = []
    for symbol in symbols:
        signals = list(signals_by_symbol.get(symbol, []))
        records = [
            item for item in _list(records_by_symbol.get(symbol)) if isinstance(item, Mapping)
        ]
        returns = [
            _float(item.get("realized_return_pct"))
            for item in records
            if _float(item.get("realized_return_pct")) is not None
        ]
        reasons = Counter(str(item.get("reason", "")).strip() or "unknown" for item in records)
        paths.append(
            {
                "symbol": symbol,
                "signal_count": len(signals),
                "buy_signal_count": sum(
                    1 for item in signals if str(item.get("action", "")).lower() == "buy"
                ),
                "execution_count": len(records),
                "avg_realized_return_pct": round(mean(returns), 6) if returns else None,
                "loss_count": sum(1 for item in returns if item is not None and item < 0),
                "execution_reasons": dict(reasons.most_common(8)),
                "reentry_hint": len(signals) > 1
                and any(item is not None and item < 0 for item in returns),
            }
        )
    return sorted(paths, key=lambda item: (-int(item["loss_count"]), str(item["symbol"])))


def _loss_path_analysis(symbol_paths: Sequence[Mapping[str, object]]) -> dict[str, object]:
    loss_paths = [item for item in symbol_paths if _int(item.get("loss_count")) > 0]
    reentry_paths = [item for item in loss_paths if bool(item.get("reentry_hint", False))]
    return {
        "loss_symbol_count": len(loss_paths),
        "reentry_after_loss_symbol_count": len(reentry_paths),
        "top_loss_symbols": [
            {
                "symbol": item.get("symbol"),
                "loss_count": item.get("loss_count"),
                "avg_realized_return_pct": item.get("avg_realized_return_pct"),
                "execution_reasons": item.get("execution_reasons"),
            }
            for item in loss_paths[:12]
        ],
    }


def _row_reason_text(row: Mapping[str, object]) -> str:
    parts: list[str] = []
    for item in _list(row.get("reasons")):
        parts.append(str(item).lower())
    parts.append(str(row.get("execution_rerank_reason", "")).lower())
    trace = _mapping(row.get("decision_trace"))
    parts.append(json.dumps(trace, ensure_ascii=False, sort_keys=True).lower())
    return " ".join(parts)


def _score_threshold(config: StockAnalyzerConfig) -> float:
    return float(config.score.thresholds.a)


def _equity_from_returns(returns: Sequence[float]) -> float | None:
    if not returns:
        return None
    equity = 1.0
    for value in returns:
        equity *= 1.0 + float(value)
    return round(equity, 6)


def _max_drawdown_from_returns(returns: Sequence[float]) -> float | None:
    if not returns:
        return None
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in returns:
        equity *= 1.0 + float(value)
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    return round(max_drawdown, 6)


def _losing_symbol_count(records_by_symbol: Mapping[str, object]) -> int:
    count = 0
    for records in records_by_symbol.values():
        returns = [
            _float(item.get("realized_return_pct"))
            for item in _list(records)
            if isinstance(item, Mapping)
        ]
        realized = [float(item) for item in returns if item is not None]
        if realized and mean(realized) < 0:
            count += 1
    return count


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _list_mut(item: dict[str, object], key: str) -> list[object]:
    value = item.get(key)
    if not isinstance(value, list):
        value = []
        item[key] = value
    return value


def _counter_mut(item: dict[str, object], key: str) -> Counter[str]:
    value = item.get(key)
    if not isinstance(value, Counter):
        value = Counter()
        item[key] = value
    return value


def _dedupe_preserve_order(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _quantile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    pos = (len(values) - 1) * q
    lower = int(pos)
    upper = min(lower + 1, len(values) - 1)
    if lower == upper:
        return float(values[lower])
    weight = pos - lower
    return float(values[lower]) * (1.0 - weight) + float(values[upper]) * weight


def _label_health_status(
    *,
    samples: int,
    positive: int,
    rate: float | None,
) -> str:
    if samples <= 0:
        return "missing_input"
    if rate is None or rate < 0.03 or positive < 5:
        return "needs_label_split_repair"
    return "usable"


def _positive_count(*, samples: int, positive_rate: float | None) -> int:
    if samples <= 0 or positive_rate is None:
        return 0
    return int(round(samples * max(0.0, positive_rate)))


def _load_json(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(data) if isinstance(data, Mapping) else {}


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: object) -> list[object]:
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


def _int(value: object, *, fallback: object | None = None) -> int:
    parsed = _float(value)
    if parsed is None and fallback is not None:
        parsed = _float(fallback)
    return int(parsed) if parsed is not None else 0
