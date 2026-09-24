"""P3.3 DRY-RUN repair planner (read-only). Emits a manifest; writes no library.

A. vendor source gaps  -> Tushare `daily` + `daily_basic`  (external source)
B. feature-side drops  -> incumbent RAW x vendor factor     (no external source)

Verifies the one unit mapping it has not already verified (circ_mv ->
float_market_cap) on control dates, and prints the planned row counts + provenance.
"""

from __future__ import annotations

import io
import json
import os
import sys
import zipfile
from collections import Counter
from pathlib import Path

import duckdb
import pandas as pd
import tushare as ts

# Loaded from /tmp by path on purpose: the repair module is NOT on this branch's
# deployed image, and copying anything into /app/src of a running production
# container is how the 2026-09-08 vendor-schema incident happened.
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "vendor_bar_repair_standalone", "/tmp/vendor_bar_repair.py"
)
vbr = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
# dataclasses resolves string annotations through sys.modules[cls.__module__];
# without this the frozen dataclass below dies with AttributeError on exec_module.
sys.modules["vendor_bar_repair_standalone"] = vbr
_spec.loader.exec_module(vbr)  # type: ignore[union-attr]

RAW_DB = "/app/artifacts/vendor_delta_raw/market_delta_raw.duckdb"
QFQ_DB = "/app/artifacts/vendor_delta/market_delta.duckdb"
FACTOR_ZIP = "/data/vendor_history/复权因子/复权因子_前复权.zip"
SOURCE_GAP_DATES = [
    "2025-11-11",
    "2025-11-12",
    "2025-11-13",
    "2025-11-17",
    "2025-11-18",
    "2025-12-24",
]
CONTROL = ["2025-11-14", "2025-12-23"]
DEC_START, DEC_END = "2025-06-02", "2026-08-31"

pro = ts.pro_api(
    os.environ.get("SA__MARKET_WAREHOUSE__TUSHARE_TOKEN")
    or os.environ.get("TUSHARE_TOKEN")
    or sys.exit("no token")
)
raw = duckdb.connect(RAW_DB, read_only=True)
raw.execute("SET memory_limit='500MB'")
raw.execute("SET threads=2")
qfq = duckdb.connect(QFQ_DB, read_only=True)
qfq.execute("SET memory_limit='500MB'")
qfq.execute("SET threads=2")


def ts_rows(day: str) -> pd.DataFrame:
    return pro.daily(trade_date=day.replace("-", ""))


def basic_rows(day: str) -> pd.DataFrame:
    return pro.daily_basic(
        trade_date=day.replace("-", ""),
        fields="ts_code,trade_date,circ_mv,total_mv,turnover_rate_f",
    )


manifest: dict[str, object] = {
    "generated_at": vbr.utc_now_iso(),
    "applied": False,
    "mode": "DRY_RUN",
    "raw_rows_before": raw.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0],
    "qfq_rows_before": qfq.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0],
    "unit_check_circ_mv": {},
    "vendor_source_gap_plan": [],
    "feature_side_plan": {},
}

# ── circ_mv -> float_market_cap unit check on control dates ─────────────────
for day in CONTROL:
    dbf = raw.execute(
        "SELECT symbol, float_market_cap FROM daily_bars WHERE date=?", [day]
    ).df()
    b = basic_rows(day)
    b["symbol"] = b["ts_code"].astype(str).str.split(".").str[0]
    m = dbf.merge(b[["symbol", "circ_mv"]], on="symbol", how="inner")
    ratio = (
        pd.to_numeric(m["float_market_cap"], errors="coerce")
        / pd.to_numeric(m["circ_mv"], errors="coerce")
    ).round(6)
    top = Counter(ratio.dropna().tolist()).most_common(2)
    manifest["unit_check_circ_mv"][day] = {
        "joined": int(len(m)),
        "dominant_factor": top[0][0] if top else None,
        "dominant_share": round(top[0][1] / max(1, len(m)), 6) if top else None,
        "other": top[1:],
    }

# ── A. vendor source gap plan ───────────────────────────────────────────────
# board 不重新推断：直接取该票在 delta 里已有的值（每票 200+ 行），
# 避免在修复路径里再造一套"前缀 -> 板块"规则。
BOARDS = {
    str(a): (None if b is None else str(b))
    for a, b in raw.execute(
        "SELECT symbol, ANY_VALUE(board) FROM daily_bars "
        "WHERE board IS NOT NULL GROUP BY symbol"
    ).fetchall()
}
for day in SOURCE_GAP_DATES:
    incumbent = raw.execute(
        "SELECT symbol, CAST(date AS VARCHAR) d FROM daily_bars WHERE date=?", [day]
    ).df()
    have = set(zip(incumbent["symbol"], incumbent["d"], strict=False))
    daily = ts_rows(day)
    daily["symbol"] = daily["ts_code"].astype(str).str.split(".").str[0]
    basic = basic_rows(day)
    basic["symbol"] = basic["ts_code"].astype(str).str.split(".").str[0]
    joined = daily.merge(
        basic[["symbol", "circ_mv"]], on="symbol", how="left", suffixes=("", "_b")
    )
    prov = vbr.RepairProvenance(
        repair_batch_id=f"p33-sourcegap-{day}",
        repair_source=vbr.ADJUSTMENT_SOURCE_REPAIR_FROM_TUSHARE,
        repair_reason=vbr.REPAIR_REASON_SOURCE_GAP,
        verified_by="p3_3_tushare_semantics_check(OHLC exact, vol x100, amount x1000)",
        source_file=f"tushare:daily+daily_basic/trade_date={day.replace('-', '')}",
        source_query_time=vbr.utc_now_iso(),
        vendor_original_missing=True,
    )
    candidates: list[dict] = []
    for _, r in joined.iterrows():
        sym = str(r["symbol"])
        if (sym, day) in have:
            continue
        candidates.append(
            {
                "symbol": sym,
                "date": day,
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["vol"]) * 100.0,
                "turnover": float(r["amount"]) * 1000.0,
                "float_market_cap": (
                    None if pd.isna(r["circ_mv"]) else float(r["circ_mv"]) * 10000.0
                ),
                "board": BOARDS.get(sym),
                "is_st": False,
                "is_delisting_risk": False,
                "suspended": False,
                "_source_row_hash": vbr.row_content_hash(
                    {k: v for k, v in r.items() if k != "symbol"}
                ),
            }
        )
    planned, skipped = vbr.plan_insert_missing_only(
        incumbent_keys=have,
        candidate_rows=candidates,
        price_series_mode="raw",
        provenance=prov,
    )
    prov_df = vbr.provenance_rows(
        planned_rows=planned, price_series_mode="raw", provenance=prov
    )
    manifest["vendor_source_gap_plan"].append(
        {
            "date": day,
            "tushare_rows": int(len(daily)),
            "delta_rows": int(len(incumbent)),
            "candidates": len(candidates),
            "planned_raw_rows": len(planned),
            "skipped_already_present": skipped,
            "board_resolved": sum(1 for p in planned if p.get("board")),
            "float_cap_null": sum(1 for p in planned if p.get("float_market_cap") is None),
            "provenance_registered": int(len(prov_df)),
            "sample": planned[:2],
        }
    )

# ── B. feature-side plan (RAW x factor, no external source) ─────────────────
both = duckdb.connect(":memory:")
both.execute("SET memory_limit='700MB'")
both.execute("SET threads=2")
both.execute(f"ATTACH '{QFQ_DB}' AS q (READ_ONLY)")
both.execute(f"ATTACH '{RAW_DB}' AS r (READ_ONLY)")
missing = both.execute(
    f"""SELECT r.symbol, CAST(r.date AS VARCHAR) d FROM r.daily_bars r
        LEFT JOIN q.daily_bars s ON r.symbol=s.symbol AND r.date=s.date
        WHERE r.date BETWEEN '{DEC_START}' AND '{DEC_END}' AND s.date IS NULL"""
).df()
zf = zipfile.ZipFile(FACTOR_ZIP)
FACTOR_ENTRIES = zf.namelist()  # one directory scan, not one per symbol
factor_cache: dict[str, pd.Series] = {}

for sym in sorted(set(missing["symbol"])):
    frames = []
    for n in FACTOR_ENTRIES:
        name = Path(n.replace("\\", "/")).name
        if not name.upper().startswith(sym) or not name.endswith(".csv"):
            continue
        csv = pd.read_csv(io.BytesIO(zf.read(n)))
        dc = next((c for c in ("交易日期", "trade_date", "date") if c in csv.columns), None)
        fc = next((c for c in ("复权因子", "adj_factor", "factor") if c in csv.columns), None)
        if not dc or not fc:
            continue
        dts = pd.to_datetime(csv[dc].astype(str), errors="coerce")
        vals = pd.to_numeric(csv[fc], errors="coerce")
        ser = pd.Series(vals.to_numpy(), index=pd.DatetimeIndex(dts))
        ser = ser[ser.index.notna() & ser.notna() & (ser > 0)]
        if not ser.empty:
            frames.append(ser)
    factor_cache[sym] = (
        pd.concat(frames).sort_index() if frames else pd.Series(dtype=float)
    )

derived, unDerivable = [], []
for sym, day in zip(missing["symbol"], missing["d"], strict=True):
    dbf = raw.execute(
        "SELECT * FROM daily_bars WHERE symbol=? AND date=?", [sym, day]
    ).df()
    fac = factor_cache.get(sym)
    if dbf.empty or fac is None or fac.empty:
        unDerivable.append(f"{sym}@{day}")
        continue
    prior = fac[fac.index <= pd.Timestamp(day)]
    if prior.empty:
        unDerivable.append(f"{sym}@{day}(no_factor_on_date)")
        continue
    factor = float(prior.iloc[-1])
    base = dbf.iloc[0]
    prices = vbr.derive_qfq_from_raw(
        {c: float(base[c]) for c in vbr.REPAIR_PRICE_COLUMNS}, factor=factor
    )
    derived.append(
        {
            "symbol": sym,
            "date": day,
            **prices,
            "volume": float(base["volume"]),
            "turnover": float(base["turnover"]),
            "float_market_cap": (
                None if base["float_market_cap"] is None else float(base["float_market_cap"])
            ),
            "board": base["board"],
            "is_st": bool(base["is_st"]),
            "is_delisting_risk": bool(base["is_delisting_risk"]),
            "suspended": bool(base["suspended"]),
        }
    )
qprov = vbr.RepairProvenance(
    repair_batch_id="p33-featuredrop-202607",
    repair_source=vbr.ADJUSTMENT_SOURCE_REPAIR_QFQ_DERIVED,
    repair_reason=vbr.REPAIR_REASON_FEATURE_DROP,
    verified_by="p3_3_qfq_equals_raw_times_factor(891/891 exact, max rel 5.4e-11)",
    source_file="vendor_delta_raw daily_bars x 复权因子_前复权.zip",
    source_query_time=vbr.utc_now_iso(),
    vendor_original_missing=False,
)
qplanned, qskipped = vbr.plan_insert_missing_only(
    incumbent_keys={
        (str(sym), str(d))
        for sym, d in qfq.execute(
            "SELECT symbol, CAST(date AS VARCHAR) FROM daily_bars "
            "WHERE date BETWEEN ? AND ?",
            [DEC_START, DEC_END],
        ).fetchall()
    },
    candidate_rows=derived,
    price_series_mode="qfq",
    provenance=qprov,
)
manifest["feature_side_plan"] = {
    "missing_keys": int(len(missing)),
    "derivable_from_raw_times_factor": len(derived),
    "not_derivable": len(unDerivable),
    "planned_qfq_rows": len(qplanned),
    "skipped_already_present": qskipped,
    "planned_by_date": dict(Counter(row["date"] for row in qplanned)),
    "sample": qplanned[:2],
}
manifest["totals"] = {
    "planned_raw_rows": sum(p["planned_raw_rows"] for p in manifest["vendor_source_gap_plan"]),
    "planned_qfq_rows": len(qplanned),
}
Path("/tmp/p33_manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
)
print(json.dumps(manifest, ensure_ascii=False, indent=1, default=str))
