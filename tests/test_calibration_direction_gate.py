"""校准窗方向门（label v3 生产重训捆绑包 Phase 0）。

门只卡 manifest 训练入口：那条路径的工件会落盘、进 registry、被热载。
研究侧 temporal 路径继续原样产出反向折线——9/13 的根因就是从那条路径的
指标里看出来的，把它一起挡掉等于砸掉诊断手段。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stock_analyzer.config import LabelsConfig, TrainingConfig, load_config
from stock_analyzer.learning.dataset_manifest import DatasetManifestBuilder
from stock_analyzer.learning.feature_schema_registry import FeatureSchemaRegistry
from stock_analyzer.learning.label_policy_registry import LabelPolicyRegistry
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    MaturityStatus,
    OutcomeRecord,
    SignalSnapshot,
)
from stock_analyzer.learning.sample_store import SampleStore
from stock_analyzer.models.trainer import ModelTrainer, _calibration_direction_report

FEATURE_NAMES = ["feature_alpha", "feature_beta"]


def test_manifest_training_fails_closed_on_reverse_calibration_window(
    tmp_path: Path,
) -> None:
    config, store, feature_registry, label_registry, feature_record, label_record = (
        _build_manifest_fixture(tmp_path, direction="reverse")
    )
    manifest = _create_manifest(config, store, feature_record, label_record)
    trainer = ModelTrainer(
        training=config.training,
        labels=config.labels,
        models=config.models,
    )

    with pytest.raises(ValueError) as excinfo:
        trainer.train_on_dataset_manifest(
            store=store,
            dataset_manifest=manifest,
            feature_schema_registry=feature_registry,
            label_policy_registry=label_registry,
        )

    message = str(excinfo.value)
    assert "calibration window direction gate failed" in message
    assert "lgbm_calibration_auc_reverse" in message
    assert "xgb_calibration_auc_reverse" in message
    assert "calibration_hard_samples=" in message
    assert "min_hard_samples=50" in message


def test_manifest_training_records_direction_metrics_on_forward_window(
    tmp_path: Path,
) -> None:
    config, store, feature_registry, label_registry, feature_record, label_record = (
        _build_manifest_fixture(tmp_path, direction="forward")
    )
    manifest = _create_manifest(config, store, feature_record, label_record)
    trainer = ModelTrainer(
        training=config.training,
        labels=config.labels,
        models=config.models,
    )

    result = trainer.train_on_dataset_manifest(
        store=store,
        dataset_manifest=manifest,
        feature_schema_registry=feature_registry,
        label_policy_registry=label_registry,
    )

    assert result.metrics["calibration_hard_samples"] >= 50.0
    assert result.metrics["calibration_auc_hard_lgbm"] > 0.5
    assert result.metrics["calibration_auc_hard_xgb"] > 0.5
    # 指标必须真的进工件：注册记录与晋级门读的是工件，不是内存里的 result。
    assert (
        result.artifact.training_metrics["calibration_auc_hard_lgbm"]
        == result.metrics["calibration_auc_hard_lgbm"]
    )


def test_reverse_window_below_hard_sample_floor_still_trains(tmp_path: Path) -> None:
    config, store, feature_registry, label_registry, feature_record, label_record = (
        _build_manifest_fixture(tmp_path, direction="reverse")
    )
    manifest = _create_manifest(
        config,
        store,
        feature_record,
        label_record,
        calibration_ratio=0.05,
    )
    trainer = ModelTrainer(
        training=config.training,
        labels=config.labels,
        models=config.models,
    )

    result = trainer.train_on_dataset_manifest(
        store=store,
        dataset_manifest=manifest,
        feature_schema_registry=feature_registry,
        label_policy_registry=label_registry,
    )

    # 证明确实是"样本不足"放行的，而不是门没生效，只需要硬样本低于下限这条。
    # 不断言该窗的 AUC 方向：校准窗压到下限以下时只剩十几行，训练出的模型在
    # 这么小的窗口上给出 1.0 还是 <=0.5 依赖训练非确定性（CI 实测 flip 过），
    # 而此刻门的语义本来就该是"不评估方向"——这里断言方向等于重造一个脆弱点。
    assert result.metrics["calibration_hard_samples"] < 50.0
    assert "calibration_auc_hard_lgbm" in result.metrics


def test_direction_gate_can_be_disabled_by_config(tmp_path: Path) -> None:
    config, store, feature_registry, label_registry, feature_record, label_record = (
        _build_manifest_fixture(tmp_path, direction="reverse")
    )
    config.training.calibration_direction_gate_enabled = False
    manifest = _create_manifest(config, store, feature_record, label_record)
    trainer = ModelTrainer(
        training=config.training,
        labels=config.labels,
        models=config.models,
    )

    result = trainer.train_on_dataset_manifest(
        store=store,
        dataset_manifest=manifest,
        feature_schema_registry=feature_registry,
        label_policy_registry=label_registry,
    )

    assert result.metrics["calibration_auc_hard_lgbm"] <= 0.5


def test_temporal_path_reports_reverse_window_without_blocking() -> None:
    dates = pd.bdate_range(start="2024-01-02", periods=60)
    symbols = ["600000", "000001", "600519", "000858"]
    rows = [(symbol, ts) for ts in dates for symbol in symbols]
    index = pd.MultiIndex.from_tuples(rows, names=["symbol", "date"])
    label_values = [
        _label_for(day_index, symbol_index)
        for day_index in range(len(dates))
        for symbol_index in range(len(symbols))
    ]
    feature_values = [
        _signal(position // len(symbols), label, flip_day=36)
        for position, label in enumerate(label_values)
    ]
    features = pd.DataFrame(
        {"feature_alpha": feature_values, "feature_beta": [0.5 * v for v in feature_values]},
        index=index,
    )
    label = pd.Series(label_values, index=index, name="label_soup_tp_before_sl")
    trainer = ModelTrainer(
        training=TrainingConfig(
            calibration_ratio=0.3,
            test_ratio=0.1,
            min_samples=8,
        ),
        labels=LabelsConfig(horizon_days=2),
    )

    result = trainer.train_on_feature_label(features=features, labels=label)

    assert result.artifact.metadata["dataset_split_strategy"] == "temporal"
    assert result.metrics["calibration_hard_samples"] >= 50.0
    assert result.metrics["calibration_auc_hard_lgbm"] <= 0.5


def test_single_class_calibration_window_is_not_a_direction(tmp_path: Path) -> None:
    # _binary_auc 在单类别时返回 0.5，那是"无法评估"而不是"反向"。
    y_true = np.ones(120, dtype=float)
    scores = {"lgbm": np.linspace(0.0, 1.0, 120, dtype=float)}

    metrics, violations = _calibration_direction_report(
        y_true=y_true,
        raw_scores=scores,
        min_hard_samples=50,
    )

    assert metrics["calibration_hard_samples"] == 120.0
    assert violations == []


def _build_manifest_fixture(
    tmp_path: Path,
    *,
    direction: str,
    flip_day: int = 28,
):
    """40 决策日 × 8 标的；``direction="reverse"`` 时训练窗与校准窗关系翻转。

    单纯的全局反相关不构成反向模型（负系数照样 AUC=1）；9/13 的形态是"老窗口的
    关系在新窗口里反过去"，所以 fixture 在 ``flip_day`` 处切换关系。默认 28 对齐
    校准窗起点（ratio 0.2/0.1 下 train=0~27、calibration=28~35、test=36~39），
    于是校准窗整段落在翻转侧。
    """

    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.training.min_samples = 50
    config.training.min_test_split_window_days = 1
    config.training.min_test_split_unique_symbol_dates = 1
    config.training.calibration_direction_gate_min_hard_samples = 50

    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    feature_registry = FeatureSchemaRegistry(db_path=tmp_path / "feature_schema.duckdb")
    label_registry = LabelPolicyRegistry(db_path=tmp_path / "label_policy.duckdb")
    feature_record = feature_registry.register_feature_names(
        feature_names=FEATURE_NAMES,
        feature_engineer_version="test",
        code_version="git:test",
    )
    label_record = label_registry.register_from_config(config.labels)

    base_time = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    for day in range(40):
        for symbol_index in range(8):
            label = _label_for(day, symbol_index)
            signal = _signal(
                day,
                label,
                flip_day=flip_day if direction == "reverse" else 40,
            )
            decision_time = base_time + timedelta(days=day * 20)
            snapshot_id = f"snap-{day:03d}-{symbol_index}"
            snapshot = SignalSnapshot(
                snapshot_id=snapshot_id,
                code_version="git:test",
                symbol=f"60{symbol_index:04d}.SH",
                strategy="trend",
                decision_time=decision_time,
                feature_vector={
                    "feature_alpha": signal,
                    "feature_beta": 0.5 * signal + (0.1 if label == 1.0 else -0.1),
                },
                feature_schema_id=feature_record.feature_schema_id,
                feature_schema_hash=feature_record.feature_schema_hash,
                runtime_config_hash="runtime_hash_gate",
                label_policy_id=label_record.label_policy_id,
                label_policy_hash=label_record.label_policy_hash,
            )
            outcome = OutcomeRecord(
                snapshot_id=snapshot_id,
                maturity_status=MaturityStatus.RECONCILED,
                label_mature_time=decision_time + timedelta(days=7),
                realized_return=0.09 if label == 1.0 else -0.07,
                max_favorable_excursion=0.10 if label == 1.0 else 0.01,
                max_adverse_excursion=-0.01 if label == 1.0 else -0.07,
                backfill_fidelity_tier=BackfillFidelityTier.GOLD,
                backfill_source="runtime_observed",
            )
            store.write_snapshot(snapshot)
            store.upsert_outcome(outcome)

    return config, store, feature_registry, label_registry, feature_record, label_record


def _label_for(day_index: int, symbol_index: int) -> float:
    return 1.0 if (day_index + symbol_index) % 2 == 0 else 0.0


def _signal(day_index: int, label: float, *, flip_day: int) -> float:
    """训练窗内 label↔高特征值，``flip_day`` 之后整段反过去。"""

    base = 2.0 if label == 1.0 else -2.0
    return -base if day_index >= flip_day else base


def _create_manifest(
    config: object,
    store: SampleStore,
    feature_record: object,
    label_record: object,
    *,
    calibration_ratio: float = 0.2,
) -> object:
    return DatasetManifestBuilder(store=store).create_manifest(
        feature_schema_id=feature_record.feature_schema_id,
        feature_schema_hash=feature_record.feature_schema_hash,
        label_policy_id=label_record.label_policy_id,
        label_policy_hash=label_record.label_policy_hash,
        fidelity_filter=[BackfillFidelityTier.GOLD],
        calibration_ratio=calibration_ratio,
        test_ratio=0.1,
    )
