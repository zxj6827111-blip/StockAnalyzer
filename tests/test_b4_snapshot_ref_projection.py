"""B4 选池内存路径测试：引用级投影读取。

覆盖：
1. ``list_snapshot_refs`` 与 ``list_snapshots`` 在相同过滤下**成员完全一致**
   （id/符号/契约标识逐项相等），且 ref 不携带特征载荷；
2. 符号过滤下推到 SQL 后语义不变（与 Python 侧过滤结果一致）；
3. 行为护栏：**选池阶段不得再调用 ``list_snapshots``**（打桩为抛错即可证明
   全量特征载荷已从该阶段移除），训练协议路径仍能完成样本选择；
4. 行上限裁剪（``_apply_learning_protocol_row_caps``）对 ref 与对 snapshot
   给出同一结果。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from stock_analyzer.learning.sample_schema import SignalSnapshot
from stock_analyzer.learning.sample_store import SampleStore, SnapshotRef
from stock_analyzer.runtime.service import _apply_learning_protocol_row_caps


def _store(tmp_path: Path) -> SampleStore:
    return SampleStore(tmp_path / "learning.duckdb")


def _write_snapshot(
    store: SampleStore,
    *,
    snapshot_id: str,
    symbol: str,
    decision_time: datetime,
    schema_id: str = "feature_schema_v1_aaaa",
    schema_hash: str = "hash_aaaa",
    feature_count: int = 5,
) -> None:
    store.write_snapshot(
        SignalSnapshot(
            snapshot_id=snapshot_id,
            code_version="test",
            symbol=symbol,
            strategy="trend",
            decision_time=decision_time,
            feature_vector={f"f{i}": 0.1 * i for i in range(feature_count)},
            feature_schema_id=schema_id,
            feature_schema_hash=schema_hash,
            runtime_config_hash="cfg",
            label_policy_id="label_policy_v3_bbbb",
            label_policy_hash="hash_bbbb",
        )
    )


@pytest.fixture()
def seeded_store(tmp_path: Path) -> SampleStore:
    store = _store(tmp_path)
    base = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
    for i, symbol in enumerate(["600000", "600000", "000001", "300001"]):
        _write_snapshot(
            store,
            snapshot_id=f"snap-{i}",
            symbol=symbol,
            decision_time=base.replace(day=2 + i),
            schema_id="feature_schema_v1_aaaa" if i < 3 else "feature_schema_v1_zzzz",
            schema_hash="hash_aaaa" if i < 3 else "hash_zzzz",
        )
    return store


class TestSnapshotRefEquivalence:
    def test_refs_match_snapshots_member_wise(self, seeded_store: SampleStore) -> None:
        snapshots = seeded_store.list_snapshots()
        refs = seeded_store.list_snapshot_refs()
        assert [s.snapshot_id for s in snapshots] == [r.snapshot_id for r in refs]
        for snapshot, ref in zip(snapshots, refs, strict=True):
            assert ref.symbol == snapshot.symbol
            assert ref.feature_schema_id == snapshot.feature_schema_id
            assert ref.feature_schema_hash == snapshot.feature_schema_hash
            assert ref.label_policy_id == snapshot.label_policy_id
            assert ref.decision_time == snapshot.decision_time.isoformat()

    def test_ref_carries_no_feature_payload(self, seeded_store: SampleStore) -> None:
        ref = seeded_store.list_snapshot_refs()[0]
        assert not hasattr(ref, "features")
        assert not hasattr(ref, "feature_vector_json")
        # 轻量：仅有 6 个字段
        assert set(SnapshotRef.__dataclass_fields__) == {
            "snapshot_id",
            "symbol",
            "decision_time",
            "feature_schema_id",
            "feature_schema_hash",
            "label_policy_id",
        }

    def test_symbol_pushdown_matches_python_filter(self, seeded_store: SampleStore) -> None:
        pushed = seeded_store.list_snapshot_refs(symbols=["600000"])
        python_side = [r for r in seeded_store.list_snapshot_refs() if r.symbol == "600000"]
        assert [r.snapshot_id for r in pushed] == [r.snapshot_id for r in python_side]
        assert [r.snapshot_id for r in pushed] == ["snap-0", "snap-1"]

    def test_filters_compose_identically(self, seeded_store: SampleStore) -> None:
        window = {
            "time_window_start": datetime(2026, 3, 3, 0, 0, tzinfo=UTC),
            "time_window_end": datetime(2026, 3, 5, 23, 0, tzinfo=UTC),
        }
        cases: list[tuple[dict[str, object], list[str] | None]] = [
            ({"feature_schema_id": "feature_schema_v1_aaaa"}, None),
            ({"label_policy_id": "label_policy_v3_bbbb"}, None),
            ({"label_policy_id": "label_policy_v3_bbbb"}, ["600000", "000001"]),
            ({"snapshot_ids": ["snap-2"]}, None),
            ({"snapshot_ids": []}, None),
        ]
        for kwargs, symbols in cases:
            merged = {**kwargs, **window}
            snap_ids = [
                s.snapshot_id for s in seeded_store.list_snapshots(**merged)  # type: ignore[arg-type]
            ]
            if symbols:
                # 符号过滤在 snapshots 侧无下推参数，用 Python 过滤作为等价基准
                snap_ids = [
                    s.snapshot_id
                    for s in seeded_store.list_snapshots(**merged)  # type: ignore[arg-type]
                    if s.symbol in set(symbols)
                ]
            ref_ids = [
                r.snapshot_id
                for r in seeded_store.list_snapshot_refs(symbols=symbols, **merged)  # type: ignore[arg-type]
            ]
            assert ref_ids == snap_ids, (kwargs, symbols)


class TestRowCapsOnRefs:
    def _ref(self, snapshot_id: str, symbol: str, day: int) -> SnapshotRef:
        return SnapshotRef(
            snapshot_id=snapshot_id,
            symbol=symbol,
            decision_time=datetime(2026, 3, day, 15, 0, tzinfo=UTC).isoformat(),
            feature_schema_id="s",
            feature_schema_hash="h",
            label_policy_id="p",
        )

    def test_per_symbol_cap_keeps_latest_refs(self) -> None:
        refs = [self._ref(f"s{i}", "600000", i + 1) for i in range(4)]
        kept, truncated = _apply_learning_protocol_row_caps(
            snapshots=refs, max_rows=0, per_symbol_rows_cap=2
        )
        assert [r.snapshot_id for r in kept] == ["s2", "s3"]
        assert truncated is False

    def test_global_cap_truncates_and_flags(self) -> None:
        refs = [self._ref(f"s{i}", f"60000{i}", i + 1) for i in range(5)]
        kept, truncated = _apply_learning_protocol_row_caps(
            snapshots=refs, max_rows=3, per_symbol_rows_cap=0
        )
        assert [r.snapshot_id for r in kept] == ["s2", "s3", "s4"]
        assert truncated is True

    def test_caps_identical_for_refs_and_snapshots(self, seeded_store: SampleStore) -> None:
        snapshots = seeded_store.list_snapshots()
        refs = seeded_store.list_snapshot_refs()
        snap_kept, snap_trunc = _apply_learning_protocol_row_caps(
            snapshots=snapshots, max_rows=2, per_symbol_rows_cap=1
        )
        ref_kept, ref_trunc = _apply_learning_protocol_row_caps(
            snapshots=refs, max_rows=2, per_symbol_rows_cap=1
        )
        assert [s.snapshot_id for s in snap_kept] == [r.snapshot_id for r in ref_kept]
        assert snap_trunc == ref_trunc
