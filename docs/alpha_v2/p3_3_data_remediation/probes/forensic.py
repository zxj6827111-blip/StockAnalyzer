"""P3.3 read-only forensics: 2026-07 qfq-only bar gap shape.

Opens both production delta libraries read_only. Writes nothing.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import duckdb

QFQ_DB = "/app/artifacts/vendor_delta/market_delta.duckdb"
RAW_DB = "/app/artifacts/vendor_delta_raw/market_delta_raw.duckdb"
WIN_START = "2026-07-16"
WIN_END = "2026-07-31"


def bars(path: str) -> dict:
    con = duckdb.connect(path, read_only=True)
    try:
        per_day = con.execute(
            "SELECT CAST(date AS VARCHAR) d, COUNT(DISTINCT symbol) n, COUNT(*) n_rows "
            "FROM daily_bars WHERE date BETWEEN ? AND ? GROUP BY 1 ORDER BY 1",
            [WIN_START, WIN_END],
        ).fetchall()
        syms = {
            r[0]: set(r[1])
            for r in con.execute(
                "SELECT symbol, LIST(CAST(date AS VARCHAR)) FROM daily_bars "
                "WHERE date BETWEEN ? AND ? GROUP BY symbol",
                [WIN_START, WIN_END],
            ).fetchall()
        }
        span = con.execute(
            "SELECT CAST(MIN(date) AS VARCHAR), CAST(MAX(date) AS VARCHAR), COUNT(*), "
            "COUNT(DISTINCT symbol) FROM daily_bars"
        ).fetchone()
        return {"per_day": per_day, "symbols": syms, "span": span}
    finally:
        con.close()


def main() -> None:
    qfq = bars(QFQ_DB)
    raw = bars(RAW_DB)

    sessions = sorted({d for d, _n, _r in qfq["per_day"]})
    inner = [s for s in sessions if WIN_START < s < WIN_END]

    qfq_present = set(qfq["symbols"])
    raw_present = set(raw["symbols"])
    edge_ok = [
        s
        for s in qfq_present
        if WIN_START in qfq["symbols"][s] and WIN_END in qfq["symbols"][s]
    ]
    affected = {}
    for sym in edge_ok:
        lost = [d for d in inner if d not in qfq["symbols"][sym]]
        if lost:
            affected[sym] = {
                "qfq_lost_sessions": sorted(lost),
                "raw_bars_in_window": sorted(
                    d for d in raw["symbols"].get(sym, ()) if d in inner
                ),
            }

    print(
        json.dumps(
            {
                "qfq_span": qfq["span"],
                "raw_span": raw["span"],
                "per_day": {
                    "sessions": sessions,
                    "qfq": {d: n for d, n, _r in qfq["per_day"]},
                    "raw": {d: n for d, n, _r in raw["per_day"]},
                },
                "inner_sessions": inner,
                "symbols_only_in_qfq": sorted(qfq_present - raw_present)[:10],
                "affected_symbol_count": len(affected),
                "affected_sample": dict(sorted(affected.items())[:8]),
                "affected_all_have_raw": sum(
                    1 for v in affected.values() if v["raw_bars_in_window"]
                ),
                "loss_shape_distribution": {
                    f"{k[0]}|n_lost={k[1]}": c
                    for k, c in Counter(
                        (
                            v["qfq_lost_sessions"][0],
                            len(v["qfq_lost_sessions"]),
                        )
                        for v in affected.values()
                    ).items()
                },
                "raw_only_symbols": sorted(raw_present - qfq_present)[:12],
                "raw_only_symbol_count": len(raw_present - qfq_present),
            },
            ensure_ascii=False,
            indent=1,
            default=str,
        )
    )
    Path("/tmp/p33_qfq_gap.json").write_text(
        json.dumps({"affected": affected}, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


main()
