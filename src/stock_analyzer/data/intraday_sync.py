"""Unified intraday sync interface (PLAN Section 2).

Public entry point
------------------
``sync_intraday_symbols(warehouse, symbols, required_trade_date, ...) ->
IntradaySyncReport``

with the guarantees required by the PLAN:

* One 1-minute request per stock; the 5-minute frame is derived locally
  from the 1-minute frame via a session-aware resample (AM 09:30-11:30,
  PM 13:00-15:00).  ``summarize_minute_bars(interval="5m")`` is never called
  directly with a 1-minute frame.
* Batch ``latest_intraday_dates()`` prefetch, only the missing window is
  fetched; the warehouse is written via ``upsert_intraday_summaries`` (no
  read-full-history-then-replace).
* Two-probe capability check.  Business errors (permission / quota) open
  the circuit and cut the remaining Shanghai/Shenzhen stocks to Sina;
  transient network errors are retried once.
* Sina fallback also pulls only 1m and locally derives 5m.
* Concurrency 4, per-request timeout 5 s, total deadline 180 s.
* Session completeness threshold: required date present, first bar
  ≤ 09:35, last bar ≥ 14:55, valid 1-minute count ≥ 230.
* BJ stays in the light/audit reports but as ``unsupported_market`` and
  never enters dependence on minute features (deep/final).
"""

from __future__ import annotations

import os
import re
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from time import monotonic
from typing import Any

import pandas as pd

from stock_analyzer.data.provider import DataSourceError

_SYMBOL_RE = re.compile(r"(\d{6})")
_SESSION_COMPLETE_MINUTE_THRESHOLD = 230
_PROBE_COUNT = 2


def _normalize_symbol(symbol: str) -> str:
    text = str(symbol or "").strip().upper()
    match = _SYMBOL_RE.search(text)
    return match.group(1) if match else ""


def _is_bj_symbol(symbol: str) -> bool:
    try:
        from stock_analyzer.data.trading_calendar import is_bj_symbol as _is_bj  # noqa: WPS433

        return bool(_is_bj(symbol))
    except Exception:
        code = _normalize_symbol(symbol)
        if not code:
            return False
        if str(symbol).strip().upper().endswith(".BJ"):
            return True
        if code.startswith("920"):
            return True
        if code.startswith(("4", "8")):
            return True
        return False


def _coerce_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        parsed = pd.to_datetime(value, errors="coerce")  # type: ignore[arg-type]
        if not pd.isna(parsed):
            return pd.Timestamp(parsed).date()
    except Exception:
        pass
    return None


def _check_minute_session_completeness(
    frame: pd.DataFrame,
    required_trade_date: date,
) -> tuple[bool, str]:
    """Check PLAN §2 minute-session completeness gate."""
    if frame is None or frame.empty:
        return False, "empty_frame"
    if not isinstance(frame.index, pd.DatetimeIndex):
        return False, "no_datetime_index"
    try:
        required_ts = pd.Timestamp(required_trade_date)
    except Exception:
        return False, "bad_required_date"
    # Deduplicate duplicate wall-clock stamps (Sina occasionally emits
    # 09:30:00 twice: order-book snapshot + bar snapshot) before selecting
    # the target date and counting session length.
    try:
        deduped = frame[~frame.index.duplicated(keep="last")].sort_index()
    except Exception:
        deduped = frame.sort_index()
    try:
        day_bars = deduped.loc[deduped.index.normalize() == required_ts.normalize()]
    except Exception:
        return False, "index_filter_failed"
    if day_bars.empty:
        return False, "missing_required_date"
    # First / last wall-clock checks (A-share regular session is
    # 09:30-11:30, 13:00-15:00; PLAN requires first ≤ 09:35 and last ≥ 14:55).
    first_time = pd.Timestamp(day_bars.index.min()).time()
    last_time = pd.Timestamp(day_bars.index.max()).time()
    if first_time > time(9, 35):
        return False, f"first_bar_late:{first_time.isoformat()}"
    if last_time < time(14, 55):
        return False, f"last_bar_early:{last_time.isoformat()}"
    valid_count = int(len(day_bars))
    if valid_count < _SESSION_COMPLETE_MINUTE_THRESHOLD:
        return False, f"insufficient_bars:{valid_count}"
    return True, ""


@dataclass(slots=True)
class IntradaySyncReport:
    """Report returned by :func:`sync_intraday_symbols`."""

    target_trade_date: str = ""
    symbols_total: int = 0
    ok: int = 0
    skipped: int = 0
    failed: int = 0
    session_incomplete: list[str] = field(default_factory=list)
    stale_symbols: list[str] = field(default_factory=list)
    unsupported_market: list[str] = field(default_factory=list)
    source_breakdown: dict[str, int] = field(default_factory=dict)
    elapsed_ms: int = 0
    capability_probe: dict[str, object] = field(default_factory=dict)
    # Extra observability fields (PLAN says report must contain target date,
    # per-source counts, ok/skipped/failed, session incomplete, stale,
    # elapsed and probe result — extra keys are allowed).
    detail: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "target_trade_date": self.target_trade_date,
            "symbols_total": int(self.symbols_total),
            "ok": int(self.ok),
            "skipped": int(self.skipped),
            "failed": int(self.failed),
            "session_incomplete": list(self.session_incomplete),
            "stale_symbols": list(self.stale_symbols),
            "unsupported_market": list(self.unsupported_market),
            "source_breakdown": dict(self.source_breakdown),
            "elapsed_ms": int(self.elapsed_ms),
            "capability_probe": dict(self.capability_probe),
            "detail": dict(self.detail),
        }


def _resolve_tushare_provider(warehouse: Any | None) -> Any | None:
    """Best-effort: instantiate a TushareProvider when token is available."""
    try:
        from stock_analyzer.data.tushare_provider import TushareProvider  # noqa: WPS433

        token = ""
        for key in ("SA__MARKET_WAREHOUSE__TUSHARE_TOKEN", "TUSHARE_TOKEN", "TS_TOKEN"):
            value = str(os.environ.get(key, "") or "").strip()
            if value:
                token = value
                break
        if not token and warehouse is not None:
            # Some warehouse wrappers carry config.token
            for attr in ("tushare_token", "_token", "token"):
                try:
                    candidate = str(getattr(warehouse, attr, "") or "").strip()
                    if candidate:
                        token = candidate
                        break
                except Exception:
                    continue
        provider = TushareProvider(token=token) if token else TushareProvider()
        return provider
    except Exception:
        return None


def _is_transport_failure(exc: Exception) -> bool:
    try:
        from stock_analyzer.data.tushare_provider import _is_transport_failure as _impl  # noqa: WPS433

        return bool(_impl(exc))
    except Exception:
        # Fallback: only DataSourceError is non-transient
        return not isinstance(exc, DataSourceError)


def _fetch_with_sina(symbol: str, timeout_sec: int = 5) -> pd.DataFrame:
    try:
        from stock_analyzer.data.intraday_summary import fetch_sina_minute_bars  # noqa: WPS433

        return fetch_sina_minute_bars(
            symbol=symbol, interval="1m", timeout_sec=max(5, int(timeout_sec))
        )
    except Exception:
        return pd.DataFrame()


def sync_intraday_symbols(
    warehouse: Any | None,
    symbols: list[str],
    required_trade_date: date | str | datetime,
    primary: str = "tushare",
    fallback: str = "sina",
    deadline_sec: int = 180,
    concurrency: int = 4,
    timeout_sec: int = 5,
    tushare_provider: Any | None = None,
    vendor_overlay: Any | None = None,
) -> IntradaySyncReport:
    """Fetch minute bars for *symbols* and upsert warehoused summaries.

    PLAN Section 2 closed-loop:
    - Only 1-minute data is fetched per symbol; 5-minute is derived locally
      with a session-aware resample (AM/PM split, 5-minute bucket).
    - ``warehouse.latest_intraday_dates()`` is prefetched once so only the
      missing window is requested.
    - Two cheap probes decide the Tushare batch capability; business /
      permission errors open the circuit for the remaining HS-eligible
      symbols (uniform fallback to Sina); transient network errors are
      retried once.
    - Completeness gate per symbol:
      required date present, first bar ≤ 09:35, last bar ≥ 14:55,
      valid 1-minute count ≥ 230.
    - Stale symbols and BJ (``unsupported_market``) never enter deep/final.
    - Shared cross-process lock ``intraday_sync.lock`` guards the DuckDB
      write.

    Args:
        warehouse: the delta :class:`MarketWarehouse` (or any object exposing
            ``latest_intraday_dates`` / ``upsert_intraday_summaries``).
        symbols: candidate symbols (post-light, pre-BJ-filter is also OK;
            BJ inside the list will be moved to ``unsupported_market``).
        required_trade_date: the previous open trading date before the
            snapshot's latest day (``date`` / ``YYYY-MM-DD`` / ``datetime``).
        primary / fallback: "tushare" / "sina" (PLAN default).
        deadline_sec: wall-clock budget for the whole round (default 180).
        concurrency / timeout_sec: per PLAN (4 / 5 s).
        tushare_provider: optional pre-instantiated provider (tests).
        vendor_overlay: optional overlay whose intraday cache will be cleared
            after a successful write.

    Returns:
        IntradaySyncReport with the fields required by Section 2.
    """
    started = monotonic()
    required_date = _coerce_date(required_trade_date)
    if required_date is None:
        raise ValueError(f"invalid required_trade_date: {required_trade_date!r}")

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in symbols or []:
        code = _normalize_symbol(str(raw))
        if not code or code in seen:
            continue
        seen.add(code)
        normalized.append(code)

    # BJ bucket (unsupported_market) — reported in light + audit but not deep.
    unsupported_market: list[str] = [s for s in normalized if _is_bj_symbol(s)]
    eligible: list[str] = [s for s in normalized if s not in set(unsupported_market)]

    report = IntradaySyncReport(
        target_trade_date=required_date.isoformat(),
        symbols_total=len(normalized),
        unsupported_market=sorted(unsupported_market),
        source_breakdown={"tushare": 0, "sina": 0, "skipped": 0},
        capability_probe={"probed": 0, "tushare_ok": False, "error": ""},
    )

    if not eligible:
        report.elapsed_ms = max(1, int((monotonic() - started) * 1000))
        report.skipped = len(normalized)
        return report

    # Shared lock — API and scheduler must not write DuckDB concurrently.
    lock = None
    lock_acquired = True
    try:
        from stock_analyzer.ops.file_lock import DistributedFileLock  # noqa: WPS433

        # Prefer the runtime-wide path; fall back to warehouse parent.
        lock_path = Path("artifacts/runtime/intraday_sync.lock")
        if warehouse is not None:
            try:
                db_path = Path(str(getattr(warehouse, "db_path", "") or "")).expanduser()
                if db_path and db_path.parent.exists():
                    # Keep both paths equivalent: the warehouse-adjacent lock is
                    # also materialised via a symlink / same filesystem mount
                    # in production (artifacts is shared via compose mount).
                    # We keep the stable artifacts path as the primary.
                    pass
            except Exception:
                pass
        lock = DistributedFileLock(lock_path, stale_after_sec=max(30, int(deadline_sec)))
        lock_acquired = lock.acquire()
        if not lock_acquired:
            report.skipped = len(eligible)
            report.failed = 0
            report.detail["lock_busy"] = True
            report.elapsed_ms = max(1, int((monotonic() - started) * 1000))
            return report
    except Exception as exc:
        # Lock construction failure: fail-closed, do not proceed without lock.
        report.skipped = 0
        report.failed = len(eligible)
        report.detail = {"lock_error": f"{type(exc).__name__}:{exc}"}
        report.elapsed_ms = max(1, int((monotonic() - started) * 1000))
        return report

    try:
        # Batch prefetch latest dates for both 1m and 5m so we only pull
        # missing windows.  A symbol is "up to date" only when BOTH intervals
        # cover the required date (P0-2 fix: 5m must also be checked).
        def _prefetch_interval(interval: str) -> dict[str, date]:
            if warehouse is None:
                return {}
            try:
                fn = getattr(warehouse, "latest_intraday_dates", None)
                if not callable(fn):
                    return {}
                result = fn(interval=interval, symbols=eligible)
                return result if isinstance(result, dict) else {}
            except Exception:
                return {}

        latest_1m = _prefetch_interval("1m")
        latest_5m = _prefetch_interval("5m")

        # Idempotent skip: both 1m and 5m must be >= required_date.
        # Moved BEFORE the probe (P1-4 fix: probe only symbols that need
        # fetching, not up-to-date ones).
        up_to_date: set[str] = set()
        for sym in eligible:
            d1 = latest_1m.get(sym)
            d5 = latest_5m.get(sym)
            if d1 is not None and d5 is not None and d1 >= required_date and d5 >= required_date:
                up_to_date.add(sym)
        skipped_up_to_date = sorted(up_to_date)
        fetch_needed = [s for s in eligible if s not in up_to_date]

        # Primary provider resolution (tushare vs sina).
        primary_norm = str(primary or "tushare").strip().lower() or "tushare"
        fallback_norm = str(fallback or "sina").strip().lower() or "sina"

        tushare = tushare_provider or _resolve_tushare_provider(warehouse)
        # Probe capability with up to 2 symbols that actually need fetching.
        # Never probe up_to_date symbols — they may not need fetching at all.
        probe_symbols: list[str] = fetch_needed[:_PROBE_COUNT]
        if len(probe_symbols) < _PROBE_COUNT:
            for sym in fetch_needed:
                if sym not in probe_symbols:
                    probe_symbols.append(sym)
                    if len(probe_symbols) >= _PROBE_COUNT:
                        break
        # If still short (e.g. fetch_needed empty), do not probe.

        tushare_ok = True
        probe_error = ""
        probed = 0
        circuit_open = False
        if primary_norm == "tushare" and tushare is not None and probe_symbols:
            for sym in probe_symbols[:_PROBE_COUNT]:
                probed += 1
                try:
                    frame = tushare.fetch_minute_bars(  # type: ignore[union-attr]
                        symbol=sym,
                        start_date=required_date,
                        end_date=required_date,
                        freq="1min",
                    )
                    # Empty frame is not a capability failure — the stock may be
                    # suspended on that day.  Only an exception counts.
                    _ = frame
                except Exception as exc:
                    msg = f"{type(exc).__name__}:{exc}"
                    probe_error = msg
                    if _is_transport_failure(exc):
                        # Transient: retry once per PLAN
                        try:
                            frame = tushare.fetch_minute_bars(  # type: ignore[union-attr]
                                symbol=sym,
                                start_date=required_date,
                                end_date=required_date,
                                freq="1min",
                            )
                            _ = frame
                            continue
                        except Exception as exc2:
                            probe_error = f"{type(exc2).__name__}:{exc2}"
                            if not _is_transport_failure(exc2):
                                circuit_open = True
                                tushare_ok = False
                                probe_error = msg + f" | retry:{probe_error}"
                                break
                            # Transient even after retry: treat as probe OK but
                            # let per-symbol logic fall back individually.
                            continue
                    else:
                        # Business / permission / quota error -> circuit open
                        circuit_open = True
                        tushare_ok = False
                        break
            report.capability_probe = {
                "probed": probed,
                "tushare_ok": tushare_ok,
                "error": probe_error,
            }

        # Per-symbol fetch + completeness + summarise.
        # We collect 1m and 5m payloads and upsert them in bulk.
        use_tushare_for_symbol = (
            (lambda _sym: not circuit_open and primary_norm == "tushare" and tushare is not None)
            if primary_norm == "tushare"
            else (lambda _sym: False)
        )

        # If circuit is open for tushare, remaining HS stocks uniformly use fallback.
        # The per-symbol fetcher will honour that.
        from stock_analyzer.data.tushare_provider import resample_1m_to_5m_session_aware  # noqa: WPS433
        from stock_analyzer.data.intraday_summary import summarize_minute_bars  # noqa: WPS433

        ok_symbols: list[str] = []
        failed_symbols: list[str] = []
        session_incomplete: list[str] = []
        stale_symbols: list[str] = []
        source_counts = {"tushare": 0, "sina": 0, "skipped": 0}

        payloads_1m: list[pd.DataFrame] = []
        payloads_5m: list[pd.DataFrame] = []

        def _fetch_one(symbol: str) -> tuple[str, str, pd.DataFrame, str]:
            """Return (symbol, source_used, frame_1m, error_reason)."""
            # Budget check — float comparison, no int truncation.
            if monotonic() - started > max(5.0, float(deadline_sec)):
                return symbol, "deadline", pd.DataFrame(), "deadline_exceeded"
            prefer_tushare = use_tushare_for_symbol(symbol)
            # Try primary first
            last_error = ""
            frame = pd.DataFrame()
            source = ""
            if prefer_tushare:
                try:
                    frame = tushare.fetch_minute_bars(  # type: ignore[union-attr]
                        symbol=symbol,
                        start_date=required_date,
                        end_date=required_date,
                        freq="1min",
                    )
                    source = "tushare"
                except Exception as exc:
                    last_error = f"{type(exc).__name__}:{exc}"
                    if _is_transport_failure(exc):
                        try:
                            frame = tushare.fetch_minute_bars(  # type: ignore[union-attr]
                                symbol=symbol,
                                start_date=required_date,
                                end_date=required_date,
                                freq="1min",
                            )
                            source = "tushare"
                        except Exception as exc2:
                            last_error = f"{type(exc2).__name__}:{exc2}"
                            frame = pd.DataFrame()
                            source = ""
                    else:
                        # Business error: immediately try fallback for this symbol
                        frame = pd.DataFrame()
                        source = ""
            # Fallback to Sina when primary yielded nothing and fallback is sina
            if frame.empty and fallback_norm == "sina":
                sina_frame = _fetch_with_sina(symbol, timeout_sec=timeout_sec)
                if not sina_frame.empty:
                    frame = sina_frame
                    source = "sina"
                    last_error = ""
                elif not last_error:
                    last_error = "sina_empty"
            return symbol, source, frame, last_error

        # Submit only the missing window with bounded concurrency and hard deadline.
        # Uses wait(FIRST_COMPLETED, timeout=remaining) for real per-iteration
        # timeout enforcement.  The executor is shut down without waiting so
        # running HTTP calls do not block beyond the deadline (P1-3 fix).
        results: dict[str, tuple[str, pd.DataFrame, str]] = {}
        executor = ThreadPoolExecutor(max_workers=max(1, int(concurrency)))
        try:
            futures_to_symbol: dict[Any, str] = {}
            for sym in fetch_needed:
                fut = executor.submit(_fetch_one, sym)
                futures_to_symbol[fut] = sym
            pending = set(futures_to_symbol.keys())
            while pending:
                remaining = max(0.0, float(deadline_sec) - (monotonic() - started))
                if remaining <= 0:
                    for fut in pending:
                        fut.cancel()
                    break
                iter_timeout = min(remaining, max(1.0, float(timeout_sec)) + 1.0)
                done, pending = wait(pending, timeout=iter_timeout, return_when=FIRST_COMPLETED)
                for fut in done:
                    sym = futures_to_symbol[fut]
                    try:
                        symbol, source, frame, err = fut.result(timeout=0)
                    except Exception as exc:
                        symbol, source, frame, err = (
                            sym,
                            "",
                            pd.DataFrame(),
                            f"{type(exc).__name__}:{exc}",
                        )
                    results[symbol] = (source, frame, err)
        finally:
            # Do not block on running HTTP calls beyond the deadline.
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                # Python <3.9: cancel_futures not supported.
                try:
                    executor.shutdown(wait=False)
                except Exception:
                    pass
        # Seed results for the up-to-date stocks so the loop below can skip them.
        for sym in skipped_up_to_date:
            if sym not in results:
                d1 = latest_1m.get(sym)
                d5 = latest_5m.get(sym)
                results[sym] = ("skipped", pd.DataFrame(), f"up_to_date:1m={d1},5m={d5}")

        # Classify up-to-date symbols as skipped upfront (no fetch, no stale).
        for sym in skipped_up_to_date:
            source_counts["skipped"] = int(source_counts.get("skipped", 0)) + 1

        for symbol in eligible:
            if symbol in up_to_date:
                continue
            source, frame, err = results.get(symbol, ("", pd.DataFrame(), "not_fetched"))
            if frame.empty:
                # No data at all: treat as failed (stale)
                failed_symbols.append(symbol)
                stale_symbols.append(symbol)
                source_counts["skipped"] += 1
                continue
            complete, reason = _check_minute_session_completeness(frame, required_date)
            if not complete:
                session_incomplete.append(symbol)
                stale_symbols.append(symbol)
                # Do not mark fresh; count as failed for freshness but keep reason
                # PLAN: 不完整数据不得标记 fresh.
                failed_symbols.append(symbol)
                if source:
                    source_counts[source] = int(source_counts.get(source, 0)) + 1
                else:
                    source_counts["skipped"] += 1
                continue
            # Complete session: derive 5m locally from 1m, never fetch 5m separately.
            try:
                frame_5m = resample_1m_to_5m_session_aware(frame)
            except Exception:
                frame_5m = pd.DataFrame()
            # Summarise both intervals to daily factors.
            try:
                summary_1m = summarize_minute_bars(frame, interval="1m")
            except Exception:
                summary_1m = pd.DataFrame()
            try:
                summary_5m = (
                    summarize_minute_bars(frame_5m, interval="5m")
                    if not frame_5m.empty
                    else pd.DataFrame()
                )
            except Exception:
                summary_5m = pd.DataFrame()

            # Filter summaries to the required date only (keep history untouched).
            # The warehouse upsert will merge per-date rows; we only pass that date.
            def _filter_to_date(summary: pd.DataFrame) -> pd.DataFrame:
                if summary is None or summary.empty:
                    return pd.DataFrame()
                try:
                    idx = pd.DatetimeIndex(summary.index)
                    required_ts = pd.Timestamp(required_date)
                    filtered = summary.loc[idx.normalize() == required_ts.normalize()]
                    return filtered
                except Exception:
                    return summary

            summary_1m = _filter_to_date(summary_1m)
            summary_5m = _filter_to_date(summary_5m)
            # PLAN §2 + P0 fix: both 1m and 5m summaries are required for the
            # required date.  5m is locally derived from 1m, so a missing 5m
            # summary indicates a summariser failure and must not be fresh.
            if summary_1m.empty or summary_5m.empty:
                failed_symbols.append(symbol)
                stale_symbols.append(symbol)
                if summary_1m.empty and summary_5m.empty:
                    session_incomplete.append(symbol)
                elif summary_5m.empty:
                    # Keep signal distinct from minute-level incompleteness
                    session_incomplete.append(symbol)
                if source:
                    source_counts[source] = int(source_counts.get(source, 0)) + 1
                continue
            # Accumulate payload rows for bulk upsert.
            try:
                row_1m = summary_1m.reset_index().rename(columns={"index": "date"})
                row_1m.insert(0, "symbol", symbol)
                payloads_1m.append(row_1m)
            except Exception:
                pass
            try:
                row_5m = summary_5m.reset_index().rename(columns={"index": "date"})
                row_5m.insert(0, "symbol", symbol)
                payloads_5m.append(row_5m)
            except Exception:
                pass
            ok_symbols.append(symbol)
            if source:
                source_counts[source] = int(source_counts.get(source, 0)) + 1
            else:
                source_counts["skipped"] += 1

        # Bulk upsert — do not read full history then replace.
        # DuckDB write failure must be fail-closed: on any upsert error the
        # whole interval is considered failed (no partial freshness).
        upsert_errors: list[str] = []
        upsert_ok_1m = True
        upsert_ok_5m = True
        if payloads_1m and warehouse is not None:
            try:
                combined_1m = pd.concat(payloads_1m, axis=0, sort=False, ignore_index=True)
                warehouse.upsert_intraday_summaries(interval="1m", frame=combined_1m)
            except Exception as exc:
                upsert_errors.append(f"1m_upsert:{type(exc).__name__}:{exc}")
                upsert_ok_1m = False
        if payloads_5m and warehouse is not None:
            try:
                combined_5m = pd.concat(payloads_5m, axis=0, sort=False, ignore_index=True)
                warehouse.upsert_intraday_summaries(interval="5m", frame=combined_5m)
            except Exception as exc:
                upsert_errors.append(f"5m_upsert:{type(exc).__name__}:{exc}")
                upsert_ok_5m = False
        if upsert_errors:
            # Any interval failure -> demote all ok_symbols to failed/stale,
            # because the freshness gate would otherwise see the in-memory
            # frames as fresh while DuckDB still misses the date.
            if not upsert_ok_1m or not upsert_ok_5m:
                failed_symbols.extend([s for s in ok_symbols if s not in failed_symbols])
                stale_symbols.extend([s for s in ok_symbols if s not in stale_symbols])
                session_incomplete.extend([s for s in ok_symbols if s not in session_incomplete])
                ok_symbols = []

        # Clear the intraday cache on the overlay so the next deep read sees
        # the freshly upserted rows.
        for src in (vendor_overlay, warehouse):
            try:
                fn = getattr(src, "clear_cache", None)
                if callable(fn):
                    fn()
                # Also drop the batch cache on the overlay specifically
                if hasattr(src, "_intraday_batch_cache"):
                    try:
                        src._intraday_batch_cache.clear()  # type: ignore[union-attr]
                    except Exception:
                        pass
            except Exception:
                pass

        report.ok = len(ok_symbols)
        report.failed = len(failed_symbols)
        # skipped here means waived by deadline / upsert path; eligible-ok-failed
        report.skipped = max(0, len(eligible) - len(ok_symbols) - len(failed_symbols))
        report.session_incomplete = sorted(session_incomplete)
        report.stale_symbols = sorted(set(stale_symbols))
        report.source_breakdown = dict(source_counts)
        if not report.capability_probe:
            report.capability_probe = {
                "probed": probed,
                "tushare_ok": tushare_ok,
                "error": probe_error,
            }
        report.detail = {
            "upsert_errors": upsert_errors,
            "deadline_sec": int(deadline_sec),
            "concurrency": int(concurrency),
            "timeout_sec": int(timeout_sec),
            "primary": primary_norm,
            "fallback": fallback_norm,
        }
        report.elapsed_ms = max(1, int((monotonic() - started) * 1000))
        return report
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass
