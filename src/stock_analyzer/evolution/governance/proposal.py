"""Proposal artifact models."""

from __future__ import annotations

from datetime import UTC, datetime

from stock_analyzer._pydantic_compat import BaseModel, ConfigDict, Field, field_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SummaryWindow(_StrictModel):
    """Window metadata for the user-facing summary."""

    oos_days: int = Field(ge=1)
    shadow_days: int = Field(ge=0)


class UserFacingSummary(_StrictModel):
    """Readable proposal summary for human approval."""

    pnl_diff: str
    risk_diff: str
    ir_score: float
    turnover_change: str
    avg_trades_per_day: float
    key_reason: str
    summary_window: SummaryWindow
    baseline: str


class ProposalArtifact(_StrictModel):
    """Immutable proposal package for governance review."""

    proposal_id: str
    data_snapshot_id: str
    code_commit_id: str
    random_seed: dict[str, int] = Field(default_factory=dict)
    eval_protocol_id: str
    llm_prompt_version: str | None = None
    payload_uri: str
    payload_sha256: str
    payload_diff_summary: str
    user_facing_summary: UserFacingSummary
    emergency_fix: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("payload_uri")
    @classmethod
    def _validate_payload_uri(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if not normalized.startswith("suggestions/"):
            raise ValueError("payload_uri must be under suggestions/ sandbox")
        return normalized

    def is_commit_consistent(self, active_code_commit_id: str) -> bool:
        """Check whether proposal commit id matches the running commit id."""
        return self.code_commit_id == active_code_commit_id

    def assert_commit_consistent(self, active_code_commit_id: str) -> None:
        """Raise if proposal commit id mismatches active runtime commit id."""
        if not self.is_commit_consistent(active_code_commit_id=active_code_commit_id):
            raise ValueError(
                "code_commit_id mismatch: proposal artifact should be regenerated "
                "against the currently running code"
            )
