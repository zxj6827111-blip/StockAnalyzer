"""Timing / path analysis for ma_gates-passing system candidates.

For the system's historical candidates that pass the 先跌后涨 "MA金叉5日外"
conditions (the fall_then_rise bonus population), answer:

  1. Best intraday entry time on day+1 (using local TDX 5-min bars).
  2. Best exit day: holding sweep, exit at close of day 1..15 after entry.
  3. Path shape: average cumulative return curve, probability of sitting
     below entry, dip depth (MAE), and on which day the price typically
     peaks -- does it roll over after ~7 bars?

Entry convention matches the earlier evaluation: entry on the first trading
day after the signal date. Output: artifacts/analysis/ftr_timing_analysis.json

Usage:
    python scripts/analyze_ftr_timing.py --vipdoc "D:/通达信/vipdoc"
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evaluate_fall_then_rise import (  # noqa: E402
    effective_signal_date,
    read_day_bars,
)

from stock_analyzer.data.intraday_summary import read_tdx_minute_bars  # noqa: E402
from stock_analyzer.feature.tdx_indicators import compute_fall_then_rise  # noqa: E402

MAX_HOLD = 15
ENTRY_BUCKETS = (
    "09:35", "09:45", "10:00", "10:30", "11:00", "11:30",
    "13:15", "13:45", "14:15", "14:45", "14:55",
)


def summarize(values: list[float]) -> dict[str, float | int]:
    arr = np.asarray([v for v in values if np.isfinite(v)])
    if arr.size == 0:
        return {"n": 0}
    return {
        "n": int(arr.size),
        "win_rate": round(float((arr > 0).mean()), 4),
        "avg_ret": round(float(arr.mean()), 5),
        "median_ret": round(float(np.median(arr)), 5),
    }


def analyze(vipdoc: Path, snapshots_db: Path) -> dict[str, object]:
    import duckdb

    con = duckdb.connect(str(snapshots_db), read_only=True)
    picks = con.execute(
        "select distinct symbol, decision_time from signal_snapshots"
    ).df()
    con.close()
    picks["decision_time"] = pd.to_datetime(
        picks["decision_time"], utc=True, format="ISO8601"
    )

    # Per-pick records
    rows: list[dict[str, object]] = []
    symbols = sorted(picks["symbol"].unique())

    def process(symbol: str) -> list[dict[str, object]]:
        daily = read_day_bars(vipdoc, symbol)
        if daily.empty or len(daily) < 80:
            return []
        flags = compute_fall_then_rise(daily, None, min60_missing_policy="pass")
        min5 = read_tdx_minute_bars(vipdoc_root=vipdoc, symbol=symbol, interval="5m")
        closes = daily["close"].to_numpy()
        opens = daily["open"].to_numpy()
        lows = daily["low"].to_numpy()
        highs = daily["high"].to_numpy()
        out: list[dict[str, object]] = []
        seen_dates: set[str] = set()
        sub = picks[picks["symbol"] == symbol]
        for _, row in sub.iterrows():
            sig_date = effective_signal_date(row["decision_time"], daily.index)
            if sig_date is None or sig_date not in flags.index:
                continue
            key = str(sig_date.date())
            if key in seen_dates:
                continue
            seen_dates.add(key)
            pos = int(daily.index.get_loc(sig_date))
            entry_pos = pos + 1
            if entry_pos + MAX_HOLD >= len(daily):
                continue  # need the full path for a consistent cohort
            entry_open = float(opens[entry_pos])
            if not np.isfinite(entry_open) or entry_open <= 0:
                continue
            ma_gates = bool(flags.at[sig_date, "ftr_ma5x28"]) and bool(
                flags.at[sig_date, "ftr_ma7x35"]
            )
            rec: dict[str, object] = {
                "symbol": symbol,
                "date": key,
                "ma_gates": ma_gates,
                "entry_open": entry_open,
            }
            # Holding sweep + path (close of entry day + k, k=0..MAX_HOLD-1
            # counts entry day as day 1)
            path = [
                float(closes[entry_pos + k]) / entry_open - 1.0
                for k in range(MAX_HOLD)
            ]
            rec["path"] = path
            window_lows = lows[entry_pos : entry_pos + MAX_HOLD]
            window_highs = highs[entry_pos : entry_pos + MAX_HOLD]
            rec["mae15"] = float(np.min(window_lows)) / entry_open - 1.0
            rec["mfe15"] = float(np.max(window_highs)) / entry_open - 1.0
            rec["mae5"] = float(np.min(window_lows[:5])) / entry_open - 1.0
            rec["mfe5"] = float(np.max(window_highs[:5])) / entry_open - 1.0
            rec["peak_day"] = int(np.argmax(path)) + 1
            rec["trough_day"] = int(np.argmin(window_lows / entry_open)) + 1
            # Intraday entry buckets on the entry day (5-min bars), exit at
            # close of day 5 for comparability.
            exit_close_d5 = float(closes[entry_pos + 4])
            entry_day = daily.index[entry_pos]
            if not min5.empty:
                day_bars = min5[min5.index.normalize() == entry_day]
                if not day_bars.empty:
                    bucket_rets: dict[str, float] = {}
                    for bucket in ENTRY_BUCKETS:
                        cutoff = pd.Timestamp(f"{entry_day.date()} {bucket}")
                        upto = day_bars[day_bars.index <= cutoff]
                        if upto.empty:
                            continue
                        price = float(upto["close"].iloc[-1])
                        if price > 0:
                            bucket_rets[bucket] = exit_close_d5 / price - 1.0
                    if bucket_rets:
                        rec["entry_buckets"] = bucket_rets
            out.append(rec)
        return out

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(process, s): s for s in symbols}
        for i, fut in enumerate(as_completed(futures), 1):
            rows.extend(fut.result())
            if i % 300 == 0:
                print(f"  {i}/{len(symbols)} symbols")

    def cohort_stats(cohort: list[dict[str, object]], label: str) -> dict[str, object]:
        n = len(cohort)
        if n == 0:
            return {"label": label, "n": 0}
        paths = np.asarray([rec["path"] for rec in cohort], dtype=float)
        holding_sweep = {}
        for k in range(MAX_HOLD):
            day_rets = paths[:, k]
            holding_sweep[f"day_{k + 1}"] = {
                "win_rate": round(float((day_rets > 0).mean()), 4),
                "avg_ret": round(float(day_rets.mean()), 5),
                "median_ret": round(float(np.median(day_rets)), 5),
            }
        below_entry = {
            f"day_{k + 1}": round(float((paths[:, k] < 0).mean()), 4)
            for k in range(MAX_HOLD)
        }
        peak_days = np.asarray([rec["peak_day"] for rec in cohort])
        peak_hist = {
            str(day): int((peak_days == day).sum()) for day in range(1, MAX_HOLD + 1)
        }
        entry_timing: dict[str, dict[str, float | int]] = {}
        for bucket in ENTRY_BUCKETS:
            vals = [
                rec["entry_buckets"][bucket]
                for rec in cohort
                if isinstance(rec.get("entry_buckets"), dict)
                and bucket in rec["entry_buckets"]
            ]
            entry_timing[bucket] = summarize(vals)
        entry_timing["next_open_baseline"] = summarize(
            [rec["path"][4] for rec in cohort]
        )
        return {
            "label": label,
            "n": n,
            "avg_cum_path": [round(float(x), 5) for x in paths.mean(axis=0)],
            "median_cum_path": [
                round(float(x), 5) for x in np.median(paths, axis=0)
            ],
            "holding_sweep": holding_sweep,
            "below_entry_prob": below_entry,
            "peak_day_hist": peak_hist,
            "peak_within_7d_ratio": round(float((peak_days <= 7).mean()), 4),
            "avg_mae5": round(float(np.mean([rec["mae5"] for rec in cohort])), 5),
            "avg_mae15": round(float(np.mean([rec["mae15"] for rec in cohort])), 5),
            "avg_mfe5": round(float(np.mean([rec["mfe5"] for rec in cohort])), 5),
            "avg_mfe15": round(float(np.mean([rec["mfe15"] for rec in cohort])), 5),
            "avg_trough_day": round(
                float(np.mean([rec["trough_day"] for rec in cohort])), 2
            ),
            "entry_timing_exit_d5_close": entry_timing,
        }

    gates = [rec for rec in rows if rec["ma_gates"]]
    others = [rec for rec in rows if not rec["ma_gates"]]
    return {
        "max_hold_days": MAX_HOLD,
        "note": (
            "entry day = first trading day after signal date; day_k = close of "
            "k-th trading day counting entry day as day 1; returns relative to "
            "entry-day open unless an intraday bucket price is used"
        ),
        "ma_gates_cohort": cohort_stats(gates, "ma_gates_pass"),
        "non_gates_cohort": cohort_stats(others, "ma_gates_fail"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vipdoc", default=r"D:/通达信/vipdoc")
    parser.add_argument(
        "--snapshots-db", default="artifacts/training/learning_protocol.duckdb"
    )
    parser.add_argument("--out", default="artifacts/analysis/ftr_timing_analysis.json")
    args = parser.parse_args()

    report = analyze(Path(args.vipdoc), Path(args.snapshots_db))
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"report written to {out_path}")


if __name__ == "__main__":
    main()
