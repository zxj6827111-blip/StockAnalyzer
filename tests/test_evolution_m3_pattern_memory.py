from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from stock_analyzer.evolution.modules.m3_pattern_memory import PatternMemoryStore


def _set_mtime(path: Path, timestamp: datetime) -> None:
    ts = timestamp.timestamp()
    os.utime(path, (ts, ts))


def test_m3_pattern_memory_append_and_search(tmp_path: Path) -> None:
    store = PatternMemoryStore(base_dir=tmp_path / "m3", vector_dim=5, batch_size=2)
    vectors = np.asarray(
        [
            [1.0, 1.1, 0.9, 1.0, 10.0],
            [2.0, 2.1, 1.9, 2.0, 12.0],
            [3.0, 3.1, 2.9, 3.0, 14.0],
        ],
        dtype=float,
    )
    append = store.append(vectors)
    assert append.appended == 3
    assert append.total == 3

    result = store.search(np.asarray([[2.0, 2.1, 1.9, 2.0, 12.0]], dtype=float), top_k=2)
    assert len(result.indices) == 2
    assert result.indices[0] in {0, 1, 2}


def test_m3_safe_delete_rename_and_purge(tmp_path: Path) -> None:
    store = PatternMemoryStore(base_dir=tmp_path / "m3", vector_dim=5)
    snapshot = store.create_snapshot(now=datetime(2026, 3, 1, tzinfo=UTC))
    pending = store.safe_remove_snapshot(
        snapshot_path=snapshot, now=datetime(2026, 3, 1, tzinfo=UTC)
    )
    assert pending.exists() is True

    _set_mtime(pending, datetime(2026, 2, 28, tzinfo=UTC))
    purged = store.purge_pending(now=datetime(2026, 3, 1, tzinfo=UTC) + timedelta(hours=25))
    assert pending in purged
    assert pending.exists() is False


def test_m3_pattern_memory_isolates_vectors_by_profile_id(tmp_path: Path) -> None:
    legacy = PatternMemoryStore(
        base_dir=tmp_path / "m3",
        vector_dim=5,
        vector_profile_id="m3_price_volume_v1",
    )
    current = PatternMemoryStore(
        base_dir=tmp_path / "m3",
        vector_dim=20,
        vector_profile_id="m3_price_shape_execution_v2",
    )

    legacy.append(np.asarray([[1.0, 1.1, 0.9, 1.0, 10.0]], dtype=float))
    current.append(np.asarray([list(range(20))], dtype=float))

    assert legacy.count() == 1
    assert current.count() == 1
    assert (tmp_path / "m3" / "profiles" / "m3_price_volume_v1" / "vectors.npy").exists() is True
    assert (
        tmp_path / "m3" / "profiles" / "m3_price_shape_execution_v2" / "vectors.npy"
    ).exists() is True
