#!/usr/bin/env python3
"""Import verified RAW daily, exact limit, and explicit trade-status CSVs to research DB.

This CLI requires an explicit DuckDB target and never writes to the production warehouse.
Incomplete collections need the deliberate ``--allow-partial`` switch; any status row that
is imported must still be independently supported by its source row and RAW daily evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from stock_analyzer.research.tail_reference_store import (  # noqa: E402
    TailReferenceError,
    TailReferenceStore,
)

SCHEMA = "tail_trade_reference_collection.v2"
DAILY_SOURCE = "tushare_daily_unadjusted"
LIMIT_SOURCE = "tushare_stk_limit"
TRADED_STATUS_SOURCE = "tushare_daily_positive_volume"
SUSPEND_STATUS_SOURCE = "tushare_suspend_d"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _date(value: Any, *, field: str) -> str:
    text = str(value or "").strip()[:10]
    try:
        parsed = (
            datetime.strptime(text, "%Y%m%d").date() if len(text) == 8 else date.fromisoformat(text)
        )
    except ValueError as exc:
        raise ValueError(f"invalid {field}") from exc
    return parsed.isoformat()


def _bool(value: Any, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "t"}:
        return True
    if text in {"false", "0", "f"}:
        return False
    raise ValueError(f"invalid {field}")


def _finite_positive(value: Any, *, field: str, allow_empty: bool = False) -> float | None:
    if value is None or str(value).strip().lower() in {"", "nan", "none"}:
        if allow_empty:
            return None
        raise ValueError(f"missing {field}")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"invalid {field}")
    return parsed


def _read_csv(path: Path, *, dtypes: dict[str, str]):
    import pandas as pd

    if not path.is_file():
        raise ValueError(f"missing input file: {path}")
    frame = pd.read_csv(path, dtype=dtypes, keep_default_na=False)
    return frame


def load_bundle(
    *,
    daily_path: Path,
    limits_path: Path,
    status_path: Path,
    manifest_path: Path,
    allow_partial: bool,
):
    import pandas as pd

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA or manifest.get("scope") != "requested_symbols_only":
        raise ValueError("unsupported collection manifest")
    if not str(manifest.get("as_of") or "").strip():
        raise ValueError("manifest has no as_of")
    if manifest.get("sources") != {
        "daily_bars": DAILY_SOURCE,
        "limit_prices": LIMIT_SOURCE,
        "trade_status": [TRADED_STATUS_SOURCE, SUSPEND_STATUS_SOURCE],
    }:
        raise ValueError("manifest source contract does not match importer")
    if not bool(manifest.get("coverage_complete")) and not allow_partial:
        raise ValueError("collection is incomplete; pass --allow-partial to import proven rows")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict):
        raise ValueError("manifest has no output identities")
    paths = {
        "daily_bars": daily_path,
        "limit_prices": limits_path,
        "trade_status": status_path,
    }
    for name, path in paths.items():
        identity = outputs.get(name)
        if not isinstance(identity, dict) or identity.get("sha256") != _sha256(path):
            raise ValueError(f"{name} CSV does not match collection manifest")

    daily = _read_csv(daily_path, dtypes={"symbol": "string", "date": "string"})
    limits = _read_csv(limits_path, dtypes={"symbol": "string", "trade_date": "string"})
    statuses = _read_csv(status_path, dtypes={"symbol": "string", "trade_date": "string"})
    required = {
        "daily": {"symbol", "date", "open", "high", "low", "close", "volume", "source", "as_of"},
        "limits": {
            "symbol",
            "trade_date",
            "up_limit",
            "down_limit",
            "approximated",
            "coverage_complete",
            "source",
            "as_of",
        },
        "statuses": {
            "symbol",
            "trade_date",
            "suspended",
            "suspend_type",
            "trade_status",
            "suspend_events_json",
            "coverage_complete",
            "source",
            "as_of",
        },
    }
    for name, frame, output_name in (
        ("daily", daily, "daily_bars"),
        ("limits", limits, "limit_prices"),
        ("statuses", statuses, "trade_status"),
    ):
        missing = sorted(required[name] - set(frame.columns))
        if missing:
            raise ValueError(f"{name} CSV is missing required columns")
        if len(frame) != int(outputs[output_name].get("rows", -1)):
            raise ValueError(f"{name} CSV row count differs from manifest")

    for frame, date_column, name in (
        (daily, "date", "daily"),
        (limits, "trade_date", "limits"),
        (statuses, "trade_date", "statuses"),
    ):
        if frame.empty:
            continue
        frame["symbol"] = frame["symbol"].astype(str).str.strip()
        if not frame["symbol"].str.fullmatch(r"\d{6}").all():
            raise ValueError(f"{name} has invalid research symbols")
        frame[date_column] = frame[date_column].map(
            lambda value, column=date_column: _date(value, field=column)
        )
        if frame.duplicated(["symbol", date_column]).any():
            raise ValueError(f"{name} has duplicate symbol dates")
        if (frame["source"].astype(str).str.strip() == "").any():
            raise ValueError(f"{name} has missing source provenance")
        if (frame["as_of"].astype(str).str.strip() == "").any():
            raise ValueError(f"{name} has missing as_of provenance")

    if not set(daily.get("source", [])) <= {DAILY_SOURCE}:
        raise ValueError("daily CSV source does not match RAW contract")
    for _, row in daily.iterrows():
        prices = [
            _finite_positive(row[name], field=name) for name in ("open", "high", "low", "close")
        ]
        volume = float(row["volume"])
        if not math.isfinite(volume) or volume < 0:
            raise ValueError("daily CSV has invalid volume")
        open_, high, low, close = prices
        if high < max(open_, close) or low > min(open_, close) or high < low:
            raise ValueError("daily CSV has inconsistent OHLC")

    if not set(limits.get("source", [])) <= {LIMIT_SOURCE}:
        raise ValueError("limit CSV source is not exact Tushare stk_limit")
    for _, row in limits.iterrows():
        if _bool(row["approximated"], field="approximated"):
            raise ValueError("approximated limit prices are forbidden")
        complete = _bool(row["coverage_complete"], field="coverage_complete")
        up = _finite_positive(row["up_limit"], field="up_limit", allow_empty=not complete)
        down = _finite_positive(row["down_limit"], field="down_limit", allow_empty=not complete)
        if complete and (up is None or down is None):
            raise ValueError("complete exact limit row lacks bounds")

    traded_daily = {
        (str(row.symbol), str(row.date))
        for row in daily.itertuples(index=False)
        if float(row.volume) > 0
    }
    if not set(statuses.get("source", [])) <= {TRADED_STATUS_SOURCE, SUSPEND_STATUS_SOURCE}:
        raise ValueError("status CSV source does not match explicit-status contract")
    for _, row in statuses.iterrows():
        suspended = _bool(row["suspended"], field="suspended")
        complete = _bool(row["coverage_complete"], field="coverage_complete")
        if not complete:
            raise ValueError("status rows must each be proven complete")
        trade_status = str(row["trade_status"]).strip()
        source = str(row["source"]).strip()
        suspend_type = str(row["suspend_type"]).strip().upper()
        try:
            events = json.loads(str(row["suspend_events_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("status row has malformed suspend event evidence") from exc
        if not isinstance(events, list) or any(
            not isinstance(event, dict)
            or str(event.get("suspend_type") or "").strip().upper() not in {"S", "R"}
            or event.get("suspend_timing") is not None
            and not str(event.get("suspend_timing")).strip()
            for event in events
        ):
            raise ValueError("status row has malformed suspend event evidence")
        full_day_suspension = any(
            str(event["suspend_type"]).strip().upper() == "S"
            and event.get("suspend_timing") is None
            for event in events
        )
        key = (str(row["symbol"]), str(row["trade_date"]))
        if suspended:
            if (
                trade_status != "suspended"
                or source != SUSPEND_STATUS_SOURCE
                or suspend_type != "S"
                or not events
                or any(
                    str(event["suspend_type"]).strip().upper() != "S"
                    or event.get("suspend_timing") is not None
                    for event in events
                )
                or key in traded_daily
            ):
                raise ValueError("suspension row conflicts with RAW traded-day evidence")
        else:
            if (
                trade_status != "trading"
                or source != TRADED_STATUS_SOURCE
                or suspend_type
                or full_day_suspension
                or key not in traded_daily
            ):
                raise ValueError("trading status lacks positive-volume RAW daily evidence")

    for frame in (daily, limits, statuses):
        if not frame.empty and not (frame["as_of"].astype(str) == manifest["as_of"]).all():
            raise ValueError("as_of provenance differs from manifest")

    # Normalize all values before the database is opened so bad bundles cannot partially write.
    if not daily.empty:
        daily["volume"] = pd.to_numeric(daily["volume"], errors="raise")
    if not limits.empty:
        limits["approximated"] = limits["approximated"].map(
            lambda value: _bool(value, field="approximated")
        )
        limits["coverage_complete"] = limits["coverage_complete"].map(
            lambda value: _bool(value, field="coverage_complete")
        )
    if not statuses.empty:
        statuses["suspended"] = statuses["suspended"].map(
            lambda value: _bool(value, field="suspended")
        )
        statuses["coverage_complete"] = statuses["coverage_complete"].map(
            lambda value: _bool(value, field="coverage_complete")
        )
    return daily, limits, statuses, manifest


def sync_bundle(
    *,
    db_path: Path,
    daily_path: Path,
    limits_path: Path,
    status_path: Path,
    manifest_path: Path,
    allow_partial: bool = False,
) -> dict[str, Any]:
    resolved = db_path.expanduser().resolve()
    normalized = str(resolved).lower()
    if resolved.suffix.lower() != ".duckdb" or "market_warehouse" in resolved.name.lower():
        raise ValueError("target must be an explicitly named research .duckdb file")
    if "/vol1/docker/stockanalyzer" in normalized:
        raise ValueError("refusing to write inside the production NAS project")
    daily, limits, statuses, manifest = load_bundle(
        daily_path=daily_path,
        limits_path=limits_path,
        status_path=status_path,
        manifest_path=manifest_path,
        allow_partial=allow_partial,
    )
    with TailReferenceStore(resolved) as reference:
        reference._con.execute("BEGIN TRANSACTION")
        try:
            daily_written = reference.upsert_daily_bars(
                daily,
                price_basis="raw",
                source=DAILY_SOURCE,
                as_of=manifest["as_of"],
            )
            limits_written = reference.upsert_limit_prices(
                limits,
                source=LIMIT_SOURCE,
                as_of=manifest["as_of"],
                approximated=False,
            )
            statuses_written = reference.upsert_suspend_status(
                statuses,
                source="tushare_explicit_trade_status",
                as_of=manifest["as_of"],
            )
            reference._con.execute("COMMIT")
        except Exception:
            reference._con.execute("ROLLBACK")
            raise
        coverage = reference.coverage()
    return {
        "db": str(resolved),
        "collection_coverage_complete": bool(manifest["coverage_complete"]),
        "daily_rows_written": daily_written,
        "exact_limit_rows_written": limits_written,
        "known_trade_status_rows_written": statuses_written,
        "input_sha256": {
            "daily_bars": _sha256(daily_path),
            "limit_prices": _sha256(limits_path),
            "trade_status": _sha256(status_path),
            "manifest": _sha256(manifest_path),
        },
        "coverage": coverage,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="explicit local research DuckDB path")
    parser.add_argument("--daily-csv", required=True)
    parser.add_argument("--limits-csv", required=True)
    parser.add_argument("--status-csv", required=True)
    parser.add_argument("--manifest", required=True, help="collector report JSON")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)
    try:
        report = sync_bundle(
            db_path=Path(args.db),
            daily_path=Path(args.daily_csv),
            limits_path=Path(args.limits_csv),
            status_path=Path(args.status_csv),
            manifest_path=Path(args.manifest),
            allow_partial=args.allow_partial,
        )
    except (OSError, ValueError, TailReferenceError) as exc:
        print(f"trade reference sync failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 5
    if args.report:
        target = Path(args.report).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
