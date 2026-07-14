"""Training split and label-policy diagnostics for v1.3 acceptance."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from stock_analyzer.config import (
    LabelsConfig,
    MarketRelativeFeatureConfig,
    ModelsConfig,
    TrainingConfig,
)
from stock_analyzer.data.provider import MarketDataProvider
from stock_analyzer.feature.engineer import FeatureEngineer
from stock_analyzer.feature.market_context import build_market_relative_frame
from stock_analyzer.labels.soup import build_soup_labels, detect_soup_label_same_bar_conflicts
from stock_analyzer.models.adapters import LightGBMAdapter, XGBoostAdapter
from stock_analyzer.models.calibration import IsotonicCalibrator
from stock_analyzer.models.trainer import (
    ModelTrainer,
    _build_meta_weights,
    _evaluate_metrics,
)
from stock_analyzer.time_semantics import apply_time_invariants_to_frame


@dataclass(slots=True)
class PreparedTrainingDataset:
    bars: pd.DataFrame
    features: pd.DataFrame
    aligned: pd.DataFrame
    feature_columns: list[str]
    label_column: str


def build_training_evaluation_report(
    *,
    bars: pd.DataFrame,
    training: TrainingConfig,
    labels: LabelsConfig,
    models: ModelsConfig,
    settlement_lag_days: int = 1,
    intraday_1m: pd.DataFrame | None = None,
    intraday_5m: pd.DataFrame | None = None,
    provider: MarketDataProvider | None = None,
    market_relative_feature: MarketRelativeFeatureConfig | None = None,
) -> dict[str, object]:
    prepared = _prepare_training_dataset(
        bars=bars,
        training=training,
        labels=labels,
        models=models,
        settlement_lag_days=settlement_lag_days,
        intraday_1m=intraday_1m,
        intraday_5m=intraday_5m,
        provider=provider,
        market_relative_feature=market_relative_feature,
    )
    strict = _strict_temporal_regime_report(
        prepared=prepared,
        training=training,
        labels=labels,
        models=models,
        settlement_lag_days=settlement_lag_days,
    )
    legacy = _legacy_validation_regime_report(
        prepared=prepared,
        training=training,
    )
    return {
        "generated_at": datetime.now().isoformat(),
        "dataset": {
            "rows": int(len(prepared.aligned)),
            "feature_count": len(prepared.feature_columns),
            "label_column": prepared.label_column,
            "label_conflict_policy": labels.conflict_policy,
            "same_bar_conflict_rows": int(
                detect_soup_label_same_bar_conflicts(
                    prepared.bars,
                    take_profit_pct=labels.take_profit_pct,
                    stop_loss_pct=labels.stop_loss_pct,
                    horizon_days=labels.horizon_days,
                    price_basis=labels.pnl_price_basis,
                    exclude_untradable=labels.exclude_untradable,
                ).sum()
            ),
        },
        "split_regimes": {
            "strict_temporal": strict,
            "legacy_validation_only": legacy,
        },
    }


def build_label_conflict_shadow_report(
    *,
    bars: pd.DataFrame,
    training: TrainingConfig,
    labels: LabelsConfig,
    models: ModelsConfig,
    settlement_lag_days: int = 1,
    policies: list[str] | None = None,
    intraday_1m: pd.DataFrame | None = None,
    intraday_5m: pd.DataFrame | None = None,
    provider: MarketDataProvider | None = None,
    market_relative_feature: MarketRelativeFeatureConfig | None = None,
) -> dict[str, object]:
    engineer = FeatureEngineer()
    filtered_bars, _bars_time_gate = apply_time_invariants_to_frame(
        bars,
        decision_time=datetime.now(),
        timezone="Asia/Shanghai",
        holding_horizon_days=labels.horizon_days,
        settlement_lag_days=settlement_lag_days,
        require_mature_label=False,
    )
    if filtered_bars.empty:
        raise ValueError("no bars available after time invariants gate")

    features = engineer.transform(
        filtered_bars,
        intraday_1m=intraday_1m,
        intraday_5m=intraday_5m,
        market_index=_maybe_build_market_index(
            bars=filtered_bars,
            provider=provider,
            market_relative_feature=market_relative_feature,
        ),
    )
    conflict_mask = detect_soup_label_same_bar_conflicts(
        filtered_bars,
        take_profit_pct=labels.take_profit_pct,
        stop_loss_pct=labels.stop_loss_pct,
        horizon_days=labels.horizon_days,
        price_basis=labels.pnl_price_basis,
        exclude_untradable=labels.exclude_untradable,
    )
    active_policies = _normalize_policies(
        configured=labels.conflict_policy,
        requested=policies,
    )
    baseline_labels = build_soup_labels(
        filtered_bars,
        take_profit_pct=labels.take_profit_pct,
        stop_loss_pct=labels.stop_loss_pct,
        horizon_days=labels.horizon_days,
        price_basis=labels.pnl_price_basis,
        exclude_untradable=labels.exclude_untradable,
        conflict_policy=labels.conflict_policy,
        conflict_soft_label_value=labels.conflict_soft_label_value,
    )

    items: list[dict[str, object]] = []
    for policy in active_policies:
        policy_labels = build_soup_labels(
            filtered_bars,
            take_profit_pct=labels.take_profit_pct,
            stop_loss_pct=labels.stop_loss_pct,
            horizon_days=labels.horizon_days,
            price_basis=labels.pnl_price_basis,
            exclude_untradable=labels.exclude_untradable,
            conflict_policy=policy,
            conflict_soft_label_value=labels.conflict_soft_label_value,
        )
        policy_config = labels.model_copy(update={"conflict_policy": policy})
        trainer = ModelTrainer(
            training=training,
            labels=policy_config,
            models=models,
            settlement_lag_days=settlement_lag_days,
        )
        result = trainer.train_on_feature_label(features=features, labels=policy_labels)
        aligned = features.join(policy_labels, how="inner")
        aligned = aligned.dropna(subset=[policy_labels.name or "label_soup_tp_before_sl"])
        aligned, _time_gate = apply_time_invariants_to_frame(
            aligned,
            decision_time=datetime.now(),
            timezone="Asia/Shanghai",
            holding_horizon_days=labels.horizon_days,
            settlement_lag_days=settlement_lag_days,
            require_mature_label=True,
        )
        values = aligned[policy_labels.name or "label_soup_tp_before_sl"].to_numpy(dtype=float)
        baseline_aligned = baseline_labels.reindex(aligned.index)
        delta_count = int(
            np.sum(np.abs(values - baseline_aligned.to_numpy(dtype=float)) > 1e-9)
        )
        conflict_values = policy_labels.reindex(conflict_mask.index[conflict_mask]).dropna()
        items.append(
            {
                "policy": policy,
                "positive_rate": round(float(np.mean(values >= 0.5)), 6) if len(values) else 0.0,
                "neutral_rate": round(
                    float(np.mean((values > 0.0) & (values < 1.0))),
                    6,
                )
                if len(values)
                else 0.0,
                "rows_changed_vs_configured": delta_count,
                "same_bar_conflict_rows": int(conflict_mask.sum()),
                "conflict_positive_rate": round(
                    float(np.mean(conflict_values.to_numpy(dtype=float) >= 0.5)),
                    6,
                )
                if len(conflict_values)
                else 0.0,
                "train_samples": result.samples_train,
                "calibration_samples": result.samples_calibration,
                "test_samples": result.samples_test,
                "embargo_days": result.samples_embargo,
                "lgbm_backend": result.lgbm_backend,
                "xgb_backend": result.xgb_backend,
                "metrics": dict(result.metrics),
            }
        )

    return {
        "generated_at": datetime.now().isoformat(),
        "configured_policy": labels.conflict_policy,
        "same_bar_conflict_rows": int(conflict_mask.sum()),
        "policies": items,
    }


def persist_diagnostic_report(*, report: dict[str, object], output_path: str | Path) -> str:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(target)


def _prepare_training_dataset(
    *,
    bars: pd.DataFrame,
    training: TrainingConfig,
    labels: LabelsConfig,
    models: ModelsConfig,
    settlement_lag_days: int,
    intraday_1m: pd.DataFrame | None,
    intraday_5m: pd.DataFrame | None,
    provider: MarketDataProvider | None,
    market_relative_feature: MarketRelativeFeatureConfig | None,
) -> PreparedTrainingDataset:
    trainer = ModelTrainer(
        training=training,
        labels=labels,
        models=models,
        settlement_lag_days=settlement_lag_days,
        provider=provider,
        market_relative_feature=market_relative_feature,
    )
    filtered_bars, _bars_time_gate = apply_time_invariants_to_frame(
        bars,
        decision_time=datetime.now(),
        timezone="Asia/Shanghai",
        holding_horizon_days=labels.horizon_days,
        settlement_lag_days=settlement_lag_days,
        require_mature_label=False,
    )
    if filtered_bars.empty:
        raise ValueError("no bars available after time invariants gate")

    features = trainer._engineer.transform(
        filtered_bars,
        intraday_1m=intraday_1m,
        intraday_5m=intraday_5m,
        market_index=_maybe_build_market_index(
            bars=filtered_bars,
            provider=provider,
            market_relative_feature=market_relative_feature,
        ),
    )
    raw_labels = build_soup_labels(
        bars=filtered_bars,
        take_profit_pct=labels.take_profit_pct,
        stop_loss_pct=labels.stop_loss_pct,
        horizon_days=labels.horizon_days,
        price_basis=labels.pnl_price_basis,
        exclude_untradable=labels.exclude_untradable,
        conflict_policy=labels.conflict_policy,
        conflict_soft_label_value=labels.conflict_soft_label_value,
    )
    raw_label_name = raw_labels.name
    if not isinstance(raw_label_name, str) or not raw_label_name.strip():
        raw_labels = raw_labels.rename("label_soup_tp_before_sl")
        label_column = "label_soup_tp_before_sl"
    else:
        label_column = raw_label_name
    aligned = features.join(raw_labels, how="inner").dropna(subset=[label_column])
    aligned, _time_gate = apply_time_invariants_to_frame(
        aligned,
        decision_time=datetime.now(),
        timezone="Asia/Shanghai",
        holding_horizon_days=labels.horizon_days,
        settlement_lag_days=settlement_lag_days,
        require_mature_label=True,
    )
    if aligned.shape[0] < training.min_samples:
        raise ValueError(
            f"insufficient samples for diagnostics: {aligned.shape[0]} < {training.min_samples}"
        )

    feature_columns = list(features.columns)
    if models.include_random_feature_baseline:
        aligned = aligned.copy()
        rng = np.random.default_rng(20260302)
        aligned["__random_baseline__"] = rng.normal(0.0, 1.0, size=len(aligned))
        feature_columns.append("__random_baseline__")

    return PreparedTrainingDataset(
        bars=filtered_bars,
        features=features,
        aligned=aligned,
        feature_columns=feature_columns,
        label_column=label_column,
    )


def _strict_temporal_regime_report(
    *,
    prepared: PreparedTrainingDataset,
    training: TrainingConfig,
    labels: LabelsConfig,
    models: ModelsConfig,
    settlement_lag_days: int,
) -> dict[str, object]:
    trainer = ModelTrainer(
        training=training,
        labels=labels,
        models=models,
        settlement_lag_days=settlement_lag_days,
    )
    result = trainer.train_on_feature_label(
        features=prepared.features,
        labels=prepared.aligned[prepared.label_column],
    )
    effective_embargo = int(result.metrics.get("embargo_days", result.samples_embargo))
    return {
        "regime": "strict_temporal",
        "uses_distinct_calibration_and_test": True,
        "train_samples": result.samples_train,
        "calibration_samples": result.samples_calibration,
        "test_samples": result.samples_test,
        "embargo_days": effective_embargo,
        "lgbm_backend": result.lgbm_backend,
        "xgb_backend": result.xgb_backend,
        "metrics": dict(result.metrics),
    }


def _legacy_validation_regime_report(
    *,
    prepared: PreparedTrainingDataset,
    training: TrainingConfig,
) -> dict[str, object]:
    aligned = prepared.aligned
    validation_ratio = max(0.02, float(training.validation_ratio))
    validation_count = max(1, int(round(len(aligned) * validation_ratio)))
    train_count = max(1, len(aligned) - validation_count)
    if train_count <= 0 or validation_count <= 0:
        raise ValueError("legacy validation regime produced empty split")

    x = aligned[prepared.feature_columns].to_numpy(dtype=float)
    y = aligned[prepared.label_column].to_numpy(dtype=float)
    x_train = x[:train_count]
    y_train = y[:train_count]
    x_validation = x[train_count:]
    y_validation = y[train_count:]

    lgbm = LightGBMAdapter()
    xgb = XGBoostAdapter()
    lgbm.fit(x_train, y_train)
    xgb.fit(x_train, y_train)

    lgbm_raw = lgbm.predict_proba(x_validation)
    xgb_raw = xgb.predict_proba(x_validation)
    lgbm_calibrator = IsotonicCalibrator()
    xgb_calibrator = IsotonicCalibrator()
    lgbm_calibrator.fit(lgbm_raw, y_validation)
    xgb_calibrator.fit(xgb_raw, y_validation)

    lgbm_prob = lgbm_calibrator.predict(lgbm_raw)
    xgb_prob = xgb_calibrator.predict(xgb_raw)
    meta_weights = _build_meta_weights(
        y_true=y_validation,
        lgbm=lgbm_prob,
        xgb=xgb_prob,
    )
    meta_prob = lgbm_prob * meta_weights["lgbm"] + xgb_prob * meta_weights["xgb"]
    metrics = _evaluate_metrics(
        y_true=y_validation,
        lgbm=lgbm_prob,
        xgb=xgb_prob,
        meta=meta_prob,
        precision_at_k_ratio=max(0.01, float(training.precision_at_k_ratio)),
    )
    metrics["calibration_samples"] = float(len(x_validation))
    metrics["test_samples"] = float(len(x_validation))
    metrics["embargo_days"] = 0.0
    return {
        "regime": "legacy_validation_only",
        "uses_distinct_calibration_and_test": False,
        "train_samples": int(len(x_train)),
        "calibration_samples": int(len(x_validation)),
        "test_samples": int(len(x_validation)),
        "embargo_days": 0,
        "lgbm_backend": lgbm.backend,
        "xgb_backend": xgb.backend,
        "metrics": metrics,
        "warning": "validation set is reused for calibration and evaluation",
    }


def _maybe_build_market_index(
    *,
    bars: pd.DataFrame,
    provider: MarketDataProvider | None,
    market_relative_feature: MarketRelativeFeatureConfig | None,
) -> pd.DataFrame | None:
    config = market_relative_feature or MarketRelativeFeatureConfig()
    if not bool(config.enabled):
        return None
    if provider is None:
        raise ValueError("market_relative_feature_enabled_requires_provider")
    return build_market_relative_frame(provider, bars=bars, config=config)


def _normalize_policies(*, configured: str, requested: list[str] | None) -> list[str]:
    active: list[str] = []
    for item in [
        configured,
        "bar_shape_heuristic",
        "soft_label",
        "conservative_zero",
    ]:
        normalized = str(item).strip().lower()
        if normalized and normalized not in active:
            active.append(normalized)
    if requested:
        for item in requested:
            normalized = str(item).strip().lower()
            if normalized and normalized not in active:
                active.append(normalized)
    return active
