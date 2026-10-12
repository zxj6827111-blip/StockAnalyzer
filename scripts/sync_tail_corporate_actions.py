#!/usr/bin/env python3
"""将离线公司行动 CSV 写入独立尾盘研究库；不打开或修改生产仓库。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.research.tail_reference_store import (  # noqa: E402
    REFERENCE_DB_DEFAULT,
    TailReferenceError,
    TailReferenceStore,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    import pandas as pd

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=REFERENCE_DB_DEFAULT)
    parser.add_argument("--events-csv", required=True)
    parser.add_argument("--coverage-csv", required=True)
    parser.add_argument(
        "--adj-factors-csv", default="",
        help="optional per-symbol adj_factor history used to cross-check action days",
    )
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)
    events_path = Path(args.events_csv).expanduser()
    coverage_path = Path(args.coverage_csv).expanduser()
    if not events_path.is_file() or not coverage_path.is_file():
        parser.error("events CSV and coverage CSV must both exist")
    events = pd.read_csv(events_path, dtype={"symbol": "string", "ts_code": "string"})
    coverage = pd.read_csv(coverage_path)
    factors_path = Path(args.adj_factors_csv).expanduser() if args.adj_factors_csv else None
    factors = (
        pd.read_csv(
            factors_path,
            dtype={"symbol": "string", "ts_code": "string", "trade_date": "string"},
        )
        if factors_path is not None else None
    )
    sources = set(str(value) for value in coverage.get("source", []))
    as_of_values = set(str(value) for value in coverage.get("as_of", []))
    if len(sources) != 1 or len(as_of_values) != 1:
        parser.error("coverage CSV must contain exactly one source and one as_of value")
    source = next(iter(sources))
    as_of = next(iter(as_of_values))
    if not source or not as_of or "coverage_complete" not in coverage:
        parser.error("coverage CSV is missing its provenance or coverage flag")
    factor_source = ""
    factor_as_of = ""
    if factors is not None:
        factor_sources = set(str(value) for value in factors.get("source", []))
        factor_as_ofs = set(str(value) for value in factors.get("as_of", []))
        if len(factor_sources) != 1 or len(factor_as_ofs) != 1:
            parser.error("adj-factor CSV must contain exactly one source and one as_of value")
        factor_source = next(iter(factor_sources))
        factor_as_of = next(iter(factor_as_ofs))
    try:
        with TailReferenceStore(args.db) as reference:
            action_rows = reference.upsert_corporate_actions(
                events, source=source, as_of=as_of,
            )
            coverage_rows = reference.upsert_corporate_action_coverage(
                coverage, source=source, as_of=as_of,
            )
            factor_rows = (
                reference.upsert_adj_factors(factors, source=factor_source, as_of=factor_as_of)
                if factors is not None else 0
            )
            readiness = reference.coverage()["sources"]["corporate_action_coverage"]
            factor_readiness = reference.coverage()["sources"]["adj_factor_history"]
    except (OSError, TailReferenceError) as exc:
        print(f"公司行动同步失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 5
    report = {
        "db": str(args.db),
        "source": source,
        "as_of": as_of,
        "action_rows_written": action_rows,
        "coverage_rows_written": coverage_rows,
        "adj_factor_rows_written": factor_rows,
        "events_csv_sha256": _sha256(events_path),
        "coverage_csv_sha256": _sha256(coverage_path),
        "adj_factors_csv_sha256": _sha256(factors_path) if factors_path else None,
        "coverage": readiness,
        "adj_factor_coverage": factor_readiness,
    }
    if args.report:
        target = Path(args.report).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    incomplete = int(readiness.get("rows", 0)) == 0 or (
        int(readiness.get("complete_dates", 0)) < int(readiness.get("rows", 0))
    )
    return 5 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
