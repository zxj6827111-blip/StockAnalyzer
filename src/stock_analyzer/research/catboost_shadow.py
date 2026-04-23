"""Offline CatBoost shadow-model sidecar."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from stock_analyzer.models.fallback import LogisticProbModel

_NON_FEATURE_COLUMNS = {
    "symbol",
    "date",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "label",
    "target",
    "p_lgbm",
    "p_xgb",
    "p_meta",
}


def run_catboost_shadow(
    *,
    reference_frame: pd.DataFrame | None = None,
    records: Sequence[Mapping[str, object]] | None = None,
    feature_columns: Sequence[str] | None = None,
    label_column: str = "label",
    baseline_probability_column: str = "p_meta",
    test_ratio: float = 0.3,
    random_seed: int = 2026,
) -> dict[str, object]:
    frame = _coerce_frame(reference_frame=reference_frame, records=records)
    if frame.empty:
        return {
            "status": "empty",
            "engine": "catboost_shadow_sidecar",
            "backend": "fallback_logit_shadow",
            "feature_count": 0,
            "sample_count": 0,
            "train_samples": 0,
            "test_samples": 0,
            "affects_main_model": False,
            "metrics": {},
            "baseline_metrics": {},
            "top_features": [],
        }

    working = frame.copy()
    if "trade_date" in working.columns:
        working["trade_date"] = pd.to_datetime(working["trade_date"], errors="coerce")
        working = working.sort_values(
            ["trade_date", "symbol"] if "symbol" in working.columns else ["trade_date"]
        )
    elif "date" in working.columns:
        working["date"] = pd.to_datetime(working["date"], errors="coerce")
        working = working.sort_values(
            ["date", "symbol"] if "symbol" in working.columns else ["date"]
        )

    label_series = _resolve_label_series(working=working, label_column=label_column)
    working = working.assign(_label=label_series).dropna(subset=["_label"])
    if working.empty or working["_label"].nunique() < 2:
        return {
            "status": "invalid_input",
            "engine": "catboost_shadow_sidecar",
            "backend": "fallback_logit_shadow",
            "feature_count": 0,
            "sample_count": int(len(working)),
            "train_samples": 0,
            "test_samples": 0,
            "affects_main_model": False,
            "metrics": {},
            "baseline_metrics": {},
            "top_features": [],
        }

    feature_names = (
        list(feature_columns) if feature_columns is not None else _infer_feature_columns(working)
    )
    if not feature_names:
        return {
            "status": "invalid_input",
            "engine": "catboost_shadow_sidecar",
            "backend": "fallback_logit_shadow",
            "feature_count": 0,
            "sample_count": int(len(working)),
            "train_samples": 0,
            "test_samples": 0,
            "affects_main_model": False,
            "metrics": {},
            "baseline_metrics": {},
            "top_features": [],
        }

    matrix = working.loc[:, feature_names].apply(pd.to_numeric, errors="coerce")
    matrix = matrix.fillna(matrix.median()).fillna(0.0)
    usable = working.assign(**{name: matrix[name] for name in feature_names})
    usable = usable.dropna(subset=["_label"])
    if len(usable) < 6:
        return {
            "status": "too_small",
            "engine": "catboost_shadow_sidecar",
            "backend": "fallback_logit_shadow",
            "feature_count": len(feature_names),
            "sample_count": int(len(usable)),
            "train_samples": 0,
            "test_samples": 0,
            "affects_main_model": False,
            "metrics": {},
            "baseline_metrics": {},
            "top_features": [],
        }

    split_at = min(max(int(round(len(usable) * (1.0 - float(test_ratio)))), 3), len(usable) - 2)
    train = usable.iloc[:split_at].copy()
    test = usable.iloc[split_at:].copy()
    x_train = train.loc[:, feature_names].to_numpy(dtype=float)
    y_train = train["_label"].to_numpy(dtype=float)
    x_test = test.loc[:, feature_names].to_numpy(dtype=float)
    y_test = test["_label"].to_numpy(dtype=float)

    probabilities, backend, feature_importance = _fit_shadow_model(
        x_train=x_train,
        y_train=y_train,
        x_test=x_test,
        feature_names=feature_names,
        random_seed=random_seed,
    )
    baseline_probabilities = _resolve_baseline_probabilities(
        test=test,
        baseline_probability_column=baseline_probability_column,
        train_labels=y_train,
    )

    metrics = _classification_metrics(labels=y_test, probabilities=probabilities)
    baseline_metrics = _classification_metrics(labels=y_test, probabilities=baseline_probabilities)

    return {
        "status": "ok",
        "engine": "catboost_shadow_sidecar",
        "backend": backend,
        "feature_count": len(feature_names),
        "sample_count": int(len(usable)),
        "train_samples": int(len(train)),
        "test_samples": int(len(test)),
        "affects_main_model": False,
        "metrics": metrics,
        "baseline_metrics": baseline_metrics,
        "delta_metrics": {
            "delta_logloss": round(
                float(metrics["logloss"]) - float(baseline_metrics["logloss"]), 6
            ),
            "delta_brier": round(float(metrics["brier"]) - float(baseline_metrics["brier"]), 6),
            "delta_accuracy": round(
                float(metrics["accuracy"]) - float(baseline_metrics["accuracy"]), 6
            ),
        },
        "top_features": feature_importance[:5],
    }


def persist_catboost_shadow_report(*, report: Mapping[str, object], output_path: str | Path) -> str:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _coerce_frame(
    *,
    reference_frame: pd.DataFrame | None,
    records: Sequence[Mapping[str, object]] | None,
) -> pd.DataFrame:
    if reference_frame is not None:
        return reference_frame.copy()
    if records is not None:
        return pd.DataFrame(list(records))
    return pd.DataFrame()


def _resolve_label_series(*, working: pd.DataFrame, label_column: str) -> pd.Series:
    if label_column in working.columns:
        raw = pd.to_numeric(working[label_column], errors="coerce")
        return raw.apply(
            lambda value: 1.0 if float(value) >= 0.5 else 0.0 if pd.notna(value) else np.nan
        )
    if "target" in working.columns:
        raw = pd.to_numeric(working["target"], errors="coerce")
        return raw.apply(
            lambda value: 1.0 if float(value) >= 0.5 else 0.0 if pd.notna(value) else np.nan
        )
    return pd.Series([np.nan] * len(working), index=working.index, dtype=float)


def _infer_feature_columns(frame: pd.DataFrame) -> list[str]:
    result: list[str] = []
    for column in frame.columns:
        lowered = str(column).strip().lower()
        if lowered in _NON_FEATURE_COLUMNS:
            continue
        series = pd.to_numeric(frame[column], errors="coerce")
        if series.notna().sum() < 5:
            continue
        result.append(str(column))
    return result


def _fit_shadow_model(
    *,
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_test: np.ndarray,
    feature_names: Sequence[str],
    random_seed: int,
) -> tuple[np.ndarray, str, list[dict[str, object]]]:
    try:
        from catboost import CatBoostClassifier  # type: ignore[import-not-found]

        model = CatBoostClassifier(
            loss_function="Logloss",
            depth=4,
            learning_rate=0.05,
            iterations=120,
            random_seed=int(random_seed),
            verbose=False,
            allow_writing_files=False,
        )
        model.fit(x_train, y_train.astype(int))
        probabilities = np.asarray(model.predict_proba(x_test)[:, 1], dtype=float)
        raw_importance = model.get_feature_importance()
        importance = [
            {"feature": str(name), "importance": round(float(value), 6)}
            for name, value in sorted(
                zip(feature_names, cast(Sequence[float], raw_importance), strict=False),
                key=lambda item: abs(float(item[1])),
                reverse=True,
            )
        ]
        return _clip_probabilities(probabilities), "catboost_shadow", importance
    except Exception:
        model = LogisticProbModel(learning_rate=0.06, epochs=260, l2=8e-4, seed=int(random_seed))
        model.fit(x_train, y_train)
        probabilities = np.asarray(model.predict_proba(x_test), dtype=float)
        weights = np.asarray(
            model.weights if model.weights is not None else np.zeros(len(feature_names)),
            dtype=float,
        )
        importance = [
            {"feature": str(name), "importance": round(float(abs(value)), 6)}
            for name, value in sorted(
                zip(feature_names, weights, strict=False),
                key=lambda item: abs(float(item[1])),
                reverse=True,
            )
        ]
        return _clip_probabilities(probabilities), "fallback_logit_shadow", importance


def _resolve_baseline_probabilities(
    *,
    test: pd.DataFrame,
    baseline_probability_column: str,
    train_labels: np.ndarray,
) -> NDArray[np.float64]:
    if baseline_probability_column in test.columns:
        series = pd.to_numeric(test[baseline_probability_column], errors="coerce")
        if series.notna().sum() >= max(1, len(test) // 2):
            return _clip_probabilities(series.fillna(series.mean()).to_numpy(dtype=float))
    base_rate = float(np.mean(train_labels)) if len(train_labels) else 0.5
    baseline_probabilities: NDArray[np.float64] = np.asarray([base_rate] * len(test), dtype=float)
    return baseline_probabilities


def _classification_metrics(*, labels: np.ndarray, probabilities: np.ndarray) -> dict[str, float]:
    clipped = _clip_probabilities(probabilities)
    binary = (clipped >= 0.5).astype(float)
    accuracy = float(np.mean(binary == labels))
    brier = float(np.mean((clipped - labels) ** 2))
    losses = []
    for label, probability in zip(labels, clipped, strict=False):
        if int(label) == 1:
            losses.append(-np.log(max(float(probability), 1e-6)))
        else:
            losses.append(-np.log(max(float(1.0 - probability), 1e-6)))
    return {
        "accuracy": round(accuracy, 6),
        "brier": round(brier, 6),
        "logloss": round(float(np.mean(losses)) if losses else 0.0, 6),
    }


def _clip_probabilities(values: np.ndarray) -> NDArray[np.float64]:
    array: NDArray[np.float64] = np.asarray(values, dtype=float)
    clipped: NDArray[np.float64] = np.clip(array, 1e-6, 1.0 - 1e-6)
    return clipped
