"""Benchmark: full-market feature snapshot build vs rolling incremental refresh.

Covers the four operational scenarios from the acceptance plan:
  1. first full build (now parallelized)
  2. a normal trading day (every symbol advances one trade date -> all dirty)
  3. an unchanged day (no new dates -> probe-only, zero dirty)
  4. a partial-failure day (some symbols fail -> snapshot not fully current)

The provider simulates vendor-ZIP latency scaled by the number of bars read
(full window vs PROBE_DAYS probe), which is what the NAS pays per fetch.

Usage:
    .venv-codex312/Scripts/python.exe scripts/bench_snapshot_incremental.py \
        --symbols 5500 --fetch-ms 1.0 --workers 4
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stock_analyzer.config import load_config  # noqa: E402
from stock_analyzer.feature.snapshot import (  # noqa: E402
    build_feature_snapshot,
    load_feature_snapshot,
    snapshot_is_current,
)

_LOOKBACK = 250


class _BenchProvider:
    """Deterministic bars with per-bars-read simulated I/O latency."""

    def __init__(
        self,
        symbol_count: int,
        fetch_ms: float = 1.0,
        seed: int = 42,
        date_offset_days: int = 0,
        failing: set[str] | None = None,
    ) -> None:
        self.symbols = [f"{600000 + i}" for i in range(symbol_count)]
        self.fetch_ms = float(fetch_ms)
        self.seed = seed
        self.date_offset_days = int(date_offset_days)
        self.failing = set(failing or [])
        self.fetch_calls = 0
        self.bars_read = 0
        self._bar_cache: dict[str, pd.DataFrame] = {}

    def fetch_daily_bars(self, symbol: str, lookback_days: int = 120, **kwargs):
        self.fetch_calls += 1
        effective = max(20, int(lookback_days))
        self.bars_read += effective
        if symbol in self.failing:
            raise RuntimeError("simulated_source_failure")
        time.sleep(self.fetch_ms * effective / 250.0 / 1000.0)
        if symbol not in self._bar_cache:
            self._bar_cache[symbol] = self._generate(symbol)
        # The cache is generated far enough into the future; the current date
        # offset decides how much of it is "published" — so a normal trading
        # day appends new bars and an unchanged day returns the same tail.
        last_trade = pd.bdate_range(
            end=pd.Timestamp.today().normalize()
            + pd.Timedelta(days=self.date_offset_days),
            periods=1,
        )[0]
        window = self._bar_cache[symbol].loc[
            self._bar_cache[symbol].index <= last_trade
        ]
        return window.tail(effective)

    def _generate(self, symbol: str) -> pd.DataFrame:
        rng = np.random.default_rng((self.seed + hash(symbol)) % (2**32))
        end = pd.Timestamp.today().normalize() + pd.Timedelta(days=10)
        dates = pd.bdate_range(end=end, periods=_LOOKBACK + 60)
        close = np.cumprod(1 + rng.normal(0.0012, 0.02, size=len(dates))) * 10
        open_price = close * (1 + rng.normal(0, 0.003, size=len(dates)))
        high = np.maximum(open_price, close) * 1.02
        low = np.minimum(open_price, close) * 0.98
        volume = rng.integers(2_000_000, 12_000_000, size=len(dates)).astype(float)
        frame = pd.DataFrame(
            {
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "turnover": volume * close,
                "float_market_cap": np.full(len(dates), 1.2e10),
                "suspended": False,
                "is_st": False,
                "is_delisting_risk": False,
                "roe": np.full(len(dates), 0.10),
                "debt_ratio": np.full(len(dates), 0.4),
                "holder_count": np.full(len(dates), 60_000.0),
                "block_trade_net": np.zeros(len(dates)),
                "financing_balance": np.full(len(dates), 2.5e9),
                "northbound_net": np.zeros(len(dates)),
                "dragon_tiger_flag": np.zeros(len(dates)),
            },
            index=dates,
        )
        frame.index.name = "date"
        return frame

    def latest_daily_dates(self, symbols=None):
        last_trade = pd.bdate_range(
            end=pd.Timestamp.today().normalize()
            + pd.Timedelta(days=self.date_offset_days),
            periods=1,
        )[0].date()
        return {symbol: last_trade for symbol in (symbols or self.symbols)}

    def status(self) -> dict[str, object]:
        return {}


def _report(label: str, seconds: float, provider: _BenchProvider, payload: dict) -> None:
    print(
        f"{label:16s} {seconds:8.1f}s  "
        f"fetch_calls={provider.fetch_calls:6d} bars_read={provider.bars_read:9d}  "
        f"dirty={payload.get('dirty_symbols', 0)} "
        f"refreshed={payload.get('refreshed_count', 0)} "
        f"failed={payload.get('failed_symbols', 0)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", type=int, default=5500)
    parser.add_argument("--fetch-ms", type=float, default=1.0,
                        help="simulated I/O ms for a full 250-day fetch")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--root", default=None, help="snapshot root (tmp if empty)")
    args = parser.parse_args()

    config = load_config("config/default.yaml")
    config.data_source.primary = "bench"
    config.week5.feature_snapshot_max_age_days = 30
    config.week5.feature_snapshot_require_current = True
    config.week5.feature_snapshot_root = str(
        Path(args.root) / "features_light"
        if args.root
        else Path(tempfile.mkdtemp()) / "features_light"
    )

    provider = _BenchProvider(symbol_count=args.symbols, fetch_ms=args.fetch_ms)
    print(
        f"bench: symbols={args.symbols} fetch_ms={args.fetch_ms} "
        f"workers={args.workers} root={config.week5.feature_snapshot_root}"
    )

    # 1. First full build (parallelized fetch + transform).
    provider.fetch_calls = 0
    provider.bars_read = 0
    start = time.perf_counter()
    full = build_feature_snapshot(
        config, provider, symbols=provider.symbols,
        lookback_days=_LOOKBACK, force=True, max_workers=args.workers,
    )
    full_seconds = time.perf_counter() - start
    _report("full build", full_seconds, provider, full)
    assert full["ok"] is True and full["failed_symbols"] == 0

    # 2. Normal trading day: every symbol advances to the next business day.
    #    (Step by 3 calendar days so the next trade date actually differs even
    #    when the run lands on a weekend.)
    provider.date_offset_days += 3
    provider.fetch_calls = 0
    provider.bars_read = 0
    start = time.perf_counter()
    trading_day = build_feature_snapshot(
        config, provider, symbols=provider.symbols,
        lookback_days=_LOOKBACK, max_workers=args.workers,
    )
    trading_seconds = time.perf_counter() - start
    _report("trading day", trading_seconds, provider, trading_day)
    assert trading_day["ok"] is True
    assert trading_day.get("dirty_symbols", 0) > 0, (
        "a normal trading day must mark dirty symbols for refresh"
    )

    # 3. Unchanged day: same dates -> probe-only, zero dirty.
    provider.fetch_calls = 0
    provider.bars_read = 0
    start = time.perf_counter()
    unchanged = build_feature_snapshot(
        config, provider, symbols=provider.symbols,
        lookback_days=_LOOKBACK, max_workers=args.workers,
    )
    unchanged_seconds = time.perf_counter() - start
    _report("unchanged day", unchanged_seconds, provider, unchanged)

    # 4. Partial-failure day: some symbols fail -> not fully current.
    provider.failing = set(provider.symbols[: max(1, args.symbols // 10)])
    provider.date_offset_days += 3
    provider.fetch_calls = 0
    provider.bars_read = 0
    start = time.perf_counter()
    partial = build_feature_snapshot(
        config, provider, symbols=provider.symbols,
        lookback_days=_LOOKBACK, max_workers=args.workers,
    )
    partial_seconds = time.perf_counter() - start
    _report("partial fail", partial_seconds, provider, partial)
    manifest, _ = load_feature_snapshot(config)
    assert manifest is not None
    assert snapshot_is_current(manifest, config) is False, (
        "partial-failure snapshot must not be current"
    )

    speedup = full_seconds / max(trading_seconds, 1e-6)
    print(f"\nsummary: trading-day wall-clock {speedup:.1f}x vs full build")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
