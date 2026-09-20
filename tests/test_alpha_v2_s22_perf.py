"""S22 NAS 性能加固阶段验收测试。

守住：性能优化不得改变结果语义；超预算必须显式记录；单遍纪律可断言。
"""

from __future__ import annotations

import time

import pandas as pd
import pytest

from stock_analyzer.alpha_v2.research.multi_head import BuildStats
from stock_analyzer.alpha_v2.research.perf import (
    DEFAULT_MAX_PEAK_RSS_MIB,
    PERF_SCHEMA,
    RSS_UNAVAILABLE,
    STAGE_FEATURE,
    STAGE_FETCH,
    STAGE_MATRIX,
    STAGE_PREDICT,
    PerfBudget,
    StageTimer,
    assert_deterministic,
    build_perf_report,
    compare_baseline,
    determinism_evidence,
    guard_budget,
    prediction_digest,
    rss_mib,
    selection_order,
)


def _predictions() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "decision_date": ["2026-03-02", "2026-03-02", "2026-03-03"],
            "symbol": ["600001", "600002", "600003"],
            "alpha_rank_score": [0.9, 0.5, 0.7],
            "f0": [0.1, 0.2, 0.3],
        }
    )


# ---------------------------------------------------------------------------
# 计时与内存
# ---------------------------------------------------------------------------


def test_stage_timer_accumulates_and_reports_wall() -> None:
    timer = StageTimer()
    with timer.stage(STAGE_FETCH):
        time.sleep(0.01)
    with timer.stage(STAGE_FETCH):
        time.sleep(0.01)
    with timer.stage(STAGE_FEATURE):
        time.sleep(0.01)
    payload = timer.to_payload()
    assert payload["stages_seconds"][STAGE_FETCH] > payload["stages_seconds"][STAGE_FEATURE]
    assert payload["stage_total_seconds"] >= payload["stages_seconds"][STAGE_FETCH]
    assert payload["wall_seconds"] > 0


def test_rss_unavailable_is_reported_not_zero() -> None:
    value = rss_mib()
    assert value == RSS_UNAVAILABLE or value > 0
    timer = StageTimer(peak_rss_mib=RSS_UNAVAILABLE)
    payload = timer.to_payload()
    assert payload["peak_rss_mib"] is None
    assert payload["peak_rss_status"] == "unavailable"


def test_build_perf_report_includes_single_pass_counters() -> None:
    timer = StageTimer()
    with timer.stage(STAGE_MATRIX):
        pass
    stats = BuildStats(matrix_build_calls=1, prediction_calls=1, persist_calls=1)
    payload = build_perf_report(timer=timer, stats=stats)
    assert payload["schema"] == PERF_SCHEMA
    assert payload["single_pass"]["matrix_build_calls"] == 1
    assert payload["budget_check"]["status"] in {"ok", "exceeded"}
    stats.assert_single_pass()


# ---------------------------------------------------------------------------
# before / after 对照
# ---------------------------------------------------------------------------


def test_compare_baseline_reports_stage_deltas() -> None:
    before = {"stages_seconds": {"feature": 10.0, "predict": 4.0}, "wall_seconds": 20.0}
    after = {"stages_seconds": {"feature": 6.0, "predict": 4.5}, "wall_seconds": 14.0}
    delta = compare_baseline(before, after)
    assert delta["stages"]["feature"]["delta_seconds"] == pytest.approx(-4.0)
    assert delta["stages"]["predict"]["delta_seconds"] == pytest.approx(0.5)
    assert delta["wall_seconds"]["delta"] == pytest.approx(-6.0)


def test_compare_baseline_marks_missing_stages_not_available() -> None:
    before = {"stages_seconds": {"feature": 10.0}, "wall_seconds": 20.0}
    after = {"stages_seconds": {"predict": 4.0}, "wall_seconds": 12.0}
    delta = compare_baseline(before, after)
    assert delta["stages"]["feature"]["delta_seconds"] == "not_available"
    assert delta["stages"]["predict"]["delta_seconds"] == "not_available"


def test_compare_baseline_without_rss_is_not_available() -> None:
    delta = compare_baseline({"wall_seconds": 1.0}, {"wall_seconds": 2.0})
    assert delta["peak_rss_mib"]["delta"] == "not_available"


def test_build_perf_report_without_baseline_omits_delta() -> None:
    payload = build_perf_report(timer=StageTimer())
    assert "baseline" not in payload
    assert "delta" not in payload


# ---------------------------------------------------------------------------
# 预算
# ---------------------------------------------------------------------------


def test_guard_budget_flags_exceeded_without_silent_pass() -> None:
    current = {"wall_seconds": 5000.0, "peak_rss_mib": 4000.0}
    result = guard_budget({"current": current}, budget=PerfBudget(max_peak_rss_mib=3072))
    assert result["status"] == "exceeded"
    assert set(result["exceeded"]) == {"peak_rss_mib", "wall_seconds"}


def test_guard_budget_ok_within_limits() -> None:
    current = {"wall_seconds": 100.0, "peak_rss_mib": 512.0}
    result = guard_budget({"current": current})
    assert result["status"] == "ok"
    assert result["exceeded"] == []


def test_default_budget_leaves_headroom_inside_heavy_container() -> None:
    # heavy 容器 4GiB；默认硬预算必须在其内，否则"过预算"永远不会被触发
    assert 0 < DEFAULT_MAX_PEAK_RSS_MIB < 4096


# ---------------------------------------------------------------------------
# determinism 不变量
# ---------------------------------------------------------------------------


def test_prediction_digest_is_order_insensitive() -> None:
    frame = _predictions()
    shuffled = frame.sample(frac=1.0, random_state=7).reset_index(drop=True)
    assert prediction_digest(frame, columns=["alpha_rank_score"]) == prediction_digest(
        shuffled, columns=["alpha_rank_score"]
    )


def test_prediction_digest_changes_with_values() -> None:
    frame = _predictions()
    changed = frame.copy()
    changed.loc[0, "alpha_rank_score"] = 0.11
    assert prediction_digest(frame, columns=["alpha_rank_score"]) != prediction_digest(
        changed, columns=["alpha_rank_score"]
    )


def test_selection_order_is_deterministic_and_score_sorted() -> None:
    order = selection_order(_predictions(), score_column="alpha_rank_score", top_k=1)
    assert order == ["600001", "600003"]  # 每天一只，按日期顺序
    assert selection_order(_predictions(), score_column="alpha_rank_score", top_k=1) == order


def test_assert_deterministic_passes_for_identical_evidence() -> None:
    matrix = type("M", (), {"fingerprint": "abc123"})()
    predictions = _predictions()
    first = determinism_evidence(matrix=matrix, predictions=predictions, feature_columns=["f0"])
    second = determinism_evidence(
        matrix=matrix, predictions=predictions.copy(), feature_columns=["f0"]
    )
    assert_deterministic(first, second)


def test_assert_deterministic_rejects_changed_selection() -> None:
    left = {"prediction_digest": "aaa", "matrix_fingerprint": "fff", "selection_order": ["600001"]}
    right = {"prediction_digest": "aaa", "matrix_fingerprint": "fff", "selection_order": ["600002"]}
    with pytest.raises(AssertionError, match="determinism 被破坏"):
        assert_deterministic(left, right)


def test_assert_deterministic_rejects_changed_matrix_fingerprint() -> None:
    left = {"matrix_fingerprint": "aaa"}
    right = {"matrix_fingerprint": "bbb"}
    with pytest.raises(AssertionError):
        assert_deterministic(left, right)


def test_assert_deterministic_ignores_missing_keys() -> None:
    assert_deterministic({"prediction_digest": "aaa"}, {"matrix_fingerprint": "bbb"})


def test_perf_payload_round_trips_as_json() -> None:
    import json

    timer = StageTimer()
    with timer.stage(STAGE_PREDICT):
        pass
    payload = build_perf_report(
        timer=timer,
        stats=BuildStats(matrix_build_calls=1, prediction_calls=1),
        baseline={"stages_seconds": {STAGE_PREDICT: 1.0}, "wall_seconds": 2.0},
    )
    restored = json.loads(json.dumps(payload, default=str))
    assert restored["schema"] == PERF_SCHEMA
    assert restored["single_pass"]["prediction_calls"] == 1
