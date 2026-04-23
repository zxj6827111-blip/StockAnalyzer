from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from stock_analyzer.v13_acceptance import (
    build_v13_acceptance_report,
    persist_v13_acceptance_report,
    summarize_model_artifact,
)


def _section(report: Mapping[str, object], name: str) -> Mapping[str, object]:
    sections = cast(Mapping[str, object], report["sections"])
    return cast(Mapping[str, object], sections[name])


def _checks(report: Mapping[str, object], section_name: str) -> Sequence[Mapping[str, object]]:
    return cast(Sequence[Mapping[str, object]], _section(report, section_name)["checks"])


def _find_check(
    report: Mapping[str, object],
    section_name: str,
    check_name: str,
) -> Mapping[str, object]:
    return next(
        item for item in _checks(report, section_name) if str(item.get("name", "")) == check_name
    )


def _as_float(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    raise AssertionError(f"Expected numeric value, got {value!r}")


def _sample_label_conflict_shadow_report() -> Mapping[str, object]:
    return {
        "configured_policy": "conservative_zero",
        "same_bar_conflict_rows": 18,
        "policies": [
            {
                "policy": "conservative_zero",
                "rows_changed_vs_configured": 0,
                "train_samples": 120,
                "calibration_samples": 20,
                "test_samples": 20,
                "metrics": {
                    "auc": 0.58,
                    "brier": 0.22,
                    "precision_at_k": 0.61,
                    "recall_at_k": 0.34,
                },
            },
            {
                "policy": "bar_shape_heuristic",
                "rows_changed_vs_configured": 11,
                "train_samples": 120,
                "calibration_samples": 20,
                "test_samples": 20,
                "metrics": {
                    "auc": 0.64,
                    "brier": 0.2,
                    "precision_at_k": 0.66,
                    "recall_at_k": 0.39,
                },
            },
            {
                "policy": "soft_label",
                "rows_changed_vs_configured": 18,
                "train_samples": 120,
                "calibration_samples": 20,
                "test_samples": 20,
                "metrics": {
                    "auc": 0.62,
                    "brier": 0.21,
                    "precision_at_k": 0.63,
                    "recall_at_k": 0.36,
                },
            },
        ],
    }


def test_summarize_model_artifact_counts_background_and_intraday_features(tmp_path: Path) -> None:
    artifact_path = tmp_path / "model.json"
    artifact_path.write_text(
        json.dumps(
            {
                "feature_columns": [
                    "bg_roe",
                    "holder_count_chg_5",
                    "northbound_net_20",
                    "i1m_tail30_volume_share",
                    "i5m_close_vwap_stability",
                ],
                "lgbm_model": {"backend": "fallback_logit"},
                "xgb_model": {"backend": "fallback_logit"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary = summarize_model_artifact(artifact_path=artifact_path)

    assert summary["feature_count"] == 5
    assert summary["background_feature_count"] == 3
    assert summary["intraday_feature_count"] == 2


def test_build_v13_acceptance_report_evaluates_sections_and_persists(tmp_path: Path) -> None:
    report = build_v13_acceptance_report(
        baseline_report={"baseline_type": "fallback_baseline"},
        provider_status={
            "predictor_mode": "artifact_loaded",
            "reason": "",
            "degraded_model_mode": False,
        },
        artifact_summary={
            "feature_count": 167,
            "background_feature_count": 36,
            "intraday_feature_count": 24,
            "lgbm_backend": "fallback_logit",
            "xgb_backend": "fallback_logit",
        },
        week5_report={
            "signal_pool": {
                "candidates": [
                    {
                        "action": "buy",
                        "reasons": ["board_component:0.61", "completion_component:0.82"],
                        "board_component": 0.61,
                        "completion_component": 0.82,
                        "background_completion_score": 0.80,
                        "shortlist_score": 71.0,
                        "shortlist_reasons": ["signal_strength"],
                        "shortlist_selected": True,
                        "shortlist_components": {
                            "signal": 0.8,
                            "capital_flow": 0.7,
                            "trend": 0.7,
                            "price_volume": 0.7,
                            "execution_liquidity": 0.7,
                            "risk_penalty": 0.1,
                        },
                    },
                    {
                        "action": "watch",
                        "reasons": ["board_component:0.63", "completion_component:0.84"],
                        "board_component": 0.63,
                        "completion_component": 0.84,
                        "background_completion_score": 0.75,
                        "shortlist_score": 69.0,
                        "shortlist_reasons": ["trend_alignment"],
                        "shortlist_selected": True,
                        "shortlist_components": {
                            "signal": 0.75,
                            "capital_flow": 0.65,
                            "trend": 0.68,
                            "price_volume": 0.66,
                            "execution_liquidity": 0.62,
                            "risk_penalty": 0.2,
                        },
                    },
                ]
            }
        },
        positions=[],
        label_conflict_shadow_report=_sample_label_conflict_shadow_report(),
    )

    assert str(_section(report, "11.2_data_utilization").get("status", "")) == "pass"
    assert str(_section(report, "11.3_shortlist_quality").get("status", "")) == "pass"
    assert str(_section(report, "11.1_mainline_credibility").get("status", "")) == "fail"
    shadow_summary = cast(Mapping[str, object], report["label_conflict_shadow_summary"])
    assert shadow_summary["configured_policy"] == "conservative_zero"
    assert shadow_summary["policy_count"] == 3
    assert shadow_summary["comparison_ready_count"] == 3
    assert shadow_summary["rows_changed_policy_count"] == 2
    assert shadow_summary["best_auc_policy"] == "bar_shape_heuristic"
    shadow_report_check = _find_check(
        report,
        "11.1_mainline_credibility",
        "label_conflict_shadow_report_present",
    )
    shadow_ready_check = _find_check(
        report,
        "11.1_mainline_credibility",
        "label_conflict_policy_comparison_ready",
    )
    assert str(shadow_report_check.get("status", "")) == "pass"
    assert str(shadow_ready_check.get("status", "")) == "pass"
    output_path = tmp_path / "acceptance" / "v13_acceptance_report.json"
    written = persist_v13_acceptance_report(report=report, output_path=output_path)
    payload = json.loads(Path(written).read_text(encoding="utf-8"))
    assert payload["status"] in {"fail", "warn", "pass"}


def test_build_v13_acceptance_report_uses_degrade_reason_fallbacks() -> None:
    report = build_v13_acceptance_report(
        baseline_report={"baseline_type": "native_baseline"},
        provider_status={
            "predictor_mode": "artifact_loaded",
            "degraded_model_mode": True,
            "degrade_reason": "health_guardrail",
            "degraded_reason_at": "2026-03-12T09:30:00",
            "lgbm_backend": "lightgbm",
            "xgb_backend": "xgboost",
        },
        artifact_summary={
            "feature_count": 167,
            "background_feature_count": 36,
            "intraday_feature_count": 24,
            "lgbm_backend": "lightgbm",
            "xgb_backend": "xgboost",
        },
        week5_report={"signal_pool": {"candidates": []}},
        positions=[],
    )

    degraded_reason_check = _find_check(
        report,
        "11.4_runtime_quality",
        "degraded_reason_timestamp_visible",
    )

    assert str(degraded_reason_check.get("status", "")) == "pass"
    provider_status = cast(Mapping[str, object], report["provider_status"])
    assert str(provider_status.get("reason", "")) == "health_guardrail"


def test_build_v13_acceptance_report_uses_status_timestamp_when_reason_at_blank() -> None:
    report = build_v13_acceptance_report(
        baseline_report={"baseline_type": "native_baseline"},
        provider_status={
            "predictor_mode": "artifact_loaded",
            "degraded_model_mode": True,
            "degrade_reason": "m2_trend_up",
            "degraded_reason_at": "",
            "status_timestamp": "2026-03-12T10:30:19",
            "lgbm_backend": "lightgbm",
            "xgb_backend": "xgboost",
        },
        artifact_summary={
            "feature_count": 167,
            "background_feature_count": 36,
            "intraday_feature_count": 24,
            "lgbm_backend": "lightgbm",
            "xgb_backend": "xgboost",
        },
        week5_report={"signal_pool": {"candidates": []}},
        positions=[],
    )

    degraded_reason_check = _find_check(
        report,
        "11.4_runtime_quality",
        "degraded_reason_timestamp_visible",
    )

    assert str(degraded_reason_check.get("status", "")) == "pass"
    assert "timestamp=2026-03-12T10:30:19" in str(degraded_reason_check.get("detail", ""))


def test_build_v13_acceptance_report_marks_empty_actionable_ratio_as_warn() -> None:
    report = build_v13_acceptance_report(
        baseline_report={"baseline_type": "native_baseline"},
        provider_status={
            "predictor_mode": "artifact_loaded",
            "reason": "",
            "degraded_model_mode": False,
            "lgbm_backend": "lightgbm",
            "xgb_backend": "xgboost",
        },
        artifact_summary={
            "feature_count": 167,
            "background_feature_count": 36,
            "intraday_feature_count": 24,
            "lgbm_backend": "lightgbm",
            "xgb_backend": "xgboost",
        },
        week5_report={"signal_pool": {"candidates": []}},
        positions=[],
    )

    buy_watch_ratio_check = _find_check(report, "11.4_runtime_quality", "buy_watch_reasons_ratio")

    assert str(buy_watch_ratio_check.get("status", "")) == "warn"
    assert str(buy_watch_ratio_check.get("actual", "")) == "not_tested"


def test_build_v13_acceptance_report_uses_m9_failure_retention_artifact() -> None:
    report = build_v13_acceptance_report(
        baseline_report={"baseline_type": "native_baseline"},
        provider_status={
            "predictor_mode": "artifact_loaded",
            "reason": "",
            "degraded_model_mode": False,
            "lgbm_backend": "lightgbm",
            "xgb_backend": "xgboost",
        },
        artifact_summary={
            "feature_count": 167,
            "background_feature_count": 36,
            "intraday_feature_count": 24,
            "lgbm_backend": "lightgbm",
            "xgb_backend": "xgboost",
        },
        week5_report={"signal_pool": {"candidates": []}},
        positions=[],
        m9_failure_retention_report={
            "retention_ratio": 0.9,
            "retained_fields": 9,
            "total_fields": 10,
        },
    )

    retention_check = _find_check(report, "11.4_runtime_quality", "m9_failure_output_retention")

    assert str(retention_check.get("status", "")) == "pass"
    assert _as_float(retention_check.get("actual", 0.0)) == 0.9


def test_build_v13_acceptance_report_uses_portfolio_execution_report() -> None:
    report = build_v13_acceptance_report(
        baseline_report={"baseline_type": "native_baseline"},
        provider_status={
            "predictor_mode": "artifact_loaded",
            "reason": "",
            "degraded_model_mode": False,
            "lgbm_backend": "lightgbm",
            "xgb_backend": "xgboost",
        },
        artifact_summary={
            "feature_count": 167,
            "background_feature_count": 36,
            "intraday_feature_count": 24,
            "lgbm_backend": "lightgbm",
            "xgb_backend": "xgboost",
        },
        week5_report={"signal_pool": {"candidates": []}},
        positions=[],
        portfolio_execution_report={
            "staged_take_profit": {
                "average_return_delta": 0.0215,
                "scenario_count": 3,
                "source": "deterministic_path_benchmark",
            },
            "hrp_shadow": {
                "baseline_max_drawdown": 0.084,
                "shadow_max_drawdown": 0.061,
                "sample_count": 60,
                "source": "inverse_vol_fallback",
            },
        },
    )

    staged_check = _find_check(
        report,
        "11.5_portfolio_execution",
        "staged_take_profit_vs_single_exit",
    )
    hrp_check = _find_check(
        report,
        "11.5_portfolio_execution",
        "hrp_shadow_max_drawdown_vs_baseline",
    )

    assert str(staged_check.get("status", "")) == "pass"
    assert str(hrp_check.get("status", "")) == "pass"
