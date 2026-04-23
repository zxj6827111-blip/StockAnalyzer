"""Offline heavy end-to-end temporal model research sidecar."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np
import pandas as pd

from stock_analyzer.models.fallback import LogisticProbModel


def run_heavy_ts_shadow(
    *,
    records: Sequence[Mapping[str, object]],
    horizon: int = 3,
    lookback: int = 8,
    test_ratio: float = 0.3,
    random_seed: int = 2026,
) -> dict[str, object]:
    frame = _normalize_frame(records=records)
    if frame.empty:
        return {
            "status": "empty",
            "engine": "heavy_ts_shadow_sidecar",
            "backend": "sequence_logit_fallback",
            "affects_runtime": False,
            "sample_count": 0,
            "metrics": {},
            "baseline_metrics": {},
        }

    samples = _build_temporal_samples(
        frame=frame,
        horizon=max(1, int(horizon)),
        lookback=max(3, int(lookback)),
    )
    if samples.empty or samples["_label"].nunique() < 2:
        return {
            "status": "too_small",
            "engine": "heavy_ts_shadow_sidecar",
            "backend": "sequence_logit_fallback",
            "affects_runtime": False,
            "sample_count": 0,
            "metrics": {},
            "baseline_metrics": {},
        }

    feature_columns = [column for column in samples.columns if column.startswith("seq_")]
    split_at = min(max(int(round(len(samples) * (1.0 - float(test_ratio)))), 4), len(samples) - 2)
    train = samples.iloc[:split_at].copy()
    test = samples.iloc[split_at:].copy()
    x_train = train.loc[:, feature_columns].to_numpy(dtype=float)
    y_train = train["_label"].to_numpy(dtype=float)
    x_test = test.loc[:, feature_columns].to_numpy(dtype=float)
    y_test = test["_label"].to_numpy(dtype=float)
    baseline_probabilities = np.clip(test["baseline_probability"].to_numpy(dtype=float), 1e-6, 1.0 - 1e-6)

    model = LogisticProbModel(learning_rate=0.035, epochs=420, l2=1.2e-3, seed=int(random_seed))
    model.fit(x_train, y_train)
    probabilities = np.asarray(model.predict_proba(x_test), dtype=float)
    weights = np.asarray(
        model.weights if model.weights is not None else np.zeros(len(feature_columns)),
        dtype=float,
    )
    top_features = [
        {"feature": str(name), "importance": round(float(abs(value)), 6)}
        for name, value in sorted(
            zip(feature_columns, weights, strict=False),
            key=lambda item: abs(float(item[1])),
            reverse=True,
        )
    ]
    return {
        "status": "ok",
        "engine": "heavy_ts_shadow_sidecar",
        "backend": "sequence_logit_fallback",
        "affects_runtime": False,
        "sample_count": int(len(samples)),
        "train_samples": int(len(train)),
        "test_samples": int(len(test)),
        "lookback": int(lookback),
        "horizon": int(horizon),
        "labeling_mode": str(samples["labeling_mode"].iloc[0]) if "labeling_mode" in samples.columns else "future_return_sign",
        "metrics": _classification_metrics(labels=y_test, probabilities=probabilities),
        "baseline_metrics": _classification_metrics(labels=y_test, probabilities=baseline_probabilities),
        "top_features": top_features[:8],
    }


def persist_heavy_ts_shadow_report(
    *, report: Mapping[str, object], output_path: str | Path
) -> str:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _normalize_frame(*, records: Sequence[Mapping[str, object]]) -> pd.DataFrame:
    frame = pd.DataFrame(list(records))
    if frame.empty:
        return frame
    if "symbol" not in frame.columns or "close" not in frame.columns:
        return pd.DataFrame()
    date_column = "trade_date" if "trade_date" in frame.columns else ("date" if "date" in frame.columns else "")
    if not date_column:
        return pd.DataFrame()
    working = frame.copy()
    working["symbol"] = working["symbol"].astype(str).str.strip()
    working["trade_date"] = pd.to_datetime(working[date_column], errors="coerce")
    for column in ("open", "high", "low", "close", "volume", "p_meta"):
        if column in working.columns:
            working[column] = pd.to_numeric(working[column], errors="coerce")
    if "volume" not in working.columns:
        working["volume"] = 0.0
    if "p_meta" not in working.columns:
        working["p_meta"] = 0.5
    working = working.dropna(subset=["symbol", "trade_date", "close"])
    working = working[working["symbol"] != ""]
    return working.sort_values(["symbol", "trade_date"]).reset_index(drop=True)


def _build_temporal_samples(*, frame: pd.DataFrame, horizon: int, lookback: int) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for symbol, group in frame.groupby("symbol", sort=True):
        ordered = group.sort_values("trade_date").reset_index(drop=True)
        closes = ordered["close"].to_numpy(dtype=float)
        volumes = ordered["volume"].to_numpy(dtype=float)
        baseline = ordered["p_meta"].to_numpy(dtype=float)
        for idx in range(lookback, len(ordered) - horizon + 1):
            future_close = float(closes[idx + horizon - 1])
            current_close = float(closes[idx - 1])
            future_return = future_close / max(current_close, 1e-6) - 1.0
            row = {
                "symbol": symbol,
                "trade_date": ordered.loc[idx - 1, "trade_date"],
                "_label": 1.0 if future_return >= 0.0 else 0.0,
                "baseline_probability": float(baseline[idx - 1]),
                "future_return": future_return,
            }
            close_window = closes[idx - lookback : idx]
            volume_window = volumes[idx - lookback : idx]
            for offset in range(lookback):
                prev_close = float(close_window[offset - 1]) if offset > 0 else float(close_window[0])
                row[f"seq_return_{offset + 1}"] = float(close_window[offset] / max(prev_close, 1e-6) - 1.0)
                row[f"seq_volume_{offset + 1}"] = float(volume_window[offset])
            rows.append(row)
    samples = pd.DataFrame(rows)
    if samples.empty:
        return samples
    samples["labeling_mode"] = "future_return_sign"
    if samples["_label"].nunique() < 2:
        future_returns = pd.to_numeric(samples["future_return"], errors="coerce")
        if future_returns.nunique(dropna=True) >= 2:
            threshold = float(future_returns.median())
            fallback_labels = (future_returns >= threshold).astype(float)
            if fallback_labels.nunique() >= 2:
                samples["_label"] = fallback_labels
                samples["labeling_mode"] = "future_return_median"
    return samples


def _classification_metrics(*, labels: np.ndarray, probabilities: np.ndarray) -> dict[str, float]:
    clipped = np.clip(np.asarray(probabilities, dtype=float), 1e-6, 1.0 - 1e-6)
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
