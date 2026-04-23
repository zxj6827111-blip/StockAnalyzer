"""Offline Temporal Fusion Transformer style forecasting sidecar."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np
import pandas as pd


def run_tft_sidecar(
    *,
    records: Sequence[Mapping[str, object]],
    horizon: int = 1,
    encoder_length: int = 5,
    train_ratio: float = 0.7,
) -> dict[str, object]:
    frame = _normalize_frame(records=records)
    if frame.empty:
        return {
            "status": "empty",
            "engine": "tft_sidecar",
            "backend": "sequence_regression_fallback",
            "affects_runtime": False,
            "sample_count": 0,
            "metrics": {},
            "baseline_metrics": {},
        }

    samples = _build_sequence_samples(
        frame=frame,
        horizon=max(1, int(horizon)),
        encoder_length=max(2, int(encoder_length)),
    )
    if samples.empty:
        return {
            "status": "too_small",
            "engine": "tft_sidecar",
            "backend": "sequence_regression_fallback",
            "affects_runtime": False,
            "sample_count": 0,
            "metrics": {},
            "baseline_metrics": {},
        }

    split_at = min(max(int(round(len(samples) * float(train_ratio))), 4), len(samples) - 2)
    train = samples.iloc[:split_at].copy()
    test = samples.iloc[split_at:].copy()
    feature_columns = [column for column in samples.columns if column.startswith("lag_")]
    x_train = train.loc[:, feature_columns].to_numpy(dtype=float)
    y_train = train["target_return"].to_numpy(dtype=float)
    x_test = test.loc[:, feature_columns].to_numpy(dtype=float)
    y_test = test["target_return"].to_numpy(dtype=float)

    weights = _fit_ridge_regression(x_train=x_train, y_train=y_train, l2=1e-2)
    predictions = _predict_ridge(x=x_test, weights=weights)
    baseline_predictions = np.zeros(len(test), dtype=float)

    return {
        "status": "ok",
        "engine": "tft_sidecar",
        "backend": "sequence_regression_fallback",
        "affects_runtime": False,
        "sample_count": int(len(samples)),
        "train_samples": int(len(train)),
        "test_samples": int(len(test)),
        "encoder_length": int(encoder_length),
        "horizon": int(horizon),
        "metrics": _forecast_metrics(actual=y_test, predicted=predictions),
        "baseline_metrics": _forecast_metrics(actual=y_test, predicted=baseline_predictions),
        "symbols": sorted(frame["symbol"].astype(str).unique().tolist()),
    }


def persist_tft_sidecar_report(*, report: Mapping[str, object], output_path: str | Path) -> str:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _normalize_frame(*, records: Sequence[Mapping[str, object]]) -> pd.DataFrame:
    frame = pd.DataFrame(list(records))
    if frame.empty:
        return frame
    symbol_column = "symbol" if "symbol" in frame.columns else ""
    date_column = "trade_date" if "trade_date" in frame.columns else ("date" if "date" in frame.columns else "")
    if not symbol_column or not date_column or "close" not in frame.columns:
        return pd.DataFrame()
    working = frame.copy()
    working["symbol"] = working[symbol_column].astype(str).str.strip()
    working["trade_date"] = pd.to_datetime(working[date_column], errors="coerce")
    working["close"] = pd.to_numeric(working["close"], errors="coerce")
    working["volume"] = pd.to_numeric(working.get("volume", 0.0), errors="coerce").fillna(0.0)
    working = working.dropna(subset=["symbol", "trade_date", "close"])
    working = working[working["symbol"] != ""]
    return working.sort_values(["symbol", "trade_date"]).reset_index(drop=True)


def _build_sequence_samples(
    *,
    frame: pd.DataFrame,
    horizon: int,
    encoder_length: int,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for symbol, group in frame.groupby("symbol", sort=True):
        ordered = group.sort_values("trade_date").reset_index(drop=True)
        closes = ordered["close"].to_numpy(dtype=float)
        volumes = ordered["volume"].to_numpy(dtype=float)
        for idx in range(encoder_length, len(ordered) - horizon + 1):
            window_closes = closes[idx - encoder_length : idx]
            window_volumes = volumes[idx - encoder_length : idx]
            current_close = float(window_closes[-1])
            future_close = float(closes[idx + horizon - 1])
            row = {
                "symbol": symbol,
                "trade_date": ordered.loc[idx - 1, "trade_date"],
                "target_return": (future_close / max(current_close, 1e-6)) - 1.0,
            }
            for lag in range(encoder_length):
                prev_close = float(window_closes[max(0, lag - 1)]) if lag > 0 else float(window_closes[0])
                row[f"lag_close_{lag + 1}"] = float(window_closes[lag] / max(prev_close, 1e-6) - 1.0)
                row[f"lag_volume_{lag + 1}"] = float(window_volumes[lag])
            rows.append(row)
    return pd.DataFrame(rows)


def _fit_ridge_regression(*, x_train: np.ndarray, y_train: np.ndarray, l2: float) -> np.ndarray:
    features = np.c_[np.ones(len(x_train)), x_train]
    identity = np.eye(features.shape[1], dtype=float)
    identity[0, 0] = 0.0
    lhs = features.T @ features + float(l2) * identity
    rhs = features.T @ y_train
    return np.linalg.pinv(lhs) @ rhs


def _predict_ridge(*, x: np.ndarray, weights: np.ndarray) -> np.ndarray:
    features = np.c_[np.ones(len(x)), x]
    return np.asarray(features @ weights, dtype=float)


def _forecast_metrics(*, actual: np.ndarray, predicted: np.ndarray) -> dict[str, float]:
    errors = np.asarray(predicted, dtype=float) - np.asarray(actual, dtype=float)
    mae = float(np.mean(np.abs(errors)))
    rmse = float(np.sqrt(np.mean(errors**2)))
    direction = float(
        np.mean((np.asarray(predicted, dtype=float) >= 0.0) == (np.asarray(actual, dtype=float) >= 0.0))
    )
    return {
        "mae": round(mae, 6),
        "rmse": round(rmse, 6),
        "directional_accuracy": round(direction, 6),
    }
