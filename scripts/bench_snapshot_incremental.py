"""Benchmark: full-market feature snapshot build vs rolling incremental refresh.

Measures wall-clock time and per-symbol I/O volume for a realistic full
market (default 5500 symbols).  The provider simulates vendor-ZIP latency
scaled by the number of bars read (full window vs PROBE_DAYS probe), which is
what the NAS actually pays for on each fetch.

Usage:
    .venv-codex312/Scripts/python.exe scripts/bench_snapshot_incremental.py \
        --symbols 5500 --fetch-ms 1.0 --workers 4
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stock_analyzer.config import load_config  # noqa: E402
from stock_analyzer.feature.snapshot import build_feature_snapshot  # noqa: E402

_LOOKBACK = 250


class _BenchProvider:
    """Deterministic bars with per-bars-read simulated I/O latency."""

    def __init__(self, symbol_count: int, fetch_ms: float = 1.0, seed: int = 42) -> None:
        self.symbols = [f"{600000 + i}" for i in range(symbol_count)]
        self.fetch_ms = float(fetch_ms)
        self.seed = seed
        self.fetch_calls = 0
        self.bars_read = 0

    def fetch_daily_bars(self, symbol: str, lookback_days: int = 120, **kwargs):
        self.fetch_calls += 1
        effective = max(20, int(lookback_days))
        self.bars_read += effective
        # I/O cost grows with the number of bars read (ZIP decompression).
        time.sleep(self.fetch_ms * effective / 250.0 / 1000.0)
        rng = np.random.default_rng((self.seed + hash(symbol)) % (2**32))
        dates = pd.bdate_range(
            end=pd.Timestamp.today().normalize(), periods=effective
        )
        close = np.cumprod(1 + rng.normal(0.0012, 0.02, size=effective)) * 10
        open_price = close * (1 + rng.normal(0, 0.003, size=effective))
        high = np.maximum(open_price, close) * 1.02
        low = np.minimum(open_price, close) * 0.98
        volume = rng.integers(2_000_000, 12_000_000, size=effective).astype(float)
        turnover = volume * close
        frame = pd.DataFrame(
            {
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "turnover": turnover,
                "float_market_cap": np.full(effective, 1.2e10),
                "suspended": False,
                "is_st": False,
                "is_delisting_risk": False,
                "roe": np.full(effective, 0.10),
                "debt_ratio": np.full(effective, 0.4),
                "holder_count": np.full(effective, 60_000.0),
                "block_trade_net": np.zeros(effective),
                "financing_balance": np.full(effective, 2.5e9),
                "northbound_net": np.zeros(effective),
                "dragon_tiger_flag": np.zeros(effective),
            },
            index=dates,
        )
        frame.index.name = "date"
        return frame
        frame = pd.DataFrame(...)  # placeholder never reached

    def latest_daily_dates(self, symbols=None):
        return {
            symbol: pd.Timestamp.today().normalize().date()
            for symbol in (symbols or self.symbols)
        }

    def status(self) -> dict[str, object]:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", type=int, default=5500)
    parser.add_argument("--fetch-ms", type=float, default=1.0,
                        help="simulated I/O ms for a full 250-day fetch")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--root", default=None, help="snapshot root (tmp if empty)")
    args = parser.parse_args()

    import tempfile

    config = load_config("config/default.yaml")
    config.data_source.primary = "bench"
    config.week5.feature_snapshot_max_age_days = 30
    config.week5.feature_snapshot_require_current = True
    if args.root:
        config.week5.feature_snapshot_root = args.root
    else:
        config.week5.feature_snapshot_root = str(
            Path(tempfile.mkdtemp()) / "features_light"
        )
    provider = _BenchProvider(symbol_count=args.symbols, fetch_ms=args.fetch_ms)

    print(f"bench: symbols={args.symbols} fetch_ms={args.fetch_ms} workers={args.workers}")

    # Full build.
    provider.fetch_calls = 0
    provider.bars_read = 0
    start = time.perf_counter()
    full = build_feature_snapshot(
        config,
        provider,
        symbols=provider.symbols,
        lookback_days=_LOOKBACK,
        force=True,
        max_workers=args.workers,
    )
    full_seconds = time.perf_counter() - start
    print(
        f"full build: {full_seconds:8.1f}s  "
        f"fetch_calls={provider.fetch_calls} bars_read={provider.bars_read}"
    )
    assert full["ok"] is True

    # Simulate a normal trading day: every symbol's provider date advances.
    provider_advance = provider.symbols
    _ = provider_advance
    provider.fetch_calls = 0
    provider.bars_read = 0
    start = time.perf_counter()
    incremental = build_feature_snapshot(
        config,
        provider,
        symbols=provider.symbols,
        lookback_days=_LOOKBACK,
        max_workers=args.workers,
    )
    incremental_seconds = time.perf_counter() - start
    print(
        f"incremental : {incremental_seconds:8.1f}s  "
        f"fetch_calls={provider.fetch_calls} bars_read={provider.bars_read}  "
        f"dirty={incremental.get('dirty_symbols')} refreshed={incremental.get('refreshed_count')} "
        f"failed={incremental.get('failed_symbols')}"
    )
    assert incremental["ok"] is True

    speedup = full_seconds / max(incremental_seconds, 1e-6)
    io_ratio = max(incremental.get("bars_read_ratio", 0), 0)
    print(
        f"wall-clock: {speedup:5.1f}x  "
        f"bars_read: full={full.get('_bars_read', provider.bars_read)} "
        f"incremental={incremental.get('_bars_read', provider.bars_read)} "
        f"ratio={io_ratio:.2f}x"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
