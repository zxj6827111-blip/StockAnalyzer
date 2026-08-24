"""Portfolio-level A/B backtest: baseline vs ma_gates bonus / hard filter.

Uses historical signal_snapshots + TDX daily bars.
Entry: T+1 open; Exit: TP8% / SL5% / max 10 hold days.
Max concurrent holdings = 3, equal weight, costs/slippage applied.

Variants:
  - baseline_topk: each day take top-K by raw score among that day's candidates
  - bonus_rerank_topk: score +=4 if ma_gates, then top-K
  - hard_ma_gates_topk: only ma_gates candidates, top-K by raw score
  - baseline_score50 / bonus_score50: score threshold gate (grade B+)
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from evaluate_fall_then_rise import (  # noqa: E402
    effective_signal_date,
    read_day_bars,
)

from stock_analyzer.config import BacktestMatcherConfig  # noqa: E402
from stock_analyzer.feature.tdx_indicators import compute_fall_then_rise  # noqa: E402

WEIGHTS = {
    "trend": {
        "lgbm": 0.35,
        "xgb": 0.30,
        "meta": 0.20,
        "news": 0.05,
        "board": 0.05,
        "completion": 0.05,
    },
    "monster": {
        "lgbm": 0.25,
        "xgb": 0.25,
        "meta": 0.20,
        "news": 0.10,
        "board": 0.10,
        "completion": 0.10,
    },
    "default": {
        "lgbm": 0.30,
        "xgb": 0.25,
        "meta": 0.15,
        "news": 0.10,
        "board": 0.10,
        "completion": 0.10,
    },
}

TAKE_PROFIT = 0.08
STOP_LOSS = 0.05
MAX_HOLD_DAYS = 10
MAX_HOLDINGS = 3
POSITION_FRAC = 1.0 / MAX_HOLDINGS
_MATCHER_CONFIG = BacktestMatcherConfig()
SLIPPAGE = float(_MATCHER_CONFIG.slippage_by_strategy.get("trend", 0.0))
COMMISSION = _MATCHER_CONFIG.commission_rate
STAMP = _MATCHER_CONFIG.stamp_tax_rate
BONUS_POINTS = 4.0
TOP_K = 3
SCORE_THRESH = 50.0


def score_components(components: dict, strategy: str) -> float:
    weights = WEIGHTS.get(strategy, WEIGHTS["default"])
    total_w = sum(weights.values())
    total = 0.0
    for k, w in weights.items():
        v = max(0.0, min(1.0, float(components.get(k, 0.0))))
        total += v * (w / total_w)
    return total * 100.0


def load_candidates(db_path: Path, vipdoc: Path) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    picks = con.execute(
        """
        select symbol, strategy, decision_time, score_breakdown_json
        from signal_snapshots
        where score_breakdown_json is not null
        """
    ).df()
    con.close()
    picks["decision_time"] = pd.to_datetime(
        picks["decision_time"], utc=True, format="ISO8601"
    )
    symbols = sorted(picks["symbol"].unique())
    print(f"snapshots symbols={len(symbols)} rows={len(picks)}")

    daily_cache: dict[str, pd.DataFrame] = {}

    def load_one(symbol: str) -> tuple[str, pd.DataFrame, pd.DataFrame]:
        daily = read_day_bars(vipdoc, symbol)
        if daily.empty or len(daily) < 80:
            return symbol, pd.DataFrame(), pd.DataFrame()
        flags = compute_fall_then_rise(daily, None, min60_missing_policy="pass")
        return symbol, daily, flags

    flags_cache: dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(load_one, s) for s in symbols]
        done = 0
        for fut in as_completed(futs):
            symbol, daily, flags = fut.result()
            if not daily.empty:
                daily_cache[symbol] = daily
                flags_cache[symbol] = flags
            done += 1
            if done % 200 == 0:
                print(f"  bars loaded {done}/{len(symbols)}")

    rows: list[dict] = []
    for _, row in picks.iterrows():
        symbol = str(row["symbol"])
        daily = daily_cache.get(symbol)
        flags = flags_cache.get(symbol)
        if daily is None or flags is None:
            continue
        sig_date = effective_signal_date(row["decision_time"], daily.index)
        if sig_date is None or sig_date not in flags.index:
            continue
        try:
            components = json.loads(row["score_breakdown_json"])
        except Exception:
            continue
        strategy = str(row["strategy"]).strip().lower()
        raw_score = score_components(components, strategy)
        ma_gates = bool(flags.at[sig_date, "ftr_ma5x28"]) and bool(
            flags.at[sig_date, "ftr_ma7x35"]
        )
        bonus_score = min(100.0, raw_score + BONUS_POINTS) if ma_gates else raw_score
        try:
            pos = daily.index.get_loc(sig_date)
        except KeyError:
            continue
        if isinstance(pos, slice):
            pos = pos.stop - 1
        elif isinstance(pos, np.ndarray):
            pos = int(pos[-1])
        else:
            pos = int(pos)
        if pos + 1 >= len(daily):
            continue
        entry_date = daily.index[pos + 1]
        entry_open = float(daily.iloc[pos + 1]["open"])
        if entry_open <= 0:
            continue
        rows.append(
            {
                "symbol": symbol,
                "strategy": strategy,
                "signal_date": sig_date,
                "entry_date": entry_date,
                "raw_score": raw_score,
                "bonus_score": bonus_score,
                "ma_gates": ma_gates,
            }
        )

    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame, daily_cache
    frame = frame.sort_values("raw_score", ascending=False)
    frame = frame.drop_duplicates(subset=["symbol", "entry_date"], keep="first")
    frame = frame.sort_values(["entry_date", "raw_score"], ascending=[True, False])
    print(
        f"candidates={len(frame)} ma_gates_rate={frame['ma_gates'].mean():.3f} "
        f"score_p50={frame['raw_score'].median():.1f} "
        f"score_p90={frame['raw_score'].quantile(0.9):.1f} "
        f"dates={frame['entry_date'].nunique()}"
    )
    return frame, daily_cache


def select_daily(
    candidates: pd.DataFrame,
    *,
    mode: str,
    top_k: int = TOP_K,
    score_thresh: float | None = None,
) -> pd.DataFrame:
    """Return rows marked selected for entry on their entry_date."""
    if candidates.empty:
        return candidates.copy()
    selected_rows: list[pd.DataFrame] = []
    for entry_date, group in candidates.groupby("entry_date"):
        g = group.copy()
        if mode == "baseline_topk":
            g = g.sort_values("raw_score", ascending=False).head(top_k)
        elif mode == "bonus_rerank_topk":
            g = g.sort_values("bonus_score", ascending=False).head(top_k)
        elif mode == "hard_ma_gates_topk":
            g = g.loc[g["ma_gates"]].sort_values("raw_score", ascending=False).head(top_k)
        elif mode == "baseline_score":
            thr = score_thresh if score_thresh is not None else SCORE_THRESH
            g = g.loc[g["raw_score"] >= thr].sort_values("raw_score", ascending=False)
        elif mode == "bonus_score":
            thr = score_thresh if score_thresh is not None else SCORE_THRESH
            g = g.loc[g["bonus_score"] >= thr].sort_values("bonus_score", ascending=False)
        elif mode == "hard_ma_gates_score":
            thr = score_thresh if score_thresh is not None else SCORE_THRESH
            g = g.loc[g["ma_gates"] & (g["raw_score"] >= thr)].sort_values(
                "raw_score", ascending=False
            )
        else:
            raise ValueError(mode)
        if not g.empty:
            selected_rows.append(g)
    if not selected_rows:
        return candidates.iloc[0:0].copy()
    return pd.concat(selected_rows, ignore_index=True)


@dataclass
class Position:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    qty_notional: float
    tp: float
    sl: float
    hold_days: int = 0


@dataclass
class PortfolioResult:
    name: str
    equity_curve: list[tuple[pd.Timestamp, float]] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    skipped: int = 0

    def metrics(self) -> dict:
        if not self.equity_curve:
            return {"n_trades": 0}
        eq = pd.Series({d: v for d, v in self.equity_curve}, dtype=float).sort_index()
        # collapse duplicate dates (force eod may append same day)
        eq = eq[~eq.index.duplicated(keep="last")]
        rets = eq.pct_change().dropna()
        total_ret = float(eq.iloc[-1] / eq.iloc[0] - 1.0) if len(eq) > 1 else 0.0
        days = max(1, (eq.index[-1] - eq.index[0]).days)
        years = days / 365.25
        if years > 0 and total_ret > -0.999999:
            ann = (1.0 + total_ret) ** (1.0 / years) - 1.0
        else:
            ann = 0.0
        peak = eq.cummax()
        dd = float((eq / peak - 1.0).min()) if len(eq) else 0.0
        trade_rets = [t["net_ret"] for t in self.trades if t.get("net_ret") is not None]
        wins = [r for r in trade_rets if r > 0]
        # annualized from mean trade if enough trades (secondary)
        return {
            "n_trades": len(self.trades),
            "win_rate": round(len(wins) / len(trade_rets), 4) if trade_rets else 0.0,
            "avg_trade_ret": round(float(np.mean(trade_rets)), 5) if trade_rets else 0.0,
            "median_trade_ret": round(float(np.median(trade_rets)), 5) if trade_rets else 0.0,
            "total_return": round(total_ret, 4),
            "ann_return": round(float(ann), 4),
            "max_drawdown": round(dd, 4),
            "final_equity": round(float(eq.iloc[-1]), 4),
            "calendar_days": int(days),
            "avg_daily_ret": round(float(rets.mean()), 6) if len(rets) else 0.0,
            "sharpe_daily": round(
                float(rets.mean() / rets.std() * np.sqrt(242)) if len(rets) and rets.std() > 0 else 0.0,
                3,
            ),
            "skipped_no_slot": self.skipped,
            "start": str(eq.index[0].date()),
            "end": str(eq.index[-1].date()),
        }


def simulate(
    name: str,
    selected: pd.DataFrame,
    daily_cache: dict[str, pd.DataFrame],
) -> PortfolioResult:
    result = PortfolioResult(name=name)
    if selected.empty:
        return result

    start = selected["entry_date"].min()
    end = selected["entry_date"].max() + pd.Timedelta(days=MAX_HOLD_DAYS * 2 + 20)
    calendar = sorted(
        {
            d
            for daily in daily_cache.values()
            for d in daily.index
            if start - pd.Timedelta(days=5) <= d <= end
        }
    )
    if not calendar:
        return result

    cash = 1.0
    positions: list[Position] = []
    by_entry = {
        d: g.sort_values("raw_score", ascending=False)
        for d, g in selected.groupby("entry_date")
    }

    def mark_price(symbol: str, day: pd.Timestamp) -> float | None:
        daily = daily_cache.get(symbol)
        if daily is None or day not in daily.index:
            return None
        return float(daily.at[day, "close"])

    def try_exit(pos: Position, day: pd.Timestamp, bar: pd.Series) -> dict | None:
        open_p = float(bar["open"])
        high_p = float(bar["high"])
        low_p = float(bar["low"])
        close_p = float(bar["close"])
        reason = None
        exit_px = None
        if open_p <= pos.sl:
            reason, exit_px = "stop_loss_gap", open_p
        elif open_p >= pos.tp:
            reason, exit_px = "take_profit_gap", open_p
        elif low_p <= pos.sl:
            reason, exit_px = "stop_loss", pos.sl
        elif high_p >= pos.tp:
            reason, exit_px = "take_profit", pos.tp
        elif pos.hold_days >= MAX_HOLD_DAYS:
            reason, exit_px = "max_hold", close_p
        if reason is None:
            return None
        sell_fill = exit_px * (1.0 - SLIPPAGE)
        proceeds = pos.qty_notional * (sell_fill / pos.entry_price)
        cost = proceeds * (COMMISSION + STAMP)
        net_proceeds = proceeds - cost
        net_ret = net_proceeds / pos.qty_notional - 1.0
        return {
            "symbol": pos.symbol,
            "entry_date": str(pos.entry_date.date()),
            "exit_date": str(day.date()),
            "hold_days": pos.hold_days,
            "reason": reason,
            "net_ret": float(net_ret),
            "net_proceeds": float(net_proceeds),
            "variant": name,
        }

    for day in calendar:
        still: list[Position] = []
        for pos in positions:
            daily = daily_cache.get(pos.symbol)
            if daily is None or day not in daily.index or day <= pos.entry_date:
                still.append(pos)
                continue
            pos.hold_days += 1
            trade = try_exit(pos, day, daily.loc[day])
            if trade is None:
                still.append(pos)
            else:
                cash += trade["net_proceeds"]
                result.trades.append(trade)
        positions = still

        cands = by_entry.get(day)
        if cands is not None:
            free_slots = MAX_HOLDINGS - len(positions)
            held = {p.symbol for p in positions}
            if free_slots <= 0:
                result.skipped += int(len(cands))
            else:
                # mark equity for sizing
                equity_now = cash
                for p in positions:
                    px = mark_price(p.symbol, day) or p.entry_price
                    equity_now += p.qty_notional * (px / p.entry_price)
                for _, row in cands.iterrows():
                    if free_slots <= 0:
                        result.skipped += 1
                        continue
                    symbol = str(row["symbol"])
                    if symbol in held:
                        continue
                    daily = daily_cache.get(symbol)
                    if daily is None or day not in daily.index:
                        continue
                    open_p = float(daily.at[day, "open"])
                    if open_p <= 0:
                        continue
                    alloc = min(equity_now * POSITION_FRAC, cash)
                    if alloc <= 1e-6:
                        result.skipped += 1
                        continue
                    buy_fill = open_p * (1.0 + SLIPPAGE)
                    buy_cost = alloc * COMMISSION
                    gross = alloc - buy_cost
                    if gross <= 0:
                        continue
                    cash -= alloc
                    positions.append(
                        Position(
                            symbol=symbol,
                            entry_date=day,
                            entry_price=buy_fill,
                            qty_notional=gross,
                            tp=buy_fill * (1.0 + TAKE_PROFIT),
                            sl=buy_fill * (1.0 - STOP_LOSS),
                            hold_days=0,
                        )
                    )
                    held.add(symbol)
                    free_slots -= 1
                    # refresh equity_now roughly
                    equity_now = cash + sum(
                        p.qty_notional
                        * ((mark_price(p.symbol, day) or p.entry_price) / p.entry_price)
                        for p in positions
                    )

        mtm = cash
        for pos in positions:
            px = mark_price(pos.symbol, day) or pos.entry_price
            mtm += pos.qty_notional * (px / pos.entry_price)
        result.equity_curve.append((day, float(mtm)))

    if positions and result.equity_curve:
        last_day = result.equity_curve[-1][0]
        for pos in list(positions):
            daily = daily_cache.get(pos.symbol)
            if daily is None:
                continue
            idx = daily.index[daily.index <= last_day]
            if len(idx) == 0:
                continue
            day = idx[-1]
            close_p = float(daily.at[day, "close"])
            sell_fill = close_p * (1.0 - SLIPPAGE)
            proceeds = pos.qty_notional * (sell_fill / pos.entry_price)
            net_proceeds = proceeds - proceeds * (COMMISSION + STAMP)
            cash += net_proceeds
            result.trades.append(
                {
                    "symbol": pos.symbol,
                    "entry_date": str(pos.entry_date.date()),
                    "exit_date": str(day.date()),
                    "hold_days": pos.hold_days,
                    "reason": "force_eod",
                    "net_ret": float(net_proceeds / pos.qty_notional - 1.0),
                    "net_proceeds": float(net_proceeds),
                    "variant": name,
                }
            )
        result.equity_curve.append((last_day, float(cash)))

    return result


def main() -> None:
    vipdoc = Path(r"D:\通达信\vipdoc")
    db = ROOT / "artifacts" / "training" / "learning_protocol.duckdb"
    out = ROOT / "artifacts" / "analysis" / "ma_gates_portfolio_ab.json"

    candidates, daily_cache = load_candidates(db, vipdoc)
    if candidates.empty:
        print("no candidates")
        return

    modes = [
        ("baseline_topk", "baseline_topk", None),
        ("bonus_rerank_topk", "bonus_rerank_topk", None),
        ("hard_ma_gates_topk", "hard_ma_gates_topk", None),
        ("baseline_score50", "baseline_score", 50.0),
        ("bonus_score50", "bonus_score", 50.0),
        ("hard_ma_gates_score50", "hard_ma_gates_score", 50.0),
        ("baseline_score45", "baseline_score", 45.0),
        ("bonus_score45", "bonus_score", 45.0),
    ]

    report: dict = {
        "params": {
            "take_profit": TAKE_PROFIT,
            "stop_loss": STOP_LOSS,
            "max_hold_days": MAX_HOLD_DAYS,
            "max_holdings": MAX_HOLDINGS,
            "top_k": TOP_K,
            "slippage": SLIPPAGE,
            "commission": COMMISSION,
            "stamp": STAMP,
            "bonus_points": BONUS_POINTS,
            "entry": "T+1 open",
            "data": "signal_snapshots + TDX vipdoc daily",
            "period": {
                "min": str(candidates["entry_date"].min().date()),
                "max": str(candidates["entry_date"].max().date()),
            },
        },
        "candidate_summary": {
            "n": int(len(candidates)),
            "ma_gates_rate": round(float(candidates["ma_gates"].mean()), 4),
            "raw_score_mean": round(float(candidates["raw_score"].mean()), 2),
            "raw_score_p50": round(float(candidates["raw_score"].median()), 2),
            "raw_score_p90": round(float(candidates["raw_score"].quantile(0.9)), 2),
            "n_days": int(candidates["entry_date"].nunique()),
        },
        "variants": {},
    }

    for name, mode, thr in modes:
        print(f"select+sim {name} ...")
        selected = select_daily(candidates, mode=mode, top_k=TOP_K, score_thresh=thr)
        print(f"  selected={len(selected)}")
        res = simulate(name, selected, daily_cache)
        m = res.metrics()
        m["selected_signals"] = int(len(selected))
        report["variants"][name] = m
        print(" ", m)

    base = report["variants"].get("baseline_topk", {})
    for name, m in report["variants"].items():
        if name == "baseline_topk" or not base.get("n_trades"):
            continue
        if not m.get("n_trades"):
            continue
        report["variants"][name]["delta_vs_baseline_topk"] = {
            "win_rate_pp": round((m.get("win_rate", 0) - base.get("win_rate", 0)) * 100, 2),
            "ann_return_pp": round((m.get("ann_return", 0) - base.get("ann_return", 0)) * 100, 2),
            "total_return_pp": round(
                (m.get("total_return", 0) - base.get("total_return", 0)) * 100, 2
            ),
            "avg_trade_ret": round(
                m.get("avg_trade_ret", 0) - base.get("avg_trade_ret", 0), 5
            ),
            "max_drawdown_pp": round(
                (m.get("max_drawdown", 0) - base.get("max_drawdown", 0)) * 100, 2
            ),
        }

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", out)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
