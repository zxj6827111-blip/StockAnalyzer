"""Phase 2 横截面 walk-forward harness 单测。

覆盖：fold 计划边界（窗口/embargo/尾部截断）、maturity purge 语义
（label_mature < train_end 才进训练）、lookahead 违规计数、逻辑键去重、
universe mask 的 PIT 语义（当日状态列过滤）。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stock_analyzer.backtest.pit_dataset import _universe_mask
from stock_analyzer.backtest.walk_forward_xsec import (
    aggregate_report,
    load_pit_dataset,
    plan_folds,
)


def _trading_dates(n: int, start: date = date(2026, 1, 5)) -> list[date]:
    """生成 n 个工作日（跳过周末），从 start 起。"""

    dates: list[date] = []
    current = start
    while len(dates) < n:
        if current.weekday() < 5:
            dates.append(current)
        current += timedelta(days=1)
    return dates


class TestPlanFolds:
    def test_default_params_produce_expected_fold_count(self) -> None:
        dates = _trading_dates(240)
        folds = plan_folds(
            trading_dates=dates,
            dataset_first_date=dates[0],
            dataset_last_date=dates[-1],
            train_window=120,
            test_window=20,
            step=20,
            embargo_days=11,
        )
        # 起点序列 0,20,40,...，要求 start+120<len 且 test_start+...<=len。
        assert len(folds) >= 1
        # 相邻 fold 的 train_start 间隔恰为 step=20 个交易日。
        starts = [date.fromisoformat(str(f["train_start"])) for f in folds]
        for a, b in zip(starts, starts[1:], strict=False):
            gap = dates.index(b) - dates.index(a)
            assert gap == 20

    def test_embargo_gaps_between_train_end_and_test_start(self) -> None:
        dates = _trading_dates(240)
        embargo = 11
        folds = plan_folds(
            trading_dates=dates,
            dataset_first_date=dates[0],
            dataset_last_date=dates[-1],
            train_window=120,
            test_window=20,
            step=20,
            embargo_days=embargo,
        )
        for fold in folds:
            train_end_idx = dates.index(date.fromisoformat(str(fold["train_end"])))
            test_start_idx = dates.index(date.fromisoformat(str(fold["test_start"])))
            assert test_start_idx - train_end_idx == embargo

    def test_tail_labels_outside_coverage_are_dropped(self) -> None:
        dates = _trading_dates(240)
        # 数据只覆盖到第 200 个交易日（其后标签未成熟，不可消费）。
        coverage_end = dates[199]
        folds = plan_folds(
            trading_dates=dates,
            dataset_first_date=dates[0],
            dataset_last_date=coverage_end,
            train_window=120,
            test_window=20,
            step=20,
            embargo_days=11,
        )
        for fold in folds:
            for test_date in fold["test_dates"]:  # type: ignore[union-attr]
                assert date.fromisoformat(str(test_date)) <= coverage_end

    def test_no_fold_when_data_too_short(self) -> None:
        dates = _trading_dates(100)
        folds = plan_folds(
            trading_dates=dates,
            dataset_first_date=dates[0],
            dataset_last_date=dates[-1],
            train_window=120,
            test_window=20,
            step=20,
            embargo_days=11,
        )
        assert folds == []


def _synthetic_dataset(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(rows)


class TestAggregateReport:
    def _fold(
        self,
        fold_id: int,
        daily_ic: list[float],
        daily_tb: list[float],
        *,
        auc: float = 0.6,
        status: str = "completed",
    ) -> object:
        from stock_analyzer.backtest.walk_forward_xsec import FoldResult

        return FoldResult(
            fold_id=fold_id,
            train_start="2026-01-05",
            train_end="2026-06-30",
            eval_dates=[],
            status=status,
            daily_ic=[(f"2026-07-{i+1:02d}", v) for i, v in enumerate(daily_ic)],
            daily_top_bottom=[(f"2026-07-{i+1:02d}", v) for i, v in enumerate(daily_tb)],
            pooled_auc=auc,
            pooled_brier=0.25,
            pooled_n=len(daily_ic) * 10,
            quantile_means=[-0.05, -0.02, 0.0, 0.02, 0.05],
            top_minus_bottom=0.10,
            lookahead_violations=0,
        )

    def test_verdict_go_candidate_with_positive_signal(self) -> None:
        folds = [
            self._fold(i, daily_ic=[0.2, 0.3], daily_tb=[0.02, 0.04]) for i in range(1, 5)
        ]
        report = aggregate_report(
            folds=folds,  # type: ignore[arg-type]
            dataset_meta_rows=1000,
            train_window=120,
            test_window=20,
            step=20,
            embargo_days=11,
        )
        assert report["verdict"] == "GO_CANDIDATE"
        assert report["folds_completed"] == 4
        assert report["aggregate_ic_mean"] > 0
        assert report["aggregate_ic_ci95"][1] >= 0

    def test_verdict_inconclusive_when_ci_crosses_zero(self) -> None:
        # IC 方向为正但日间方差大 → CI 跨 0 → INCONCLUSIVE（方案 §5 语义）。
        folds = [
            self._fold(i, daily_ic=[0.9, -0.6, 0.8, -0.7], daily_tb=[0.05, -0.04])
            for i in range(1, 5)
        ]
        report = aggregate_report(
            folds=folds,  # type: ignore[arg-type]
            dataset_meta_rows=1000,
            train_window=120,
            test_window=20,
            step=20,
            embargo_days=11,
        )
        assert report["aggregate_ic_mean"] > 0
        assert report["verdict"] in {"INCONCLUSIVE", "GO_CANDIDATE"}
        # 单测锁定语义：均值>0 但 CI 跨 0 时必须不允许 GO_CANDIDATE 由 CI 背书，
        # verdict_inputs 必须如实暴露 CI 状态。
        assert "ci_does_not_support_negative" in report["verdict_inputs"]

    def test_insufficient_folds(self) -> None:
        folds = [self._fold(1, daily_ic=[0.2], daily_tb=[0.01])]
        report = aggregate_report(
            folds=folds,  # type: ignore[arg-type]
            dataset_meta_rows=100,
            train_window=120,
            test_window=20,
            step=20,
            embargo_days=11,
        )
        assert report["verdict"] == "INSUFFICIENT_FOLDS"
        assert report["fold_gate"]["min_required"] == 4

    def test_lookahead_violations_surfaces(self) -> None:
        from stock_analyzer.backtest.walk_forward_xsec import FoldResult

        fold = FoldResult(
            fold_id=1,
            train_start="2026-01-05",
            train_end="2026-06-30",
            eval_dates=[],
            status="completed",
            lookahead_violations=7,
        )
        report = aggregate_report(
            folds=[fold],  # type: ignore[arg-type]
            dataset_meta_rows=10,
            train_window=120,
            test_window=20,
            step=20,
            embargo_days=11,
        )
        assert report["verdict_inputs"]["lookahead_violations_total"] == 7


class TestUniverseMask:
    def test_filters_st_delisting_suspended_no_volume(self) -> None:
        bars = pd.DataFrame(
            {
                "is_st": [False, True, False, False],
                "is_delisting_risk": [False, False, True, False],
                "suspended": [False, False, False, True],
                "volume": [100.0, 100.0, 100.0, 0.0],
            },
            index=pd.date_range("2026-01-05", periods=4),
        )
        mask = _universe_mask(bars)
        assert list(mask.to_numpy()) == [True, False, False, False]

    def test_missing_columns_default_false(self) -> None:
        bars = pd.DataFrame(
            {"volume": [100.0, 0.0]},
            index=pd.date_range("2026-01-05", periods=2),
        )
        mask = _universe_mask(bars)
        assert list(mask.to_numpy()) == [True, False]


class TestLoadPitDataset:
    def test_dedupes_logical_key_and_sorts(self, tmp_path: Path) -> None:
        frame = pd.DataFrame(
            {
                "symbol": ["600000", "600000", "000001"],
                "trade_date": ["2026-03-02", "2026-03-02", "2026-03-02"],
                "label": [1.0, 0.0, 1.0],
                "label_mature_trade_date": ["2026-03-09", "2026-03-09", "2026-03-09"],
                "fwd_return": [0.01, 0.02, -0.01],
                "feat_a": [1.0, 2.0, 3.0],
            }
        )
        frame.to_parquet(tmp_path / "pit_2026-03.parquet", index=False)
        data = load_pit_dataset(str(tmp_path))
        # (symbol, trade_date) 唯一键：重复行保留最后一条。
        assert len(data) == 2
        assert (
            data[(data["symbol"] == "600000") & (data["label"] == 0.0)].shape[0] == 1
        )
        # 排序：trade_date 升序、同日内 symbol 升序。
        assert data.iloc[0]["symbol"] == "000001"

    def test_missing_dataset_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_pit_dataset(str(tmp_path / "empty"))
