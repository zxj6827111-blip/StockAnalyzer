"""Market-observed outcomes kept separate from confirmed execution outcomes."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd


def summarize_market_observation(
    *,
    bars: pd.DataFrame,
    recommended_at: datetime,
    observed_at: datetime,
    reference_price: float,
    horizon_days: int,
) -> dict[str, object]:
    horizon = max(1, int(horizon_days))
    if reference_price <= 0:
        return {"status": "pending", "pending_reason": "reference_price_missing"}
    if bars.empty:
        return {"status": "pending", "pending_reason": "market_path_missing"}
    frame = bars.copy()
    frame.index = pd.to_datetime(frame.index, errors="coerce")
    frame = frame[frame.index.notna()].sort_index()
    frame = frame.loc[frame.index >= pd.Timestamp(recommended_at.date())]
    required_rows = horizon + 1
    if len(frame) < required_rows:
        stale_after = recommended_at + timedelta(days=max(14, horizon * 2))
        reason = (
            "market_path_incomplete_after_maturity"
            if observed_at >= stale_after
            else "observation_window_not_matured"
        )
        return {"status": "pending", "pending_reason": reason, "observed_rows": len(frame)}
    window = frame.iloc[:required_rows]
    if not {"close", "high", "low"} <= set(window.columns):
        return {"status": "pending", "pending_reason": "market_path_price_missing"}
    close = pd.to_numeric(window["close"], errors="coerce").dropna()
    high = pd.to_numeric(window["high"], errors="coerce").dropna()
    low = pd.to_numeric(window["low"], errors="coerce").dropna()
    if close.empty or high.empty or low.empty:
        return {"status": "pending", "pending_reason": "market_path_price_missing"}
    expiry_price = float(close.iloc[-1])
    return {
        "status": "observed",
        "observed_at": observed_at.isoformat(),
        "horizon_days": horizon,
        "observed_rows": len(window),
        "reference_price": round(reference_price, 6),
        "expiry_price": round(expiry_price, 6),
        "expiry_return_pct": round(expiry_price / reference_price - 1.0, 6),
        "maximum_favorable_excursion_pct": round(float(high.max()) / reference_price - 1.0, 6),
        "maximum_adverse_excursion_pct": round(float(low.min()) / reference_price - 1.0, 6),
    }
