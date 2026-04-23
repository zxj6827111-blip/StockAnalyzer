from __future__ import annotations

import pytest

from stock_analyzer.backtest.walk_forward import WalkForwardEngine
from stock_analyzer.config import (
    BacktestMatcherConfig,
    LabelsConfig,
    TrainingConfig,
    WalkForwardConfig,
)
from stock_analyzer.data.provider import SyntheticProvider


def test_walk_forward_generates_fold_reports() -> None:
    bars = SyntheticProvider(seed_offset=999).fetch_daily_bars(symbol="600000", lookback_days=360)
    engine = WalkForwardEngine(
        training=TrainingConfig(enabled=True, min_samples=40, validation_ratio=0.2),
        labels=LabelsConfig(take_profit_pct=0.05, stop_loss_pct=0.05, horizon_days=5),
        walk_forward=WalkForwardConfig(
            enabled=True, train_window=120, test_window=40, step=40, decision_threshold=0.5
        ),
        matcher=BacktestMatcherConfig(),
    )
    report = engine.run_on_bars(bars)
    assert len(report.folds) >= 1
    assert all(fold.train_samples > 0 for fold in report.folds)
    assert all(fold.calibration_samples > 0 for fold in report.folds)
    assert all(fold.test_samples > 0 for fold in report.folds)
    assert all(fold.embargo_days >= 0 for fold in report.folds)
    assert report.summary["final_equity"] > 0
    assert report.summary["avg_train_samples"] > 0
    assert report.summary["avg_calibration_samples"] > 0
    assert report.summary["avg_test_samples"] > 0
    assert report.summary["avg_embargo_days"] >= 0
    assert "avg_precision_at_k" in report.summary
    assert "avg_recall_at_k" in report.summary
    assert "avg_mean_prob_spread" in report.summary
    assert report.summary["total_skipped_slippage"] >= 0
    assert report.summary["total_entry_no_fill"] >= 0
    assert report.summary["total_exit_no_fill"] >= 0
    assert report.summary["total_forced_exit"] >= 0


def test_walk_forward_rejects_future_available_time_rows() -> None:
    bars = SyntheticProvider(seed_offset=321).fetch_daily_bars(symbol="600000", lookback_days=180)
    bars["event_time"] = [
        ts.to_pydatetime().strftime("%Y-%m-%dT09:30:00+08:00")
        for ts in bars.index
    ]
    bars["available_time"] = "2099-01-01T09:30:00+08:00"
    engine = WalkForwardEngine(
        training=TrainingConfig(enabled=True, min_samples=40, validation_ratio=0.2),
        labels=LabelsConfig(take_profit_pct=0.05, stop_loss_pct=0.05, horizon_days=5),
        walk_forward=WalkForwardConfig(
            enabled=True, train_window=120, test_window=40, step=40, decision_threshold=0.5
        ),
        matcher=BacktestMatcherConfig(),
    )
    with pytest.raises(ValueError, match="no bars available after time invariants gate"):
        engine.run_on_bars(bars)


def test_walk_forward_tracks_entry_no_fill_audit_counts() -> None:
    bars = SyntheticProvider(seed_offset=777).fetch_daily_bars(symbol="600000", lookback_days=260)
    bars["up_limit"] = bars["close"]
    engine = WalkForwardEngine(
        training=TrainingConfig(enabled=True, min_samples=40, validation_ratio=0.2),
        labels=LabelsConfig(take_profit_pct=0.05, stop_loss_pct=0.05, horizon_days=5),
        walk_forward=WalkForwardConfig(
            enabled=True, train_window=120, test_window=40, step=40, decision_threshold=0.0
        ),
        matcher=BacktestMatcherConfig(reject_limit_up_buy=True),
    )
    report = engine.run_on_bars(bars)
    assert report.summary["total_entry_no_fill"] > 0
    assert report.summary["total_forced_exit"] == 0


def test_walk_forward_applies_min_notional_rule_in_execution_path() -> None:
    bars = SyntheticProvider(seed_offset=778).fetch_daily_bars(symbol="600000", lookback_days=260)
    engine = WalkForwardEngine(
        training=TrainingConfig(enabled=True, min_samples=40, validation_ratio=0.2),
        labels=LabelsConfig(take_profit_pct=0.05, stop_loss_pct=0.05, horizon_days=5),
        walk_forward=WalkForwardConfig(
            enabled=True, train_window=120, test_window=40, step=40, decision_threshold=0.0
        ),
        matcher=BacktestMatcherConfig(
            reject_limit_up_buy=False,
            min_notional_per_order=1_000_000_000.0,
        ),
    )
    report = engine.run_on_bars(bars)
    assert report.summary["total_trades"] > 0
    assert report.summary["total_entry_no_fill"] > 0
