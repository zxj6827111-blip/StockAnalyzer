from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from stock_analyzer.learning.dataset_manifest import DatasetManifestBuilder
from stock_analyzer.learning.feature_schema_registry import FeatureSchemaRegistry
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    MaturityStatus,
    OutcomeRecord,
    SignalSnapshot,
)
from stock_analyzer.learning.sample_store import SampleStore


def test_dataset_manifest_builder_filters_candidates_and_persists_membership(
    tmp_path: Path,
) -> None:
    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    builder = DatasetManifestBuilder(store=store)

    snapshots = [
        _build_snapshot("snap-001", "2026-03-01T14:30:00+00:00"),
        _build_snapshot("snap-002", "2026-03-02T14:30:00+00:00"),
        _build_snapshot("snap-003", "2026-03-03T14:30:00+00:00"),
        _build_snapshot("snap-004", "2026-03-04T14:30:00+00:00"),
        _build_snapshot("snap-005", "2026-03-05T14:30:00+00:00"),
        _build_snapshot(
            "snap-006",
            "2026-03-06T14:30:00+00:00",
            feature_schema_hash="feature_hash_other",
        ),
        _build_snapshot("snap-007", "2026-03-07T14:30:00+00:00"),
    ]
    outcomes = [
        _build_outcome("snap-001", maturity_status=MaturityStatus.LABEL_MATURED),
        _build_outcome("snap-002", maturity_status=MaturityStatus.RECONCILED),
        _build_outcome("snap-003", maturity_status=MaturityStatus.FULLY_MATURED),
        _build_outcome("snap-004", maturity_status=MaturityStatus.PENDING),
        _build_outcome(
            "snap-005",
            maturity_status=MaturityStatus.RECONCILED,
            fidelity_tier=BackfillFidelityTier.BRONZE,
        ),
        _build_outcome("snap-006", maturity_status=MaturityStatus.RECONCILED),
    ]

    for snapshot in snapshots:
        store.write_snapshot(snapshot)
    for outcome in outcomes:
        store.upsert_outcome(outcome)

    manifest = builder.create_manifest(
        feature_schema_id="feature_schema_v1_abc",
        feature_schema_hash="feature_hash_1",
        label_policy_id="label_policy_v1_abc",
        label_policy_hash="label_hash_1",
        fidelity_filter=[BackfillFidelityTier.GOLD, BackfillFidelityTier.SILVER],
    )

    assert manifest.included_snapshot_count == 3
    assert manifest.included_outcome_count == 3
    assert manifest.fidelity_breakdown == {"gold": 3}
    assert manifest.dropped_reason_breakdown == {
        "maturity_filtered:pending": 1,
        "fidelity_filtered:bronze": 1,
        "feature_schema_hash_mismatch": 1,
        "missing_outcome": 1,
    }
    assert [item.split_name for item in manifest.split_plan] == [
        "train",
        "calibration",
        "test",
    ]
    assert store.list_manifest_snapshot_ids(manifest.dataset_manifest_id) == [
        "snap-001",
        "snap-002",
        "snap-003",
    ]


def test_dataset_manifest_builder_is_deterministic_and_idempotent(tmp_path: Path) -> None:
    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    builder = DatasetManifestBuilder(store=store)

    for snapshot_id, decision_time in (
        ("snap-001", "2026-03-01T14:30:00+00:00"),
        ("snap-002", "2026-03-02T14:30:00+00:00"),
        ("snap-003", "2026-03-03T14:30:00+00:00"),
        ("snap-004", "2026-03-04T14:30:00+00:00"),
    ):
        store.write_snapshot(_build_snapshot(snapshot_id, decision_time))
        store.upsert_outcome(_build_outcome(snapshot_id, maturity_status=MaturityStatus.RECONCILED))

    first = builder.create_manifest(
        feature_schema_id="feature_schema_v1_abc",
        feature_schema_hash="feature_hash_1",
        label_policy_id="label_policy_v1_abc",
        label_policy_hash="label_hash_1",
        fidelity_filter=[BackfillFidelityTier.GOLD],
    )
    second = builder.create_manifest(
        feature_schema_id="feature_schema_v1_abc",
        feature_schema_hash="feature_hash_1",
        label_policy_id="label_policy_v1_abc",
        label_policy_hash="label_hash_1",
        fidelity_filter=[BackfillFidelityTier.GOLD],
    )

    assert first.dataset_manifest_id == second.dataset_manifest_id
    assert store.counts()["dataset_manifests"] == 1
    assert [item.split_name for item in store.list_manifest_items(first.dataset_manifest_id)] == [
        "train",
        "train",
        "calibration",
        "test",
    ]


def test_dataset_manifest_builder_includes_projection_compatible_legacy_snapshots(
    tmp_path: Path,
) -> None:
    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    registry = FeatureSchemaRegistry(db_path=tmp_path / "feature_schema.duckdb")
    legacy = registry.register_feature_names(
        ["ret_1d", "atr14"],
        feature_schema_id="feature_schema_legacy",
        feature_engineer_version="test",
        code_version="git:test",
    )
    current = registry.register_feature_names(
        ["ret_1d", "volume_ratio_5", "atr14"],
        feature_schema_id="feature_schema_current",
        feature_engineer_version="test",
        code_version="git:test",
        projection_compatible_from=[legacy.feature_schema_id],
    )
    builder = DatasetManifestBuilder(store=store, feature_schema_registry=registry)

    store.write_snapshot(
        _build_snapshot(
            "snap-legacy",
            "2026-03-01T14:30:00+00:00",
            feature_schema_id=legacy.feature_schema_id,
            feature_schema_hash=legacy.feature_schema_hash,
            feature_vector={"ret_1d": 0.01, "atr14": 0.4},
        )
    )
    store.write_snapshot(
        _build_snapshot(
            "snap-current",
            "2026-03-02T14:30:00+00:00",
            feature_schema_id=current.feature_schema_id,
            feature_schema_hash=current.feature_schema_hash,
            feature_vector={"ret_1d": 0.02, "volume_ratio_5": 1.1, "atr14": 0.5},
        )
    )
    store.upsert_outcome(_build_outcome("snap-legacy", maturity_status=MaturityStatus.RECONCILED))
    store.upsert_outcome(_build_outcome("snap-current", maturity_status=MaturityStatus.RECONCILED))

    manifest = builder.create_manifest(
        feature_schema_id=current.feature_schema_id,
        feature_schema_hash=current.feature_schema_hash,
        label_policy_id="label_policy_v1_abc",
        label_policy_hash="label_hash_1",
        fidelity_filter=[BackfillFidelityTier.GOLD],
    )

    assert manifest.feature_schema_id == current.feature_schema_id
    assert manifest.included_snapshot_count == 2
    assert manifest.dropped_reason_breakdown == {}
    assert store.list_manifest_snapshot_ids(manifest.dataset_manifest_id) == [
        "snap-legacy",
        "snap-current",
    ]


def test_dataset_manifest_builder_purges_label_windows_crossing_split_boundary(
    tmp_path: Path,
) -> None:
    """embargo 分组切分按 label 成熟日归集，calibration 集合不会被清空。

    用「交易日」间隔的样本（每天一条），label_mature_time 各不相同，验证：
    样本按其 label 成熟日落入对应集合（同日成熟同集合），train/calibration/
    test 三个集合全部非空——回归旧 purge 逻辑把 calibration 清空导致训练失败
    的缺陷。
    """
    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    builder = DatasetManifestBuilder(store=store)

    # 8 个交易日，比例 0.25/0.25 会得到 4 train / 2 calibration / 2 test。
    for index in range(8):
        decision_time = datetime(2026, 1, 1, 14, 30, tzinfo=UTC) + timedelta(days=index)
        store.write_snapshot(
            _build_snapshot(
                f"snap-{index:03d}",
                decision_time.isoformat(),
            )
        )
        # 前 3 天标签成熟得早；其余样本 label 窗口较长，跨越多个集合边界。
        if index <= 2:
            mature = decision_time + timedelta(days=1)
        else:
            mature = decision_time + timedelta(days=10)
        store.upsert_outcome(
            OutcomeRecord(
                snapshot_id=f"snap-{index:03d}",
                maturity_status=MaturityStatus.RECONCILED,
                label_mature_time=mature,
                realized_return=0.05,
                backfill_fidelity_tier=BackfillFidelityTier.GOLD,
                backfill_source="runtime_observed",
            )
        )

    manifest = builder.create_manifest(
        feature_schema_id="feature_schema_v1_abc",
        feature_schema_hash="feature_hash_1",
        label_policy_id="label_policy_v1_abc",
        label_policy_hash="label_hash_1",
        fidelity_filter=[BackfillFidelityTier.GOLD],
        calibration_ratio=0.25,
        test_ratio=0.25,
        embargo_days=7,
    )

    items = store.list_manifest_items(manifest.dataset_manifest_id)
    # 结构性 embargo：同一 label 成熟日的样本必须落在同一 split（无同日
    # 标签跨集合泄漏），且没有样本被事后 purge 掉。
    assert manifest.included_snapshot_count == 8
    outcome_map = {outcome.snapshot_id: outcome for outcome in store.list_outcomes()}
    by_mature_date: dict[str, set[str]] = {}
    for item in items:
        mature = outcome_map[item.snapshot_id].label_mature_time
        assert mature is not None
        by_mature_date.setdefault(str(mature.date()), set()).add(item.split_name)
    assert all(len(splits) == 1 for splits in by_mature_date.values())

    # 回归：三集合全部非空——旧逻辑会因标签窗口跨入下一集合而清空
    # calibration，导致训练端 "empty train/calibration/test set" 失败。
    split_names = {item.split_name for item in items}
    assert split_names == {"train", "calibration", "test"}


def _build_snapshot(
    snapshot_id: str,
    decision_time: str,
    *,
    feature_schema_id: str = "feature_schema_v1_abc",
    feature_schema_hash: str = "feature_hash_1",
    feature_vector: dict[str, float] | None = None,
) -> SignalSnapshot:
    return SignalSnapshot(
        snapshot_id=snapshot_id,
        code_version="git:test",
        symbol="600000.SH",
        strategy="trend",
        decision_time=datetime.fromisoformat(decision_time).astimezone(UTC),
        feature_vector=feature_vector or {"ret_1d": 0.01, "atr14": 0.4},
        feature_schema_id=feature_schema_id,
        feature_schema_hash=feature_schema_hash,
        runtime_config_hash="runtime_hash_1",
        label_policy_id="label_policy_v1_abc",
        label_policy_hash="label_hash_1",
    )


def _build_outcome(
    snapshot_id: str,
    *,
    maturity_status: MaturityStatus,
    fidelity_tier: BackfillFidelityTier = BackfillFidelityTier.GOLD,
) -> OutcomeRecord:
    return OutcomeRecord(
        snapshot_id=snapshot_id,
        maturity_status=maturity_status,
        label_mature_time=datetime(2026, 3, 10, 15, 0, tzinfo=UTC),
        realized_return=0.05,
        backfill_fidelity_tier=fidelity_tier,
        backfill_source="runtime_observed",
    )
