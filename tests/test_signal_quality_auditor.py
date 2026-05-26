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
        report["execution_risk_context"]["reason_counts"][
            "execution_risk_artifact_unavailable"
        ]
        == 1
    )
    assert any(
        item["code"] == "train_execution_risk_artifact"
        for item in report["recommended_next_actions"]
    )
