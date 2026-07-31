"""Quick perf check with 1000 symbols to estimate 5000-scale timing."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")

import time

from stock_analyzer.data.market_warehouse import MarketWarehouse
from stock_analyzer.runtime.universe_candidate_selector import UniverseCandidateSelector

from tests.test_universe_candidate_selector import _make_daily_frame


def main() -> None:
    prefixes = ["000", "300", "600", "688", "830"]
    spec: dict = {}
    for prefix in prefixes:
        for i in range(1, 201):
            symbol = f"{prefix}{i:03d}"
            spec[symbol] = dict(
                days=100, start_close=10.0 + i * 0.01, drift=0.002,
                turnover=30e6, float_market_cap=2e9, roe=0.12, debt_ratio=0.35,
            )

    tmp = Path(tempfile.mkdtemp())
    wh = MarketWarehouse(db_path=tmp / "w" / "m.duckdb", package_root=tmp / "p")
    t0 = time.perf_counter()
    for s, kw in spec.items():
        wh.replace_daily_bars(symbol=s, frame=_make_daily_frame(**kw))
    t1 = time.perf_counter()
    print(f"Build {len(spec)} symbols: {t1 - t0:.1f}s")

    sel = UniverseCandidateSelector(
        warehouse=wh, min_history_days=60, min_avg_turnover_20=5e6,
        min_float_market_cap=3e8, exploration_ratio=0.05,
        min_quota_per_in_scope_board=10, lookback_days=120,
    )
    syms = sorted(spec.keys())
    t2 = time.perf_counter()
    r = sel.select(
        symbols=syms, target_size=300, trade_date="2026-07-31",
        ruleset_id="r1", board_scope=["SSE", "SZSE", "BSE"],
    )
    t3 = time.perf_counter()
    elapsed = t3 - t2
    report = r["report"]
    print(f"Selection: {elapsed:.2f}s")
    print(f"batch_calls={report['batch_calls']}")
    print(f"selected={report['selected_count']}")
    print(f"selector_mode={report['selector_mode']}")
    print(f"Estimated 5000-scale (x5): {elapsed * 5:.1f}s")


if __name__ == "__main__":
    main()
