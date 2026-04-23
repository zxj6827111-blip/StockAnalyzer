from __future__ import annotations

import pytest
from pydantic import ValidationError

from stock_analyzer.evolution.governance.authorization import (
    AuthorizationLevel,
    CodeCommitMismatchError,
    authorize_proposal,
    determine_authorization,
)
from stock_analyzer.evolution.governance.proposal import ProposalArtifact


def _proposal(payload_uri: str = "suggestions/M2/hmm_params/prop_1.json") -> ProposalArtifact:
    return ProposalArtifact.model_validate(
        {
            "proposal_id": "prop_1",
            "data_snapshot_id": "snapshot_1",
            "code_commit_id": "git:abc123",
            "random_seed": {"optuna": 42},
            "eval_protocol_id": "v7.2",
            "llm_prompt_version": "classify_v3.1",
            "payload_uri": payload_uri,
            "payload_sha256": "deadbeef",
            "payload_diff_summary": "minor tuning",
            "user_facing_summary": {
                "pnl_diff": "+1.2%",
                "risk_diff": "-0.4%",
                "ir_score": 0.45,
                "turnover_change": "+12%",
                "avg_trades_per_day": 3.2,
                "key_reason": "regime adaptation",
                "summary_window": {"oos_days": 60, "shadow_days": 14},
                "baseline": "Champion_same_window_after_cost",
            },
        }
    )


def test_proposal_payload_uri_must_be_suggestions() -> None:
    with pytest.raises(ValidationError):
        _proposal(payload_uri="artifacts/unsafe.json")


def test_determine_authorization_c_level_auto_approved() -> None:
    decision = determine_authorization(["alert_threshold_volatility", "log_verbosity"])
    assert decision.level == AuthorizationLevel.C
    assert decision.auto_approved is True
    assert decision.fallback_applied is False


def test_determine_authorization_unknown_key_falls_back_to_a() -> None:
    decision = determine_authorization(["unmapped_parameter"])
    assert decision.level == AuthorizationLevel.A
    assert decision.fallback_applied is True


def test_authorize_proposal_validates_code_commit_id_consistency() -> None:
    proposal = _proposal()
    with pytest.raises(CodeCommitMismatchError):
        authorize_proposal(
            proposal=proposal,
            change_keys=["alert_threshold_volatility"],
            active_code_commit_id="git:different",
        )


def test_determine_authorization_b_level_for_blacklist_change() -> None:
    decision = determine_authorization(["blacklist_symbols"])
    assert decision.level == AuthorizationLevel.B
    assert decision.auto_approved is False
