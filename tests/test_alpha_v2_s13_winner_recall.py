"""S13 Winner Recall：赢家定义必须来自真实 outcome 的阶段验收测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from stock_analyzer.alpha_v2.research.winner_recall import (
    DEFAULT_WINNER_QUANTILE,
    RecallSpec,
    assert_outcome_metric,
    build_stage_membership,
    compute_winner_recall,
    recall_curve,
    winner_mask,
)


def _frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    if "executable" not in frame.columns:
        frame["executable"] = True
    return frame


def _day(day: str, count: int, *, stage_sizes: tuple[int, int, int] = (10, 5, 2)) -> list[dict]:
    quality, light, deep = stage_sizes
    rows: list[dict] = []
    for index in range(count):
        # 收益从高到低排列：第 0 名最好
        rows.append(
            {
                "decision_date": day,
                "symbol": f"{600000 + index}",
                "executable": True,
                "excess_return_5d": (count - index) / 100.0,
                "net_return_5d": (count - index) / 200.0,
                "rank": index + 1,
                "quality_pool": index < quality,
                "light_pool": index < light,
                "deep_pool": index < deep,
                "final_pool": index < 1,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# 赢家定义守卫
# ---------------------------------------------------------------------------


def test_metric_must_be_outcome_column() -> None:
    assert assert_outcome_metric("excess_return_5d") == "excess_return_5d"
    assert assert_outcome_metric("net_return_3d") == "net_return_3d"
    assert assert_outcome_metric("mae_5d") == "mae_5d"
    for bad in ("score", "predicted_alpha", "p_up_5d", "rank_score", "v2_rank_score"):
        with pytest.raises(ValueError, match="自证循环"):
            assert_outcome_metric(bad)


def test_winner_mask_rejects_prediction_column() -> None:
    frame = _frame(_day("2026-01-05", 30))
    frame["predicted_alpha"] = np.linspace(1, 0, len(frame))
    with pytest.raises(ValueError, match="自证循环"):
        winner_mask(frame, sort_column="predicted_alpha")


def test_compute_winner_recall_rejects_score_metric() -> None:
    frame = _frame(_day("2026-01-05", 30))
    with pytest.raises(ValueError, match="自证循环"):
        compute_winner_recall(frame, spec=RecallSpec(metric="v2_rank_score"))


# ---------------------------------------------------------------------------
# 召回计算
# ---------------------------------------------------------------------------


def test_winner_mask_picks_top_quantile_per_day() -> None:
    frame = _frame(_day("2026-01-05", 40) + _day("2026-01-06", 20))
    mask = winner_mask(frame, sort_column="excess_return_5d", quantile=0.10)
    assert int(frame[mask].groupby("decision_date").size().iloc[0]) == 4
    assert int(frame[mask].groupby("decision_date").size().iloc[1]) == 2
    # 赢家确实是当日收益最高的那批
    for _, group in frame.groupby("decision_date"):
        selected = group[group.index.isin(frame[mask].index)]
        assert selected["excess_return_5d"].min() >= group["excess_return_5d"].quantile(0.5)


def test_winner_mask_supports_top_n() -> None:
    frame = _frame(_day("2026-01-05", 40))
    mask = winner_mask(frame, sort_column="excess_return_5d", top_n=7)
    assert int(mask.sum()) == 7


def test_recall_counts_winners_surviving_each_stage() -> None:
    frame = _frame(_day("2026-01-05", 40, stage_sizes=(40, 10, 5)))
    report = compute_winner_recall(
        frame,
        spec=RecallSpec(winner_quantile=0.10, min_pool_size=10, scope_column="quality_pool"),
    )
    assert report.summary["status"] == "ok"
    assert report.summary["mature_dates"] == 1
    row = report.daily.iloc[0]
    # 赢家 = 收益前 10% = 4 只，全部在前 10 名内 → light 召回 100%
    assert int(row["winner_count"]) == 4
    assert float(row["recall_light"]) == pytest.approx(1.0)
    assert float(row["recall_deep"]) == pytest.approx(1.0)


def test_recall_drops_when_stage_cuts_winners() -> None:
    rows = []
    for index in range(40):
        rows.append(
            {
                "decision_date": "2026-01-05",
                "symbol": f"{600000 + index}",
                "executable": True,
                # 收益排名与漏斗名次**相反**：赢家在漏斗里排最后
                "excess_return_5d": index / 100.0,
                "rank": index + 1,
                "quality_pool": True,
            }
        )
    frame = build_stage_membership(
        _frame(rows), stage_sizes={"light": 10, "deep": 5, "final": 1}, rank_column="rank"
    )
    report = compute_winner_recall(frame, spec=RecallSpec(winner_quantile=0.10, min_pool_size=10))
    row = report.daily.iloc[0]
    assert float(row["recall_light"]) == pytest.approx(0.0)
    assert float(row["recall_deep"]) == pytest.approx(0.0)
    assert report.summary["winner_minus_pool_mean"] > 0


def test_recall_summary_reports_rolling_windows() -> None:
    rows: list[dict] = []
    for day_index in range(30):
        rows.extend(_day(f"2026-02-{day_index + 1:02d}", 30, stage_sizes=(30, 12, 6)))
    frame = _frame(rows)
    report = compute_winner_recall(frame, spec=RecallSpec(min_pool_size=10, rolling_windows=(20,)))
    assert report.summary["mature_dates"] == 30
    assert "recall_light_20d" in report.summary
    assert report.summary["research_gate"] == "failure_alert_only"


def test_days_below_min_pool_size_are_skipped() -> None:
    frame = _frame(
        _day("2026-01-05", 5, stage_sizes=(5, 5, 5))
        + _day("2026-01-06", 40, stage_sizes=(40, 12, 6))
    )
    report = compute_winner_recall(frame, spec=RecallSpec(min_pool_size=20))
    assert report.summary["mature_dates"] == 1
    assert report.daily["pool_size"].iloc[0] == 40


def test_missing_stage_column_is_not_available_not_full_recall() -> None:
    frame = _frame(_day("2026-01-05", 40, stage_sizes=(40, 40, 40)))
    # 去掉 deep 级成员列 → 该级必须 not_available，不能被当成"全都留下了"
    report = compute_winner_recall(
        frame,
        spec=RecallSpec(
            min_pool_size=10,
            stage_columns=(("light", "light_pool"), ("deep", "deep_pool_absent")),
        ),
    )
    row = report.daily.iloc[0]
    assert row["recall_light"] == pytest.approx(1.0)
    assert row["recall_deep"] == "not_available"
    assert (
        "recall_deep_20d"
        not in {key: value for key, value in report.summary.items() if value != "not_available"}
        or report.summary.get("recall_deep_mean") == "not_available"
    )


def test_non_executable_rows_excluded_from_pool() -> None:
    rows = _day("2026-01-05", 30, stage_sizes=(30, 12, 6))
    rows[0]["executable"] = False  # 收益最高的那只买不进 → 不得进池
    frame = _frame(rows)
    report = compute_winner_recall(frame, spec=RecallSpec(min_pool_size=10))
    assert int(report.daily.iloc[0]["pool_size"]) == 29
    assert str(rows[0]["symbol"]) not in set(frame[frame["executable"]]["symbol"].astype(str))


def test_empty_frame_reports_no_data() -> None:
    report = compute_winner_recall(pd.DataFrame(), spec=RecallSpec())
    assert report.summary["status"] == "no_data"
    assert report.daily.empty


def test_recall_curve_returns_per_stage_means() -> None:
    rows: list[dict] = []
    for day_index in range(5):
        rows.extend(_day(f"2026-03-{day_index + 1:02d}", 40, stage_sizes=(40, 12, 6)))
    curve = recall_curve(_frame(rows), spec=RecallSpec(min_pool_size=10))
    assert set(curve) == {"quality", "light", "deep", "final"}
    assert curve["quality"] == pytest.approx(1.0)
    assert curve["final"] <= curve["deep"] <= curve["light"]


def test_spec_payload_documents_definition() -> None:
    payload = RecallSpec().to_payload()
    assert payload["winner_definition"] == "top_by_future_real_executable_outcome"
    assert payload["grouped_by"] == "decision_date"
    assert payload["winner_quantile"] == DEFAULT_WINNER_QUANTILE
    assert payload["rolling_windows"] == [20, 60]
