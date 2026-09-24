"""P3.3 read-only: asymmetry inside the DECISION window only + factor forensics."""

from __future__ import annotations

import io
import json
import zipfile
from collections import Counter
from pathlib import Path

import duckdb
import pandas as pd

QFQ_DB = "/app/artifacts/vendor_delta/market_delta.duckdb"
RAW_DB = "/app/artifacts/vendor_delta_raw/market_delta_raw.duckdb"
FACTOR_ZIP = "/data/vendor_history/复权因子/复权因子_前复权.zip"
DEC_START = "2025-06-02"
DEC_END = "2026-08-31"

con = duckdb.connect(":memory:")
con.execute("SET memory_limit='700MB'")
con.execute("SET threads=2")
try:
    con.execute("SET temp_directory='/tmp'")
except duckdb.Error:
    pass
con.execute(f"ATTACH '{QFQ_DB}' AS q (READ_ONLY)")
con.execute(f"ATTACH '{RAW_DB}' AS r (READ_ONLY)")

rows = con.execute(
    f"""
    SELECT COALESCE(r.symbol, s.symbol) sym, COALESCE(r.d, s.d) d,
           (r.d IS NOT NULL) in_raw, (s.d IS NOT NULL) in_q
    FROM (SELECT symbol, CAST(date AS VARCHAR) d FROM r.daily_bars
          WHERE date BETWEEN '{DEC_START}' AND '{DEC_END}') r
    FULL OUTER JOIN (SELECT symbol, CAST(date AS VARCHAR) d FROM q.daily_bars
          WHERE date BETWEEN '{DEC_START}' AND '{DEC_END}') s
      ON r.symbol = s.symbol AND r.d = s.d
    WHERE r.d IS NULL OR s.d IS NULL
    """
).fetchall()

feat = [(s, d) for s, d, in_raw, in_q in rows if in_raw and not in_q]
execm = [(s, d) for s, d, in_raw, in_q in rows if in_q and not in_raw]
print(
    json.dumps(
        {
            "decision_window": [DEC_START, DEC_END],
            "feature_missing_keys": len(feat),
            "execution_missing_keys": len(execm),
            "feature_missing_by_date": dict(
                sorted(Counter(d for _s, d in feat).items())
            ),
            "execution_missing_by_date": dict(
                sorted(Counter(d for _s, d in execm).items())
            ),
            "feature_missing_symbols": sorted({s for s, _ in feat}),
        },
        ensure_ascii=False,
        indent=1,
    )
)

syms = sorted({s for s, _ in feat})
zf = zipfile.ZipFile(FACTOR_ZIP)
names = zf.namelist()
out = []
for sym in syms:
    hits = [
        n for n in names
        if Path(n.replace("\\", "/")).name.upper().startswith(sym)
        and Path(n).suffix == ".csv"
    ]
    notes = []
    for n in hits:
        if not n.endswith("2026/" + Path(n).name):
            continue
        try:
            raw = pd.read_csv(io.BytesIO(zf.read(n)))
            cols = list(raw.columns)
            fc = next((c for c in ("复权因子", "adj_factor", "factor") if c in cols), None)
            dc = next((c for c in ("交易日期", "trade_date", "date") if c in cols), None)
            if fc is None or dc is None:
                notes.append(f"{n}: BAD_HEADER cols={cols}")
                continue
            vals = pd.to_numeric(raw[fc], errors="coerce")
            dts = pd.to_datetime(raw[dc].astype(str), errors="coerce")
            win = raw[(dts >= "2026-07-01") & (dts <= "2026-08-31")]
            wv = pd.to_numeric(win[fc], errors="coerce")
            notes.append(
                f"rows={len(raw)} nonpos_or_nan={int((vals <= 0).sum() + vals.isna().sum())} "
                f"nan_date={int(dts.isna().sum())} julaug_rows={len(win)} "
                f"julaug_bad={int((wv <= 0).sum() + wv.isna().sum())} "
                f"max={dts.max().date() if dts.notna().any() else None}"
            )
        except Exception as exc:  # noqa: BLE001
            notes.append(f"RAISES {type(exc).__name__}: {exc}")
    out.append(f"{sym} entries={len(hits)} | " + " ; ".join(notes))
print("\n".join(out[:40]))
