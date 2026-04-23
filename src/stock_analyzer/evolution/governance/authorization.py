"""Authorization level classification and approval guards."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from stock_analyzer._pydantic_compat import BaseModel, ConfigDict, Field
from stock_analyzer.evolution.governance.proposal import ProposalArtifact

_A_LEVEL_KEYWORDS = {
    "stop_loss",
    "position",
    "new_factor",
    "label",
    "hmm",
    "eval_protocol",
    "backtestmatcher",
    "embargo",
    "fdr",
    "bootstrap",
}
_B_LEVEL_KEYWORDS = {"blacklist", "failure_mode"}
_C_LEVEL_PREFIXES = ("alert_threshold_", "observation_queue_", "dashboard_display_")
_C_LEVEL_EXACT = {"log_verbosity"}


class AuthorizationLevel(StrEnum):
    """Governance authorization level."""

    A = "A"
    B = "B"
    C = "C"


class AuthorizationDecision(BaseModel):
    """Classification output for a proposal's change surface."""

    model_config = ConfigDict(extra="forbid")

    level: AuthorizationLevel
    auto_approved: bool
    fallback_applied: bool = False
    matched_rules: list[str] = Field(default_factory=list)


class CodeCommitMismatchError(RuntimeError):
    """Raised when proposal commit id differs from active runtime commit."""


def determine_authorization(change_keys: Sequence[str]) -> AuthorizationDecision:
    """Determine authorization level with strict fallback.

    Args:
        change_keys: Names of changed controls/parameters in the proposal payload.

    Returns:
        Authorization classification with fallback signal.
    """
    normalized = [item.strip().lower() for item in change_keys if item.strip()]
    if not normalized:
        return AuthorizationDecision(
            level=AuthorizationLevel.A,
            auto_approved=False,
            fallback_applied=True,
            matched_rules=["empty_change_set"],
        )

    max_level = AuthorizationLevel.C
    matched_rules: list[str] = []

    for key in normalized:
        level = _classify_single_key(key=key)
        if level is None:
            return AuthorizationDecision(
                level=AuthorizationLevel.A,
                auto_approved=False,
                fallback_applied=True,
                matched_rules=[f"unknown:{key}"],
            )
        matched_rules.append(f"{key}:{level.value}")
        if _severity(level) > _severity(max_level):
            max_level = level

    return AuthorizationDecision(
        level=max_level,
        auto_approved=(max_level == AuthorizationLevel.C),
        fallback_applied=False,
        matched_rules=matched_rules,
    )


def authorize_proposal(
    proposal: ProposalArtifact,
    change_keys: Sequence[str],
    active_code_commit_id: str,
) -> AuthorizationDecision:
    """Classify authorization level and enforce approval-time commit validation.

    Args:
        proposal: Proposal artifact under review.
        change_keys: Proposal change-set keys.
        active_code_commit_id: Running runtime code commit id.

    Returns:
        Authorization classification for this proposal.

    Raises:
        CodeCommitMismatchError: If code commit ids do not match.
    """
    if not proposal.is_commit_consistent(active_code_commit_id=active_code_commit_id):
        raise CodeCommitMismatchError(
            "proposal.code_commit_id does not match active runtime commit id"
        )
    return determine_authorization(change_keys=change_keys)


def _classify_single_key(key: str) -> AuthorizationLevel | None:
    if any(keyword in key for keyword in _A_LEVEL_KEYWORDS):
        return AuthorizationLevel.A
    if any(keyword in key for keyword in _B_LEVEL_KEYWORDS):
        return AuthorizationLevel.B
    if key in _C_LEVEL_EXACT or key.startswith(_C_LEVEL_PREFIXES):
        return AuthorizationLevel.C
    return None


def _severity(level: AuthorizationLevel) -> int:
    if level == AuthorizationLevel.A:
        return 3
    if level == AuthorizationLevel.B:
        return 2
    return 1
