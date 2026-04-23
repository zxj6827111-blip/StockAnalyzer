"""Build protocol-bound shadow datasets from registered model artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from stock_analyzer.learning.feature_schema_registry import FeatureSchemaRegistry
from stock_analyzer.learning.label_policy_registry import LabelPolicyRecord, LabelPolicyRegistry
from stock_analyzer.learning.sample_schema import DatasetManifest, DatasetSplitPlanEntry
from stock_analyzer.learning.sample_store import SampleStore
from stock_analyzer.models.artifact import ModelArtifact
from stock_analyzer.models.predictor import SignalPredictor
from stock_analyzer.models.registry import ModelRegistry, ModelRegistryRecord
from stock_analyzer.models.trainer import _label_from_outcome


@dataclass(slots=True)
class ShadowDatasetRow:
    """One manifest-backed row projected into model feature space."""

    snapshot_id: str
    symbol: str
    strategy: str
    decision_time: datetime
    label_mature_time: datetime | None
    split_name: str
    ordinal: int
    label: float
    sample_weight: float
    data_quality_score: float | None
    maturity_status: str
    reconcile_status: str
    backfill_fidelity_tier: str
    backfill_source: str
    realized_return: float | None = None
    max_favorable_excursion: float | None = None
    max_adverse_excursion: float | None = None
    execution_fill_ratio: float | None = None
    realized_slippage_bp: float | None = None
    sim_vs_broker_diff: float | None = None
    baseline_scores: dict[str, float] = field(default_factory=dict)
    recorded_model_outputs: dict[str, float] = field(default_factory=dict)
    feature_vector: dict[str, float] = field(default_factory=dict)
    score_breakdown: dict[str, float] = field(default_factory=dict)
    risk_context: dict[str, object] = field(default_factory=dict)
    regime_context: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        payload = {
            "snapshot_id": self.snapshot_id,
            "symbol": self.symbol,
            "strategy": self.strategy,
            "trade_date": self.decision_time.date().isoformat(),
            "decision_time": self.decision_time.isoformat(),
            "label_mature_time": (
                self.label_mature_time.isoformat() if self.label_mature_time is not None else ""
            ),
            "split_name": self.split_name,
            "ordinal": self.ordinal,
            "label": self.label,
            "sample_weight": self.sample_weight,
            "data_quality_score": self.data_quality_score,
            "maturity_status": self.maturity_status,
            "reconcile_status": self.reconcile_status,
            "backfill_fidelity_tier": self.backfill_fidelity_tier,
            "backfill_source": self.backfill_source,
            "realized_return": self.realized_return,
            "max_favorable_excursion": self.max_favorable_excursion,
            "max_adverse_excursion": self.max_adverse_excursion,
            "execution_fill_ratio": self.execution_fill_ratio,
            "realized_slippage_bp": self.realized_slippage_bp,
            "sim_vs_broker_diff": self.sim_vs_broker_diff,
            "baseline_scores": dict(self.baseline_scores),
            "recorded_model_outputs": dict(self.recorded_model_outputs),
            "feature_vector": dict(self.feature_vector),
            "score_breakdown": dict(self.score_breakdown),
            "risk_context": dict(self.risk_context),
            "regime_context": dict(self.regime_context),
        }
        payload.update(dict(self.baseline_scores))
        return payload

    def preview_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "symbol": self.symbol,
            "trade_date": self.decision_time.date().isoformat(),
            "split_name": self.split_name,
            "ordinal": self.ordinal,
            "label": self.label,
            "p_meta": self.baseline_scores.get("p_meta"),
            "realized_return": self.realized_return,
            "sample_weight": self.sample_weight,
            "maturity_status": self.maturity_status,
        }


@dataclass(slots=True)
class ShadowDataset:
    """Stable shadow dataset with protocol bindings and audit metadata."""

    shadow_dataset_id: str
    model_id: str
    role: str
    lifecycle_state: str
    artifact_uri: str
    artifact_created_at: str
    dataset_manifest_id: str
    feature_schema_id: str
    feature_schema_hash: str
    label_policy_id: str
    label_policy_hash: str
    model_feature_columns: list[str]
    schema_feature_columns: list[str]
    requested_split_names: list[str]
    row_count: int
    split_counts: dict[str, int]
    predictor_mode: dict[str, object]
    manifest_split_plan: list[DatasetSplitPlanEntry] = field(default_factory=list)
    rows: list[ShadowDatasetRow] = field(default_factory=list)

    def to_dict(
        self,
        *,
        include_rows: bool = True,
        preview_limit: int = 5,
    ) -> dict[str, object]:
        payload = {
            "shadow_dataset_id": self.shadow_dataset_id,
            "model_id": self.model_id,
            "role": self.role,
            "lifecycle_state": self.lifecycle_state,
            "artifact_uri": self.artifact_uri,
            "artifact_created_at": self.artifact_created_at,
            "dataset_manifest_id": self.dataset_manifest_id,
            "feature_schema_id": self.feature_schema_id,
            "feature_schema_hash": self.feature_schema_hash,
            "label_policy_id": self.label_policy_id,
            "label_policy_hash": self.label_policy_hash,
            "model_feature_columns": list(self.model_feature_columns),
            "schema_feature_columns": list(self.schema_feature_columns),
            "requested_split_names": list(self.requested_split_names),
            "row_count": self.row_count,
            "split_counts": dict(self.split_counts),
            "predictor_mode": dict(self.predictor_mode),
            "manifest_split_plan": [
                entry.model_dump(mode="json") for entry in self.manifest_split_plan
            ],
            "preview": [row.preview_dict() for row in self.rows[: max(1, int(preview_limit))]],
        }
        if include_rows:
            payload["rows"] = [row.to_dict() for row in self.rows]
        return payload


class ShadowDatasetBuilder:
    """Build deterministic shadow datasets from one registered model id."""

    def __init__(
        self,
        *,
        store: SampleStore,
        model_registry: ModelRegistry,
        feature_schema_registry: FeatureSchemaRegistry | None = None,
        label_policy_registry: LabelPolicyRegistry | None = None,
    ) -> None:
        self._store = store
        self._model_registry = model_registry
        self._feature_schema_registry = feature_schema_registry
        self._label_policy_registry = label_policy_registry

    def build_for_model(
        self,
        *,
        model_id: str,
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
        include_baseline_scores: bool = True,
    ) -> ShadowDataset:
        normalized_model_id = str(model_id).strip()
        if not normalized_model_id:
            raise ValueError("model_id must not be empty")
        record = self._model_registry.get_by_id(normalized_model_id)
        if record is None:
            raise ValueError(f"model_id not found: {normalized_model_id}")
        return self.build_from_registry_record(
            registry_record=record,
            split_names=split_names,
            max_rows=max_rows,
            include_baseline_scores=include_baseline_scores,
        )

    def build_from_registry_record(
        self,
        *,
        registry_record: ModelRegistryRecord,
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
        include_baseline_scores: bool = True,
    ) -> ShadowDataset:
        artifact_path = Path(registry_record.artifact_uri).expanduser().resolve()
        artifact = ModelArtifact.load(artifact_path)
        manifest = self._resolve_manifest(registry_record=registry_record, artifact=artifact)
        self._validate_protocol_alignment(
            registry_record=registry_record,
            artifact=artifact,
            manifest=manifest,
        )
        label_policy = self._resolve_label_policy(manifest=manifest)
        schema_feature_columns = self._resolve_schema_feature_columns(manifest=manifest)
        predictor = (
            SignalPredictor.from_artifact(artifact, artifact_root=artifact_path.parent)
            if include_baseline_scores
            else None
        )

        normalized_splits = _normalize_split_names(split_names)
        manifest_items = self._store.list_manifest_items(manifest.dataset_manifest_id)
        if normalized_splits:
            allowed_splits = set(normalized_splits)
            manifest_items = [
                item for item in manifest_items if item.split_name in allowed_splits
            ]
        if not manifest_items:
            raise ValueError(
                "shadow dataset has no manifest items after filtering: "
                f"{manifest.dataset_manifest_id}"
            )

        resolved_max_rows = max(0, int(max_rows or 0))
        if resolved_max_rows > 0 and len(manifest_items) > resolved_max_rows:
            manifest_items = manifest_items[-resolved_max_rows:]

        snapshot_ids = [item.snapshot_id for item in manifest_items]
        snapshots = {
            snapshot.snapshot_id: snapshot
            for snapshot in self._store.list_snapshots(snapshot_ids=snapshot_ids)
        }
        outcomes = {
            outcome.snapshot_id: outcome
            for outcome in self._store.list_outcomes(snapshot_ids=snapshot_ids)
        }

        rows: list[ShadowDatasetRow] = []
        split_counts: dict[str, int] = {}
        for item in manifest_items:
            snapshot = snapshots.get(item.snapshot_id)
            if snapshot is None:
                raise ValueError(f"snapshot missing for manifest item: {item.snapshot_id}")
            outcome = outcomes.get(item.snapshot_id)
            if outcome is None:
                raise ValueError(f"outcome missing for manifest item: {item.snapshot_id}")

            label_value = _label_from_outcome(outcome=outcome, policy=label_policy)
            if label_value is None:
                raise ValueError(
                    "manifest row is not label-resolvable: "
                    f"{manifest.dataset_manifest_id}:{item.snapshot_id}"
                )

            projected_features = {
                column: float(snapshot.feature_vector.get(column, 0.0))
                for column in artifact.feature_columns
            }
            baseline_scores = (
                _normalize_probability_scores(
                    predictor.predict_row(pd.Series(projected_features))
                )
                if predictor is not None
                else {}
            )
            recorded_outputs = _normalize_probability_scores(snapshot.model_outputs)
            row = ShadowDatasetRow(
                snapshot_id=snapshot.snapshot_id,
                symbol=snapshot.symbol,
                strategy=snapshot.strategy,
                decision_time=snapshot.decision_time,
                label_mature_time=outcome.label_mature_time,
                split_name=item.split_name,
                ordinal=item.ordinal,
                label=float(label_value),
                sample_weight=float(snapshot.sample_weight),
                data_quality_score=snapshot.data_quality_score,
                maturity_status=outcome.maturity_status.value,
                reconcile_status=outcome.reconcile_status,
                backfill_fidelity_tier=(
                    outcome.backfill_fidelity_tier.value
                    if outcome.backfill_fidelity_tier is not None
                    else ""
                ),
                backfill_source=outcome.backfill_source,
                realized_return=outcome.realized_return,
                max_favorable_excursion=outcome.max_favorable_excursion,
                max_adverse_excursion=outcome.max_adverse_excursion,
                execution_fill_ratio=outcome.execution_fill_ratio,
                realized_slippage_bp=outcome.realized_slippage_bp,
                sim_vs_broker_diff=outcome.sim_vs_broker_diff,
                baseline_scores=baseline_scores,
                recorded_model_outputs=recorded_outputs,
                feature_vector=projected_features,
                score_breakdown=dict(snapshot.score_breakdown),
                risk_context=dict(snapshot.risk_context),
                regime_context=dict(snapshot.regime_context),
            )
            rows.append(row)
            split_counts[item.split_name] = split_counts.get(item.split_name, 0) + 1

        shadow_dataset_id = _build_shadow_dataset_id(
            model_id=registry_record.model_id,
            dataset_manifest_id=manifest.dataset_manifest_id,
            requested_split_names=normalized_splits,
            rows=rows,
        )
        predictor_mode = predictor.mode_details() if predictor is not None else {}
        return ShadowDataset(
            shadow_dataset_id=shadow_dataset_id,
            model_id=registry_record.model_id,
            role=registry_record.role.value,
            lifecycle_state=registry_record.lifecycle_state.value,
            artifact_uri=registry_record.artifact_uri,
            artifact_created_at=(
                registry_record.artifact_created_at.isoformat()
                if registry_record.artifact_created_at is not None
                else str(artifact.created_at).strip()
            ),
            dataset_manifest_id=manifest.dataset_manifest_id,
            feature_schema_id=manifest.feature_schema_id,
            feature_schema_hash=manifest.feature_schema_hash,
            label_policy_id=manifest.label_policy_id,
            label_policy_hash=manifest.label_policy_hash,
            model_feature_columns=list(artifact.feature_columns),
            schema_feature_columns=schema_feature_columns,
            requested_split_names=normalized_splits,
            row_count=len(rows),
            split_counts=split_counts,
            predictor_mode=predictor_mode,
            manifest_split_plan=list(manifest.split_plan),
            rows=rows,
        )

    def _resolve_manifest(
        self,
        *,
        registry_record: ModelRegistryRecord,
        artifact: ModelArtifact,
    ) -> DatasetManifest:
        manifest_id = str(artifact.dataset_manifest_id).strip() or str(
            registry_record.dataset_manifest_id
        ).strip()
        if not manifest_id:
            raise ValueError(
                "registered model has no dataset manifest binding: "
                f"{registry_record.model_id}"
            )
        manifest = self._store.get_manifest(manifest_id)
        if manifest is None:
            raise ValueError(f"dataset manifest not found: {manifest_id}")
        return manifest

    def _validate_protocol_alignment(
        self,
        *,
        registry_record: ModelRegistryRecord,
        artifact: ModelArtifact,
        manifest: DatasetManifest,
    ) -> None:
        comparisons = (
            (
                "dataset_manifest_id",
                str(registry_record.dataset_manifest_id).strip(),
                str(artifact.dataset_manifest_id).strip(),
                str(manifest.dataset_manifest_id).strip(),
            ),
            (
                "feature_schema_id",
                str(registry_record.feature_schema_id).strip(),
                str(artifact.feature_schema_id).strip(),
                str(manifest.feature_schema_id).strip(),
            ),
            (
                "feature_schema_hash",
                str(registry_record.feature_schema_hash).strip(),
                str(artifact.feature_schema_hash).strip(),
                str(manifest.feature_schema_hash).strip(),
            ),
            (
                "label_policy_id",
                str(registry_record.label_policy_id).strip(),
                str(artifact.label_policy_id).strip(),
                str(manifest.label_policy_id).strip(),
            ),
            (
                "label_policy_hash",
                str(registry_record.label_policy_hash).strip(),
                str(artifact.label_policy_hash).strip(),
                str(manifest.label_policy_hash).strip(),
            ),
        )
        for field_name, registry_value, artifact_value, manifest_value in comparisons:
            values = [value for value in (registry_value, artifact_value, manifest_value) if value]
            if not values:
                raise ValueError(f"protocol binding missing for field: {field_name}")
            if len(set(values)) != 1:
                raise ValueError(
                    "protocol binding mismatch for field "
                    f"{field_name}: registry={registry_value}, "
                    f"artifact={artifact_value}, manifest={manifest_value}"
                )

    def _resolve_label_policy(self, *, manifest: DatasetManifest) -> LabelPolicyRecord:
        if self._label_policy_registry is None:
            raise ValueError("label_policy_registry is required for shadow dataset building")
        record = self._label_policy_registry.get_by_id(manifest.label_policy_id)
        if record is None:
            raise ValueError(f"label policy not found: {manifest.label_policy_id}")
        if record.label_policy_hash != manifest.label_policy_hash:
            raise ValueError(
                "label policy hash mismatch for manifest: "
                f"{manifest.label_policy_id}"
            )
        return record

    def _resolve_schema_feature_columns(self, *, manifest: DatasetManifest) -> list[str]:
        if self._feature_schema_registry is None:
            return []
        record = self._feature_schema_registry.get_by_id(manifest.feature_schema_id)
        if record is None:
            raise ValueError(f"feature schema not found: {manifest.feature_schema_id}")
        if record.feature_schema_hash != manifest.feature_schema_hash:
            raise ValueError(
                "feature schema hash mismatch for manifest: "
                f"{manifest.feature_schema_id}"
            )
        return list(record.feature_names)


def _normalize_split_names(split_names: Sequence[str] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in split_names or ():
        text = str(item).strip().lower()
        if not text:
            continue
        if text == "validation":
            text = "calibration"
        if text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def _normalize_probability_scores(payload: dict[str, float] | None) -> dict[str, float]:
    if not isinstance(payload, dict):
        return {}
    normalized: dict[str, float] = {}
    alias_map = {
        "lgbm": "p_lgbm",
        "p_lgbm": "p_lgbm",
        "xgb": "p_xgb",
        "p_xgb": "p_xgb",
        "meta": "p_meta",
        "p_meta": "p_meta",
    }
    for key, value in payload.items():
        canonical = alias_map.get(str(key).strip().lower())
        if canonical is None:
            continue
        normalized[canonical] = max(0.0, min(1.0, float(value)))
    return normalized


def _build_shadow_dataset_id(
    *,
    model_id: str,
    dataset_manifest_id: str,
    requested_split_names: Sequence[str],
    rows: Sequence[ShadowDatasetRow],
) -> str:
    payload = {
        "model_id": model_id,
        "dataset_manifest_id": dataset_manifest_id,
        "requested_split_names": list(requested_split_names),
        "rows": [
            {
                "snapshot_id": row.snapshot_id,
                "split_name": row.split_name,
                "ordinal": row.ordinal,
            }
            for row in rows
        ],
    }
    serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"shadow_dataset_v1_{digest[:12]}"
