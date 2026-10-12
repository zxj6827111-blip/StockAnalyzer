from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_financial_pit_research_panel.py"
_SPEC = importlib.util.spec_from_file_location("build_financial_pit_research_panel", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def _snapshots() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "symbol": "000001",
            "end_date": "2024-12-31",
            "ann_date": "2025-03-01",
            "roe": 12.5,
            "debt_ratio": 48.0,
            "update_flag": 0,
            "financial_report_date": "2024-12-31",
            "financial_as_of": "2025-03-01",
            "financial_source": "tushare_fina_indicator",
            "financial_trust_level": "reported",
            "financial_missing_fields": "",
            "financial_data_complete": True,
            "financial_completeness": 1.0,
            "coverage_complete": True,
            "as_of": "2025-03-02T09:15:00+08:00",
            "source": "tushare_fina_indicator",
        },
        {
            "symbol": "000001",
            "end_date": "2025-03-31",
            "ann_date": "2025-04-30",
            "roe": 10.0,
            "debt_ratio": 50.0,
            "update_flag": 0,
            "financial_report_date": "2025-03-31",
            "financial_as_of": "2025-04-30",
            "financial_source": "tushare_fina_indicator",
            "financial_trust_level": "reported",
            "financial_missing_fields": "",
            "financial_data_complete": True,
            "financial_completeness": 1.0,
            "coverage_complete": True,
            "as_of": "2025-05-01T09:15:00+08:00",
            "source": "tushare_fina_indicator",
        },
    ])


def test_panel_uses_only_announced_financial_snapshots() -> None:
    daily = pd.DataFrame({
        "symbol": ["000001"] * 5,
        "date": pd.to_datetime([
            "2025-02-28", "2025-03-01", "2025-03-02", "2025-04-30", "2025-05-01",
        ]),
    })

    panel, summary = _MODULE.build_panel_frame(daily, _snapshots())

    assert panel.loc[0, "financial_as_of"] is pd.NaT or pd.isna(
        panel.loc[0, "financial_as_of"]
    )
    assert panel.loc[0, "financial_source"] == "tushare_pending"
    assert panel.loc[1, "financial_as_of"] == date(2025, 3, 1)
    assert panel.loc[2, "roe"] == pytest.approx(12.5)
    assert panel.loc[3, "roe"] == pytest.approx(10.0)
    assert panel.loc[4, "financial_report_date"] == date(2025, 3, 31)
    assert summary["financial_as_of_future_rows"] == 0
    assert summary["financial_report_date_future_rows"] == 0
    assert summary["financial_as_of_same_day_rows"] == 2
    assert "date-only" in summary["same_day_timestamp_limitation"]


def test_panel_excludes_a_future_report_period_disclosed_before_period_end() -> None:
    snapshots = _snapshots()
    snapshots.loc[0, "end_date"] = "2025-03-31"
    snapshots.loc[0, "financial_report_date"] = "2025-03-31"
    panel, summary = _MODULE.build_panel_frame(
        pd.DataFrame({"symbol": ["000001"], "date": ["2025-03-02"]}),
        snapshots,
    )
    assert pd.isna(panel.loc[0, "financial_as_of"])
    assert summary["invalid_snapshot_rows_excluded"] == 1
    assert summary["invalid_snapshot_sample"] == [{
        "symbol": "000001",
        "end_date": "2025-03-31 00:00:00",
        "ann_date": "2025-03-01 00:00:00",
    }]


def test_build_research_panel_writes_new_file_without_mutating_source(tmp_path) -> None:
    source = tmp_path / "source.duckdb"
    output = tmp_path / "pit-panel.duckdb"
    con = duckdb.connect(str(source))
    try:
        con.execute("CREATE TABLE daily_bars(symbol VARCHAR, date DATE)")
        con.execute(
            "INSERT INTO daily_bars VALUES "
            "('000001', '2025-03-01'), ('000001', '2025-03-02')"
        )
        con.register("snapshots", _snapshots())
        con.execute(
            "CREATE TABLE financial_snapshots AS SELECT * FROM snapshots"
        )
        source_rows_before = con.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
    finally:
        con.close()

    manifest = _MODULE.build_research_panel(
        source_db=source,
        output_db=output,
        start=date(2025, 3, 1),
        end=date(2025, 3, 2),
    )

    source_read = duckdb.connect(str(source), read_only=True)
    target_read = duckdb.connect(str(output), read_only=True)
    try:
        assert (
            source_read.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
            == source_rows_before
        )
        assert target_read.execute("SELECT COUNT(*) FROM pit_financial_panel").fetchone()[0] == 2
        stored_manifest = json.loads(
            target_read.execute("SELECT manifest_json FROM audit_manifest").fetchone()[0]
        )
        assert stored_manifest["summary"]["financial_as_of_future_rows"] == 0
        assert manifest["summary"]["daily_rows"] == 2
    finally:
        source_read.close()
        target_read.close()

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        _MODULE.build_research_panel(
            source_db=source,
            output_db=output,
            start=date(2025, 3, 1),
            end=date(2025, 3, 2),
        )
