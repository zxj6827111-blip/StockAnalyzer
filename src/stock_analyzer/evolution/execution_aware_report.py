"""Execution-aware reranking report built from shadow comparisons and risk sidecars."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from stock_analyzer.evolution.champion_shadow_report import ChampionShadowReportBuilder
from stock_analyzer.evolution.execution_aware_scoring import (
    execution_aware_score,
    is_high_execution_risk,
    normalize_execution_model_outputs,
    normalize_execution_risk_payload,
)
from stock_analyzer.learning.execution_risk_labels import (
    build_execution_risk_feature_vector,
)
from stock_analyzer.learning.feature_schema_registry import FeatureSchemaRegistry
from stock_analyzer.learning.label_policy_registry import LabelPolicyRegistry
from stock_analyzer.learning.sample_store import SampleStore
from stock_analyzer.models.execution_risk_artifact import ExecutionRiskArtifact
from stock_analyzer.models.execution_risk_predictor import ExecutionRiskPredictor
from stock_analyzer.models.registry import ModelRegistry


@dataclass(slots=True)
class ExecutionAwareReportRow:
    """One reranked execution-aware comparison row."""

    snapshot_id: str
    symbol: str
    trade_date: str
    split_name: str
    label: float
    champion_probability: float
    shadow_probability: float
    champion_execution_score: float
    shadow_execution_score: float
    champion_high_risk: bool
    shadow_high_risk: bool
    champion_risk: dict[str, float] = field(default_factory=dict)
    shadow_risk: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "symbol": self.symbol,
            "trade_date": self.trade_date,
            "split_name": self.split_name,
            "label": self.label,
            "champion_probability": self.champion_probability,
            "shadow_probability": self.shadow_probability,
            "champion_execution_score": self.champion_execution_score,
            "shadow_execution_score": self.shadow_execution_score,
            "champion_high_risk": self.champion_high_risk,
            "shadow_high_risk": self.shadow_high_risk,
            "champion_risk": dict(self.champion_risk),
            "shadow_risk": dict(self.shadow_risk),
        }

    def preview_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "symbol": self.symbol,
            "trade_date": self.trade_date,
            "split_name": self.split_name,
            "champion_execution_score": self.champion_execution_score,
            "shadow_execution_score": self.shadow_execution_score,
            "shadow_high_risk": self.shadow_high_risk,
        }


@dataclass(slots=True)
class ExecutionAwareReport:
    """Execution-aware reranking summary over champion-vs-shadow rows."""

    report_id: str
    champion_model_id: str
    shadow_model_id: str
    dataset_manifest_id: str
    comparison_report_id: str
    execution_risk_artifact_path: str
    execution_risk_dataset_id: str
    row_count: int
    split_counts: dict[str, int]
    summary_metrics: dict[str, float]
    rows: list[ExecutionAwareReportRow] = field(default_factory=list)

    def to_dict(
        self,
        *,
        include_rows: bool = True,
        preview_limit: int = 5,
    ) -> dict[str, object]:
        payload = {
            "report_id": self.report_id,
            "champion_model_id": self.champion_model_id,
            "shadow_model_id": self.shadow_model_id,
            "dataset_manifest_id": self.dataset_manifest_id,
            "comparison_report_id": self.comparison_report_id,
            "execution_risk_artifact_path": self.execution_risk_artifact_path,
            "execution_risk_dataset_id": self.execution_risk_dataset_id,
            "row_count": self.row_count,
            "split_counts": dict(self.split_counts),
            "summary_metrics": dict(self.summary_metrics),
            "preview": [row.preview_dict() for row in self.rows[: max(1, int(preview_limit))]],
        }
        if include_rows:
            payload["rows"] = [row.to_dict() for row in self.rows]
        return payload


class ExecutionAwareReportBuilder:
    """Build execution-aware reranking reports from registered models."""

    def __init__(
        self,
        *,
        store: SampleStore,
        model_registry: ModelRegistry,
        feature_schema_registry: FeatureSchemaRegistry | None = None,
        label_policy_registry: LabelPolicyRegistry | None = None,
    ) -> None:
        self._store = store
        self._comparison_builder = ChampionShadowReportBuilder(
            store=store,
            model_registry=model_registry,
            feature_schema_registry=feature_schema_registry,
            label_policy_registry=label_policy_registry,
        )

    def build_report(
        self,
        *,
        shadow_model_id: str,
        execution_risk_artifact_path: str,
        champion_model_id: str = "",
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
    ) -> ExecutionAwareReport:
        artifact_path = str(execution_risk_artifact_path).strip()
        if not artifact_path:
            raise ValueError("execution_risk_artifact_path must not be empty")
        artifact = ExecutionRiskArtifact.load(artifact_path)
        predictor = ExecutionRiskPredictor.load(artifact_path)
        comparison = self._comparison_builder.build_report(
            shadow_model_id=shadow_model_id,
            champion_model_id=champion_model_id,
            split_names=split_names,
            max_rows=max_rows,
        )
        snapshots = {
            snapshot.snapshot_id: snapshot
            for snapshot in self._store.list_snapshots(
                snapshot_ids=[row.snapshot_id for row in comparison.rows]
            )
        }

        rows: list[ExecutionAwareReportRow] = []
        split_counts: dict[str, int] = {}
        champion_scores: list[float] = []
        shadow_scores: list[float] = []
        champion_high_risk: list[float] = []
        shadow_high_risk: list[float] = []
        champion_fill: list[float] = []
        shadow_fill: list[float] = []
        champion_slip: list[float] = []
        shadow_slip: list[float] = []
        champion_div: list[float] = []
        shadow_div: list[float] = []

        for item in comparison.rows:
            snapshot = snapshots.get(item.snapshot_id)
            if snapshot is None:
                raise ValueError(f"snapshot missing for execution-aware row: {item.snapshot_id}")
            champion_prob = float(item.champion_scores.get("p_meta", 0.5))
            shadow_prob = float(item.shadow_scores.get("p_meta", 0.5))
            champion_feature_vector = build_execution_risk_feature_vector(
                snapshot=snapshot,
                model_outputs=normalize_execution_model_outputs(item.champion_scores),
            )
            shadow_feature_vector = build_execution_risk_feature_vector(
                snapshot=snapshot,
                model_outputs=normalize_execution_model_outputs(item.shadow_scores),
            )
            champion_risk = predictor.predict_features(champion_feature_vector)
            shadow_risk = predictor.predict_features(shadow_feature_vector)
            champion_execution_score = execution_aware_score(
                base_probability=champion_prob,
                risk=champion_risk,
            )
            shadow_execution_score = execution_aware_score(
                base_probability=shadow_prob,
                risk=shadow_risk,
            )
            champion_is_high_risk = is_high_execution_risk(champion_risk)
            shadow_is_high_risk = is_high_execution_risk(shadow_risk)
            row = ExecutionAwareReportRow(
                snapshot_id=item.snapshot_id,
                symbol=item.symbol,
                trade_date=item.trade_date,
                split_name=item.split_name,
                label=item.label,
                champion_probability=round(champion_prob, 6),
                shadow_probability=round(shadow_prob, 6),
                champion_execution_score=round(champion_execution_score, 6),
                shadow_execution_score=round(shadow_execution_score, 6),
                champion_high_risk=champion_is_high_risk,
                shadow_high_risk=shadow_is_high_risk,
                champion_risk=normalize_execution_risk_payload(champion_risk),
                shadow_risk=normalize_execution_risk_payload(shadow_risk),
            )
            rows.append(row)
            split_counts[item.split_name] = split_counts.get(item.split_name, 0) + 1
            champion_scores.append(champion_execution_score)
            shadow_scores.append(shadow_execution_score)
            champion_high_risk.append(1.0 if champion_is_high_risk else 0.0)
            shadow_high_risk.append(1.0 if shadow_is_high_risk else 0.0)
            champion_fill.append(float(champion_risk.get("can_fill", 0.0)))
            shadow_fill.append(float(shadow_risk.get("can_fill", 0.0)))
            champion_slip.append(
                float(champion_risk.get("likely_slippage_high", 0.0))
            )
            shadow_slip.append(
                float(shadow_risk.get("likely_slippage_high", 0.0))
            )
            champion_div.append(
                float(champion_risk.get("sim_broker_divergence_risk", 0.0))
            )
            shadow_div.append(
                float(shadow_risk.get("sim_broker_divergence_risk", 0.0))
            )

        summary_metrics = {
            "champion_mean_execution_score": _safe_mean(champion_scores),
            "shadow_mean_execution_score": _safe_mean(shadow_scores),
            "shadow_minus_champion_execution_score": round(
                _safe_mean(shadow_scores) - _safe_mean(champion_scores),
                6,
            ),
            "champion_high_risk_ratio": _safe_mean(champion_high_risk),
            "shadow_high_risk_ratio": _safe_mean(shadow_high_risk),
            "champion_mean_can_fill": _safe_mean(champion_fill),
            "shadow_mean_can_fill": _safe_mean(shadow_fill),
            "champion_mean_slippage_risk": _safe_mean(champion_slip),
            "shadow_mean_slippage_risk": _safe_mean(shadow_slip),
            "champion_mean_divergence_risk": _safe_mean(champion_div),
            "shadow_mean_divergence_risk": _safe_mean(shadow_div),
        }
        report_id = _build_report_id(
            comparison_report_id=comparison.comparison_report_id,
            execution_risk_dataset_id=artifact.dataset_id,
            rows=rows,
        )
        return ExecutionAwareReport(
            report_id=report_id,
            champion_model_id=comparison.champion_model_id,
            shadow_model_id=comparison.shadow_model_id,
            dataset_manifest_id=comparison.dataset_manifest_id,
            comparison_report_id=comparison.comparison_report_id,
            execution_risk_artifact_path=artifact_path,
            execution_risk_dataset_id=artifact.dataset_id,
            row_count=len(rows),
            split_counts=split_counts,
            summary_metrics=summary_metrics,
            rows=rows,
        )

def _safe_mean(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return round(sum(float(item) for item in values) / len(values), 6)


def _build_report_id(
    *,
    comparison_report_id: str,
    execution_risk_dataset_id: str,
    rows: Sequence[ExecutionAwareReportRow],
) -> str:
    material = {
        "comparison_report_id": comparison_report_id,
        "execution_risk_dataset_id": execution_risk_dataset_id,
        "snapshot_ids": [row.snapshot_id for row in rows],
    }
    digest = hashlib.sha256(
        json.dumps(material, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"execution_aware_report_v1_{digest[:12]}"
