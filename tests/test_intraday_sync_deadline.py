"""Deadline is a hard wall: slow probes/fetches must not block beyond it."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from stock_analyzer.data.intraday_sync import sync_intraday_symbols


def _slow_fetch_provider(probe_sleep: float = 0.05, fetch_sleep: float = 2.0) -> object:
    """First 2 calls (probes) are fast, subsequent fetches are slow."""

    class _Slow:
        def __init__(self) -> None:
            self.calls = 0

        def fetch_minute_bars(self, symbol: str, start_date: object, end_date: object, freq: str = "1min") -> pd.DataFrame:
            self.calls += 1
            if self.calls <= 2:
                time.sleep(probe_sleep)
            else:
                time.sleep(fetch_sleep)
            return pd.DataFrame()

    return _Slow()


def test_deadline_does_not_wait_for_slow_concurrent_fetches(tmp_path: Path) -> None:
    """Concurrent fetches that overrun deadline must not block caller.

    Probes are fast; the 4 concurrent fetches each sleep 2s but deadline
    is 1s — with hard deadline the sync should return well before 4s
    (old bug: shutdown(wait=True) blocked until all 4 workers finished).
    """
    with patch("stock_analyzer.data.intraday_sync._fetch_with_sina", return_value=pd.DataFrame()):
        provider = _slow_fetch_provider(probe_sleep=0.05, fetch_sleep=2.0)
        start = time.monotonic()
        report = sync_intraday_symbols(
            warehouse=None,
            symbols=["600000", "600001", "600002", "600003"],
            required_trade_date="2026-08-18",
            primary="tushare",
            fallback="sina",
            deadline_sec=1,
            concurrency=4,
            timeout_sec=1,
            tushare_provider=provider,
        )
        elapsed = time.monotonic() - start
        assert elapsed < 2.5, f"elapsed {elapsed:.3f}s exceeds hard deadline"
        assert report.elapsed_ms < 2500
