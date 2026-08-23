"""Commit 1（P0-b）验收测试：schema-aware 标签分派 + soft/hard 评估口径分离。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from stock_analyzer.config import load_config
from stock_analyzer.learning.label_policy_registry import (
    LabelPolicyRegistry,
    build_label_policy_record,
)
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    MaturityStatus,
    OutcomeRecord,
)
from stock_analyzer.models.trainer import (
    _build_meta_weights,
    _evaluate_metrics,
    _label_from_outcome,
)


def _outcome(
    *,
    mfe: float | None,
    mae: float | None,
    realized_return: float | None = 0.0,
) -> OutcomeRecord:
    return OutcomeRecord(
        snapshot_id="snap-test",
        maturity_status=MaturityStatus.RECONCILED,
        label_mature_time=datetime(2026, 1, 8, tzinfo=UTC),
        realized_return=realized_return,
        max_favorable_excursion=mfe,
        max_adverse_excursion=mae,
        backfill_fidelity_tier=BackfillFidelityTier.GOLD,
        backfill_source="runtime_observed",
    )


def _policy(conflict_policy: str, schema_version: str):
    return build_label_policy_record(
        label_name="soup_5d_tp5_before_sl5",
        take_profit_pct=0.05,
        stop_loss_pct=0.05,
        horizon_days=5,
        price_basis="next_tradable_vwap",
        exclude_untradable=True,
        conflict_policy=conflict_policy,
        conflict_soft_label_value=0.5,
        schema_version=schema_version,
    )


_CONFLICT_OUTCOME = dict(mfe=0.08, mae=-0.08)


@pytest.mark.parametrize(
    ("conflict_policy", "schema_version", "expected"),
    [
        # 三 policy × 两 schema 冲突矩阵。
        ("soft_label", "1", 0.5),
        ("soft_label", "2", 0.5),
        # v1 历史口径：bar_shape_heuristic 未实现，静默退化为 0.0（逐位保留）。
        ("bar_shape_heuristic", "1", 0.0),
        # v2 修复口径：bar_shape_heuristic 映射为配置软值。
        ("bar_shape_heuristic", "2", 0.5),
        ("conservative_zero", "1", 0.0),
        ("conservative_zero", "2", 0.0),
    ],
)
def test_conflict_label_matrix_by_policy_and_schema(
    conflict_policy: str,
    schema_version: str,
    expected: float,
) -> None:
    label = _label_from_outcome(
        outcome=_outcome(**_CONFLICT_OUTCOME),
        policy=_policy(conflict_policy, schema_version),
    )
    assert label == expected


def test_v1_non_conflict_paths_are_bit_identical_across_schemas() -> None:
    cases = [
        _outcome(mfe=0.08, mae=-0.01),  # 仅触发止盈
        _outcome(mfe=0.01, mae=-0.08),  # 仅触发止损
        _outcome(mfe=None, mae=None, realized_return=0.06),
        _outcome(mfe=None, mae=None, realized_return=-0.02),
        _outcome(mfe=None, mae=None, realized_return=None),
    ]
    for outcome in cases:
        v1 = _label_from_outcome(outcome=outcome, policy=_policy("soft_label", "1"))
        v2 = _label_from_outcome(outcome=outcome, policy=_policy("soft_label", "2"))
        legacy = _label_from_outcome(outcome=outcome, policy=_policy("conservative_zero", "1"))
        assert v1 == v2 == legacy
    assert _label_from_outcome(outcome=cases[0], policy=_policy("soft_label", "1")) == 1.0
    assert _label_from_outcome(outcome=cases[1], policy=_policy("soft_label", "1")) == 0.0
    assert _label_from_outcome(outcome=cases[2], policy=_policy("soft_label", "1")) == 1.0
    assert _label_from_outcome(outcome=cases[3], policy=_policy("soft_label", "1")) == 0.0
    assert _label_from_outcome(outcome=cases[4], policy=_policy("soft_label", "1")) is None


def test_v2_unknown_conflict_policy_raises() -> None:
    with pytest.raises(ValueError, match="unsupported conflict_policy"):
        _label_from_outcome(
            outcome=_outcome(**_CONFLICT_OUTCOME),
            policy=_policy("magic_bar_rule", "2"),
        )


def test_unknown_schema_version_raises() -> None:
    with pytest.raises(ValueError, match="unsupported label policy schema_version"):
        _label_from_outcome(
            outcome=_outcome(**_CONFLICT_OUTCOME),
            policy=_policy("soft_label", "3"),
        )


def test_register_from_config_defaults_to_schema_v2(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    registry = LabelPolicyRegistry(db_path=tmp_path / "label_policy.duckdb")
    record = registry.register_from_config(config.labels)
    assert record.schema_version == "2"

    explicit_v1 = registry.register_from_config(config.labels, schema_version="1")
    assert explicit_v1.schema_version == "1"
    # v1/v2 共存：同一配置不同 schema 版本是两条独立契约。
    assert explicit_v1.label_policy_hash != record.label_policy_hash


def test_evaluate_metrics_auc_uses_hard_labels_only_and_brier_soft_target() -> None:
    # 硬标签子集上 AUC=0.0（正样本概率更低）；若把 0.5 软标签二值化并入，
    # AUC 会变成 0.5——用该差分锁定“只计硬标签”的口径。
    y_true = np.array([0.0, 1.0, 0.5])
    meta = np.array([0.6, 0.4, 0.7])
    lgbm = np.array([0.55, 0.45, 0.65])
    xgb = np.array([0.65, 0.35, 0.75])

    metrics = _evaluate_metrics(
        y_true=y_true,
        lgbm=lgbm,
        xgb=xgb,
        meta=meta,
        precision_at_k_ratio=0.5,
    )

    assert metrics["auc"] == 0.0
    assert metrics["auc_valid"] == 1.0
    assert metrics["accuracy"] == 0.0  # 硬子集预测全错
    # Brier 用原始软目标：(0.6²+0.4²+0.2²)/3。
    assert metrics["brier"] == round(float(np.mean((meta - y_true) ** 2)), 6)
    assert metrics["soft_label_count"] == 1.0
    assert metrics["hard_label_count"] == 2.0
    assert metrics["hard_positive_count"] == 1.0
    assert metrics["hard_negative_count"] == 1.0
    assert metrics["mean_prob_spread"] == round(0.4 - 0.6, 6)


def test_evaluate_metrics_all_hard_labels_matches_legacy_binary_semantics() -> None:
    y_true = np.array([0.0, 1.0, 1.0, 0.0])
    meta = np.array([0.2, 0.8, 0.6, 0.3])
    lgbm = meta.copy()
    xgb = meta.copy()

    metrics = _evaluate_metrics(
        y_true=y_true,
        lgbm=lgbm,
        xgb=xgb,
        meta=meta,
        precision_at_k_ratio=0.5,
    )

    assert metrics["auc_valid"] == 1.0
    assert metrics["auc"] == 1.0
    # 全硬标签下软目标与二值目标数值一致（v1 回归）。
    assert metrics["brier"] == round(float(np.mean((meta - y_true.astype(float)) ** 2)), 6)
    assert metrics["soft_label_count"] == 0.0
    assert metrics["hard_label_count"] == 4.0
    assert metrics["validation_samples"] == 4.0


def test_evaluate_metrics_single_hard_class_marks_auc_invalid() -> None:
    y_true = np.array([1.0, 1.0, 0.5])
    metrics = _evaluate_metrics(
        y_true=y_true,
        lgbm=np.array([0.8, 0.7, 0.6]),
        xgb=np.array([0.8, 0.7, 0.6]),
        meta=np.array([0.8, 0.7, 0.6]),
        precision_at_k_ratio=0.5,
    )

    assert metrics["auc"] == 0.5
    assert metrics["auc_valid"] == 0.0
    assert metrics["hard_negative_count"] == 0.0


def test_build_meta_weights_use_soft_targets() -> None:
    # 软目标下 lgbm 完美（Brier=0），xgb 差；若按二值目标（0.5≥0.5→1）计算，
    # 权重会反转到 xgb 一侧——用该差分锁定权重使用软目标的口径。
    y_true = np.array([0.5])
    weights = _build_meta_weights(
        y_true=y_true,
        lgbm=np.array([0.5]),
        xgb=np.array([0.9]),
    )
    assert weights["lgbm"] > weights["xgb"]

    binary_weights_reference_lgbm_brier = float(np.mean((np.array([0.5]) - 1.0) ** 2))
    binary_weights_reference_xgb_brier = float(np.mean((np.array([0.9]) - 1.0) ** 2))
    assert binary_weights_reference_lgbm_brier > binary_weights_reference_xgb_brier


def test_build_meta_weights_all_hard_labels_unchanged() -> None:
    y_true = np.array([0.0, 1.0, 1.0, 0.0])
    weights = _build_meta_weights(
        y_true=y_true,
        lgbm=np.array([0.2, 0.8, 0.6, 0.3]),
        xgb=np.array([0.3, 0.7, 0.5, 0.4]),
    )
    lgbm_brier = float(np.mean((np.array([0.2, 0.8, 0.6, 0.3]) - y_true) ** 2))
    xgb_brier = float(np.mean((np.array([0.3, 0.7, 0.5, 0.4]) - y_true) ** 2))
    expected_lgbm = round((1.0 / max(lgbm_brier, 1e-6)) / (
        1.0 / max(lgbm_brier, 1e-6) + 1.0 / max(xgb_brier, 1e-6)
    ), 6)
    assert weights["lgbm"] == expected_lgbm
