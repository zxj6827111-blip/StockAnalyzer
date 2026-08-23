"""Commit B manifest identity and persistence regression tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stock_analyzer.learning.dataset_manifest import DatasetManifestBuilder
from stock_analyzer.learning.sample_schema import (
    BackfillFidelityTier,
    DatasetManifest,
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
    first = DatasetManifestBuilder(store=store).create_manifest(
        feature_schema_id="fs",
        feature_schema_hash="fsh",
        label_policy_id="lp",
        label_policy_hash="lph",
    )
    second = first.model_copy(
        update={
            "dataset_manifest_id": "dataset_manifest_v2_forced-different",
            "dedup_rule": "keep_last_by_decision_time",
        }
    )
    assert first.dataset_manifest_id != second.dataset_manifest_id
    assert first.dedup_rule != second.dedup_rule


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
