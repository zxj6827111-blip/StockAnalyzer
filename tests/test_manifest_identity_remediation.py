"""Commit B manifest identity and persistence regression tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stock_analyzer.learning.dataset_manifest import (
    DatasetManifestBuilder,
    _build_dataset_manifest_id,
)
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    MaturityStatus,
    OutcomeRecord,
    SignalSnapshot,
)
from stock_analyzer.learning.sample_store import SampleStore


def _store(tmp_path: Path) -> SampleStore:
    store = SampleStore(db_path=tmp_path / "store.duckdb")
    now = datetime(2026, 1, 1, 1, tzinfo=UTC)
    for index in range(5):
        snapshot_id = f"snap-{index}"
        snapshot = SignalSnapshot(
            snapshot_id=snapshot_id,
            code_version="git:test",
            symbol="600000.SH",
            strategy="trend",
            decision_time=now + timedelta(days=index),
            created_at=now + timedelta(days=index, minutes=1),
            feature_vector={"f": float(index)},
            feature_schema_id="fs",
            feature_schema_hash="fsh",
            runtime_config_hash="rc",
            label_policy_id="lp",
            label_policy_hash="lph",
        )
        store.write_snapshot(snapshot)
        store.upsert_outcome(
            OutcomeRecord(
                snapshot_id=snapshot_id,
                maturity_status=MaturityStatus.RECONCILED,
                label_mature_time=snapshot.decision_time + timedelta(days=5),
                realized_return=0.08 if index % 2 == 0 else -0.04,
                max_favorable_excursion=0.08 if index % 2 == 0 else 0.01,
                max_adverse_excursion=-0.01 if index % 2 == 0 else -0.07,
                backfill_fidelity_tier=BackfillFidelityTier.GOLD,
                backfill_source="test",
            )
        )
    return store


def test_manifest_identity_changes_with_schema_and_dedup_rule(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = DatasetManifestBuilder(store=store).create_manifest(
        feature_schema_id="fs",
        feature_schema_hash="fsh",
        label_policy_id="lp",
        label_policy_hash="lph",
    )
    # 端到端：新 manifest 的 ID 必须携带 v2 前缀。
    assert manifest.dataset_manifest_id.startswith("dataset_manifest_v2_")

    # 单元级：同一份成员/协议绑定输入下，直接驱动 ID 派生函数——
    # schema_version 或 dedup_rule 任一变化都必须改变派生 ID（防止
    # 去重协议演进后新旧身份撞车）。
    base_kwargs = dict(
        source_store_version="test",
        feature_schema_id="fs",
        feature_schema_hash="fsh",
        label_policy_id="lp",
        label_policy_hash="lph",
        sample_selection_rule="all",
        time_window_start=None,
        time_window_end=None,
        fidelity_filter=[],
        snapshot_ids=["snap-0", "snap-1"],
        item_blueprints=[
            {"snapshot_id": "snap-0", "split_name": "train", "ordinal": 0},
            {"snapshot_id": "snap-1", "split_name": "test", "ordinal": 1},
        ],
    )
    v2_id = _build_dataset_manifest_id(
        schema_version="2",
        dedup_key="symbol+strategy+decision_date_sh",
        dedup_rule="keep_first_by_decision_time",
        **base_kwargs,
    )
    v1_id = _build_dataset_manifest_id(
        schema_version="1",
        dedup_key="symbol+strategy+decision_date_sh",
        dedup_rule="keep_first_by_decision_time",
        **base_kwargs,
    )
    assert v2_id != v1_id
    assert v1_id.startswith("dataset_manifest_v1_")

    alt_rule_id = _build_dataset_manifest_id(
        schema_version="2",
        dedup_key="symbol+strategy+decision_date_sh",
        dedup_rule="keep_last_by_decision_time",
        **base_kwargs,
    )
    assert alt_rule_id != v2_id

    alt_key_id = _build_dataset_manifest_id(
        schema_version="2",
        dedup_key="symbol+decision_date_sh",
        dedup_rule="keep_first_by_decision_time",
        **base_kwargs,
    )
    assert alt_key_id != v2_id


def test_v2_id_never_returns_v1_record_on_schema_collision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = DatasetManifestBuilder(store=store).create_manifest(
        feature_schema_id="fs",
        feature_schema_hash="fsh",
        label_policy_id="lp",
        label_policy_hash="lph",
    )
    # 以同 ID 写入一个 v1 记录，验证 builder 的跨 schema 防御路径。
    conn = store._connect()
    try:
        conn.execute(
            "UPDATE dataset_manifests SET schema_version = '1' WHERE dataset_manifest_id = ?",
            [manifest.dataset_manifest_id],
        )
    finally:
        conn.close()
    with pytest.raises(ValueError, match="collision across schema versions"):
        DatasetManifestBuilder(store=store).create_manifest(
            feature_schema_id="fs",
            feature_schema_hash="fsh",
            label_policy_id="lp",
            label_policy_hash="lph",
        )


def test_manifest_flags_json_round_trip_is_stable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = DatasetManifestBuilder(store=store).create_manifest(
        feature_schema_id="fs",
        feature_schema_hash="fsh",
        label_policy_id="lp",
        label_policy_hash="lph",
    )
    reloaded = store.get_manifest(manifest.dataset_manifest_id)
    assert reloaded is not None
    assert reloaded.blocking_quality_flags == manifest.blocking_quality_flags
    assert reloaded.warning_quality_flags == manifest.warning_quality_flags
    assert reloaded.model_dump(mode="json")["blocking_quality_flags"] == []


def test_manifest_creation_is_idempotent_twice(tmp_path: Path) -> None:
    store = _store(tmp_path)
    builder = DatasetManifestBuilder(store=store)
    kwargs = dict(
        feature_schema_id="fs",
        feature_schema_hash="fsh",
        label_policy_id="lp",
        label_policy_hash="lph",
    )
    first = builder.create_manifest(**kwargs)
    second = builder.create_manifest(**kwargs)
    assert first.dataset_manifest_id == second.dataset_manifest_id
    assert len(store.list_manifests(limit=100)) == 1
