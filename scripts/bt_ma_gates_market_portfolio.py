"""Market-wide portfolio backtest for ma_gates (MA5x28 & MA7x35 both true).

Compares:
  - ma_gates_signal: enter when both golden-cross-over-5d flags fire (fresh day only)
  - random_matched: same count/date matched random names (control)
  - always_top_liquidity: each day pick most liquid names (weak control)

Entry T+1 open; TP8/SL5/max10; max 3 holdings; costs applied.
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

from evaluate_fall_then_rise import list_universe, read_day_bars  # noqa: E402
from stock_analyzer.config import BacktestMatcherConfig  # noqa: E402
from stock_analyzer.feature.tdx_indicators import compute_fall_then_rise  # noqa: E402

TAKE_PROFIT = 0.08
STOP_LOSS = 0.05
MAX_HOLD_DAYS = 10
MAX_HOLDINGS = 3
POSITION_FRAC = 1.0 / MAX_HOLDINGS
_MATCHER_CONFIG = BacktestMatcherConfig()
SLIPPAGE = float(_MATCHER_CONFIG.slippage_by_strategy.get("trend", 0.0))
COMMISSION = _MATCHER_CONFIG.commission_rate
STAMP = _MATCHER_CONFIG.stamp_tax_rate
TOP_K = 3
START = "2020-01-01"
END = "2026-07-16"
MAX_SYMBOLS = 0  # 0 = all


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

    def metrics(self) -> dict:
        if not self.equity_curve:
            return {"n_trades": 0}
        eq = pd.Series({d: v for d, v in self.equity_curve}, dtype=float).sort_index()
        eq = eq[~eq.index.duplicated(keep="last")]
        rets = eq.pct_change().dropna()
        total_ret = float(eq.iloc[-1] / eq.iloc[0] - 1.0) if len(eq) > 1 else 0.0
        days = max(1, (eq.index[-1] - eq.index[0]).days)
        years = days / 365.25
        ann = (1.0 + total_ret) ** (1.0 / years) - 1.0 if years > 0 and total_ret > -0.999 else 0.0
        peak = eq.cummax()
        dd = float((eq / peak - 1.0).min()) if len(eq) else 0.0
        trade_rets = [t["net_ret"] for t in self.trades]
        wins = [r for r in trade_rets if r > 0]
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
            "years": round(years, 2),
            "avg_daily_ret": round(float(rets.mean()), 6) if len(rets) else 0.0,
            "sharpe_daily": round(
                float(rets.mean() / rets.std() * np.sqrt(242)) if len(rets) and rets.std() > 0 else 0.0,
                3,
            ),
            "start": str(eq.index[0].date()),
            "end": str(eq.index[-1].date()),
        }


def process_symbol(vipdoc: Path, symbol: str, start: pd.Timestamp, end: pd.Timestamp):
    daily = read_day_bars(vipdoc, symbol)
    if daily.empty or len(daily) < 120:
        return symbol, None, []
    flags = compute_fall_then_rise(daily, None, min60_missing_policy="pass")
    ma = flags["ftr_ma5x28"] & flags["ftr_ma7x35"]
    # fresh entry: first day of a True streak
    fresh = ma & (~ma.shift(1, fill_value=False))
    window = (daily.index >= start) & (daily.index <= end)
    signal_dates = daily.index[window & fresh.to_numpy()]
    events = []
    for sig in signal_dates:
        pos = daily.index.get_loc(sig)
        if isinstance(pos, slice):
            pos = pos.stop - 1
        elif isinstance(pos, np.ndarray):
            pos = int(pos[-1])
        else:
            pos = int(pos)
        if pos + 1 >= len(daily):
            continue
        entry_date = daily.index[pos + 1]
        # liquidity proxy: 20d mean amount if available else volume*close
        if "volume" in daily.columns:
            vol = float(daily.iloc[pos]["volume"])
            close = float(daily.iloc[pos]["close"])
            liq = vol * close
        else:
            liq = 0.0
        events.append(
            {
                "symbol": symbol,
                "signal_date": sig,
                "entry_date": entry_date,
                "liq": liq,
            }
        )
    return symbol, daily, events


def build_events(vipdoc: Path, start: str, end: str, max_symbols: int = 0):
    symbols = list_universe(vipdoc)
    if max_symbols and max_symbols > 0:
        symbols = symbols[:max_symbols]
    print(f"universe={len(symbols)}")
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    daily_cache: dict[str, pd.DataFrame] = {}
    events: list[dict] = []

    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = [pool.submit(process_symbol, vipdoc, s, lo, hi) for s in symbols]
        done = 0
        for fut in as_completed(futs):
            symbol, daily, ev = fut.result()
            if daily is not None:
                daily_cache[symbol] = daily
                events.extend(ev)
            done += 1
            if done % 400 == 0:
                print(f"  processed {done}/{len(symbols)} events={len(events)}")

    frame = pd.DataFrame(events)
    if not frame.empty:
        frame = frame.sort_values(["entry_date", "liq"], ascending=[True, False])
    print(f"events={len(frame)} symbols_with_bars={len(daily_cache)}")
    return frame, daily_cache


def daily_topk(events: pd.DataFrame, top_k: int = TOP_K) -> pd.DataFrame:
    if events.empty:
        return events
    return (
        events.sort_values("liq", ascending=False)
        .groupby("entry_date", group_keys=False)
        .head(top_k)
        .reset_index(drop=True)
    )


def random_matched(events: pd.DataFrame, daily_cache: dict, seed: int = 42) -> pd.DataFrame:
    """For each day, sample same count of random symbols that have a bar that day."""
    if events.empty:
        return events
    rng = np.random.default_rng(seed)
    # precompute date -> symbols
    date_to_syms: dict[pd.Timestamp, list[str]] = {}
    for sym, daily in daily_cache.items():
        for d in daily.index:
            date_to_syms.setdefault(d, []).append(sym)

    counts = events.groupby("entry_date").size()
    rows = []
    for day, n in counts.items():
        pool = date_to_syms.get(day, [])
        if not pool:
            continue
        n = min(int(n), len(pool))
        chosen = rng.choice(pool, size=n, replace=False)
        for s in chosen:
            rows.append({"symbol": s, "entry_date": day, "liq": 0.0, "signal_date": day})
    return pd.DataFrame(rows)


def simulate(name: str, selected: pd.DataFrame, daily_cache: dict) -> PortfolioResult:
    result = PortfolioResult(name=name)
    if selected.empty:
        return result
    start = selected["entry_date"].min()
    end = selected["entry_date"].max() + pd.Timedelta(days=MAX_HOLD_DAYS * 2 + 30)
    calendar = sorted(
        {d for daily in daily_cache.values() for d in daily.index if start <= d <= end}
    )
    if not calendar:
        return result

    cash = 1.0
    positions: list[Position] = []
    by_entry = {d: g for d, g in selected.groupby("entry_date")}

    def mark(symbol: str, day: pd.Timestamp) -> float | None:
        daily = daily_cache.get(symbol)
        if daily is None or day not in daily.index:
            return None
        return float(daily.at[day, "close"])

    for day in calendar:
        still = []
        for pos in positions:
            daily = daily_cache.get(pos.symbol)
            if daily is None or day not in daily.index or day <= pos.entry_date:
                still.append(pos)
                continue
            pos.hold_days += 1
            bar = daily.loc[day]
            open_p, high_p, low_p, close_p = map(
                float, (bar["open"], bar["high"], bar["low"], bar["close"])
            )
            reason = exit_px = None
            if open_p <= pos.sl:
                reason, exit_px = "sl_gap", open_p
            elif open_p >= pos.tp:
                reason, exit_px = "tp_gap", open_p
            elif low_p <= pos.sl:
                reason, exit_px = "sl", pos.sl
            elif high_p >= pos.tp:
                reason, exit_px = "tp", pos.tp
            elif pos.hold_days >= MAX_HOLD_DAYS:
                reason, exit_px = "max_hold", close_p
            if reason is None:
                still.append(pos)
            else:
                sell = exit_px * (1 - SLIPPAGE)
                proceeds = pos.qty_notional * (sell / pos.entry_price)
                net = proceeds * (1 - COMMISSION - STAMP)
                cash += net
                result.trades.append(
                    {
                        "symbol": pos.symbol,
                        "entry_date": str(pos.entry_date.date()),
                        "exit_date": str(day.date()),
                        "hold_days": pos.hold_days,
                        "reason": reason,
                        "net_ret": float(net / pos.qty_notional - 1),
                    }
                )
        positions = still

        cands = by_entry.get(day)
        if cands is not None:
            free = MAX_HOLDINGS - len(positions)
            held = {p.symbol for p in positions}
            equity = cash + sum(
                p.qty_notional * ((mark(p.symbol, day) or p.entry_price) / p.entry_price)
                for p in positions
            )
            for _, row in cands.iterrows():
                if free <= 0:
                    break
                symbol = str(row["symbol"])
                if symbol in held:
                    continue
                daily = daily_cache.get(symbol)
                if daily is None or day not in daily.index:
                    continue
                open_p = float(daily.at[day, "open"])
                if open_p <= 0:
                    continue
                alloc = min(equity * POSITION_FRAC, cash)
                if alloc <= 1e-9:
                    break
                fill = open_p * (1 + SLIPPAGE)
                gross = alloc * (1 - COMMISSION)
                cash -= alloc
                positions.append(
                    Position(
                        symbol=symbol,
                        entry_date=day,
                        entry_price=fill,
                        qty_notional=gross,
                        tp=fill * (1 + TAKE_PROFIT),
                        sl=fill * (1 - STOP_LOSS),
                    )
                )
                held.add(symbol)
                free -= 1
                equity = cash + sum(
                    p.qty_notional * ((mark(p.symbol, day) or p.entry_price) / p.entry_price)
                    for p in positions
                )

        mtm = cash + sum(
            p.qty_notional * ((mark(p.symbol, day) or p.entry_price) / p.entry_price)
            for p in positions
        )
        result.equity_curve.append((day, float(mtm)))

    if positions and result.equity_curve:
        last = result.equity_curve[-1][0]
        for pos in positions:
            daily = daily_cache.get(pos.symbol)
            if daily is None:
                continue
            idx = daily.index[daily.index <= last]
            if len(idx) == 0:
                continue
            day = idx[-1]
            close_p = float(daily.at[day, "close"])
            sell = close_p * (1 - SLIPPAGE)
            net = pos.qty_notional * (sell / pos.entry_price) * (1 - COMMISSION - STAMP)
            cash += net
            result.trades.append(
                {
                    "symbol": pos.symbol,
                    "entry_date": str(pos.entry_date.date()),
                    "exit_date": str(day.date()),
                    "hold_days": pos.hold_days,
                    "reason": "force_eod",
                    "net_ret": float(net / pos.qty_notional - 1),
                }
            )
        result.equity_curve.append((last, float(cash)))
    return result


def main() -> None:
    vipdoc = Path(r"D:\通达信\vipdoc")
    out = ROOT / "artifacts" / "analysis" / "ma_gates_market_portfolio.json"

    events, daily_cache = build_events(vipdoc, START, END, MAX_SYMBOLS)
    if events.empty:
        print("no events")
        return

    ma_sel = daily_topk(events, TOP_K)
    rnd_sel = daily_topk(random_matched(ma_sel, daily_cache), TOP_K)

    report = {
        "params": {
            "start": START,
            "end": END,
            "take_profit": TAKE_PROFIT,
            "stop_loss": STOP_LOSS,
            "max_hold_days": MAX_HOLD_DAYS,
            "max_holdings": MAX_HOLDINGS,
            "top_k_per_day": TOP_K,
            "signal": "fresh ma_gates (ftr_ma5x28 & ftr_ma7x35 first day)",
            "entry": "T+1 open",
            "costs": {"slippage": SLIPPAGE, "commission": COMMISSION, "stamp": STAMP},
        },
        "event_summary": {
            "n_events": int(len(events)),
            "n_days": int(events["entry_date"].nunique()),
            "n_symbols": int(events["symbol"].nunique()),
            "selected_ma_gates": int(len(ma_sel)),
            "selected_random": int(len(rnd_sel)),
        },
        "variants": {},
    }

    for name, sel in (("ma_gates_topk", ma_sel), ("random_matched_topk", rnd_sel)):
        print(f"sim {name} selected={len(sel)}")
        res = simulate(name, sel, daily_cache)
        m = res.metrics()
        report["variants"][name] = m
        print(name, m)

    base = report["variants"].get("random_matched_topk", {})
    sig = report["variants"].get("ma_gates_topk", {})
    if base and sig:
        report["delta_ma_gates_vs_random"] = {
            "win_rate_pp": round((sig.get("win_rate", 0) - base.get("win_rate", 0)) * 100, 2),
            "ann_return_pp": round((sig.get("ann_return", 0) - base.get("ann_return", 0)) * 100, 2),
            "total_return_pp": round(
                (sig.get("total_return", 0) - base.get("total_return", 0)) * 100, 2
            ),
            "avg_trade_ret": round(
                sig.get("avg_trade_ret", 0) - base.get("avg_trade_ret", 0), 5
            ),
            "max_drawdown_pp": round(
                (sig.get("max_drawdown", 0) - base.get("max_drawdown", 0)) * 100, 2
            ),
            "sharpe_delta": round(sig.get("sharpe_daily", 0) - base.get("sharpe_daily", 0), 3),
        }

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", out)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
