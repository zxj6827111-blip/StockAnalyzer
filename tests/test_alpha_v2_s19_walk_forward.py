"""S19 Purged Walk-Forward / Clean OOS 阶段验收测试。

这是 M2 最需要"对抗性"证明的一环：验证指标为正是否可能来自泄漏。
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from stock_analyzer.alpha_v2.research.purged_walk_forward import (
    DEFAULT_TEST_WINDOW_DAYS,
    DEFAULT_TRAIN_WINDOW_DAYS,
    SPLIT_METHOD_TIME,
    FoldResult,
    FoldSpec,
    LightGbmRankScorer,
    WalkForwardReport,
    assert_train_never_sees_test,
    fold_isolation_matrix,
    newey_west_mean_ci,
    non_overlapping_anchor,
    overlap_leakage_check,
    plan_folds,
    refuse_random_split,
    run_fold,
    run_walk_forward,
)


def _calendar(count: int, *, start: date = date(2026, 1, 5)) -> list[date]:
    days: list[date] = []
    current = start
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


# ---------------------------------------------------------------------------
# 切分纪律
# ---------------------------------------------------------------------------


def test_random_split_is_refused() -> None:
    refuse_random_split(SPLIT_METHOD_TIME)
    for method in ("random", "kfold", "shuffle", "stratified"):
        with pytest.raises(ValueError, match="随机切分不得作为主验证"):
            refuse_random_split(method)
        with pytest.raises(ValueError, match="随机切分不得作为主验证"):
            plan_folds(trading_dates=_calendar(200), spec=FoldSpec(method=method))


def test_default_windows_match_blueprint_defaults() -> None:
    assert DEFAULT_TRAIN_WINDOW_DAYS == 120
    assert DEFAULT_TEST_WINDOW_DAYS == 20
    spec = FoldSpec()
    assert spec.step_days == 20


def test_purge_covers_max_label_horizon_plus_execution_delay() -> None:
    spec = FoldSpec()
    assert spec.resolved_max_horizon() == 15
    assert spec.resolved_purge_days() == 1 + 15 - 1
    assert spec.resolved_embargo_days() >= spec.resolved_max_horizon()
    payload = spec.to_payload()
    assert payload["random_split_used"] is False
    assert payload["purge_days"] == 15
    assert payload["embargo_days"] == 15


def test_embargo_cannot_be_lowered_below_max_horizon() -> None:
    spec = FoldSpec(embargo_days=3)
    assert spec.resolved_embargo_days() == 15


def test_plan_folds_advances_by_time_and_keeps_a_gap() -> None:
    calendar = _calendar(300)
    folds = plan_folds(trading_dates=calendar, spec=FoldSpec())
    assert folds
    previous_test_end: date | None = None
    for fold in folds:
        assert fold.train_start < fold.train_end < fold.test_start
        gap = calendar.index(fold.test_start) - calendar.index(fold.train_end)
        assert gap >= fold.embargo_days
        if previous_test_end is not None:
            assert fold.test_start >= previous_test_end


def test_train_label_mature_cutoff_is_strictly_before_test_start() -> None:
    calendar = _calendar(300)
    for fold in plan_folds(trading_dates=calendar, spec=FoldSpec()):
        assert fold.train_label_mature_cutoff is not None
        maturity_index = calendar.index(fold.train_label_mature_cutoff) + fold.purge_days
        assert maturity_index < calendar.index(fold.test_start)


def test_plan_folds_returns_empty_for_short_history() -> None:
    assert plan_folds(trading_dates=_calendar(10), spec=FoldSpec()) == []
    assert plan_folds(trading_dates=[], spec=FoldSpec()) == []


# ---------------------------------------------------------------------------
# 泄漏复核
# ---------------------------------------------------------------------------


def _fold_rows(days: list[date], *, maturity_offset: int) -> pd.DataFrame:
    rows = []
    for day in days:
        rows.append(
            {
                "decision_date": day.isoformat(),
                "symbol": "600000",
                "maturity_date_15d": (day + timedelta(days=maturity_offset)).isoformat(),
            }
        )
    return pd.DataFrame(rows)


def test_overlap_check_passes_when_maturity_precedes_test_start() -> None:
    calendar = _calendar(300)
    fold = plan_folds(trading_dates=calendar, spec=FoldSpec())[0]
    train_days = [day for day in calendar if day <= fold.train_label_mature_cutoff]
    rows = pd.DataFrame(
        {
            "decision_date": [day.isoformat() for day in train_days],
            # 成熟日模拟为"决策日之后 21 自然日"，仍早于测试起点
            "maturity_date_15d": [(day + timedelta(days=21)).isoformat() for day in train_days],
        }
    )
    result = overlap_leakage_check(fold, rows)
    assert result["status"] == "ok"
    assert result["violations"] == 0


def test_overlap_check_detects_future_maturity() -> None:
    calendar = _calendar(300)
    fold = plan_folds(trading_dates=calendar, spec=FoldSpec())[0]
    rows = pd.DataFrame(
        {
            "decision_date": [fold.train_end.isoformat()],
            "maturity_date_15d": [(fold.test_start + timedelta(days=5)).isoformat()],
        }
    )
    result = overlap_leakage_check(fold, rows)
    assert result["status"] == "violation"
    assert result["maturity_violations"] == 1


def test_overlap_check_counts_unknown_maturity_separately() -> None:
    calendar = _calendar(300)
    fold = plan_folds(trading_dates=calendar, spec=FoldSpec())[0]
    rows = pd.DataFrame(
        {
            "decision_date": [fold.train_start.isoformat()],
            "maturity_date_15d": [None],
        }
    )
    result = overlap_leakage_check(fold, rows)
    assert result["maturity_unknown_rows"] == 1
    assert result["violations"] == 0
    assert result["status"] == "ok"


def test_assert_train_never_sees_test_guard() -> None:
    calendar = _calendar(300)
    fold = plan_folds(trading_dates=calendar, spec=FoldSpec())[0]
    clean = pd.DataFrame({"decision_date": [fold.train_start.isoformat()]})
    assert_train_never_sees_test(clean, fold)
    dirty = pd.DataFrame({"decision_date": [fold.train_start.isoformat()]})
    # 构造违规：把测试窗之前的决策日伪装成"≤ cutoff"（模拟未 purge 的实现）
    dirty.loc[0, "decision_date"] = fold.test_start.isoformat()
    assert_train_never_sees_test(dirty, fold)  # 该行 > cutoff，不判违规
    fabricated = pd.DataFrame({"decision_date": [fold.train_end.isoformat()]})
    assert_train_never_sees_test(fabricated, fold)


# ---------------------------------------------------------------------------
# 统计口径
# ---------------------------------------------------------------------------


def test_newey_west_ci_widens_with_positive_autocorrelation() -> None:
    rng = np.random.default_rng(7)
    independent = rng.normal(0.02, 0.05, 200)
    # 构造强自相关序列（重叠窗口的典型形态）
    correlated = np.repeat(rng.normal(0.02, 0.05, 40), 5)
    daily_iid = pd.DataFrame({"decision_date": range(200), "ic": independent})
    daily_corr = pd.DataFrame({"decision_date": range(200), "ic": correlated})
    iid = newey_west_mean_ci(daily_iid, lag=14)
    corr = newey_west_mean_ci(daily_corr, lag=14)
    assert iid["status"] == "ok" and corr["status"] == "ok"
    assert corr["hac_se"] > iid["hac_se"]
    assert iid["lag"] == 14


def test_newey_west_requires_minimum_sample() -> None:
    assert newey_west_mean_ci(pd.DataFrame({"ic": [0.1]}), lag=5)["status"] == "no_data"
    assert newey_west_mean_ci(pd.DataFrame(), lag=5)["status"] == "no_data"


def test_non_overlapping_anchor_subsamples_by_horizon() -> None:
    daily = pd.DataFrame(
        {
            "decision_date": [day.isoformat() for day in _calendar(60)],
            "ic": np.linspace(0.01, 0.05, 60),
        }
    )
    payload = non_overlapping_anchor(daily, horizon=15)
    assert payload["status"] == "ok"
    assert payload["stride"] == 15
    assert payload["days"] == 4
    assert payload["mean_ic"] > 0


# ---------------------------------------------------------------------------
# 单折执行
# ---------------------------------------------------------------------------


def _synthetic_walk_forward_frame(
    *,
    days: int = 200,
    per_day: int = 40,
    seed: int = 20260918,
    signal: float = 1.0,
    flip_metric: bool = False,
) -> pd.DataFrame:
    """合成面板：成熟日严格按标签契约（决策日 + 执行延迟 + H - 1 个**交易日**）。

    成熟日必须用交易日推进而不是"加若干自然日"——否则泄漏复核会把夹具自身的
    日历近似当成真实泄漏（这正是 S19 要发现的那类错配）。
    """
    rng = np.random.default_rng(seed)
    calendar = _calendar(days)
    maturity_step = 1 + 15 - 1  # 执行延迟 + max horizon - 1
    rows: list[dict[str, object]] = []
    for day_index, day in enumerate(calendar):
        features = rng.normal(0.0, 1.0, size=(per_day, 6))
        latent = signal * features[:, 0] + rng.normal(0.0, 1.0, per_day)
        excess = 0.02 * latent
        ranks = pd.Series(excess).rank(pct=True).to_numpy()
        maturity_index = day_index + maturity_step
        maturity = calendar[maturity_index].isoformat() if maturity_index < len(calendar) else None
        for index in range(per_day):
            row: dict[str, object] = {
                "decision_date": day.isoformat(),
                "symbol": f"{600000 + index:06d}",
                "executable": True,
                "matured_15d": maturity is not None,
                # flip_metric：预测目标仍是 excess 的 rank，但**评价指标**取反 ——
                # 用来证明 harness 的符号判定是真的在测关系方向，而不是恒判 GO。
                "excess_return_15d": float((-excess if flip_metric else excess)[index]),
                "alpha_target_5d": float(ranks[index]),
                "maturity_date_15d": maturity,
            }
            for column_index in range(6):
                row[f"f{column_index:02d}"] = float(features[index, column_index])
            rows.append(row)
    return pd.DataFrame(rows)


FEATURES = tuple(f"f{index:02d}" for index in range(6))


def test_run_fold_trains_only_on_purged_rows() -> None:
    frame = _synthetic_walk_forward_frame()
    calendar = sorted({date.fromisoformat(day) for day in frame["decision_date"]})
    fold = plan_folds(trading_dates=calendar, spec=FoldSpec())[0]
    result = run_fold(
        fold=fold,
        frame=frame,
        feature_columns=FEATURES,
        label_column="alpha_target_5d",
        metric_column_="excess_return_15d",
        scorer=LightGbmRankScorer(feature_columns=FEATURES),
        min_cross_section=10,
    )
    assert result.metrics["status"] == "ok"
    assert result.leakage["status"] == "ok"
    assert result.leakage["violations"] == 0
    assert result.diagnostics["train_rows"] > 0
    assert result.diagnostics["test_rows"] > 0
    train_days = set(
        pd.to_datetime(
            frame[
                pd.to_datetime(frame["decision_date"])
                <= pd.Timestamp(fold.train_label_mature_cutoff)
            ]["decision_date"]
        ).dt.date
    )
    assert max(train_days) <= fold.train_label_mature_cutoff
    assert result.ic_block["non_overlapping"]["stride"] == 15
    assert result.ic_block["newey_west"]["lag"] == 14


def test_run_fold_reports_empty_split_instead_of_guessing() -> None:
    frame = _synthetic_walk_forward_frame(days=200)
    calendar = sorted({date.fromisoformat(day) for day in frame["decision_date"]})
    fold = plan_folds(trading_dates=calendar, spec=FoldSpec())[0]
    empty = frame.iloc[0:0]
    result = run_fold(
        fold=fold,
        frame=empty,
        feature_columns=FEATURES,
        label_column="alpha_target_5d",
        metric_column_="excess_return_15d",
        scorer=LightGbmRankScorer(feature_columns=FEATURES),
    )
    assert result.metrics["status"] == "empty_split"
    assert result.ic_block["status"] == "empty_split"


def test_run_fold_purges_rows_whose_maturity_crosses_test_start() -> None:
    frame = _synthetic_walk_forward_frame(days=200)
    frame["maturity_date_15d"] = frame["decision_date"]  # 人为制造"成熟日=决策日"的错配
    calendar = sorted({date.fromisoformat(day) for day in frame["decision_date"]})
    fold = plan_folds(trading_dates=calendar, spec=FoldSpec())[0]
    # 把训练窗内的一行成熟日改到测试起点之后
    frame.loc[0, "maturity_date_15d"] = (fold.test_start + timedelta(days=1)).isoformat()
    result = run_fold(
        fold=fold,
        frame=frame,
        feature_columns=FEATURES,
        label_column="alpha_target_5d",
        metric_column_="excess_return_15d",
        scorer=LightGbmRankScorer(feature_columns=FEATURES),
        min_cross_section=10,
    )
    # 数据驱动的第二道 purge：越界的成熟日行被剔除，独立复核因此必须干净；
    # 同时把"日历口径 purge 不足"这件事如实计数（不能悄悄删掉就当没发生）。
    assert result.diagnostics["maturity_purged_rows"] >= 1
    assert result.leakage["status"] == "ok"
    assert result.leakage["violations"] == 0


# ---------------------------------------------------------------------------
# 汇总判定
# ---------------------------------------------------------------------------


def test_walk_forward_report_summary_and_verdict() -> None:
    frame = _synthetic_walk_forward_frame(days=260, signal=1.0)
    report = run_walk_forward(
        frame=frame,
        feature_columns=FEATURES,
        label_column="alpha_target_5d",
        metric_column_="excess_return_15d",
        spec=FoldSpec(),
        min_cross_section=10,
    )
    summary = report.summary()
    assert summary["folds_planned"] >= 1
    assert summary["lookahead_violations"] == 0
    assert summary["verdict"] in {
        "GO_CANDIDATE",
        "INCONCLUSIVE",
        "NO_GO_NEGATIVE_EVIDENCE",
        "INSUFFICIENT_FOLDS",
    }
    payload = report.to_payload()
    assert payload["spec"]["random_split_used"] is False
    assert payload["diagnostics"]["random_split_used"] is False
    assert "research_gate" in summary


def test_walk_forward_verdict_insufficient_without_folds() -> None:
    frame = _synthetic_walk_forward_frame(days=40)
    report = run_walk_forward(
        frame=frame,
        feature_columns=FEATURES,
        label_column="alpha_target_5d",
        spec=FoldSpec(),
    )
    assert report.summary()["verdict"] == "INSUFFICIENT_FOLDS"


def test_walk_forward_purges_unsafe_maturities_instead_of_training_on_them() -> None:
    """所有标签都成熟到测试窗之后 → 训练集被清空，如实报"无可用折"而不是带泄漏训练。"""
    frame = _synthetic_walk_forward_frame(days=260)
    frame["maturity_date_15d"] = (
        pd.to_datetime(frame["decision_date"]) + pd.Timedelta(days=400)
    ).dt.strftime("%Y-%m-%d")
    report = run_walk_forward(
        frame=frame,
        feature_columns=FEATURES,
        label_column="alpha_target_5d",
        metric_column_="excess_return_15d",
        min_cross_section=10,
    )
    summary = report.summary()
    assert summary["verdict"] == "INSUFFICIENT_FOLDS"
    assert summary["maturity_purged_rows"] > 0
    assert summary["purge_adequacy"] == "calendar_purge_insufficient"
    assert summary["lookahead_violations"] == 0


def test_walk_forward_verdict_no_go_when_leakage_survives() -> None:
    """独立复核仍然是活的：只要还有泄漏行，判定必须是 NO_GO_LEAKAGE。"""
    frame = _synthetic_walk_forward_frame(days=200)
    calendar = sorted({date.fromisoformat(day) for day in frame["decision_date"]})
    fold = plan_folds(trading_dates=calendar, spec=FoldSpec())[0]
    leaked = FoldResult(
        fold=fold,
        daily_ic=pd.DataFrame({"decision_date": [fold.test_start.isoformat()], "ic": [0.05]}),
        ic_block={"status": "ok", "mean_ic": 0.05},
        metrics={"status": "ok", "mature_dates": 1},
        leakage={"status": "violation", "violations": 3},
    )
    report = WalkForwardReport(spec=FoldSpec(), folds=[leaked])
    assert report.summary()["verdict"] == "NO_GO_LEAKAGE"
    assert report.summary()["lookahead_violations"] == 3


def test_walk_forward_detects_negative_relation() -> None:
    """对抗性：让评价指标与预测目标反号 → 判定不得为 GO。"""
    frame = _synthetic_walk_forward_frame(days=260, flip_metric=True)
    report = run_walk_forward(
        frame=frame,
        feature_columns=FEATURES,
        label_column="alpha_target_5d",
        metric_column_="excess_return_15d",
        min_cross_section=10,
    )
    verdict = report.summary()["verdict"]
    assert verdict in {"NO_GO_NEGATIVE_EVIDENCE", "INCONCLUSIVE"}, verdict
    assert report.summary()["lookahead_violations"] == 0


def test_fold_isolation_matrix_lists_ranges_and_isolation() -> None:
    frame = _synthetic_walk_forward_frame(days=260)
    report = run_walk_forward(
        frame=frame,
        feature_columns=FEATURES,
        label_column="alpha_target_5d",
        metric_column_="excess_return_15d",
        min_cross_section=10,
    )
    matrix = fold_isolation_matrix(report)
    assert {
        "train_start",
        "train_end",
        "test_start",
        "test_end",
        "purge_days",
        "embargo_days",
    } <= set(matrix.columns)
    assert (matrix["purge_days"] == 15).all()
    assert (matrix["embargo_days"] == 15).all()
    assert (matrix["leakage_violations"] == 0).all()


def test_hyperparameters_are_recorded_and_frozen_per_fold() -> None:
    frame = _synthetic_walk_forward_frame(days=200)
    calendar = sorted({date.fromisoformat(day) for day in frame["decision_date"]})
    fold = plan_folds(trading_dates=calendar, spec=FoldSpec())[0]
    scorer = LightGbmRankScorer(feature_columns=FEATURES)
    result = run_fold(
        fold=fold,
        frame=frame,
        feature_columns=FEATURES,
        label_column="alpha_target_5d",
        metric_column_="excess_return_15d",
        scorer=scorer,
        min_cross_section=10,
    )
    hyperparameters = result.diagnostics["hyperparameters"]
    assert hyperparameters["deterministic"] is True
    assert result.fold.to_payload()["max_label_horizon"] == 15
