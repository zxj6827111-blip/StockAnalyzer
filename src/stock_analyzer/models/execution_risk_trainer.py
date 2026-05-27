"""Execution-risk sidecar trainer built on top of sample-store labels."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime

import numpy as np
import numpy.typing as npt

from stock_analyzer.learning.execution_risk_labels import (
    ExecutionRiskDataset,
    ExecutionRiskLabelBuilder,
    ExecutionRiskLabelingConfig,
    ExecutionRiskTarget,
)
from stock_analyzer.learning.sample_schema import MaturityStatus
from stock_analyzer.learning.sample_store import SampleStore
from stock_analyzer.models.calibration import IsotonicCalibrator
from stock_analyzer.models.execution_risk_artifact import ExecutionRiskArtifact
from stock_analyzer.models.fallback import LogisticProbModel

FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class ExecutionRiskTrainingConfig:
    min_samples_per_target: int = 24
    calibration_ratio: float = 0.2
    test_ratio: float = 0.2
    learning_rate: float = 0.05
    epochs: int = 240
    l2: float = 1e-3
    seed: int = 42


@dataclass(slots=True)
class ExecutionRiskTrainResult:
    artifact: ExecutionRiskArtifact
    target_metrics: dict[str, dict[str, float]]
    target_row_counts: dict[str, int]
    trained_targets: list[str]
    skipped_targets: dict[str, str]

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact": self.artifact.to_dict(),
            "target_metrics": {key: dict(value) for key, value in self.target_metrics.items()},
            "target_row_counts": dict(self.target_row_counts),
            "trained_targets": list(self.trained_targets),
            "skipped_targets": dict(self.skipped_targets),
        }


def diagnose_execution_risk_dataset(
    *,
    dataset: ExecutionRiskDataset,
    config: ExecutionRiskTrainingConfig | None = None,
) -> dict[str, object]:
    """Explain whether an execution-risk dataset can train per-target models."""

    resolved_config = config or ExecutionRiskTrainingConfig()
    min_samples = max(4, int(resolved_config.min_samples_per_target))
    target_row_counts: dict[str, int] = {}
    target_class_counts: dict[str, dict[str, int]] = {}
    skipped_targets: dict[str, str] = {}
    trainable_targets: list[str] = []

    for target_name in _ordered_targets(dataset=dataset):
        rows = dataset.rows_for_target(target_name)
        target_row_counts[target_name] = len(rows)
        labels = [float(row.targets[target_name]) for row in rows]
        positive_count = sum(1 for value in labels if value >= 0.5)
        negative_count = len(labels) - positive_count
        target_class_counts[target_name] = {
            "negative": negative_count,
            "positive": positive_count,
        }
        if len(rows) < min_samples:
            skipped_targets[target_name] = "insufficient_samples"
            continue
        if positive_count <= 0 or negative_count <= 0:
            skipped_targets[target_name] = "single_class_target"
            continue
        split = _build_target_split(
            total_rows=len(rows),
            calibration_ratio=float(resolved_config.calibration_ratio),
            test_ratio=float(resolved_config.test_ratio),
        )
        split_lengths = {
            "train": len(rows[split.train_slice]),
            "calibration": len(rows[split.calibration_slice]),
            "test": len(rows[split.test_slice]),
        }
        if min(split_lengths.values()) <= 0:
            skipped_targets[target_name] = "empty_split"
            continue
        train_labels = labels[split.train_slice]
        train_positive_count = sum(1 for value in train_labels if value >= 0.5)
        train_negative_count = len(train_labels) - train_positive_count
        if train_positive_count <= 0 or train_negative_count <= 0:
            skipped_targets[target_name] = "single_class_train_split"
            continue
        trainable_targets.append(target_name)

    return {
        "dataset_id": dataset.dataset_id,
        "row_count": dataset.row_count,
        "source_snapshot_count": dataset.source_snapshot_count,
        "feature_count": len(dataset.feature_names),
        "requested_maturity_statuses": list(dataset.requested_maturity_statuses),
        "skipped_missing_outcome": dataset.skipped_missing_outcome,
        "skipped_by_maturity": dataset.skipped_by_maturity,
        "skipped_missing_targets": dataset.skipped_missing_targets,
        "target_coverage": dict(dataset.target_coverage),
        "target_row_counts": target_row_counts,
        "target_class_counts": target_class_counts,
        "trainable_targets": sorted(trainable_targets),
        "skipped_targets": skipped_targets,
        "min_samples_per_target": min_samples,
        "can_train": bool(dataset.rows and dataset.feature_names and trainable_targets),
    }


@dataclass(frozen=True, slots=True)
class _TargetSplit:
    train_slice: slice
    calibration_slice: slice
    test_slice: slice


class ExecutionRiskTrainer:
    """Train per-target execution-risk classifiers from protocol-bound samples."""

    def __init__(
        self,
        *,
        config: ExecutionRiskTrainingConfig | None = None,
        labeling: ExecutionRiskLabelingConfig | None = None,
    ) -> None:
        self._config = config or ExecutionRiskTrainingConfig()
        self._labeling = labeling or ExecutionRiskLabelingConfig()

    def train_from_sample_store(
        self,
        *,
        store: SampleStore,
        snapshot_ids: Sequence[str] | None = None,
        maturity_statuses: Sequence[MaturityStatus | str] | None = None,
        max_rows: int | None = None,
        now: datetime | None = None,
    ) -> ExecutionRiskTrainResult:
        dataset = self.build_dataset_from_sample_store(
            store=store,
            snapshot_ids=snapshot_ids,
            maturity_statuses=maturity_statuses,
            max_rows=max_rows,
            now=now,
        )
        return self.train(dataset=dataset)

    def build_dataset_from_sample_store(
        self,
        *,
        store: SampleStore,
        snapshot_ids: Sequence[str] | None = None,
        maturity_statuses: Sequence[MaturityStatus | str] | None = None,
        max_rows: int | None = None,
        now: datetime | None = None,
    ) -> ExecutionRiskDataset:
        return ExecutionRiskLabelBuilder(
            store=store,
            labeling=self._labeling,
        ).build_dataset(
            snapshot_ids=snapshot_ids,
            maturity_statuses=maturity_statuses,
            max_rows=max_rows,
            now=now,
        )

    @property
    def config(self) -> ExecutionRiskTrainingConfig:
        return self._config

    def train(self, *, dataset: ExecutionRiskDataset) -> ExecutionRiskTrainResult:
        if not dataset.rows:
            raise ValueError("execution-risk dataset is empty")
        if not dataset.feature_names:
            raise ValueError("execution-risk dataset has no feature columns")

        feature_names = list(dataset.feature_names)
        target_models: dict[str, dict[str, object]] = {}
        target_metrics: dict[str, dict[str, float]] = {}
        target_row_counts: dict[str, int] = {}
        skipped_targets: dict[str, str] = {}

        for target_name in _ordered_targets(dataset=dataset):
            rows = dataset.rows_for_target(target_name)
            target_row_counts[target_name] = len(rows)
            if len(rows) < max(4, int(self._config.min_samples_per_target)):
                skipped_targets[target_name] = "insufficient_samples"
                continue

            x = np.asarray(
                [
                    [float(row.feature_vector.get(name, 0.0)) for name in feature_names]
                    for row in rows
                ],
                dtype=float,
            )
            y = np.asarray([float(row.targets[target_name]) for row in rows], dtype=float)
            if len(np.unique(y)) < 2:
                skipped_targets[target_name] = "single_class_target"
                continue

            split = _build_target_split(
                total_rows=len(rows),
                calibration_ratio=float(self._config.calibration_ratio),
                test_ratio=float(self._config.test_ratio),
            )
            x_train = x[split.train_slice]
            y_train = y[split.train_slice]
            x_calibration = x[split.calibration_slice]
            y_calibration = y[split.calibration_slice]
            x_test = x[split.test_slice]
            y_test = y[split.test_slice]
            if min(len(x_train), len(x_calibration), len(x_test)) <= 0:
                skipped_targets[target_name] = "empty_split"
                continue
            if len(np.unique(y_train)) < 2:
                skipped_targets[target_name] = "single_class_train_split"
                continue

            model = LogisticProbModel(
                learning_rate=float(self._config.learning_rate),
                epochs=int(self._config.epochs),
                l2=float(self._config.l2),
                seed=int(self._config.seed),
            )
            model.fit(x_train, y_train)

            calibration_scores = model.predict_proba(x_calibration)
            calibrator = IsotonicCalibrator()
            calibrator.fit(calibration_scores, y_calibration)

            test_scores = calibrator.predict(model.predict_proba(x_test))
            metrics = _evaluate_binary_metrics(y_true=y_test, y_prob=test_scores)
            metrics.update(
                {
                    "samples_total": float(len(rows)),
                    "samples_train": float(len(x_train)),
                    "samples_calibration": float(len(x_calibration)),
                    "samples_test": float(len(x_test)),
                }
            )
            target_metrics[target_name] = metrics
            target_models[target_name] = {
                "feature_names": list(feature_names),
                "model": model.to_dict(),
                "calibrator": calibrator.to_dict(),
                "metrics": dict(metrics),
            }

        if not target_models:
            raise ValueError("execution-risk training produced no trainable targets")

        artifact = ExecutionRiskArtifact.create(
            dataset_id=dataset.dataset_id,
            feature_names=feature_names,
            target_models=target_models,
            training_summary={
                "row_count": dataset.row_count,
                "source_snapshot_count": dataset.source_snapshot_count,
                "skipped_missing_outcome": dataset.skipped_missing_outcome,
                "skipped_by_maturity": dataset.skipped_by_maturity,
                "skipped_missing_targets": dataset.skipped_missing_targets,
                "target_coverage": dict(dataset.target_coverage),
            },
            metadata={
                "requested_maturity_statuses": list(dataset.requested_maturity_statuses),
                "labeling": asdict(self._labeling),
                "trainer_config": asdict(self._config),
            },
        )
        return ExecutionRiskTrainResult(
            artifact=artifact,
            target_metrics=target_metrics,
            target_row_counts=target_row_counts,
            trained_targets=sorted(target_models.keys()),
            skipped_targets=skipped_targets,
        )


def _ordered_targets(*, dataset: ExecutionRiskDataset) -> list[str]:
    preferred = [
        ExecutionRiskTarget.CAN_FILL.value,
        ExecutionRiskTarget.LIKELY_SLIPPAGE_HIGH.value,
        ExecutionRiskTarget.SIM_BROKER_DIVERGENCE_RISK.value,
        ExecutionRiskTarget.RECONCILE_MISMATCH_RISK.value,
    ]
    present = set(dataset.target_coverage.keys())
    ordered = [item for item in preferred if item in present]
    extras = sorted(present.difference(ordered))
    return ordered + extras


def _build_target_split(
    *,
    total_rows: int,
    calibration_ratio: float,
    test_ratio: float,
) -> _TargetSplit:
    if total_rows < 3:
        raise ValueError("execution-risk target split requires at least 3 rows")
    calibration_count = max(1, int(round(total_rows * max(0.0, calibration_ratio))))
    test_count = max(1, int(round(total_rows * max(0.0, test_ratio))))
    if calibration_count + test_count >= total_rows:
        calibration_count = max(1, calibration_count - 1)
        if calibration_count + test_count >= total_rows:
            test_count = max(1, test_count - 1)
    train_end = max(1, total_rows - calibration_count - test_count)
    calibration_start = train_end
    calibration_end = min(total_rows - 1, calibration_start + calibration_count)
    test_start = calibration_end
    if test_start >= total_rows:
        test_start = total_rows - 1
    return _TargetSplit(
        train_slice=slice(0, train_end),
        calibration_slice=slice(calibration_start, test_start),
        test_slice=slice(test_start, total_rows),
    )


def _evaluate_binary_metrics(*, y_true: FloatArray, y_prob: FloatArray) -> dict[str, float]:
    clipped = np.clip(y_prob.astype(float), 1e-6, 1.0 - 1e-6)
    y = y_true.astype(float)
    loss = float(-np.mean(y * np.log(clipped) + (1.0 - y) * np.log(1.0 - clipped)))
    brier = float(np.mean((clipped - y) ** 2))
    accuracy = float(np.mean((clipped >= 0.5) == (y >= 0.5)))
    auc = _binary_auc(y_true=y, y_prob=clipped)
    return {
        "logloss": round(loss, 6),
        "brier": round(brier, 6),
        "accuracy": round(accuracy, 6),
        "auc": round(auc, 6),
        "positive_rate": round(float(np.mean(y)), 6),
        "avg_probability": round(float(np.mean(clipped)), 6),
    }


def _binary_auc(*, y_true: FloatArray, y_prob: FloatArray) -> float:
    positives = y_true >= 0.5
    negatives = ~positives
    pos_count = int(np.sum(positives))
    neg_count = int(np.sum(negatives))
    if pos_count == 0 or neg_count == 0:
        return 0.5
    order = np.argsort(y_prob)
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(y_prob) + 1, dtype=float)
    pos_ranks = float(np.sum(ranks[positives]))
    auc = (pos_ranks - (pos_count * (pos_count + 1) / 2.0)) / (pos_count * neg_count)
    if not math.isfinite(auc):
        return 0.5
    return max(0.0, min(1.0, float(auc)))
