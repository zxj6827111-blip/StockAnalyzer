"""Learning protocol primitives for sample-store driven training."""

from stock_analyzer.learning.backfill import LearningBackfillEngine
from stock_analyzer.learning.dataset_manifest import DatasetManifestBuilder
from stock_analyzer.learning.execution_risk_labels import (
    ExecutionRiskDataset,
    ExecutionRiskLabeledRow,
    ExecutionRiskLabelBuilder,
    ExecutionRiskLabelingConfig,
    ExecutionRiskTarget,
)
from stock_analyzer.learning.feedback_weighting import (
    FeedbackWeightResult,
    FeedbackWeightSummary,
    build_feedback_weight,
    summarize_feedback_weights,
)
from stock_analyzer.learning.feature_schema_registry import (
    FeatureSchemaRecord,
    FeatureSchemaRegistry,
    build_feature_schema_record,
    project_frame_to_schema,
)
from stock_analyzer.learning.label_policy_registry import (
    LabelPolicyRecord,
    LabelPolicyRegistry,
    build_label_policy_record,
)
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    DatasetManifest,
    DatasetManifestItem,
    DatasetSplitPlanEntry,
    FeatureCaptureMode,
    MaturityStatus,
    OutcomeRecord,
    SignalSnapshot,
)
from stock_analyzer.learning.sample_store import SampleStore

__all__ = [
    "BackfillFidelityTier",
    "DatasetManifest",
    "DatasetManifestBuilder",
    "DatasetManifestItem",
    "DatasetSplitPlanEntry",
    "ExecutionRiskDataset",
    "ExecutionRiskLabeledRow",
    "ExecutionRiskLabelBuilder",
    "ExecutionRiskLabelingConfig",
    "ExecutionRiskTarget",
    "FeedbackWeightResult",
    "FeedbackWeightSummary",
    "FeatureSchemaRecord",
    "FeatureSchemaRegistry",
    "FeatureCaptureMode",
    "LabelPolicyRecord",
    "LabelPolicyRegistry",
    "LearningBackfillEngine",
    "MaturityStatus",
    "OutcomeRecord",
    "SampleStore",
    "SignalSnapshot",
    "build_feedback_weight",
    "build_feature_schema_record",
    "build_label_policy_record",
    "project_frame_to_schema",
    "summarize_feedback_weights",
]
