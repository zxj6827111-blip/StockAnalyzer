"""P3.3 read-only: what a repair row must fill.

Null-rate / value profile of every column the alpha_v2 freeze panel consumes,
on a clean control day, in both delta libraries. A repair row is faithful only if
its null profile matches the incumbent rows'.
"""

from __future__ import annotations

import json

import duckdb

COLS = [
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "float_market_cap",
    "board",
    "is_st",
    "is_delisting_risk",
    "suspended",
    "up_limit",
    "down_limit",
    "price_series_mode",
    "adjustment_source",
    "name",
    "pre_close",
]
DAYS = {
    "raw": ("vendor_delta_raw", "2025-11-14"),
    "qfq": ("vendor_delta", "2025-11-14"),
}
OUT = {}
for role, (db, day) in DAYS.items():
    path = (
        "/app/artifacts/vendor_delta_raw/market_delta_raw.duckdb"
        if role == "raw"
        else "/app/artifacts/vendor_delta/market_delta.duckdb"
    )
    con = duckdb.connect(path, read_only=True)
    con.execute("SET memory_limit='300MB'")
    have = {r[0] for r in con.execute("DESCRIBE SELECT * FROM daily_bars").fetchall()}
    row = con.execute(
        "SELECT COUNT(*) n, COUNT(DISTINCT symbol) s FROM daily_bars WHERE date=?", [day]
    ).fetchone()
    prof = {"n_rows": row[0], "n_symbols": row[1], "columns_absent": sorted(set(COLS) - have)}
    for c in [x for x in COLS if x in have]:
        q = con.execute(
            f"SELECT COUNT(*) tot, COUNT({c}) non_null, "
            f"COUNT(DISTINCT {c}) distinct_vals, MIN({c}), MAX({c}) FROM daily_bars "
            "WHERE date=?",
            [day],
        ).fetchone()
        tot, non_null, distinct, mn, mx = q
        prof[c] = {
            "null_rate": round(1 - non_null / tot, 6) if tot else None,
            "distinct": int(distinct),
            "min": None if mn is None else str(mn)[:24],
            "max": None if mx is None else str(mx)[:24],
        }
    OUT[role] = prof
    con.close()
print(json.dumps(OUT, ensure_ascii=False, indent=1))
