from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from stock_analyzer.config import load_config
from stock_analyzer.research.p0_analysis_inputs import (
    build_cross_review_failure_analysis,
    build_model_diagnosis_final,
    collect_signal_rows,
    write_p0_analysis_inputs,
)


def _load_test_config():
    root = Path(__file__).resolve().parents[1]
    return load_config(root / "config" / "default.yaml")


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_model_diagnosis_flags_zero_positive_test_split(tmp_path: Path) -> None:
    artifact = tmp_path / "model_v1.json"
    _write_json(
        artifact,
        {
            "version": "v2",
            "created_at": "2026-06-15T12:00:00",
            "feature_columns": ["a", "b"],
            "lgbm_model": {"backend": "fallback_logit"},
            "xgb_model": {"backend": "fallback_logit"},
            "training_metrics": {
                "positive_rate": 0.0,
                "test_samples": 52,
                "meta_mean_prob": 0.04175,
                "lgbm_mean_prob": 0.8,
                "xgb_mean_prob": 0.0,
            },
            "metadata": {
                "train_samples": 412,
                "calibration_samples": 52,
                "test_samples": 52,
                "degraded_model_mode": True,
            },
        },
    )

    report = build_model_diagnosis_final(
        model_artifact_path=artifact,
        generated_at=datetime.fromisoformat("2026-06-18T12:00:00"),
    )

    assert report["status"] == "needs_label_split_repair"
    assert report["label_distribution"]["test"]["positive"] == 0
    action_codes = {item["code"] for item in report["recommended_next_actions"]}
    assert "label_split_trainability_shadow" in action_codes
    assert "model_backend_dependency_check" in action_codes


def test_cross_review_failure_analysis_replays_current_thresholds() -> None:
    config = _load_test_config()
    report = build_cross_review_failure_analysis(
        signals=[
            {
                "symbol": "600000",
                "action": "buy",
                "score": 80,
                "probabilities": {"lgbm": 0.7, "xgb": 0.65, "meta": 0.62},
            },
            {
                "symbol": "000001",
                "action": "hold",
                "score": 45,
                "probabilities": {"lgbm": 1.0, "xgb": 0.2, "meta": 0.49},
            },
        ],
        config=config,
        generated_at=datetime.fromisoformat("2026-06-18T12:00:00"),
    )

    stats = report["cross_review_analysis"]["gate_statistics"]
    assert stats["total_evaluated_rows"] == 2
    assert stats["total_cross_review_pass"] == 1
    assert stats["total_cross_review_fail"] == 1
    reasons = report["cross_review_analysis"]["reason_counts"]
    assert reasons["xgb<0.55"] == 1
    assert reasons["model_diff>0.18"] == 1
    distribution = report["cross_review_analysis"]["probability_distribution"]
    assert distribution["meta"]["pass"] == 1


def test_write_p0_analysis_inputs_collects_runtime_and_week5_rows(tmp_path: Path) -> None:
    config = _load_test_config()
    model_artifact = tmp_path / "model_v1.json"
    runtime_state = tmp_path / "runtime_state.json"
    _write_json(
        model_artifact,
        {
            "training_metrics": {"positive_rate": 0.0, "test_samples": 10},
            "metadata": {"test_samples": 10},
        },
    )
    _write_json(
        runtime_state,
        {
            "latest_signals": {
                "timestamp": "2026-06-18T10:00:00",
                "signals": [
                    {
                        "symbol": "600000",
                        "score": 80,
                        "action": "buy",
                        "probabilities": {"lgbm": 0.7, "xgb": 0.65, "meta": 0.62},
                    }
                ],
            },
            "week5_scan_latest": {
                "timestamp": "2026-06-18T11:00:00",
                "signal_pool": {
                    "candidates": [
                        {
                            "symbol": "000001",
                            "shortlist_score": 45,
                            "action": "hold",
                            "probabilities": {"lgbm": 1.0, "xgb": 0.2, "meta": 0.49},
                        }
                    ]
                },
            },
        },
    )

    rows = collect_signal_rows([runtime_state])
    assert {row["symbol"] for row in rows} == {"600000", "000001"}

    manifest = write_p0_analysis_inputs(
        analysis_dir=tmp_path / "analysis",
        model_artifact_path=model_artifact,
        learning_manifest_paths=[],
        signal_source_paths=[runtime_state],
        config=config,
        generated_at=datetime.fromisoformat("2026-06-18T12:00:00"),
    )

    assert manifest["inputs"]["signal_rows"] == 2
    assert (tmp_path / "analysis" / "model_diagnosis_final.json").exists()
    assert (tmp_path / "analysis" / "p4_cross_review_failure_analysis_v1.json").exists()
    assert (tmp_path / "analysis" / "final_report_v3.json").exists()
    assert (tmp_path / "analysis" / "p4_feature_family_ablation_v1.json").exists()
    assert (tmp_path / "analysis" / "p5_position" / "position_framework_analysis.json").exists()
    assert manifest["remaining_expected_inputs"] == []


def test_collect_signal_rows_preserves_same_symbol_from_distinct_sources(
    tmp_path: Path,
) -> None:
    runtime_state = tmp_path / "runtime_state.json"
    _write_json(
        runtime_state,
        {
            "latest_signals": {
                "timestamp": "2026-06-18T10:00:00",
                "source": "latest_signals",
                "signals": [
                    {
                        "symbol": "600000",
                        "score": 61,
                        "action": "watch",
                        "probabilities": {"lgbm": 0.7, "xgb": 0.31, "meta": 0.49},
                    }
                ],
            },
            "week5_scan_latest": {
                "timestamp": "2026-06-18T10:00:00",
                "signal_pool": {
                    "candidates": [
                        {
                            "symbol": "600000",
                            "shortlist_score": 55,
                            "action": "hold",
                            "probabilities": {"lgbm": 0.8, "xgb": 0.2, "meta": 0.41},
                        }
                    ]
                },
            },
        },
    )

    rows = collect_signal_rows([runtime_state])

    assert len(rows) == 2
    assert {row["source_container"] for row in rows} == {
        "latest_signals",
        "week5_candidates",
    }


def test_write_p0_analysis_inputs_does_not_mutate_runtime_state(tmp_path: Path) -> None:
    config = _load_test_config()
    model_artifact = tmp_path / "model_v1.json"
    runtime_state = tmp_path / "runtime_state.json"
    _write_json(
        model_artifact,
        {
            "training_metrics": {"positive_rate": 0.0, "test_samples": 10},
            "metadata": {"test_samples": 10},
        },
    )
    original_runtime_state = {
        "latest_signals": {
            "timestamp": "2026-06-18T10:00:00",
            "signals": [
                {
                    "symbol": "600000",
                    "score": 80,
                    "action": "buy",
                    "probabilities": {"lgbm": 0.7, "xgb": 0.65, "meta": 0.62},
                }
            ],
        }
    }
    _write_json(runtime_state, original_runtime_state)
    before = runtime_state.read_text(encoding="utf-8")

    write_p0_analysis_inputs(
        analysis_dir=tmp_path / "analysis",
        model_artifact_path=model_artifact,
        learning_manifest_paths=[],
        signal_source_paths=[runtime_state],
        config=config,
        generated_at=datetime.fromisoformat("2026-06-18T12:00:00"),
    )

    assert runtime_state.read_text(encoding="utf-8") == before
    assert (tmp_path / "analysis" / "p0_analysis_inputs_manifest.json").exists()


def test_write_p0_analysis_inputs_links_runtime_portfolio_trades_to_outcomes(
    tmp_path: Path,
) -> None:
    config = _load_test_config()
    model_artifact = tmp_path / "model_v1.json"
    runtime_state = tmp_path / "runtime_state.json"
    _write_json(
        model_artifact,
        {
            "training_metrics": {"positive_rate": 0.12, "test_samples": 100},
            "metadata": {"test_samples": 100},
        },
    )
    _write_json(
        runtime_state,
        {
            "updated_at": "2026-06-18T10:00:00",
            "latest_signals": {
                "timestamp": "2026-06-18T10:00:00",
                "source": "pipeline_run",
                "signals": [
                    {
                        "symbol": "600000",
                        "score": 56,
                        "action": "watch",
                        "probabilities": {"lgbm": 0.5, "xgb": 0.31, "meta": 0.49},
                    }
                ],
            },
            "portfolio": {
                "trades": [
                    {
                        "trade_id": "TRD-1",
                        "side": "buy",
                        "symbol": "600000",
                        "strategy": "trend",
                        "timestamp": "2026-06-17T10:00:00",
                        "entry_price": 10.0,
                        "quantity": 100,
                    },
                    {
                        "trade_id": "TRD-2",
                        "side": "sell",
                        "symbol": "600000",
                        "strategy": "trend",
                        "timestamp": "2026-06-18T10:00:00",
                        "exit_price": 10.8,
                        "exit_quantity": 100,
                        "reason": "take_profit_stage_1_reached",
                    },
                ],
            },
        },
    )

    manifest = write_p0_analysis_inputs(
        analysis_dir=tmp_path / "analysis",
        model_artifact_path=model_artifact,
        learning_manifest_paths=[],
        signal_source_paths=[runtime_state],
        audit_event_paths=[runtime_state],
        config=config,
        generated_at=datetime.fromisoformat("2026-06-18T12:00:00"),
    )

    final_report = json.loads(
        (tmp_path / "analysis" / "final_report_v3.json").read_text("utf-8")
    )
    position_report = json.loads(
        (tmp_path / "analysis" / "p5_position" / "position_framework_analysis.json")
        .read_text("utf-8")
    )
    variants = final_report["threshold_sweep"]["results"]
    symbol_path = {
        item["symbol"]: item for item in position_report["symbol_paths"]
    }["600000"]
    linkage = final_report["threshold_sweep"]["outcome_linkage"]

    assert manifest["inputs"]["audit_events"] == 1
    baseline = final_report["multisymbol_multiwindow"]["summaries"]["baseline"]
    assert baseline["total_trades"] == 1
    assert linkage["symbols_with_returns"] == 1
    assert linkage["total_return_samples"] == 1
    assert linkage["return_samples_for_candidate_symbols"] == 1
    assert any(item["observed_trade_count"] == 1 for item in variants)
    assert symbol_path["execution_count"] == 1
    assert symbol_path["avg_realized_return_pct"] == 0.08
    assert symbol_path["execution_reasons"]["take_profit_stage_1_reached"] == 1


def test_write_p0_analysis_inputs_writes_research_completeness_artifacts(
    tmp_path: Path,
) -> None:
    config = _load_test_config()
    model_artifact = tmp_path / "model_v1.json"
    runtime_state = tmp_path / "runtime_state.json"
    audit_events = tmp_path / "audit_events.jsonl"
    _write_json(
        model_artifact,
        {
            "feature_columns": [
                "financial_low_roe",
                "market_relative_ret",
                "volume_ma",
                "atr14",
                "i1m_session_return",
                "weekday_sin",
                "mfi14",
                "stoch_k",
            ],
            "training_metrics": {"positive_rate": 0.12, "test_samples": 100},
            "metadata": {"test_samples": 100},
        },
    )
    _write_json(
        runtime_state,
        {
            "latest_signals": {
                "timestamp": "2026-06-18T10:00:00",
                "signals": [
                    {
                        "symbol": "600000",
                        "score": 48,
                        "action": "watch",
                        "probabilities": {"lgbm": 1.0, "xgb": 0.3, "meta": 0.48},
                        "reasons": [
                            "xgb<0.55",
                            "meta<0.54",
                            "financial_penalty:low_roe",
                            "financial_data_complete_false",
                        ],
                    },
                    {
                        "symbol": "000159",
                        "score": 52,
                        "action": "buy",
                        "probabilities": {"lgbm": 0.8, "xgb": 0.31, "meta": 0.49},
                    },
                    {
                        "symbol": "000159",
                        "score": 55,
                        "action": "watch",
                        "probabilities": {"lgbm": 0.75, "xgb": 0.32, "meta": 0.5},
                    },
                    {
                        "symbol": "001258",
                        "score": 61,
                        "action": "buy",
                        "probabilities": {"lgbm": 0.78, "xgb": 0.3, "meta": 0.48},
                    }
                ],
            }
        },
    )
    audit_events.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event_type": "pipeline_run",
                        "payload": {
                            "execution_mode": "portfolio_auto_apply_dry_run",
                            "dry_run_execution": True,
                            "portfolio_update": {
                                "dry_run": True,
                                "execution_attempts": {
                                    "buy_signals": 1,
                                    "buy_new_attempted": 1,
                                },
                                "executions": [
                                    {
                                        "symbol": "600000",
                                        "status": "opened",
                                        "reason": "auto_simulated_buy",
                                    },
                                    {
                                        "symbol": "000159",
                                        "status": "closed",
                                        "reason": "stop_loss",
                                        "realized_return_pct": -0.0623,
                                    }
                                ],
                            },
                        },
                    },
                    ensure_ascii=False,
                )
            ]
        ),
        encoding="utf-8",
    )

    manifest = write_p0_analysis_inputs(
        analysis_dir=tmp_path / "analysis",
        model_artifact_path=model_artifact,
        learning_manifest_paths=[],
        signal_source_paths=[runtime_state],
        audit_event_paths=[audit_events],
        config=config,
        generated_at=datetime.fromisoformat("2026-06-18T12:00:00"),
    )

    analysis_dir = tmp_path / "analysis"
    final_report = json.loads((analysis_dir / "final_report_v3.json").read_text("utf-8"))
    feature_report = json.loads(
        (analysis_dir / "p4_feature_family_ablation_v1.json").read_text("utf-8")
    )
    position_report = json.loads(
        (analysis_dir / "p5_position" / "position_framework_analysis.json").read_text("utf-8")
    )
    assert manifest["remaining_expected_inputs"] == []
    assert final_report["production_change_allowed"] is False
    assert final_report["threshold_sweep"]["grid"]["xgb_min"] == [0.25, 0.3, 0.33]
    assert final_report["source_scope"]["dry_run_events"] == 1
    assert feature_report["financial_data_quality"]["reason_counts"]["missing_financials"] == 1
    classification = feature_report["financial_data_quality"]["classification"]
    assert classification["missing_or_default_evidence_rows"] >= 1
    assert (
        classification["recommendation"]
        == "repair_or_refresh_financial_data_before_relaxing_filter"
    )
    assert feature_report["feature_families"]["volatility"] == 1
    assert feature_report["feature_families"]["intraday"] == 1
    assert feature_report["feature_families"]["calendar_time"] == 1
    assert "expanded_feature_family_taxonomy_from_local_p4_scripts" in feature_report[
        "local_change_review"
    ]["applied"]
    assert position_report["production_change_allowed"] is False
    assert "atr_bounds_position_shadow" in position_report["recommended_shadow"]
    sizing = position_report["position_sizing_analysis"]
    assert sizing["atr_bounds_shadow"]["status"] == "design_only_no_production_change"
    assert len(sizing["atr_bounds_shadow"]["scenario_grid"]) == 9
    assert "no SoupStrategy._dynamic_position production change" in position_report[
        "local_change_review"
    ]["not_applied"]
    focus = {
        item["symbol"]: item
        for item in position_report["focus_symbols"]["symbols"]
    }
    assert set(focus) == {"000159", "001258", "600956"}
    assert focus["000159"]["loss_count"] == 1
    assert focus["000159"]["reentry_hint"] is True
    assert "re-entry quality issue" in " ".join(focus["000159"]["diagnosis"])
    assert focus["001258"]["buy_signal_count"] == 1
    assert focus["001258"]["execution_count"] == 0
    assert "cash/lot/risk gates" in " ".join(focus["001258"]["diagnosis"])
    assert focus["600956"]["status"] == "missing_runtime_evidence"
    assert position_report["focus_symbols"]["loss_observed_count"] == 1
    assert position_report["focus_symbols"]["missing_evidence_symbols"] == ["600956"]


def test_final_report_threshold_sweep_links_candidate_variants_to_outcomes(
    tmp_path: Path,
) -> None:
    config = _load_test_config()
    model_artifact = tmp_path / "model_v1.json"
    runtime_state = tmp_path / "runtime_state.json"
    audit_events = tmp_path / "audit_events.jsonl"
    _write_json(
        model_artifact,
        {
            "training_metrics": {"positive_rate": 0.12, "test_samples": 100},
            "metadata": {"test_samples": 100},
        },
    )
    _write_json(
        runtime_state,
        {
            "latest_signals": {
                "timestamp": "2026-06-18T10:00:00",
                "signals": [
                    {
                        "symbol": "600000",
                        "score": 56,
                        "action": "watch",
                        "probabilities": {"lgbm": 0.5, "xgb": 0.31, "meta": 0.49},
                    },
                    {
                        "symbol": "000001",
                        "score": 44,
                        "action": "watch",
                        "probabilities": {"lgbm": 0.5, "xgb": 0.28, "meta": 0.46},
                    },
                ],
            }
        },
    )
    audit_events.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event_type": "pipeline_run",
                        "payload": {
                            "execution_mode": "portfolio_auto_apply",
                            "portfolio_update": {
                                "executions": [
                                    {
                                        "symbol": "600000",
                                        "status": "closed",
                                        "realized_return_pct": 0.08,
                                    },
                                    {
                                        "symbol": "000001",
                                        "status": "closed",
                                        "realized_return_pct": -0.04,
                                    },
                                ],
                            },
                        },
                    },
                    ensure_ascii=False,
                )
            ]
        ),
        encoding="utf-8",
    )

    manifest = write_p0_analysis_inputs(
        analysis_dir=tmp_path / "analysis",
        model_artifact_path=model_artifact,
        learning_manifest_paths=[],
        signal_source_paths=[runtime_state],
        audit_event_paths=[audit_events],
        config=config,
        generated_at=datetime.fromisoformat("2026-06-18T12:00:00"),
    )

    final_report = json.loads(
        (tmp_path / "analysis" / "final_report_v3.json").read_text("utf-8")
    )
    variants = final_report["threshold_sweep"]["results"]
    best = final_report["threshold_sweep"]["top_candidate_generating_variants"][0]

    assert manifest["inputs"]["audit_events"] == 1
    linkage = final_report["threshold_sweep"]["outcome_linkage"]
    assert linkage["symbols_with_returns"] == 2
    assert linkage["total_return_samples"] == 2
    assert linkage["return_samples_for_candidate_symbols"] == 2
    assert linkage["variants_with_observed_trades"] > 0
    assert any(item["observed_trade_count"] == 2 for item in variants)
    assert best["final_equity"] is not None
    assert "win_rate" in best
    assert "max_drawdown" in best
    diagnostics = final_report["threshold_sweep"]["blocking_diagnostics"]
    assert diagnostics["complete_probability_score_rows"] == 2
    assert diagnostics["minimum_grid_pass_count"] >= 1
    assert diagnostics["distributions"]["xgb"]["pass_threshold"] == 0.25


def test_final_report_threshold_sweep_explains_zero_candidate_grid(tmp_path: Path) -> None:
    config = _load_test_config()
    model_artifact = tmp_path / "model_v1.json"
    runtime_state = tmp_path / "runtime_state.json"
    _write_json(
        model_artifact,
        {
            "training_metrics": {"positive_rate": 0.12, "test_samples": 100},
            "metadata": {"test_samples": 100},
        },
    )
    _write_json(
        runtime_state,
        {
            "latest_signals": {
                "timestamp": "2026-06-18T10:00:00",
                "source": "pipeline_run",
                "signals": [
                    {
                        "symbol": "600000",
                        "score": 39.5,
                        "action": "watch",
                        "probabilities": {"lgbm": 0.36, "xgb": 0.18, "meta": 0.26},
                    },
                    {
                        "symbol": "000001",
                        "score": 20.0,
                        "action": "watch",
                    },
                ],
            }
        },
    )

    write_p0_analysis_inputs(
        analysis_dir=tmp_path / "analysis",
        model_artifact_path=model_artifact,
        learning_manifest_paths=[],
        signal_source_paths=[runtime_state],
        config=config,
        generated_at=datetime.fromisoformat("2026-06-18T12:00:00"),
    )

    final_report = json.loads(
        (tmp_path / "analysis" / "final_report_v3.json").read_text("utf-8")
    )
    sweep = final_report["threshold_sweep"]
    diagnostics = sweep["blocking_diagnostics"]
    suggested = sweep["minimum_candidate_thresholds"]

    assert sweep["status"] == "not_effective"
    assert diagnostics["complete_probability_score_rows"] == 1
    assert diagnostics["missing_probability_or_score_rows"] == 1
    assert diagnostics["minimum_grid_pass_count"] == 0
    assert diagnostics["blocker_counts"]["xgb_below_min_grid"] == 1
    assert diagnostics["blocker_counts"]["meta_below_min_grid"] == 1
    assert diagnostics["blocker_counts"]["score_below_min_grid"] == 1
    assert "probability scale" in diagnostics["interpretation"]
    assert suggested["status"] == "suggested_from_complete_rows"


def test_financial_data_quality_classifies_missing_stale_default_and_low_roe(
    tmp_path: Path,
) -> None:
    config = _load_test_config()
    model_artifact = tmp_path / "model_v1.json"
    runtime_state = tmp_path / "runtime_state.json"
    _write_json(
        model_artifact,
        {
            "training_metrics": {"positive_rate": 0.12, "test_samples": 100},
            "metadata": {"test_samples": 100},
        },
    )
    _write_json(
        runtime_state,
        {
            "latest_signals": {
                "timestamp": "2026-06-18T10:00:00",
                "signals": [
                    {
                        "symbol": "600000",
                        "score": 80,
                        "action": "watch",
                        "reasons": ["financial_penalty:low_roe"],
                        "decision_trace": {
                            "financial_gate": {
                                "allowed": False,
                                "data_complete": False,
                                "default_penalty": True,
                                "stale": True,
                            }
                        },
                    },
                    {
                        "symbol": "000001",
                        "score": 62,
                        "action": "watch",
                        "reasons": ["financial_penalty:low_roe"],
                        "decision_trace": {
                            "financial_gate": {
                                "allowed": False,
                                "data_complete": True,
                                "roe": -0.01,
                            }
                        },
                    },
                ],
            }
        },
    )

    write_p0_analysis_inputs(
        analysis_dir=tmp_path / "analysis",
        model_artifact_path=model_artifact,
        learning_manifest_paths=[],
        signal_source_paths=[runtime_state],
        config=config,
        generated_at=datetime.fromisoformat("2026-06-18T12:00:00"),
    )

    feature_report = json.loads(
        (tmp_path / "analysis" / "p4_feature_family_ablation_v1.json").read_text("utf-8")
    )
    quality = feature_report["financial_data_quality"]

    assert quality["reason_counts"]["low_roe_penalty"] == 2
    assert quality["reason_counts"]["missing_financials"] == 1
    assert quality["reason_counts"]["default_financial_penalty"] == 1
    assert quality["reason_counts"]["stale_financials"] == 1
    overblock = quality["classification"]["short_term_strength_may_be_overblocked"]
    assert overblock["rows"] == 2
    assert overblock["data_quality_issue_rows"] == 1
    assert overblock["confirmed_low_roe_rows"] == 1
    low_roe = quality["classification"]["low_roe_evidence"]
    assert low_roe["confirmed_true_low_roe_rows"] == 1
    assert low_roe["ambiguous_low_roe_rows"] == 1
    assert low_roe["inferred_low_roe_rows"] == 0
    assert quality["classification"]["true_low_roe_evidence_rows"] == 1
