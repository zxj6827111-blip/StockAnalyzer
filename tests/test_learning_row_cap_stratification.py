"""按日分层行数 cap：在总样本数不变的前提下把保留的决策日数放大。

背景（2026-09-14 NAS 实测）：``SA__TRAINING__BOOTSTRAP_DATASET_MAX_ROWS=40000``
配合每日约 334 只候选截面，等于只保留约 120 个决策日（manifest 的
``decision_days_total=120`` 可对上）。A1 的决策日粒度 purge 剔掉 26 个决策日后，
test 段只剩 12 个决策日 / 16 个自然日，低于 ``min_test_split_window_days=20`` →
``test_window_too_narrow`` → 学习协议无法产出可训练 manifest。
（A1 本身没错：它清掉的是 8/6~8/12 每天仅 1~2 行、且与 calibration 段同日重叠的
碎片，那 5 天正是把窗口虚撑到 23 天的东西。）

修法：每根决策日只留 ``per_day_rows_cap`` 条，全局上限退化为**整日滑窗**。
总样本数不变（内存前提不破），保留的决策日数从 ``max_rows/每日截面``
升到 ``max_rows/per_day_rows_cap``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest

from stock_analyzer.learning.dataset_manifest import decision_date_shanghai
from stock_analyzer.runtime.service import (
    _apply_learning_protocol_row_caps,
    _decision_day_of,
    _resolve_per_day_rows_cap,
    _resolve_row_cap_strategy,
)


@dataclass(frozen=True)
class _Row:
    snapshot_id: str
    symbol: str
    decision_time: datetime

    @property
    def feature_schema_id(self) -> str:
        return "schema"

    @property
    def feature_schema_hash(self) -> str:
        return "hash"


def _decision_time(day_offset: int, hour: int = 6) -> datetime:
    """构造 UTC 决策时刻：+8h 后落到上海当日，故 hour=6 表示当地 14:00。"""
    return datetime(2026, 3, 2, hour, tzinfo=UTC) + timedelta(days=day_offset)


def _rows(*, days: int, per_day: int) -> list[_Row]:
    return [
        _Row(
            snapshot_id=f"snap-{day:03d}-{idx:04d}",
            symbol=f"{300000 + idx:06d}",
            decision_time=_decision_time(day),
        )
        for day in range(days)
        for idx in range(per_day)
    ]


def test_per_day_cap_keeps_all_days_at_constant_sample_count() -> None:
    """核心诉求：每日 334 → 120 条后，总样本数降一半以内，但决策日数翻 2.7 倍。"""
    rows = _rows(days=330, per_day=334)
    kept, truncated = _apply_learning_protocol_row_caps(
        snapshots=rows,
        max_rows=40_000,
        per_symbol_rows_cap=0,
        per_day_rows_cap=120,
    )
    days = {decision_date_shanghai(row.decision_time) for row in kept}
    assert truncated is True
    assert len(kept) == 330 * 120  # 39,600 ≤ 40,000，全局门未再回退
    assert len(days) == 330  # 旧口径下只有 40_000 // 334 ≈ 119 天


def test_legacy_semantics_unchanged_when_cap_disabled() -> None:
    """``per_day_rows_cap=0`` 必须完全保持历史 keep-last-N 逐行截断。"""
    rows = _rows(days=10, per_day=50)
    kept, truncated = _apply_learning_protocol_row_caps(
        snapshots=rows,
        max_rows=120,
        per_symbol_rows_cap=0,
        per_day_rows_cap=0,
    )
    assert truncated is True
    assert len(kept) == 120
    assert [row.snapshot_id for row in kept] == [row.snapshot_id for row in rows[-120:]]


def test_global_cap_slides_by_whole_days_not_mid_day() -> None:
    """全局上限回退时整日丢弃，不能把最新决策日拦腰截断。

    最新一日的半截截面会让该日的横截面标签（return_rank 分位）与训练样本口径不一致。
    """
    rows = _rows(days=10, per_day=30)  # 300 行
    kept, truncated = _apply_learning_protocol_row_caps(
        snapshots=rows,
        max_rows=100,
        per_symbol_rows_cap=0,
        per_day_rows_cap=30,
    )
    assert truncated is True
    per_day: dict[object, int] = {}
    for row in kept:
        key = decision_date_shanghai(row.decision_time)
        per_day[key] = per_day.get(key, 0) + 1
    assert len(kept) == 90  # 3 个整日
    assert set(per_day.values()) == {30}
    # 保留的是**最新**的 3 天
    assert len(per_day) == 3
    newest = max(per_day)
    assert newest == decision_date_shanghai(_decision_time(9))


def test_per_day_cap_is_deterministic_and_ordered() -> None:
    """同一输入重复调用结果一致，且输出仍按 (decision_time, snapshot_id) 升序。"""
    rows = _rows(days=4, per_day=20)
    first, _ = _apply_learning_protocol_row_caps(
        snapshots=rows, max_rows=0, per_symbol_rows_cap=0, per_day_rows_cap=5
    )
    second, _ = _apply_learning_protocol_row_caps(
        snapshots=list(reversed(rows)), max_rows=0, per_symbol_rows_cap=0, per_day_rows_cap=5
    )
    assert [r.snapshot_id for r in first] == [r.snapshot_id for r in second]
    assert [r.snapshot_id for r in first] == sorted(r.snapshot_id for r in first)


def test_per_symbol_cap_still_applied_before_day_stratification() -> None:
    """单票上限仍先生效：一票在多个决策日出现时只留最近 N 条，再按日分层。"""
    rows = [
        _Row(
            snapshot_id=f"snap-{day:02d}",
            symbol="300001",
            decision_time=_decision_time(day),
        )
        for day in range(10)
    ]
    kept, _ = _apply_learning_protocol_row_caps(
        snapshots=rows,
        max_rows=0,
        per_symbol_rows_cap=3,
        per_day_rows_cap=10,
    )
    assert [row.snapshot_id for row in kept] == ["snap-07", "snap-08", "snap-09"]


def test_single_oversized_day_falls_back_to_row_slice() -> None:
    """兜底：连最新一个整日都放不下时退回按行截断，保证有样本可用而非空集。"""
    rows = _rows(days=2, per_day=50)
    kept, truncated = _apply_learning_protocol_row_caps(
        snapshots=rows,
        max_rows=10,
        per_symbol_rows_cap=0,
        per_day_rows_cap=50,
    )
    assert truncated is True
    assert len(kept) == 10
    assert {decision_date_shanghai(row.decision_time) for row in kept} == {
        decision_date_shanghai(_decision_time(1))
    }


def test_decision_day_uses_shanghai_boundary() -> None:
    """日界按上海时区切：UTC 17:00 与 UTC 次日 06:00 属同一个上海决策日。"""
    rows = [
        _Row("a", "300001", datetime(2026, 3, 2, 17, tzinfo=UTC)),
        _Row("b", "300002", datetime(2026, 3, 3, 6, tzinfo=UTC)),
    ]
    assert decision_date_shanghai(rows[0].decision_time) == decision_date_shanghai(
        rows[1].decision_time
    )
    kept, _ = _apply_learning_protocol_row_caps(
        snapshots=rows, max_rows=0, per_symbol_rows_cap=0, per_day_rows_cap=1
    )
    assert len(kept) == 1


def test_auto_spread_uses_whole_budget_across_all_days() -> None:
    """自动档把整个 max_rows 预算平摊到全部决策日——决策日数拉满且不超预算。

    这是取代「人工试出一个每日条数」的做法：480 个可用决策日、预算 40,000
    → 每日 83 条 → 保留全部 480 天（39,840 ≤ 40,000）。若按旧 keep-last-N，
    只剩 40,000/334 ≈ 119 天。
    """
    rows = _rows(days=480, per_day=334)
    available_days = len({decision_date_shanghai(r.decision_time) for r in rows})
    auto = _resolve_per_day_rows_cap(
        strategy="per_day", configured=0, max_rows=40_000, available_days=available_days
    )
    assert auto == 83
    kept, _ = _apply_learning_protocol_row_caps(
        snapshots=rows,
        max_rows=40_000,
        per_symbol_rows_cap=0,
        per_day_rows_cap=auto,
    )
    kept_days = {decision_date_shanghai(row.decision_time) for row in kept}
    assert len(kept_days) == available_days  # 一天都没丢
    assert len(kept) <= 40_000  # 预算没被突破


def test_strategy_resolution_fails_closed_on_typo() -> None:
    """策略名写错必须报错：静默退回 keep_last 会让窗口问题看起来已修。"""
    assert _resolve_row_cap_strategy(None) == "keep_last"
    assert _resolve_row_cap_strategy("  PER_DAY ") == "per_day"
    with pytest.raises(ValueError, match="bootstrap_row_cap_strategy"):
        _resolve_row_cap_strategy("per_dayy")


def test_per_day_cap_disabled_paths() -> None:
    """策略为 keep_last / 无可算日数 / 无总预算时都不启用按日分层。"""
    assert (
        _resolve_per_day_rows_cap(
            strategy="keep_last", configured=120, max_rows=40_000, available_days=480
        )
        == 0
    )
    assert (
        _resolve_per_day_rows_cap(
            strategy="per_day", configured=0, max_rows=40_000, available_days=0
        )
        == 0
    )
    assert (
        _resolve_per_day_rows_cap(strategy="per_day", configured=0, max_rows=0, available_days=480)
        == 0
    )
    # 显式给出每日上限时优先采用配置值
    assert (
        _resolve_per_day_rows_cap(
            strategy="per_day", configured=120, max_rows=40_000, available_days=480
        )
        == 120
    )


def test_decision_time_accepts_ref_string_form() -> None:
    """``SnapshotRef.decision_time`` 是数据库 ISO 字符串，日界必须能直接吃它。

    这是真实生产形态：协议路径裁的是 ref、不是 SignalSnapshot。缺这层规范化会
    在 NAS 上直接 TypeError（字符串 + timedelta），而本地按 datetime 造样本时
    看不出来——所以这里显式用字符串形态再验一遍。
    """
    rows = [
        _Row("snap-a", "300001", "2026-03-02T17:00:00+00:00"),  # 沪 3/3 01:00
        _Row("snap-b", "300002", "2026-03-03T06:00:00+00:00"),  # 沪 3/3 14:00
        _Row("snap-c", "300003", "2026-03-03T17:00:00+00:00"),  # 沪 3/4 01:00
    ]
    assert _decision_day_of("2026-03-02T17:00:00+00:00") == date(2026, 3, 3)
    kept, _ = _apply_learning_protocol_row_caps(
        snapshots=rows, max_rows=0, per_symbol_rows_cap=0, per_day_rows_cap=1
    )
    # a 与 b 同属沪 3/3（留 snapshot_id 较小者 a），c 单独一天 → 共 2 条
    assert [row.snapshot_id for row in kept] == ["snap-a", "snap-c"]


def test_public_day_helper_matches_legacy_private_name() -> None:
    """公共日helper与历史私有名必须是同一实现，避免 cap 与 purge/split 日界错位。"""
    from stock_analyzer.learning.dataset_manifest import _decision_date_shanghai

    probe = datetime(2026, 3, 2, 17, 30, tzinfo=UTC)
    assert decision_date_shanghai(probe) == _decision_date_shanghai(probe)
