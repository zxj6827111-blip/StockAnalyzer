from __future__ import annotations

from datetime import UTC, datetime

from stock_analyzer.learning.feedback_weighting import (
    build_feedback_weight,
    summarize_feedback_weights,
)
from stock_analyzer.learning.sample_schema import OutcomeRecord, SignalSnapshot


def test_feedback_weight_combines_m1_m3_m7_signals() -> None:
    snapshot = SignalSnapshot(
        snapshot_id="snap-001",
        code_version="git:test",
        symbol="600000.SH",
        strategy="trend",
        decision_time=datetime(2026, 3, 26, 14, 50, tzinfo=UTC),
        feature_vector={"ret_1d": 0.01, "atr14": 0.4},
        feature_schema_id="feature_schema_v1_demo",
        feature_schema_hash="feature_hash_demo",
        runtime_config_hash="runtime_hash_demo",
        label_policy_id="label_policy_v1_demo",
        label_policy_hash="label_hash_demo",
        sample_weight=1.2,
        risk_context={
            "m1_negative_case_bucket": "severe",
            "m1_reason_codes": ["chase_high"],
        },
        regime_context={"m3_match_score": 0.8},
        news_context={
            "news_component": 0.9,
            "m7_effectiveness_score": 0.75,
            "m7_source_reliability": 0.8,
        },
    )
    outcome = OutcomeRecord(
        snapshot_id="snap-001",
        realized_return=-0.12,
        max_adverse_excursion=-0.15,
    )

    result = build_feedback_weight(snapshot=snapshot, outcome=outcome)

    assert result.base_weight == 1.2
    assert result.module_weights["M1"] > 1.0
    assert result.module_weights["M3"] > 1.0
    assert result.module_weights["M7"] > 1.0
    assert result.final_weight > result.base_weight
    assert "m1_bucket:severe" in result.reason_codes
    assert "m3_pattern_memory" in result.reason_codes
    assert "m7_effectiveness" in result.reason_codes


def test_feedback_weight_summary_counts_nontrivial_rows() -> None:
    neutral_snapshot = SignalSnapshot(
        snapshot_id="snap-plain",
        code_version="git:test",
        symbol="000001.SZ",
        strategy="trend",
        decision_time=datetime(2026, 3, 26, 14, 51, tzinfo=UTC),
        feature_vector={"ret_1d": 0.01},
        feature_schema_id="feature_schema_v1_demo",
        feature_schema_hash="feature_hash_demo",
        runtime_config_hash="runtime_hash_demo",
        label_policy_id="label_policy_v1_demo",
        label_policy_hash="label_hash_demo",
    )
    weighted_snapshot = neutral_snapshot.model_copy(
        update={
            "snapshot_id": "snap-weighted",
            "news_context": {"news_component": 0.95},
            "regime_context": {"m3_match_score": 0.6},
        }
    )
    outcome = OutcomeRecord(snapshot_id="snap-plain", realized_return=0.03)
    weighted_outcome = OutcomeRecord(
        snapshot_id="snap-weighted",
        realized_return=-0.08,
        max_adverse_excursion=-0.09,
    )

    summary = summarize_feedback_weights(
        [
            build_feedback_weight(snapshot=neutral_snapshot, outcome=outcome),
            build_feedback_weight(snapshot=weighted_snapshot, outcome=weighted_outcome),
        ]
    )

    assert summary.row_count == 2
    assert summary.applied_rows >= 1
    assert summary.max_weight >= summary.min_weight
    assert summary.module_active_rows["M3"] >= 1
    assert summary.module_active_rows["M7"] >= 1
