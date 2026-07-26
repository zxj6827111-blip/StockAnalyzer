"""Evaluate the 先跌后涨5日外 (fall-then-rise) TDX signal.

Two evaluations:
  A. Overlay on the system's historical candidates (signal_snapshots in the
     learning protocol DB): win rate / forward returns of all candidates vs
     candidates that also pass the fall-then-rise signal on the decision date.
  B. Whole-market standalone signal quality over a recent window (full signal
     incl. 60-min condition) and a longer window (daily-only variant),
     compared against the date-matched market average.

Data source: local TDX vipdoc (lday daily bars, fzline .lc5 5-min bars).

Usage:
    python scripts/evaluate_fall_then_rise.py --vipdoc "D:/通达信/vipdoc" \
        --snapshots-db artifacts/training/learning_protocol.duckdb \
        --out artifacts/analysis/fall_then_rise_eval.json
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

from stock_analyzer.data.intraday_summary import read_tdx_minute_bars  # noqa: E402
from stock_analyzer.feature.tdx_indicators import (  # noqa: E402
    compute_fall_then_rise,
    resample_session_60m,
)

DAY_DTYPE = np.dtype(
    [
        ("date", "<u4"),
        ("open", "<u4"),
        ("high", "<u4"),
        ("low", "<u4"),
        ("close", "<u4"),
        ("amount", "<f4"),
        ("volume", "<u4"),
        ("reserved", "<u4"),
    ]
)

STOCK_PREFIXES_SH = ("600", "601", "603", "605", "688")
STOCK_PREFIXES_SZ = ("000", "001", "002", "003", "300", "301")
HOLD_DAYS = 5


def market_for_symbol(symbol: str) -> str:
    if symbol.startswith(("6", "5", "9")):
        return "sh"
    if symbol.startswith(("0", "1", "2", "3")):
        return "sz"
    return ""


def read_day_bars(vipdoc: Path, symbol: str) -> pd.DataFrame:
    market = market_for_symbol(symbol)
    if not market:
        return pd.DataFrame()
    path = vipdoc / market / "lday" / f"{market}{symbol}.day"
    if not path.exists():
        return pd.DataFrame()
    raw = np.fromfile(path, dtype=DAY_DTYPE)
    if raw.size == 0:
        return pd.DataFrame()
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(
                raw["date"].astype(np.uint32).astype(str),
                format="%Y%m%d",
                errors="coerce",
            ),
            "open": raw["open"].astype(np.float64) / 100.0,
            "high": raw["high"].astype(np.float64) / 100.0,
            "low": raw["low"].astype(np.float64) / 100.0,
            "close": raw["close"].astype(np.float64) / 100.0,
            "volume": raw["volume"].astype(np.float64),
        }
    )
    frame = frame.dropna(subset=["date"]).drop_duplicates("date").set_index("date")
    return frame.sort_index()


def load_min60_close(vipdoc: Path, symbol: str) -> pd.Series | None:
    bars = read_tdx_minute_bars(vipdoc_root=vipdoc, symbol=symbol, interval="5m")
    if bars.empty:
        return None
    m60 = resample_session_60m(bars)
    if m60.empty:
        return None
    return m60["close"]


def forward_returns(daily: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Per signal-date forward returns.

    oc5: buy next trading day's open, sell at close HOLD_DAYS trading days
         after the signal date (T+1 entry).
    cc5: signal-date close to close HOLD_DAYS days later.
    """
    open_next = daily["open"].shift(-1)
    close_exit = daily["close"].shift(-HOLD_DAYS)
    oc5 = close_exit / open_next - 1.0
    cc5 = close_exit / daily["close"] - 1.0
    return oc5, cc5


def summarize(rets: pd.Series) -> dict[str, float | int]:
    rets = rets.dropna()
    if rets.empty:
        return {"n": 0}
    return {
        "n": int(len(rets)),
        "win_rate": round(float((rets > 0).mean()), 4),
        "avg_ret": round(float(rets.mean()), 5),
        "median_ret": round(float(rets.median()), 5),
        "p25": round(float(rets.quantile(0.25)), 5),
        "p75": round(float(rets.quantile(0.75)), 5),
    }


# ---------------------------------------------------------------- Eval A ----


def effective_signal_date(
    decision_time_utc: pd.Timestamp, trading_index: pd.DatetimeIndex
) -> pd.Timestamp | None:
    """Last trading day whose 15:00 close is at or before the decision time."""
    beijing = decision_time_utc.tz_convert("Asia/Shanghai")
    limit = pd.Timestamp(beijing.date())
    if beijing.hour < 15:
        limit -= pd.Timedelta(days=1)
    pos = trading_index.searchsorted(limit, side="right") - 1
    if pos < 0:
        return None
    return trading_index[pos]


DEFAULT_CONDITION_COLUMNS = (
    "ftr_m60_dtg", "ftr_mtm_dtg", "ftr_hist_turn", "ftr_ma5x28",
    "ftr_ma7x35", "ftr_above_ma",
)


def evaluate_snapshots(
    vipdoc: Path,
    snapshots_db: Path,
    *,
    compute_fn=compute_fall_then_rise,
    condition_columns: tuple[str, ...] = DEFAULT_CONDITION_COLUMNS,
) -> dict[str, object]:
    import duckdb

    con = duckdb.connect(str(snapshots_db), read_only=True)
    picks = con.execute(
        "select distinct symbol, strategy, decision_time from signal_snapshots"
    ).df()
    con.close()
    picks["decision_time"] = pd.to_datetime(picks["decision_time"], utc=True, format="ISO8601")

    rows: list[dict[str, object]] = []
    symbols = sorted(picks["symbol"].unique())

    def process(symbol: str) -> list[dict[str, object]]:
        daily = read_day_bars(vipdoc, symbol)
        if daily.empty or len(daily) < 80:
            return []
        min60 = load_min60_close(vipdoc, symbol)
        flags = compute_fn(daily, min60, min60_missing_policy="fail")
        oc5, cc5 = forward_returns(daily)
        out = []
        sub = picks[picks["symbol"] == symbol]
        for _, row in sub.iterrows():
            sig_date = effective_signal_date(row["decision_time"], daily.index)
            if sig_date is None or sig_date not in flags.index:
                continue
            rec = {
                "symbol": symbol,
                "strategy": row["strategy"],
                "date": str(sig_date.date()),
                "oc5": float(oc5.get(sig_date, np.nan)),
                "cc5": float(cc5.get(sig_date, np.nan)),
            }
            for col in ("ftr_signal", "ftr_signal_daily", *condition_columns):
                rec[col] = bool(flags.at[sig_date, col])
            out.append(rec)
        return out

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(process, s): s for s in symbols}
        for i, fut in enumerate(as_completed(futures), 1):
            rows.extend(fut.result())
            if i % 300 == 0:
                print(f"  [snapshots] {i}/{len(symbols)} symbols")

    frame = pd.DataFrame(rows)
    if frame.empty:
        return {"error": "no snapshot rows evaluated"}
    dedup = frame.drop_duplicates(subset=["symbol", "date"]).copy()
    dedup_ret = dedup.dropna(subset=["oc5"])

    result: dict[str, object] = {
        "picks_total": int(len(dedup)),
        "picks_with_forward_return": int(len(dedup_ret)),
        "baseline_all_candidates": summarize(dedup_ret["oc5"]),
        "baseline_all_candidates_cc5": summarize(dedup_ret["cc5"]),
        "with_full_signal": summarize(dedup_ret.loc[dedup_ret["ftr_signal"], "oc5"]),
        "with_daily_only_signal": summarize(
            dedup_ret.loc[dedup_ret["ftr_signal_daily"], "oc5"]
        ),
        "condition_pass_rates": {
            col: round(float(dedup[col].mean()), 4)
            for col in (*condition_columns, "ftr_signal_daily", "ftr_signal")
        },
        "by_strategy": {},
        "single_condition_overlay": {},
    }
    for strat, group in frame.dropna(subset=["oc5"]).groupby("strategy"):
        result["by_strategy"][strat] = {
            "baseline": summarize(group["oc5"]),
            "with_full_signal": summarize(group.loc[group["ftr_signal"], "oc5"]),
        }
    # How each single condition shifts candidate quality on its own.
    for col in condition_columns:
        result["single_condition_overlay"][col] = summarize(
            dedup_ret.loc[dedup_ret[col], "oc5"]
        )
    matched = dedup_ret.loc[dedup_ret["ftr_signal"]]
    result["full_signal_matches"] = matched[
        ["symbol", "date", "strategy", "oc5", "cc5"]
    ].to_dict(orient="records")
    return result


# ---------------------------------------------------------------- Eval B ----


def list_universe(vipdoc: Path) -> list[str]:
    symbols: list[str] = []
    for market, prefixes in (("sh", STOCK_PREFIXES_SH), ("sz", STOCK_PREFIXES_SZ)):
        folder = vipdoc / market / "lday"
        if not folder.exists():
            continue
        for path in folder.iterdir():
            name = path.stem
            code = name[2:]
            if code.startswith(prefixes):
                symbols.append(code)
    return sorted(set(symbols))


def evaluate_market(
    vipdoc: Path,
    *,
    full_start: str,
    full_end: str,
    daily_start: str,
    daily_end: str,
    compute_fn=compute_fall_then_rise,
) -> dict[str, object]:
    symbols = list_universe(vipdoc)
    print(f"  [market] universe: {len(symbols)} symbols")

    signal_rows: list[dict[str, object]] = []
    daily_only_rows: list[dict[str, object]] = []
    # date -> [sum, count, wins] for the market baseline (full window)
    base_full: dict[pd.Timestamp, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    base_daily: dict[pd.Timestamp, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])

    full_lo, full_hi = pd.Timestamp(full_start), pd.Timestamp(full_end)
    dly_lo, dly_hi = pd.Timestamp(daily_start), pd.Timestamp(daily_end)

    def process(symbol: str) -> tuple[list[dict], list[dict], list[tuple], list[tuple]]:
        daily = read_day_bars(vipdoc, symbol)
        if daily.empty or len(daily) < 120:
            return [], [], [], []
        min60 = load_min60_close(vipdoc, symbol)
        flags = compute_fn(daily, min60, min60_missing_policy="fail")
        oc5, cc5 = forward_returns(daily)

        sig_full, sig_daily, b_full, b_daily = [], [], [], []
        window_full = (daily.index >= full_lo) & (daily.index <= full_hi)
        window_daily = (daily.index >= dly_lo) & (daily.index <= dly_hi)
        rets = oc5.to_numpy()
        rets_cc = cc5.to_numpy()
        for mask, bucket in ((window_full, b_full), (window_daily, b_daily)):
            idx = np.flatnonzero(mask & np.isfinite(rets))
            for i in idx:
                bucket.append((daily.index[i], rets[i]))
        for i in np.flatnonzero(window_full & flags["ftr_signal"].to_numpy() & np.isfinite(rets)):
            sig_full.append(
                {
                    "symbol": symbol,
                    "date": str(daily.index[i].date()),
                    "oc5": float(rets[i]),
                    "cc5": float(rets_cc[i]) if np.isfinite(rets_cc[i]) else None,
                }
            )
        daily_mask = window_daily & flags["ftr_signal_daily"].to_numpy() & np.isfinite(rets)
        for i in np.flatnonzero(daily_mask):
            sig_daily.append(
                {
                    "symbol": symbol,
                    "date": str(daily.index[i].date()),
                    "oc5": float(rets[i]),
                    "cc5": float(rets_cc[i]) if np.isfinite(rets_cc[i]) else None,
                }
            )
        return sig_full, sig_daily, b_full, b_daily

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(process, s): s for s in symbols}
        for i, fut in enumerate(as_completed(futures), 1):
            sig_full, sig_daily, b_full, b_daily = fut.result()
            signal_rows.extend(sig_full)
            daily_only_rows.extend(sig_daily)
            for dt, r in b_full:
                acc = base_full[dt]
                acc[0] += r
                acc[1] += 1
                acc[2] += 1.0 if r > 0 else 0.0
            for dt, r in b_daily:
                acc = base_daily[dt]
                acc[0] += r
                acc[1] += 1
                acc[2] += 1.0 if r > 0 else 0.0
            if i % 500 == 0:
                print(f"  [market] {i}/{len(symbols)} symbols")

    def baseline_stats(base: dict[pd.Timestamp, list[float]]) -> dict[str, float | int]:
        total = sum(int(v[1]) for v in base.values())
        if not total:
            return {"n": 0}
        return {
            "n": total,
            "win_rate": round(sum(v[2] for v in base.values()) / total, 4),
            "avg_ret": round(sum(v[0] for v in base.values()) / total, 5),
        }

    def excess(rows: list[dict], base: dict[pd.Timestamp, list[float]]) -> dict[str, float | int]:
        vals = []
        for row in rows:
            dt = pd.Timestamp(row["date"])
            acc = base.get(dt)
            if acc and acc[1] > 0:
                vals.append(row["oc5"] - acc[0] / acc[1])
        if not vals:
            return {"n": 0}
        arr = np.asarray(vals)
        return {
            "n": int(arr.size),
            "beat_market_rate": round(float((arr > 0).mean()), 4),
            "avg_excess_ret": round(float(arr.mean()), 5),
        }

    sig_frame = pd.DataFrame(signal_rows)
    daily_frame = pd.DataFrame(daily_only_rows)
    return {
        "full_signal_window": {"start": full_start, "end": full_end},
        "daily_only_window": {"start": daily_start, "end": daily_end},
        "full_signal": summarize(
            sig_frame["oc5"] if not sig_frame.empty else pd.Series(dtype=float)
        ),
        "full_signal_cc5": summarize(
            pd.to_numeric(sig_frame["cc5"], errors="coerce")
            if not sig_frame.empty
            else pd.Series(dtype=float)
        ),
        "full_signal_excess_vs_market": excess(signal_rows, base_full),
        "market_baseline_full_window": baseline_stats(base_full),
        "daily_only_signal": summarize(
            daily_frame["oc5"] if not daily_frame.empty else pd.Series(dtype=float)
        ),
        "daily_only_signal_cc5": summarize(
            pd.to_numeric(daily_frame["cc5"], errors="coerce")
            if not daily_frame.empty
            else pd.Series(dtype=float)
        ),
        "daily_only_excess_vs_market": excess(daily_only_rows, base_daily),
        "market_baseline_daily_window": baseline_stats(base_daily),
        "full_signal_count_by_month": (
            sig_frame.assign(ym=sig_frame["date"].str[:7]).groupby("ym").size().to_dict()
            if not sig_frame.empty
            else {}
        ),
    }


# ------------------------------------------------------------------ main ----


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vipdoc", default=r"D:/通达信/vipdoc")
    parser.add_argument(
        "--snapshots-db", default="artifacts/training/learning_protocol.duckdb"
    )
    parser.add_argument("--out", default="artifacts/analysis/fall_then_rise_eval.json")
    parser.add_argument("--full-start", default="2026-01-05")
    parser.add_argument("--full-end", default="2026-07-16")
    parser.add_argument("--daily-start", default="2023-01-01")
    parser.add_argument("--daily-end", default="2026-07-16")
    parser.add_argument("--skip-market", action="store_true")
    args = parser.parse_args()

    vipdoc = Path(args.vipdoc)
    report: dict[str, object] = {"hold_days": HOLD_DAYS}

    print("[1/2] evaluating overlay on system candidates ...")
    report["snapshot_overlay"] = evaluate_snapshots(vipdoc, Path(args.snapshots_db))

    if not args.skip_market:
        print("[2/2] evaluating whole-market signal quality ...")
        report["market_wide"] = evaluate_market(
            vipdoc,
            full_start=args.full_start,
            full_end=args.full_end,
            daily_start=args.daily_start,
            daily_end=args.daily_end,
        )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"report written to {out_path}")
    print(json.dumps(
        {k: v for k, v in report.items() if k != "snapshot_overlay"}
        | {
            "snapshot_overlay": {
                k: v
                for k, v in report["snapshot_overlay"].items()
                if k != "full_signal_matches"
            }
        },
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
