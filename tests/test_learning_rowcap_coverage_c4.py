"""C4（本地半）：行数 cap 的覆盖口径可见化（F22）。

方案 §C4 第 5 条：「扩 lookback 必须同时处理行数 cap（F22），否则历史不会变长
而只是窗口平移」。截断策略是从尾部保留，触发后历史起点被推后——本测试锁定
"这必须看得出来"，而不是静默地以为历史加长了。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from stock_analyzer.learning.sample_store import SnapshotRef
from stock_analyzer.runtime.service import _history_coverage


def _ref(day: str) -> SnapshotRef:
    return SnapshotRef(
        snapshot_id=f"snap-{day}",
        symbol="600000",
        decision_time=f"{day}T15:00:00+00:00",
        feature_schema_id="s",
        feature_schema_hash="h",
        label_policy_id="p",
    )


class TestHistoryCoverage:
    def test_covered_when_history_reaches_requested_start(self) -> None:
        rows = [_ref("2026-01-05"), _ref("2026-03-02"), _ref("2026-06-30")]
        cov = _history_coverage(
            rows,
            requested_start=datetime(2026, 1, 1, tzinfo=UTC),
            truncated_by_row_cap=False,
        )
        assert cov["effective_history_start"] == "2026-01-05"
        assert cov["requested_window_start"] == "2026-01-01"
        # 历史起点晚于请求起点 = 请求窗的开头没覆盖到（"覆盖"= 历史回溯到至少请求起点）
        assert cov["requested_window_covered"] is False
        assert cov["history_truncated_by_row_cap"] is False

    def test_covered_when_history_starts_at_or_before_requested(self) -> None:
        rows = [_ref("2025-12-20"), _ref("2026-03-02")]
        cov = _history_coverage(
            rows,
            requested_start=datetime(2026, 1, 1, tzinfo=UTC),
            truncated_by_row_cap=False,
        )
        assert cov["effective_history_start"] == "2025-12-20"
        assert cov["requested_window_covered"] is True

    def test_row_cap_truncation_shows_window_shift(self) -> None:
        # cap 从尾部保留 → 历史起点被推到请求窗之后：扩 lookback 只是平移
        rows = [_ref("2026-05-02"), _ref("2026-06-30")]
        cov = _history_coverage(
            rows,
            requested_start=date(2026, 1, 1),
            truncated_by_row_cap=True,
        )
        assert cov["effective_history_start"] == "2026-05-02"
        assert cov["requested_window_covered"] is False
        assert cov["history_truncated_by_row_cap"] is True

    def test_timezone_and_format_differences_do_not_flip_conclusion(self) -> None:
        # 请求侧 naive local、落库侧带偏移：只比日粒度，同一自然日不得判未覆盖
        rows = [_ref("2026-03-02")]
        cov = _history_coverage(
            rows,
            requested_start=datetime(2026, 3, 2, 22, 30),
            truncated_by_row_cap=False,
        )
        assert cov["effective_history_start"] == "2026-03-02"
        assert cov["requested_window_covered"] is True

    def test_empty_rows_are_unknown_not_covered(self) -> None:
        cov = _history_coverage([], requested_start=date(2026, 1, 1), truncated_by_row_cap=True)
        assert cov["effective_history_start"] == ""
        assert cov["requested_window_covered"] is None  # 未知，不谎报"已覆盖"

    def test_missing_requested_start_is_unknown(self) -> None:
        cov = _history_coverage(
            [_ref("2026-03-02")], requested_start=None, truncated_by_row_cap=False
        )
        assert cov["requested_window_covered"] is None
        assert cov["effective_history_start"] == "2026-03-02"
