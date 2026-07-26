"""Evaluate the 735金叉及趋势 TDX signal, same conventions as the
fall_then_rise evaluation so the two indicators are directly comparable.

  A. Overlay on the system's historical candidates (signal_snapshots).
  B. Whole-market standalone quality 2023-01..2026-07 vs date-matched market
     baseline (the signal is daily-only, so the full long window applies).

Entry: next trading day's open; exit: close of the 5th trading day counting
entry day as day 1 (oc5). cc5 = signal close -> +5 close.

Usage:
    python scripts/evaluate_ma735.py --vipdoc "D:/通达信/vipdoc"
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evaluate_fall_then_rise import (  # noqa: E402
    effective_signal_date,
    forward_returns,
    list_universe,
    read_day_bars,
    summarize,
)

from stock_analyzer.feature.tdx_indicators import compute_ma735_trend  # noqa: E402


def evaluate_snapshots(vipdoc: Path, snapshots_db: Path) -> dict[str, object]:
    import duckdb

    con = duckdb.connect(str(snapshots_db), read_only=True)
    picks = con.execute(
        "select distinct symbol, decision_time from signal_snapshots"
    ).df()
    con.close()
    picks["decision_time"] = pd.to_datetime(
        picks["decision_time"], utc=True, format="ISO8601"
    )

    rows: list[dict[str, object]] = []
    symbols = sorted(picks["symbol"].unique())

    def process(symbol: str) -> list[dict[str, object]]:
        daily = read_day_bars(vipdoc, symbol)
        if daily.empty or len(daily) < 80:
            return []
        flags = compute_ma735_trend(daily)
        oc5, cc5 = forward_returns(daily)
        out: list[dict[str, object]] = []
        sub = picks[picks["symbol"] == symbol]
        for _, row in sub.iterrows():
            sig_date = effective_signal_date(row["decision_time"], daily.index)
            if sig_date is None or sig_date not in flags.index:
                continue
            out.append(
                {
                    "symbol": symbol,
                    "date": str(sig_date.date()),
                    "oc5": float(oc5.get(sig_date, np.nan)),
                    "cc5": float(cc5.get(sig_date, np.nan)),
                    "m735_signal": bool(flags.at[sig_date, "m735_signal"]),
                    "m735_tj1": bool(flags.at[sig_date, "m735_tj1"]),
                    "m735_tj2": bool(flags.at[sig_date, "m735_tj2"]),
                }
            )
        return out

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(process, s): s for s in symbols}
        for i, fut in enumerate(as_completed(futures), 1):
            rows.extend(fut.result())
            if i % 300 == 0:
                print(f"  [snapshots] {i}/{len(symbols)} symbols")

    frame = pd.DataFrame(rows).drop_duplicates(subset=["symbol", "date"])
    frame = frame.dropna(subset=["oc5"])
    return {
        "picks_with_forward_return": int(len(frame)),
        "baseline_all_candidates": summarize(frame["oc5"]),
        "with_m735_signal": summarize(frame.loc[frame["m735_signal"], "oc5"]),
        "with_m735_tj1_only": summarize(frame.loc[frame["m735_tj1"], "oc5"]),
        "with_m735_tj2_only": summarize(frame.loc[frame["m735_tj2"], "oc5"]),
        "pass_rates": {
            col: round(float(frame[col].mean()), 4)
            for col in ("m735_signal", "m735_tj1", "m735_tj2")
        },
    }


def evaluate_market(vipdoc: Path, *, start: str, end: str) -> dict[str, object]:
    symbols = list_universe(vipdoc)
    print(f"  [market] universe: {len(symbols)} symbols")
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)

    signal_rows: list[dict[str, object]] = []
    tj1_flags: list[bool] = []
    base: dict[pd.Timestamp, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])

    def process(symbol: str) -> tuple[list[dict], list[bool], list[tuple]]:
        daily = read_day_bars(vipdoc, symbol)
        if daily.empty or len(daily) < 120:
            return [], [], []
        flags = compute_ma735_trend(daily)
        oc5, cc5 = forward_returns(daily)
        rets = oc5.to_numpy()
        rets_cc = cc5.to_numpy()
        window = (daily.index >= lo) & (daily.index <= hi)
        b = [
            (daily.index[i], rets[i])
            for i in np.flatnonzero(window & np.isfinite(rets))
        ]
        sig, tj1s = [], []
        sig_mask = window & flags["m735_signal"].to_numpy() & np.isfinite(rets)
        for i in np.flatnonzero(sig_mask):
            sig.append(
                {
                    "symbol": symbol,
                    "date": str(daily.index[i].date()),
                    "oc5": float(rets[i]),
                    "cc5": float(rets_cc[i]) if np.isfinite(rets_cc[i]) else None,
                }
            )
            tj1s.append(bool(flags["m735_tj1"].iloc[i]))
        return sig, tj1s, b

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(process, s): s for s in symbols}
        for i, fut in enumerate(as_completed(futures), 1):
            sig, tj1s, b = fut.result()
            signal_rows.extend(sig)
            tj1_flags.extend(tj1s)
            for dt, r in b:
                acc = base[dt]
                acc[0] += r
                acc[1] += 1
                acc[2] += 1.0 if r > 0 else 0.0
            if i % 500 == 0:
                print(f"  [market] {i}/{len(symbols)} symbols")

    frame = pd.DataFrame(signal_rows)
    frame["tj1"] = tj1_flags

    total = sum(int(v[1]) for v in base.values())
    baseline = {
        "n": total,
        "win_rate": round(sum(v[2] for v in base.values()) / total, 4),
        "avg_ret": round(sum(v[0] for v in base.values()) / total, 5),
    }

    excess_vals = []
    for _, row in frame.iterrows():
        acc = base.get(pd.Timestamp(row["date"]))
        if acc and acc[1] > 0:
            excess_vals.append(row["oc5"] - acc[0] / acc[1])
    arr = np.asarray(excess_vals)
    excess = {
        "n": int(arr.size),
        "beat_market_rate": round(float((arr > 0).mean()), 4) if arr.size else None,
        "avg_excess_ret": round(float(arr.mean()), 5) if arr.size else None,
    }

    return {
        "window": {"start": start, "end": end},
        "signal": summarize(frame["oc5"]),
        "signal_cc5": summarize(pd.to_numeric(frame["cc5"], errors="coerce")),
        "signal_tj1_fresh_cross": summarize(frame.loc[frame["tj1"], "oc5"]),
        "signal_tj2_trend_pullback": summarize(frame.loc[~frame["tj1"], "oc5"]),
        "signal_excess_vs_market": excess,
        "market_baseline": baseline,
        "signals_per_month_avg": round(len(frame) / 43.0, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vipdoc", default=r"D:/通达信/vipdoc")
    parser.add_argument(
        "--snapshots-db", default="artifacts/training/learning_protocol.duckdb"
    )
    parser.add_argument("--out", default="artifacts/analysis/ma735_eval.json")
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end", default="2026-07-16")
    args = parser.parse_args()

    vipdoc = Path(args.vipdoc)
    report: dict[str, object] = {"hold_days": 5}
    print("[1/2] overlay on system candidates ...")
    report["snapshot_overlay"] = evaluate_snapshots(vipdoc, Path(args.snapshots_db))
    print("[2/2] whole-market ...")
    report["market_wide"] = evaluate_market(vipdoc, start=args.start, end=args.end)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"report written to {out_path}")


if __name__ == "__main__":
    main()
