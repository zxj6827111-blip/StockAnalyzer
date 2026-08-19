from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from scripts import build_vendor_intraday_summary as builder
from stock_analyzer.data.market_warehouse import MarketWarehouse


def _write_minute_zip(path: Path, *, entry_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = pd.DataFrame(
        {
            "datetime": [
                "2026-08-17 09:31:00",
                "2026-08-17 09:32:00",
                "2026-08-18 09:31:00",
                "2026-08-18 09:32:00",
            ],
            "open": [10.0, 10.1, 10.2, 10.3],
            "high": [10.2, 10.3, 10.4, 10.5],
            "low": [9.9, 10.0, 10.1, 10.2],
            "close": [10.1, 10.2, 10.3, 10.4],
            "volume": [100.0, 120.0, 130.0, 140.0],
            "amount": [1010.0, 1224.0, 1339.0, 1456.0],
        }
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(entry_name, rows.to_csv(index=False).encode("utf-8"))


@pytest.mark.parametrize(
    ("entry_name", "expected"),
    [
        ("600000.csv", "600000"),
        ("sh600000_2026.csv", "600000"),
        ("nested/600000.SH_202608.csv", "600000"),
        ("nested/600000_20260818.csv", "600000"),
    ],
)
def test_entry_symbol_accepts_annual_and_monthly_names(
    entry_name: str,
    expected: str,
) -> None:
    assert builder._entry_symbol(entry_name) == expected


def test_build_summary_opens_each_source_zip_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "vendor"
    archive_path = source_root / "StockA_1min_vendor-now" / "2026-08.zip"
    _write_minute_zip(archive_path, entry_name="monthly/600000_202608.csv")
    output = tmp_path / "vendor_intraday_summary.duckdb"

    real_zip_file = zipfile.ZipFile
    open_count = 0

    def _counting_zip_file(file: object, *args: object, **kwargs: object) -> zipfile.ZipFile:
        nonlocal open_count
        if Path(file) == archive_path:
            open_count += 1
        return real_zip_file(file, *args, **kwargs)

    monkeypatch.setattr(builder.zipfile, "ZipFile", _counting_zip_file)

    manifest = builder.build_summary(
        root=source_root,
        output=output,
        keep_days=480,
        intervals=("1m",),
        volume_multiplier=1.0,
    )

    assert open_count == 1
    assert output.exists()
    assert builder.manifest_path(output).exists()
    assert manifest["coverage"]["1m"]["max_date"] == "2026-08-18"
    assert manifest["zip_fingerprint"]["1m"]["archives"] == [
        {
            "path": "StockA_1min_vendor-now/2026-08.zip",
            "size": archive_path.stat().st_size,
            "mtime_ns": archive_path.stat().st_mtime_ns,
            "entries": 1,
        }
    ]

    warehouse = MarketWarehouse(
        db_path=output,
        package_root=tmp_path / "package",
        package_writes_enabled=False,
        read_only=True,
    )
    summary = warehouse.fetch_intraday_summary(
        symbol="600000",
        interval="1m",
        lookback_days=10,
    )
    assert list(summary.index.strftime("%Y-%m-%d")) == ["2026-08-17", "2026-08-18"]


def test_promote_restores_old_pair_when_manifest_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "summary.duckdb"
    final_manifest = builder.manifest_path(output)
    built = Path(str(output) + ".next")
    built_manifest = builder.manifest_path(built)
    output.write_text("old-db", encoding="utf-8")
    final_manifest.write_text(json.dumps({"generation": "old"}), encoding="utf-8")
    built.write_text("new-db", encoding="utf-8")
    built_manifest.write_text(json.dumps({"generation": "new"}), encoding="utf-8")

    real_replace = builder.os.replace

    def _replace(source: object, destination: object) -> None:
        if Path(source) == built_manifest and Path(destination) == final_manifest:
            raise OSError("simulated manifest promotion failure")
        real_replace(source, destination)

    monkeypatch.setattr(builder.os, "replace", _replace)

    with pytest.raises(OSError, match="manifest promotion failure"):
        builder._promote(output, built, built_manifest)

    assert output.read_text(encoding="utf-8") == "old-db"
    assert json.loads(final_manifest.read_text(encoding="utf-8")) == {"generation": "old"}
    assert not Path(str(output) + ".previous").exists()
    assert not builder.manifest_path(Path(str(output) + ".previous")).exists()
