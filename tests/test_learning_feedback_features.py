from __future__ import annotations

import pandas as pd

from stock_analyzer.learning.feedback_features import (
    LEARNING_PROTOCOL_FEEDBACK_FEATURE_COLUMNS,
    build_feedback_feature_vector,
    ensure_feedback_feature_frame,
    merge_feedback_feature_vector,
)


def test_build_feedback_feature_vector_extracts_stable_lp_columns() -> None:
    feature_vector = build_feedback_feature_vector(
        risk_context={
            "m1_negative_case_applied": True,
            "m1_negative_case_bucket": "severe",
            "m1_negative_case_similarity": 0.84,
            "m1_negative_case_penalty": 11.5,
            "m1_reason_codes": ["data_incomplete", "chase_high"],
        },
        regime_context={
            "m3_match_score": 0.91,
            "m3_passed_gates": 5,
            "m3_gate_total": 6,
        },
        news_context={
            "m7_effectiveness_score": 0.72,
            "m7_source_reliability": 0.88,
            "m7_mean_sentiment": 0.25,
            "m7_mean_confidence": 0.82,
            "m7_news_count": 3,
        },
    )

    assert feature_vector["lp_m1_negative_case_applied"] == 1.0
    assert feature_vector["lp_m1_negative_case_bucket_severe"] == 1.0
    assert feature_vector["lp_m1_negative_case_similarity"] == 0.84
    assert feature_vector["lp_m1_negative_case_penalty"] == 11.5
    assert feature_vector["lp_m1_negative_case_reason_count"] == 2.0
    assert feature_vector["lp_m3_match_score"] == 0.91
    assert feature_vector["lp_m3_gate_pass_ratio"] == 5 / 6
    assert feature_vector["lp_m7_effectiveness_score"] == 0.72
    assert feature_vector["lp_m7_source_reliability"] == 0.88
    assert feature_vector["lp_m7_mean_sentiment"] == 0.25
    assert feature_vector["lp_m7_mean_confidence"] == 0.82
    assert feature_vector["lp_m7_news_count"] == 3.0


def test_merge_feedback_feature_vector_respects_existing_contract_when_requested() -> None:
    merged = merge_feedback_feature_vector(
        {"feature_a": 1.0},
        risk_context={"m1_negative_case_applied": True},
        add_missing_columns=False,
    )

    assert merged == {"feature_a": 1.0}


def test_ensure_feedback_feature_frame_appends_lp_columns_in_stable_order() -> None:
    frame = ensure_feedback_feature_frame(pd.DataFrame({"feature_b": [2.0], "feature_a": [1.0]}))

    assert list(frame.columns) == [
        "feature_b",
        "feature_a",
        *LEARNING_PROTOCOL_FEEDBACK_FEATURE_COLUMNS,
    ]
    assert frame.iloc[0]["lp_m1_negative_case_applied"] == 0.0
