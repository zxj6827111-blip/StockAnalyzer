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
    resolve_execution_risk_targets,
)
from stock_analyzer.learning.sample_schema import MaturityStatus, OutcomeRecord
from stock_analyzer.learning.sample_store import SampleStore
from stock_analyzer.models.calibration import IsotonicCalibrator
from stock_analyzer.models.execution_risk_artifact import ExecutionRiskArtifact
from stock_analyzer.models.fallback import LogisticProbModel

FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class ExecutionRiskTrainingConfig:
    min_samples_per_target: int = 24
    min_class_samples_per_target: int = 3
    calibration_ratio: float = 0.2
    test_ratio: float = 0.2
    learning_rate: float = 0.05
    epochs: int = 240
    l2: float = 1e-3
    seed: int = 42


@dataclass(frozen=True, slots=True)
class ExecutionRiskQualificationConfig:
    min_samples_per_target: int = 500
    min_class_samples_per_target: int = 100
    min_label_coverage_ratio: float = 0.10
    min_auc: float = 0.65
    max_ece: float = 0.10
    min_baseline_improvement_ratio: float = 0.05


@dataclass(slots=True)
class ExecutionRiskTrainResult:
    artifact: ExecutionRiskArtifact
    target_metrics: dict[str, dict[str, float]]
    target_row_counts: dict[str, int]
    target_split_strategies: dict[str, str]
    trained_targets: list[str]
    skipped_targets: dict[str, str]

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact": self.artifact.to_dict(),
            "target_metrics": {key: dict(value) for key, value in self.target_metrics.items()},
            "target_row_counts": dict(self.target_row_counts),
            "target_split_strategies": dict(self.target_split_strategies),
            "trained_targets": list(self.trained_targets),
            "skipped_targets": dict(self.skipped_targets),
        }


def diagnose_execution_risk_dataset(
    *,
    dataset: ExecutionRiskDataset,
    config: ExecutionRiskTrainingConfig | None = None,
    outcomes: Sequence[OutcomeRecord] | None = None,
    labeling: ExecutionRiskLabelingConfig | None = None,
) -> dict[str, object]:
    """Explain whether an execution-risk dataset can train per-target models."""

    resolved_config = config or ExecutionRiskTrainingConfig()
    min_samples = max(4, int(resolved_config.min_samples_per_target))
    min_class_samples = max(1, int(resolved_config.min_class_samples_per_target))
    target_row_counts: dict[str, int] = {}
    target_class_counts: dict[str, dict[str, int]] = {}
    target_trainability: dict[str, dict[str, object]] = {}
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
        detail: dict[str, object] = {
            "rows": len(rows),
            "positive": positive_count,
            "negative": negative_count,
            "min_samples_per_target": min_samples,
            "min_class_samples_per_target": min_class_samples,
            "sample_deficit": max(0, min_samples - len(rows)),
            "positive_deficit": max(0, min_class_samples - positive_count),
            "negative_deficit": max(0, min_class_samples - negative_count),
            "minority_count": min(positive_count, negative_count),
            "minority_deficit": max(0, min_class_samples - min(positive_count, negative_count)),
            "split_lengths": {},
            "split_class_counts": {},
            "split_strategy": "",
            "train_positive": 0,
            "train_negative": 0,
            "calibration_positive": 0,
            "calibration_negative": 0,
            "test_positive": 0,
            "test_negative": 0,
            "train_minority_count": 0,
            "train_minority_deficit": min_class_samples,
        }
        if len(rows) < min_samples:
            skipped_targets[target_name] = "insufficient_samples"
            detail["skipped_reason"] = "insufficient_samples"
            target_trainability[target_name] = detail
            continue
        if positive_count <= 0 or negative_count <= 0:
            skipped_targets[target_name] = "single_class_target"
            detail["skipped_reason"] = "single_class_target"
            target_trainability[target_name] = detail
            continue
        if min(positive_count, negative_count) < min_class_samples:
            skipped_targets[target_name] = "minority_class_too_small"
            detail["skipped_reason"] = "minority_class_too_small"
            target_trainability[target_name] = detail
            continue
        split = _build_target_split(
            total_rows=len(rows),
            calibration_ratio=float(resolved_config.calibration_ratio),
            test_ratio=float(resolved_config.test_ratio),
            labels=labels,
        )
        split_lengths = {
            "train": len(split.train_indices),
            "calibration": len(split.calibration_indices),
            "test": len(split.test_indices),
        }
        detail["split_lengths"] = split_lengths
        detail["split_strategy"] = split.strategy
        split_class_counts = {
            "train": _split_class_counts(labels=labels, indices=split.train_indices),
            "calibration": _split_class_counts(
                labels=labels,
                indices=split.calibration_indices,
            ),
            "test": _split_class_counts(labels=labels, indices=split.test_indices),
        }
        detail["split_class_counts"] = split_class_counts
        if min(split_lengths.values()) <= 0:
            skipped_targets[target_name] = "empty_split"
            detail["skipped_reason"] = "empty_split"
            target_trainability[target_name] = detail
            continue
        train_counts = split_class_counts["train"]
        calibration_counts = split_class_counts["calibration"]
        test_counts = split_class_counts["test"]
        train_positive_count = train_counts["positive"]
        train_negative_count = train_counts["negative"]
        detail["train_positive"] = train_positive_count
        detail["train_negative"] = train_negative_count
        detail["calibration_positive"] = calibration_counts["positive"]
        detail["calibration_negative"] = calibration_counts["negative"]
        detail["test_positive"] = test_counts["positive"]
        detail["test_negative"] = test_counts["negative"]
        detail["train_minority_count"] = min(train_positive_count, train_negative_count)
        detail["train_minority_deficit"] = max(
            0,
            min_class_samples - min(train_positive_count, train_negative_count),
        )
        if train_positive_count <= 0 or train_negative_count <= 0:
            skipped_targets[target_name] = "single_class_train_split"
            detail["skipped_reason"] = "single_class_train_split"
            target_trainability[target_name] = detail
            continue
        if calibration_counts["positive"] <= 0 or calibration_counts["negative"] <= 0:
            skipped_targets[target_name] = "single_class_calibration_split"
            detail["skipped_reason"] = "single_class_calibration_split"
            target_trainability[target_name] = detail
            continue
        if test_counts["positive"] <= 0 or test_counts["negative"] <= 0:
            skipped_targets[target_name] = "single_class_test_split"
            detail["skipped_reason"] = "single_class_test_split"
            target_trainability[target_name] = detail
            continue
        detail["skipped_reason"] = ""
        target_trainability[target_name] = detail
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
        "target_trainability": target_trainability,
        "outcome_coverage": _diagnose_outcome_coverage(
            outcomes=outcomes or [],
            requested_maturity_statuses=dataset.requested_maturity_statuses,
            labeling=labeling,
        ),
        "trainable_targets": sorted(trainable_targets),
        "skipped_targets": skipped_targets,
        "min_samples_per_target": min_samples,
        "min_class_samples_per_target": min_class_samples,
        "can_train": bool(dataset.rows and dataset.feature_names and trainable_targets),
    }


@dataclass(frozen=True, slots=True)
class _TargetSplit:
    train_indices: npt.NDArray[np.int64]
    calibration_indices: npt.NDArray[np.int64]
    test_indices: npt.NDArray[np.int64]
    strategy: str = "chronological"


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

    @property
    def labeling(self) -> ExecutionRiskLabelingConfig:
        return self._labeling

    def train(self, *, dataset: ExecutionRiskDataset) -> ExecutionRiskTrainResult:
        if not dataset.rows:
            raise ValueError("execution-risk dataset is empty")
        if not dataset.feature_names:
            raise ValueError("execution-risk dataset has no feature columns")

        feature_names = list(dataset.feature_names)
        target_models: dict[str, dict[str, object]] = {}
        target_metrics: dict[str, dict[str, float]] = {}
        target_row_counts: dict[str, int] = {}
        target_split_strategies: dict[str, str] = {}
        skipped_targets: dict[str, str] = {}
        min_class_samples = max(1, int(self._config.min_class_samples_per_target))

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
            positive_count = int(np.sum(y >= 0.5))
            negative_count = int(len(y) - positive_count)
            if min(positive_count, negative_count) < min_class_samples:
                skipped_targets[target_name] = "minority_class_too_small"
                continue

            split = _build_target_split(
                total_rows=len(rows),
                calibration_ratio=float(self._config.calibration_ratio),
                test_ratio=float(self._config.test_ratio),
                labels=y,
            )
            x_train = x[split.train_indices]
            y_train = y[split.train_indices]
            x_calibration = x[split.calibration_indices]
            y_calibration = y[split.calibration_indices]
            x_test = x[split.test_indices]
            y_test = y[split.test_indices]
            if min(len(x_train), len(x_calibration), len(x_test)) <= 0:
                skipped_targets[target_name] = "empty_split"
                continue
            if len(np.unique(y_train)) < 2:
                skipped_targets[target_name] = "single_class_train_split"
                continue
            if len(np.unique(y_calibration)) < 2:
                skipped_targets[target_name] = "single_class_calibration_split"
                continue
            if len(np.unique(y_test)) < 2:
                skipped_targets[target_name] = "single_class_test_split"
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
            target_split_strategies[target_name] = split.strategy
            target_models[target_name] = {
                "feature_names": list(feature_names),
                "model": model.to_dict(),
                "calibrator": calibrator.to_dict(),
                "metrics": dict(metrics),
                "split_strategy": split.strategy,
            }

        if not target_models:
            raise ValueError("execution-risk training produced no trainable targets")

        qualification = qualify_execution_risk_artifact(
            dataset=dataset,
            target_models=target_models,
        )
        artifact = ExecutionRiskArtifact.create(
            dataset_id=dataset.dataset_id,
            feature_names=feature_names,
            target_models=target_models,
            qualification_status=str(qualification["status"]),
            qualification=qualification,
            training_summary={
                "row_count": dataset.row_count,
                "source_snapshot_count": dataset.source_snapshot_count,
                "skipped_missing_outcome": dataset.skipped_missing_outcome,
                "skipped_by_maturity": dataset.skipped_by_maturity,
                "skipped_missing_targets": dataset.skipped_missing_targets,
                "target_coverage": dict(dataset.target_coverage),
                "target_split_strategies": dict(target_split_strategies),
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
            target_split_strategies=target_split_strategies,
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


def _diagnose_outcome_coverage(
    *,
    outcomes: Sequence[OutcomeRecord],
    requested_maturity_statuses: Sequence[str],
    labeling: ExecutionRiskLabelingConfig | None,
) -> dict[str, object]:
    requested = {
        str(item).strip().lower()
        for item in requested_maturity_statuses
        if str(item).strip()
    }
    maturity_counts: dict[str, int] = {}
    field_coverage_by_maturity: dict[str, dict[str, int]] = {}
    target_coverage_by_maturity: dict[str, dict[str, int]] = {}
    requested_field_coverage: dict[str, int] = {
        "outcomes": 0,
        "execution_fill_ratio": 0,
        "realized_slippage_bp": 0,
        "reconcile_status": 0,
        "sim_vs_broker_diff": 0,
    }
    requested_target_coverage: dict[str, int] = {}
    outside_requested_target_coverage: dict[str, int] = {}

    for outcome in outcomes:
        maturity = outcome.maturity_status.value
        maturity_counts[maturity] = maturity_counts.get(maturity, 0) + 1
        field_counts = field_coverage_by_maturity.setdefault(
            maturity,
            {
                "outcomes": 0,
                "execution_fill_ratio": 0,
                "realized_slippage_bp": 0,
                "reconcile_status": 0,
                "sim_vs_broker_diff": 0,
            },
        )
        field_counts["outcomes"] += 1
        if outcome.execution_fill_ratio is not None:
            field_counts["execution_fill_ratio"] += 1
        if outcome.realized_slippage_bp is not None:
            field_counts["realized_slippage_bp"] += 1
        if str(outcome.reconcile_status or "").strip():
            field_counts["reconcile_status"] += 1
        if outcome.sim_vs_broker_diff is not None:
            field_counts["sim_vs_broker_diff"] += 1

        targets = resolve_execution_risk_targets(outcome=outcome, labeling=labeling)
        maturity_target_counts = target_coverage_by_maturity.setdefault(maturity, {})
        for target_name in targets:
            maturity_target_counts[target_name] = maturity_target_counts.get(target_name, 0) + 1

        if maturity in requested:
            requested_field_coverage["outcomes"] += 1
            for field_name in (
                "execution_fill_ratio",
                "realized_slippage_bp",
                "reconcile_status",
                "sim_vs_broker_diff",
            ):
                if _outcome_has_field(outcome, field_name):
                    requested_field_coverage[field_name] += 1
            for target_name in targets:
                requested_target_coverage[target_name] = (
                    requested_target_coverage.get(target_name, 0) + 1
                )
        else:
            for target_name in targets:
                outside_requested_target_coverage[target_name] = (
                    outside_requested_target_coverage.get(target_name, 0) + 1
                )

    return {
        "total_outcomes": len(outcomes),
        "requested_maturity_statuses": sorted(requested),
        "maturity_counts": maturity_counts,
        "field_coverage_by_maturity": field_coverage_by_maturity,
        "target_coverage_by_maturity": target_coverage_by_maturity,
        "requested_field_coverage": requested_field_coverage,
        "requested_target_coverage": requested_target_coverage,
        "outside_requested_target_coverage": outside_requested_target_coverage,
    }


def _outcome_has_field(outcome: OutcomeRecord, field_name: str) -> bool:
    if field_name == "execution_fill_ratio":
        return outcome.execution_fill_ratio is not None
    if field_name == "realized_slippage_bp":
        return outcome.realized_slippage_bp is not None
    if field_name == "reconcile_status":
        return bool(str(outcome.reconcile_status or "").strip())
    if field_name == "sim_vs_broker_diff":
        return outcome.sim_vs_broker_diff is not None
    return False


def _build_target_split(
    *,
    total_rows: int,
    calibration_ratio: float,
    test_ratio: float,
    labels: Sequence[float] | FloatArray | None = None,
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
    split = _TargetSplit(
        train_indices=np.arange(0, train_end, dtype=np.int64),
        calibration_indices=np.arange(calibration_start, test_start, dtype=np.int64),
        test_indices=np.arange(test_start, total_rows, dtype=np.int64),
    )
    if labels is None:
        return split
    return _build_balanced_stratified_split(
        split=split,
        labels=labels,
        total_rows=total_rows,
    )


def _build_balanced_stratified_split(
    *,
    split: _TargetSplit,
    labels: Sequence[float] | FloatArray,
    total_rows: int,
) -> _TargetSplit:
    label_array = np.asarray(labels, dtype=float)
    if label_array.shape[0] != total_rows:
        raise ValueError("execution-risk target split labels must match row count")
    if len(np.unique(label_array)) < 2:
        return split
    if _all_splits_binary(split=split, labels=label_array):
        return split

    positive_indices = np.asarray(
        [index for index, value in enumerate(label_array) if float(value) >= 0.5],
        dtype=np.int64,
    )
    negative_indices = np.asarray(
        [index for index, value in enumerate(label_array) if float(value) < 0.5],
        dtype=np.int64,
    )
    split_lengths = {
        "train": len(split.train_indices),
        "calibration": len(split.calibration_indices),
        "test": len(split.test_indices),
    }
    if min(len(positive_indices), len(negative_indices)) < len(split_lengths):
        return split

    if min(split_lengths.values()) < 2:
        return split
    positive_is_minority = len(positive_indices) <= len(negative_indices)
    minority_count = len(positive_indices) if positive_is_minority else len(negative_indices)
    minority_counts = _allocate_minority_counts(
        class_count=minority_count,
        split_lengths=split_lengths,
    )
    if minority_counts is None:
        return split
    majority_counts = {
        name: split_lengths[name] - minority_counts[name]
        for name in split_lengths
    }
    if any(count <= 0 for count in majority_counts.values()):
        return split
    if positive_is_minority:
        positive_counts = minority_counts
        negative_counts = majority_counts
    else:
        positive_counts = majority_counts
        negative_counts = minority_counts

    split_indices: dict[str, list[int]] = {
        "train": [],
        "calibration": [],
        "test": [],
    }
    positive_offset = 0
    negative_offset = 0
    for split_name in ("train", "calibration", "test"):
        positive_count = positive_counts[split_name]
        negative_count = negative_counts[split_name]
        split_indices[split_name].extend(
            int(index)
            for index in positive_indices[positive_offset : positive_offset + positive_count]
        )
        split_indices[split_name].extend(
            int(index)
            for index in negative_indices[negative_offset : negative_offset + negative_count]
        )
        positive_offset += positive_count
        negative_offset += negative_count
    return _TargetSplit(
        train_indices=np.asarray(sorted(split_indices["train"]), dtype=np.int64),
        calibration_indices=np.asarray(sorted(split_indices["calibration"]), dtype=np.int64),
        test_indices=np.asarray(sorted(split_indices["test"]), dtype=np.int64),
        strategy="stratified_class_balanced",
    )


def _all_splits_binary(*, split: _TargetSplit, labels: FloatArray) -> bool:
    return all(
        len(np.unique(labels[indices])) >= 2
        for indices in (split.train_indices, split.calibration_indices, split.test_indices)
    )


def _allocate_minority_counts(
    *,
    class_count: int,
    split_lengths: dict[str, int],
) -> dict[str, int] | None:
    split_names = ("train", "calibration", "test")
    if class_count < len(split_names):
        return None
    counts = {name: 1 for name in split_names}
    remaining = class_count - len(split_names)
    total_rows = sum(split_lengths.values())
    fractional_targets = {
        name: (class_count * (split_lengths[name] / total_rows)) - counts[name]
        for name in split_names
    }
    while remaining > 0:
        candidates = [
            name
            for name in split_names
            if counts[name] < split_lengths[name] - 1
        ]
        if not candidates:
            return None
        selected = max(candidates, key=lambda name: (fractional_targets[name], split_lengths[name]))
        counts[selected] += 1
        fractional_targets[selected] -= 1.0
        remaining -= 1
    if any(counts[name] > split_lengths[name] for name in split_names):
        return None
    return counts


def _split_class_counts(
    *,
    labels: Sequence[float] | FloatArray,
    indices: npt.NDArray[np.int64],
) -> dict[str, int]:
    label_array = np.asarray(labels, dtype=float)
    selected = label_array[indices]
    positive = int(np.sum(selected >= 0.5))
    return {
        "negative": int(len(selected) - positive),
        "positive": positive,
    }


def _evaluate_binary_metrics(*, y_true: FloatArray, y_prob: FloatArray) -> dict[str, float]:
    clipped = np.clip(y_prob.astype(float), 1e-6, 1.0 - 1e-6)
    y = y_true.astype(float)
    loss = float(-np.mean(y * np.log(clipped) + (1.0 - y) * np.log(1.0 - clipped)))
    brier = float(np.mean((clipped - y) ** 2))
    accuracy = float(np.mean((clipped >= 0.5) == (y >= 0.5)))
    auc = _binary_auc(y_true=y, y_prob=clipped)
    positive_rate = float(np.mean(y))
    baseline = np.full_like(y, positive_rate, dtype=float)
    baseline_clipped = np.clip(baseline, 1e-6, 1.0 - 1e-6)
    baseline_logloss = float(
        -np.mean(y * np.log(baseline_clipped) + (1.0 - y) * np.log(1.0 - baseline_clipped))
    )
    baseline_brier = float(np.mean((baseline_clipped - y) ** 2))
    ece = _expected_calibration_error(y_true=y, y_prob=clipped)
    return {
        "logloss": round(loss, 6),
        "brier": round(brier, 6),
        "accuracy": round(accuracy, 6),
        "auc": round(auc, 6),
        "ece": round(ece, 6),
        "baseline_logloss": round(baseline_logloss, 6),
        "baseline_brier": round(baseline_brier, 6),
        "positive_rate": round(positive_rate, 6),
        "avg_probability": round(float(np.mean(clipped)), 6),
    }


def qualify_execution_risk_artifact(
    *,
    dataset: ExecutionRiskDataset,
    target_models: dict[str, dict[str, object]],
    config: ExecutionRiskQualificationConfig | None = None,
) -> dict[str, object]:
    """Return an auditable active-rerank qualification decision."""

    resolved = config or ExecutionRiskQualificationConfig()
    target_checks: dict[str, dict[str, object]] = {}
    blockers: list[str] = []
    source_count = max(1, int(dataset.source_snapshot_count))
    for target_name, model_payload in sorted(target_models.items()):
        rows = int(dataset.target_coverage.get(target_name, 0))
        labels = [
            float(row.targets[target_name])
            for row in dataset.rows_for_target(target_name)
        ]
        positive = sum(1 for value in labels if value >= 0.5)
        negative = len(labels) - positive
        coverage = rows / source_count
        raw_metrics = model_payload.get("metrics", {})
        metrics = raw_metrics if isinstance(raw_metrics, dict) else {}
        auc = _finite_metric(metrics.get("auc"))
        ece = _finite_metric(metrics.get("ece"))
        brier = _finite_metric(metrics.get("brier"))
        logloss = _finite_metric(metrics.get("logloss"))
        baseline_brier = _finite_metric(metrics.get("baseline_brier"))
        baseline_logloss = _finite_metric(metrics.get("baseline_logloss"))
        brier_improvement = _relative_improvement(baseline_brier, brier)
        logloss_improvement = _relative_improvement(baseline_logloss, logloss)
        checks = {
            "samples": rows >= resolved.min_samples_per_target,
            "classes": min(positive, negative) >= resolved.min_class_samples_per_target,
            "coverage": coverage >= resolved.min_label_coverage_ratio,
            "finite_metrics": all(
                value is not None
                for value in (auc, ece, brier, logloss, baseline_brier, baseline_logloss)
            ),
            "auc": auc is not None and auc >= resolved.min_auc,
            "ece": ece is not None and ece <= resolved.max_ece,
            "brier_improvement": (
                brier_improvement is not None
                and brier_improvement >= resolved.min_baseline_improvement_ratio
            ),
            "logloss_improvement": (
                logloss_improvement is not None
                and logloss_improvement >= resolved.min_baseline_improvement_ratio
            ),
        }
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            blockers.extend(f"{target_name}:{name}" for name in failed)
        target_checks[target_name] = {
            "rows": rows,
            "positive": positive,
            "negative": negative,
            "coverage_ratio": round(coverage, 6),
            "checks": checks,
            "failed_checks": failed,
            "metrics": dict(metrics),
            "brier_improvement_ratio": brier_improvement,
            "logloss_improvement_ratio": logloss_improvement,
        }

    if not target_models:
        blockers.append("no_trained_targets")
    status = "qualified" if not blockers else "shadow_only"
    return {
        "status": status,
        "active_rerank_allowed": status == "qualified",
        "blockers": blockers,
        "thresholds": asdict(resolved),
        "targets": target_checks,
    }


def _expected_calibration_error(
    *,
    y_true: FloatArray,
    y_prob: FloatArray,
    bins: int = 10,
) -> float:
    total = max(1, len(y_true))
    error = 0.0
    for idx in range(max(2, bins)):
        low = idx / bins
        high = (idx + 1) / bins
        mask = (y_prob >= low) & (y_prob < high if idx < bins - 1 else y_prob <= high)
        count = int(np.sum(mask))
        if count <= 0:
            continue
        error += (count / total) * abs(float(np.mean(y_prob[mask])) - float(np.mean(y_true[mask])))
    return float(error)


def _finite_metric(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _relative_improvement(baseline: float | None, observed: float | None) -> float | None:
    if baseline is None or observed is None or baseline <= 0:
        return None
    return round((baseline - observed) / baseline, 6)


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
