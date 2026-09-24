"""P3.3 read-only: is stored QFQ exactly RAW x vendor factor?

If yes, the 295 feature-side missing keys are repairable in-house (RAW bar x
factor) with no external source and no cross-vendor semantics risk.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import duckdb
import pandas as pd

QFQ_DB = "/app/artifacts/vendor_delta/market_delta.duckdb"
RAW_DB = "/app/artifacts/vendor_delta_raw/market_delta_raw.duckdb"
FACTOR_ZIP = "/data/vendor_history/复权因子/复权因子_前复权.zip"
DEC_START = "2025-06-02"
DEC_END = "2026-08-31"
PRICE_COLS = ("open", "high", "low", "close")

con = duckdb.connect(":memory:")
con.execute("SET memory_limit='700MB'")
con.execute("SET threads=2")
try:
    con.execute("SET temp_directory='/tmp'")
except duckdb.Error:
    pass
con.execute(f"ATTACH '{QFQ_DB}' AS q (READ_ONLY)")
con.execute(f"ATTACH '{RAW_DB}' AS r (READ_ONLY)")

missing = con.execute(
    f"""
    SELECT r.symbol, CAST(r.date AS VARCHAR) d FROM r.daily_bars r
    LEFT JOIN q.daily_bars s ON r.symbol = s.symbol AND r.date = s.date
    WHERE r.date BETWEEN '{DEC_START}' AND '{DEC_END}' AND s.date IS NULL
    ORDER BY 1,2
    """
).fetchall()
missing_keys = {(s, d) for s, d in missing}
affected = sorted({s for s, _ in missing_keys})

controls = con.execute(
    f"""
    SELECT r.symbol, CAST(r.date AS VARCHAR) d, r.open, r.high, r.low, r.close,
           s.open, s.high, s.low, s.close, s.price_series_mode, s.adjustment_source,
           s.adjustment_anchor_date, s.adjustment_anchor_factor
    FROM r.daily_bars r JOIN q.daily_bars s ON r.symbol = s.symbol AND r.date = s.date
    WHERE r.date BETWEEN '2026-06-01' AND '2026-07-16'
      AND r.symbol IN ({','.join("'" + a + "'" for a in affected)})
    ORDER BY r.symbol, r.date
    """
).fetchall()

zf = zipfile.ZipFile(FACTOR_ZIP)
factor_by_symbol: dict[str, pd.Series] = {}
for sym in affected:
    frames = []
    for n in zf.namelist():
        name = Path(n.replace("\\", "/")).name
        if not name.upper().startswith(sym) or Path(name).suffix != ".csv":
            continue
        raw = pd.read_csv(io.BytesIO(zf.read(n)))
        dc = next((c for c in ("交易日期", "trade_date", "date") if c in raw.columns), None)
        fc = next((c for c in ("复权因子", "adj_factor", "factor") if c in raw.columns), None)
        if dc is None or fc is None:
            continue
        dts = pd.to_datetime(raw[dc].astype(str), errors="coerce")
        vals = pd.to_numeric(raw[fc], errors="coerce")
        ser = pd.Series(vals.to_numpy(), index=pd.DatetimeIndex(dts))
        ser = ser[ser.index.notna() & ser.notna()]
        ser = ser[ser > 0]
        if not ser.empty:
            frames.append(ser)
    if frames:
        merged = pd.concat(frames)
        factor_by_symbol[sym] = merged[~merged.index.duplicated(keep="last")].sort_index()

exact = 0
checked = 0
ratios: list[float] = []
worst_row = None
meta: dict[str, int] = {}
for row in controls:
    sym, d = row[0], row[1]
    raw_p, qfq_p = row[2:6], row[6:10]
    fac = factor_by_symbol.get(sym)
    if fac is None or fac.empty:
        meta["no_factor_series"] = meta.get("no_factor_series", 0) + 1
        continue
    try:
        f = float(fac.loc[pd.Timestamp(d)])
    except KeyError:
        prior = fac[fac.index <= pd.Timestamp(d)]
        if prior.empty:
            meta["factor_before_window"] = meta.get("factor_before_window", 0) + 1
            continue
        f = float(prior.iloc[-1])
        meta["factor_ffilled"] = meta.get("factor_ffilled", 0) + 1
    if any(p is None for p in qfq_p) or any(p is None for p in raw_p):
        continue
    checked += 1
    diffs = []
    for rp, qp in zip(raw_p, qfq_p):
        expect = float(rp) * f
        rel = abs(expect - float(qp)) / max(1e-9, abs(float(qp)))
        diffs.append(rel)
    if max(diffs) < 1e-9:
        exact += 1
    elif worst_row is None or max(diffs) > worst_row[0]:
        worst_row = (max(diffs), sym, d, f, list(raw_p), list(qfq_p))
    ratios.extend(diffs)
    meta.setdefault(
        "mode_" + str(row[10]), 0
    )
    meta["mode_" + str(row[10])] += 1
    meta.setdefault("adj_" + str(row[11]), 0)
    meta["adj_" + str(row[11])] += 1

derivable = 0
no_raw = 0
no_factor = 0
for sym, d in missing_keys:
    has_raw = con.execute(
        "SELECT COUNT(*) FROM r.daily_bars WHERE symbol=? AND date=?", [sym, d]
    ).fetchone()[0]
    fac = factor_by_symbol.get(sym)
    has_fac = fac is not None and not fac[fac.index <= pd.Timestamp(d)].empty
    if not has_raw:
        no_raw += 1
    elif not has_fac:
        no_factor += 1
    else:
        derivable += 1

print(
    json.dumps(
        {
            "control_rows_checked": checked,
            "control_exact_match_under_1e-9": exact,
            "exact_match_ratio": round(exact / max(1, checked), 6),
            "max_rel_diff": max(ratios) if ratios else None,
            "p99_rel_diff": (
                sorted(ratios)[int(len(ratios) * 0.99)] if ratios else None
            ),
            "worst_row": worst_row,
            "meta": meta,
            "missing_keys": len(missing_keys),
            "missing_repairable_from_raw_times_factor": derivable,
            "missing_no_raw_bar": no_raw,
            "missing_no_factor": no_factor,
        },
        ensure_ascii=False,
        indent=1,
        default=str,
    )
)
