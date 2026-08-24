"""Commit 3（P1-a）验收测试：manifest v2 去重 + trainer 防御 + 上游幂等。

Manifest v2 uses one sample per (symbol, Shanghai trade_date) and keeps the
largest stable ordinal, representing the latest snapshot.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stock_analyzer.learning.dataset_manifest import (
    DEDUP_KEY,
    DEDUP_RULE,
    DatasetManifestBuilder,
    build_manifest_quality_report,
    _decision_date_shanghai,
    _dedup_quality_flags,
)
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    MaturityStatus,
    OutcomeRecord,
    SignalSnapshot,
)
from stock_analyzer.learning.sample_store import SampleStore
from stock_analyzer.models.trainer import ModelTrainer


def _build_store_with_duplicates(
    tmp_path: Path,
    *,
    duplicate_symbol_day: bool = True,
    protocol_ids: tuple[str, str, str, str] = (
        "feature_schema_v1_dedup",
        "fshash",
        "label_policy_v1_dedup",
        "lphash",
    ),
) -> tuple[SampleStore, str, str, str, str]:
    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    feature_schema_id, feature_schema_hash, label_policy_id, label_policy_hash = protocol_ids
    base_time = datetime(2026, 1, 1, 1, 30, tzinfo=UTC)  # 上海时间 09:30
    ordinal = 0
    for day_offset in range(20):
        for symbol in ("600000.SH", "000001.SZ"):
            decision_time = base_time + timedelta(days=day_offset)
            snapshots_times = [decision_time]
            if duplicate_symbol_day and day_offset % 2 == 0:
                # 偶数天注入两次同日重复快照（模拟反复 pipeline_run_once）。
                snapshots_times.extend(
                    [decision_time + timedelta(hours=2), decision_time + timedelta(hours=4)]
                )
            for snap_time in snapshots_times:
                snapshot_id = f"snap-{ordinal:03d}"
                store.write_snapshot(
                    SignalSnapshot(
                        snapshot_id=snapshot_id,
                        code_version="git:test",
                        symbol=symbol,
                        strategy="trend",
                        decision_time=snap_time,
                        feature_vector={"feature_b": float(ordinal % 5), "feature_a": 0.5},
                        feature_schema_id=feature_schema_id,
                        feature_schema_hash=feature_schema_hash,
                        runtime_config_hash="runtime_hash_1",
                        label_policy_id=label_policy_id,
                        label_policy_hash=label_policy_hash,
                    )
                )
                store.upsert_outcome(
                    OutcomeRecord(
                        snapshot_id=snapshot_id,
                        maturity_status=MaturityStatus.RECONCILED,
                        label_mature_time=snap_time + timedelta(days=7),
                        realized_return=0.08 if ordinal % 2 == 0 else -0.05,
                        max_favorable_excursion=0.09 if ordinal % 2 == 0 else 0.01,
                        max_adverse_excursion=-0.01 if ordinal % 2 == 0 else -0.07,
                        backfill_fidelity_tier=BackfillFidelityTier.GOLD,
                        backfill_source="runtime_observed",
                    )
                )
                ordinal += 1
    return store, feature_schema_id, feature_schema_hash, label_policy_id, label_policy_hash


def _create_manifest(store: SampleStore, ids: tuple[str, str, str, str]):
    feature_schema_id, feature_schema_hash, label_policy_id, label_policy_hash = ids
    return DatasetManifestBuilder(store=store).create_manifest(
        feature_schema_id=feature_schema_id,
        feature_schema_hash=feature_schema_hash,
        label_policy_id=label_policy_id,
        label_policy_hash=label_policy_hash,
    )


def test_manifest_v2_dedupes_by_symbol_trade_date_keeps_latest(tmp_path: Path) -> None:
    ids_tuple = _build_store_with_duplicates(tmp_path)
    store = ids_tuple[0]
    manifest = _create_manifest(store, ids_tuple[1:])

    assert manifest.schema_version == "2"
    assert manifest.dataset_manifest_id.startswith("dataset_manifest_v2_")
    assert manifest.dedup_key == DEDUP_KEY
    assert manifest.dedup_rule == DEDUP_RULE
    # 40 个“唯一键”行中，10 天 × 2 symbol 各多写 2 条 → 注入 40 行，共 80 行。
    assert manifest.rows_before_dedup == 80
    assert manifest.rows_dropped_by_dedup == 40
    assert manifest.included_snapshot_count == 40
    assert "duplicate_rows_present" in manifest.warning_quality_flags
    # 占比恰为 50%，不触发 blocking（阈值是严格大于）。
    assert manifest.blocking_quality_flags == []

    item_ids = set(store.list_manifest_snapshot_ids(manifest.dataset_manifest_id))
    stored_snapshots = {
        snapshot.snapshot_id: snapshot
        for snapshot in store.list_snapshots()
    }
    # Each retained row must be the latest snapshot for its symbol-day key.
    for snapshot_id in item_ids:
        snapshot = stored_snapshots[snapshot_id]
        same_key_rows = [
            other
            for other in stored_snapshots.values()
            if other.symbol == snapshot.symbol
            and _decision_date_shanghai(other.decision_time)
            == _decision_date_shanghai(snapshot.decision_time)
        ]
        expected = max(
            same_key_rows,
            key=lambda other: (
                other.decision_time,
                other.created_at,
                other.snapshot_id,
            ),
        )
        assert snapshot_id == expected.snapshot_id

    # 幂等重放：同输入再建返回同一 v2 记录，绝不回落 v1。
    replayed = _create_manifest(store, ids_tuple[1:])
    assert replayed.dataset_manifest_id == manifest.dataset_manifest_id
    assert replayed.schema_version == "2"


def test_dedup_collapses_strategies_and_respects_shanghai_date_boundary(
    tmp_path: Path,
) -> None:
    store = SampleStore(db_path=tmp_path / "boundary.duckdb")
    common = dict(
        code_version="git:test",
        feature_vector={"f": 1.0},
        feature_schema_id="fs",
        feature_schema_hash="fsh",
        runtime_config_hash="rc",
        label_policy_id="lp",
        label_policy_hash="lph",
    )
    # UTC 15:59 → 上海当日 23:59；UTC 16:00 → 上海次日 00:00（不同决策日，不去重）。
    late_evening = datetime(2026, 3, 2, 15, 59, tzinfo=UTC)
    next_day_midnight = datetime(2026, 3, 2, 16, 0, tzinfo=UTC)
    rows = [
        ("snap-a", "600000.SH", "trend", late_evening),
        ("snap-b", "600000.SH", "soup", late_evening),
        ("snap-c", "600000.SH", "trend", next_day_midnight),
    ]
    for snapshot_id, symbol, strategy, decision_time in rows:
        store.write_snapshot(
            SignalSnapshot(
                snapshot_id=snapshot_id,
                symbol=symbol,
                strategy=strategy,
                decision_time=decision_time,
                **common,
            )
        )
        store.upsert_outcome(
            OutcomeRecord(
                snapshot_id=snapshot_id,
                maturity_status=MaturityStatus.RECONCILED,
                label_mature_time=decision_time + timedelta(days=7),
                realized_return=0.01,
                max_favorable_excursion=0.02,
                max_adverse_excursion=-0.01,
                backfill_fidelity_tier=BackfillFidelityTier.GOLD,
                backfill_source="runtime_observed",
            )
        )

    manifest = DatasetManifestBuilder(store=store).create_manifest(
        feature_schema_id="fs",
        feature_schema_hash="fsh",
        label_policy_id="lp",
        label_policy_hash="lph",
    )
    assert manifest.rows_before_dedup == 3
    assert manifest.rows_dropped_by_dedup == 1
    assert manifest.included_snapshot_count == 2
    assert set(store.list_manifest_snapshot_ids(manifest.dataset_manifest_id)) == {
        "snap-b",
        "snap-c",
    }


def test_manifest_quality_flags_are_recorded_for_narrow_test_split(tmp_path: Path) -> None:
    ids_tuple = _build_store_with_duplicates(tmp_path, duplicate_symbol_day=False)
    store = ids_tuple[0]
    manifest = DatasetManifestBuilder(store=store).create_manifest(
        feature_schema_id=ids_tuple[1],
        feature_schema_hash=ids_tuple[2],
        label_policy_id=ids_tuple[3],
        label_policy_hash=ids_tuple[4],
        min_test_split_window_days=20,
        min_test_split_unique_symbol_dates=30,
    )

    assert "test_window_too_narrow" in manifest.manifest_quality_flags
    assert "test_coverage_insufficient" in manifest.manifest_quality_flags
    assert manifest.test_split_window_days < 20
    assert manifest.test_split_unique_symbol_dates < 30

    trainer = ModelTrainer(
        training=_minimal_training_config(),
        labels=_minimal_labels_config(),
    )
    with pytest.raises(ValueError, match="manifest quality flags"):
        trainer.train_on_dataset_manifest(store=store, dataset_manifest=manifest)


def test_manifest_quality_window_uses_shanghai_trade_dates() -> None:
    snapshots = {}
    common = dict(
        code_version="git:test",
        feature_vector={"f": 1.0},
        feature_schema_id="fs",
        feature_schema_hash="fsh",
        runtime_config_hash="rc",
        label_policy_id="lp",
        label_policy_hash="lph",
    )
    # 15:59 UTC is still the same Shanghai trade date; 16:00 UTC is the next one.
    for snapshot_id, decision_time in (
        ("snap-a", datetime(2026, 3, 2, 15, 59, tzinfo=UTC)),
        ("snap-b", datetime(2026, 3, 2, 16, 0, tzinfo=UTC)),
    ):
        snapshots[snapshot_id] = SignalSnapshot(
            snapshot_id=snapshot_id,
            symbol="600000.SH",
            strategy="trend",
            decision_time=decision_time,
            **common,
        )

    report = build_manifest_quality_report(
        item_blueprints=[
            {"snapshot_id": "snap-a", "split_name": "test"},
            {"snapshot_id": "snap-b", "split_name": "test"},
        ],
        snapshots=snapshots,
        min_test_split_window_days=2,
        min_test_split_unique_symbol_dates=1,
    )

    # Both rows are on consecutive Shanghai dates, so the inclusive window is 2.
    assert report["test_split_window_days"] == 2
    assert report["flags"] == []

def test_duplicate_dominance_blocks_training_fail_closed(tmp_path: Path) -> None:
    store, *ids = _build_store_with_duplicates_force_dominance(tmp_path)
    manifest = _create_manifest(store, tuple(ids))

    assert "duplicate_dominance" in manifest.blocking_quality_flags

    trainer = ModelTrainer(
        training=_minimal_training_config(),
        labels=_minimal_labels_config(),
    )
    with pytest.raises(ValueError, match="blocked by quality flags"):
        trainer.train_on_dataset_manifest(
            store=store,
            dataset_manifest=manifest,
        )


def test_trainer_passes_through_dedup_stats_on_success(tmp_path: Path) -> None:
    from stock_analyzer.config import load_config
    from stock_analyzer.learning.feature_schema_registry import FeatureSchemaRegistry
    from stock_analyzer.learning.label_policy_registry import LabelPolicyRegistry

    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.training.min_samples = 20
    config.models.include_random_feature_baseline = False

    # 用真实注册表生成可解析的 schema/policy 契约，再按其 id/hash 造重复样本。
    feature_registry = FeatureSchemaRegistry(db_path=tmp_path / "fs.duckdb")
    label_registry = LabelPolicyRegistry(db_path=tmp_path / "lp.duckdb")
    feature_record = feature_registry.register_feature_names(
        feature_names=["feature_b", "feature_a"],
        feature_engineer_version="test",
        code_version="git:test",
    )
    label_record = label_registry.register_from_config(config.labels)
    ids_tuple = _build_store_with_duplicates(
        tmp_path,
        duplicate_symbol_day=False,
        protocol_ids=(
            feature_record.feature_schema_id,
            feature_record.feature_schema_hash,
            label_record.label_policy_id,
            label_record.label_policy_hash,
        ),
    )
    store = ids_tuple[0]
    manifest = _create_manifest(store, ids_tuple[1:])

    trainer = ModelTrainer(training=config.training, labels=config.labels, models=config.models)
    result = trainer.train_on_dataset_manifest(
        store=store,
        dataset_manifest=manifest,
        feature_schema_registry=feature_registry,
        label_policy_registry=label_registry,
    )

    assert result.metrics["rows_before_dedup"] == float(manifest.rows_before_dedup)
    assert result.metrics["rows_dropped_by_dedup"] == float(manifest.rows_dropped_by_dedup)
    dedup_metadata = result.artifact.metadata["dataset_dedup_quality"]
    assert dedup_metadata["dedup_rule"] == DEDUP_RULE
    assert dedup_metadata["rows_dropped_by_dedup"] == manifest.rows_dropped_by_dedup


def test_upstream_snapshot_write_is_idempotent_per_decision_date(tmp_path: Path) -> None:
    store = SampleStore(db_path=tmp_path / "upstream.duckdb")
    common = dict(
        code_version="git:test",
        feature_vector={"f": 1.0},
        feature_schema_id="fs",
        feature_schema_hash="fsh",
        runtime_config_hash="rc",
        label_policy_id="lp",
        label_policy_hash="lph",
    )
    first_time = datetime(2026, 5, 6, 3, 0, tzinfo=UTC)

    def _write(snapshot_id: str, strategy: str, decision_time: datetime) -> None:
        store.write_snapshot(
            SignalSnapshot(
                snapshot_id=snapshot_id,
                symbol="300483.SZ",
                strategy=strategy,
                decision_time=decision_time,
                **common,
            )
        )

    assert not store.has_snapshot_for_decision_date(
        symbol="300483.SZ", strategy="monster", decision_time=first_time
    )
    _write("snap-first", "monster", first_time)
    # 同 symbol+strategy 同日（含上海时区归一后的晚间时点）→ True。
    assert store.has_snapshot_for_decision_date(
        symbol="300483.SZ",
        strategy="monster",
        decision_time=first_time + timedelta(hours=11),
    )
    # 不同策略 / 不同 symbol / 次日 → False。
    assert not store.has_snapshot_for_decision_date(
        symbol="300483.SZ", strategy="trend", decision_time=first_time
    )
    assert not store.has_snapshot_for_decision_date(
        symbol="600000.SH", strategy="monster", decision_time=first_time
    )
    assert not store.has_snapshot_for_decision_date(
        symbol="300483.SZ",
        strategy="monster",
        decision_time=first_time + timedelta(days=1),
    )


def test_dedup_quality_flags_rules() -> None:
    blocking, warning = _dedup_quality_flags(rows_before=10, rows_dropped=6)
    assert blocking == ["duplicate_dominance"]
    assert warning == ["duplicate_rows_present"]

    blocking, warning = _dedup_quality_flags(rows_before=10, rows_dropped=5)
    assert blocking == []
    assert warning == ["duplicate_rows_present"]

    blocking, warning = _dedup_quality_flags(rows_before=4, rows_dropped=4)
    assert blocking == ["empty_after_dedup"]
    assert warning == ["duplicate_rows_present"]

    blocking, warning = _dedup_quality_flags(rows_before=0, rows_dropped=0)
    assert blocking == []
    assert warning == []


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _build_store_with_duplicates_force_dominance(tmp_path: Path):
    """构造丢弃占比 >50% 的数据集：每键 1 条保留 + 3 条重复。"""

    store = SampleStore(db_path=tmp_path / "dominance.duckdb")
    base_time = datetime(2026, 1, 1, 1, 30, tzinfo=UTC)
    ordinal = 0
    for day_offset in range(12):
        decision_time = base_time + timedelta(days=day_offset)
        for snap_time in (
            decision_time,
            decision_time + timedelta(hours=1),
            decision_time + timedelta(hours=2),
            decision_time + timedelta(hours=3),
        ):
            snapshot_id = f"snap-d{ordinal:03d}"
            store.write_snapshot(
                SignalSnapshot(
                    snapshot_id=snapshot_id,
                    code_version="git:test",
                    symbol="600000.SH",
                    strategy="trend",
                    decision_time=snap_time,
                    feature_vector={"feature_b": float(ordinal % 3), "feature_a": 0.5},
                    feature_schema_id="fs",
                    feature_schema_hash="fsh",
                    runtime_config_hash="rc",
                    label_policy_id="lp",
                    label_policy_hash="lph",
                )
            )
            store.upsert_outcome(
                OutcomeRecord(
                    snapshot_id=snapshot_id,
                    maturity_status=MaturityStatus.RECONCILED,
                    label_mature_time=snap_time + timedelta(days=7),
                    realized_return=0.08 if ordinal % 2 == 0 else -0.05,
                    max_favorable_excursion=0.09 if ordinal % 2 == 0 else 0.01,
                    max_adverse_excursion=-0.01 if ordinal % 2 == 0 else -0.07,
                    backfill_fidelity_tier=BackfillFidelityTier.GOLD,
                    backfill_source="runtime_observed",
                )
            )
            ordinal += 1
    return store, "fs", "fsh", "lp", "lph"


def _minimal_training_config():
    from stock_analyzer.config import TrainingConfig

    return TrainingConfig(min_samples=5)


def _minimal_labels_config():
    from stock_analyzer.config import LabelsConfig

    return LabelsConfig(
        primary="soup_5d_tp5_before_sl5",
        take_profit_pct=0.05,
        stop_loss_pct=0.05,
        horizon_days=5,
        exclude_untradable=True,
        pnl_price_basis="next_tradable_vwap",
        conflict_policy="conservative_zero",
        conflict_soft_label_value=0.5,
    )
