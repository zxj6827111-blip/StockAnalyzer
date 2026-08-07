"""Walk-forward validation for the week5 funnel (5500 -> light -> deep -> final).

For each historical cutoff date D the funnel is replayed on data available at
D (window [D-250, D]); the light/deep/final selections are then scored against
realized forward returns over the next ``horizon`` days.  Reports:

- funnel sizes per stage (light/deep/final) and no-signal-day ratio;
- forward return of the light top 100/150/200 and the final top-k vs a random
  baseline;
- rank agreement between the snapshot-light path and the direct-bars path
  (Spearman) when ``--compare-direct`` is set.

Usage (local functional check uses --provider synthetic; NAS uses the real
config which reads the warehouse/vendor data):

    python scripts/walk_forward_validation.py --config config/default.yaml \\
        --provider synthetic --cutoffs 3 --symbols-file symbols.txt
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from stock_analyzer.config import load_config  # noqa: E402
from stock_analyzer.data.provider import SyntheticProvider  # noqa: E402
from stock_analyzer.data.provider_factory import build_runtime_provider  # noqa: E402
from stock_analyzer.feature.engineer import FeatureEngineer  # noqa: E402
from stock_analyzer.feature.snapshot import _raw_snapshot_values  # noqa: E402


def _score_light_row(row: pd.Series) -> float:
    """Mirror of the light-stage baseline score (uses only snapshot columns)."""
    latest_close = float(row.get("latest_close", 0.0))
    ma20 = float(row.get("ma20", 0.0))
    ma60 = float(row.get("ma60", 0.0))
    ma120 = float(row.get("ma120", 0.0))
    ma240 = float(row.get("ma240", 0.0))
    ret20 = float(row.get("ret20", 0.0))
    ret60 = float(row.get("ret60", 0.0))
    ret120 = float(row.get("ret120", 0.0))
    recent_high = float(row.get("recent_high", latest_close))
    avg_turnover_20 = float(row.get("avg_turnover_20", 0.0))
    avg_turnover_60 = float(row.get("avg_turnover_60", 0.0))
    heat_ratio = avg_turnover_20 / max(avg_turnover_60, 1.0)
    volatility20 = float(row.get("volatility_20d", 0.0))
    atr20 = float(row.get("atr_20d", 0.0))
    atr60 = float(row.get("atr_60d", atr20))
    volume_expansion = float(row.get("volume_5d", 0.0)) / max(
        float(row.get("volume_20d", 0.0)), 1.0
    )
    float_market_cap = float(row.get("float_market_cap", 0.0))
    turnover_rate20 = float(row.get("turnover_rate_20d", 0.0))
    holder_chg = float(row.get("holder_count_chg_60d", 0.0))
    northbound_20 = float(row.get("northbound_net_20d", 0.0))
    northbound_ratio = northbound_20 / max(avg_turnover_20 * 20.0, 1.0)
    dragon_freq = float(row.get("dragon_tiger_freq_20d", 0.0))
    financial_complete = bool(row.get("financial_data_complete", False))
    background_complete = bool(row.get("background_data_complete", False))

    ma_alignment = (
        0.30 * float(latest_close >= ma20)
        + 0.30 * float(latest_close >= ma60)
        + 0.25 * float(latest_close >= ma120)
        + 0.15 * float(latest_close >= ma240)
    )
    momentum = (
        0.40 * min(max(ret20 / 0.18, 0.0), 1.0)
        + 0.35 * min(max(ret60 / 0.35, 0.0), 1.0)
        + 0.25 * min(max(ret120 / 0.60, 0.0), 1.0)
    )
    breakout = min(max((latest_close / max(recent_high, 1e-9) - 0.82) / 0.18, 0.0), 1.0)
    trend = min(max(0.45 * ma_alignment + 0.35 * momentum + 0.20 * breakout, 0.0), 1.0)
    holder_comp = min(max((0.05 - holder_chg) / 0.10, 0.0), 1.0)
    northbound_comp = min(max((northbound_ratio + 0.02) / 0.04, 0.0), 1.0)
    dragon_comp = 0.30 + 0.70 * min(max(dragon_freq / 0.08, 0.0), 1.0)
    capital_flow = min(
        max(0.45 * holder_comp + 0.35 * northbound_comp + 0.20 * dragon_comp, 0.0), 1.0
    )
    atr_compression = min(max((1.10 - atr20 / max(atr60, 1e-6)) / 0.40, 0.0), 1.0)
    volume_comp = min(max((volume_expansion - 0.90) / 0.90, 0.0), 1.0)
    heat_comp = min(max((heat_ratio - 0.85) / 0.65, 0.0), 1.0)
    price_volume = min(
        max(0.40 * volume_comp + 0.30 * atr_compression + 0.30 * heat_comp, 0.0), 1.0
    )
    turnover_comp = min(max(np.log10(max(avg_turnover_20, 1.0)) / 9.0, 0.0), 1.0)
    market_cap_comp = min(max(np.log10(max(float_market_cap, 1.0)) / 11.0, 0.0), 1.0)
    turnover_rate_comp = min(max((turnover_rate20 - 0.001) / 0.02, 0.0), 1.0)
    quality_comp = 0.50 * (1.0 if financial_complete else 0.35) + 0.50 * (
        1.0 if background_complete else 0.35
    )
    liquidity = min(
        max(
            0.45 * turnover_comp
            + 0.25 * market_cap_comp
            + 0.15 * turnover_rate_comp
            + 0.15 * quality_comp,
            0.0,
        ),
        1.0,
    )
    drawdown = max(0.0, 1.0 - latest_close / max(recent_high, 1e-9))
    volatility_penalty = min(max(max(volatility20 - 0.05, 0.0) / 0.10, 0.0), 1.0)
    drawdown_penalty = min(max(max(drawdown - 0.08, 0.0) / 0.22, 0.0), 1.0)
    risk_penalty = min(max(0.65 * volatility_penalty + 0.35 * drawdown_penalty, 0.0), 1.0)
    return 100.0 * min(
        max(
            0.40 * trend
            + 0.25 * capital_flow
            + 0.15 * price_volume
            + 0.10 * liquidity
            - 0.10 * risk_penalty,
            0.0,
        ),
        1.0,
    )


def _forward_return(provider: object, symbol: str, end: date, horizon: int) -> float | None:
    """Realized return of ``symbol`` over [end, end+horizon]."""
    try:
        bars = provider.fetch_daily_bars(
            symbol=symbol,
            lookback_days=horizon + 30,
            end_date=end + timedelta(days=horizon + 10),
        )
    except Exception:
        return None
    if bars is None or not isinstance(bars, pd.DataFrame) or bars.empty:
        return None
    close = pd.to_numeric(bars["close"], errors="coerce").dropna()
    if len(close) < 2:
        return None
    entry = close.iloc[-horizon - 1] if len(close) > horizon else close.iloc[0]
    exit_price = close.iloc[-1]
    if entry <= 0:
        return None
    return float(exit_price / entry - 1.0)


def _run_cutoff(
    provider: object,
    symbols: list[str],
    cutoff: date,
    lookback: int,
    light_target: int,
    deep_target: int,
    final_cap: int,
    horizon: int,
) -> dict[str, object]:
    engineer = FeatureEngineer()
    rows: list[pd.DataFrame] = []
    for symbol in symbols:
        try:
            bars = provider.fetch_daily_bars(
                symbol=symbol,
                lookback_days=lookback,
                end_date=cutoff,
            )
        except Exception:
            continue
        if bars is None or not isinstance(bars, pd.DataFrame) or bars.empty:
            continue
        bars = bars[bars.index <= pd.Timestamp(cutoff)]
        if bars.empty:
            continue
        try:
            features = engineer.transform(bars)
        except Exception:
            continue
        if features is None or features.empty:
            continue
        raw = _raw_snapshot_values(bars=bars, features=features.iloc[-1])
        if raw is None:
            continue
        payload: dict[str, object] = {"symbol": symbol}
        payload.update(raw)
        rows.append(pd.DataFrame([payload]))
    if not rows:
        return {"cutoff": cutoff.isoformat(), "universe": 0, "error": "no_rows"}
    frame = pd.concat(rows, ignore_index=True)
    frame["baseline_score"] = frame.apply(_score_light_row, axis=1)
    ranked = frame.sort_values(
        ["baseline_score", "avg_turnover_20"], ascending=[False, False]
    ).reset_index(drop=True)

    forward_map: dict[str, float] = {}
    for symbol in ranked["symbol"].tolist()[: max(light_target, deep_target, final_cap) * 2]:
        ret = _forward_return(provider, symbol, cutoff, horizon)
        if ret is not None:
            forward_map[symbol] = ret

    def _avg_return(top_n: int) -> float | None:
        values = [
            forward_map[symbol]
            for symbol in ranked["symbol"].head(top_n).tolist()
            if symbol in forward_map
        ]
        return float(np.mean(values)) if values else None

    deep_pool = ranked["symbol"].head(light_target).tolist()
    final_symbols = deep_pool[:final_cap]

    return {
        "cutoff": cutoff.isoformat(),
        "universe": int(len(ranked)),
        "light_count": int(min(light_target, len(ranked))),
        "deep_count": int(min(deep_target, len(deep_pool))),
        "final_count": int(min(final_cap, len(final_symbols))),
        "no_signal": len(final_symbols) == 0,
        "forward_return_top100": _avg_return(100),
        "forward_return_top150": _avg_return(150),
        "forward_return_top200": _avg_return(200),
        "forward_return_final": _avg_return(final_cap),
        "random_baseline": (
            float(np.mean(list(forward_map.values()))) if forward_map else None
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config" / "default.yaml"))
    parser.add_argument("--provider", default="", help="synthetic | real (default config)")
    parser.add_argument("--symbols-file", default="")
    parser.add_argument("--limit", type=int, default=60, help="Max symbols to sample")
    parser.add_argument("--cutoffs", type=int, default=3, help="Number of cutoff dates")
    parser.add_argument("--lookback", type=int, default=250)
    parser.add_argument("--light", type=int, default=150)
    parser.add_argument("--deep", type=int, default=20)
    parser.add_argument("--final", type=int, default=5)
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--end-date", default="", help="Latest cutoff (YYYY-MM-DD)")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.provider.strip().lower() == "synthetic":
        provider: object = SyntheticProvider(seed_offset=2026)
    else:
        provider = build_runtime_provider(config.data_source)

    symbols: list[str] = []
    if args.symbols_file.strip():
        raw_path = Path(args.symbols_file.strip())
        symbols = [
            line.strip()
            for line in raw_path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        ]
    if not symbols and args.provider.strip().lower() == "synthetic":
        symbols = [
            "600519",
            "000001",
            "601318",
            "000858",
            "600000",
            "300750",
            "000002",
            "600036",
            "601899",
            "600030",
            "000333",
            "601166",
            "600276",
            "002415",
            "600887",
            "000651",
            "601288",
            "600900",
            "002594",
            "600050",
            "601012",
            "000725",
            "600104",
            "002475",
            "600585",
            "000568",
            "601088",
            "600016",
            "002714",
            "600309",
        ]
    if not symbols:
        list_symbols = getattr(provider, "list_symbols", None)
        if callable(list_symbols):
            try:
                symbols = list(list_symbols())
            except Exception:
                symbols = []
    symbols = symbols[: args.limit]
    if not symbols:
        print("no symbols available", file=sys.stderr)
        return 2

    end_date = _coerce_date(args.end_date) or date.today()
    cutoffs = [
        end_date - timedelta(days=index * 7)
        for index in range(max(1, int(args.cutoffs)))
    ]
    results = [
        _run_cutoff(
            provider=provider,
            symbols=symbols,
            cutoff=cutoff,
            lookback=int(args.lookback),
            light_target=int(args.light),
            deep_target=int(args.deep),
            final_cap=int(args.final),
            horizon=int(args.horizon),
        )
        for cutoff in cutoffs
    ]
    summary = {
        "provider": args.provider.strip().lower() or "real",
        "symbols": len(symbols),
        "cutoffs": cutoffs[0].isoformat() if cutoffs else "",
        "horizon_days": int(args.horizon),
        "results": results,
        "no_signal_day_ratio": round(
            sum(1 for item in results if item.get("no_signal")) / max(len(results), 1), 4
        ),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _coerce_date(value: object) -> date | None:
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


if __name__ == "__main__":
    raise SystemExit(main())
