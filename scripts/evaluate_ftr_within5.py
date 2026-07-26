"""Evaluate the 先跌后涨5日内 (within-5-bars) variant, same conventions as the
5日外 evaluation so results are directly comparable.

Usage:
    python scripts/evaluate_ftr_within5.py --vipdoc "D:/通达信/vipdoc"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from evaluate_fall_then_rise import (  # noqa: E402
    evaluate_market,
    evaluate_snapshots,
)

from stock_analyzer.feature.tdx_indicators import (  # noqa: E402
    compute_fall_then_rise_within5,
)

CONDITION_COLUMNS = (
    "ftr_m60_dtg", "ftr_mtm_dtg", "ftr_hist_turn", "ftr_ma5x28",
    "ftr_ma7x35", "ftr_above_ma", "ftr_bull_ma",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vipdoc", default=r"D:/通达信/vipdoc")
    parser.add_argument(
        "--snapshots-db", default="artifacts/training/learning_protocol.duckdb"
    )
    parser.add_argument("--out", default="artifacts/analysis/ftr_within5_eval.json")
    parser.add_argument("--full-start", default="2026-01-05")
    parser.add_argument("--full-end", default="2026-07-16")
    parser.add_argument("--daily-start", default="2023-01-01")
    parser.add_argument("--daily-end", default="2026-07-16")
    args = parser.parse_args()

    vipdoc = Path(args.vipdoc)
    report: dict[str, object] = {"hold_days": 5, "variant": "先跌后涨5日内"}
    print("[1/2] overlay on system candidates ...")
    report["snapshot_overlay"] = evaluate_snapshots(
        vipdoc,
        Path(args.snapshots_db),
        compute_fn=compute_fall_then_rise_within5,
        condition_columns=CONDITION_COLUMNS,
    )
    print("[2/2] whole-market ...")
    report["market_wide"] = evaluate_market(
        vipdoc,
        full_start=args.full_start,
        full_end=args.full_end,
        daily_start=args.daily_start,
        daily_end=args.daily_end,
        compute_fn=compute_fall_then_rise_within5,
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"report written to {out_path}")


if __name__ == "__main__":
    main()
