"""Per-symbol intraday freshness report (PLAN Section 3).

Builds a single freshness assessment for the nightly deep funnel:

- ``required_trade_date``: the previous open trading date before the
  snapshot's latest day (via trading_calendar).
- For each eligible symbol (post-light, BJ-excluded), checks both the
  summary DuckDB (intraday_warehouse) and the delta warehouse.
- ``effective_as_of = max(summary_latest, delta_latest)`` per symbol.
  ``allowed_lag`` is 0 (not 3) for the fresh gate.
- Classification per symbol:
  - BJ -> unsupported_market
  - summary_missing + delta_missing -> missing
  - effective_as_of is None or < required -> effective_stale
  - else check session completeness: fetch the summary row for the
    required date and check ``minute_count >= 230`` (full A-share
    session ~240 min; 230 is the completeness threshold). Missing row
    or insufficient minutes -> session_incomplete.
  - otherwise -> fresh

Fresh ratio gate (mimics Week5Service expectation):
- ``fresh_ratio = fresh_count / len(eligible)``  (0 when eligible empty)
- ``deep_candidate_target`` comparison drives fail-closed.

The function is intentionally tolerant: a missing warehouse, missing
method, or per-symbol exception degrades to stale/missing rather than
raising.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd


SESSION_COMPLETE_MINUTE_THRESHOLD = 230
SESSION_COMPLETE_MINUTE_THRESHOLD_5M = 46


def _coerce_date(value: object) -> date | None:
    if isinstance(value, date):
        # datetime is subclass of date; handle explicitly
        from datetime import datetime as _dt

        if isinstance(value, _dt):
            return value.date()
        return value
    if isinstance(value, str) and value.strip():
        try:
            from datetime import datetime as _dt

            return _dt.fromisoformat(value.strip()).date()
        except ValueError:
            pass
        try:
            import pandas as pd  # type: ignore

            parsed = pd.to_datetime(value, errors="coerce")
            if not pd.isna(parsed):
                return parsed.date()
        except Exception:
            pass
    return None


def _is_bj_symbol(symbol: str) -> bool:
    # Canonical predicate lives in trading_calendar.is_bj_symbol (covers 920/4/8/.BJ).
    try:
        from stock_analyzer.data.trading_calendar import is_bj_symbol as _is_bj  # noqa: WPS433

        return bool(_is_bj(symbol))
    except Exception:
        text = str(symbol).strip().upper()
        if text.endswith(".BJ"):
            return True
        code = "".join(ch for ch in text if ch.isdigit())
        if len(code) == 6 and (code.startswith("920") or code.startswith(("4", "8"))):
            return True
        return False


def _infer_is_bj(symbol: str, warehouse: Any | None) -> bool:
    # Prefer board metadata from daily bars if available
    if warehouse is not None:
        try:
            bars = warehouse.fetch_all_daily_bars(symbol=symbol)  # type: ignore[attr-defined]
            if isinstance(bars, pd.DataFrame) and not bars.empty and "board" in bars.columns:
                board = str(bars.iloc[-1].get("board", "")).strip().lower()
                if board in {"bse", "bj", "beijing", "北交所"}:
                    return True
                # If board explicitly says not BJ, trust it
                if board:
                    return False
        except Exception:
            pass
    return _is_bj_symbol(symbol)


def _fetch_latest_intraday_date(
    warehouse: Any | None,
    symbol: str,
    interval: str = "1m",
) -> date | None:
    if warehouse is None:
        return None
    # Try latest_intraday_dates batch first, then single
    for method_name in ("latest_intraday_dates", "latest_intraday_date"):
        fn = getattr(warehouse, method_name, None)
        if not callable(fn):
            continue
        try:
            if method_name == "latest_intraday_dates":
                result = fn(interval=interval, symbols=[symbol])
                if isinstance(result, dict):
                    val = result.get(symbol)
                    coerced = _coerce_date(val)
                    if coerced is not None:
                        return coerced
            else:
                val = fn(symbol=symbol, interval=interval)
                coerced = _coerce_date(val)
                if coerced is not None:
                    return coerced
        except Exception:
            continue
    return None


def _fetch_summary_minute_count(
    warehouse: Any | None,
    vendor_overlay: Any | None,
    symbol: str,
    interval: str,
    required_trade_date: date,
) -> int | None:
    """Return minute_count for the required date, or None when missing."""
    # Try vendor_overlay first (it merges summary + delta)
    for provider in (vendor_overlay, warehouse):
        if provider is None:
            continue
        for method_name in ("fetch_intraday_summary", "fetch_intraday_summaries"):
            fn = getattr(provider, method_name, None)
            if not callable(fn):
                continue
            try:
                if method_name == "fetch_intraday_summaries":
                    mapping = fn(symbols=[symbol], interval=interval, lookback_days=10)
                    if isinstance(mapping, dict):
                        frame = mapping.get(symbol)
                        if isinstance(frame, pd.DataFrame) and not frame.empty:
                            # frame indexed by date
                            for idx in frame.index:
                                d = _coerce_date(idx)
                                if d == required_trade_date:
                                    row = frame.loc[idx]
                                    # row may be Series when single row
                                    if isinstance(row, pd.DataFrame):
                                        row = row.iloc[0]
                                    for col in ("minute_count", "minutes", "count"):
                                        if col in row.index:
                                            try:
                                                return int(float(row[col]))
                                            except Exception:
                                                pass
                                    return 0
                            return None
                else:
                    frame = fn(symbol=symbol, interval=interval, lookback_days=10)
                    if isinstance(frame, pd.DataFrame) and not frame.empty:
                        for idx in frame.index:
                            d = _coerce_date(idx)
                            if d == required_trade_date:
                                row = frame.loc[idx]
                                if isinstance(row, pd.DataFrame):
                                    row = row.iloc[0]
                                for col in ("minute_count", "minutes", "count"):
                                    if col in row.index:
                                        try:
                                            return int(float(row[col]))
                                        except Exception:
                                            pass
                                return 0
                        return None
            except Exception:
                continue
    return None


@dataclass(slots=True)
class IntradayFreshnessReport:
    required_trade_date: date | None
    summary_missing: list[str] = field(default_factory=list)
    delta_missing: list[str] = field(default_factory=list)
    effective_stale: list[str] = field(default_factory=list)
    session_incomplete: list[str] = field(default_factory=list)
    unsupported_market: list[str] = field(default_factory=list)
    source_breakdown: dict[str, int] = field(default_factory=dict)
    fresh_symbols: list[str] = field(default_factory=list)
    fresh_count: int = 0
    fresh_ratio: float = 0.0
    deep_candidate_count: int = 0
    deep_candidate_target: int = 20
    eligible_count: int = 0
    total_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "required_trade_date": self.required_trade_date.isoformat() if self.required_trade_date else "",
            "summary_missing": list(self.summary_missing),
            "delta_missing": list(self.delta_missing),
            "effective_stale": list(self.effective_stale),
            "session_incomplete": list(self.session_incomplete),
            "unsupported_market": list(self.unsupported_market),
            "source_breakdown": dict(self.source_breakdown),
            "fresh_symbols": list(self.fresh_symbols),
            "fresh_count": int(self.fresh_count),
            "fresh_ratio": float(self.fresh_ratio),
            "deep_candidate_count": int(self.deep_candidate_count),
            "deep_candidate_target": int(self.deep_candidate_target),
            "eligible_count": int(self.eligible_count),
            "total_count": int(self.total_count),
        }


def build_intraday_freshness_report(
    warehouse: Any | None,
    vendor_overlay: Any | None,
    symbols: list[str],
    required_trade_date: date | str | None,
    *,
    interval: str = "1m",
    deep_candidate_target: int = 20,
) -> IntradayFreshnessReport:
    """Build per-symbol freshness report for the deep funnel.

    Args:
        warehouse: delta MarketWarehouse (or None).
        vendor_overlay: VendorZipOverlayProvider (or None). When present,
            its intraday_warehouse is used as the summary warehouse; the
            overlay itself is also used for merged fetch.
        symbols: eligible symbols (post-light, pre-deep; BJ already excluded
            is also supported -- this function will additionally mark BJ as
            unsupported).
        required_trade_date: previous open trading date (from trading_calendar).
        interval: intraday interval to check (default "1m").
        deep_candidate_target: deep funnel target (for the gate).

    Returns:
        IntradayFreshnessReport with per-symbol classification.
    """
    req_date = _coerce_date(required_trade_date) if required_trade_date is not None else None

    # Resolve summary warehouse: vendor_overlay._intraday_warehouse when available
    summary_warehouse: Any | None = None
    if vendor_overlay is not None:
        summary_warehouse = getattr(vendor_overlay, "_intraday_warehouse", None)
        # Fallback: vendor_overlay itself can serve summaries
        if summary_warehouse is None:
            summary_warehouse = vendor_overlay
    delta_warehouse = warehouse

    # Normalise symbols
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in symbols or []:
        s = str(raw).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        normalized.append(s)

    report = IntradayFreshnessReport(
        required_trade_date=req_date,
        deep_candidate_target=max(1, int(deep_candidate_target)),
        eligible_count=len(normalized),
        total_count=len(normalized),
    )

    if req_date is None:
        # No required date -> everything is stale/missing
        report.effective_stale = list(normalized)
        report.source_breakdown = {
            "unsupported_market": 0,
            "summary_missing": 0,
            "delta_missing": 0,
            "effective_stale": len(normalized),
            "session_incomplete": 0,
            "fresh": 0,
        }
        return report

    unsupported: list[str] = []
    summary_missing: list[str] = []
    delta_missing: list[str] = []
    effective_stale: list[str] = []
    session_incomplete: list[str] = []
    fresh: list[str] = []

    for symbol in normalized:
        # 1) BJ exclusion
        # Use delta warehouse board when available for accurate BJ detection
        is_bj = _infer_is_bj(symbol, delta_warehouse)
        if is_bj:
            unsupported.append(symbol)
            continue

        # 2) Fetch latest dates from summary and delta
        summary_latest = _fetch_latest_intraday_date(summary_warehouse, symbol, interval=interval)
        delta_latest = _fetch_latest_intraday_date(delta_warehouse, symbol, interval=interval)

        has_summary = summary_latest is not None
        has_delta = delta_latest is not None

        if not has_summary and not has_delta:
            # Both missing
            summary_missing.append(symbol)
            delta_missing.append(symbol)
            effective_stale.append(symbol)
            continue
        if not has_summary:
            summary_missing.append(symbol)
        if not has_delta:
            delta_missing.append(symbol)

        # Effective as_of = max(summary_latest, delta_latest)
        candidates = [d for d in (summary_latest, delta_latest) if d is not None]
        effective_as_of = max(candidates) if candidates else None

        # allowed_lag = 0: must be >= required date
        if effective_as_of is None or effective_as_of < req_date:
            effective_stale.append(symbol)
            continue

        # 3) Session completeness for required date
        minute_count = _fetch_summary_minute_count(
            warehouse=delta_warehouse,
            vendor_overlay=vendor_overlay,
            symbol=symbol,
            interval=interval,
            required_trade_date=req_date,
        )
        if minute_count is None:
            session_incomplete.append(symbol)
            continue
        threshold = SESSION_COMPLETE_MINUTE_THRESHOLD_5M if interval == "5m" else SESSION_COMPLETE_MINUTE_THRESHOLD
        if minute_count < threshold:
            session_incomplete.append(symbol)
            continue

        fresh.append(symbol)

    report.unsupported_market = sorted(unsupported)
    report.summary_missing = sorted(summary_missing)
    report.delta_missing = sorted(delta_missing)
    report.effective_stale = sorted(effective_stale)
    report.session_incomplete = sorted(session_incomplete)
    report.fresh_symbols = sorted(fresh)
    report.fresh_count = len(fresh)
    # Fresh ratio is fresh / eligible (BJ excluded)
    eligible_for_ratio = [s for s in normalized if s not in unsupported]
    if eligible_for_ratio:
        report.fresh_ratio = round(len(fresh) / len(eligible_for_ratio), 4)
    else:
        report.fresh_ratio = 1.0 if not normalized else 0.0
    report.deep_candidate_count = len(fresh)
    report.source_breakdown = {
        "unsupported_market": len(unsupported),
        "summary_missing": len(summary_missing),
        "delta_missing": len(delta_missing),
        "effective_stale": len(effective_stale),
        "session_incomplete": len(session_incomplete),
        "fresh": len(fresh),
    }
    return report
