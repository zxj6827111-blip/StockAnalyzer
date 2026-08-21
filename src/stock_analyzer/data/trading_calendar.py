"""Pure A-share trading calendar helpers (no network, no np.busday_count).

PLAN Section 3 requires the intraday date to be the previous **open**
trading date before the snapshot's latest day, resolved via the
A-share trading calendar -- not bare ``date - timedelta(days=1)`` and
not ``np.busday_count`` alone.

This module provides:
- ``resolve_required_intraday_date``: the single entry point used by
  the Week5 funnel and by the FeatureEngineer T-1 contract test.
- helpers for weekday fallback and provider-backed calendar lookup.

The provider-backed path uses ``provider.list_open_trade_dates`` with a
20-day window when available; otherwise it falls back to a pure
weekday (Mon-Fri) loop.  ``np.busday_count`` is never used as the sole
source of truth.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any


def _coerce_date(value: object) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip()).date()
        except ValueError:
            pass
        # fallback: try pandas-style parse without importing pandas
        try:
            import pandas as pd  # type: ignore

            parsed = pd.to_datetime(value, errors="coerce")
            if not pd.isna(parsed):
                return parsed.date()
        except Exception:
            pass
    return None


def is_open_trading_date(d: date) -> bool:
    """Weekday-only open check (Mon-Fri).

    This is the pure fallback when no exchange calendar is available.
    It intentionally does NOT use ``np.busday_count`` alone -- the caller
    that needs holiday-aware dates should supply a provider with
    ``list_open_trade_dates``.
    """
    return d.weekday() < 5


def is_bj_symbol(symbol: str) -> bool:
    """Conservative Beijing-market check shared by freshness and sync.

    Covers ``.BJ`` suffix and all current BJ prefixes (``4``, ``8``,
    ``92`` canonical).  Other boards must not be misclassified here.
    """
    text = str(symbol or "").strip().upper()
    if text.endswith(".BJ"):
        return True
    code = "".join(ch for ch in text if ch.isdigit())
    if len(code) != 6:
        return False
    if code.startswith("920"):
        return True
    if code.startswith(("4", "8")):
        return True
    return False


def _weekday_previous_open(snapshot_date: date) -> date:
    """Previous open trading date via weekday loop (no np.busday_count)."""
    cursor = snapshot_date - timedelta(days=1)
    # Guard against infinite loop: at most 10 days back (covers Golden Week).
    for _ in range(15):
        if is_open_trading_date(cursor):
            return cursor
        cursor -= timedelta(days=1)
    return cursor


def _provider_previous_open(
    snapshot_date: date,
    provider: Any,
) -> date | None:
    """Try provider's ``list_open_trade_dates`` with 20-day window."""
    fn = getattr(provider, "list_open_trade_dates", None)
    if not callable(fn):
        return None
    start = snapshot_date - timedelta(days=20)
    try:
        dates = fn(start_date=start, end_date=snapshot_date)
    except TypeError:
        # Some providers use positional args
        try:
            dates = fn(start, snapshot_date)
        except Exception:
            return None
    except Exception:
        return None
    if not dates:
        return None
    # Normalise to date objects and sort
    parsed: list[date] = []
    for item in dates:
        coerced = _coerce_date(item)
        if coerced is not None:
            parsed.append(coerced)
    if not parsed:
        return None
    parsed = sorted(set(parsed))
    # Find previous open before snapshot_date
    # snapshot_date itself may be an open date; we need strictly < snapshot_date
    candidates = [d for d in parsed if d < snapshot_date]
    if candidates:
        return candidates[-1]
    # If no candidate before snapshot (e.g. snapshot is earliest), fallback
    return None


def resolve_required_intraday_date(
    snapshot_trade_date: date | str | datetime,
    provider_or_trade_cal: Any | None = None,
) -> date:
    """Resolve the required intraday (minute) date for the nightly deep stage.

    Contract (Section 3): FeatureEngineer is T-1 (shift(1)), so the deep
    stage's intraday summaries must be from the **previous open trading date**
    before the snapshot's latest day.  This is an A-share trading-calendar
    concept, not a bare calendar subtraction.

    Resolution order:
    1. Try ``provider_or_trade_cal.list_open_trade_dates`` with a 20-day
       window ``[snapshot-20d, snapshot]`` and pick the previous open date.
    2. Fallback to pure weekday loop (Mon-Fri).

    ``np.busday_count`` is never used as the sole decision source.

    Args:
        snapshot_trade_date: latest daily trade date from the feature snapshot
            (``YYYY-MM-DD`` string, ``date`` or ``datetime``).
        provider_or_trade_cal: object exposing ``list_open_trade_dates`` or
            ``None`` for pure weekday fallback.

    Returns:
        The previous open trading date (``date``).

    Raises:
        ValueError: when ``snapshot_trade_date`` cannot be parsed.
    """
    parsed = _coerce_date(snapshot_trade_date)
    if parsed is None:
        raise ValueError(f"invalid snapshot_trade_date: {snapshot_trade_date!r}")

    # 1) Provider-backed calendar with 20-day window
    if provider_or_trade_cal is not None:
        # provider may be a callable returning dates directly
        if callable(provider_or_trade_cal) and not hasattr(
            provider_or_trade_cal, "list_open_trade_dates"
        ):
            try:
                result = provider_or_trade_cal(parsed)
                coerced = _coerce_date(result)
                if coerced is not None:
                    return coerced
            except Exception:
                pass
        else:
            provider_result = _provider_previous_open(parsed, provider_or_trade_cal)
            if provider_result is not None:
                return provider_result

    # 2) Pure weekday fallback (禁止裸用自然日减法或仅用 np.busday_count)
    return _weekday_previous_open(parsed)


# Convenience alias for callers that already have a provider
resolve_required_minute_date = resolve_required_intraday_date
