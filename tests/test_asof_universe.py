"""S03 PIT 历史股票池：as_of=T 只能用 T 时刻可观测事实构造 universe。

对应蓝图 §5 P0-04 / 阶段施工提示词 S03 的 Done When：

```text
A: 2026-01 已上市    B: 2026-10 才上市
回测 2026-09 时：B 不得进入 universe，且不得进入 coverage 分母
```

另外钉住三条"禁止"（阶段提示词）：不用当前 ST 状态回填历史、不用当前退市名单
过滤历史、无法证明退市覆盖时如实标 ``incomplete_or_unknown``。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stock_analyzer.data.asof_universe import (
    COVERAGE_COMPLETE_PIT,
    COVERAGE_INCOMPLETE_OR_UNKNOWN,
    EXCLUDE_FUTURE_LISTED,
    EXCLUDE_INSUFFICIENT_HISTORY,
    SymbolPitStats,
    build_pit_stats,
    history_window_days,
    resolve_asof_universe,
)

AS_OF = date(2026, 9, 30)
MIN_HISTORY_DAYS = 60
LOOKBACK_DAYS = 5


def _probe(rows: list[tuple[str, pd.DatetimeIndex]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"symbol": symbol, "date": timestamp, "close": 10.0}
            for symbol, dates in rows
            for timestamp in dates
        ]
    )


def _stats(probe: pd.DataFrame) -> dict[str, SymbolPitStats]:
    return build_pit_stats(
        metrics=probe,
        as_of=AS_OF,
        lookback_days=LOOKBACK_DAYS,
        history_window_days=history_window_days(
            min_history_days=MIN_HISTORY_DAYS, lookback_days=LOOKBACK_DAYS
        ),
    )


def _resolve(probe: pd.DataFrame, *, index: list[str]):
    return resolve_asof_universe(
        as_of=AS_OF,
        index_symbols=index,
        stats=_stats(probe),
        min_history_days=MIN_HISTORY_DAYS,
        expected_active_lookback_days=LOOKBACK_DAYS,
    )


# ---------------------------------------------------------------------------
# Done When：未来上市票必须同时排除出 universe 与 coverage 分母
# ---------------------------------------------------------------------------


def test_future_listed_symbol_excluded_from_universe_and_denominator() -> None:
    probe = _probe(
        [
            ("A00001", pd.bdate_range("2026-01-05", "2026-09-30")),  # 2026-01 已上市
            ("B00002", pd.bdate_range("2026-10-05", "2026-12-31")),  # 2026-10 才上市
        ]
    )
    snapshot = _resolve(probe, index=["A00001", "B00002"])

    assert snapshot.eligible_symbols == ("A00001",)
    assert snapshot.expected_active_symbols == ("A00001",)
    assert "B00002" not in snapshot.eligible_symbols
    assert "B00002" not in snapshot.expected_active_symbols
    assert snapshot.excluded_reasons["B00002"] == EXCLUDE_FUTURE_LISTED
    assert snapshot.reason_counts[EXCLUDE_FUTURE_LISTED] == 1
    # 分母口径：即使 B 在 provider 索引里，也不进 eligible/expected_active
    assert snapshot.index_symbol_count == 2
    assert snapshot.expected_active_count == 1
    assert snapshot.coverage_ratio(valid_symbol_count=1) == 1.0


def test_probe_rows_after_as_of_are_ignored() -> None:
    """探针多给了未来行时不得把未来上市票算成历史有效（只统计 ≤ as_of）。"""
    probe = _probe(
        [
            ("A00001", pd.bdate_range("2026-01-05", "2026-09-30")),
            ("B00002", pd.bdate_range("2026-09-20", "2026-12-31")),  # 跨过 as_of
        ]
    )
    snapshot = _resolve(probe, index=["A00001", "B00002"])
    # B 在 as_of 之前只有 ~8 个交易日 → 窗口内历史不足（不得因未来行变"足"）
    assert "B00002" not in snapshot.eligible_symbols
    assert snapshot.excluded_reasons["B00002"] == EXCLUDE_INSUFFICIENT_HISTORY


# ---------------------------------------------------------------------------
# 停牌与覆盖率分母
# ---------------------------------------------------------------------------


def test_known_suspended_is_listed_separately_and_not_in_denominator() -> None:
    probe = _probe(
        [
            ("A00001", pd.bdate_range("2026-01-05", "2026-09-30")),
            # eligible（窗口内历史仍足够）但最近 5 个交易日没有任何 bar → 停牌
            ("S00003", pd.bdate_range("2026-01-05", "2026-09-15")),
        ]
    )
    snapshot = _resolve(probe, index=["A00001", "S00003"])
    assert snapshot.eligible_symbols == ("A00001", "S00003")
    assert snapshot.known_suspended_symbols == ("S00003",)
    assert snapshot.expected_active_symbols == ("A00001",)
    assert snapshot.reason_counts["known_suspended"] == 1
    assert snapshot.coverage_ratio(valid_symbol_count=1) == 1.0
    # 停牌票不得让覆盖率看起来更差（分母不含它）
    assert snapshot.expected_active_count == 1


def test_long_suspension_falls_into_insufficient_history_not_suspended() -> None:
    """长期停牌（窗口内 bar 也不够）归到 ``insufficient_history_window_bars``。

    这是第一版窗口口径的**已知限制**：一次批量探针无法区分"新上市"与"长期停牌"，
    因此只声称"窗口内历史不足"，不谎称原因（模块 docstring 有说明）。
    """
    probe = _probe(
        [
            ("A00001", pd.bdate_range("2026-01-05", "2026-09-30")),
            ("S00004", pd.bdate_range("2026-01-05", "2026-07-01")),  # 停牌超 2 个月
        ]
    )
    snapshot = _resolve(probe, index=["A00001", "S00004"])
    assert "S00004" not in snapshot.eligible_symbols
    assert snapshot.excluded_reasons["S00004"] == EXCLUDE_INSUFFICIENT_HISTORY
    assert snapshot.known_suspended_symbols == ()


def test_coverage_ratio_uses_expected_active_denominator() -> None:
    probe = _probe(
        [
            ("A00001", pd.bdate_range("2026-01-05", "2026-09-30")),
            ("A00002", pd.bdate_range("2026-01-05", "2026-09-30")),
            ("A00003", pd.bdate_range("2026-01-05", "2026-09-30")),
            ("A00004", pd.bdate_range("2026-01-05", "2026-09-30")),
        ]
    )
    snapshot = _resolve(probe, index=["A00001", "A00002", "A00003", "A00004"])
    assert snapshot.expected_active_count == 4
    assert snapshot.coverage_ratio(valid_symbol_count=3) == pytest.approx(0.75)
    # 分母为 0 时不得当满分
    empty = resolve_asof_universe(as_of=AS_OF, index_symbols=[], stats={})
    assert empty.coverage_ratio(valid_symbol_count=0) == 0.0


def test_symbol_absent_from_probe_is_treated_as_future_listed() -> None:
    probe = _probe([("A00001", pd.bdate_range("2026-01-05", "2026-09-30"))])
    snapshot = _resolve(probe, index=["A00001", "MISSING"])
    assert snapshot.excluded_reasons["MISSING"] == EXCLUDE_FUTURE_LISTED


# ---------------------------------------------------------------------------
# Survivorship 口径（禁止假装完整退市覆盖）
# ---------------------------------------------------------------------------


def test_survivorship_coverage_defaults_to_incomplete_or_unknown() -> None:
    probe = _probe([("A00001", pd.bdate_range("2026-01-05", "2026-09-30"))])
    snapshot = _resolve(probe, index=["A00001"])
    assert snapshot.survivorship_coverage == COVERAGE_INCOMPLETE_OR_UNKNOWN
    assert snapshot.delisting_coverage_verified is False


def test_survivorship_coverage_complete_only_when_explicitly_verified() -> None:
    probe = _probe([("A00001", pd.bdate_range("2026-01-05", "2026-09-30"))])
    snapshot = resolve_asof_universe(
        as_of=AS_OF,
        index_symbols=["A00001"],
        stats=_stats(probe),
        min_history_days=MIN_HISTORY_DAYS,
        delisting_coverage_verified=True,
    )
    assert snapshot.survivorship_coverage == COVERAGE_COMPLETE_PIT
    assert snapshot.delisting_coverage_verified is True


# ---------------------------------------------------------------------------
# 快照身份（可复现、可审计）
# ---------------------------------------------------------------------------


def test_snapshot_id_is_stable_and_as_of_sensitive() -> None:
    probe = _probe([("A00001", pd.bdate_range("2026-01-05", "2026-09-30"))])
    first = _resolve(probe, index=["A00001"])
    second = _resolve(probe, index=["A00001"])
    assert first.universe_snapshot_id == second.universe_snapshot_id
    other_day = resolve_asof_universe(
        as_of=date(2026, 9, 29),
        index_symbols=["A00001"],
        stats=_stats(probe),
        min_history_days=MIN_HISTORY_DAYS,
    )
    assert other_day.universe_snapshot_id != first.universe_snapshot_id


def test_payload_self_documents_basis_and_denominator() -> None:
    probe = _probe([("A00001", pd.bdate_range("2026-01-05", "2026-09-30"))])
    payload = _resolve(probe, index=["A00001"]).to_payload()
    assert payload["listed_age_basis"] == "history_window_bar_count"
    assert payload["coverage_denominator"] == "expected_active"
    assert payload["survivorship_coverage"] == COVERAGE_INCOMPLETE_OR_UNKNOWN
    assert payload["expected_active_lookback_days"] == LOOKBACK_DAYS
    assert payload["eligible_count"] == 1


# ---------------------------------------------------------------------------
# 明确的"禁止项"守卫
# ---------------------------------------------------------------------------


def test_resolver_has_no_current_state_inputs() -> None:
    """守卫：resolver 不得接受 ST 名单 / 退市名单 / 当前索引状态等"当前状态"输入。

    只允许 as_of + 可观测 bar 事实 + 阈值；否则"用当前状态回填历史"就会重新长回来。
    """
    import inspect

    signature = inspect.signature(resolve_asof_universe)
    assert set(signature.parameters) == {
        "as_of",
        "index_symbols",
        "stats",
        "min_history_days",
        "expected_active_lookback_days",
        "delisting_coverage_verified",
    }
    source = inspect.getsource(resolve_asof_universe)
    for forbidden in ("is_st", "delisted_symbols", "delist_date", "blacklist"):
        assert forbidden not in source


def test_build_pit_stats_counts_only_rows_upto_as_of() -> None:
    probe = _probe(
        [
            ("A00001", pd.bdate_range("2026-09-01", "2026-10-31")),
        ]
    )
    stats = _stats(probe)
    stat = stats["A00001"]
    # 9 月工作日约 22 天（≤ as_of），10 月的行必须被丢弃
    assert stat.last_bar_date is not None and stat.last_bar_date <= AS_OF
    assert stat.bars_in_window == sum(
        1 for timestamp in pd.bdate_range("2026-09-01", "2026-09-30")
    )
