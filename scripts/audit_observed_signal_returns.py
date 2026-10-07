"""Read-only audit of logged candidates; never trains or changes serving state.

QFQ open-to-close ratios are research returns, not executable trade P&L. Entry is
strictly after the decision day; horizon 1 exits at that entry day's close.
For legacy NAS snapshots whose local clock was incorrectly tagged UTC, pass
--day-policy stored-wall-clock explicitly. This is a forensic interpretation,
not a repair of historical timestamps. Archive mode reads the split-orientation
JSON export used by the 2026-10-07 NAS audit (snapshots and qfq).
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

HORIZONS = (1, 3, 5, 10)
SCORES = ("meta", "lgbm", "xgb", "board", "completion")


def decision_days(values: pd.Series, policy: str) -> pd.Series:
    if policy == "stored-wall-clock":
        return pd.to_datetime(values.str[:10], format="%Y-%m-%d")
    if policy != "exchange-tz":
        raise ValueError(f"unknown decision day policy: {policy}")
    return (
        pd.to_datetime(values, utc=True, format="ISO8601")
        .dt.tz_convert("Asia/Shanghai")
        .dt.tz_localize(None)
        .dt.normalize()
    )


def deduplicate(snapshots: pd.DataFrame, *, keep: str = "first") -> pd.DataFrame:
    """Deduplicate within capture mode so a replay cannot displace an observation."""
    return (
        snapshots.sort_values(["decision_time", "created_at", "snapshot_id"], kind="stable")
        .drop_duplicates(["feature_capture_mode", "symbol", "decision_day", "strategy"], keep=keep)
        .reset_index(drop=True)
    )


def attach_returns(signals: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    bars = bars.copy()
    bars["date"] = pd.to_datetime(bars["date"]).dt.normalize()
    if bars.duplicated(["symbol", "date"]).any():
        raise ValueError("duplicate symbol/date bars; source is ambiguous")
    if (
        bars[["open", "close", "low"]].isna().any().any()
        or (bars[["open", "close", "low"]] <= 0).any().any()
    ):
        raise ValueError("invalid prices; do not silently filter source defects")
    result = signals.reset_index(drop=True).copy()
    result["entry_day"] = pd.NaT
    for h in HORIZONS:
        result[f"exit_day_{h}"] = pd.NaT
        result[f"return_{h}"] = np.nan
        result[f"mae_{h}"] = np.nan
    series = {s: g.sort_values("date") for s, g in bars.groupby("symbol")}
    for symbol, group in result.groupby("symbol"):
        prices = series.get(symbol)
        if prices is None:
            continue  # Remains missing and is counted in the audit.
        dates = prices["date"].to_numpy(dtype="datetime64[ns]")
        opens, closes, lows = (prices[c].to_numpy(float) for c in ("open", "close", "low"))
        entries = np.searchsorted(dates, group["decision_day"].to_numpy(), side="right")
        has_entry = entries < len(dates)
        result.loc[group.index[has_entry], "entry_day"] = dates[entries[has_entry]]
        for h in HORIZONS:
            exits = entries + h - 1
            valid = exits < len(dates)
            labels = group.index[valid]
            ent, end = entries[valid], exits[valid]
            result.loc[labels, f"exit_day_{h}"] = dates[end]
            result.loc[labels, f"return_{h}"] = closes[end] / opens[ent] - 1
            if len(dates) >= h:
                forward_low = np.lib.stride_tricks.sliding_window_view(lows, h).min(axis=1)
                result.loc[labels, f"mae_{h}"] = forward_low[ent] / opens[ent] - 1
    return result


def summarize(frame: pd.DataFrame, h: int) -> dict[str, Any]:
    valid = frame.dropna(subset=[f"return_{h}"])
    r = valid[f"return_{h}"]
    return {
        "candidates": len(frame),
        "matured": len(valid),
        "missing_entry": int(frame["entry_day"].isna().sum()),
        "pending_or_missing_outcome": len(frame) - len(valid),
        "win_rate_gross": float((r > 0).mean()) if len(r) else None,
        "mean_return_gross": float(r.mean()) if len(r) else None,
        "terminal_loss_gt_5pct": float((r < -0.05).mean()) if len(r) else None,
        "intrahold_low_loss_gt_5pct": float((valid[f"mae_{h}"] < -0.05).mean()) if len(r) else None,
        "cost_scenarios": {
            str(bp): {
                "win_rate": float((r > bp / 10000).mean()) if len(r) else None,
                "mean_return": float(r.mean() - bp / 10000) if len(r) else None,
            }
            for bp in (0, 20, 40)
        },
    }


def daily_ic(frame: pd.DataFrame, score: str, h: int) -> pd.Series:
    values: dict[pd.Timestamp, float] = {}
    for day, group in frame.groupby("decision_day"):
        group = group.dropna(subset=[score, f"return_{h}"])
        if len(group) >= 5 and group[score].nunique() > 1 and group[f"return_{h}"].nunique() > 1:
            values[day] = group[score].rank().corr(group[f"return_{h}"].rank())
    return pd.Series(values, dtype=float).sort_index()


def block_ci(values: pd.Series, *, block: int, seed: int = 20261007) -> list[float] | None:
    """Moving blocks of consecutive observed decision days; handles overlap in returns."""
    array = values.dropna().sort_index().to_numpy(float)
    n = len(array)
    if n < max(10, block * 2):
        return None
    size = min(block, n)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n - size + 1, size=(3000, int(np.ceil(n / size))))
    indices = (starts[..., None] + np.arange(size)).reshape(3000, -1)[:, :n]
    return np.quantile(array[indices].mean(axis=1), [0.025, 0.975]).tolist()


def top_k(frame: pd.DataFrame, score: str, k: int = 5) -> pd.DataFrame:
    if frame[score].isna().any():
        raise ValueError(f"missing scores in {score}; cannot silently shrink the candidate pool")
    # Select before looking at outcomes, and do not select the same stock twice.
    ordered = frame.sort_values(
        ["decision_day", score, "symbol", "strategy", "snapshot_id"],
        ascending=[True, False, True, True, True],
        kind="stable",
    ).drop_duplicates(["decision_day", "symbol"])
    return ordered.groupby("decision_day", sort=True).head(k).copy()


def walk_forward(frame: pd.DataFrame, *, min_history_days: int = 20) -> pd.DataFrame:
    """Fixed diagnostic: mean past 5D daily IC weights on current percentile ranks.

    Every contributing historical day's entire available candidate cross-section
    must have matured strictly before the current decision day. No in-sample fit.
    This protocol is independent of the earlier Qwen probe's unspecified weights.
    """
    records = []
    for day, current in frame.groupby("decision_day"):
        past = frame[frame["decision_day"] < day]
        maturity = past.groupby("decision_day").agg(
            exit=("exit_day_5", "max"), n=("return_5", "size"), known=("return_5", "count")
        )
        available = maturity.index[(maturity["exit"] < day) & (maturity.n == maturity.known)]
        past = past[past["decision_day"].isin(available)]
        ics = {score: daily_ic(past, score, 5) for score in SCORES}
        if any(len(v) < min_history_days for v in ics.values()):
            continue
        weights = pd.Series({score: v.mean() for score, v in ics.items()})
        norm = weights.abs().sum()
        if not np.isfinite(norm) or norm <= 0:
            continue
        current = current.copy()
        current["wf_ic_mix"] = current[list(SCORES)].rank(pct=True).mul(weights / norm).sum(axis=1)
        current["weight_training_last_exit"] = maturity.loc[available, "exit"].max()
        current["weight_history_days"] = len(available)
        records.append(current)
    return pd.concat(records, ignore_index=True) if records else pd.DataFrame()


def ranking_report(frame: pd.DataFrame, h: int) -> dict[str, Any]:
    chosen = {score: top_k(frame, score) for score in ("meta", "completion")}
    if "wf_ic_mix" in frame:
        chosen["wf_ic_mix"] = top_k(frame, "wf_ic_mix")
    # Match dates across all rules and pool; no rule gets an easier maturity window.
    ready = frame.groupby("decision_day")[f"return_{h}"].agg(["size", "count"])
    days = ready.index[ready["size"] == ready["count"]]
    pool = frame[frame["decision_day"].isin(days)]
    baseline = pool.groupby("decision_day")[f"return_{h}"].mean()
    output: dict[str, Any] = {"common_decision_days": len(days), "pool": summarize(pool, h)}
    for score, selected in chosen.items():
        selected = selected[selected["decision_day"].isin(days)]
        result = summarize(selected, h)
        delta = selected.groupby("decision_day")[f"return_{h}"].mean() - baseline
        result["mean_daily_excess_vs_pool"] = float(delta.mean()) if len(delta) else None
        result["paired_block_ci_excess"] = block_ci(delta, block=h)
        output[score] = result
    return output


def build_report(
    s: pd.DataFrame, b: pd.DataFrame, *, start: str, end: str, policy: str
) -> tuple[dict[str, Any], pd.DataFrame]:
    s = s.copy()
    delta = (
        pd.to_datetime(s.decision_time, utc=True, format="ISO8601")
        - pd.to_datetime(s.created_at, utc=True, format="ISO8601")
    ).dt.total_seconds()
    if (
        policy == "exchange-tz"
        and (delta[s.feature_capture_mode == "observed_snapshot"] > 3600).any()
    ):
        raise ValueError(
            "observed decision timestamps postdate creation by over an hour; "
            "audit timezone provenance before using --day-policy stored-wall-clock"
        )
    s["decision_day"] = decision_days(s["decision_time"], policy)
    s = s[s["decision_day"].between(start, end)].copy()
    for score in SCORES:
        column = (
            "model_outputs_json" if score in ("meta", "lgbm", "xgb") else "score_breakdown_json"
        )
        s[score] = s[column].map(lambda v, key=score: json.loads(v).get(key, np.nan))
    first = attach_returns(deduplicate(s), b)
    last = attach_returns(deduplicate(s, keep="last"), b)
    # Reproduce the earlier cross-mode, latest-per-day selection separately.
    legacy = s.sort_values(["decision_time", "created_at", "snapshot_id"]).drop_duplicates(
        ["symbol", "decision_day", "strategy"], keep="last"
    )
    legacy = attach_returns(legacy, b)
    observed = first[first.feature_capture_mode == "observed_snapshot"]
    bars = b.copy()
    bars["date"] = pd.to_datetime(bars["date"]).dt.normalize()
    baseline = bars[bars["date"].between(start, end)][["symbol", "date"]].rename(
        columns={"date": "decision_day"}
    )
    baseline = attach_returns(baseline, b)
    same_names = baseline[baseline.symbol.isin(observed.symbol.unique())]
    report: dict[str, Any] = {
        "protocol": {
            "start": start,
            "end": end,
            "day_policy": policy,
            "primary_dedup": "capture_mode+symbol+day+strategy: earliest observation",
            "entry": "first subsequent bar open; suspension/missing-bar distinction unverified",
            "exit": "entry bar + horizon - 1: QFQ close / QFQ open - 1",
            "trade_simulation": False,
            "top_k": "up to 5 distinct stocks; select before inspecting outcomes",
            "same_names_baseline": "retrospective diagnostic, not a PIT strategy",
            "wf": (
                "past fully matured 5D IC, minimum 20 days, signed mean weights, "
                "current percentile ranks"
            ),
            "costs": "0/20/40 bp round-trip sensitivity; no calibrated execution/slippage model",
            "block_bootstrap": (
                "3000 moving-block draws; block=holding horizon; consecutive observed days"
            ),
            "price_asof": str(bars.date.max().date()),
        },
        "raw_rows": len(s),
        "cross_mode_latest_count": len(legacy),
        "mode_first_counts": first.feature_capture_mode.value_counts().to_dict(),
        "mode_last_counts": last.feature_capture_mode.value_counts().to_dict(),
        "legacy_mode_counts": legacy.feature_capture_mode.value_counts().to_dict(),
        "cohorts": {},
        "ic": {},
        "by_strategy": {},
        "monthly": {},
        "rankings": {},
        "selected_symbols_history": {},
        "matched_baselines": {},
    }
    cohorts = {
        "legacy_all": legacy,
        "legacy_observed": legacy[legacy.feature_capture_mode == "observed_snapshot"],
        "legacy_replayed": legacy[legacy.feature_capture_mode == "replayed_recompute"],
        "observed_first": observed,
        "observed_last": last[last.feature_capture_mode == "observed_snapshot"],
        "all_market": baseline,
        "same_names_all_days": same_names,
    }
    for name, frame in cohorts.items():
        report["cohorts"][name] = {str(h): summarize(frame, h) for h in HORIZONS}
    for score in SCORES:
        report["ic"][score] = {}
        for h in (5, 10):
            ic = daily_ic(observed, score, h)
            report["ic"][score][str(h)] = {
                "days": len(ic),
                "mean": float(ic.mean()) if len(ic) else None,
                "positive_days_ratio": float((ic > 0).mean()) if len(ic) else None,
                "block_ci": block_ci(ic, block=h),
                "unique_values": int(observed[score].nunique()),
            }
    for strategy, frame in observed.groupby("strategy"):
        report["by_strategy"][strategy] = {str(h): summarize(frame, h) for h in (5, 10)}
    matched_cohorts = {
        "observed_first": observed,
        "legacy_observed": cohorts["legacy_observed"],
        **{strategy: frame for strategy, frame in observed.groupby("strategy")},
    }
    for name, frame in matched_cohorts.items():
        report["matched_baselines"][name] = {}
        for h in (5, 10):
            result = {}
            for baseline_name, control in (("market", baseline), ("same_names", same_names)):
                returns = control.groupby("entry_day")[f"return_{h}"].mean()
                win_rates = (
                    control.dropna(subset=[f"return_{h}"])
                    .assign(win=lambda x, horizon=h: x[f"return_{horizon}"] > 0)
                    .groupby("entry_day")
                    .win.mean()
                )
                valid = frame.dropna(subset=[f"return_{h}"]).copy()
                valid["control"] = valid.entry_day.map(returns)
                valid["control_win"] = valid.entry_day.map(win_rates)
                known = valid.dropna(subset=["control"])
                excess = (known[f"return_{h}"] - known.control).groupby(known.decision_day).mean()
                result[baseline_name] = {
                    "matched_rows": len(known),
                    "unmatched_rows": len(valid) - len(known),
                    "signal_win_rate": float((known[f"return_{h}"] > 0).mean()),
                    "control_win_rate": float(known.control_win.mean()),
                    "signal_return": float(known[f"return_{h}"].mean()),
                    "control_return": float(known.control.mean()),
                    "mean_daily_excess": float(excess.mean()),
                    "paired_block_ci_excess": block_ci(excess, block=h),
                }
            report["matched_baselines"][name][str(h)] = result
    for month, frame in observed.groupby(observed["decision_day"].dt.strftime("%Y-%m")):
        report["monthly"][month] = {str(h): summarize(frame, h) for h in (5, 10)}
    for symbol in ("600663", "002832"):
        report["selected_symbols_history"][symbol] = {
            str(h): summarize(observed[observed.symbol == symbol], h) for h in (5, 10)
        }
    for h in (5, 10):
        report["rankings"][str(h)] = ranking_report(observed, h)
    wf = walk_forward(observed)
    report["wf_candidate_days"] = int(wf.decision_day.nunique()) if len(wf) else 0
    report["wf_rankings"] = {str(h): ranking_report(wf, h) for h in (5, 10)} if len(wf) else {}
    if "created_at" in s:
        delta = (
            pd.to_datetime(s.decision_time, utc=True, format="ISO8601")
            - pd.to_datetime(s.created_at, utc=True, format="ISO8601")
        ).dt.total_seconds()
        observed_delta = delta[s.feature_capture_mode == "observed_snapshot"]
        report["timestamp_audit"] = {
            "observed_rows": len(observed_delta),
            "decision_minus_created_median_hours": float(observed_delta.median() / 3600),
            "near_positive_8h_rows": int(observed_delta.between(7.95 * 3600, 8.05 * 3600).sum()),
        }
    return report, first


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="gzip JSON split-frame forensic export")
    parser.add_argument("--protocol-db", type=Path)
    parser.add_argument("--qfq-db", type=Path)
    parser.add_argument("--start", default="2026-07-07")
    parser.add_argument("--end", default="2026-09-30")
    parser.add_argument(
        "--day-policy", choices=["exchange-tz", "stored-wall-clock"], default="exchange-tz"
    )
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.archive:
        content = args.archive.read_bytes()
        archive = json.loads(gzip.decompress(content))
        snapshots = pd.DataFrame(
            archive["snapshots"]["data"], columns=archive["snapshots"]["columns"]
        )
        bars = pd.DataFrame(archive["qfq"]["data"], columns=archive["qfq"]["columns"])
        source = {
            "archive_sha256": hashlib.sha256(content).hexdigest(),
            "source_build_commit": archive.get("source_build_commit"),
        }
    else:
        if not args.protocol_db or not args.qfq_db:
            parser.error("provide --archive, or both --protocol-db and --qfq-db")
        import duckdb

        cfg = {"memory_limit": "450MB", "threads": 1}
        with duckdb.connect(str(args.protocol_db), read_only=True, config=cfg) as conn:
            snapshots = conn.execute(
                """select snapshot_id,symbol,strategy,decision_time,
                created_at,feature_capture_mode,model_outputs_json,score_breakdown_json
                from signal_snapshots where substr(decision_time,1,10) between ? and ?""",
                [str(pd.Timestamp(args.start).date() - pd.Timedelta(days=1)), args.end],
            ).fetchdf()
        with duckdb.connect(str(args.qfq_db), read_only=True, config=cfg) as conn:
            bars = conn.execute(
                """select symbol,date,open,close,low,price_series_mode
                from daily_bars where date between ? and ?""",
                [args.start, args.end],
            ).fetchdf()
        source = {
            "read_only": True,
            "protocol_db": str(args.protocol_db),
            "qfq_db": str(args.qfq_db),
        }
    if bars.empty or not bars.price_series_mode.eq("qfq").all():
        raise ValueError("research-return source must explicitly declare qfq on every bar")
    report, rows = build_report(
        snapshots, bars, start=args.start, end=args.end, policy=args.day_policy
    )
    report["source"] = source
    args.out_dir.mkdir(parents=True, exist_ok=True)
    result = args.out_dir / "signal_return_audit.json"
    if result.exists() or (args.out_dir / "candidate_returns.csv").exists():
        raise FileExistsError("use a new output directory to preserve previous audit evidence")
    result.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    rows.drop(columns=[c for c in rows if c.endswith("_json")]).to_csv(
        args.out_dir / "candidate_returns.csv", index=False
    )
    print(
        json.dumps({"report": str(result), "status": "ANALYSIS_ONLY", "production_changed": False})
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
