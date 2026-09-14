"""B1：raw / calibrated 指标分离 + 逐阶段互斥账本（学习链整改 v2 批次 B）。

口径（v2 §批次 B1）：

- 每个底模与 blend、pooled 与逐日、过滤前后、有效/无效日期分母**全部标明**；
- 工件同时记录 raw / calibrated 的 AUC、唯一取值数、并列占比；
- 账本为「进入阶段数 = 输出数 + 各互斥原因剔除数」，**不**把
  ``included_snapshot_count - dataset_rows`` 当唯一恒等式。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.labels.return_rank import (
    LABEL_REASON_MIDDLE_DROPPED,
    LABEL_REASON_MISSING_RETURN,
    LABEL_REASON_THIN_CROSS_SECTION,
    build_return_rank_labels,
    build_return_rank_labels_with_reasons,
)
from stock_analyzer.learning.label_policy_registry import (
    LabelPolicyRegistry,
    resolve_return_rank_params,
)
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    MaturityStatus,
    OutcomeRecord,
    SignalSnapshot,
)
from stock_analyzer.learning.sample_store import SampleStore
from stock_analyzer.models.trainer import (
    ModelTrainer,
    _evaluate_metrics,
    _return_rank_labels_with_ledger,
)

_ROOT = Path(__file__).resolve().parents[1]


def _config() -> StockAnalyzerConfig:
    config = load_config(_ROOT / "config" / "default.yaml")
    config.training.min_samples = 20
    config.training.validation_ratio = 0.2
    config.training.calibration_ratio = 0.1
    config.training.test_ratio = 0.1
    config.training.min_test_split_window_days = 1
    config.training.min_test_split_unique_symbol_dates = 1
    return config


def _write_store(
    tmp_path: Path,
    *,
    day_count: int = 24,
    symbols_per_day: int = 10,
    realized_returns: list[float] | None = None,
    missing_return_days: set[int] | None = None,
    thin_days: set[int] | None = None,
) -> tuple[SampleStore, LabelPolicyRegistry, object]:
    config = _config()
    labels = config.labels.model_copy(
        update={
            "basis": "return_rank",
            "horizon_days": 2,
            "return_rank_min_cross_section": 5,
            "return_rank_top_quantile": 0.3,
            "return_rank_bottom_quantile": 0.3,
        }
    )
    registry = LabelPolicyRegistry(db_path=tmp_path / "label_policy.duckdb")
    record = registry.register_from_config(labels)
    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    base_time = datetime(2026, 1, 1, 14, 30, tzinfo=UTC)
    default_returns = [0.2 - 0.004 * index for index in range(symbols_per_day)]
    for day in range(day_count):
        decision_time = base_time + timedelta(days=day)
        symbols = 2 if day in (thin_days or set()) else symbols_per_day
        for index in range(symbols):
            snapshot_id = f"snap-{day:03d}-{index:03d}"
            store.write_snapshot(
                SignalSnapshot(
                    snapshot_id=snapshot_id,
                    code_version="git:test",
                    symbol=f"600{index:03d}.SH",
                    strategy="trend",
                    decision_time=decision_time,
                    feature_vector={"feature_a": float(index) / 10.0, "feature_b": 0.5},
                    feature_schema_id="fs-test",
                    feature_schema_hash="fsh-test",
                    runtime_config_hash="runtime_hash_test",
                    label_policy_id=record.label_policy_id,
                    label_policy_hash=record.label_policy_hash,
                )
            )
            returns = realized_returns or default_returns
            return_value: float | None = returns[index]
            if day in (missing_return_days or set()) and index == 0:
                return_value = None
            store.upsert_outcome(
                OutcomeRecord(
                    snapshot_id=snapshot_id,
                    maturity_status=MaturityStatus.RECONCILED,
                    label_anchor_time=decision_time,
                    label_mature_time=decision_time + timedelta(days=2),
                    realized_return=return_value,
                    backfill_fidelity_tier=BackfillFidelityTier.GOLD,
                    backfill_source="runtime_observed",
                )
            )
    return store, registry, record


def test_label_reasons_are_mutually_exclusive_and_match_labels() -> None:
    """三类剔除原因互斥；拿到标签的行原因为空。"""

    frame = pd.DataFrame(
        {
            "snapshot_id": [f"s{index:02d}" for index in range(14)],
            "trade_date": (["2026-01-01"] * 10 + ["2026-01-02"] * 3 + ["2026-01-03"]),
            "fwd_return": [0.2 - 0.02 * index for index in range(10)]
            + [0.1, 0.05, None]
            + [float("nan")],
        }
    )
    labels, reasons = build_return_rank_labels_with_reasons(
        frame["fwd_return"],
        top_quantile=0.3,
        bottom_quantile=0.3,
        drop_middle=True,
        min_cross_section=5,
        trade_dates=frame["trade_date"],
    )

    # 第 1 天（10 行）可用：3 top + 3 bottom = 6 个标签，4 行落中间段。
    assert int(labels.notna().sum()) == 6
    assert int((reasons == LABEL_REASON_MIDDLE_DROPPED).sum()) == 4
    # 第 2 天只有 3 行有效（< min_cross_section=5）：整日归因 thin_cross_section
    # ——其中缺收益的那一行仍归因 missing_or_nonfinite_return（行级优先）。
    assert int((reasons == LABEL_REASON_THIN_CROSS_SECTION).sum()) == 2
    # 缺收益：第 2 天 1 行 + 第 3 天 1 行。
    assert int((reasons == LABEL_REASON_MISSING_RETURN).sum()) == 2
    # 互斥 + 全覆盖：每行恰好落一类。
    assert int((reasons != "").sum()) == len(frame) - int(labels.notna().sum())
    assert set(reasons.unique()) <= {
        "",
        LABEL_REASON_MIDDLE_DROPPED,
        LABEL_REASON_THIN_CROSS_SECTION,
        LABEL_REASON_MISSING_RETURN,
    }
    # 标签本体与不带原因的入口逐位一致（单一实现，防漂移）。
    plain = build_return_rank_labels(
        frame["fwd_return"],
        top_quantile=0.3,
        bottom_quantile=0.3,
        drop_middle=True,
        min_cross_section=5,
        trade_dates=frame["trade_date"],
    )
    pd.testing.assert_series_equal(labels, plain)


def test_transformer_output_health_reports_raw_and_calibrated_separately() -> None:
    """raw 与 calibrated 必须分别报 AUC / 唯一值 / 并列占比，且可识别塌缩。"""

    y_true = np.asarray([1.0, 1.0, 0.0, 0.0], dtype=float)
    informative_raw = np.asarray([0.8, 0.7, 0.3, 0.2], dtype=float)
    # 校准器把有序分数压成常数（9/13 工件的塌缩形态）。
    collapsed_calibrated = np.full(4, 0.5, dtype=float)
    dates = np.asarray(["2026-01-01"] * 4)

    metrics = _evaluate_metrics(
        y_true=y_true,
        lgbm=collapsed_calibrated,
        xgb=collapsed_calibrated,
        meta=collapsed_calibrated,
        precision_at_k_ratio=0.5,
        raw_scores={"blend": informative_raw},
        calibrated_scores={"blend": collapsed_calibrated},
        test_trade_dates=dates,
    )

    assert metrics["auc_raw_blend"] == pytest.approx(1.0)
    assert metrics["auc_calibrated_blend"] == pytest.approx(0.5)
    assert metrics["unique_values_raw_blend"] == 4.0
    assert metrics["unique_values_calibrated_blend"] == 1.0
    assert metrics["tie_fraction_raw_blend"] == pytest.approx(0.0)
    assert metrics["tie_fraction_calibrated_blend"] == pytest.approx(0.75)
    assert metrics["positive_rate_calibrated_blend"] == pytest.approx(1.0)
    assert metrics["mean_prob_spread_calibrated_blend"] == pytest.approx(0.0)
    # 逐日分母：四行同属一天且两类标签都在 → 有效日 1、无效日 0。
    assert metrics["daily_auc_valid_days_raw_blend"] == 1.0
    assert metrics["daily_auc_invalid_days_raw_blend"] == 0.0
    assert metrics["daily_auc_mean_raw_blend"] == pytest.approx(1.0)
    # 既有顶层键语义不变（= calibrated blend）。
    assert metrics["auc"] == pytest.approx(0.5)


def test_daily_denominator_separates_invalid_days() -> None:
    """只有一类标签的决策日算无效日，单独计数、不并进有效分母。"""

    y_true = np.asarray([1.0, 1.0, 0.0, 0.0, 1.0, 1.0], dtype=float)
    scores = np.asarray([0.9, 0.8, 0.2, 0.1, 0.7, 0.6], dtype=float)
    dates = np.asarray(["d1"] * 4 + ["d2"] * 2)

    metrics = _evaluate_metrics(
        y_true=y_true,
        lgbm=scores,
        xgb=scores,
        meta=scores,
        precision_at_k_ratio=0.5,
        raw_scores={"blend": scores},
        calibrated_scores={"blend": scores},
        test_trade_dates=dates,
    )

    assert metrics["daily_auc_valid_days_calibrated_blend"] == 1.0
    assert metrics["daily_auc_invalid_days_calibrated_blend"] == 1.0
    assert metrics["daily_auc_mean_calibrated_blend"] == pytest.approx(1.0)


def test_non_finite_scores_are_counted(tmp_path: Path) -> None:
    """非有限输出必须计数（B2 的确定性失败判据依赖它）。"""

    y_true = np.asarray([1.0, 0.0, 1.0, 0.0], dtype=float)
    scores = np.asarray([0.9, np.nan, 0.7, 0.2], dtype=float)

    metrics = _evaluate_metrics(
        y_true=y_true,
        lgbm=scores,
        xgb=scores,
        meta=scores,
        precision_at_k_ratio=0.5,
        calibrated_scores={"blend": scores},
    )

    assert metrics["non_finite_count_calibrated_blend"] == 1.0
    assert metrics["scored_samples_calibrated_blend"] == 4.0


def test_training_metrics_carry_raw_calibrated_split_and_ledger(tmp_path: Path) -> None:
    """端到端：工件指标同时含 raw/calibrated 分离字段与闭合账本。"""

    store, registry, record = _write_store(
        tmp_path,
        missing_return_days={3},
        thin_days={5},
    )
    config = _config()
    trainer = ModelTrainer(
        training=config.training,
        labels=config.labels.model_copy(
            update={
                "basis": "return_rank",
                "horizon_days": 2,
                "return_rank_min_cross_section": 5,
                "return_rank_top_quantile": 0.3,
                "return_rank_bottom_quantile": 0.3,
            }
        ),
        models=config.models,
    )
    result = trainer.train_on_sample_store(
        store=store,
        feature_schema_id="fs-test",
        feature_schema_hash="fsh-test",
        label_policy_id=record.label_policy_id,
        label_policy_hash=record.label_policy_hash,
        label_policy_registry=registry,
    )

    metrics = result.metrics
    for key in (
        "auc_raw_lgbm",
        "auc_raw_xgb",
        "auc_raw_blend",
        "auc_calibrated_lgbm",
        "auc_calibrated_xgb",
        "auc_calibrated_blend",
        "unique_values_raw_blend",
        "unique_values_calibrated_blend",
        "tie_fraction_raw_blend",
        "tie_fraction_calibrated_blend",
        "daily_auc_valid_days_calibrated_blend",
        "daily_auc_invalid_days_calibrated_blend",
    ):
        assert key in metrics, key
    # 工件里同样可见（registry 读的是 artifact.training_metrics）。
    assert metrics["auc_raw_blend"] == result.artifact.training_metrics["auc_raw_blend"]

    # 账本闭合：进入 = 输出 + 各互斥原因剔除，且未归类为 0。
    # 账本入口是 manifest 成员（purge 在其之前结算，剔除规模另有字段），
    # 不与 store 总行数比较——这正是 v1 那个恒等式失效的地方。
    manifest = store.get_manifest(str(result.artifact.dataset_manifest_id))
    assert manifest is not None
    assert metrics["label_ledger_entered"] == pytest.approx(float(manifest.included_snapshot_count))
    assert metrics["label_ledger_unaccounted"] == 0.0
    assert (
        metrics["label_ledger_labelled"] + metrics["label_ledger_dropped_total"]
        == (metrics["label_ledger_entered"])
    )
    assert metrics["label_ledger_labelled"] == pytest.approx(
        float(result.samples_train) + float(result.samples_calibration) + float(result.samples_test)
    )
    # 注入的原因确实进了账本（缺失收益 / 薄截面）。
    assert metrics["label_ledger_dropped_missing_or_nonfinite_return"] > 0.0
    assert metrics["label_ledger_dropped_middle_dropped"] > 0.0
    assert metrics["label_ledger_dropped_thin_cross_section"] > 0.0

    # 确定性复算：同一 manifest 成员 + 同一契约参数重算账本，逐项一致。
    item_ids = [
        item.snapshot_id for item in store.list_manifest_items(manifest.dataset_manifest_id)
    ]
    _labels, expected_ledger = _return_rank_labels_with_ledger(
        outcomes={o.snapshot_id: o for o in store.list_outcomes(snapshot_ids=item_ids)},
        snapshots={s.snapshot_id: s for s in store.list_snapshots(snapshot_ids=item_ids)},
        params=resolve_return_rank_params(
            _registry_record(registry, record), config_labels=trainer._labels
        ),
    )
    for ledger_key, expected_value in expected_ledger.items():
        assert metrics[f"label_ledger_{ledger_key}"] == pytest.approx(expected_value), ledger_key


def _registry_record(registry: LabelPolicyRegistry, record: object) -> object:
    loaded = registry.get_by_id(str(record.label_policy_id))
    assert loaded is not None
    return loaded


def test_ledger_reconciles_with_multiple_injected_reasons(tmp_path: Path) -> None:
    """注入多种剔除原因后账本仍闭合（防止只对 happy path 成立）。"""

    store, registry, record = _write_store(
        tmp_path,
        day_count=24,
        symbols_per_day=10,
        missing_return_days=set(range(0, 24, 3)),
        thin_days={8, 14, 20},
    )
    config = _config()
    labels = config.labels.model_copy(
        update={
            "basis": "return_rank",
            "horizon_days": 2,
            "return_rank_min_cross_section": 5,
            "return_rank_top_quantile": 0.3,
            "return_rank_bottom_quantile": 0.3,
        }
    )
    trainer = ModelTrainer(training=config.training, labels=labels, models=config.models)
    result = trainer.train_on_sample_store(
        store=store,
        feature_schema_id="fs-test",
        feature_schema_hash="fsh-test",
        label_policy_id=record.label_policy_id,
        label_policy_hash=record.label_policy_hash,
        label_policy_registry=registry,
    )

    metrics = result.metrics
    manifest = store.get_manifest(str(result.artifact.dataset_manifest_id))
    assert manifest is not None
    entered = float(manifest.included_snapshot_count)
    assert metrics["label_ledger_entered"] == entered
    assert metrics["label_ledger_unaccounted"] == 0.0
    assert metrics["label_ledger_dropped_total"] + metrics["label_ledger_labelled"] == entered
    # 24 天里每 3 天注入 1 行缺失收益（含被 purge 的天，故只断言下界）。
    assert metrics["label_ledger_dropped_missing_or_nonfinite_return"] >= 5.0
    # 薄截面日按构造只写入 2 行（< min_cross_section=5），故整日 2 行归因 thin；
    # 其中被 purge 掉的日子不计入（Purge 在账本入口之前结算）。
    assert metrics["label_ledger_dropped_thin_cross_section"] >= 2.0
    assert metrics["label_ledger_dropped_middle_dropped"] > 0.0
