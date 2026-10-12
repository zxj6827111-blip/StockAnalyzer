from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from scripts.sync_tail_trade_references import sync_bundle

AS_OF = "2026-10-10T10:00:00+08:00"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bundle(tmp_path: Path, *, complete: bool = True, approximated: bool = False):
    daily_path = tmp_path / "daily.csv"
    limits_path = tmp_path / "limits.csv"
    status_path = tmp_path / "status.csv"
    pd.DataFrame(
        [
            {
                "symbol": "000001",
                "date": "2026-01-05",
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.1,
                "volume": 1000.0,
                "source": "tushare_daily_unadjusted",
                "as_of": AS_OF,
            }
        ]
    ).to_csv(daily_path, index=False)
    pd.DataFrame(
        [
            {
                "symbol": "000001",
                "trade_date": "2026-01-05",
                "up_limit": 11.0,
                "down_limit": 9.0,
                "approximated": approximated,
                "coverage_complete": True,
                "source": "tushare_stk_limit",
                "as_of": AS_OF,
            }
        ]
    ).to_csv(limits_path, index=False)
    pd.DataFrame(
        [
            {
                "symbol": "000001",
                "trade_date": "2026-01-05",
                "suspended": False,
                "suspend_type": "",
                "trade_status": "trading",
                "suspend_events_json": "[]",
                "coverage_complete": True,
                "source": "tushare_daily_positive_volume",
                "as_of": AS_OF,
            }
        ]
    ).to_csv(status_path, index=False)
    manifest_path = tmp_path / "collection.json"
    manifest = {
        "schema": "tail_trade_reference_collection.v2",
        "scope": "requested_symbols_only",
        "as_of": AS_OF,
        "coverage_complete": complete,
        "sources": {
            "daily_bars": "tushare_daily_unadjusted",
            "limit_prices": "tushare_stk_limit",
            "trade_status": ["tushare_daily_positive_volume", "tushare_suspend_d"],
        },
        "outputs": {
            "daily_bars": {"rows": 1, "sha256": _sha(daily_path)},
            "limit_prices": {"rows": 1, "sha256": _sha(limits_path)},
            "trade_status": {"rows": 1, "sha256": _sha(status_path)},
        },
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return daily_path, limits_path, status_path, manifest_path


def _refresh_status_hash(status_path: Path, manifest_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["outputs"]["trade_status"]["sha256"] = _sha(status_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_sync_imports_only_hash_verified_raw_reference_rows(tmp_path: Path) -> None:
    daily, limits, status, manifest = _bundle(tmp_path)
    db_path = tmp_path / "research.duckdb"

    report = sync_bundle(
        db_path=db_path,
        daily_path=daily,
        limits_path=limits,
        status_path=status,
        manifest_path=manifest,
    )

    assert report["daily_rows_written"] == 1
    assert report["exact_limit_rows_written"] == 1
    assert report["known_trade_status_rows_written"] == 1
    con = __import__("duckdb").connect(str(db_path), read_only=True)
    try:
        assert con.execute("SELECT symbol, price_basis FROM ref_daily_bars_raw").fetchone() == (
            "000001",
            "raw",
        )
        assert con.execute("SELECT approximated FROM ref_limit_prices").fetchone() == (False,)
        assert con.execute("SELECT trade_status FROM ref_suspend_status").fetchone() == ("trading",)
    finally:
        con.close()


def test_sync_requires_explicit_opt_in_for_incomplete_collection(tmp_path: Path) -> None:
    daily, limits, status, manifest = _bundle(tmp_path, complete=False)
    db_path = tmp_path / "must_not_be_created.duckdb"

    with pytest.raises(ValueError, match="collection is incomplete"):
        sync_bundle(
            db_path=db_path,
            daily_path=daily,
            limits_path=limits,
            status_path=status,
            manifest_path=manifest,
        )

    assert not db_path.exists()


def test_sync_rejects_approximated_limits_before_opening_target(tmp_path: Path) -> None:
    daily, limits, status, manifest = _bundle(tmp_path, approximated=True)
    db_path = tmp_path / "must_not_be_created.duckdb"

    with pytest.raises(ValueError, match="approximated limit prices are forbidden"):
        sync_bundle(
            db_path=db_path,
            daily_path=daily,
            limits_path=limits,
            status_path=status,
            manifest_path=manifest,
        )

    assert not db_path.exists()


def test_sync_rejects_full_day_suspension_conflicting_with_traded_status(tmp_path: Path) -> None:
    daily, limits, status, manifest = _bundle(tmp_path)
    statuses = pd.read_csv(
        status, dtype={"symbol": "string", "trade_date": "string"}, keep_default_na=False
    )
    statuses.loc[0, "suspend_events_json"] = '[{"suspend_type":"S","suspend_timing":null}]'
    statuses.to_csv(status, index=False)
    _refresh_status_hash(status, manifest)
    db_path = tmp_path / "must_not_be_created.duckdb"

    with pytest.raises(ValueError, match="trading status lacks"):
        sync_bundle(
            db_path=db_path,
            daily_path=daily,
            limits_path=limits,
            status_path=status,
            manifest_path=manifest,
        )

    assert not db_path.exists()


def test_sync_allows_verified_partial_rows_only_when_requested(tmp_path: Path) -> None:
    daily, limits, status, manifest = _bundle(tmp_path, complete=False)
    db_path = tmp_path / "partial_research.duckdb"

    report = sync_bundle(
        db_path=db_path,
        daily_path=daily,
        limits_path=limits,
        status_path=status,
        manifest_path=manifest,
        allow_partial=True,
    )

    assert report["collection_coverage_complete"] is False
    assert report["known_trade_status_rows_written"] == 1
