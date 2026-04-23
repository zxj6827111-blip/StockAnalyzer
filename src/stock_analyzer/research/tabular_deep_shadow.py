"""Offline tabular deep-model shadow sidecar for TabNet / FT-Transformer style research."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np
import pandas as pd

from stock_analyzer.models.fallback import LogisticProbModel

_NON_FEATURE_COLUMNS = {
    "symbol",
    "date",
    "trade_date",
    "decision_time",
    "label_mature_time",
    "split_name",
    "ordinal",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "label",
    "target",
    "sample_weight",
    "data_quality_score",
    "maturity_status",
    "reconcile_status",
    "backfill_fidelity_tier",
    "backfill_source",
    "realized_return",
    "max_favorable_excursion",
    "max_adverse_excursion",
    "execution_fill_ratio",
    "realized_slippage_bp",
    "sim_vs_broker_diff",
    "baseline_scores",
    "recorded_model_outputs",
    "feature_vector",
    "score_breakdown",
    "risk_context",
    "regime_context",
    "p_lgbm",
    "p_xgb",
    "p_meta",
}

_DEFAULT_MODEL_FAMILIES = ("tabnet", "ft_transformer")


def run_tabular_deep_shadow(
    *,
    reference_frame: pd.DataFrame | None = None,
    records: Sequence[Mapping[str, object]] | None = None,
    feature_columns: Sequence[str] | None = None,
    label_column: str = "label",
    baseline_probability_column: str = "p_meta",
    test_ratio: float = 0.3,
    random_seed: int = 2026,
    model_families: Sequence[str] = _DEFAULT_MODEL_FAMILIES,
) -> dict[str, object]:
    frame = _coerce_frame(reference_frame=reference_frame, records=records)
    if frame.empty:
        return {
            "status": "empty",
            "engine": "tabular_deep_shadow_sidecar",
            "affects_main_model": False,
            "families": [],
            "recommended_family": "",
            "baseline_metrics": {},
        }

    working = _expand_embedded_feature_columns(frame.copy())
    if "trade_date" in working.columns:
        working["trade_date"] = pd.to_datetime(working["trade_date"], errors="coerce")
        working = working.sort_values(
            ["trade_date", "symbol"] if "symbol" in working.columns else ["trade_date"]
        )
    elif "date" in working.columns:
        working["date"] = pd.to_datetime(working["date"], errors="coerce")
        working = working.sort_values(["date", "symbol"] if "symbol" in working.columns else ["date"])

    label_series = _resolve_label_series(working=working, label_column=label_column)
    working = working.assign(_label=label_series).dropna(subset=["_label"])
    if working.empty or working["_label"].nunique() < 2:
        return {
            "status": "invalid_input",
            "engine": "tabular_deep_shadow_sidecar",
            "affects_main_model": False,
            "families": [],
            "recommended_family": "",
            "baseline_metrics": {},
        }

    feature_names = (
        list(feature_columns) if feature_columns is not None else _infer_feature_columns(working)
    )
    if not feature_names:
        return {
            "status": "invalid_input",
            "engine": "tabular_deep_shadow_sidecar",
            "affects_main_model": False,
            "families": [],
            "recommended_family": "",
            "baseline_metrics": {},
        }

    matrix = working.loc[:, feature_names].apply(pd.to_numeric, errors="coerce")
    matrix = matrix.fillna(matrix.median()).fillna(0.0)
    usable = working.assign(**{name: matrix[name] for name in feature_names})
    if len(usable) < 6:
        return {
            "status": "too_small",
            "engine": "tabular_deep_shadow_sidecar",
            "feature_count": len(feature_names),
            "sample_count": int(len(usable)),
            "affects_main_model": False,
            "families": [],
            "recommended_family": "",
            "baseline_metrics": {},
        }

    split_at = min(max(int(round(len(usable) * (1.0 - float(test_ratio)))), 4), len(usable) - 2)
    train = usable.iloc[:split_at].copy()
    test = usable.iloc[split_at:].copy()
    x_train = train.loc[:, feature_names].to_numpy(dtype=float)
    y_train = train["_label"].to_numpy(dtype=float)
    x_test = test.loc[:, feature_names].to_numpy(dtype=float)
    y_test = test["_label"].to_numpy(dtype=float)
    baseline_probabilities = _resolve_baseline_probabilities(
        test=test,
        baseline_probability_column=baseline_probability_column,
        train_labels=y_train,
    )

    family_reports: list[dict[str, object]] = []
    for family in _normalize_families(model_families):
        probabilities, backend, feature_importance = _fit_tabular_family(
            family=family,
            x_train=x_train,
            y_train=y_train,
            x_test=x_test,
            feature_names=feature_names,
            random_seed=random_seed,
        )
        family_reports.append(
            {
                "family": family,
                "backend": backend,
                "metrics": _classification_metrics(labels=y_test, probabilities=probabilities),
                "top_features": feature_importance[:5],
            }
        )

    family_reports.sort(
        key=lambda item: (
            float(_as_mapping(item.get("metrics")).get("logloss", 999.0)),
            -float(_as_mapping(item.get("metrics")).get("accuracy", 0.0)),
            str(item.get("family", "")),
        )
    )
    recommended = family_reports[0] if family_reports else {}
    return {
        "status": "ok" if family_reports else "invalid_input",
        "engine": "tabular_deep_shadow_sidecar",
        "feature_count": len(feature_names),
        "sample_count": int(len(usable)),
        "train_samples": int(len(train)),
        "test_samples": int(len(test)),
        "affects_main_model": False,
        "families": family_reports,
        "recommended_family": str(recommended.get("family", "")),
        "recommended_backend": str(recommended.get("backend", "")),
        "baseline_metrics": _classification_metrics(labels=y_test, probabilities=baseline_probabilities),
    }


def persist_tabular_deep_shadow_report(
    *, report: Mapping[str, object], output_path: str | Path
) -> str:
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


def _expand_embedded_feature_columns(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or "feature_vector" not in frame.columns:
        return frame
    expanded_rows: list[dict[str, object]] = []
    for value in frame["feature_vector"].tolist():
        mapping = value if isinstance(value, Mapping) else {}
        expanded_rows.append(
            {
                f"fv_{str(key).strip()}": nested_value
                for key, nested_value in mapping.items()
                if str(key).strip()
            }
        )
    if not any(expanded_rows):
        return frame
    expanded = pd.DataFrame(expanded_rows, index=frame.index)
    for column in expanded.columns:
        frame[column] = pd.to_numeric(expanded[column], errors="coerce")
    return frame


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


def _normalize_families(model_families: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in model_families:
        family = str(item).strip().lower()
        if not family or family in seen:
            continue
        seen.add(family)
        normalized.append(family)
    return normalized or list(_DEFAULT_MODEL_FAMILIES)


def _fit_tabular_family(
    *,
    family: str,
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_test: np.ndarray,
    feature_names: Sequence[str],
    random_seed: int,
) -> tuple[np.ndarray, str, list[dict[str, object]]]:
    model = LogisticProbModel(
        learning_rate=0.05 if family == "tabnet" else 0.035,
        epochs=320 if family == "tabnet" else 420,
        l2=8e-4 if family == "tabnet" else 1.2e-3,
        seed=int(random_seed) + (11 if family == "tabnet" else 23),
    )
    model.fit(x_train, y_train)
    probabilities = np.asarray(model.predict_proba(x_test), dtype=float)
    weights = np.asarray(
        model.weights if model.weights is not None else np.zeros(len(feature_names)),
        dtype=float,
    )
    feature_importance = [
        {"feature": str(name), "importance": round(float(abs(value)), 6)}
        for name, value in sorted(
            zip(feature_names, weights, strict=False),
            key=lambda item: abs(float(item[1])),
            reverse=True,
        )
    ]
    backend = f"{family}_fallback_logit"
    return _clip_probabilities(probabilities), backend, feature_importance


def _resolve_baseline_probabilities(
    *,
    test: pd.DataFrame,
    baseline_probability_column: str,
    train_labels: np.ndarray,
) -> np.ndarray:
    if baseline_probability_column in test.columns:
        series = pd.to_numeric(test[baseline_probability_column], errors="coerce")
        if series.notna().sum() >= max(1, len(test) // 2):
            return _clip_probabilities(series.fillna(series.mean()).to_numpy(dtype=float))
    base_rate = float(np.mean(train_labels)) if len(train_labels) else 0.5
    return np.asarray([base_rate] * len(test), dtype=float)


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


def _clip_probabilities(values: np.ndarray) -> np.ndarray:
    return np.clip(np.asarray(values, dtype=float), 1e-6, 1.0 - 1e-6)


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    return {}
