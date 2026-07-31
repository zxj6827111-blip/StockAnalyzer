"""Performance measurement for UniverseCandidateSelector at 5000-symbol scale."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

from stock_analyzer.data.market_warehouse import MarketWarehouse
from stock_analyzer.runtime.universe_candidate_selector import UniverseCandidateSelector

from tests.test_universe_candidate_selector import _make_daily_frame


def main() -> None:
    prefixes = [
        ("000", "SZ_MAIN"),
        ("300", "SZ_GEM"),
        ("600", "SH_MAIN"),
        ("688", "SH_STAR"),
        ("830", "BSE"),
    ]
    spec: dict[str, dict] = {}
    for prefix, _ in prefixes:
        for i in range(1000):
            symbol = f"{prefix}{i + 1:03d}"
            spec[symbol] = {
                "days": 120,
                "start_close": 10.0 + i * 0.01,
                "drift": 0.002,
                "turnover": 30_000_000.0,
                "float_market_cap": 2_000_000_000.0,
                "roe": 0.12,
                "debt_ratio": 0.35,
            }

    import tempfile

    tmp = Path(tempfile.mkdtemp())
    print(f"Building warehouse with {len(spec)} symbols x 120 days...")
    t0 = time.perf_counter()
    warehouse = MarketWarehouse(
        db_path=tmp / "warehouse" / "market.duckdb", package_root=tmp / "package"
    )
    for symbol, kwargs in spec.items():
        warehouse.replace_daily_bars(symbol=symbol, frame=_make_daily_frame(**kwargs))
    t1 = time.perf_counter()
    print(f"Warehouse build: {t1 - t0:.1f}s")

    selector = UniverseCandidateSelector(
        warehouse=warehouse,
        min_history_days=60,
        min_avg_turnover_20=5_000_000.0,
        min_float_market_cap=300_000_000.0,
        exploration_ratio=0.05,
        min_quota_per_in_scope_board=10,
        lookback_days=120,
    )

    symbols = sorted(spec.keys())
    print(f"Selecting 300 from {len(symbols)} symbols...")
    t2 = time.perf_counter()
    result = selector.select(
        symbols=symbols,
        target_size=300,
        trade_date="2026-07-31",
        ruleset_id="a_share_default_v1",
        board_scope=["SSE", "SZSE", "BSE"],
    )
    t3 = time.perf_counter()
    report = result["report"]
    print(f"Selection elapsed: {t3 - t2:.2f}s")
    batch_calls = report.get("batch_calls", "N/A")
    print(f"batch_calls: {batch_calls}")
    print(f"selector_mode: {report['selector_mode']}")
    print(f"input_count: {report['input_count']}")
    print(f"hard_eligible_count: {report['hard_eligible_count']}")
    print(f"selected_count: {report['selected_count']}")
    print(f"core_selected_count: {report['core_selected_count']}")
    print(f"exploration_selected_count: {report['exploration_selected_count']}")
    print(f"selected_by_board: {report['selected_by_board']}")
    print(f"score_distribution: {report['score_distribution']}")
    print(f"input_symbol_hash: {report['input_symbol_hash']}")
    print(f"output_symbol_hash: {report['output_symbol_hash']}")
    print(f"TOTAL (build+select): {t3 - t0:.1f}s")
    assert batch_calls == 1, f"Expected 1 batch call, got {batch_calls}"
    assert t3 - t2 < 30.0, f"Selection took {t3 - t2:.2f}s, exceeds 30s budget"
    print("PERF OK: single batch call, under 30s")


if __name__ == "__main__":
    main()
