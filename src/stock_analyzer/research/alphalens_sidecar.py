"""Offline Alphalens-style factor diagnostics."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pandas as pd

_BASE_FIELDS = {
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
    "p_lgbm",
    "p_xgb",
    "p_meta",
}


def run_alphalens_sidecar(
    *,
    records: Sequence[Mapping[str, object]],
    factor_columns: Sequence[str] | None = None,
    horizons: Sequence[int] = (1, 5, 10),
    quantiles: int = 5,
) -> dict[str, object]:
    frame = pd.DataFrame(list(records))
    if frame.empty:
        return {
            "status": "empty",
            "engine": "alphalens_fallback",
            "records": 0,
            "factor_count": 0,
            "horizons": list(horizons),
            "factors": [],
        }

    symbol_col = "symbol"
    date_col = (
        "trade_date"
        if "trade_date" in frame.columns
        else ("date" if "date" in frame.columns else "")
    )
    if not date_col or "close" not in frame.columns or symbol_col not in frame.columns:
        return {
            "status": "invalid_input",
            "engine": "alphalens_fallback",
            "records": int(len(frame)),
            "factor_count": 0,
            "horizons": list(horizons),
            "factors": [],
        }

    working = frame.copy()
    working[date_col] = pd.to_datetime(working[date_col], errors="coerce")
    working["close"] = pd.to_numeric(working["close"], errors="coerce")
    working[symbol_col] = working[symbol_col].astype(str).str.strip()
    working = working.dropna(subset=[date_col, "close"])
    working = working[working[symbol_col] != ""]
    if working.empty:
        return {
            "status": "invalid_input",
            "engine": "alphalens_fallback",
            "records": 0,
            "factor_count": 0,
            "horizons": list(horizons),
            "factors": [],
        }

    factor_names = (
        list(factor_columns) if factor_columns is not None else _infer_factor_columns(working)
    )
    cleaned_horizons = [max(1, int(item)) for item in horizons]
    factor_items: list[dict[str, object]] = []
    for factor_name in factor_names:
        if factor_name not in working.columns:
            continue
        factor_series = pd.to_numeric(working[factor_name], errors="coerce")
        usable = working.assign(_factor=factor_series).dropna(subset=["_factor"])
        if usable.empty:
            continue
        horizon_metrics: list[dict[str, object]] = []
        for horizon in cleaned_horizons:
            sample = usable.copy()
            sample[f"_ret_{horizon}"] = (
                sample.groupby(symbol_col)["close"].shift(-horizon) / sample["close"] - 1.0
            )
            sample = sample.dropna(subset=[f"_ret_{horizon}"])
            if sample.empty:
                continue
            ic_series = _cross_sectional_rank_ic(
                factor=sample["_factor"],
                returns=sample[f"_ret_{horizon}"],
                dates=sample[date_col],
            )
            ic_summary = _ic_summary(ic_series)
            spread = _quantile_spread(
                sample["_factor"],
                sample[f"_ret_{horizon}"],
                quantiles=max(3, int(quantiles)),
            )
            horizon_metrics.append(
                {
                    "horizon": horizon,
                    "samples": int(len(sample)),
                    "rank_ic": round(ic_summary["mean"], 6),
                    "rank_ic_std": round(ic_summary["std"], 6),
                    "icir": round(ic_summary["icir"], 6),
                    "cross_sectional_days": ic_summary["days"],
                    "quantile_return_spread": round(spread, 6),
                }
            )
        if not horizon_metrics:
            continue
        best_metric = max(horizon_metrics, key=lambda item: abs(_as_float(item.get("rank_ic"))))
        max_abs_ic = max(abs(_as_float(item.get("rank_ic"))) for item in horizon_metrics)
        factor_items.append(
            {
                "factor": factor_name,
                "best_horizon": _as_int(best_metric.get("horizon")),
                "max_abs_rank_ic": round(max_abs_ic, 6),
                "health": "weak"
                if max_abs_ic < 0.02
                else ("watch" if max_abs_ic < 0.05 else "healthy"),
                "horizons": horizon_metrics,
            }
        )

    factor_items.sort(
        key=lambda item: (-abs(_as_float(item.get("max_abs_rank_ic"))), str(item["factor"]))
    )
    return {
        "status": "ok" if factor_items else "no_factors",
        "engine": "alphalens_fallback",
        "records": int(len(working)),
        "factor_count": int(len(factor_items)),
        "horizons": cleaned_horizons,
        "factors": factor_items,
    }


def persist_alphalens_sidecar_report(
    *, report: Mapping[str, object], output_path: str | Path
) -> str:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _infer_factor_columns(frame: pd.DataFrame) -> list[str]:
    inferred: list[str] = []
    for column in frame.columns:
        if str(column).strip().lower() in _BASE_FIELDS:
            continue
        series = pd.to_numeric(frame[column], errors="coerce")
        if series.notna().sum() < 3:
            continue
        inferred.append(str(column))
    return inferred


def _rank_ic(left: pd.Series, right: pd.Series) -> float:
    ranked_left = left.rank(method="average")
    ranked_right = right.rank(method="average")
    corr = ranked_left.corr(ranked_right)
    return float(corr) if corr == corr else 0.0


def _cross_sectional_rank_ic(
    *,
    factor: pd.Series,
    returns: pd.Series,
    dates: pd.Series,
) -> pd.Series:
    """Per-trading-date cross-sectional RankIC series.

    The previous implementation pooled every row into one Spearman across all
    dates, which is not how factor IC is evaluated.  This computes one RankIC
    per trading date (factor vs forward return within that date's
    cross-section) so callers can see the IC time series and its stability.
    Dates with fewer than two rows are dropped: a single-row cross-section has
    no rank correlation.
    """
    frame = pd.DataFrame({"date": dates, "factor": factor, "ret": returns}).dropna()
    if frame.empty:
        return pd.Series(dtype=float)
    ics: dict[object, float] = {}
    for trading_date, group in frame.groupby("date"):
        if len(group) < 2:
            continue
        ics[trading_date] = _rank_ic(group["factor"], group["ret"])
    return pd.Series(ics, dtype=float)


def _ic_summary(ic_series: pd.Series) -> dict[str, float]:
    if ic_series.empty:
        return {"mean": 0.0, "std": 0.0, "icir": 0.0, "days": 0.0}
    mean = float(ic_series.mean())
    std = float(ic_series.std(ddof=1)) if len(ic_series) > 1 else 0.0
    icir = mean / std if std > 1e-9 else 0.0
    return {"mean": mean, "std": std, "icir": icir, "days": float(len(ic_series))}


def _quantile_spread(factor: pd.Series, returns: pd.Series, quantiles: int) -> float:
    if factor.nunique(dropna=True) < 2:
        return 0.0
    ranked = factor.rank(method="first")
    try:
        buckets = pd.qcut(
            ranked,
            q=min(quantiles, max(2, int(ranked.notna().sum()))),
            labels=False,
            duplicates="drop",
        )
    except ValueError:
        return 0.0
    sample = pd.DataFrame({"bucket": buckets, "ret": returns}).dropna()
    if sample.empty or sample["bucket"].nunique() < 2:
        return 0.0
    grouped = sample.groupby("bucket")["ret"].mean()
    return float(grouped.iloc[-1] - grouped.iloc[0])


def _as_float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        try:
            return float(text)
        except ValueError:
            return 0.0
    return 0.0


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        try:
            return int(float(text))
        except ValueError:
            return 0
    return 0
