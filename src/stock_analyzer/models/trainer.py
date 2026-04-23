"""Dual-model training with strict temporal split and calibration."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TypeAlias

import numpy as np
import numpy.typing as npt
import pandas as pd

from stock_analyzer.config import (
    LabelsConfig,
    MarketRelativeFeatureConfig,
    ModelsConfig,
    TrainingConfig,
)
from stock_analyzer.data.provider import MarketDataProvider
from stock_analyzer.feature.engineer import FeatureEngineer
from stock_analyzer.feature.market_context import build_market_relative_frame
from stock_analyzer.labels.soup import build_soup_labels
from stock_analyzer.learning.dataset_manifest import DatasetManifestBuilder
from stock_analyzer.learning.feedback_weighting import (
    build_feedback_weight,
    summarize_feedback_weights,
)
from stock_analyzer.learning.feature_schema_registry import FeatureSchemaRegistry
from stock_analyzer.learning.label_policy_registry import (
    LabelPolicyRecord,
    LabelPolicyRegistry,
    build_label_policy_record,
)
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    DatasetManifest,
    OutcomeRecord,
)
from stock_analyzer.learning.sample_store import SampleStore
from stock_analyzer.models.adapters import (
    LightGBMAdapter,
    XGBoostAdapter,
    inspect_model_backend_dependencies,
)
from stock_analyzer.models.artifact import ModelArtifact
from stock_analyzer.models.calibration import IsotonicCalibrator
from stock_analyzer.time_semantics import apply_time_invariants_to_frame

FloatArray: TypeAlias = npt.NDArray[np.float64]


@dataclass(slots=True)
class TrainResult:
    artifact: ModelArtifact
    metrics: dict[str, float]
    samples_total: int
    samples_train: int
    samples_validation: int
    samples_calibration: int
    samples_test: int
    samples_embargo: int
    lgbm_backend: str
    xgb_backend: str

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["artifact"] = self.artifact.to_dict()
        return payload


@dataclass(frozen=True, slots=True)
class _TemporalSplit:
    train_slice: slice
    calibration_slice: slice
    test_slice: slice
    embargo_rows: int


class ModelTrainer:
    """Train cross-review models on engineered features and soup labels."""

    def __init__(
        self,
        training: TrainingConfig,
        labels: LabelsConfig,
        models: ModelsConfig | None = None,
        settlement_lag_days: int = 1,
        provider: MarketDataProvider | None = None,
        market_relative_feature: MarketRelativeFeatureConfig | None = None,
    ) -> None:
        self._training = training
        self._labels = labels
        self._models = models
        self._engineer = FeatureEngineer()
        self._settlement_lag_days = max(0, int(settlement_lag_days))
        self._provider = provider
        self._market_relative_feature = (
            market_relative_feature
            if market_relative_feature is not None
            else MarketRelativeFeatureConfig()
        )

    def train_on_bars(
        self,
        bars: pd.DataFrame,
        intraday_1m: pd.DataFrame | None = None,
        intraday_5m: pd.DataFrame | None = None,
        market_index: pd.DataFrame | None = None,
    ) -> TrainResult:
        filtered_bars, _bars_time_gate = apply_time_invariants_to_frame(
            bars,
            decision_time=datetime.now(),
            timezone="Asia/Shanghai",
            holding_horizon_days=self._labels.horizon_days,
            settlement_lag_days=self._settlement_lag_days,
            require_mature_label=False,
        )
        if filtered_bars.empty:
            raise ValueError("no bars available after time invariants gate")
        effective_market_index = market_index
        if effective_market_index is None and bool(self._market_relative_feature.enabled):
            if self._provider is None:
                raise ValueError("market_relative_feature_enabled_requires_provider")
            effective_market_index = build_market_relative_frame(
                self._provider,
                bars=filtered_bars,
                config=self._market_relative_feature,
            )
        features = self._engineer.transform(
            filtered_bars,
            intraday_1m=intraday_1m,
            intraday_5m=intraday_5m,
            market_index=effective_market_index,
        )
        labels = build_soup_labels(
            bars=filtered_bars,
            take_profit_pct=self._labels.take_profit_pct,
            stop_loss_pct=self._labels.stop_loss_pct,
            horizon_days=self._labels.horizon_days,
            price_basis=self._labels.pnl_price_basis,
            exclude_untradable=self._labels.exclude_untradable,
            conflict_policy=self._labels.conflict_policy,
            conflict_soft_label_value=self._labels.conflict_soft_label_value,
        )
        return self.train_on_feature_label(features=features, labels=labels)

    def train_on_sample_store(
        self,
        *,
        store: SampleStore,
        feature_schema_id: str,
        feature_schema_hash: str,
        label_policy_id: str,
        label_policy_hash: str,
        snapshot_ids: Sequence[str] | None = None,
        feature_schema_registry: FeatureSchemaRegistry | None = None,
        label_policy_registry: LabelPolicyRegistry | None = None,
        sample_selection_rule: str = "",
        time_window_start: datetime | None = None,
        time_window_end: datetime | None = None,
        fidelity_filter: Sequence[BackfillFidelityTier] | None = None,
    ) -> TrainResult:
        """Build a manifest from sample-store rows and train directly on that manifest."""

        normalized_fidelity = _normalize_fidelity_filter(fidelity_filter)
        manifest = DatasetManifestBuilder(
            store=store,
            feature_schema_registry=feature_schema_registry,
        ).create_manifest(
            feature_schema_id=feature_schema_id,
            feature_schema_hash=feature_schema_hash,
            label_policy_id=label_policy_id,
            label_policy_hash=label_policy_hash,
            snapshot_ids=snapshot_ids,
            sample_selection_rule=sample_selection_rule,
            time_window_start=time_window_start,
            time_window_end=time_window_end,
            fidelity_filter=normalized_fidelity,
            calibration_ratio=max(0.0, float(self._training.calibration_ratio)),
            test_ratio=max(0.0, float(self._training.test_ratio)),
        )
        return self.train_on_dataset_manifest(
            store=store,
            dataset_manifest=manifest,
            feature_schema_registry=feature_schema_registry,
            label_policy_registry=label_policy_registry,
        )

    def train_on_dataset_manifest(
        self,
        *,
        store: SampleStore,
        dataset_manifest: str | DatasetManifest,
        feature_schema_registry: FeatureSchemaRegistry | None = None,
        label_policy_registry: LabelPolicyRegistry | None = None,
    ) -> TrainResult:
        """Train directly from manifest-referenced snapshot/ outcome rows."""

        manifest = (
            store.get_manifest(dataset_manifest)
            if isinstance(dataset_manifest, str)
            else dataset_manifest
        )
        if manifest is None:
            raise ValueError(f"dataset manifest not found: {dataset_manifest}")
        manifest_items = store.list_manifest_items(manifest.dataset_manifest_id)
        if not manifest_items:
            raise ValueError(
                "dataset manifest has no membership items: "
                f"{manifest.dataset_manifest_id}"
            )

        snapshot_ids = [item.snapshot_id for item in manifest_items]
        snapshots = {
            snapshot.snapshot_id: snapshot
            for snapshot in store.list_snapshots(snapshot_ids=snapshot_ids)
        }
        outcomes = {
            outcome.snapshot_id: outcome
            for outcome in store.list_outcomes(snapshot_ids=snapshot_ids)
        }
        feature_columns = self._resolve_manifest_feature_columns(
            manifest=manifest,
            snapshots=list(snapshots.values()),
            feature_schema_registry=feature_schema_registry,
        )
        label_policy = self._resolve_manifest_label_policy(
            manifest=manifest,
            label_policy_registry=label_policy_registry,
        )
        label_column = label_policy.label_name

        row_index: list[tuple[str, datetime, str]] = []
        row_payloads: list[dict[str, float]] = []
        split_labels: list[str] = []
        sample_weights: list[float] = []
        feedback_rows = []
        for item in manifest_items:
            snapshot = snapshots.get(item.snapshot_id)
            if snapshot is None:
                raise ValueError(f"snapshot missing for manifest item: {item.snapshot_id}")
            outcome = outcomes.get(item.snapshot_id)
            if outcome is None:
                raise ValueError(f"outcome missing for manifest item: {item.snapshot_id}")
            label_value = _label_from_outcome(outcome=outcome, policy=label_policy)
            if label_value is None:
                continue
            row_payload = {
                column: float(snapshot.feature_vector.get(column, 0.0))
                for column in feature_columns
            }
            row_payload[label_column] = label_value
            row_payloads.append(row_payload)
            row_index.append((snapshot.symbol, snapshot.decision_time, snapshot.snapshot_id))
            split_labels.append(_normalize_split_name(item.split_name))
            feedback_weight = build_feedback_weight(
                snapshot=snapshot,
                outcome=outcome,
                apply_feedback=bool(self._training.learning_feedback_weighting_enabled),
                clip_low=max(0.01, float(self._training.learning_feedback_weight_clip_low)),
                clip_high=max(
                    float(self._training.learning_feedback_weight_clip_low),
                    float(self._training.learning_feedback_weight_clip_high),
                ),
            )
            feedback_rows.append(feedback_weight)
            sample_weights.append(float(feedback_weight.final_weight))

        if not row_payloads:
            raise ValueError(
                "dataset manifest produced no trainable rows after label resolution: "
                f"{manifest.dataset_manifest_id}"
            )

        aligned = pd.DataFrame(
            row_payloads,
            index=pd.MultiIndex.from_tuples(
                row_index,
                names=["symbol", "decision_time", "snapshot_id"],
            ),
        )
        return self._train_aligned_dataset(
            aligned=aligned,
            feature_columns=feature_columns,
            label_column=label_column,
            split_labels=split_labels,
            time_gate={
                "total_rows": len(aligned),
                "kept_rows": len(aligned),
                "dropped_rows": 0,
            },
            row_weights=sample_weights,
            weight_summary=summarize_feedback_weights(feedback_rows).to_dict(),
            feature_schema_id=manifest.feature_schema_id,
            feature_schema_hash=manifest.feature_schema_hash,
            label_policy_id=manifest.label_policy_id,
            label_policy_hash=manifest.label_policy_hash,
            dataset_manifest_id=manifest.dataset_manifest_id,
        )

    def train_on_feature_label(self, features: pd.DataFrame, labels: pd.Series) -> TrainResult:
        aligned = features.join(labels, how="inner")
        label_column = labels.name or "label_soup_tp_before_sl"
        aligned = aligned.dropna(subset=[label_column])
        aligned, time_gate = apply_time_invariants_to_frame(
            aligned,
            decision_time=datetime.now(),
            timezone="Asia/Shanghai",
            holding_horizon_days=self._labels.horizon_days,
            settlement_lag_days=self._settlement_lag_days,
            require_mature_label=True,
        )
        if aligned.shape[0] < self._training.min_samples:
            raise ValueError(
                "insufficient samples for training: "
                f"{aligned.shape[0]} < {self._training.min_samples}"
            )

        return self._train_aligned_dataset(
            aligned=aligned,
            feature_columns=list(features.columns),
            label_column=label_column,
            split_labels=None,
            time_gate=time_gate,
        )

    def _train_aligned_dataset(
        self,
        *,
        aligned: pd.DataFrame,
        feature_columns: list[str],
        label_column: str,
        split_labels: Sequence[str] | None,
        time_gate: dict[str, object] | None,
        row_weights: Sequence[float] | None = None,
        weight_summary: dict[str, object] | None = None,
        feature_schema_id: str = "",
        feature_schema_hash: str = "",
        label_policy_id: str = "",
        label_policy_hash: str = "",
        dataset_manifest_id: str = "",
    ) -> TrainResult:
        feature_columns = list(feature_columns)
        if aligned.shape[0] < self._training.min_samples:
            raise ValueError(
                "insufficient samples for training: "
                f"{aligned.shape[0]} < {self._training.min_samples}"
            )
        if self._models is not None and self._models.include_random_feature_baseline:
            aligned = aligned.copy()
            random_feature = "__random_baseline__"
            rng = np.random.default_rng(20260302)
            aligned[random_feature] = rng.normal(0.0, 1.0, size=len(aligned))
            feature_columns.append(random_feature)

        x = aligned[feature_columns].to_numpy(dtype=float)
        y = aligned[label_column].to_numpy(dtype=float)
        sample_weight_array = (
            np.asarray(row_weights, dtype=float) if row_weights is not None else None
        )
        if sample_weight_array is not None and sample_weight_array.shape[0] != x.shape[0]:
            raise ValueError("row_weights length must match aligned dataset rows")
        if split_labels is None:
            split = self._build_temporal_split(total_rows=len(aligned))
            x_train = x[split.train_slice]
            y_train = y[split.train_slice]
            x_calibration = x[split.calibration_slice]
            y_calibration = y[split.calibration_slice]
            x_test = x[split.test_slice]
            y_test = y[split.test_slice]
            train_weight = (
                sample_weight_array[split.train_slice]
                if sample_weight_array is not None
                else None
            )
            samples_embargo = int(split.embargo_rows)
            split_source = "temporal"
        else:
            x_train, y_train, x_calibration, y_calibration, x_test, y_test = (
                _split_by_manifest_labels(x=x, y=y, split_labels=split_labels)
            )
            train_weight = (
                _split_weights_by_manifest_labels(
                    weights=sample_weight_array,
                    split_labels=split_labels,
                )
                if sample_weight_array is not None
                else None
            )
            samples_embargo = 0
            split_source = "manifest"
        if len(x_train) == 0 or len(x_calibration) == 0 or len(x_test) == 0:
            raise ValueError("training split produced empty train/calibration/test set")

        lgbm = LightGBMAdapter()
        xgb = XGBoostAdapter()
        lgbm.fit(x_train, y_train, sample_weight=train_weight)
        xgb.fit(x_train, y_train, sample_weight=train_weight)

        lgbm_calibration_raw = lgbm.predict_proba(x_calibration)
        xgb_calibration_raw = xgb.predict_proba(x_calibration)
        lgbm_calibrator = IsotonicCalibrator()
        xgb_calibrator = IsotonicCalibrator()
        lgbm_calibrator.fit(lgbm_calibration_raw, y_calibration)
        xgb_calibrator.fit(xgb_calibration_raw, y_calibration)

        lgbm_calibration_prob = lgbm_calibrator.predict(lgbm_calibration_raw)
        xgb_calibration_prob = xgb_calibrator.predict(xgb_calibration_raw)
        meta_weights = _build_meta_weights(
            y_true=y_calibration,
            lgbm=lgbm_calibration_prob,
            xgb=xgb_calibration_prob,
        )

        lgbm_test_prob = lgbm_calibrator.predict(lgbm.predict_proba(x_test))
        xgb_test_prob = xgb_calibrator.predict(xgb.predict_proba(x_test))
        meta_test_prob = lgbm_test_prob * meta_weights["lgbm"] + xgb_test_prob * meta_weights["xgb"]

        metrics = _evaluate_metrics(
            y_true=y_test,
            lgbm=lgbm_test_prob,
            xgb=xgb_test_prob,
            meta=meta_test_prob,
            precision_at_k_ratio=max(0.01, float(self._training.precision_at_k_ratio)),
        )
        resolved_time_gate = time_gate or {}
        metrics["time_gate_total_rows"] = _as_float(resolved_time_gate.get("total_rows"))
        metrics["time_gate_kept_rows"] = _as_float(resolved_time_gate.get("kept_rows"))
        metrics["time_gate_dropped_rows"] = _as_float(resolved_time_gate.get("dropped_rows"))
        metrics["calibration_samples"] = float(len(x_calibration))
        metrics["test_samples"] = float(len(x_test))
        metrics["embargo_days"] = float(samples_embargo)
        metrics["train_sample_weight_mean"] = (
            float(np.mean(train_weight)) if train_weight is not None and len(train_weight) else 1.0
        )
        metrics["train_sample_weight_max"] = (
            float(np.max(train_weight)) if train_weight is not None and len(train_weight) else 1.0
        )
        metrics["train_sample_weight_min"] = (
            float(np.min(train_weight)) if train_weight is not None and len(train_weight) else 1.0
        )

        metadata = {
            "artifact_created_at": datetime.now().isoformat(),
            "lgbm_backend": lgbm.backend,
            "xgb_backend": xgb.backend,
            "degraded_model_mode": lgbm.backend.startswith("fallback")
            and xgb.backend.startswith("fallback"),
            "calibration_method": self._models.calibration
            if self._models is not None
            else "isotonic",
            "train_samples": int(len(x_train)),
            "calibration_samples": int(len(x_calibration)),
            "test_samples": int(len(x_test)),
            "embargo_days": int(samples_embargo),
            "dataset_split_strategy": split_source,
            "label_conflict_policy": self._labels.conflict_policy,
            "meta_blend_weights": meta_weights,
            "dependency_status": inspect_model_backend_dependencies(),
            "sample_weighting": dict(weight_summary or {}),
        }
        artifact = ModelArtifact.create(
            feature_schema_id=feature_schema_id,
            feature_schema_hash=feature_schema_hash,
            label_policy_id=label_policy_id,
            label_policy_hash=label_policy_hash,
            dataset_manifest_id=dataset_manifest_id,
            feature_columns=feature_columns,
            lgbm_model=lgbm.to_dict(),
            xgb_model=xgb.to_dict(),
            lgbm_calibrator=lgbm_calibrator.to_dict(),
            xgb_calibrator=xgb_calibrator.to_dict(),
            training_metrics=metrics,
            metadata=metadata,
        )
        return TrainResult(
            artifact=artifact,
            metrics=metrics,
            samples_total=int(len(aligned)),
            samples_train=int(len(x_train)),
            samples_validation=int(len(x_calibration)),
            samples_calibration=int(len(x_calibration)),
            samples_test=int(len(x_test)),
            samples_embargo=int(samples_embargo),
            lgbm_backend=lgbm.backend,
            xgb_backend=xgb.backend,
        )

    def _resolve_manifest_feature_columns(
        self,
        *,
        manifest: DatasetManifest,
        snapshots: Sequence[object],
        feature_schema_registry: FeatureSchemaRegistry | None,
    ) -> list[str]:
        if feature_schema_registry is not None:
            record = feature_schema_registry.get_by_id(manifest.feature_schema_id)
            if record is None:
                raise ValueError(
                    "feature schema not found in registry: "
                    f"{manifest.feature_schema_id}"
                )
            if record.feature_schema_hash != manifest.feature_schema_hash:
                raise ValueError(
                    "feature schema hash mismatch for manifest: "
                    f"{manifest.feature_schema_id}"
                )
            return list(record.feature_names)

        inferred_columns = sorted(
            {
                str(column).strip()
                for snapshot in snapshots
                if hasattr(snapshot, "feature_vector")
                for column in getattr(snapshot, "feature_vector", {}).keys()
                if str(column).strip()
            }
        )
        if not inferred_columns:
            raise ValueError(
                "unable to infer feature columns from manifest snapshots: "
                f"{manifest.dataset_manifest_id}"
            )
        return inferred_columns

    def _resolve_manifest_label_policy(
        self,
        *,
        manifest: DatasetManifest,
        label_policy_registry: LabelPolicyRegistry | None,
    ) -> LabelPolicyRecord:
        if label_policy_registry is not None:
            record = label_policy_registry.get_by_id(manifest.label_policy_id)
            if record is None:
                raise ValueError(
                    "label policy not found in registry: "
                    f"{manifest.label_policy_id}"
                )
            if record.label_policy_hash != manifest.label_policy_hash:
                raise ValueError(
                    "label policy hash mismatch for manifest: "
                    f"{manifest.label_policy_id}"
                )
            return record

        fallback_record = build_label_policy_record(
            label_name=self._labels.primary,
            take_profit_pct=self._labels.take_profit_pct,
            stop_loss_pct=self._labels.stop_loss_pct,
            horizon_days=self._labels.horizon_days,
            price_basis=self._labels.pnl_price_basis,
            exclude_untradable=self._labels.exclude_untradable,
            conflict_policy=self._labels.conflict_policy,
            conflict_soft_label_value=self._labels.conflict_soft_label_value,
            label_policy_id=manifest.label_policy_id,
        )
        if fallback_record.label_policy_hash != manifest.label_policy_hash:
            raise ValueError(
                "label policy registry required for manifest policy resolution: "
                f"{manifest.label_policy_id}"
            )
        return fallback_record

    def train_and_save(
        self,
        bars: pd.DataFrame,
        output_path: str | Path | None = None,
        intraday_1m: pd.DataFrame | None = None,
        intraday_5m: pd.DataFrame | None = None,
        market_index: pd.DataFrame | None = None,
    ) -> TrainResult:
        result = self.train_on_bars(
            bars,
            intraday_1m=intraday_1m,
            intraday_5m=intraday_5m,
            market_index=market_index,
        )
        path = Path(output_path) if output_path else Path(self._training.artifact_path)
        result.artifact.save(path)
        return result

    def _build_temporal_split(self, *, total_rows: int) -> _TemporalSplit:
        calibration_ratio, test_ratio = _resolve_split_ratios(self._training)
        calibration_count = max(1, int(round(total_rows * calibration_ratio)))
        test_count = max(1, int(round(total_rows * test_ratio)))
        embargo_rows = max(
            0,
            int(self._training.embargo_days)
            if int(self._training.embargo_days) > 0
            else int(self._labels.horizon_days + self._settlement_lag_days),
        )

        test_start = total_rows - test_count
        calibration_end = max(0, test_start - embargo_rows)
        calibration_start = max(0, calibration_end - calibration_count)
        train_end = max(0, calibration_start - embargo_rows)

        while train_end <= 0 and (calibration_count > 1 or test_count > 1):
            if calibration_count >= test_count and calibration_count > 1:
                calibration_count -= 1
            elif test_count > 1:
                test_count -= 1
            test_start = total_rows - test_count
            calibration_end = max(0, test_start - embargo_rows)
            calibration_start = max(0, calibration_end - calibration_count)
            train_end = max(0, calibration_start - embargo_rows)

        return _TemporalSplit(
            train_slice=slice(0, train_end),
            calibration_slice=slice(calibration_start, calibration_end),
            test_slice=slice(test_start, total_rows),
            embargo_rows=embargo_rows,
        )


def _resolve_split_ratios(training: TrainingConfig) -> tuple[float, float]:
    calibration_ratio = max(0.0, float(training.calibration_ratio))
    test_ratio = max(0.0, float(training.test_ratio))
    if calibration_ratio <= 0 and test_ratio <= 0:
        holdout = max(0.02, float(training.validation_ratio))
        calibration_ratio = holdout / 2.0
        test_ratio = holdout / 2.0
    elif calibration_ratio <= 0:
        calibration_ratio = max(0.01, float(training.validation_ratio) - test_ratio)
    elif test_ratio <= 0:
        test_ratio = max(0.01, float(training.validation_ratio) - calibration_ratio)
    total = calibration_ratio + test_ratio
    if total >= 0.95:
        scale = 0.95 / total
        calibration_ratio *= scale
        test_ratio *= scale
    return calibration_ratio, test_ratio


def _build_meta_weights(
    *,
    y_true: FloatArray,
    lgbm: FloatArray,
    xgb: FloatArray,
) -> dict[str, float]:
    y_binary = (y_true >= 0.5).astype(float)
    lgbm_brier = float(np.mean((lgbm - y_binary) ** 2))
    xgb_brier = float(np.mean((xgb - y_binary) ** 2))
    lgbm_weight = 1.0 / max(lgbm_brier, 1e-6)
    xgb_weight = 1.0 / max(xgb_brier, 1e-6)
    total = lgbm_weight + xgb_weight
    return {
        "lgbm": round(lgbm_weight / total, 6),
        "xgb": round(xgb_weight / total, 6),
    }


def _evaluate_metrics(
    *,
    y_true: FloatArray,
    lgbm: FloatArray,
    xgb: FloatArray,
    meta: FloatArray,
    precision_at_k_ratio: float,
) -> dict[str, float]:
    y_binary = (y_true >= 0.5).astype(float)
    meta_pred = (meta >= 0.5).astype(float)

    accuracy = float(np.mean(meta_pred == y_binary))
    brier = float(np.mean((meta - y_binary) ** 2))
    positive_rate = float(np.mean(meta_pred))
    auc = _binary_auc(y_binary, meta)
    precision_at_k, recall_at_k = _precision_recall_at_k(
        y_true=y_binary,
        probabilities=meta,
        top_ratio=precision_at_k_ratio,
    )
    positive_probs = meta[y_binary >= 0.5]
    negative_probs = meta[y_binary < 0.5]
    mean_prob_spread = (
        float(positive_probs.mean() - negative_probs.mean())
        if len(positive_probs) and len(negative_probs)
        else 0.0
    )
    return {
        "accuracy": round(accuracy, 6),
        "auc": round(auc, 6),
        "brier": round(brier, 6),
        "precision_at_k": round(precision_at_k, 6),
        "recall_at_k": round(recall_at_k, 6),
        "positive_rate": round(positive_rate, 6),
        "mean_prob_spread": round(mean_prob_spread, 6),
        "validation_samples": float(y_true.shape[0]),
        "meta_mean_prob": round(float(np.mean(meta)), 6),
        "lgbm_mean_prob": round(float(np.mean(lgbm)), 6),
        "xgb_mean_prob": round(float(np.mean(xgb)), 6),
    }


def _binary_auc(y_true: FloatArray, probabilities: FloatArray) -> float:
    positives = int(np.sum(y_true >= 0.5))
    negatives = int(np.sum(y_true < 0.5))
    if positives == 0 or negatives == 0:
        return 0.5
    order = np.argsort(probabilities)
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(probabilities) + 1, dtype=float)
    positive_rank_sum = float(ranks[y_true >= 0.5].sum())
    auc = (positive_rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives)
    return float(max(0.0, min(1.0, auc)))


def _precision_recall_at_k(
    *,
    y_true: FloatArray,
    probabilities: FloatArray,
    top_ratio: float,
) -> tuple[float, float]:
    sample_count = len(probabilities)
    if sample_count == 0:
        return 0.0, 0.0
    top_k = max(1, int(round(sample_count * max(0.01, top_ratio))))
    order = np.argsort(probabilities)[::-1][:top_k]
    top_labels = y_true[order]
    true_positives = float(np.sum(top_labels >= 0.5))
    precision = true_positives / max(len(top_labels), 1)
    recall = true_positives / max(float(np.sum(y_true >= 0.5)), 1.0)
    return float(precision), float(recall)


def _split_by_manifest_labels(
    *,
    x: FloatArray,
    y: FloatArray,
    split_labels: Sequence[str],
) -> tuple[FloatArray, FloatArray, FloatArray, FloatArray, FloatArray, FloatArray]:
    normalized = [_normalize_split_name(item) for item in split_labels]
    if len(normalized) != len(x):
        raise ValueError("split_labels length must match dataset rows")
    train_mask = np.asarray([label == "train" for label in normalized], dtype=bool)
    calibration_mask = np.asarray(
        [label == "calibration" for label in normalized],
        dtype=bool,
    )
    test_mask = np.asarray([label == "test" for label in normalized], dtype=bool)
    return (
        x[train_mask],
        y[train_mask],
        x[calibration_mask],
        y[calibration_mask],
        x[test_mask],
        y[test_mask],
    )


def _split_weights_by_manifest_labels(
    *,
    weights: FloatArray,
    split_labels: Sequence[str],
) -> FloatArray:
    normalized = [_normalize_split_name(item) for item in split_labels]
    if len(normalized) != len(weights):
        raise ValueError("split_labels length must match weight rows")
    train_mask = np.asarray([label == "train" for label in normalized], dtype=bool)
    return weights[train_mask]


def _normalize_split_name(value: str) -> str:
    normalized = value.strip().lower()
    if normalized == "validation":
        return "calibration"
    return normalized


def _normalize_fidelity_filter(
    fidelity_filter: Sequence[BackfillFidelityTier] | None,
) -> list[BackfillFidelityTier]:
    normalized: list[BackfillFidelityTier] = []
    seen: set[BackfillFidelityTier] = set()
    for item in fidelity_filter or ():
        if item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return normalized


def _label_from_outcome(
    *,
    outcome: OutcomeRecord,
    policy: LabelPolicyRecord,
) -> float | None:
    take_profit_hit = (
        outcome.max_favorable_excursion is not None
        and float(outcome.max_favorable_excursion) >= float(policy.take_profit_pct)
    )
    stop_loss_hit = (
        outcome.max_adverse_excursion is not None
        and float(outcome.max_adverse_excursion) <= -float(policy.stop_loss_pct)
    )
    if take_profit_hit and stop_loss_hit:
        normalized_policy = policy.conflict_policy.strip().lower()
        if normalized_policy == "soft_label":
            return float(max(0.0, min(1.0, policy.conflict_soft_label_value)))
        return 0.0
    if take_profit_hit:
        return 1.0
    if stop_loss_hit:
        return 0.0
    if outcome.realized_return is None:
        return None
    realized_return = float(outcome.realized_return)
    if realized_return >= float(policy.take_profit_pct):
        return 1.0
    return 0.0


def _as_float(value: object, default: float = 0.0) -> float:
    if not isinstance(value, (int, float, str, bytes, bytearray)):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
