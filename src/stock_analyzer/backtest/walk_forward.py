"""Walk-forward training and evaluation pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

import numpy as np
import pandas as pd
from pandas import Timestamp

from stock_analyzer.backtest.matcher import ExecutionMatcher
from stock_analyzer.config import (
    BacktestMatcherConfig,
    LabelsConfig,
    LimitRuleConfig,
    MarketRelativeFeatureConfig,
    ModelsConfig,
    TrainingConfig,
    WalkForwardConfig,
)
from stock_analyzer.data.provider import MarketDataProvider
from stock_analyzer.feature.engineer import FeatureEngineer
from stock_analyzer.feature.market_context import build_market_relative_frame
from stock_analyzer.labels.soup import build_soup_labels
from stock_analyzer.models.predictor import SignalPredictor
from stock_analyzer.models.trainer import ModelTrainer
from stock_analyzer.time_semantics import apply_time_invariants_to_frame


@dataclass(slots=True)
class FoldReport:
    fold_id: int
    train_samples: int
    calibration_samples: int
    test_samples: int
    embargo_days: int
    accuracy: float
    auc: float
    brier: float
    precision_at_k: float
    recall_at_k: float
    mean_prob_spread: float
    trade_count: int
    win_count: int
    skipped_slippage: int
    entry_no_fill_count: int
    exit_no_fill_count: int
    forced_exit_count: int
    equity_end: float
    lgbm_backend: str
    xgb_backend: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class WalkForwardReport:
    folds: list[FoldReport]
    summary: dict[str, float]

    def to_dict(self) -> dict[str, object]:
        return {"folds": [fold.to_dict() for fold in self.folds], "summary": self.summary}


class WalkForwardEngine:
    """Chronological walk-forward with rolling retraining."""

    def __init__(
        self,
        training: TrainingConfig,
        labels: LabelsConfig,
        walk_forward: WalkForwardConfig,
        matcher: BacktestMatcherConfig,
        limit_rule: LimitRuleConfig | None = None,
        models: ModelsConfig | None = None,
        settlement_lag_days: int = 1,
        provider: MarketDataProvider | None = None,
        market_relative_feature: MarketRelativeFeatureConfig | None = None,
    ) -> None:
        self._training = training
        self._labels = labels
        self._walk_forward = walk_forward
        self._settlement_lag_days = max(0, int(settlement_lag_days))
        self._matcher = ExecutionMatcher(matcher, limit_rule=limit_rule)
        self._engineer = FeatureEngineer()
        self._provider = provider
        self._market_relative_feature = (
            market_relative_feature
            if market_relative_feature is not None
            else MarketRelativeFeatureConfig()
        )
        self._trainer = ModelTrainer(
            training=training,
            labels=labels,
            models=models,
            settlement_lag_days=self._settlement_lag_days,
            provider=provider,
            market_relative_feature=self._market_relative_feature,
        )

    def run_on_bars(
        self,
        bars: pd.DataFrame,
        strategy: str = "trend",
        market_index: pd.DataFrame | None = None,
    ) -> WalkForwardReport:
        active_bars, _bars_time_gate = apply_time_invariants_to_frame(
            bars,
            decision_time=datetime.now(),
            timezone="Asia/Shanghai",
            holding_horizon_days=self._labels.horizon_days,
            settlement_lag_days=self._settlement_lag_days,
            require_mature_label=False,
        )
        if active_bars.empty:
            raise ValueError("no bars available after time invariants gate")
        effective_market_index = market_index
        if effective_market_index is None and bool(self._market_relative_feature.enabled):
            if self._provider is None:
                raise ValueError("market_relative_feature_enabled_requires_provider")
            effective_market_index = build_market_relative_frame(
                self._provider,
                bars=active_bars,
                config=self._market_relative_feature,
            )
        features = self._engineer.transform(
            active_bars,
            market_index=effective_market_index,
        )
        labels = build_soup_labels(
            bars=active_bars,
            take_profit_pct=self._labels.take_profit_pct,
            stop_loss_pct=self._labels.stop_loss_pct,
            horizon_days=self._labels.horizon_days,
            price_basis=self._labels.pnl_price_basis,
            exclude_untradable=self._labels.exclude_untradable,
            conflict_policy=self._labels.conflict_policy,
            conflict_soft_label_value=self._labels.conflict_soft_label_value,
        )
        label_name = labels.name or "label_soup_tp_before_sl"
        aligned = features.join(labels, how="inner").dropna(subset=[label_name])
        aligned, _time_gate = apply_time_invariants_to_frame(
            aligned,
            decision_time=datetime.now(),
            timezone="Asia/Shanghai",
            holding_horizon_days=self._labels.horizon_days,
            settlement_lag_days=self._settlement_lag_days,
            require_mature_label=True,
        )
        if aligned.empty:
            raise ValueError("no aligned rows for walk-forward")

        train_window = self._walk_forward.train_window
        test_window = self._walk_forward.test_window
        step = self._walk_forward.step
        if train_window <= 0 or test_window <= 0 or step <= 0:
            raise ValueError("walk-forward window sizes must be positive")

        folds: list[FoldReport] = []
        equity = 1.0
        fold_id = 1
        start = 0
        while start + train_window + test_window <= len(aligned):
            train_slice = aligned.iloc[start : start + train_window]
            test_slice = aligned.iloc[start + train_window : start + train_window + test_window]

            train_features = train_slice[features.columns]
            train_labels = train_slice[label_name]
            result = self._trainer.train_on_feature_label(train_features, train_labels)
            predictor = SignalPredictor.from_artifact(result.artifact)

            predictions: list[int] = []
            actuals: list[int] = []
            trades = 0
            wins = 0
            skipped_slippage = 0
            entry_no_fill_count = 0
            exit_no_fill_count = 0
            forced_exit_count = 0
            for idx, row in test_slice.iterrows():
                probs = predictor.predict_row(row)
                pred = 1 if probs["meta"] >= self._walk_forward.decision_threshold else 0
                label_value = float(row.get(label_name, 0.0))
                actual = int(label_value >= 0.5)
                predictions.append(pred)
                actuals.append(actual)

                if pred != 1:
                    continue
                if not isinstance(idx, Timestamp):
                    continue
                bar_source = active_bars.loc[idx]
                if isinstance(bar_source, pd.DataFrame):
                    bar = _bar_snapshot(bar_source.iloc[-1])
                else:
                    bar = _bar_snapshot(bar_source)
                decision = self._matcher.can_buy(bar)
                if not decision.executable:
                    trades += 1
                    entry_no_fill_count += 1
                    continue

                atr14 = float(row.get("atr14", 0.0))
                volume_ratio = float(row.get("volume_ratio", 1.0))
                entry_price = float(bar.get("close", 0.0))
                if entry_price <= 0:
                    trades += 1
                    entry_no_fill_count += 1
                    continue
                slippage_ratio = self._matcher.dynamic_slippage_ratio(
                    strategy=strategy,
                    atr14=atr14,
                    close=entry_price,
                    volume_ratio=volume_ratio,
                )
                if self._matcher.should_downgrade_by_slippage(slippage_ratio):
                    skipped_slippage += 1
                    continue

                buy_fill = self._matcher.apply_slippage(
                    price=entry_price,
                    side="buy",
                    slippage_ratio=slippage_ratio,
                )
                entry_plan = self._matcher.plan_order(
                    side="buy",
                    price=buy_fill,
                    requested_quantity=1000,
                )
                if not entry_plan.executable:
                    trades += 1
                    entry_no_fill_count += 1
                    continue

                trades += 1
                future_bars = _future_bars(
                    bars=active_bars,
                    anchor=idx,
                    horizon_days=self._labels.horizon_days + self._matcher.max_exit_carry_days + 1,
                )
                exit_result = self._matcher.simulate_exit(
                    entry_price=entry_price,
                    entry_date=idx.to_pydatetime(),
                    future_bars=future_bars,
                    take_profit_pct=self._labels.take_profit_pct,
                    stop_loss_pct=self._labels.stop_loss_pct,
                    horizon_days=self._labels.horizon_days,
                )
                if exit_result.exit_no_fill:
                    exit_no_fill_count += 1
                if exit_result.forced_exit:
                    forced_exit_count += 1

                net_return = 0.0
                if exit_result.executed:
                    sell_fill = self._matcher.apply_slippage(
                        price=exit_result.exit_price,
                        side="sell",
                        slippage_ratio=slippage_ratio,
                    )
                    gross_return = (
                        (sell_fill - buy_fill) / buy_fill
                        if buy_fill > 0
                        else 0.0
                    )
                    round_trip_cost = _estimate_round_trip_cost(
                        matcher=self._matcher,
                        buy_price=buy_fill,
                        sell_price=sell_fill,
                        quantity=entry_plan.quantity,
                    )
                    net_return = gross_return - round_trip_cost
                if net_return > 0:
                    wins += 1
                equity *= max(0.01, 1.0 + net_return)

            accuracy = float(np.mean(np.asarray(predictions) == np.asarray(actuals)))
            folds.append(
                FoldReport(
                    fold_id=fold_id,
                    train_samples=len(train_slice),
                    calibration_samples=result.samples_calibration,
                    test_samples=result.samples_test,
                    embargo_days=result.samples_embargo,
                    accuracy=round(accuracy, 6),
                    auc=_as_float(result.metrics.get("auc"), default=0.5),
                    brier=_as_float(result.metrics.get("brier"), default=0.0),
                    precision_at_k=_as_float(result.metrics.get("precision_at_k"), default=0.0),
                    recall_at_k=_as_float(result.metrics.get("recall_at_k"), default=0.0),
                    mean_prob_spread=_as_float(result.metrics.get("mean_prob_spread"), default=0.0),
                    trade_count=trades,
                    win_count=wins,
                    skipped_slippage=skipped_slippage,
                    entry_no_fill_count=entry_no_fill_count,
                    exit_no_fill_count=exit_no_fill_count,
                    forced_exit_count=forced_exit_count,
                    equity_end=round(equity, 6),
                    lgbm_backend=result.lgbm_backend,
                    xgb_backend=result.xgb_backend,
                )
            )

            fold_id += 1
            start += step

        summary = _summary_metrics(folds, equity)
        return WalkForwardReport(folds=folds, summary=summary)


def _bar_snapshot(row: pd.Series) -> dict[str, float | bool]:
    close = float(row.get("close", 0.0))
    open_price = float(row.get("open", close))
    high_price = float(row.get("high", max(open_price, close)))
    low_price = float(row.get("low", min(open_price, close)))
    return {
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close,
        "up_limit": float(row.get("up_limit", close * 1.1)),
        "down_limit": float(row.get("down_limit", close * 0.9)),
        "suspended": bool(row.get("suspended", False)),
    }


def _future_bars(
    bars: pd.DataFrame,
    anchor: Timestamp,
    horizon_days: int,
) -> list[tuple[datetime, dict[str, float | bool]]]:
    if horizon_days <= 0:
        return []
    try:
        raw_loc = bars.index.get_loc(anchor)
    except KeyError:
        return []

    if isinstance(raw_loc, slice):
        anchor_pos = raw_loc.stop - 1
    elif isinstance(raw_loc, np.ndarray):
        if raw_loc.size == 0:
            return []
        anchor_pos = int(raw_loc[-1])
    else:
        anchor_pos = int(raw_loc)

    start = anchor_pos + 1
    end = min(len(bars), start + horizon_days)
    result: list[tuple[datetime, dict[str, float | bool]]] = []
    for pos in range(start, end):
        ts = bars.index[pos]
        if isinstance(ts, Timestamp):
            date_value = ts.to_pydatetime()
        else:
            date_value = pd.Timestamp(ts).to_pydatetime()
        row = bars.iloc[pos]
        result.append((date_value, _bar_snapshot(row)))
    return result


def _estimate_round_trip_cost(
    matcher: ExecutionMatcher,
    buy_price: float,
    sell_price: float,
    quantity: int = 1000,
) -> float:
    amount = buy_price * quantity
    if amount <= 0:
        return 0.0
    buy_cost = matcher.estimate_cost("buy", price=buy_price, quantity=quantity)
    sell_cost = matcher.estimate_cost("sell", price=sell_price, quantity=quantity)
    return (buy_cost + sell_cost) / amount


def _summary_metrics(folds: list[FoldReport], final_equity: float) -> dict[str, float]:
    if not folds:
        return {
            "folds": 0.0,
            "avg_accuracy": 0.0,
            "avg_auc": 0.0,
            "avg_brier": 0.0,
            "avg_precision_at_k": 0.0,
            "avg_recall_at_k": 0.0,
            "avg_mean_prob_spread": 0.0,
            "avg_train_samples": 0.0,
            "avg_calibration_samples": 0.0,
            "avg_test_samples": 0.0,
            "avg_embargo_days": 0.0,
            "final_equity": 1.0,
            "total_trades": 0.0,
            "total_skipped_slippage": 0.0,
            "total_entry_no_fill": 0.0,
            "total_exit_no_fill": 0.0,
            "total_forced_exit": 0.0,
            "win_rate": 0.0,
        }

    avg_accuracy = float(np.mean([fold.accuracy for fold in folds]))
    avg_auc = float(np.mean([fold.auc for fold in folds]))
    avg_brier = float(np.mean([fold.brier for fold in folds]))
    avg_precision_at_k = float(np.mean([fold.precision_at_k for fold in folds]))
    avg_recall_at_k = float(np.mean([fold.recall_at_k for fold in folds]))
    avg_mean_prob_spread = float(np.mean([fold.mean_prob_spread for fold in folds]))
    avg_train_samples = float(np.mean([fold.train_samples for fold in folds]))
    avg_calibration_samples = float(np.mean([fold.calibration_samples for fold in folds]))
    avg_test_samples = float(np.mean([fold.test_samples for fold in folds]))
    avg_embargo_days = float(np.mean([fold.embargo_days for fold in folds]))
    total_trades = float(np.sum([fold.trade_count for fold in folds]))
    win_trades = float(np.sum([fold.win_count for fold in folds]))
    total_skipped_slippage = float(np.sum([fold.skipped_slippage for fold in folds]))
    total_entry_no_fill = float(np.sum([fold.entry_no_fill_count for fold in folds]))
    total_exit_no_fill = float(np.sum([fold.exit_no_fill_count for fold in folds]))
    total_forced_exit = float(np.sum([fold.forced_exit_count for fold in folds]))
    win_rate = win_trades / total_trades if total_trades > 0 else 0.0

    return {
        "folds": float(len(folds)),
        "avg_accuracy": round(avg_accuracy, 6),
        "avg_auc": round(avg_auc, 6),
        "avg_brier": round(avg_brier, 6),
        "avg_precision_at_k": round(avg_precision_at_k, 6),
        "avg_recall_at_k": round(avg_recall_at_k, 6),
        "avg_mean_prob_spread": round(avg_mean_prob_spread, 6),
        "avg_train_samples": round(avg_train_samples, 6),
        "avg_calibration_samples": round(avg_calibration_samples, 6),
        "avg_test_samples": round(avg_test_samples, 6),
        "avg_embargo_days": round(avg_embargo_days, 6),
        "final_equity": round(final_equity, 6),
        "total_trades": total_trades,
        "total_skipped_slippage": total_skipped_slippage,
        "total_entry_no_fill": total_entry_no_fill,
        "total_exit_no_fill": total_exit_no_fill,
        "total_forced_exit": total_forced_exit,
        "win_rate": round(win_rate, 6),
        "timestamp": float(datetime.now().timestamp()),
    }


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default
