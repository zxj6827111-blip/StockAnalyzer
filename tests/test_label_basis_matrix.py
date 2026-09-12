"""Phase 3 子线① 双入口矩阵测试（docs/week5_phase3_subline1_basis_implementation.md §3）。

核心风险是生产训练链与 PIT 回测链的横截面 label 口径漂移：两入口必须
共用 ``labels/return_rank.apply_return_rank_labels_by_day``。本文件用同
一份 (symbol, trade_date, fwd_return) 数据分别走两条链逐行对账，并覆盖
soup 基线回归、fail-closed 门与 registry 契约分支。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pandas as pd
import pytest

import stock_analyzer.models.trainer as trainer_module
from stock_analyzer.backtest.pit_dataset import _apply_return_rank_labels
from stock_analyzer.config import LabelsConfig, StockAnalyzerConfig, load_config
from stock_analyzer.learning.dataset_manifest import DatasetManifestBuilder
from stock_analyzer.learning.label_policy_registry import LabelPolicyRegistry
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    MaturityStatus,
    OutcomeRecord,
    SignalSnapshot,
)
from stock_analyzer.learning.sample_store import SampleStore
from stock_analyzer.labels.return_rank import apply_return_rank_labels_by_day
from stock_analyzer.models.trainer import ModelTrainer, _return_rank_labels_from_outcomes

_ROOT = Path(__file__).resolve().parents[1]
_DAYS = ("2026-08-03", "2026-08-04")
_PER_DAY_RETURNS: dict[str, list[float]] = {
    # 0803 刻意含一对平秩（两个 0.02）与一个恰在 top 截断线上的 0.03：
    # pct 秩 0.7 严格大于才算 top，用于钉死边界语义。
    "2026-08-03": [0.09, 0.07, 0.05, 0.03, 0.02, 0.02, -0.01, -0.03, -0.05, -0.07],
    "2026-08-04": [0.08, 0.06, 0.04, 0.02, 0.00, -0.02, -0.04, -0.06, -0.08, -0.10],
}


def _cross_section_rows() -> list[tuple[str, str, float]]:
    """两日 × 10 只票的 (symbol, trade_date, fwd_return) 截面数据。"""

    rows: list[tuple[str, str, float]] = []
    index = 0
    for day in _DAYS:
        for value in _PER_DAY_RETURNS[day]:
            rows.append((f"SYM{index:03d}", day, value))
            index += 1
    return rows


def _shanghai_anchor(day: str) -> datetime:
    """上海决策日 10:00 对应的 UTC 锚点（+8h 折算回上海日期须等于 day）。"""

    return datetime.fromisoformat(f"{day}T02:00:00+00:00")


def _base_config() -> StockAnalyzerConfig:
    return load_config(_ROOT / "config" / "default.yaml")


def _labels_config(**overrides: object) -> LabelsConfig:
    # 夹具规模适配：tp/sl 阈值显式给定（MFE/MAE 期望按此推导），horizon=2
    # 让 manifest 的时间语义与两天截面数据自洽；min_cross_section=10 与
    # 夹具每日 10 行截面匹配（config 默认 30 是全市场规模）。
    values: dict[str, object] = {
        "take_profit_pct": 0.06,
        "stop_loss_pct": 0.04,
        "horizon_days": 2,
        "conflict_policy": "soft_label",
        "conflict_soft_label_value": 0.5,
        "return_rank_min_cross_section": 10,
    }
    values.update(overrides)
    return _base_config().labels.model_copy(update=cast("dict[str, object]", values))


def _training_config(tmp_path: Path) -> object:
    training = _base_config().training
    return training.model_copy(
        update={
            "artifact_path": str(tmp_path / "protocol_model.json"),
            "model_archive_dir": str(tmp_path / "model_archive"),
            "min_samples": 2,
            "min_test_split_window_days": 0,
            "min_test_split_unique_symbol_dates": 0,
        }
    )


def _build_store(
    tmp_path: Path,
    rows: list[tuple[str, str, float]],
    *,
    labels_config: LabelsConfig,
) -> tuple[SampleStore, dict[str, str], LabelPolicyRegistry, object]:
    """按截面数据写 snapshot + outcome（realized_return=MFE 度量同源）。

    同时建 label policy registry 并按 labels_config 登记契约（soup 用例
    得 v2、return_rank 用例得 v3），snapshot 携带该契约身份。
    """

    store = SampleStore(db_path=tmp_path / "learning_protocol.duckdb")
    registry = LabelPolicyRegistry(db_path=tmp_path / "label_policy.duckdb")
    policy = registry.register_from_config(labels_config)
    snapshot_to_symbol: dict[str, str] = {}
    for symbol, day, value in rows:
        anchor = _shanghai_anchor(day)
        snapshot_id = f"snap-{symbol}-{day}"
        snapshot_to_symbol[snapshot_id] = symbol
        store.write_snapshot(
            SignalSnapshot(
                snapshot_id=snapshot_id,
                code_version="git:test",
                symbol=symbol,
                strategy="trend",
                decision_time=anchor,
                feature_vector={"feature_a": 0.5, "feature_b": -0.5},
                feature_schema_id="fs-test",
                feature_schema_hash="fsh-test",
                runtime_config_hash="runtime_hash_test",
                label_policy_id=policy.label_policy_id,
                label_policy_hash=policy.label_policy_hash,
            )
        )
        store.upsert_outcome(
            OutcomeRecord(
                snapshot_id=snapshot_id,
                maturity_status=MaturityStatus.RECONCILED,
                label_anchor_time=anchor,
                label_mature_time=anchor + timedelta(days=labels_config.horizon_days),
                realized_return=value,
                max_favorable_excursion=round(value + 0.01, 6),
                max_adverse_excursion=round(value - 0.01, 6),
                backfill_fidelity_tier=BackfillFidelityTier.GOLD,
                backfill_source="runtime_observed",
            )
        )
    return store, snapshot_to_symbol, registry, policy


# ---------------------------------------------------------------------------
# ① return_rank 截面语义（公共函数直接验证）
# ---------------------------------------------------------------------------


def test_return_rank_cross_section_semantics_top_bottom_middle_ties() -> None:
    frame = pd.DataFrame(
        [
            ("A1", "2026-08-03", 0.09),
            ("A2", "2026-08-03", 0.07),
            ("A3", "2026-08-03", 0.05),
            ("A4", "2026-08-03", 0.03),  # pct 秩恰 0.7：严格大于才 top → middle
            ("A5", "2026-08-03", 0.02),  # 平秩对，平均 pct 0.55 → middle
            ("A6", "2026-08-03", 0.02),
            ("A7", "2026-08-03", -0.01),
            ("A8", "2026-08-03", -0.03),  # pct 0.3 ≤ bottom_cut → 0
            ("A9", "2026-08-03", -0.05),
            ("A10", "2026-08-03", -0.07),
        ],
        columns=["symbol", "trade_date", "fwd_return"],
    )
    labels = apply_return_rank_labels_by_day(
        frame,
        top_quantile=0.3,
        bottom_quantile=0.3,
        drop_middle=True,
        min_cross_section=10,
    )
    by_symbol = {symbol: value for symbol, value in zip(frame["symbol"], labels)}
    assert by_symbol["A1"] == 1.0 and by_symbol["A2"] == 1.0 and by_symbol["A3"] == 1.0
    assert by_symbol["A8"] == 0.0 and by_symbol["A9"] == 0.0 and by_symbol["A10"] == 0.0
    # 严格截断 + 平秩平均秩：0.03（pct=0.7）与平秩对（pct=0.55）都落中间。
    middle_symbols = ["A4", "A5", "A6", "A7"]
    assert all(pd.isna(by_symbol[symbol]) for symbol in middle_symbols)
    # drop_middle=False 时中间段为 soft 0.5（与 soup 冲突软标签同语义）。
    soft = apply_return_rank_labels_by_day(
        frame,
        top_quantile=0.3,
        bottom_quantile=0.3,
        drop_middle=False,
        min_cross_section=10,
    )
    soft_by_symbol = {symbol: value for symbol, value in zip(frame["symbol"], soft)}
    assert all(soft_by_symbol[symbol] == 0.5 for symbol in middle_symbols)
    assert soft_by_symbol["A1"] == 1.0 and soft_by_symbol["A10"] == 0.0


def test_return_rank_no_cross_day_leakage() -> None:
    rows = _cross_section_rows()
    base = apply_return_rank_labels_by_day(
        pd.DataFrame(rows, columns=["symbol", "trade_date", "fwd_return"]),
        min_cross_section=10,
    )
    # 打乱第二天全部 fwd_return（同日截面内排序改变），第一天 label 必须不动。
    perturbed = [
        (symbol, day, -value if day == _DAYS[1] else value)
        for symbol, day, value in rows
    ]
    after = apply_return_rank_labels_by_day(
        pd.DataFrame(perturbed, columns=["symbol", "trade_date", "fwd_return"]),
        min_cross_section=10,
    )
    base_series = pd.Series(base, index=base.index)
    after_series = pd.Series(after, index=after.index)
    day0_mask = pd.Series([row[1] == _DAYS[0] for row in rows])
    pd.testing.assert_series_equal(
        base_series[day0_mask], after_series[day0_mask], check_names=False
    )


def test_return_rank_thin_cross_section_day_is_all_nan() -> None:
    rows = _cross_section_rows()
    # 第二天只保留 5 行有效样本（< min_cross_section=10）→ 整日 NaN。
    thin_rows = [row for row in rows if row[1] == _DAYS[0]] + rows[-5:]
    labels = apply_return_rank_labels_by_day(
        pd.DataFrame(thin_rows, columns=["symbol", "trade_date", "fwd_return"]),
        min_cross_section=10,
    )
    values = list(labels)
    day0_labels = values[:10]
    day1_labels = values[10:]
    # day0 有完整截面：3 top + 3 bottom，4 个 middle 被 drop。
    assert sorted(v for v in day0_labels if not pd.isna(v)) == [0.0, 0.0, 0.0, 1.0, 1.0, 1.0]
    # day1 只有 5 行有效样本（< min_cross_section=10）→ 整日 NaN。
    assert all(pd.isna(value) for value in day1_labels)


# ---------------------------------------------------------------------------
# ② 两入口一致性（生产 trainer helper vs PIT 合并阶段，防漂移主测试）
# ---------------------------------------------------------------------------


def test_two_entry_label_consistency_production_vs_pit(tmp_path: Path) -> None:
    rows = _cross_section_rows()
    labels_config = _labels_config()

    # 生产链入口：snapshot/outcome → trainer 的 v3 预计算 helper。
    store, snapshot_to_symbol, _registry, _policy = _build_store(
        tmp_path, rows, labels_config=labels_config
    )
    snapshots = {
        snapshot.snapshot_id: snapshot
        for snapshot in store.list_snapshots()
    }
    outcomes = {
        outcome.snapshot_id: outcome
        for outcome in store.list_outcomes()
    }
    production = _return_rank_labels_from_outcomes(
        outcomes=outcomes,
        snapshots=snapshots,
        labels_config=labels_config,
    )

    # PIT 链入口：同一截面的合并阶段月度块。
    pit_frame = pd.DataFrame(rows, columns=["symbol", "trade_date", "fwd_return"])
    pit_frame = _apply_return_rank_labels(
        pit_frame,
        top_q=labels_config.return_rank_top_quantile,
        bottom_q=labels_config.return_rank_bottom_quantile,
        drop_middle=labels_config.return_rank_drop_middle,
        min_cross=labels_config.return_rank_min_cross_section,
    )

    assert len(production) > 0
    for _, row in pit_frame.iterrows():
        snapshot_id = next(
            sid for sid, symbol in snapshot_to_symbol.items() if symbol == row["symbol"]
        )
        production_value = production.get(snapshot_id)
        pit_value = row["label"]
        if production_value is None:
            assert pd.isna(pit_value), f"{row['symbol']} 生产 NaN 但 PIT={pit_value}"
        else:
            assert not pd.isna(pit_value), f"{row['symbol']} PIT NaN 但 生产={production_value}"
            assert production_value == float(pit_value), f"{row['symbol']} 两入口 label 不一致"


# ---------------------------------------------------------------------------
# ③ soup 基线回归（basis=soup 时 v2 逐行派生路径行为不变）
# ---------------------------------------------------------------------------


def test_soup_basis_production_path_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    labels_config = _labels_config()
    rows = [
        ("600100", "2026-08-03", 0.09),
        ("600200", "2026-08-03", 0.01),
        ("600300", "2026-08-03", -0.09),
        ("600400", "2026-08-03", 0.02),
    ]
    store, snapshot_to_symbol, registry, policy = _build_store(
        tmp_path, rows, labels_config=labels_config
    )
    # soup 用例需要显式 TP/SL 路径度量（_build_store 的 MFE/MAE 是由
    # realized_return 机械推导的，不会触发路径判定）：按行覆盖。
    path_metrics = {
        "snap-600100-2026-08-03": (0.10, -0.01),  # TP 命中 → 1
        "snap-600200-2026-08-03": (0.02, 0.00),  # 无触发，realized < tp → 0
        "snap-600300-2026-08-03": (-0.08, -0.10),  # SL 命中 → 0
        "snap-600400-2026-08-03": (0.10, -0.10),  # TP/SL 同期冲突 → soft 0.5
    }
    for snapshot_id, (mfe, mae) in path_metrics.items():
        outcome = store.get_outcome(snapshot_id)
        assert outcome is not None
        store.upsert_outcome(
            outcome.model_copy(
                update={
                    "max_favorable_excursion": mfe,
                    "max_adverse_excursion": mae,
                }
            )
        )

    captured: dict[str, float] = {}
    original = trainer_module._label_from_outcome

    def _spy(*, outcome: OutcomeRecord, policy: object) -> float | None:
        value = original(outcome=outcome, policy=policy)  # type: ignore[arg-type]
        captured[outcome.snapshot_id] = float("nan") if value is None else float(value)
        return value

    monkeypatch.setattr(trainer_module, "_label_from_outcome", _spy)

    trainer = ModelTrainer(
        training=_training_config(tmp_path),
        labels=labels_config,
        models=_base_config().models,
    )
    manifest = DatasetManifestBuilder(store=store, feature_schema_registry=None).create_manifest(
        feature_schema_id="fs-test",
        feature_schema_hash="fsh-test",
        label_policy_id=policy.label_policy_id,
        label_policy_hash=policy.label_policy_hash,
        snapshot_ids=sorted(snapshot_to_symbol),
        calibration_ratio=0.0,
        test_ratio=0.25,
        embargo_days=0,
    )
    trainer.train_on_dataset_manifest(
        store=store,
        dataset_manifest=manifest,
        feature_schema_registry=None,
        label_policy_registry=registry,
    )

    # v2 逐行派生路径被原样调用且结果与 TP/SL 语义一致（改动前行为；
    # _non_conflict_label：无触发时 realized_return ≥ tp → 1，否则 0）。
    expected = {
        "snap-600100-2026-08-03": 1.0,  # MFE 0.10 ≥ tp=0.06 → TP 命中
        "snap-600200-2026-08-03": 0.0,  # 无触发，realized 0.01 < tp → 0
        "snap-600300-2026-08-03": 0.0,  # MAE -0.10 ≤ -sl → SL 命中
        "snap-600400-2026-08-03": 0.5,  # TP/SL 同期冲突 → soft
    }
    assert captured == expected


# ---------------------------------------------------------------------------
# ④ fail-closed：单标的路径拒绝 return_rank
# ---------------------------------------------------------------------------


def test_train_on_bars_fails_closed_for_return_rank_basis(tmp_path: Path) -> None:
    trainer = ModelTrainer(
        training=_training_config(tmp_path),
        labels=_labels_config(basis="return_rank"),
        models=_base_config().models,
    )
    with pytest.raises(ValueError, match="return_rank_basis_requires_cross_section"):
        trainer.train_on_bars(pd.DataFrame())


# ---------------------------------------------------------------------------
# ⑤ registry 契约：register_from_config 按 basis 分支
# ---------------------------------------------------------------------------


def test_registry_branch_registers_v3_contract_for_return_rank(tmp_path: Path) -> None:
    registry = LabelPolicyRegistry(db_path=tmp_path / "label_policy.duckdb")

    soup_record = registry.register_from_config(_labels_config())
    assert soup_record.schema_version == "2"
    assert soup_record.label_name.startswith("soup_")
    assert soup_record.conflict_policy == "soft_label"

    v3_record = registry.register_from_config(_labels_config(basis="return_rank"))
    assert v3_record.schema_version == "3"
    assert v3_record.label_name == "label_return_rank"
    assert v3_record.take_profit_pct == 0.0
    assert v3_record.stop_loss_pct == 0.0
    assert v3_record.conflict_policy == "rank_quantile"

    # 分位参数进 hash payload：改参数即得新契约 id，参数不可静默混用。
    tighter = registry.register_from_config(
        _labels_config(basis="return_rank", return_rank_top_quantile=0.2)
    )
    assert tighter.label_policy_hash != v3_record.label_policy_hash
    assert tighter.label_policy_id != v3_record.label_policy_id


# ---------------------------------------------------------------------------
# ⑥ 端到端演练：v3 policy 走生产 manifest 训练全链（drop_middle 生效）
# ---------------------------------------------------------------------------


def test_production_manifest_training_with_return_rank_policy(tmp_path: Path) -> None:
    rows = _cross_section_rows()
    labels_config = _labels_config(basis="return_rank")
    store, snapshot_to_symbol, registry, policy = _build_store(
        tmp_path, rows, labels_config=labels_config
    )

    trainer = ModelTrainer(
        training=_training_config(tmp_path),
        labels=labels_config,
        models=_base_config().models,
    )
    manifest = DatasetManifestBuilder(store=store, feature_schema_registry=None).create_manifest(
        feature_schema_id="fs-test",
        feature_schema_hash="fsh-test",
        label_policy_id=policy.label_policy_id,
        label_policy_hash=policy.label_policy_hash,
        snapshot_ids=sorted(snapshot_to_symbol),
        calibration_ratio=0.25,
        test_ratio=0.25,
        embargo_days=0,
    )
    result = trainer.train_on_dataset_manifest(
        store=store,
        dataset_manifest=manifest,
        feature_schema_registry=None,
        label_policy_registry=registry,
    )

    # 20 行截面，每日 3 top + 3 bottom + 4 middle → drop_middle 后 12 行硬标签。
    assert result.metrics["dataset_hard_positive_count"] == 6.0
    assert result.metrics["dataset_hard_negative_count"] == 6.0
    # 数据集级去重/质量透传仍随 v3 路径写入（晋级有效性门依赖）。
    assert result.artifact.metadata["dataset_dedup_quality"]["rows_before_dedup"] >= 12
