"""Dual-model training with strict temporal split and calibration."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
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
from stock_analyzer.learning.feature_schema_registry import FeatureSchemaRegistry
from stock_analyzer.learning.feedback_weighting import (
    build_feedback_weight,
    summarize_feedback_weights,
)
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
    train_mask: npt.NDArray[np.bool_]
    calibration_mask: npt.NDArray[np.bool_]
    test_mask: npt.NDArray[np.bool_]
    embargo_mask: npt.NDArray[np.bool_]
    embargo_days: int
    train_dates: list[str]
    calibration_dates: list[str]
    test_dates: list[str]
    embargo_dates: list[str]


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
            embargo_days=max(0, int(self._labels.horizon_days) + self._settlement_lag_days),
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
                f"dataset manifest has no membership items: {manifest.dataset_manifest_id}"
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
            split = self._build_temporal_split(aligned=aligned)
            x_train = x[split.train_mask]
            y_train = y[split.train_mask]
            x_calibration = x[split.calibration_mask]
            y_calibration = y[split.calibration_mask]
            x_test = x[split.test_mask]
            y_test = y[split.test_mask]
            train_weight = (
                sample_weight_array[split.train_mask] if sample_weight_array is not None else None
            )
            samples_embargo = int(np.count_nonzero(split.embargo_mask))
            embargo_trading_days = int(split.embargo_days)
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
            embargo_trading_days = 0
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
        metrics["embargo_days"] = float(embargo_trading_days)
        metrics["embargo_rows"] = float(samples_embargo)
        if split_source == "temporal":
            metrics["split_train_dates"] = float(len(split.train_dates))
            metrics["split_calibration_dates"] = float(len(split.calibration_dates))
            metrics["split_test_dates"] = float(len(split.test_dates))
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
            "embargo_days": int(embargo_trading_days),
            "embargo_rows": int(samples_embargo),
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
                    f"feature schema not found in registry: {manifest.feature_schema_id}"
                )
            if record.feature_schema_hash != manifest.feature_schema_hash:
                raise ValueError(
                    f"feature schema hash mismatch for manifest: {manifest.feature_schema_id}"
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
                raise ValueError(f"label policy not found in registry: {manifest.label_policy_id}")
            if record.label_policy_hash != manifest.label_policy_hash:
                raise ValueError(
                    f"label policy hash mismatch for manifest: {manifest.label_policy_id}"
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

    def _build_temporal_split(self, *, aligned: pd.DataFrame) -> _TemporalSplit:
        """Split a training frame by unique trading dates, never by row count.

        - every row of the same trading date belongs to exactly one set;
        - ``embargo_days`` excludes whole trading-date groups at BOTH
          boundaries: between the train and calibration sets and between the
          calibration and test sets;
        - rows of the same date are selected through boolean masks, so the
          frame does not need to be contiguous or sorted by date;
        - a frame whose trading dates cannot be parsed fails loudly instead
          of silently falling back to a row-count split.
        """
        date_values = _extract_trading_dates(aligned)
        unique_dates = sorted(set(date_values))
        if not unique_dates:
            raise ValueError(
                "unable to parse trading dates for temporal split; refusing row-count fallback"
            )

        calibration_ratio, test_ratio = _resolve_split_ratios(self._training)
        calibration_count = max(1, int(round(len(unique_dates) * calibration_ratio)))
        test_count = max(1, int(round(len(unique_dates) * test_ratio)))
        embargo_days = max(
            0,
            int(self._training.embargo_days)
            if int(self._training.embargo_days) > 0
            else int(self._labels.horizon_days + self._settlement_lag_days),
        )

        while calibration_count + test_count + 2 * embargo_days >= len(unique_dates):
            if calibration_count >= test_count and calibration_count > 1:
                calibration_count -= 1
            elif test_count > 1:
                test_count -= 1
            else:
                break

        test_dates = unique_dates[-test_count:]
        test_start_date = test_dates[0]
        # Gap 1: whole trading-date groups between calibration and test.
        gap1 = _dates_immediately_before(unique_dates, test_start_date, embargo_days)
        calibration_candidates = [
            item for item in unique_dates if item < min(gap1 or [test_start_date])
        ]
        calibration_dates = calibration_candidates[-calibration_count:]
        if not calibration_dates:
            raise ValueError(
                "temporal split produced empty calibration set after date grouping; "
                "reduce calibration/test ratios or embargo days"
            )
        # Gap 2: whole trading-date groups between train and calibration.
        gap2 = _dates_immediately_before(
            unique_dates,
            calibration_dates[0],
            embargo_days,
        )
        embargo_dates = gap1 + gap2
        train_dates = [item for item in unique_dates if item < min(gap2 or [calibration_dates[0]])]

        if not train_dates:
            raise ValueError(
                "temporal split produced empty train set after date grouping; "
                "reduce calibration/test ratios or embargo days"
            )

        train_mask = np.asarray([item in set(train_dates) for item in date_values], dtype=bool)
        calibration_mask = np.asarray(
            [item in set(calibration_dates) for item in date_values], dtype=bool
        )
        test_mask = np.asarray([item in set(test_dates) for item in date_values], dtype=bool)
        embargo_mask = np.asarray([item in set(embargo_dates) for item in date_values], dtype=bool)
        if not train_mask.any() or not calibration_mask.any() or not test_mask.any():
            raise ValueError(
                "temporal split produced empty train/calibration/test set after date grouping"
            )

        return _TemporalSplit(
            train_mask=train_mask,
            calibration_mask=calibration_mask,
            test_mask=test_mask,
            embargo_mask=embargo_mask,
            embargo_days=len(embargo_dates),
            train_dates=[item.isoformat() for item in train_dates],
            calibration_dates=[item.isoformat() for item in calibration_dates],
            test_dates=[item.isoformat() for item in test_dates],
            embargo_dates=[item.isoformat() for item in embargo_dates],
        )


def _dates_immediately_before(
    unique_dates: Sequence[date],
    anchor: date,
    count: int,
) -> list[date]:
    """Return up to ``count`` trading dates immediately before ``anchor``."""
    if count <= 0:
        return []
    before = [item for item in unique_dates if item < anchor]
    return before[-count:]


def _extract_trading_dates(aligned: pd.DataFrame) -> list[date]:
    """Resolve one trading date per row, failing loudly when unparseable.

    Supported shapes:
    - ``DatetimeIndex`` (single-symbol bar frames);
    - ``MultiIndex`` with a ``decision_time`` or ``date`` level (panel rows);
    - any other index that ``pd.to_datetime`` can fully coerce.
    A row-count fallback is never attempted.
    """
    index = aligned.index
    raw_index: pd.Index
    if isinstance(index, pd.MultiIndex):
        if "decision_time" in index.names:
            raw_index = index.get_level_values("decision_time")
        elif "date" in index.names:
            raw_index = index.get_level_values("date")
        else:
            raise ValueError(
                "unable to parse trading dates for temporal split: "
                "MultiIndex has no decision_time/date level"
            )
    else:
        raw_index = index
    if getattr(raw_index, "dtype", None) is not None and raw_index.dtype.kind in "iu":
        raise ValueError(
            "unable to parse trading dates for temporal split: "
            "integer index (e.g. RangeIndex) carries no trading dates; "
            "refusing row-count fallback"
        )
    parsed = pd.to_datetime(raw_index, errors="coerce", format="ISO8601")
    if parsed is None or len(parsed) != len(aligned):
        raise ValueError("unable to parse trading dates for temporal split")
    dates: list[date] = []
    for item in parsed:
        if isinstance(item, pd.Timestamp) and not pd.isna(item):
            dates.append(item.date())
        else:
            raise ValueError(
                "unable to parse trading dates for temporal split; refusing row-count fallback"
            )
    return dates


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
    # Brier 按原始软标签目标计算（含 0.5 冲突标签）；二值化会抹掉冲突样本的
    # 校准信息，导致 meta 权重偏向把 0.5 预测成极端概率的分支。
    lgbm_brier = float(np.mean((lgbm - y_true) ** 2))
    xgb_brier = float(np.mean((xgb - y_true) ** 2))
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
    # 口径分离：AUC/accuracy/precision/recall/spread 仅在 {0,1} 硬标签子集上
    # 计算；Brier 用全部样本的原始软标签目标。全硬标签数据（v1 契约）下两套
    # 口径逐位一致。
    hard_mask = (y_true == 0.0) | (y_true == 1.0)
    y_hard = y_true[hard_mask].astype(float)
    meta_hard = meta[hard_mask]
    meta_pred_hard = (meta_hard >= 0.5).astype(float)
    soft_label_count = int(np.count_nonzero(~hard_mask))
    hard_label_count = int(np.count_nonzero(hard_mask))
    hard_positive_count = int(np.count_nonzero(y_hard >= 0.5))
    hard_negative_count = int(np.count_nonzero(y_hard < 0.5))

    if hard_label_count:
        accuracy = float(np.mean(meta_pred_hard == y_hard))
    else:
        accuracy = 0.0
    brier = float(np.mean((meta - y_true) ** 2))
    positive_rate = float(np.mean((meta >= 0.5).astype(float)))
    auc = _binary_auc(y_hard, meta_hard) if hard_label_count else 0.5
    auc_valid = 1.0 if (hard_positive_count > 0 and hard_negative_count > 0) else 0.0
    precision_at_k, recall_at_k = _precision_recall_at_k(
        y_true=y_hard,
        probabilities=meta_hard,
        top_ratio=precision_at_k_ratio,
    )
    positive_probs = meta[hard_mask & (y_true >= 0.5)]
    negative_probs = meta[hard_mask & (y_true < 0.5)]
    mean_prob_spread = (
        float(positive_probs.mean() - negative_probs.mean())
        if len(positive_probs) and len(negative_probs)
        else 0.0
    )
    return {
        "accuracy": round(accuracy, 6),
        "auc": round(auc, 6),
        "auc_valid": round(auc_valid, 6),
        "brier": round(brier, 6),
        "precision_at_k": round(precision_at_k, 6),
        "recall_at_k": round(recall_at_k, 6),
        "positive_rate": round(positive_rate, 6),
        "mean_prob_spread": round(mean_prob_spread, 6),
        "validation_samples": float(y_true.shape[0]),
        "soft_label_count": float(soft_label_count),
        "hard_label_count": float(hard_label_count),
        "hard_positive_count": float(hard_positive_count),
        "hard_negative_count": float(hard_negative_count),
        "meta_mean_prob": round(float(np.mean(meta)), 6),
        "lgbm_mean_prob": round(float(np.mean(lgbm)), 6),
        "xgb_mean_prob": round(float(np.mean(xgb)), 6),
    }


def _binary_auc(y_true: FloatArray, probabilities: FloatArray) -> float:
    """Rank-based AUC with average ranks for tied probabilities.

    ``np.argsort``-based rank assignment is unstable under ties: equal
    probabilities (e.g. a constant 0.5) get arbitrary ranks that depend on
    the label order, so the same probabilities can yield 0.0, 1.0 or anything
    between.  Average ranks assign tied values the mean of their rank span,
    matching ``sklearn.metrics.roc_auc_score`` (and
    ``scipy.stats.rankdata(method="average")``).
    """
    positives = int(np.sum(y_true >= 0.5))
    negatives = int(np.sum(y_true < 0.5))
    if positives == 0 or negatives == 0:
        return 0.5
    order = np.argsort(probabilities, kind="mergesort")
    # Average ranks: same value -> mean of its rank span.
    avg_ranks = np.empty_like(order, dtype=float)
    span_start = 0
    while span_start < len(order):
        span_end = span_start
        while (
            span_end + 1 < len(order)
            and probabilities[order[span_end + 1]] == probabilities[order[span_start]]
        ):
            span_end += 1
        average = (span_start + 1 + span_end + 1) / 2.0
        for pos in range(span_start, span_end + 1):
            avg_ranks[order[pos]] = average
        span_start = span_end + 1
    positive_rank_sum = float(avg_ranks[y_true >= 0.5].sum())
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
    normalized_schema = policy.schema_version.strip()
    if normalized_schema == "1":
        return _label_from_outcome_schema_v1(outcome=outcome, policy=policy)
    if normalized_schema == "2":
        return _label_from_outcome_schema_v2(outcome=outcome, policy=policy)
    raise ValueError(
        f"unsupported label policy schema_version: {policy.schema_version!r} "
        f"(policy_id={policy.label_policy_id})"
    )


def _conflict_label_for_policy(*, policy: LabelPolicyRecord, allow_soft: bool) -> float:
    """Resolve the TP/SL conflict label for one policy under schema v2 rules.

    ``bar_shape_heuristic`` was never implemented as a real heuristic and
    silently degraded to a hard 0.0 (systematically negative labels for
    high-volatility samples); under v2 it maps to the configured soft value.
    """

    normalized_policy = policy.conflict_policy.strip().lower()
    if normalized_policy == "conservative_zero":
        return 0.0
    if normalized_policy in ("soft_label", "bar_shape_heuristic"):
        if allow_soft:
            return float(max(0.0, min(1.0, policy.conflict_soft_label_value)))
        return 0.0
    raise ValueError(
        f"unsupported conflict_policy {policy.conflict_policy!r} for label policy "
        f"schema v2 (policy_id={policy.label_policy_id})"
    )


def _label_from_outcome_schema_v1(
    *,
    outcome: OutcomeRecord,
    policy: LabelPolicyRecord,
) -> float | None:
    take_profit_hit = outcome.max_favorable_excursion is not None and float(
        outcome.max_favorable_excursion
    ) >= float(policy.take_profit_pct)
    stop_loss_hit = outcome.max_adverse_excursion is not None and float(
        outcome.max_adverse_excursion
    ) <= -float(policy.stop_loss_pct)
    if take_profit_hit and stop_loss_hit:
        # v1 历史口径逐位保留：仅 soft_label 给软值，其余一律 0.0。
        normalized_policy = policy.conflict_policy.strip().lower()
        if normalized_policy == "soft_label":
            return float(max(0.0, min(1.0, policy.conflict_soft_label_value)))
        return 0.0
    return _non_conflict_label(outcome=outcome, policy=policy)


def _label_from_outcome_schema_v2(
    *,
    outcome: OutcomeRecord,
    policy: LabelPolicyRecord,
) -> float | None:
    take_profit_hit = outcome.max_favorable_excursion is not None and float(
        outcome.max_favorable_excursion
    ) >= float(policy.take_profit_pct)
    stop_loss_hit = outcome.max_adverse_excursion is not None and float(
        outcome.max_adverse_excursion
    ) <= -float(policy.stop_loss_pct)
    if take_profit_hit and stop_loss_hit:
        return _conflict_label_for_policy(policy=policy, allow_soft=True)
    return _non_conflict_label(outcome=outcome, policy=policy)


def _non_conflict_label(
    *,
    outcome: OutcomeRecord,
    policy: LabelPolicyRecord,
) -> float | None:
    take_profit_hit = outcome.max_favorable_excursion is not None and float(
        outcome.max_favorable_excursion
    ) >= float(policy.take_profit_pct)
    stop_loss_hit = outcome.max_adverse_excursion is not None and float(
        outcome.max_adverse_excursion
    ) <= -float(policy.stop_loss_pct)
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
