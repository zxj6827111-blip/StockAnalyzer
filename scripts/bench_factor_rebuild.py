"""Benchmark the batch-mode factor rebuild ZIP access patterns.

Old path: each symbol reopens the factor ZIP and scans every entry to find
its CSV.  New path: ``_load_factor_entry_map`` scans the ZIP once and the
per-symbol merge uses the pre-built map.  Prints a JSON summary:

    {"symbols": N, "old_ms": ..., "new_ms": ..., "speedup_x": ...}
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.update_vendor_daily_from_tushare import (  # noqa: E402
    FACTORS_QFQ_ARCHIVE,
    _load_factor_entry_map,
    _merge_factor_rows_scaled,
)

_YEARS = (2022, 2023, 2024)
_DATES_PER_YEAR = [f"{m:02d}{d:02d}" for m in range(1, 13) for d in (5, 12, 19, 26, 28)]


def _build_factor_zip(factors_root: Path, symbols: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    factors_root.mkdir(parents=True, exist_ok=True)
    ts_codes: list[str] = []
    with zipfile.ZipFile(
        factors_root / FACTORS_QFQ_ARCHIVE,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=1,
    ) as archive:
        for index in range(symbols):
            exchange = "SH" if index % 2 == 0 else "SZ"
            ts_code = f"{600000 + index:06d}.{exchange}"
            ts_codes.append(ts_code)
            for year in _YEARS:
                lines = ["股票代码,交易日期,复权因子"]
                lines.extend(
                    f"{ts_code},{year}{month_day},{rng.uniform(0.5, 3.0):.6f}"
                    for month_day in _DATES_PER_YEAR
                )
                archive.writestr(f"{year}/{ts_code}.csv", "\n".join(lines) + "\n")
    return ts_codes


def _adj_day(ts_code: str, trade_date: str, factor: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": [ts_code],
            "trade_date": [trade_date],
            "adj_factor": [factor],
        }
    )


def _run_benchmark(symbols: int, seed: int) -> dict[str, object]:
    reps = 3
    with tempfile.TemporaryDirectory(prefix="bench_factor_") as tmp:
        factors_root = Path(tmp) / "复权因子"
        ts_codes = _build_factor_zip(factors_root, symbols, seed)

        def _old_path() -> None:
            for ts_code in ts_codes:
                _merge_factor_rows_scaled(
                    ts_code=ts_code,
                    adj_new_day=_adj_day(ts_code, "20260731", 3.0),
                    adj_old_day=_adj_day(ts_code, "20260730", 2.5),
                    factors_root=factors_root,
                    archive_name=FACTORS_QFQ_ARCHIVE,
                    anchor="latest",
                )

        def _new_path() -> None:
            stored_map = _load_factor_entry_map(factors_root, FACTORS_QFQ_ARCHIVE)
            assert len(stored_map) == symbols
            for ts_code in ts_codes:
                _merge_factor_rows_scaled(
                    ts_code=ts_code,
                    adj_new_day=_adj_day(ts_code, "20260731", 3.0),
                    adj_old_day=_adj_day(ts_code, "20260730", 2.5),
                    factors_root=factors_root,
                    archive_name=FACTORS_QFQ_ARCHIVE,
                    anchor="latest",
                    stored_map=stored_map,
                )

        # Warm the OS file cache, then take the best (least noisy) of several
        # repetitions for each path.
        _old_path()
        old_best = min(_measure(_old_path) for _ in range(reps))
        new_best = min(_measure(_new_path) for _ in range(reps))

    return {
        "symbols": symbols,
        "old_ms": round(old_best, 1),
        "new_ms": round(new_best, 1),
        "speedup_x": round(old_best / new_best, 2),
    }


def _measure(fn: Callable[[], None]) -> float:
    start = time.perf_counter()
    fn()
    return (time.perf_counter() - start) * 1000.0


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols",
        type=int,
        default=500,
        help="Number of symbols in the generated factor ZIP (default 500)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for factor values (default 42)",
    )
    args = parser.parse_args(argv)
    summary = _run_benchmark(symbols=max(1, int(args.symbols)), seed=int(args.seed))
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
