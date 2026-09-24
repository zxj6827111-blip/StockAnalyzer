"""P3.3 read-only: is Tushare a semantics-compatible repair source for the
2025 vendor source gaps?  v2 (explicit column rename on both sides).

Compares the RAW delta library (P3.2 proved it equals 2025.zip bar-for-bar on all
243 sessions) against Tushare `daily` on control dates, per column: exact match
ratio, tolerance ratio and the implied unit factor. Also re-derives the six gap
dates' missing sets and checks Tushare coverage of them.

Writes nothing. Uses the container token; never prints it.
"""

from __future__ import annotations

import json
import math
import os
from collections import Counter

import duckdb
import pandas as pd
import tushare as ts

RAW_DB = "/app/artifacts/vendor_delta_raw/market_delta_raw.duckdb"
CONTROL = ["2025-11-14", "2025-11-19", "2025-12-23", "2025-12-25"]
GAPS = ["2025-11-11", "2025-11-12", "2025-11-13", "2025-11-17", "2025-11-18", "2025-12-24"]
PREV = {
    "2025-11-11": "2025-11-10",
    "2025-11-12": "2025-11-11",
    "2025-11-13": "2025-11-12",
    "2025-11-17": "2025-11-14",
    "2025-11-18": "2025-11-17",
    "2025-12-24": "2025-12-23",
}
# delta column -> tushare column
MAP = {
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "vol",
    "turnover": "amount",
}

token = os.environ.get("SA__MARKET_WAREHOUSE__TUSHARE_TOKEN") or os.environ.get(
    "TUSHARE_TOKEN"
)
if not token:
    raise SystemExit("no tushare token in env")
pro = ts.pro_api(token)

con = duckdb.connect(RAW_DB, read_only=True)
con.execute("SET memory_limit='700MB'")
con.execute("SET threads=2")
HAVE = [r[0] for r in con.execute("DESCRIBE SELECT * FROM daily_bars").fetchall()]
USABLE = {d: t for d, t in MAP.items() if d in HAVE}
ABSENT_FROM_DELTA = [d for d in MAP if d not in HAVE]


def db_day(d: str) -> pd.DataFrame:
    cols = ", ".join(USABLE)
    return con.execute(
        f"SELECT symbol, CAST(date AS VARCHAR) d, {cols} FROM daily_bars WHERE date=?",
        [d],
    ).df()


def ts_day(d: str) -> pd.DataFrame:
    raw = pro.daily(trade_date=d.replace("-", ""))
    if raw is None or raw.empty:
        return pd.DataFrame()
    out = raw.copy()
    out["symbol"] = out["ts_code"].astype(str).str.split(".").str[0]
    keep = {"symbol": "symbol", "trade_date": "ts_trade_date"}
    keep.update({t: f"ts__{t}" for t in USABLE.values() if t in out.columns})
    return out[list(keep)].rename(columns=keep)


def stats(a: pd.Series, b: pd.Series) -> dict:
    ok = a.notna() & b.notna() & (b != 0)
    a2, b2 = a[ok], b[ok]
    if a2.empty:
        return {"n": 0}
    ratio = (a2 / b2).round(8)
    top = Counter(ratio.tolist()).most_common(4)
    factor = top[0][0] if top else None
    scaled = b2 * factor if factor else b2
    rel = (a2 - scaled).abs() / a2.abs().clip(lower=1e-9)
    return {
        "n": int(len(a2)),
        "exact_match_ratio": round(float((a2 == b2).mean()), 6),
        "dominant_unit_factor": factor,
        "dominant_share": round(top[0][1] / len(a2), 6) if top else None,
        "runner_up_factors": [{"factor": f, "count": c} for f, c in top[1:]],
        "tol_1e-6_after_unit": round(float((rel < 1e-6).mean()), 6),
        "tol_1e-4_after_unit": round(float((rel < 1e-4).mean()), 6),
        "max_abs_rel_after_unit": None if rel.empty else float(rel.max()),
        "ts_nulls": int(a.isna().sum() + 0),
    }


report: dict[str, object] = {
    "delta_columns_compared": list(USABLE),
    "requested_but_absent_from_delta": ABSENT_FROM_DELTA,
    "tushare_daily_has_no": ["pre_close_in_delta", "up_limit", "down_limit"],
    "control_semantics": [],
    "gap_dates": [],
}

for d in CONTROL:
    dbf, tsf = db_day(d), ts_day(d)
    if tsf.empty:
        report["control_semantics"].append({"date": d, "error": "tushare_empty"})
        continue
    m = dbf.merge(tsf, on="symbol", how="outer", suffixes=("", "_dup"))
    entry: dict[str, object] = {
        "date": d,
        "db_rows": int(len(dbf)),
        "ts_rows": int(len(tsf)),
        "both": int(m["symbol"].isin(set(dbf["symbol"]) & set(tsf["symbol"])).sum()),
        "db_only": int(len(set(dbf["symbol"]) - set(tsf["symbol"]))),
        "ts_only": int(len(set(tsf["symbol"]) - set(dbf["symbol"]))),
        "trade_date_agrees": None,
        "cols": {},
    }
    same_d = m[m["d"].notna() & m["ts_trade_date"].notna()]
    if not same_d.empty:
        entry["trade_date_agrees"] = round(
            float(
                (
                    pd.to_datetime(same_d["ts_trade_date"].astype(str), format="%Y%m%d")
                    .dt.strftime("%Y-%m-%d")
                    == same_d["d"]
                ).mean()
            ),
            6,
        )
    for dbc, tsc in USABLE.items():
        entry["cols"][f"{dbc} <- tushare.{tsc}"] = stats(
            pd.to_numeric(m[dbc], errors="coerce"),
            pd.to_numeric(m[f"ts__{tsc}"], errors="coerce"),
        )
    report["control_semantics"].append(entry)

for d in GAPS:
    dbf = db_day(d)
    tdf = ts_day(d)
    present = set(dbf["symbol"])
    ts_syms = set(tdf["symbol"]) if not tdf.empty else set()
    report["gap_dates"].append(
        {
            "date": d,
            "delta_rows": int(len(dbf)),
            "tushare_rows": int(len(tdf)),
            "repair_candidates_delta_missing_tushare_has": len(ts_syms - present),
            "delta_has_tushare_missing": len(present - ts_syms),
            "both": len(present & ts_syms),
        }
    )

txt = json.dumps(report, ensure_ascii=False, indent=1, default=str)
print(txt.replace("NaN", "null"))
