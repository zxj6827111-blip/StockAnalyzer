"""Strict sample-store schema objects for learning protocol persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from stock_analyzer._pydantic_compat import BaseModel, ConfigDict, Field, field_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FeatureCaptureMode(StrEnum):
    OBSERVED_SNAPSHOT = "observed_snapshot"
    REPLAYED_RECOMPUTE = "replayed_recompute"
    HYBRID = "hybrid"


class MaturityStatus(StrEnum):
    PENDING = "pending"
    LABEL_MATURED = "label_matured"
    RECONCILED = "reconciled"
    FULLY_MATURED = "fully_matured"


class BackfillFidelityTier(StrEnum):
    GOLD = "gold"
    SILVER = "silver"
    BRONZE = "bronze"


class DatasetSplitPlanEntry(_StrictModel):
    split_name: str
    selector: str = ""
    row_count: int = 0
    start_time: datetime | None = None
    end_time: datetime | None = None

    @field_validator("split_name")
    @classmethod
    def _validate_split_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("split_name must not be empty")
        return normalized


class DatasetManifestItem(_StrictModel):
    dataset_manifest_id: str
    snapshot_id: str
    split_name: str
    ordinal: int = 0
    decision_time: datetime

    @field_validator("dataset_manifest_id", "snapshot_id")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("required manifest item field must not be empty")
        return normalized

    @field_validator("split_name")
    @classmethod
    def _validate_split_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("split_name must not be empty")
        return normalized

    @field_validator("ordinal")
    @classmethod
    def _validate_ordinal(cls, value: int) -> int:
        parsed = int(value)
        if parsed < 0:
            raise ValueError("ordinal must be >= 0")
        return parsed


class SignalSnapshot(_StrictModel):
    snapshot_id: str
    schema_version: str = "1"
    code_version: str
    symbol: str
    strategy: str
    decision_time: datetime
    feature_vector: dict[str, float]
    feature_schema_id: str
    feature_schema_hash: str
    feature_capture_mode: FeatureCaptureMode = FeatureCaptureMode.OBSERVED_SNAPSHOT
    feature_observed_at: datetime | None = None
    model_outputs: dict[str, float] = Field(default_factory=dict)
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    risk_context: dict[str, Any] = Field(default_factory=dict)
    news_context: dict[str, Any] = Field(default_factory=dict)
    regime_context: dict[str, Any] = Field(default_factory=dict)
    watchlist_source: str = ""
    data_quality_score: float | None = None
    sample_weight: float = 1.0
    runtime_config_hash: str
    label_policy_id: str
    label_policy_hash: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator(
        "snapshot_id",
        "code_version",
        "symbol",
        "strategy",
        "feature_schema_id",
        "feature_schema_hash",
        "runtime_config_hash",
        "label_policy_id",
        "label_policy_hash",
    )
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("required text field must not be empty")
        return normalized

    @field_validator("feature_vector")
    @classmethod
    def _validate_feature_vector(cls, value: dict[str, float]) -> dict[str, float]:
        if not value:
            raise ValueError("feature_vector must not be empty")
        normalized: dict[str, float] = {}
        for key, item in value.items():
            text = str(key).strip()
            if not text:
                raise ValueError("feature_vector keys must not be empty")
            normalized[text] = float(item)
        return normalized

    @field_validator("data_quality_score")
    @classmethod
    def _validate_data_quality_score(cls, value: float | None) -> float | None:
        if value is None:
            return None
        parsed = float(value)
        if not 0.0 <= parsed <= 1.0:
            raise ValueError("data_quality_score must be between 0 and 1")
        return parsed

    @field_validator("sample_weight")
    @classmethod
    def _validate_sample_weight(cls, value: float) -> float:
        parsed = float(value)
        if parsed <= 0.0:
            raise ValueError("sample_weight must be > 0")
        return parsed


class OutcomeRecord(_StrictModel):
    snapshot_id: str
    maturity_status: MaturityStatus = MaturityStatus.PENDING
    label_mature_time: datetime | None = None
    realized_return: float | None = None
    max_favorable_excursion: float | None = None
    max_adverse_excursion: float | None = None
    execution_fill_ratio: float | None = None
    realized_slippage_bp: float | None = None
    reconcile_status: str = ""
    sim_vs_broker_diff: float | None = None
    outcome_updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_backfill_at: datetime | None = None
    backfill_fidelity_tier: BackfillFidelityTier | None = None
    backfill_source: str = ""
    recomputed_feature_schema_id: str = ""

    @field_validator("snapshot_id")
    @classmethod
    def _validate_snapshot_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("snapshot_id must not be empty")
        return normalized

    @field_validator("execution_fill_ratio")
    @classmethod
    def _validate_execution_fill_ratio(cls, value: float | None) -> float | None:
        if value is None:
            return None
        parsed = float(value)
        if not 0.0 <= parsed <= 1.0:
            raise ValueError("execution_fill_ratio must be between 0 and 1")
        return parsed


class DatasetManifest(_StrictModel):
    dataset_manifest_id: str
    schema_version: str = "1"
    source_store_version: str
    feature_schema_id: str
    feature_schema_hash: str
    label_policy_id: str
    label_policy_hash: str
    sample_selection_rule: str
    time_window_start: datetime | None = None
    time_window_end: datetime | None = None
    fidelity_filter: list[BackfillFidelityTier] = Field(default_factory=list)
    included_snapshot_count: int = 0
    included_outcome_count: int = 0
    fidelity_breakdown: dict[str, int] = Field(default_factory=dict)
    dropped_reason_breakdown: dict[str, int] = Field(default_factory=dict)
    split_plan: list[DatasetSplitPlanEntry] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator(
        "dataset_manifest_id",
        "source_store_version",
        "feature_schema_id",
        "feature_schema_hash",
        "label_policy_id",
        "label_policy_hash",
        "sample_selection_rule",
    )
    @classmethod
    def _validate_manifest_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("required manifest field must not be empty")
        return normalized

    @field_validator("included_snapshot_count", "included_outcome_count")
    @classmethod
    def _validate_non_negative_counts(cls, value: int) -> int:
        parsed = int(value)
        if parsed < 0:
            raise ValueError("count fields must be >= 0")
        return parsed
