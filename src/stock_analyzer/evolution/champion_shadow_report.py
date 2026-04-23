"""Champion-vs-shadow per-sample comparison report builder."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from stock_analyzer.evolution.modules.m11_shadow_loader import M11ShadowObservation
from stock_analyzer.evolution.modules.m11_shadow_portfolio import evaluate_m11_shadow_portfolio
from stock_analyzer.evolution.shadow_dataset_builder import ShadowDatasetBuilder, ShadowDatasetRow
from stock_analyzer.learning.feature_schema_registry import FeatureSchemaRegistry
from stock_analyzer.learning.label_policy_registry import LabelPolicyRegistry
from stock_analyzer.learning.sample_store import SampleStore
from stock_analyzer.models.artifact import ModelArtifact
from stock_analyzer.models.predictor import SignalPredictor
from stock_analyzer.models.registry import ModelRegistry, ModelRegistryRecord


@dataclass(slots=True)
class ChampionShadowComparisonRow:
    """One aligned champion-vs-shadow comparison row."""

    snapshot_id: str
    symbol: str
    strategy: str
    trade_date: str
    decision_time: str
    label_mature_time: str
    maturity_status: str
    reconcile_status: str
    split_name: str
    ordinal: int
    label: float
    realized_return: float
    execution_fill_ratio: float | None
    realized_slippage_bp: float | None
    sample_weight: float
    data_quality_score: float | None
    champion_scores: dict[str, float] = field(default_factory=dict)
    shadow_scores: dict[str, float] = field(default_factory=dict)
    champion_signal: int = 0
    shadow_signal: int = 0
    signal_diverged: bool = False
    p_meta_delta: float = 0.0
    p_lgbm_delta: float = 0.0
    p_xgb_delta: float = 0.0
    champion_shadow_return: float = 0.0
    challenger_shadow_return: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "symbol": self.symbol,
            "strategy": self.strategy,
            "trade_date": self.trade_date,
            "decision_time": self.decision_time,
            "label_mature_time": self.label_mature_time,
            "maturity_status": self.maturity_status,
            "reconcile_status": self.reconcile_status,
            "split_name": self.split_name,
            "ordinal": self.ordinal,
            "label": self.label,
            "realized_return": self.realized_return,
            "execution_fill_ratio": self.execution_fill_ratio,
            "realized_slippage_bp": self.realized_slippage_bp,
            "sample_weight": self.sample_weight,
            "data_quality_score": self.data_quality_score,
            "champion_scores": dict(self.champion_scores),
            "shadow_scores": dict(self.shadow_scores),
            "champion_signal": self.champion_signal,
            "shadow_signal": self.shadow_signal,
            "signal_diverged": self.signal_diverged,
            "p_meta_delta": self.p_meta_delta,
            "p_lgbm_delta": self.p_lgbm_delta,
            "p_xgb_delta": self.p_xgb_delta,
            "champion_shadow_return": self.champion_shadow_return,
            "challenger_shadow_return": self.challenger_shadow_return,
        }

    def preview_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "symbol": self.symbol,
            "trade_date": self.trade_date,
            "split_name": self.split_name,
            "maturity_status": self.maturity_status,
            "label": self.label,
            "realized_return": self.realized_return,
            "champion_p_meta": self.champion_scores.get("p_meta"),
            "shadow_p_meta": self.shadow_scores.get("p_meta"),
            "p_meta_delta": self.p_meta_delta,
            "signal_diverged": self.signal_diverged,
        }


@dataclass(slots=True)
class ChampionShadowComparisonReport:
    """Stable comparison report for one shadow model against one champion."""

    comparison_report_id: str
    champion_model_id: str
    shadow_model_id: str
    dataset_manifest_id: str
    feature_schema_id: str
    label_policy_id: str
    signal_threshold: float
    row_count: int
    split_counts: dict[str, int]
    m11_report: dict[str, object]
    summary_metrics: dict[str, float]
    champion_predictor_mode: dict[str, object]
    shadow_predictor_mode: dict[str, object]
    rows: list[ChampionShadowComparisonRow] = field(default_factory=list)

    def to_dict(
        self,
        *,
        include_rows: bool = True,
        preview_limit: int = 5,
    ) -> dict[str, object]:
        payload = {
            "comparison_report_id": self.comparison_report_id,
            "champion_model_id": self.champion_model_id,
            "shadow_model_id": self.shadow_model_id,
            "dataset_manifest_id": self.dataset_manifest_id,
            "feature_schema_id": self.feature_schema_id,
            "label_policy_id": self.label_policy_id,
            "signal_threshold": self.signal_threshold,
            "row_count": self.row_count,
            "split_counts": dict(self.split_counts),
            "m11_report": dict(self.m11_report),
            "summary_metrics": dict(self.summary_metrics),
            "champion_predictor_mode": dict(self.champion_predictor_mode),
            "shadow_predictor_mode": dict(self.shadow_predictor_mode),
            "preview": [row.preview_dict() for row in self.rows[: max(1, int(preview_limit))]],
        }
        if include_rows:
            payload["rows"] = [row.to_dict() for row in self.rows]
        return payload


class ChampionShadowReportBuilder:
    """Build one per-sample comparison report between champion and shadow models."""

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
        self._shadow_dataset_builder = ShadowDatasetBuilder(
            store=store,
            model_registry=model_registry,
            feature_schema_registry=feature_schema_registry,
            label_policy_registry=label_policy_registry,
        )

    def build_report(
        self,
        *,
        shadow_model_id: str,
        champion_model_id: str = "",
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
        signal_threshold: float = 0.5,
    ) -> ChampionShadowComparisonReport:
        shadow_record = self._require_record(shadow_model_id)
        champion_record = self._resolve_champion_record(
            explicit_model_id=champion_model_id,
            shadow_record=shadow_record,
        )
        if champion_record.model_id == shadow_record.model_id:
            raise ValueError("champion and shadow model ids must be different")

        shadow_dataset = self._shadow_dataset_builder.build_for_model(
            model_id=shadow_record.model_id,
            split_names=split_names,
            max_rows=max_rows,
        )
        raw_snapshots = {
            snapshot.snapshot_id: snapshot
            for snapshot in self._store.list_snapshots(
                snapshot_ids=[row.snapshot_id for row in shadow_dataset.rows]
            )
        }
        champion_predictor, champion_mode = _load_predictor(record=champion_record)
        _shadow_predictor, shadow_mode = _load_predictor(record=shadow_record)

        threshold = max(0.0, min(1.0, float(signal_threshold)))
        rows: list[ChampionShadowComparisonRow] = []
        m11_observations: list[M11ShadowObservation] = []
        split_counts: dict[str, int] = {}
        meta_deltas: list[float] = []
        meta_abs_deltas: list[float] = []
        champion_probs: list[float] = []
        shadow_probs: list[float] = []

        for dataset_row in shadow_dataset.rows:
            snapshot = raw_snapshots.get(dataset_row.snapshot_id)
            if snapshot is None:
                raise ValueError(f"snapshot missing for comparison row: {dataset_row.snapshot_id}")
            champion_scores = _normalize_probability_scores(
                champion_predictor.predict_row(pd.Series(snapshot.feature_vector))
            )
            shadow_scores = _normalize_probability_scores(dataset_row.baseline_scores)
            champion_meta = float(champion_scores.get("p_meta", 0.5))
            shadow_meta = float(shadow_scores.get("p_meta", 0.5))
            champion_signal = int(champion_meta >= threshold)
            shadow_signal = int(shadow_meta >= threshold)
            realized_return = float(dataset_row.realized_return or 0.0)
            comparison_row = ChampionShadowComparisonRow(
                snapshot_id=dataset_row.snapshot_id,
                symbol=dataset_row.symbol,
                strategy=dataset_row.strategy,
                trade_date=dataset_row.decision_time.date().isoformat(),
                decision_time=dataset_row.decision_time.isoformat(),
                label_mature_time=(
                    dataset_row.label_mature_time.isoformat()
                    if dataset_row.label_mature_time is not None
                    else ""
                ),
                maturity_status=dataset_row.maturity_status,
                reconcile_status=dataset_row.reconcile_status,
                split_name=dataset_row.split_name,
                ordinal=dataset_row.ordinal,
                label=float(dataset_row.label),
                realized_return=realized_return,
                execution_fill_ratio=dataset_row.execution_fill_ratio,
                realized_slippage_bp=dataset_row.realized_slippage_bp,
                sample_weight=float(dataset_row.sample_weight),
                data_quality_score=dataset_row.data_quality_score,
                champion_scores=champion_scores,
                shadow_scores=shadow_scores,
                champion_signal=champion_signal,
                shadow_signal=shadow_signal,
                signal_diverged=champion_signal != shadow_signal,
                p_meta_delta=round(shadow_meta - champion_meta, 6),
                p_lgbm_delta=round(
                    float(shadow_scores.get("p_lgbm", 0.5))
                    - float(champion_scores.get("p_lgbm", 0.5)),
                    6,
                ),
                p_xgb_delta=round(
                    float(shadow_scores.get("p_xgb", 0.5))
                    - float(champion_scores.get("p_xgb", 0.5)),
                    6,
                ),
                champion_shadow_return=round(realized_return * champion_signal, 6),
                challenger_shadow_return=round(realized_return * shadow_signal, 6),
            )
            rows.append(comparison_row)
            m11_observations.append(
                M11ShadowObservation(
                    symbol=comparison_row.symbol,
                    champion_shadow_return=comparison_row.champion_shadow_return,
                    challenger_shadow_return=comparison_row.challenger_shadow_return,
                    champion_signal=comparison_row.champion_signal,
                    challenger_signal=comparison_row.shadow_signal,
                )
            )
            split_counts[comparison_row.split_name] = split_counts.get(comparison_row.split_name, 0) + 1
            meta_deltas.append(float(comparison_row.p_meta_delta))
            meta_abs_deltas.append(abs(float(comparison_row.p_meta_delta)))
            champion_probs.append(champion_meta)
            shadow_probs.append(shadow_meta)

        m11_result = evaluate_m11_shadow_portfolio(shadow_observations=m11_observations)
        summary_metrics = {
            "mean_p_meta_delta": _safe_mean(meta_deltas),
            "mean_abs_p_meta_delta": _safe_mean(meta_abs_deltas),
            "signal_divergence_ratio": _safe_mean(
                [1.0 if row.signal_diverged else 0.0 for row in rows]
            ),
            "champion_mean_p_meta": _safe_mean(champion_probs),
            "shadow_mean_p_meta": _safe_mean(shadow_probs),
            "champion_positive_rate": _safe_mean([float(row.champion_signal) for row in rows]),
            "shadow_positive_rate": _safe_mean([float(row.shadow_signal) for row in rows]),
        }
        report_id = _build_report_id(
            champion_model_id=champion_record.model_id,
            shadow_model_id=shadow_record.model_id,
            dataset_manifest_id=shadow_dataset.dataset_manifest_id,
            rows=rows,
        )
        return ChampionShadowComparisonReport(
            comparison_report_id=report_id,
            champion_model_id=champion_record.model_id,
            shadow_model_id=shadow_record.model_id,
            dataset_manifest_id=shadow_dataset.dataset_manifest_id,
            feature_schema_id=shadow_dataset.feature_schema_id,
            label_policy_id=shadow_dataset.label_policy_id,
            signal_threshold=threshold,
            row_count=len(rows),
            split_counts=split_counts,
            m11_report={
                "score": float(m11_result.score),
                "status": m11_result.status,
                "redlines": dict(m11_result.redlines),
                "metrics": {
                    "valid_samples": int(m11_result.metrics.valid_samples),
                    "champion_cum_return": float(m11_result.metrics.champion_cum_return),
                    "challenger_cum_return": float(m11_result.metrics.challenger_cum_return),
                    "champion_max_drawdown": float(m11_result.metrics.champion_max_drawdown),
                    "challenger_max_drawdown": float(m11_result.metrics.challenger_max_drawdown),
                    "drawdown_delta": float(m11_result.metrics.drawdown_delta),
                    "tail_loss_delta": float(m11_result.metrics.tail_loss_delta),
                    "execution_divergence_ratio": float(
                        m11_result.metrics.execution_divergence_ratio
                    ),
                    "champion_win_rate": float(m11_result.metrics.champion_win_rate),
                    "challenger_win_rate": float(m11_result.metrics.challenger_win_rate),
                    "mean_return_diff": float(m11_result.metrics.mean_return_diff),
                },
                "attribution": [
                    {
                        "name": item.name,
                        "value": float(item.value),
                        "threshold": float(item.threshold),
                        "breached": bool(item.breached),
                        "impact": float(item.impact),
                    }
                    for item in m11_result.attribution
                ],
            },
            summary_metrics=summary_metrics,
            champion_predictor_mode=champion_mode,
            shadow_predictor_mode=shadow_mode,
            rows=rows,
        )

    def _require_record(self, model_id: str) -> ModelRegistryRecord:
        normalized = str(model_id).strip()
        if not normalized:
            raise ValueError("model_id must not be empty")
        record = self._model_registry.get_by_id(normalized)
        if record is None:
            raise ValueError(f"model_id not found: {normalized}")
        return record

    def _resolve_champion_record(
        self,
        *,
        explicit_model_id: str,
        shadow_record: ModelRegistryRecord,
    ) -> ModelRegistryRecord:
        explicit = str(explicit_model_id).strip()
        if explicit:
            return self._require_record(explicit)
        parent = str(shadow_record.parent_model_id).strip()
        if parent:
            record = self._model_registry.get_by_id(parent)
            if record is not None:
                return record
        champion = self._model_registry.active_champion()
        if champion is None:
            raise ValueError("active champion not found")
        return champion


def _load_predictor(*, record: ModelRegistryRecord) -> tuple[SignalPredictor, dict[str, object]]:
    artifact_path = Path(record.artifact_uri).expanduser().resolve()
    artifact = ModelArtifact.load(artifact_path)
    predictor = SignalPredictor.from_artifact(artifact, artifact_root=artifact_path.parent)
    return predictor, predictor.mode_details()


def _normalize_probability_scores(payload: dict[str, float] | None) -> dict[str, float]:
    if not isinstance(payload, dict):
        return {"p_lgbm": 0.5, "p_xgb": 0.5, "p_meta": 0.5}
    alias_map = {
        "lgbm": "p_lgbm",
        "p_lgbm": "p_lgbm",
        "xgb": "p_xgb",
        "p_xgb": "p_xgb",
        "meta": "p_meta",
        "p_meta": "p_meta",
    }
    normalized = {"p_lgbm": 0.5, "p_xgb": 0.5, "p_meta": 0.5}
    for key, value in payload.items():
        canonical = alias_map.get(str(key).strip().lower())
        if canonical is None:
            continue
        normalized[canonical] = max(0.0, min(1.0, float(value)))
    return normalized


def _safe_mean(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return round(float(sum(values) / len(values)), 6)


def _build_report_id(
    *,
    champion_model_id: str,
    shadow_model_id: str,
    dataset_manifest_id: str,
    rows: Sequence[ChampionShadowComparisonRow],
) -> str:
    payload = {
        "champion_model_id": champion_model_id,
        "shadow_model_id": shadow_model_id,
        "dataset_manifest_id": dataset_manifest_id,
        "rows": [
            {
                "snapshot_id": row.snapshot_id,
                "ordinal": row.ordinal,
                "split_name": row.split_name,
            }
            for row in rows
        ],
    }
    serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"champion_shadow_report_v1_{digest[:12]}"
