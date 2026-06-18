from __future__ import annotations

from pathlib import Path

from stock_analyzer.config import load_config
from stock_analyzer.research.signal_quality_auditor import SignalQualityAuditor


def _load_config():
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.models.cross_review.p_lgbm_min = 0.60
    config.models.cross_review.p_xgb_min = 0.55
    config.models.cross_review.p_meta_min = 0.54
    config.models.cross_review.max_diff = 0.18
    return config


def test_signal_quality_auditor_attributes_cross_review_and_learning_repair() -> None:
    auditor = SignalQualityAuditor(config=_load_config())

    report = auditor.build_report(
        latest_signals=[
            {
                "symbol": "000006",
                "strategy": "trend",
                "score": 57.1,
                "grade": "B",
                "action": "watch",
                "probabilities": {"lgbm": 1.0, "xgb": 0.43, "meta": 0.49},
                "reasons": ["cross_review"],
                "decision_trace": {
                    "cross_review_gate": {"passed": False},
                    "provider": {"soft_degraded_mode": True, "degrade_reason": "m2_extreme"},
                    "financial_gate": {"passed": False},
                },
            }
        ],
        notification_filter_diagnostics={
            "rejected_by_score": 1,
            "min_score_by_action": {"watch": 50.0},
        },
        provider_status={"soft_degraded_mode": True, "degrade_reason": "m2_extreme"},
        week5_report={
            "empty_signal": {"triggered": True, "reasons": ["no_buy_streak"]},
            "signal_pool": {"candidate_count": 120},
            "buy_signals": 0,
        },
        learning_governance={
            "active_champion": None,
            "active_champion_repair": {
                "required": True,
                "reason": "active champion not found",
            },
            "config": {"active_champion_id": "champion_v7_202603"},
        },
    )

    assert report["status"] == "ok"
    assert report["summary"]["action_breakdown"] == {"watch": 1}
    assert report["gate_attribution"]["counts"]["cross_review"] == 1
    assert report["gate_attribution"]["counts"]["provider_degraded"] == 1
    assert report["learning_context"]["repair_required"] is True
    assert report["near_misses"][0]["cross_review_gap"]["xgb_below_min"] > 0
    assert report["near_misses"][0]["cross_review_gap"]["model_diff_excess"] > 0
    assert report["recommended_next_actions"][0]["code"] == "repair_active_champion"


def test_signal_quality_auditor_handles_empty_latest_signals() -> None:
    auditor = SignalQualityAuditor(config=_load_config())

    report = auditor.build_report(latest_signals=[])

    assert report["status"] == "empty"
    assert report["summary"]["signal_count"] == 0
    assert report["recommended_next_actions"][0]["code"] == "restore_signal_materialization"


def test_signal_quality_auditor_does_not_count_probe_as_cross_review_block() -> None:
    auditor = SignalQualityAuditor(config=_load_config())

    report = auditor.build_report(
        latest_signals=[
            {
                "symbol": "301369",
                "strategy": "trend",
                "score": 47.3,
                "grade": "C",
                "action": "buy",
                "probabilities": {"lgbm": 1.0, "xgb": 0.2694, "meta": 0.4970},
                "reasons": [
                    "xgb<0.55",
                    "model_diff>0.18",
                    "meta<0.54",
                    "financial_penalty:low_roe",
                    "model_disagreement_probe",
                ],
                "decision_trace": {
                    "cross_review_gate": {
                        "passed": False,
                        "mode": "strict",
                    },
                    "provider": {"soft_degraded_mode": True, "degrade_reason": "m2_extreme"},
                    "financial_gate": {"passed": True},
                },
            }
        ],
    )

    assert report["status"] == "ok"
    assert "cross_review" not in report["gate_attribution"]["counts"]
    assert report["gate_attribution"]["counts"]["provider_degraded"] == 1


def test_signal_quality_auditor_reports_execution_risk_artifact_gap() -> None:
    auditor = SignalQualityAuditor(config=_load_config())

    report = auditor.build_report(
        latest_signals=[
            {
                "symbol": "001258",
                "strategy": "week5",
                "score": 46.76,
                "grade": "C",
                "action": "buy",
                "execution_rerank_reason": "execution_risk_artifact_unavailable",
                "execution_rerank_applied": False,
                "reasons": ["model_disagreement_probe"],
            }
        ],
    )

    assert report["execution_risk_context"]["artifact_unavailable"] is True
    assert (
        report["execution_risk_context"]["reason_counts"]["execution_risk_artifact_unavailable"]
        == 1
    )
    assert any(
        item["code"] == "train_execution_risk_artifact"
        for item in report["recommended_next_actions"]
    )


def test_signal_quality_auditor_builds_signal_loss_funnel_from_signals_and_events() -> None:
    auditor = SignalQualityAuditor(config=_load_config())

    report = auditor.build_report(
        latest_signals=[
            {
                "symbol": "600000",
                "strategy": "trend",
                "score": 66.0,
                "grade": "A",
                "action": "buy",
                "target_position": 0.12,
                "probabilities": {"lgbm": 0.72, "xgb": 0.65, "meta": 0.61},
                "reasons": ["buy"],
                "decision_trace": {
                    "cross_review_gate": {"passed": True},
                    "financial_gate": {"allowed": True},
                    "liquidity_gate": {"passed": True},
                    "risk_gate": {"passed": True},
                },
            },
            {
                "symbol": "000001",
                "strategy": "trend",
                "score": 48.0,
                "grade": "C",
                "action": "hold",
                "target_position": 0.0,
                "probabilities": {"lgbm": 0.52, "xgb": 0.41, "meta": 0.44},
                "reasons": ["cross_review"],
                "decision_trace": {
                    "cross_review_gate": {"passed": False},
                    "financial_gate": {"allowed": False},
                    "liquidity_gate": {"passed": False},
                    "risk_gate": {"passed": False},
                },
            },
        ],
        audit_events=[
            {
                "event_type": "pipeline_run",
                "payload": {
                    "portfolio_update": {
                        "execution_attempts": {
                            "buy_signals": 2,
                            "buy_new_attempted": 2,
                            "buy_new_filled": 1,
                            "buy_new_rejected": 1,
                            "pre_trade_blocked": 1,
                        },
                        "executions": [
                            {
                                "symbol": "600000",
                                "status": "opened",
                                "reason": "auto_simulated_buy",
                                "realized_return_pct": 0.031,
                            },
                            {
                                "symbol": "000001",
                                "status": "rejected_no_cash",
                                "block_category": "pre_trade_blocked",
                                "reason": "auto_simulated_buy_no_cash",
                            },
                        ],
                    }
                },
            }
        ],
    )

    funnel = report["signal_loss_funnel"]
    assert funnel["signal_stages"]["raw_candidates"]["count"] == 2
    assert funnel["signal_stages"]["actionable_buy"]["count"] == 1
    assert funnel["signal_stages"]["cross_review_pass"]["count"] == 1
    assert funnel["execution_stages"]["buy_new_filled"] == 1
    assert funnel["execution_stages"]["pre_trade_blocked"] == 1
    assert funnel["loss_counts"]["cross_review_block"] == 1
    assert funnel["execution_reason_breakdown"]["auto_simulated_buy_no_cash"] == 1
    assert funnel["outcome_observation"]["profitable_count"] == 1
    ledger = funnel["symbol_ledger"]
    by_symbol = {item["symbol"]: item for item in ledger["items"]}
    assert by_symbol["600000"]["stage"] == "filled"
    assert by_symbol["600000"]["outcome_status"]["profitable"] is True
    assert by_symbol["000001"]["stage"] == "pre_trade_blocked"
    assert "pre_trade_blocked" in by_symbol["000001"]["blockers"]


def test_signal_quality_auditor_keeps_advisory_attempts_out_of_execution_stages() -> None:
    auditor = SignalQualityAuditor(config=_load_config())

    report = auditor.build_report(
        latest_signals=[],
        audit_events=[
            {
                "event_type": "pipeline_run",
                "payload": {
                    "execution_mode": "advisory_only",
                    "portfolio_update": {
                        "execution_attempts": {
                            "signals": 3,
                            "buy_signals": 2,
                            "buy_new_attempted": 0,
                            "pre_trade_blocked": 0,
                        },
                        "executions": [],
                    },
                },
            }
        ],
    )

    funnel = report["signal_loss_funnel"]
    assert funnel["execution_attempts"] == {}
    assert funnel["execution_stages"]["buy_signals"] == 0
    assert funnel["execution_stages"]["buy_new_attempted"] == 0
    assert funnel["advisory_attempts"]["signals"] == 3
    assert funnel["advisory_attempts"]["buy_signals"] == 2


def test_signal_quality_auditor_keeps_dry_run_attempts_out_of_execution_stages() -> None:
    auditor = SignalQualityAuditor(config=_load_config())

    report = auditor.build_report(
        latest_signals=[],
        audit_events=[
            {
                "event_type": "pipeline_run",
                "payload": {
                    "execution_mode": "portfolio_auto_apply_dry_run",
                    "dry_run_execution": True,
                    "portfolio_update": {
                        "dry_run": True,
                        "execution_attempts": {
                            "buy_signals": 2,
                            "buy_new_attempted": 2,
                            "buy_new_filled": 1,
                        },
                        "executions": [
                            {
                                "symbol": "600000",
                                "status": "opened",
                                "reason": "auto_simulated_buy",
                                "realized_return_pct": 0.03,
                            }
                        ],
                    },
                },
            }
        ],
    )

    funnel = report["signal_loss_funnel"]
    assert funnel["execution_attempts"] == {}
    assert funnel["execution_stages"]["buy_signals"] == 0
    assert funnel["execution_stages"]["buy_new_attempted"] == 0
    assert funnel["execution_stages"]["buy_new_filled"] == 0
    assert funnel["dry_run_attempts"]["buy_signals"] == 2
    assert funnel["dry_run_attempts"]["buy_new_filled"] == 1
    assert funnel["execution_status_breakdown"] == {}
    assert "portfolio_execution_records_missing" in funnel["data_gaps"]


def test_signal_quality_auditor_symbol_ledger_uses_block_category_for_risk_gate() -> None:
    auditor = SignalQualityAuditor(config=_load_config())

    report = auditor.build_report(
        latest_signals=[
            {
                "symbol": "001258",
                "strategy": "trend",
                "score": 68.0,
                "grade": "A",
                "action": "buy",
                "target_position": 0.15,
                "probabilities": {"lgbm": 0.71, "xgb": 0.63, "meta": 0.62},
                "decision_trace": {
                    "cross_review_gate": {"passed": True},
                    "financial_gate": {"allowed": True},
                    "liquidity_gate": {"passed": True},
                    "risk_gate": {"passed": True},
                },
            }
        ],
        audit_events=[
            {
                "event_type": "pipeline_run",
                "payload": {
                    "portfolio_update": {
                        "execution_attempts": {
                            "buy_signals": 1,
                            "buy_new_attempted": 1,
                            "buy_new_rejected": 1,
                            "risk_gate_blocked": 1,
                        },
                        "executions": [
                            {
                                "symbol": "001258",
                                "status": "rejected_same_sector",
                                "block_category": "risk_gate_blocked",
                                "reason": "auto_simulated_buy_same_sector",
                            }
                        ],
                    }
                },
            }
        ],
    )

    funnel = report["signal_loss_funnel"]
    assert funnel["execution_stages"]["risk_gate_blocked"] == 1
    item = funnel["symbol_ledger"]["items"][0]
    assert item["symbol"] == "001258"
    assert item["stage"] == "risk_gate_blocked"
    assert item["block_categories"] == ["risk_gate_blocked"]
    assert "risk_gate_blocked" in item["blockers"]


def test_signal_quality_auditor_dedupes_pipeline_and_blocked_audit_events() -> None:
    auditor = SignalQualityAuditor(config=_load_config())

    report = auditor.build_report(
        latest_signals=[],
        audit_events=[
            {
                "event_type": "pipeline_run",
                "payload": {
                    "portfolio_update": {
                        "execution_attempts": {
                            "buy_new_rejected": 1,
                            "pre_trade_blocked": 1,
                        },
                        "executions": [
                            {
                                "symbol": "000001",
                                "status": "rejected_no_cash",
                                "block_category": "pre_trade_blocked",
                                "reason": "auto_simulated_buy_no_cash",
                            }
                        ],
                    }
                },
            },
            {
                "event_type": "pre_trade_blocked",
                "payload": {
                    "symbol": "000001",
                    "reason": "auto_simulated_buy_no_cash",
                    "quantity": 0,
                    "target_position": 0.12,
                },
            },
        ],
    )

    funnel = report["signal_loss_funnel"]
    assert funnel["execution_stages"]["pre_trade_blocked"] == 1
    assert funnel["execution_reason_breakdown"]["auto_simulated_buy_no_cash"] == 1
    assert funnel["symbol_ledger"]["records"] == 1


def test_signal_quality_auditor_dedupes_aggregate_attempt_and_blocked_audit_event() -> None:
    auditor = SignalQualityAuditor(config=_load_config())

    report = auditor.build_report(
        latest_signals=[],
        audit_events=[
            {
                "event_type": "pipeline_run",
                "payload": {
                    "portfolio_update": {
                        "execution_attempts": {
                            "buy_new_rejected": 1,
                            "pre_trade_blocked": 1,
                        },
                        "executions": [],
                    }
                },
            },
            {
                "event_type": "pre_trade_blocked",
                "payload": {
                    "symbol": "000001",
                    "reason": "auto_simulated_buy_no_cash",
                    "quantity": 0,
                    "target_position": 0.12,
                },
            },
        ],
    )

    funnel = report["signal_loss_funnel"]
    assert funnel["execution_stages"]["pre_trade_blocked"] == 1
    assert funnel["execution_reason_breakdown"]["auto_simulated_buy_no_cash"] == 1
    assert funnel["symbol_ledger"]["items"][0]["stage"] == "pre_trade_blocked"


def test_signal_quality_auditor_dedupes_aggregate_risk_gate_and_blocked_event() -> None:
    auditor = SignalQualityAuditor(config=_load_config())

    report = auditor.build_report(
        latest_signals=[],
        audit_events=[
            {
                "event_type": "pipeline_run",
                "payload": {
                    "portfolio_update": {
                        "execution_attempts": {
                            "buy_new_rejected": 1,
                            "risk_gate_blocked": 1,
                        },
                        "executions": [],
                    }
                },
            },
            {
                "event_type": "risk_gate_blocked",
                "payload": {
                    "symbol": "001258",
                    "reason": "auto_simulated_buy_same_sector",
                    "quantity": 100,
                    "target_position": 0.12,
                },
            },
        ],
    )

    funnel = report["signal_loss_funnel"]
    assert funnel["execution_stages"]["risk_gate_blocked"] == 1
    assert funnel["execution_reason_breakdown"]["auto_simulated_buy_same_sector"] == 1
    assert funnel["symbol_ledger"]["items"][0]["stage"] == "risk_gate_blocked"


def test_signal_quality_auditor_dedupes_legacy_rejected_and_blocked_audit_events() -> None:
    auditor = SignalQualityAuditor(config=_load_config())

    report = auditor.build_report(
        latest_signals=[],
        audit_events=[
            {
                "event_type": "pipeline_run",
                "payload": {
                    "portfolio_update": {
                        "execution_attempts": {"buy_new_rejected": 1},
                        "executions": [
                            {
                                "symbol": "000001",
                                "status": "rejected_no_cash",
                                "reason": "auto_simulated_buy_no_cash",
                            }
                        ],
                    }
                },
            },
            {
                "event_type": "pre_trade_blocked",
                "payload": {
                    "symbol": "000001",
                    "reason": "auto_simulated_buy_no_cash",
                },
            },
        ],
    )

    funnel = report["signal_loss_funnel"]
    assert funnel["execution_stages"]["pre_trade_blocked"] == 0
    assert funnel["execution_stages"]["buy_new_rejected"] == 1
    assert funnel["execution_reason_breakdown"]["auto_simulated_buy_no_cash"] == 1
    assert funnel["symbol_ledger"]["records"] == 1


def test_signal_quality_auditor_keeps_repeated_blocked_events_across_runs() -> None:
    auditor = SignalQualityAuditor(config=_load_config())

    report = auditor.build_report(
        latest_signals=[],
        audit_events=[
            {
                "event_type": "pre_trade_blocked",
                "trace_id": "run-1",
                "payload": {
                    "symbol": "000001",
                    "reason": "auto_simulated_buy_no_cash",
                    "quantity": 0,
                    "target_position": 0.1,
                },
            },
            {
                "event_type": "pre_trade_blocked",
                "trace_id": "run-2",
                "payload": {
                    "symbol": "000001",
                    "reason": "auto_simulated_buy_no_cash",
                    "quantity": 0,
                    "target_position": 0.1,
                },
            },
        ],
    )

    funnel = report["signal_loss_funnel"]
    assert funnel["execution_stages"]["pre_trade_blocked"] == 2
    assert funnel["execution_reason_breakdown"]["auto_simulated_buy_no_cash"] == 2
    assert funnel["symbol_ledger"]["items"][0]["execution_statuses"] == [
        "pre_trade_blocked",
        "pre_trade_blocked",
    ]


def test_signal_quality_auditor_marks_buy_stage_missing_execution_evidence() -> None:
    auditor = SignalQualityAuditor(config=_load_config())

    report = auditor.build_report(
        latest_signals=[
            {
                "symbol": "600000",
                "strategy": "trend",
                "score": 70.0,
                "grade": "A",
                "action": "buy",
                "target_position": 0.1,
                "probabilities": {"lgbm": 0.7, "xgb": 0.62, "meta": 0.61},
                "decision_trace": {
                    "cross_review_gate": {"passed": True},
                    "financial_gate": {"allowed": True},
                    "liquidity_gate": {"passed": True},
                    "risk_gate": {"passed": True},
                },
            }
        ],
        audit_events=[],
    )

    item = report["signal_loss_funnel"]["symbol_ledger"]["items"][0]
    assert item["stage"] == "buy_execution_evidence_missing"
    assert "pipeline_audit_events_missing" in report["signal_loss_funnel"]["data_gaps"]


def test_signal_quality_auditor_blocked_only_event_counts_as_execution_record() -> None:
    auditor = SignalQualityAuditor(config=_load_config())

    report = auditor.build_report(
        latest_signals=[],
        audit_events=[
            {
                "event_type": "pre_trade_blocked",
                "payload": {
                    "symbol": "000001",
                    "reason": "auto_simulated_buy_no_cash",
                    "quantity": 0,
                    "target_position": 0.1,
                },
            }
        ],
    )

    funnel = report["signal_loss_funnel"]
    assert funnel["execution_stages"]["pre_trade_blocked"] == 1
    assert funnel["symbol_ledger"]["items"][0]["stage"] == "pre_trade_blocked"
    assert "portfolio_execution_records_missing" not in funnel["data_gaps"]
