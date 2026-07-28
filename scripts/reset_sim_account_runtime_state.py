#!/usr/bin/env python3
"""Offline reset of simulation equity / portfolio in runtime_state.json.

Use this on NAS host when live SET_EQUITY cannot stick because:
1) CLI opens a fresh process while api/scheduler still hold equity=0.81 in memory
2) historical sim trades recompute equity back below protect_line

Recommended flow (containers stopped):
  docker stop stock-analyzer-api stock-analyzer-scheduler
  python3 scripts/reset_sim_account_runtime_state.py --state artifacts/runtime/runtime_state.json
  # or with docker python image if host has no python:
  # docker run --rm -v "$PWD/artifacts:/app/artifacts" -v "$PWD/scripts:/scripts:ro" \\
  #   python:3.11-slim python /scripts/reset_sim_account_runtime_state.py \\
  #   --state /app/artifacts/runtime/runtime_state.json
  docker start stock-analyzer-api
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reset sim equity/portfolio in runtime_state.json")
    parser.add_argument(
        "--state",
        default="artifacts/runtime/runtime_state.json",
        help="Path to runtime_state.json",
    )
    parser.add_argument(
        "--equity",
        type=float,
        default=1.0,
        help="Target current_equity after reset (default 1.0)",
    )
    parser.add_argument(
        "--keep-portfolio",
        action="store_true",
        help="Do not clear portfolio positions/trades",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip writing .bak timestamp copy",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.equity <= 0:
        raise SystemExit("equity must be > 0")

    path = Path(args.state)
    if not path.exists():
        raise SystemExit(f"state file not found: {path}")

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit("runtime_state.json root must be an object")

    portfolio = raw.get("portfolio") if isinstance(raw.get("portfolio"), dict) else {}
    positions = portfolio.get("positions") if isinstance(portfolio.get("positions"), list) else []
    trades = portfolio.get("trades") if isinstance(portfolio.get("trades"), list) else []

    print(
        "before",
        {
            "current_equity": raw.get("current_equity"),
            "pause_new_buy": raw.get("pause_new_buy"),
            "positions": len(positions),
            "trades": len(trades),
        },
    )

    if not args.no_backup:
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        backup = path.with_name(f"{path.name}.bak.{stamp}")
        shutil.copy2(path, backup)
        print("backup", str(backup))

    raw["current_equity"] = float(args.equity)
    raw["pause_new_buy"] = False
    if not args.keep_portfolio:
        raw["portfolio"] = {"trade_seq": 0, "positions": [], "trades": []}

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(
        json.dumps(raw, ensure_ascii=False, separators=(",", ":"), default=str),
        encoding="utf-8",
    )
    tmp.replace(path)

    after_portfolio = raw.get("portfolio") if isinstance(raw.get("portfolio"), dict) else {}
    after_positions = (
        after_portfolio.get("positions")
        if isinstance(after_portfolio.get("positions"), list)
        else []
    )
    after_trades = (
        after_portfolio.get("trades") if isinstance(after_portfolio.get("trades"), list) else []
    )
    print(
        "after",
        {
            "current_equity": raw.get("current_equity"),
            "pause_new_buy": raw.get("pause_new_buy"),
            "positions": len(after_positions),
            "trades": len(after_trades),
            "path": str(path),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
