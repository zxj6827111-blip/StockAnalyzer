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


def test_decision_day_split_purges_label_availability_overlap(tmp_path: Path) -> None:
    """A1：按决策日整日 purge，逐边界断言标签可用性隔离。

    24 个连续决策日 × 3 只票，label_mature = decision + 2 天。比例 0.25/0.25 的
    目标尺寸（test=6/cal=6/train=12）可行，但必须剔除 train 尾部与 cal 尾部各两个
    决策日，否则标签在预测时尚未可知。
    """

    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    builder = DatasetManifestBuilder(store=store)
    rows = _write_daily_cross_sections(
        store,
        day_count=24,
        symbols=("600000.SH", "600001.SH", "600002.SH"),
        horizon_days=2,
    )
    assert rows == 72

    manifest = builder.create_manifest(
        feature_schema_id="feature_schema_v1_abc",
        feature_schema_hash="feature_hash_1",
        label_policy_id="label_policy_v1_abc",
        label_policy_hash="label_hash_1",
        fidelity_filter=[BackfillFidelityTier.GOLD],
        calibration_ratio=0.25,
        test_ratio=0.25,
        embargo_days=2,
    )

    report = manifest.split_isolation_report
    assert report["policy"] == "decision_day_label_availability_purge_v1"
    assert report["status"] == "isolated"
    # 逐边界判据：max(前段截面标签可用时间) < min(后段决策时间)，两侧决策日粒度。
    boundaries = {item["name"]: item for item in report["boundaries"]}  # type: ignore[union-attr]
    assert set(boundaries) == {"train->calibration", "calibration->test"}
    for name in ("train->calibration", "calibration->test"):
        boundary = boundaries[name]
        assert boundary["satisfied"] is True
        assert datetime.fromisoformat(
            boundary["prev_max_label_available"]
        ) < datetime.fromisoformat(boundary["next_min_decision"])
    assert report["violations"] == 0
    # 账本：purge 前 = 成员 + 剔除，且两者都在报告里。
    assert report["rows_before_purge"] == 72
    assert manifest.purged_decision_days == 4
    assert manifest.purged_rows == 12
    assert manifest.included_snapshot_count == 60
    assert report["rows_before_purge"] == manifest.included_snapshot_count + manifest.purged_rows
    assert report["embargo_gap_trading_days"] == {
        "train->calibration": 2,
        "calibration->test": 2,
    }
    # 四项目报告：决策日自然日跨度 / 有效交易日数 / 成熟日范围 / 标签可用性边界。
    splits = report["splits"]
    assert splits["train"]["effective_trading_days"] == 8  # type: ignore[index]
    assert splits["calibration"]["effective_trading_days"] == 6  # type: ignore[index]
    assert splits["test"]["effective_trading_days"] == 6  # type: ignore[index]
    for split_name in ("train", "calibration", "test"):
        metrics = splits[split_name]  # type: ignore[index]
        assert metrics["decision_span_calendar_days"] >= metrics["effective_trading_days"]
        assert len(metrics["maturity_range"]) == 2
        assert metrics["max_label_available_time"]


def test_decision_day_purge_keeps_cross_sections_whole(tmp_path: Path) -> None:
    """被剔除的是整日截面：某日要么 3 行全在，要么 3 行全不在。"""

    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    builder = DatasetManifestBuilder(store=store)
    _write_daily_cross_sections(
        store,
        day_count=24,
        symbols=("600000.SH", "600001.SH", "600002.SH"),
        horizon_days=2,
    )
    manifest = builder.create_manifest(
        feature_schema_id="feature_schema_v1_abc",
        feature_schema_hash="feature_hash_1",
        label_policy_id="label_policy_v1_abc",
        label_policy_hash="label_hash_1",
        fidelity_filter=[BackfillFidelityTier.GOLD],
        calibration_ratio=0.25,
        test_ratio=0.25,
        embargo_days=2,
    )

    rows_per_day: dict[str, int] = {}
    for item in store.list_manifest_items(manifest.dataset_manifest_id):
        day = (item.decision_time + timedelta(hours=8)).date().isoformat()
        rows_per_day[day] = rows_per_day.get(day, 0) + 1
    assert set(rows_per_day.values()) == {3}
    assert len(rows_per_day) == 20
    all_days = {
        (snapshot.decision_time + timedelta(hours=8)).date().isoformat()
        for snapshot in store.list_snapshots()
    }
    assert len(all_days - set(rows_per_day)) == manifest.purged_decision_days


def test_decision_day_purge_keeps_calibration_and_train_non_empty(tmp_path: Path) -> None:
    """回归 PR#20：隔离改造不得把 calibration / train 清空。"""

    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    builder = DatasetManifestBuilder(store=store)
    _write_daily_cross_sections(
        store,
        day_count=24,
        symbols=("600000.SH",),
        horizon_days=2,
    )
    manifest = builder.create_manifest(
        feature_schema_id="feature_schema_v1_abc",
        feature_schema_hash="feature_hash_1",
        label_policy_id="label_policy_v1_abc",
        label_policy_hash="label_hash_1",
        fidelity_filter=[BackfillFidelityTier.GOLD],
        calibration_ratio=0.1,
        test_ratio=0.1,
        embargo_days=2,
    )

    split_counts = {entry.split_name: entry.row_count for entry in manifest.split_plan}
    assert set(split_counts) == {"train", "calibration", "test"}
    assert all(count > 0 for count in split_counts.values())
    assert manifest.manifest_quality_flags == []


def test_decision_day_purge_degrades_split_sizes_before_failing(tmp_path: Path) -> None:
    """目标尺寸不可行时缩小 test/cal 直到判据成立，并标记 degraded。"""

    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    builder = DatasetManifestBuilder(store=store)
    # 10 个连续决策日、label 窗口 3 天：0.4/0.4 的目标尺寸装不进隔离所需间隔。
    _write_daily_cross_sections(
        store,
        day_count=10,
        symbols=("600000.SH",),
        horizon_days=3,
    )
    manifest = builder.create_manifest(
        feature_schema_id="feature_schema_v1_abc",
        feature_schema_hash="feature_hash_1",
        label_policy_id="label_policy_v1_abc",
        label_policy_hash="label_hash_1",
        fidelity_filter=[BackfillFidelityTier.GOLD],
        calibration_ratio=0.4,
        test_ratio=0.4,
        embargo_days=3,
    )

    report = manifest.split_isolation_report
    assert report["status"] == "isolated_degraded"
    assert "label_availability_split_degraded" in manifest.warning_quality_flags
    assert report["violations"] == 0
    boundaries = {item["name"]: item for item in report["boundaries"]}  # type: ignore[union-attr]
    for name in ("train->calibration", "calibration->test"):
        assert boundaries[name]["satisfied"] is True
    assert manifest.manifest_quality_flags == []


def test_decision_day_purge_infeasible_flags_blocking(tmp_path: Path) -> None:
    """判据不可满足时标记 infeasible，trainer 据此 fail-closed。"""

    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    builder = DatasetManifestBuilder(store=store)
    # 12 个连续决策日、label 窗口 5 天：最小可行布局需要 15 天，隔离不可满足。
    _write_daily_cross_sections(
        store,
        day_count=12,
        symbols=("600000.SH",),
        horizon_days=5,
    )
    manifest = builder.create_manifest(
        feature_schema_id="feature_schema_v1_abc",
        feature_schema_hash="feature_hash_1",
        label_policy_id="label_policy_v1_abc",
        label_policy_hash="label_hash_1",
        fidelity_filter=[BackfillFidelityTier.GOLD],
        calibration_ratio=0.25,
        test_ratio=0.25,
        embargo_days=5,
    )

    assert manifest.split_isolation_report["status"] == "infeasible"
    assert "label_availability_purge_infeasible" in manifest.manifest_quality_flags
    assert manifest.purged_rows == 0


def test_decision_day_purge_records_late_maturing_samples(tmp_path: Path) -> None:
    """异常延迟成熟样本必须计数留样，否则 purge 规模无法解释。"""

    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    builder = DatasetManifestBuilder(store=store)
    _write_daily_cross_sections(
        store,
        day_count=24,
        symbols=("600000.SH", "600001.SH"),
        horizon_days=2,
    )
    # 第 5 天的一行成熟时间远超名义窗口（decision + 30 天）。
    store.upsert_outcome(
        OutcomeRecord(
            snapshot_id="snap-005-600001.SH",
            maturity_status=MaturityStatus.RECONCILED,
            label_mature_time=datetime(2026, 1, 1, 14, 30, tzinfo=UTC) + timedelta(days=5 + 30),
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
        embargo_days=2,
    )

    report = manifest.split_isolation_report
    assert report["late_maturing_row_count"] == 1
    examples = report["late_maturing_examples"]
    assert len(examples) == 1
    assert examples[0]["snapshot_id"] == "snap-005-600001.SH"  # type: ignore[index]
    # 该异常截面被整日剔除，且整个截面（2 行）一起走。
    assert manifest.purged_rows >= 2


def test_manifest_isolation_report_round_trips_through_store(tmp_path: Path) -> None:
    """报告必须持久化：重新读回的 manifest 与首次创建的一致。"""

    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    builder = DatasetManifestBuilder(store=store)
    _write_daily_cross_sections(
        store,
        day_count=24,
        symbols=("600000.SH", "600001.SH"),
        horizon_days=2,
    )
    created = builder.create_manifest(
        feature_schema_id="feature_schema_v1_abc",
        feature_schema_hash="feature_hash_1",
        label_policy_id="label_policy_v1_abc",
        label_policy_hash="label_hash_1",
        fidelity_filter=[BackfillFidelityTier.GOLD],
        calibration_ratio=0.25,
        test_ratio=0.25,
        embargo_days=2,
    )

    reloaded = store.get_manifest(created.dataset_manifest_id)
    assert reloaded is not None
    assert reloaded.purged_rows == created.purged_rows
    assert reloaded.purged_decision_days == created.purged_decision_days
    assert reloaded.split_isolation_report == created.split_isolation_report
    assert reloaded.split_isolation_report["violations"] == 0


def test_manifest_invariant_holds_when_embargo_disabled(tmp_path: Path) -> None:
    """未启用 embargo（embargo_days=0）时不写隔离报告，保持旧口径。"""

    store = SampleStore(db_path=tmp_path / "sample_store.duckdb")
    builder = DatasetManifestBuilder(store=store)
    _write_daily_cross_sections(
        store,
        day_count=24,
        symbols=("600000.SH",),
        horizon_days=2,
    )
    manifest = builder.create_manifest(
        feature_schema_id="feature_schema_v1_abc",
        feature_schema_hash="feature_hash_1",
        label_policy_id="label_policy_v1_abc",
        label_policy_hash="label_hash_1",
        fidelity_filter=[BackfillFidelityTier.GOLD],
    )

    assert manifest.split_isolation_report == {}
    assert manifest.purged_rows == 0
    assert manifest.purged_decision_days == 0


def _write_daily_cross_sections(
    store: SampleStore,
    *,
    day_count: int,
    symbols: tuple[str, ...],
    horizon_days: int,
    base_time: datetime | None = None,
) -> int:
    """写入 day_count 个连续决策日 × 每个 symbol 一行的截面，返回行数。"""

    start = base_time or datetime(2026, 1, 1, 14, 30, tzinfo=UTC)
    rows = 0
    for index in range(day_count):
        decision_time = start + timedelta(days=index)
        for symbol in symbols:
            snapshot_id = f"snap-{index:03d}-{symbol}"
            store.write_snapshot(
                _build_snapshot(
                    snapshot_id,
                    decision_time.isoformat(),
                    symbol=symbol,
                )
            )
            store.upsert_outcome(
                OutcomeRecord(
                    snapshot_id=snapshot_id,
                    maturity_status=MaturityStatus.RECONCILED,
                    label_mature_time=decision_time + timedelta(days=horizon_days),
                    realized_return=0.05,
                    backfill_fidelity_tier=BackfillFidelityTier.GOLD,
                    backfill_source="runtime_observed",
                )
            )
            rows += 1
    return rows


def _build_snapshot(
    snapshot_id: str,
    decision_time: str,
    *,
    symbol: str = "600000.SH",
    feature_schema_id: str = "feature_schema_v1_abc",
    feature_schema_hash: str = "feature_hash_1",
    feature_vector: dict[str, float] | None = None,
) -> SignalSnapshot:
    return SignalSnapshot(
        snapshot_id=snapshot_id,
        code_version="git:test",
        symbol=symbol,
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
