from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from scripts.collect_tail_security_status import (
    CollectionError,
    _normalize_code,
    collect_security_status,
)
from scripts.sync_tail_reference_data import security_status_frame_from_payload


class _FakeTushare:
    def __init__(self, *, fail_symbol: str = "") -> None:
        self.fail_symbol = fail_symbol
        self.calls: list[tuple[str, str]] = []

    def stock_basic(self, *, list_status: str, fields: str) -> pd.DataFrame:
        self.calls.append(("stock_basic", list_status))
        rows = {
            "L": [
                {
                    "ts_code": "000001.SZ",
                    "name": "平安银行",
                    "list_date": "19910403",
                    "delist_date": None,
                    "list_status": "L",
                    "market": "主板",
                    "exchange": "SZSE",
                },
                {
                    "ts_code": "600018.SH",
                    "name": "现存证券",
                    "list_date": "20061026",
                    "delist_date": None,
                    "list_status": "L",
                    "market": "主板",
                    "exchange": "SSE",
                },
            ],
            "D": [
                {
                    "ts_code": "600001.SH",
                    "name": "退市测试",
                    "list_date": "19910403",
                    "delist_date": "20200102",
                    "list_status": "D",
                    "market": "主板",
                    "exchange": "SSE",
                },
                {
                    "ts_code": "T600018.SH",
                    "name": "退市历史代码",
                    "list_date": "20000719",
                    "delist_date": "20061020",
                    "list_status": "D",
                    "market": None,
                    "exchange": "SSE",
                },
            ],
            "P": [],
        }[list_status]
        return pd.DataFrame(rows, columns=fields.split(","))

    def namechange(self, *, ts_code: str, fields: str) -> pd.DataFrame:
        self.calls.append(("namechange", ts_code))
        if ts_code == self.fail_symbol:
            raise TimeoutError
        rows = []
        if ts_code == "000001.SZ":
            rows.append(
                {
                    "ts_code": ts_code,
                    "name": "平安银行",
                    "start_date": "19910403",
                    "end_date": float("nan"),
                    "ann_date": "19910403",
                    "change_reason": "上市",
                }
            )
        return pd.DataFrame(rows, columns=fields.split(","))


def test_collects_per_symbol_responses_and_resumes_successful_checkpoints(
    tmp_path: Path,
) -> None:
    checkpoint = tmp_path / "checkpoint"
    output = tmp_path / "security_status.json"
    api = _FakeTushare()

    payload = collect_security_status(
        api,
        checkpoint_dir=checkpoint,
        output_path=output,
        request_interval_sec=0,
    )

    assert payload["schema"] == "tail_security_status_payload.v2"
    assert payload["namechange_collection"]["requested_symbols"] == 4
    assert payload["namechange_collection"]["complete_symbols"] == 4
    assert payload["namechange_collection"]["coverage_by_symbol"] == {
        "000001.SZ": True,
        "600018.SH": True,
        "600001.SH": True,
        "T600018.SH": True,
    }
    assert ("namechange", "T600018.SH") in api.calls
    assert len(payload["namechange"]["rows"]) == 1
    first_call_count = len(api.calls)

    resumed = collect_security_status(
        _FakeTushare(),
        checkpoint_dir=checkpoint,
        output_path=output,
        request_interval_sec=0,
    )

    assert resumed["collection_run_id"] == payload["collection_run_id"]
    assert len(api.calls) == first_call_count
    assert json.loads((checkpoint / "manifest.json").read_text())["complete_symbols"] == 4


def test_legacy_t_prefix_requires_delisted_scope() -> None:
    with pytest.raises(CollectionError, match="invalid_or_missing_ts_code"):
        _normalize_code("T600018.SH")
    assert _normalize_code("T600018.SH", allow_legacy_delisted=True) == "T600018.SH"


def test_delisted_legacy_t_code_keeps_a_distinct_symbol_identity(tmp_path: Path) -> None:
    payload = collect_security_status(
        _FakeTushare(),
        checkpoint_dir=tmp_path / "checkpoint",
        output_path=tmp_path / "security_status.json",
        request_interval_sec=0,
    )

    frame = security_status_frame_from_payload(payload)
    legacy = frame.loc[frame["symbol"] == "T600018"]
    current = frame.loc[frame["symbol"] == "600018"]
    assert set(legacy["status_type"]) == {"delisting", "name_change_coverage"}
    assert set(current["status_type"]) == {"listing_period", "name_change_coverage"}
    assert bool(legacy["coverage_complete"].all())
    assert bool(current["coverage_complete"].all())


def test_failed_symbol_is_retained_as_incomplete_and_retried_on_resume(
    tmp_path: Path,
) -> None:
    checkpoint = tmp_path / "checkpoint"
    output = tmp_path / "security_status.json"
    failed = _FakeTushare(fail_symbol="600001.SH")

    partial = collect_security_status(
        failed,
        checkpoint_dir=checkpoint,
        output_path=output,
        request_interval_sec=0,
        retries=0,
    )

    assert partial["namechange_collection"]["incomplete_symbols"] == ["600001.SH"]
    assert len(partial["namechange"]["rows"]) == 1

    recovered = collect_security_status(
        _FakeTushare(),
        checkpoint_dir=checkpoint,
        output_path=output,
        request_interval_sec=0,
        retries=0,
    )

    assert recovered["namechange_collection"]["incomplete_symbols"] == []


def test_security_status_import_preserves_per_symbol_coverage_and_empty_history(
    tmp_path: Path,
) -> None:
    payload = collect_security_status(
        _FakeTushare(fail_symbol="600001.SH"),
        checkpoint_dir=tmp_path / "checkpoint",
        output_path=tmp_path / "security_status.json",
        request_interval_sec=0,
        retries=0,
    )

    frame = security_status_frame_from_payload(payload)
    markers = frame[frame["status_type"] == "name_change_coverage"].set_index("symbol")
    assert bool(markers.loc["000001", "coverage_complete"])
    assert not bool(markers.loc["600001", "coverage_complete"])
    assert set(frame["status_type"]) == {
        "listing_period",
        "delisting",
        "name_change",
        "name_change_coverage",
    }
    assert markers.loc["000001", "effective_from"] == payload["as_of"][:10]
    assert markers.loc["000001", "as_of"] == payload["as_of"]


def test_namechange_payload_preserves_announcement_date() -> None:
    frame = security_status_frame_from_payload(
        {
            "as_of": "2026-10-11T00:00:00+00:00",
            "namechange": {
                "rows": [
                    {
                        "ts_code": "000001.SZ",
                        "name": "平安银行",
                        "start_date": "20260929",
                        "end_date": None,
                        "ann_date": "20260908",
                    }
                ]
            },
            "namechange_total": 1,
        }
    )
    row = frame.loc[frame["status_type"] == "name_change"].iloc[0]
    assert row["effective_from"] == "2026-09-29"
    assert row["announcement_date"] == "2026-09-08"


def test_legacy_global_namechange_cap_still_fails_closed() -> None:
    frame = security_status_frame_from_payload(
        {
            "basic_L": {"rows": [["000001.SZ", "平安银行", "19910403", None]]},
            "basic_D": {"rows": []},
            "namechange_total": 10_000,
            "namechange": {
                "rows": [["000001.SZ", "平安银行", "19910403", None]]
            },
        }
    )
    assert not bool(frame.loc[frame["status_type"] == "name_change", "coverage_complete"].iloc[0])
