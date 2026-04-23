"""Offline FinRL-style policy research sidecar."""

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
    "p_lgbm",
    "p_xgb",
    "p_meta",
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
}


def run_finrl_sidecar(
    *,
    reference_frame: pd.DataFrame | None = None,
    records: Sequence[Mapping[str, object]] | None = None,
    feature_columns: Sequence[str] | None = None,
    reward_column: str = "realized_return",
    baseline_probability_column: str = "p_meta",
    test_ratio: float = 0.3,
    random_seed: int = 2026,
    action_threshold: float = 0.55,
) -> dict[str, object]:
    frame = _coerce_frame(reference_frame=reference_frame, records=records)
    if frame.empty:
        return {
            "status": "empty",
            "engine": "finrl_sidecar",
            "backend": "policy_fallback",
            "affects_runtime": False,
            "sample_count": 0,
            "policy_metrics": {},
            "baseline_metrics": {},
        }

    working = _expand_embedded_feature_columns(frame.copy())
    reward_series = pd.to_numeric(working.get(reward_column), errors="coerce")
    working = working.assign(_reward=reward_series).dropna(subset=["_reward"])
    if working.empty:
        return {
            "status": "invalid_input",
            "engine": "finrl_sidecar",
            "backend": "policy_fallback",
            "affects_runtime": False,
            "sample_count": 0,
            "policy_metrics": {},
            "baseline_metrics": {},
        }

    labels = (working["_reward"] > 0.0).astype(float)
    feature_names = (
        list(feature_columns) if feature_columns is not None else _infer_feature_columns(working)
    )
    if not feature_names or labels.nunique() < 2:
        return {
            "status": "invalid_input",
            "engine": "finrl_sidecar",
            "backend": "policy_fallback",
            "affects_runtime": False,
            "sample_count": int(len(working)),
            "policy_metrics": {},
            "baseline_metrics": {},
        }

    matrix = working.loc[:, feature_names].apply(pd.to_numeric, errors="coerce")
    matrix = matrix.fillna(matrix.median()).fillna(0.0)
    usable = working.assign(**{name: matrix[name] for name in feature_names}, _label=labels)
    if len(usable) < 6:
        return {
            "status": "too_small",
            "engine": "finrl_sidecar",
            "backend": "policy_fallback",
            "affects_runtime": False,
            "sample_count": int(len(usable)),
            "policy_metrics": {},
            "baseline_metrics": {},
        }

    split_at = min(max(int(round(len(usable) * (1.0 - float(test_ratio)))), 4), len(usable) - 2)
    train = usable.iloc[:split_at].copy()
    test = usable.iloc[split_at:].copy()
    x_train = train.loc[:, feature_names].to_numpy(dtype=float)
    y_train = train["_label"].to_numpy(dtype=float)
    x_test = test.loc[:, feature_names].to_numpy(dtype=float)
    rewards_test = test["_reward"].to_numpy(dtype=float)
    baseline_probabilities = _resolve_baseline_probabilities(
        test=test,
        baseline_probability_column=baseline_probability_column,
        train_labels=y_train,
    )

    model = LogisticProbModel(learning_rate=0.04, epochs=360, l2=1e-3, seed=int(random_seed))
    model.fit(x_train, y_train)
    policy_probabilities = np.asarray(model.predict_proba(x_test), dtype=float)
    policy_actions = (policy_probabilities >= float(action_threshold)).astype(float)
    baseline_actions = (baseline_probabilities >= float(action_threshold)).astype(float)
    policy_rewards = rewards_test * policy_actions
    baseline_rewards = rewards_test * baseline_actions
    weights = np.asarray(
        model.weights if model.weights is not None else np.zeros(len(feature_names)),
        dtype=float,
    )
    top_features = [
        {"feature": str(name), "importance": round(float(abs(value)), 6)}
        for name, value in sorted(
            zip(feature_names, weights, strict=False),
            key=lambda item: abs(float(item[1])),
            reverse=True,
        )
    ]

    return {
        "status": "ok",
        "engine": "finrl_sidecar",
        "backend": "policy_fallback_logit",
        "affects_runtime": False,
        "sample_count": int(len(usable)),
        "train_samples": int(len(train)),
        "test_samples": int(len(test)),
        "action_threshold": float(action_threshold),
        "policy_metrics": _policy_metrics(rewards=policy_rewards, actions=policy_actions),
        "baseline_metrics": _policy_metrics(rewards=baseline_rewards, actions=baseline_actions),
        "top_features": top_features[:5],
    }


def persist_finrl_sidecar_report(*, report: Mapping[str, object], output_path: str | Path) -> str:
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


def _resolve_baseline_probabilities(
    *,
    test: pd.DataFrame,
    baseline_probability_column: str,
    train_labels: np.ndarray,
) -> np.ndarray:
    if baseline_probability_column in test.columns:
        series = pd.to_numeric(test[baseline_probability_column], errors="coerce")
        if series.notna().sum() >= max(1, len(test) // 2):
            return np.clip(series.fillna(series.mean()).to_numpy(dtype=float), 1e-6, 1.0 - 1e-6)
    base_rate = float(np.mean(train_labels)) if len(train_labels) else 0.5
    return np.asarray([base_rate] * len(test), dtype=float)


def _policy_metrics(*, rewards: np.ndarray, actions: np.ndarray) -> dict[str, float]:
    mean_reward = float(np.mean(rewards)) if len(rewards) else 0.0
    action_rate = float(np.mean(actions)) if len(actions) else 0.0
    std_reward = float(np.std(rewards)) if len(rewards) else 0.0
    sharpe_proxy = mean_reward / std_reward if std_reward > 1e-9 else 0.0
    return {
        "mean_reward": round(mean_reward, 6),
        "cumulative_reward": round(float(np.sum(rewards)), 6),
        "action_rate": round(action_rate, 6),
        "positive_reward_ratio": round(float(np.mean(rewards > 0.0)) if len(rewards) else 0.0, 6),
        "sharpe_proxy": round(sharpe_proxy, 6),
    }
