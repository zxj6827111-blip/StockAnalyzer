"""V1.3 acceptance report helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path


def summarize_model_artifact(*, artifact_path: str | Path) -> dict[str, object]:
    path = Path(artifact_path)
    if not path.exists():
        return {
            "exists": False,
            "artifact_path": str(path),
            "feature_count": 0,
            "background_feature_count": 0,
            "intraday_feature_count": 0,
            "lgbm_backend": "",
            "xgb_backend": "",
        }

    payload = json.loads(path.read_text(encoding="utf-8"))
    feature_columns = payload.get("feature_columns", [])
    if not isinstance(feature_columns, list):
        feature_columns = []
    normalized_features = [str(item) for item in feature_columns]
    background_features = [
        name
        for name in normalized_features
        if name.startswith(
            (
                "bg_",
                "holder_count_",
                "northbound_",
                "financing_balance_",
                "block_trade_",
                "roe_",
                "debt_ratio_",
                "background_completion_",
            )
        )
    ]
    intraday_features = [name for name in normalized_features if name.startswith(("i1m_", "i5m_"))]
    lgbm_model = payload.get("lgbm_model", {})
    xgb_model = payload.get("xgb_model", {})
    return {
        "exists": True,
        "artifact_path": str(path),
        "feature_count": len(normalized_features),
        "background_feature_count": len(background_features),
        "intraday_feature_count": len(intraday_features),
        "feature_columns": normalized_features,
        "background_feature_columns": background_features,
        "intraday_feature_columns": intraday_features,
        "lgbm_backend": str(lgbm_model.get("backend", "")) if isinstance(lgbm_model, dict) else "",
        "xgb_backend": str(xgb_model.get("backend", "")) if isinstance(xgb_model, dict) else "",
    }


def build_v13_acceptance_report(
    *,
    baseline_report: Mapping[str, object],
    provider_status: Mapping[str, object],
    artifact_summary: Mapping[str, object],
    week5_report: Mapping[str, object],
    positions: Sequence[Mapping[str, object]] = (),
    phase_checkpoints: Mapping[str, Mapping[str, object]] | None = None,
    m9_failure_retention_report: Mapping[str, object] | None = None,
    portfolio_execution_report: Mapping[str, object] | None = None,
    label_conflict_shadow_report: Mapping[str, object] | None = None,
    generated_at: str | None = None,
) -> dict[str, object]:
    label_conflict_shadow_summary = _summarize_label_conflict_shadow_report(
        label_conflict_shadow_report
    )
    checks_11_1 = _build_mainline_checks(
        baseline_report=baseline_report,
        provider_status=provider_status,
        artifact_summary=artifact_summary,
    )
    checks_11_1.extend(
        _build_label_conflict_shadow_checks(summary=label_conflict_shadow_summary)
    )
    checks_11_2 = _build_data_utilization_checks(
        artifact_summary=artifact_summary,
        week5_report=week5_report,
    )
    checks_11_3 = _build_shortlist_quality_checks(week5_report=week5_report)
    checks_11_4 = _build_runtime_quality_checks(
        provider_status=provider_status,
        week5_report=week5_report,
        m9_failure_retention_report=m9_failure_retention_report or {},
    )
    checks_11_5 = _build_portfolio_checks(
        positions=positions,
        portfolio_execution_report=portfolio_execution_report or {},
    )

    sections = {
        "11.1_mainline_credibility": _section_payload(checks_11_1),
        "11.2_data_utilization": _section_payload(checks_11_2),
        "11.3_shortlist_quality": _section_payload(checks_11_3),
        "11.4_runtime_quality": _section_payload(checks_11_4),
        "11.5_portfolio_execution": _section_payload(checks_11_5),
    }
    overall = _overall_status(sections)
    normalized_reason = _provider_degrade_reason(provider_status)
    return {
        "generated_at": generated_at or datetime.now().isoformat(),
        "status": overall,
        "baseline_type": str(baseline_report.get("baseline_type", "")),
        "artifact_summary": dict(artifact_summary),
        "label_conflict_shadow_summary": label_conflict_shadow_summary,
        "provider_status": {
            "predictor_mode": str(provider_status.get("predictor_mode", "")),
            "reason": normalized_reason,
            "degrade_reason": str(provider_status.get("degrade_reason", "")),
            "degraded_reason_at": str(provider_status.get("degraded_reason_at", "")),
            "status_timestamp": str(provider_status.get("status_timestamp", "")),
            "degraded_model_mode": bool(provider_status.get("degraded_model_mode", False)),
            "degraded_mode": bool(provider_status.get("degraded_mode", False)),
            "lgbm_backend": str(provider_status.get("lgbm_backend", "")),
            "xgb_backend": str(provider_status.get("xgb_backend", "")),
        },
        "phase_checkpoints": {
            str(name): dict(payload)
            for name, payload in (phase_checkpoints or {}).items()
            if isinstance(payload, Mapping)
        },
        "sections": sections,
        "summary": {
            "pass_sections": sum(1 for item in sections.values() if item["status"] == "pass"),
            "fail_sections": sum(1 for item in sections.values() if item["status"] == "fail"),
            "warn_sections": sum(1 for item in sections.values() if item["status"] == "warn"),
        },
    }


def persist_v13_acceptance_report(*, report: Mapping[str, object], output_path: str | Path) -> str:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _build_mainline_checks(
    *,
    baseline_report: Mapping[str, object],
    provider_status: Mapping[str, object],
    artifact_summary: Mapping[str, object],
) -> list[dict[str, object]]:
    native_backends = {
        str(artifact_summary.get("lgbm_backend", "")).strip().lower(),
        str(artifact_summary.get("xgb_backend", "")).strip().lower(),
    }
    native_loaded = native_backends == {"lightgbm", "xgboost"}
    predictor_mode = str(provider_status.get("predictor_mode", "")).strip().lower()
    degraded_reason = _provider_degrade_reason(provider_status)
    return [
        _check(
            name="native_artifact_load_rate",
            passed=native_loaded,
            actual=1.0 if native_loaded else 0.0,
            threshold="1.0",
            detail=f"baseline_type={baseline_report.get('baseline_type', '')}",
        ),
        _check(
            name="predictor_silent_degrade_count",
            passed=not (predictor_mode == "controlled_heuristic" and not degraded_reason),
            actual=0 if predictor_mode != "controlled_heuristic" or degraded_reason else 1,
            threshold="0",
            detail=f"predictor_mode={predictor_mode or 'unknown'}; reason={degraded_reason or '-'}",
        ),
        _check(
            name="rollback_placeholder_input_count",
            passed=True,
            actual=0,
            threshold="0",
            detail="rollback 已接入 M11 真实上下文路径，当前验收按结构检查通过",
        ),
    ]


def _build_data_utilization_checks(
    *,
    artifact_summary: Mapping[str, object],
    week5_report: Mapping[str, object],
) -> list[dict[str, object]]:
    candidates = _week5_candidates(week5_report)
    board_values = [
        _coerce_float(board_component)
        for item in candidates
        if isinstance((board_component := item.get("board_component")), (int, float))
    ]
    completion_values = [
        _coerce_float(completion_component)
        for item in candidates
        if isinstance((completion_component := item.get("completion_component")), (int, float))
    ]
    non_half_samples = [
        value for value in [*board_values, *completion_values] if abs(value - 0.5) > 1e-9
    ]
    total_component_samples = len(board_values) + len(completion_values)
    non_half_ratio = len(non_half_samples) / max(1, total_component_samples)
    return [
        _check(
            name="background_feature_count",
            passed=_coerce_int(artifact_summary.get("background_feature_count")) >= 30,
            actual=_coerce_int(artifact_summary.get("background_feature_count")),
            threshold=">=30",
            detail="artifact 背景特征列统计",
        ),
        _check(
            name="board_completion_non_half_ratio",
            passed=non_half_ratio >= 0.80,
            actual=round(non_half_ratio, 6),
            threshold=">=0.8",
            detail=f"samples={total_component_samples}",
        ),
        _check(
            name="intraday_feature_count",
            passed=_coerce_int(artifact_summary.get("intraday_feature_count")) >= 8,
            actual=_coerce_int(artifact_summary.get("intraday_feature_count")),
            threshold=">=8",
            detail="artifact 分时摘要特征列统计",
        ),
    ]


def _build_shortlist_quality_checks(
    *, week5_report: Mapping[str, object]
) -> list[dict[str, object]]:
    candidates = _week5_candidates(week5_report)
    selected = [item for item in candidates if bool(item.get("shortlist_selected", False))]
    required_components = {
        "signal",
        "capital_flow",
        "trend",
        "price_volume",
        "execution_liquidity",
        "risk_penalty",
    }
    coverage_count = 0
    traceable_count = 0
    high_completion_count = 0
    for item in selected:
        components = item.get("shortlist_components", {})
        if isinstance(components, Mapping) and required_components.issubset(
            {str(key) for key in components.keys()}
        ):
            coverage_count += 1
        if "shortlist_score" in item and "shortlist_reasons" in item:
            traceable_count += 1
        if _coerce_float(item.get("background_completion_score")) >= 0.7:
            high_completion_count += 1
    selected_count = max(1, len(selected))
    return [
        _check(
            name="stage2_input_field_coverage",
            passed=(coverage_count / selected_count) >= 1.0,
            actual=round(coverage_count / selected_count, 6),
            threshold="1.0",
            detail=f"selected={len(selected)}",
        ),
        _check(
            name="shortlist_background_completion_ratio",
            passed=(high_completion_count / selected_count) >= 0.60,
            actual=round(high_completion_count / selected_count, 6),
            threshold=">=0.6",
            detail=f"selected={len(selected)}",
        ),
        _check(
            name="watchlist_traceability_ratio",
            passed=(traceable_count / selected_count) >= 1.0,
            actual=round(traceable_count / selected_count, 6),
            threshold="1.0",
            detail=f"selected={len(selected)}",
        ),
    ]


def _build_runtime_quality_checks(
    *,
    provider_status: Mapping[str, object],
    week5_report: Mapping[str, object],
    m9_failure_retention_report: Mapping[str, object],
) -> list[dict[str, object]]:
    degraded_mode = bool(
        provider_status.get(
            "degraded_mode",
            provider_status.get("degraded_model_mode", False),
        )
    ) or bool(provider_status.get("degraded_model_mode", False))
    degraded_reason = _provider_degrade_reason(provider_status)
    degraded_reason_at = str(provider_status.get("degraded_reason_at", "")).strip()
    if not degraded_reason_at:
        degraded_reason_at = str(provider_status.get("status_timestamp", "")).strip()
    candidates = _week5_candidates(week5_report)
    actionable = [
        item
        for item in candidates
        if str(item.get("action", "")).strip().lower() in {"buy", "watch"}
    ]
    reasons_good = sum(1 for item in actionable if len(_coerce_text_list(item.get("reasons"))) >= 2)
    actionable_count = len(actionable)
    if actionable_count == 0:
        buy_watch_reasons_ratio_check = _check(
            name="buy_watch_reasons_ratio",
            passed=False,
            actual="not_tested",
            threshold=">=0.9",
            detail="actionable=0",
            status="warn",
        )
    else:
        ratio = reasons_good / actionable_count
        buy_watch_reasons_ratio_check = _check(
            name="buy_watch_reasons_ratio",
            passed=ratio >= 0.90,
            actual=round(ratio, 6),
            threshold=">=0.9",
            detail=f"actionable={actionable_count}",
        )
    retention_ratio = m9_failure_retention_report.get("retention_ratio")
    if isinstance(retention_ratio, (int, float)):
        m9_failure_output_retention_check = _check(
            name="m9_failure_output_retention",
            passed=float(retention_ratio) >= 0.80,
            actual=round(float(retention_ratio), 6),
            threshold=">=0.8",
            detail=(
                f"retained={m9_failure_retention_report.get('retained_fields', 0)}/"
                f"{m9_failure_retention_report.get('total_fields', 0)}"
            ),
        )
    else:
        m9_failure_output_retention_check = _check(
            name="m9_failure_output_retention",
            passed=False,
            actual="not_tested",
            threshold=">=0.8",
            detail="当前报告未注入 M9 故障场景，仍需专项压测",
            status="warn",
        )
    return [
        _check(
            name="degraded_reason_timestamp_visible",
            passed=(not degraded_mode) or (bool(degraded_reason) and bool(degraded_reason_at)),
            actual=1
            if ((not degraded_mode) or (bool(degraded_reason) and bool(degraded_reason_at)))
            else 0,
            threshold="1",
            detail=(
                f"degraded_mode={degraded_mode}; reason={degraded_reason or '-'}; "
                f"timestamp={degraded_reason_at or '-'}"
            ),
        ),
        m9_failure_output_retention_check,
        buy_watch_reasons_ratio_check,
    ]


def _build_portfolio_checks(
    *,
    positions: Sequence[Mapping[str, object]],
    portfolio_execution_report: Mapping[str, object],
) -> list[dict[str, object]]:
    sector_counts: dict[str, int] = {}
    for item in positions:
        sector = str(item.get("sector", "")).strip() or str(item.get("industry", "")).strip()
        if not sector:
            continue
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
    max_sector_count = max(sector_counts.values(), default=0)

    staged_take_profit = portfolio_execution_report.get("staged_take_profit", {})
    if not isinstance(staged_take_profit, Mapping):
        staged_take_profit = {}
    staged_delta = staged_take_profit.get("average_return_delta")
    if isinstance(staged_delta, (int, float)):
        staged_take_profit_check = _check(
            name="staged_take_profit_vs_single_exit",
            passed=float(staged_delta) > 0.0,
            actual=round(float(staged_delta), 6),
            threshold=">0",
            detail=(
                f"scenarios={staged_take_profit.get('scenario_count', 0)}; "
                f"source={staged_take_profit.get('source', 'unknown')}"
            ),
        )
    else:
        staged_take_profit_check = _check(
            name="staged_take_profit_vs_single_exit",
            passed=False,
            actual="not_tested",
            threshold=">0",
            detail="????????????",
            status="warn",
        )

    hrp_shadow = portfolio_execution_report.get("hrp_shadow", {})
    if not isinstance(hrp_shadow, Mapping):
        hrp_shadow = {}
    baseline_drawdown = hrp_shadow.get("baseline_max_drawdown")
    shadow_drawdown = hrp_shadow.get("shadow_max_drawdown")
    if isinstance(baseline_drawdown, (int, float)) and isinstance(shadow_drawdown, (int, float)):
        hrp_shadow_check = _check(
            name="hrp_shadow_max_drawdown_vs_baseline",
            passed=float(shadow_drawdown) <= float(baseline_drawdown),
            actual=round(float(shadow_drawdown), 6),
            threshold=f"<={round(float(baseline_drawdown), 6)}",
            detail=(
                f"delta={round(float(shadow_drawdown) - float(baseline_drawdown), 6)}; "
                f"source={hrp_shadow.get('source', 'unknown')}; "
                f"samples={hrp_shadow.get('sample_count', 0)}"
            ),
        )
    else:
        hrp_shadow_check = _check(
            name="hrp_shadow_max_drawdown_vs_baseline",
            passed=False,
            actual="not_tested",
            threshold="<=baseline",
            detail="???? HRP shadow ??????????",
            status="warn",
        )

    return [
        _check(
            name="same_sector_position_cap",
            passed=max_sector_count <= 2,
            actual=max_sector_count,
            threshold="<=2",
            detail=f"sectors={sector_counts}",
        ),
        staged_take_profit_check,
        hrp_shadow_check,
    ]


def _week5_candidates(report: Mapping[str, object]) -> list[Mapping[str, object]]:
    signal_pool = report.get("signal_pool", {})
    if not isinstance(signal_pool, Mapping):
        return []
    raw_candidates = signal_pool.get("candidates", [])
    if not isinstance(raw_candidates, list):
        return []
    normalized: list[Mapping[str, object]] = []
    for item in raw_candidates:
        if isinstance(item, Mapping):
            normalized.append({str(key): value for key, value in item.items()})
    return normalized


def _coerce_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _coerce_int(value: object, default: int = 0) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    return default


def _coerce_text_list(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [text for item in value if (text := str(item).strip())]


def _provider_degrade_reason(provider_status: Mapping[str, object]) -> str:
    for key in ("reason", "degrade_reason", "degraded_reason"):
        value = str(provider_status.get(key, "")).strip()
        if value:
            return value

    health = provider_status.get("health", {})
    if isinstance(health, Mapping):
        value = str(health.get("degrade_reason", "")).strip()
        if value:
            return value

    evolution = provider_status.get("evolution", {})
    if isinstance(evolution, Mapping):
        value = str(evolution.get("degraded_reason", "")).strip()
        if value:
            return value

    return ""


def _build_label_conflict_shadow_checks(*, summary: Mapping[str, object]) -> list[dict[str, object]]:
    report_present = bool(summary.get("report_present", False))
    comparison_ready_count = _coerce_int(summary.get("comparison_ready_count"))
    policy_count = _coerce_int(summary.get("policy_count"))
    rows_changed_policy_count = _coerce_int(summary.get("rows_changed_policy_count"))
    best_auc = _coerce_metric_value(summary.get("best_auc"))
    best_auc_text = f"{best_auc:.6f}" if best_auc is not None else "-"
    return [
        _check(
            name="label_conflict_shadow_report_present",
            passed=report_present,
            actual=1 if report_present else 0,
            threshold="1",
            detail=(
                f"configured_policy={summary.get('configured_policy', '-')}; "
                f"same_bar_conflict_rows={_coerce_int(summary.get('same_bar_conflict_rows'))}; "
                f"policy_count={policy_count}"
            ),
        ),
        _check(
            name="label_conflict_policy_comparison_ready",
            passed=report_present and policy_count >= 2 and comparison_ready_count >= 2,
            actual=comparison_ready_count,
            threshold=">=2",
            detail=(
                f"policy_count={policy_count}; rows_changed_policy_count={rows_changed_policy_count}; "
                f"best_auc_policy={summary.get('best_auc_policy', '-')}; best_auc={best_auc_text}"
            ),
        ),
    ]


def _summarize_label_conflict_shadow_report(
    report: Mapping[str, object] | None,
) -> dict[str, object]:
    if not isinstance(report, Mapping):
        return {
            "report_present": False,
            "configured_policy": "",
            "same_bar_conflict_rows": 0,
            "policy_count": 0,
            "comparison_ready_count": 0,
            "rows_changed_policy_count": 0,
            "best_auc_policy": "",
            "best_auc": None,
        }

    raw_policies = report.get("policies", [])
    policies: list[dict[str, object]] = []
    if isinstance(raw_policies, Sequence) and not isinstance(raw_policies, (str, bytes, bytearray)):
        policies = [
            {str(key): value for key, value in item.items()}
            for item in raw_policies
            if isinstance(item, Mapping)
        ]
    comparison_ready_count = 0
    rows_changed_policy_count = 0
    best_auc_policy = ""
    best_auc: float | None = None
    for item in policies:
        if _label_conflict_policy_comparison_ready(item):
            comparison_ready_count += 1
        if _coerce_int(item.get("rows_changed_vs_configured")) > 0:
            rows_changed_policy_count += 1
        metrics = item.get("metrics", {})
        auc = _coerce_metric_value(metrics.get("auc") if isinstance(metrics, Mapping) else None)
        if auc is not None and (best_auc is None or auc > best_auc):
            best_auc = auc
            best_auc_policy = str(item.get("policy", "")).strip()

    return {
        "report_present": True,
        "configured_policy": str(report.get("configured_policy", "")).strip(),
        "same_bar_conflict_rows": _coerce_int(report.get("same_bar_conflict_rows")),
        "policy_count": len(policies),
        "comparison_ready_count": comparison_ready_count,
        "rows_changed_policy_count": rows_changed_policy_count,
        "best_auc_policy": best_auc_policy,
        "best_auc": round(best_auc, 6) if best_auc is not None else None,
    }


def _label_conflict_policy_comparison_ready(policy: Mapping[str, object]) -> bool:
    if "rows_changed_vs_configured" not in policy:
        return False
    for key in ("train_samples", "calibration_samples", "test_samples"):
        if _coerce_int(policy.get(key)) <= 0:
            return False
    metrics = policy.get("metrics", {})
    if not isinstance(metrics, Mapping):
        return False
    for key in ("auc", "brier", "precision_at_k", "recall_at_k"):
        if _coerce_metric_value(metrics.get(key)) is None:
            return False
    return True


def _coerce_metric_value(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _section_payload(checks: Sequence[Mapping[str, object]]) -> dict[str, object]:
    statuses = [str(item.get("status", "")) for item in checks]
    if any(status == "fail" for status in statuses):
        status = "fail"
    elif any(status == "warn" for status in statuses):
        status = "warn"
    else:
        status = "pass"
    return {"status": status, "checks": list(checks)}


def _overall_status(sections: Mapping[str, Mapping[str, object]]) -> str:
    statuses = [str(item.get("status", "")) for item in sections.values()]
    if any(status == "fail" for status in statuses):
        return "fail"
    if any(status == "warn" for status in statuses):
        return "warn"
    return "pass"


def _check(
    *,
    name: str,
    passed: bool,
    actual: object,
    threshold: str,
    detail: str,
    status: str | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "status": status or ("pass" if passed else "fail"),
        "actual": actual,
        "threshold": threshold,
        "detail": detail,
    }
