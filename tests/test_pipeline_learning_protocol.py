from __future__ import annotations

from pathlib import Path

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.learning.feedback_features import (
    LEARNING_PROTOCOL_FEEDBACK_FEATURE_COLUMNS,
)
from stock_analyzer.learning.feature_schema_registry import FeatureSchemaRegistry
from stock_analyzer.learning.label_policy_registry import LabelPolicyRegistry
from stock_analyzer.learning.sample_schema import MaturityStatus
from stock_analyzer.learning.sample_store import SampleStore
from stock_analyzer.pipeline import AnalyzerPipeline
from tests.test_pipeline import MinimalBarsProvider


def _load_default_config() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    return load_config(root / "config" / "default.yaml")


def test_pipeline_persists_learning_snapshot_when_protocol_is_configured(tmp_path: Path) -> None:
    config = _load_default_config()
    feature_registry = FeatureSchemaRegistry(db_path=tmp_path / "feature_schema.duckdb")
    label_registry = LabelPolicyRegistry(db_path=tmp_path / "label_policy.duckdb")
    sample_store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    pipeline = AnalyzerPipeline(
        config=config,
        provider=MinimalBarsProvider(),
        sample_store=sample_store,
        feature_schema_registry=feature_registry,
        label_policy_registry=label_registry,
    )

    report = pipeline.run_once(symbols=["600000"], strategy="trend", current_equity=1.0)

    assert len(report.signals) == 1
    counts = sample_store.counts()
    assert counts["signal_snapshots"] == 1
    assert counts["outcome_records"] == 1
    assert counts["dataset_manifests"] == 0
    registered_features = feature_registry.list_records()
    registered_labels = label_registry.list_records()
    assert len(registered_features) == 1
    assert len(registered_labels) == 1
    snapshot_ids = sample_store.list_snapshot_ids()
    assert len(snapshot_ids) == 1
    snapshot_row = sample_store.get_snapshot(snapshot_ids[0])
    assert snapshot_row is not None
    assert snapshot_row.symbol == "600000"
    assert snapshot_row.strategy == "trend"
    assert snapshot_row.feature_schema_id == registered_features[0].feature_schema_id
    assert snapshot_row.label_policy_id == registered_labels[0].label_policy_id
    assert snapshot_row.watchlist_source == "pipeline_run_once"
    assert registered_features[0].feature_names[-len(LEARNING_PROTOCOL_FEEDBACK_FEATURE_COLUMNS) :] == list(
        LEARNING_PROTOCOL_FEEDBACK_FEATURE_COLUMNS
    )
    assert snapshot_row.feature_vector["lp_m1_negative_case_applied"] == 0.0
    assert snapshot_row.feature_vector["lp_m3_match_score"] == 0.0
    assert snapshot_row.feature_vector["lp_m7_effectiveness_score"] == 0.0
    outcome_row = sample_store.get_outcome(snapshot_ids[0])
    assert outcome_row is not None
    assert outcome_row.snapshot_id == snapshot_row.snapshot_id
    assert outcome_row.maturity_status == MaturityStatus.PENDING
    learning_protocol = report.signals[0].decision_trace.get("learning_protocol", {})
    assert learning_protocol["snapshot_id"] == snapshot_row.snapshot_id
