"""P3.3 read-only: cross-panel asymmetry over the whole freeze source window,
plus qfq factor-archive forensics for the affected symbols.

Attaches both production delta libraries read_only. Writes nothing to them.
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
WIN_START = "2024-11-14"
WIN_END = "2026-09-23"

con = duckdb.connect(":memory:")
# The api container is capped at 4 GiB and is serving production; bound the
# analysis engine well under it and allow spilling instead of growing.
con.execute("SET memory_limit='700MB'")
con.execute("SET threads=2")
con.execute("SET enable_progress_bar=false")
try:
    con.execute("SET temp_directory='/tmp'")
except duckdb.Error:
    pass
con.execute("SET temp_directory='/tmp'")
con.execute("SET preserve_insertion_order=false")
con.execute(f"ATTACH '{QFQ_DB}' AS q (READ_ONLY)")
con.execute(f"ATTACH '{RAW_DB}' AS r (READ_ONLY)")

asym = con.execute(
    f"""
    WITH q AS (SELECT symbol, CAST(date AS VARCHAR) d FROM q.daily_bars
               WHERE date BETWEEN '{WIN_START}' AND '{WIN_END}'),
         r AS (SELECT symbol, CAST(date AS VARCHAR) d FROM r.daily_bars
               WHERE date BETWEEN '{WIN_START}' AND '{WIN_END}')
    SELECT COALESCE(r.symbol, q.symbol) symbol, COALESCE(r.d, q.d) d,
           (q.d IS NOT NULL) AS in_q, (r.d IS NOT NULL) AS in_r
    FROM r FULL OUTER JOIN q ON r.symbol = q.symbol AND r.d = q.d
    WHERE (r.d IS NULL) OR (q.d IS NULL)
    """
).fetchall()

feature_missing = [(s, d) for s, d, in_q, in_r in asym if in_r and not in_q]
exec_missing = [(s, d) for s, d, in_q, in_r in asym if in_q and not in_r]

by_date: dict[str, dict[str, int]] = {}
for s, d in feature_missing:
    by_date.setdefault(d, {"feature_missing": 0})["feature_missing"] += 1
for s, d in exec_missing:
    by_date.setdefault(d, {}).setdefault("execution_missing", 0)
    by_date[d]["execution_missing"] += 1

worst = sorted(
    by_date.items(), key=lambda kv: -kv[1].get("feature_missing", 0)
)[:15]
affected_symbols = sorted({s for s, _ in feature_missing})

print(
    json.dumps(
        {
            "window": [WIN_START, WIN_END],
            "pairs_scanned_asymmetric": len(asym),
            "feature_missing_keys": len(feature_missing),
            "execution_missing_keys": len(exec_missing),
            "dates_with_feature_missing": len(
                [d for d, v in by_date.items() if v.get("feature_missing")]
            ),
            "worst_dates": [
                {"date": d, **v} for d, v in worst
            ],
            "affected_symbol_count": len(affected_symbols),
            "affected_symbols": affected_symbols,
            "execution_missing_dates": sorted(
                {d for d, v in by_date.items() if v.get("execution_missing")}
            )[:40],
        },
        ensure_ascii=False,
        indent=1,
    )
)

zf = zipfile.ZipFile(FACTOR_ZIP)
names = zf.namelist()
probe = {}
for sym in affected_symbols[:6]:
    hits = [
        n
        for n in names
        if Path(n.replace("\\", "/")).name.upper().startswith(sym)
        and Path(n).suffix == ".csv"
    ]
    detail = []
    for n in hits:
        try:
            raw = pd.read_csv(io.BytesIO(zf.read(n)))
            cols = list(raw.columns)
            fcol = next((c for c in ("复权因子", "adj_factor", "factor") if c in cols), None)
            dcol = next((c for c in ("交易日期", "trade_date", "date") if c in cols), None)
            bad = None
            if fcol and dcol:
                vals = pd.to_numeric(raw[fcol], errors="coerce")
                dates = pd.to_datetime(raw[dcol], errors="coerce")
                recent = raw[(dates >= "2026-07-01") & (dates <= "2026-08-31")]
                rvals = pd.to_numeric(recent[fcol], errors="coerce")
                bad = {
                    "rows": int(len(raw)),
                    "nonpositive": int((vals <= 0).sum() + vals.isna().sum()),
                    "nan_dates": int(dates.isna().sum()),
                    "jul_aug_rows": int(len(recent)),
                    "jul_aug_nonpositive": int((rvals <= 0).sum() + rvals.isna().sum()),
                    "max_date": str(dates.max().date()) if dates.notna().any() else None,
                }
            detail.append(
                {"entry": n, "columns": cols, "no_factor_col": fcol is None, "stats": bad}
            )
        except Exception as exc:  # noqa: BLE001 - evidence of the failure mode
            detail.append({"entry": n, "error": f"{type(exc).__name__}: {exc}"})
    probe[sym] = {"entries_found": len(hits), "detail": detail}

print(json.dumps({"factor_archive_probe": probe}, ensure_ascii=False, indent=1)[:6000])
