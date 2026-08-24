"""Optimization grid on top of model-score + ma_gates joint backtest.

Reuses the same panel construction / walk-forward LGBM as
bt_model60_ma_gates_joint.py, then sweeps:
  - score threshold: 60 / 65 / 70
  - max holdings: 1 / 2 / 3
  - ma_gates: off / +4 bonus gate / rerank only
"""

from __future__ import annotations

import json
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from evaluate_fall_then_rise import list_universe, read_day_bars  # noqa: E402

from stock_analyzer.config import BacktestMatcherConfig  # noqa: E402
from stock_analyzer.feature.engineer import FeatureEngineer  # noqa: E402
from stock_analyzer.feature.tdx_indicators import compute_fall_then_rise  # noqa: E402
from stock_analyzer.labels.soup import build_soup_labels  # noqa: E402

warnings.filterwarnings("ignore")

VIPDOC = Path(r"D:\通达信\vipdoc")
OUT = ROOT / "artifacts" / "analysis" / "model_opt_grid.json"

MAX_SYMBOLS = 400
MIN_BARS = 400
PANEL_START = "2018-01-01"
START_EVAL = "2021-01-01"
END_EVAL = "2026-07-16"
TRAIN_DAYS = 504
TEST_DAYS = 126
STEP_DAYS = 126
MIN_TRAIN_ROWS = 5000

TAKE_PROFIT = 0.08
STOP_LOSS = 0.05
MAX_HOLD_DAYS = 10
_MATCHER_CONFIG = BacktestMatcherConfig()
SLIPPAGE = float(_MATCHER_CONFIG.slippage_by_strategy.get("trend", 0.0))
COMMISSION = _MATCHER_CONFIG.commission_rate
STAMP = _MATCHER_CONFIG.stamp_tax_rate
BONUS = 4.0
TOP_K_BUFFER = 5

THRESHOLDS = [60.0, 65.0, 70.0]
HOLDINGS = [1, 2, 3]
MODES = ["baseline", "bonus_gate", "rerank"]

LGBM_PARAMS = {
    "objective": "binary",
    "metric": "auc",
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_data_in_leaf": 80,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "verbosity": -1,
    "seed": 42,
}


def prepare_bars(daily: pd.DataFrame) -> pd.DataFrame:
    bars = daily.copy()
    bars["turnover"] = bars["volume"].astype(float) * bars["close"].astype(float)
    bars["float_market_cap"] = bars["close"].astype(float) * 1.0e9
    return bars


def pick_universe(vipdoc: Path, max_symbols: int) -> list[str]:
    symbols = list_universe(vipdoc)
    scored: list[tuple[float, str]] = []

    def one(sym: str) -> tuple[float, str] | None:
        daily = read_day_bars(vipdoc, sym)
        if daily.empty or len(daily) < MIN_BARS:
            return None
        tail = daily.tail(60)
        liq = float((tail["volume"] * tail["close"]).mean())
        if liq <= 0:
            return None
        return liq, sym

    with ThreadPoolExecutor(max_workers=12) as pool:
        futs = [pool.submit(one, s) for s in symbols]
        done = 0
        for fut in as_completed(futs):
            item = fut.result()
            if item is not None:
                scored.append(item)
            done += 1
            if done % 1000 == 0:
                print(f"  universe scan {done}/{len(symbols)}")
    scored.sort(reverse=True)
    chosen = [s for _, s in scored[:max_symbols]]
    print(f"universe chosen={len(chosen)}")
    return chosen


def build_symbol_panel(vipdoc: Path, symbol: str, engineer: FeatureEngineer):
    daily = read_day_bars(vipdoc, symbol)
    if daily.empty or len(daily) < MIN_BARS:
        return None
    bars = prepare_bars(daily)
    try:
        feats = engineer.transform(bars)
    except Exception:
        return None
    labels = build_soup_labels(
        bars=bars,
        take_profit_pct=TAKE_PROFIT,
        stop_loss_pct=STOP_LOSS,
        horizon_days=MAX_HOLD_DAYS,
        price_basis="close",
        exclude_untradable=False,
    )
    flags = compute_fall_then_rise(daily, None, min60_missing_policy="pass")
    ma_gates = (flags["ftr_ma5x28"] & flags["ftr_ma7x35"]).astype(bool)
    aligned = feats.join(labels.rename("label"), how="inner")
    aligned["ma_gates"] = ma_gates.reindex(aligned.index).fillna(False).astype(bool)
    aligned["open"] = bars["open"].reindex(aligned.index)
    aligned["high"] = bars["high"].reindex(aligned.index)
    aligned["low"] = bars["low"].reindex(aligned.index)
    aligned["close"] = bars["close"].reindex(aligned.index)
    aligned["volume"] = bars["volume"].reindex(aligned.index)
    aligned = aligned.replace([np.inf, -np.inf], np.nan).dropna(subset=["label"])
    aligned = aligned.loc[aligned.index >= pd.Timestamp(PANEL_START)]
    if len(aligned) < 200:
        return None
    aligned = aligned.reset_index()
    if "date" not in aligned.columns:
        aligned = aligned.rename(columns={aligned.columns[0]: "date"})
    aligned["date"] = pd.to_datetime(aligned["date"])
    aligned["symbol"] = symbol
    for col in aligned.columns:
        if col in {"symbol", "ma_gates", "label", "date"}:
            continue
        if pd.api.types.is_float_dtype(aligned[col]):
            aligned[col] = aligned[col].astype(np.float32)
    return aligned


def build_panel(vipdoc: Path, symbols: list[str]) -> tuple[pd.DataFrame, list[str]]:
    engineer = FeatureEngineer()
    frames: list[pd.DataFrame] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(build_symbol_panel, vipdoc, s, engineer) for s in symbols]
        done = 0
        for fut in as_completed(futs):
            frame = fut.result()
            if frame is not None:
                frames.append(frame)
            done += 1
            if done % 50 == 0:
                print(f"  panel {done}/{len(symbols)} kept={len(frames)}")
    panel = pd.concat(frames, axis=0, ignore_index=True)
    panel["date"] = pd.to_datetime(panel["date"])
    panel = panel.sort_values(["date", "symbol"])
    drop = {"label", "ma_gates", "open", "high", "low", "close", "volume", "symbol", "date"}
    feature_cols = [
        c for c in panel.columns if c not in drop and pd.api.types.is_numeric_dtype(panel[c])
    ]
    print(
        f"panel rows={len(panel)} symbols={panel['symbol'].nunique()} "
        f"feats={len(feature_cols)} mem_mb={panel.memory_usage(deep=True).sum()/1e6:.0f}"
    )
    return panel, feature_cols


def walk_forward_scores(panel: pd.DataFrame, feature_cols: list[str]) -> pd.DataFrame:
    cal = pd.DatetimeIndex(sorted(panel["date"].unique()))
    start = pd.Timestamp(START_EVAL)
    end = pd.Timestamp(END_EVAL)
    test_start_pos = int(np.searchsorted(cal.values, np.datetime64(start)))
    if test_start_pos < TRAIN_DAYS:
        test_start_pos = TRAIN_DAYS

    preds: list[pd.DataFrame] = []
    pos = test_start_pos
    fold = 0
    while pos < len(cal):
        train_lo = max(0, pos - TRAIN_DAYS)
        test_hi = min(len(cal), pos + TEST_DAYS)
        train_dates = set(cal[train_lo:pos])
        test_dates = cal[pos:test_hi]
        if len(test_dates) == 0:
            break
        train = panel.loc[panel["date"].isin(train_dates)].dropna(subset=["label"])
        test = panel.loc[panel["date"].isin(set(test_dates))]
        if len(train) < MIN_TRAIN_ROWS or train["label"].nunique() < 2:
            print(f"  fold{fold}: skip train={len(train)}")
            pos += STEP_DAYS
            fold += 1
            continue
        x_train = np.nan_to_num(
            train[feature_cols].to_numpy(dtype=np.float32), nan=0.0, posinf=0.0, neginf=0.0
        )
        y_train = (train["label"].to_numpy(dtype=float) >= 0.5).astype(int)
        model = lgb.train(
            LGBM_PARAMS,
            lgb.Dataset(x_train, label=y_train, free_raw_data=True),
            num_boost_round=100,
        )
        test_x = np.nan_to_num(
            test[feature_cols].to_numpy(dtype=np.float32), nan=0.0, posinf=0.0, neginf=0.0
        )
        proba = model.predict(test_x)
        out = test[
            ["date", "symbol", "ma_gates", "open", "high", "low", "close", "volume"]
        ].copy()
        out["score"] = (proba * 100.0).astype(np.float32)
        preds.append(out)
        print(
            f"  fold{fold}: train={len(train)} pos={y_train.mean():.3f} test={len(test)} "
            f"{test_dates[0].date()}->{test_dates[-1].date()} "
            f"p50={float(np.median(proba*100)):.1f} p90={float(np.quantile(proba*100,0.9)):.1f}"
        )
        pos += STEP_DAYS
        fold += 1
        if test_dates[-1] >= end:
            break

    scored = pd.concat(preds, ignore_index=True)
    scored = scored.loc[(scored["date"] >= start) & (scored["date"] <= end)]
    scored = scored.sort_values(["date", "score"], ascending=[True, False])
    print(
        f"scored rows={len(scored)} days={scored['date'].nunique()} "
        f"ge60={(scored['score']>=60).mean():.4f} ge65={(scored['score']>=65).mean():.4f} "
        f"ge70={(scored['score']>=70).mean():.4f}"
    )
    return scored


def select_signals(scored: pd.DataFrame, mode: str, thresh: float) -> pd.DataFrame:
    rows = []
    for day, g in scored.groupby("date"):
        if mode == "baseline":
            pick = g.loc[g["score"] >= thresh].sort_values("score", ascending=False)
            if not pick.empty:
                pick = pick.copy()
                pick["rank_score"] = pick["score"]
        elif mode == "bonus_gate":
            adj = g.copy()
            adj["rank_score"] = adj["score"] + np.where(adj["ma_gates"], BONUS, 0.0)
            pick = adj.loc[adj["rank_score"] >= thresh].sort_values(
                "rank_score", ascending=False
            )
        elif mode == "rerank":
            pick = g.loc[g["score"] >= thresh].copy()
            if not pick.empty:
                pick["rank_score"] = pick["score"] + np.where(pick["ma_gates"], BONUS, 0.0)
                pick = pick.sort_values("rank_score", ascending=False)
        else:
            raise ValueError(mode)
        if pick.empty:
            continue
        part = pick.head(TOP_K_BUFFER).copy()
        part["signal_date"] = day
        rows.append(part)
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


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
        ann = (
            (1.0 + total_ret) ** (1.0 / years) - 1.0
            if years > 0 and total_ret > -0.999999
            else 0.0
        )
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
            "years": round(years, 2),
            "sharpe_daily": round(
                float(rets.mean() / rets.std() * np.sqrt(242))
                if len(rets) and rets.std() > 0
                else 0.0,
                3,
            ),
            "start": str(eq.index[0].date()),
            "end": str(eq.index[-1].date()),
        }


def simulate(
    name: str,
    signals: pd.DataFrame,
    panel: pd.DataFrame,
    max_holdings: int,
) -> PortfolioResult:
    result = PortfolioResult(name=name)
    if signals.empty:
        return result

    bars_by_symbol: dict[str, pd.DataFrame] = {}
    for symbol, g in panel.groupby("symbol"):
        b = g[["date", "open", "high", "low", "close"]].drop_duplicates("date").sort_values("date")
        bars_by_symbol[str(symbol)] = b.set_index("date")

    entry_rows = []
    for _, row in signals.iterrows():
        symbol = str(row["symbol"])
        sig = pd.Timestamp(row["signal_date"])
        b = bars_by_symbol.get(symbol)
        if b is None or sig not in b.index:
            continue
        pos = int(b.index.get_loc(sig))
        if pos + 1 >= len(b):
            continue
        entry_rows.append(
            {
                "symbol": symbol,
                "entry_date": b.index[pos + 1],
                "score": float(row.get("rank_score", row.get("score", 0.0))),
            }
        )
    if not entry_rows:
        return result
    entries = pd.DataFrame(entry_rows)
    entries = entries.sort_values(["entry_date", "score"], ascending=[True, False])
    entries = entries.drop_duplicates(["symbol", "entry_date"], keep="first")

    start = entries["entry_date"].min()
    end = entries["entry_date"].max() + pd.Timedelta(days=MAX_HOLD_DAYS * 2 + 20)
    calendar = sorted(
        {d for b in bars_by_symbol.values() for d in b.index if start <= d <= end}
    )
    by_entry = {d: g for d, g in entries.groupby("entry_date")}
    pos_frac = 1.0 / max_holdings
    cash = 1.0
    positions: list[Position] = []

    def mark(symbol: str, day: pd.Timestamp) -> float | None:
        b = bars_by_symbol.get(symbol)
        if b is None or day not in b.index:
            return None
        return float(b.at[day, "close"])

    for day in calendar:
        still = []
        for p in positions:
            b = bars_by_symbol.get(p.symbol)
            if b is None or day not in b.index or day <= p.entry_date:
                still.append(p)
                continue
            p.hold_days += 1
            bar = b.loc[day]
            o, h, l, c = map(float, (bar["open"], bar["high"], bar["low"], bar["close"]))
            reason = exit_px = None
            if o <= p.sl:
                reason, exit_px = "sl_gap", o
            elif o >= p.tp:
                reason, exit_px = "tp_gap", o
            elif l <= p.sl:
                reason, exit_px = "sl", p.sl
            elif h >= p.tp:
                reason, exit_px = "tp", p.tp
            elif p.hold_days >= MAX_HOLD_DAYS:
                reason, exit_px = "max_hold", c
            if reason is None:
                still.append(p)
            else:
                sell = exit_px * (1 - SLIPPAGE)
                net = p.qty_notional * (sell / p.entry_price) * (1 - COMMISSION - STAMP)
                cash += net
                result.trades.append(
                    {
                        "symbol": p.symbol,
                        "entry_date": str(p.entry_date.date()),
                        "exit_date": str(day.date()),
                        "hold_days": p.hold_days,
                        "reason": reason,
                        "net_ret": float(net / p.qty_notional - 1),
                    }
                )
        positions = still

        cands = by_entry.get(day)
        if cands is not None:
            free = max_holdings - len(positions)
            held = {p.symbol for p in positions}
            equity = cash + sum(
                p.qty_notional * ((mark(p.symbol, day) or p.entry_price) / p.entry_price)
                for p in positions
            )
            for _, row in cands.sort_values("score", ascending=False).iterrows():
                if free <= 0:
                    break
                symbol = str(row["symbol"])
                if symbol in held:
                    continue
                b = bars_by_symbol.get(symbol)
                if b is None or day not in b.index:
                    continue
                open_p = float(b.at[day, "open"])
                if open_p <= 0:
                    continue
                alloc = min(equity * pos_frac, cash)
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
        for p in positions:
            b = bars_by_symbol.get(p.symbol)
            if b is None:
                continue
            idx = b.index[b.index <= last]
            if len(idx) == 0:
                continue
            day = idx[-1]
            close_p = float(b.at[day, "close"])
            sell = close_p * (1 - SLIPPAGE)
            net = p.qty_notional * (sell / p.entry_price) * (1 - COMMISSION - STAMP)
            cash += net
            result.trades.append(
                {
                    "symbol": p.symbol,
                    "entry_date": str(p.entry_date.date()),
                    "exit_date": str(day.date()),
                    "hold_days": p.hold_days,
                    "reason": "force_eod",
                    "net_ret": float(net / p.qty_notional - 1),
                }
            )
        result.equity_curve.append((last, float(cash)))
    return result


def main() -> None:
    print("== universe ==")
    symbols = pick_universe(VIPDOC, MAX_SYMBOLS)
    print("== panel ==")
    panel, feature_cols = build_panel(VIPDOC, symbols)
    print("== walk-forward ==")
    scored = walk_forward_scores(panel, feature_cols)

    report = {
        "params": {
            "max_symbols": MAX_SYMBOLS,
            "thresholds": THRESHOLDS,
            "holdings": HOLDINGS,
            "modes": MODES,
            "bonus": BONUS,
            "tp": TAKE_PROFIT,
            "sl": STOP_LOSS,
            "max_hold_days": MAX_HOLD_DAYS,
            "period": {"start": START_EVAL, "end": END_EVAL},
            "note": "Proxy LGBM walk-forward; not production dual-model pipeline.",
        },
        "data_summary": {
            "panel_rows": int(len(panel)),
            "symbols": int(panel["symbol"].nunique()),
            "scored_rows": int(len(scored)),
            "scored_days": int(scored["date"].nunique()),
            "score_p50": round(float(scored["score"].median()), 2),
            "score_p90": round(float(scored["score"].quantile(0.9)), 2),
            "score_p99": round(float(scored["score"].quantile(0.99)), 2),
            "ge60": round(float((scored["score"] >= 60).mean()), 4),
            "ge65": round(float((scored["score"] >= 65).mean()), 4),
            "ge70": round(float((scored["score"] >= 70).mean()), 4),
            "ma_gates_rate": round(float(scored["ma_gates"].mean()), 4),
        },
        "variants": {},
    }

    rows = []
    for thresh in THRESHOLDS:
        for mode in MODES:
            sigs = select_signals(scored, mode, thresh)
            for holds in HOLDINGS:
                name = f"{mode}_ge{int(thresh)}_h{holds}"
                print(f"== sim {name} signals={len(sigs)} ==")
                res = simulate(name, sigs, panel, holds)
                m = res.metrics()
                m["selected_signal_rows"] = int(len(sigs))
                m["threshold"] = thresh
                m["mode"] = mode
                m["max_holdings"] = holds
                report["variants"][name] = m
                rows.append({"name": name, **m})
                print(
                    f"  trades={m.get('n_trades')} win={m.get('win_rate')} "
                    f"ann={m.get('ann_return')} dd={m.get('max_drawdown')} sharpe={m.get('sharpe_daily')}"
                )

    # rank profitable variants
    frame = pd.DataFrame(rows)
    if not frame.empty:
        tradable = frame.loc[frame["n_trades"] >= 20].copy()
        if tradable.empty:
            tradable = frame.copy()
        # composite: prefer higher ann, lower |dd|, enough trades
        tradable["score_rank"] = (
            tradable["ann_return"] * 1.0
            + tradable["sharpe_daily"] * 0.05
            + tradable["max_drawdown"] * 0.3  # dd negative, so higher better
        )
        top = tradable.sort_values("score_rank", ascending=False).head(10)
        report["top_variants"] = top[
            [
                "name",
                "mode",
                "threshold",
                "max_holdings",
                "n_trades",
                "win_rate",
                "ann_return",
                "total_return",
                "max_drawdown",
                "sharpe_daily",
                "avg_trade_ret",
            ]
        ].to_dict(orient="records")
        # best by ann among n_trades>=30
        solid = frame.loc[frame["n_trades"] >= 30]
        if not solid.empty:
            best = solid.sort_values("ann_return", ascending=False).iloc[0]
            report["recommended"] = {
                "name": best["name"],
                "mode": best["mode"],
                "threshold": float(best["threshold"]),
                "max_holdings": int(best["max_holdings"]),
                "n_trades": int(best["n_trades"]),
                "win_rate": float(best["win_rate"]),
                "ann_return": float(best["ann_return"]),
                "max_drawdown": float(best["max_drawdown"]),
                "sharpe_daily": float(best["sharpe_daily"]),
                "reason": "highest ann_return among variants with >=30 trades",
            }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", OUT)
    print(json.dumps({k: report[k] for k in ("data_summary", "top_variants", "recommended") if k in report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
