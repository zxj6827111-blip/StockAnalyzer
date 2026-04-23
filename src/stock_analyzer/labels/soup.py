"""Soup strategy label alignment: TP-before-SL within fixed horizon."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pandas as pd


def build_soup_labels(
    bars: pd.DataFrame,
    take_profit_pct: float = 0.05,
    stop_loss_pct: float = 0.05,
    horizon_days: int = 5,
    price_basis: str = "close",
    exclude_untradable: bool = True,
    conflict_policy: str = "conservative_zero",
    conflict_soft_label_value: float = 0.5,
) -> pd.Series:
    """Return binary labels where TP is hit before SL in next N trading days.

    Label definition:
    - 1: future path reaches TP first.
    - 0: future path reaches SL first or neither is hit.
    - NaN: insufficient future data at tail.
    """
    required = {"high", "low", "close"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"missing required columns for labels: {sorted(missing)}")
    if horizon_days <= 0:
        raise ValueError("horizon_days must be positive")

    ordered = bars.sort_index()
    close = ordered["close"].to_numpy(dtype=float)
    high = ordered["high"].to_numpy(dtype=float)
    low = ordered["low"].to_numpy(dtype=float)
    basis = price_basis.strip().lower()

    labels: npt.NDArray[np.float64] = np.full(close.shape[0], np.nan, dtype=float)
    for idx in range(close.shape[0]):
        entry_idx, entry = _resolve_entry_price(
            bars=ordered,
            close=close,
            start_idx=idx,
            horizon_days=horizon_days,
            basis=basis,
            exclude_untradable=exclude_untradable,
        )
        if entry_idx < 0 or entry <= 0:
            continue

        last_future = entry_idx + horizon_days
        if last_future > close.shape[0]:
            continue
        tp_price = entry * (1.0 + take_profit_pct)
        sl_price = entry * (1.0 - stop_loss_pct)

        outcome = 0.0
        for future_idx in range(entry_idx, entry_idx + horizon_days):
            hit_tp = high[future_idx] >= tp_price
            hit_sl = low[future_idx] <= sl_price
            if hit_tp and hit_sl:
                outcome = _resolve_same_bar_conflict(
                    bar=ordered.iloc[future_idx],
                    entry_price=entry,
                    take_profit_price=tp_price,
                    stop_loss_price=sl_price,
                    policy=conflict_policy,
                    soft_label_value=conflict_soft_label_value,
                )
                break
            if hit_tp:
                outcome = 1.0
                break
            if hit_sl:
                outcome = 0.0
                break
        labels[idx] = outcome

    return pd.Series(labels, index=ordered.index, name="label_soup_tp_before_sl")


def detect_soup_label_same_bar_conflicts(
    bars: pd.DataFrame,
    take_profit_pct: float = 0.05,
    stop_loss_pct: float = 0.05,
    horizon_days: int = 5,
    price_basis: str = "close",
    exclude_untradable: bool = True,
) -> pd.Series:
    """Return a boolean mask for rows whose label path hits TP and SL on the same bar."""
    required = {"high", "low", "close"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"missing required columns for labels: {sorted(missing)}")
    if horizon_days <= 0:
        raise ValueError("horizon_days must be positive")

    ordered = bars.sort_index()
    close = ordered["close"].to_numpy(dtype=float)
    high = ordered["high"].to_numpy(dtype=float)
    low = ordered["low"].to_numpy(dtype=float)
    basis = price_basis.strip().lower()

    conflicts: npt.NDArray[np.bool_] = np.zeros(close.shape[0], dtype=bool)
    for idx in range(close.shape[0]):
        entry_idx, entry = _resolve_entry_price(
            bars=ordered,
            close=close,
            start_idx=idx,
            horizon_days=horizon_days,
            basis=basis,
            exclude_untradable=exclude_untradable,
        )
        if entry_idx < 0 or entry <= 0:
            continue

        last_future = entry_idx + horizon_days
        if last_future > close.shape[0]:
            continue
        tp_price = entry * (1.0 + take_profit_pct)
        sl_price = entry * (1.0 - stop_loss_pct)

        for future_idx in range(entry_idx, entry_idx + horizon_days):
            hit_tp = high[future_idx] >= tp_price
            hit_sl = low[future_idx] <= sl_price
            if hit_tp and hit_sl:
                conflicts[idx] = True
                break
            if hit_tp or hit_sl:
                break

    return pd.Series(conflicts, index=ordered.index, name="same_bar_conflict")


def _resolve_entry_price(
    bars: pd.DataFrame,
    close: npt.NDArray[np.float64],
    start_idx: int,
    horizon_days: int,
    basis: str,
    exclude_untradable: bool,
) -> tuple[int, float]:
    if basis != "next_tradable_vwap":
        if start_idx >= close.shape[0]:
            return -1, 0.0
        return start_idx, float(close[start_idx])

    max_search = min(close.shape[0], start_idx + horizon_days + 1)
    for idx in range(start_idx + 1, max_search):
        row = bars.iloc[idx]
        if bool(row.get("suspended", False)):
            continue
        vwap = row.get("vwap")
        if isinstance(vwap, (int, float)) and float(vwap) > 0:
            return idx, float(vwap)
        price = float(close[idx])
        if price > 0:
            return idx, price
    if exclude_untradable:
        return -1, 0.0
    fallback = start_idx if start_idx < close.shape[0] else -1
    if fallback < 0:
        return -1, 0.0
    return fallback, float(close[fallback])


def _resolve_same_bar_conflict(
    *,
    bar: pd.Series,
    entry_price: float,
    take_profit_price: float,
    stop_loss_price: float,
    policy: str,
    soft_label_value: float,
) -> float:
    normalized = policy.strip().lower()
    if normalized == "conservative_zero":
        return 0.0
    if normalized == "soft_label":
        return max(0.0, min(1.0, float(soft_label_value)))
    if normalized != "bar_shape_heuristic":
        raise ValueError(f"unsupported label conflict policy: {policy}")

    open_price = _safe_price(bar.get("open"), default=entry_price)
    close_price = _safe_price(bar.get("close"), default=entry_price)
    high_price = _safe_price(bar.get("high"), default=max(entry_price, take_profit_price))
    low_price = _safe_price(bar.get("low"), default=min(entry_price, stop_loss_price))
    bar_range = max(high_price - low_price, 1e-6)
    close_position = (close_price - low_price) / bar_range
    bullish_votes = 0
    bearish_votes = 0

    if close_price >= open_price:
        bullish_votes += 1
    else:
        bearish_votes += 1
    if close_price >= entry_price:
        bullish_votes += 1
    else:
        bearish_votes += 1
    if close_position >= 0.55:
        bullish_votes += 1
    elif close_position <= 0.45:
        bearish_votes += 1

    tp_gap = abs(take_profit_price - open_price)
    sl_gap = abs(open_price - stop_loss_price)
    if tp_gap < sl_gap:
        bullish_votes += 1
    elif sl_gap < tp_gap:
        bearish_votes += 1

    return 1.0 if bullish_votes >= bearish_votes else 0.0


def _safe_price(value: object, *, default: float) -> float:
    if isinstance(value, (int, float)):
        parsed = float(value)
        if parsed > 0:
            return parsed
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return default
        if parsed > 0:
            return parsed
    return default
