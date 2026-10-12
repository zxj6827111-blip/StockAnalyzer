from __future__ import annotations

import csv
import gzip
import json
from pathlib import Path

import pytest

from scripts.audit_tail_event_feature_provenance import _SOURCES, build_audit


def _write_source(
    root: Path,
    name: str,
    rows: list[dict[str, str]],
    *,
    reported_rows: int | None = None,
) -> None:
    spec = _SOURCES[name]
    data_path = root / str(spec["filename"])
    with gzip.open(data_path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(spec["columns"]))
        writer.writeheader()
        writer.writerows(rows)
    counts: dict[str, int] = {}
    for row in rows:
        day = row["trade_date"]
        counts[day] = counts.get(day, 0) + 1
    report = {
        "api": name,
        "requested_days": 2,
        "collected_days": 2,
        "days_failed": {},
        "empty_days": [],
        "days_at_row_limit_possibly_truncated": [],
        "start": "20250102",
        "end": "20250103",
        "rows_total": len(rows) if reported_rows is None else reported_rows,
        "rows_per_day_min": min(counts.values()),
        "rows_per_day_max": max(counts.values()),
    }
    (root / str(spec["report_filename"])).write_text(
        json.dumps(report), encoding="utf-8"
    )


def _row(name: str, day: str, symbol: str, **overrides: str) -> dict[str, str]:
    row = {column: "1" for column in _SOURCES[name]["columns"]}
    row.update({"trade_date": day, "ts_code": symbol})
    row.update(overrides)
    return row


def _write_complete_pair(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "tail_trading_calendar_2025_raw_v2.csv").write_text(
        "trade_date,is_open,is_decision_session\n"
        "2025-01-02,true,true\n"
        "2025-01-03,true,true\n",
        encoding="utf-8",
    )
    _write_source(
        root,
        "moneyflow",
        [
            _row("moneyflow", "20250102", "000001.SZ"),
            _row("moneyflow", "20250103", "000001.SZ"),
        ],
    )
    _write_source(
        root,
        "top_list",
        [
            _row("top_list", "20250102", "000001.SZ", reason="reason-a"),
            _row("top_list", "20250102", "000001.SZ", reason="reason-b"),
            _row("top_list", "20250103", "000002.SZ", reason="reason-c"),
        ],
    )


def test_audit_keeps_pit_blocked_and_counts_top_list_symbol_days(tmp_path: Path) -> None:
    _write_complete_pair(tmp_path)

    report = build_audit(tmp_path)

    assert report["status"] == "blocked"
    assert report["point_in_time_proven"] is False
    assert report["formal_stage2_event_arms_eligible"] is False
    assert report["sources"]["moneyflow"]["reported_daily_response_coverage"] == "complete"
    assert report["sources"]["moneyflow"]["observed"]["duplicate_symbol_day_rows"] == 0
    assert report["sources"]["top_list"]["observed"]["rows"] == 3
    assert report["sources"]["top_list"]["observed"]["unique_symbol_days"] == 2
    assert report["sources"]["top_list"]["observed"]["duplicate_symbol_day_rows"] == 1
    assert report["sources"]["top_list"]["observed"]["duplicate_symbol_day_keys"] == 1
    assert report["sources"]["top_list"]["observed"][
        "duplicate_symbol_days_with_multiple_reasons"
    ] == 1
    assert "historical_vendor_capture_timestamp_and_per_day_response_hashes_absent" in report[
        "blockers"
    ]


def test_audit_rejects_moneyflow_row_count_disagreement(tmp_path: Path) -> None:
    _write_complete_pair(tmp_path)
    report_path = tmp_path / str(_SOURCES["moneyflow"]["report_filename"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["rows_total"] += 1
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = build_audit(tmp_path)

    assert result["sources"]["moneyflow"]["reported_daily_response_coverage"] == (
        "incomplete_or_unverified"
    )


def test_audit_rejects_duplicate_moneyflow_symbol_days(tmp_path: Path) -> None:
    _write_complete_pair(tmp_path)
    rows = [
        _row("moneyflow", "20250102", "000001.SZ"),
        _row("moneyflow", "20250102", "000001.SZ"),
        _row("moneyflow", "20250103", "000001.SZ"),
    ]
    _write_source(tmp_path, "moneyflow", rows)

    with pytest.raises(ValueError, match="duplicate symbol-days"):
        build_audit(tmp_path)
