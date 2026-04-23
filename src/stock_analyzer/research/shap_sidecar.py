"""Offline SHAP-style explainability sidecar."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pandas as pd

_NON_FEATURE_COLUMNS = {
    "symbol",
    "date",
    "trade_date",
    "label",
    "prediction",
    "score",
    "p_lgbm",
    "p_xgb",
    "p_meta",
}


def run_shap_sidecar(
    *,
    reference_frame: pd.DataFrame,
    sample_frame: pd.DataFrame | None = None,
    prediction_column: str = "p_meta",
    baseline_importance: Mapping[str, object] | None = None,
    drift_threshold: float = 0.25,
    top_k: int = 5,
) -> dict[str, object]:
    if reference_frame.empty:
        return {
            "status": "empty",
            "engine": "shap_proxy_fallback",
            "global_importance": [],
            "sample_explanations": [],
            "drift_ratio": 0.0,
            "drift_flag": False,
        }

    working = reference_frame.copy()
    feature_columns = _infer_feature_columns(working, prediction_column=prediction_column)
    if not feature_columns:
        return {
            "status": "no_features",
            "engine": "shap_proxy_fallback",
            "global_importance": [],
            "sample_explanations": [],
            "drift_ratio": 0.0,
            "drift_flag": False,
        }

    prediction = _resolve_prediction_series(working, prediction_column=prediction_column)
    centered = working[feature_columns].apply(pd.to_numeric, errors="coerce")
    reference_mean = centered.mean(axis=0)
    reference_std = centered.std(axis=0, ddof=0).replace(0.0, 1.0).fillna(1.0)
    normalized = centered.sub(reference_mean, axis=1).div(reference_std, axis=1)
    proxy_weights = _proxy_feature_weights(
        features=centered,
        prediction=prediction,
    )
    contributions = normalized.mul(pd.Series(proxy_weights), axis=1).fillna(0.0)
    global_importance = contributions.abs().mean(axis=0).sort_values(ascending=False)
    importance_items = [
        {"feature": str(name), "importance": round(float(value), 6)}
        for name, value in global_importance.items()
    ]

    active_samples = (
        sample_frame.copy()
        if sample_frame is not None
        else working.tail(min(5, len(working))).copy()
    )
    explanations: list[dict[str, object]] = []
    for _, row in active_samples.iterrows():
        local = {}
        for feature in feature_columns:
            raw_value = pd.to_numeric(pd.Series([row.get(feature)]), errors="coerce").iloc[0]
            if pd.isna(raw_value):
                continue
            z_score = (float(raw_value) - float(reference_mean.get(feature, 0.0))) / float(
                reference_std.get(feature, 1.0)
            )
            local[feature] = z_score * float(proxy_weights.get(feature, 0.0))
        ordered = sorted(local.items(), key=lambda item: abs(item[1]), reverse=True)
        explanations.append(
            {
                "symbol": str(row.get("symbol", "")).strip(),
                "top_positive": [
                    {"feature": name, "contribution": round(value, 6)}
                    for name, value in ordered
                    if value > 0
                ][: max(1, int(top_k))],
                "top_negative": [
                    {"feature": name, "contribution": round(value, 6)}
                    for name, value in ordered
                    if value < 0
                ][: max(1, int(top_k))],
            }
        )

    current_importance: dict[str, object] = {}
    for item in importance_items:
        feature_name = item.get("feature")
        importance = item.get("importance")
        if isinstance(feature_name, str):
            current_importance[feature_name] = importance
    drift_ratio = _importance_drift_ratio(
        current=current_importance,
        baseline=baseline_importance or {},
    )
    return {
        "status": "ok",
        "engine": "shap_proxy_fallback",
        "feature_count": len(feature_columns),
        "sample_count": int(len(working)),
        "global_importance": importance_items,
        "sample_explanations": explanations,
        "drift_ratio": round(drift_ratio, 6),
        "drift_threshold": float(drift_threshold),
        "drift_flag": drift_ratio > float(drift_threshold),
    }


def persist_shap_sidecar_report(*, report: Mapping[str, object], output_path: str | Path) -> str:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _infer_feature_columns(frame: pd.DataFrame, *, prediction_column: str) -> list[str]:
    result: list[str] = []
    for column in frame.columns:
        lowered = str(column).strip().lower()
        if lowered in _NON_FEATURE_COLUMNS or str(column) == prediction_column:
            continue
        series = pd.to_numeric(frame[column], errors="coerce")
        if series.notna().sum() < 3:
            continue
        result.append(str(column))
    return result


def _resolve_prediction_series(frame: pd.DataFrame, *, prediction_column: str) -> pd.Series:
    if prediction_column in frame.columns:
        series = pd.to_numeric(frame[prediction_column], errors="coerce")
        if series.notna().sum() >= 3:
            return series.fillna(series.mean())
    if "label" in frame.columns:
        series = pd.to_numeric(frame["label"], errors="coerce")
        if series.notna().sum() >= 3:
            return series.fillna(series.mean())
    return pd.Series([0.5] * len(frame), index=frame.index, dtype=float)


def _proxy_feature_weights(*, features: pd.DataFrame, prediction: pd.Series) -> dict[str, float]:
    weights: dict[str, float] = {}
    for column in features.columns:
        series = pd.to_numeric(features[column], errors="coerce")
        aligned = pd.concat([series.rename("x"), prediction.rename("y")], axis=1).dropna()
        if len(aligned) < 3:
            weights[str(column)] = 0.0
            continue
        corr = float(aligned["x"].corr(aligned["y"]))
        weights[str(column)] = abs(corr) if corr == corr else 0.0
    if not any(value > 0 for value in weights.values()):
        return {key: 1.0 for key in weights}
    return weights


def _importance_drift_ratio(
    *, current: Mapping[str, object], baseline: Mapping[str, object]
) -> float:
    current_map = _normalize_importance_map(current)
    baseline_map = _normalize_importance_map(baseline)
    if not current_map or not baseline_map:
        return 0.0
    features = sorted(set(current_map) | set(baseline_map))
    total = 0.0
    for feature in features:
        total += abs(current_map.get(feature, 0.0) - baseline_map.get(feature, 0.0))
    return min(1.0, total / 2.0)


def _normalize_importance_map(raw: Mapping[str, object]) -> dict[str, float]:
    values = {
        str(key): float(value)
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, (int, float))
    }
    total = sum(max(0.0, value) for value in values.values())
    if total <= 0:
        return {}
    return {key: max(0.0, value) / total for key, value in values.items()}
