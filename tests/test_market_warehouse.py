from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from stock_analyzer.data.market_warehouse import MarketWarehouse, load_package_daily_bars


def _build_sample_package(root: Path) -> None:
    bars_root = root / "bars"
    bars_root.mkdir(parents=True, exist_ok=True)
    daily = pd.DataFrame(
        {
            "date": ["2026-03-03", "2026-03-04", "2026-03-05"],
            "open": [10.0, 10.2, 10.4],
            "high": [10.2, 10.5, 10.7],
            "low": [9.9, 10.1, 10.3],
            "close": [10.1, 10.4, 10.6],
            "volume": [1_000_000, 1_100_000, 1_200_000],
            "turnover": [10_100_000.0, 11_440_000.0, 12_720_000.0],
            "float_market_cap": [12_000_000_000.0, 12_000_000_000.0, 12_000_000_000.0],
            "name": ["示例股份", "示例股份", "示例股份"],
            "roe": [0.12, 0.12, 0.12],
            "debt_ratio": [0.32, 0.32, 0.32],
            "holder_count": [40_000.0, 40_100.0, 40_200.0],
            "block_trade_net": [0.0, 100_000.0, 0.0],
            "financing_balance": [1_000_000_000.0, 1_010_000_000.0, 1_020_000_000.0],
            "margin_financing_balance": [1_000_000_000.0, 1_010_000_000.0, 1_020_000_000.0],
            "northbound_net": [0.0, 0.0, 200_000.0],
            "dragon_tiger_flag": [0.0, 0.0, 1.0],
        }
    )
    daily.to_csv(bars_root / "600000.csv", index=False)

    for interval in ("1m", "5m"):
        interval_root = root / "intraday_summary" / interval
        interval_root.mkdir(parents=True, exist_ok=True)
        summary = pd.DataFrame(
            {
                "symbol": ["600000", "600000"],
                "date": ["2026-03-04", "2026-03-05"],
                "minute_count": [240, 240],
                "session_return": [0.01, 0.02],
                "session_range_pct": [0.03, 0.04],
                "realized_vol": [0.02, 0.03],
                "vwap_gap": [0.001, 0.002],
                "am_return": [0.003, 0.004],
                "pm_return": [0.007, 0.016],
                "am_pm_diff": [0.004, 0.012],
                "last30_return": [0.002, 0.003],
                "last30_volume_share": [0.15, 0.16],
                "positive_bar_ratio": [0.55, 0.60],
                "close_position": [0.7, 0.8],
            }
        )
        summary.to_csv(interval_root / "600000.csv.gz", index=False, compression="gzip")


def test_market_warehouse_bootstrap_imports_daily_and_intraday(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    _build_sample_package(package_root)
    warehouse = MarketWarehouse(
        db_path=tmp_path / "warehouse" / "market.duckdb",
        package_root=package_root,
    )

    report = warehouse.bootstrap_from_offline_package(source_root=package_root)

    assert report["symbols_total"] == 1
    assert report["daily_written"] == 1
    assert report["intraday_written"] == {"1m": 1, "5m": 1}
    assert warehouse.list_symbols() == ["600000"]

    bars = warehouse.fetch_daily_bars(symbol="600000", lookback_days=10)
    assert len(bars) == 3
    assert bars["dragon_tiger_flag"].iloc[-1] == 1.0
    assert warehouse.latest_daily_date(symbol="600000") is not None
    assert warehouse.latest_daily_dates(symbols=["600000"]) == {"600000": bars.index[-1].date()}

    intraday_1m = warehouse.fetch_intraday_summary(symbol="600000", interval="1m", lookback_days=10)
    assert len(intraday_1m) == 2
    assert float(intraday_1m["session_return"].iloc[-1]) == 0.02
    assert warehouse.latest_intraday_date(symbol="600000", interval="1m") is not None
    assert warehouse.latest_intraday_dates(interval="1m", symbols=["600000"]) == {
        "600000": intraday_1m.index[-1].date()
    }

    assert (package_root / "manifest.json").exists() is True
    assert (package_root / "intraday_summary_manifest.json").exists() is True


def test_market_warehouse_read_paths_do_not_use_schema_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "package"
    _build_sample_package(package_root)
    warehouse = MarketWarehouse(
        db_path=tmp_path / "warehouse" / "market.duckdb",
        package_root=package_root,
    )
    warehouse.bootstrap_from_offline_package(source_root=package_root)

    def _fail_ensure_schema() -> None:
        raise AssertionError("read paths should not call ensure_schema")

    def _fail_connect_write() -> None:
        raise AssertionError("read paths should not open write connections")

    monkeypatch.setattr(warehouse, "ensure_schema", _fail_ensure_schema)
    monkeypatch.setattr(warehouse, "_connect_write", _fail_connect_write)

    assert warehouse.has_daily_data() is True
    assert warehouse.list_symbols() == ["600000"]
    assert warehouse.latest_daily_date(symbol="600000") == pd.Timestamp("2026-03-05").date()
    assert warehouse.latest_daily_dates(symbols=["600000"]) == {
        "600000": pd.Timestamp("2026-03-05").date()
    }
    assert len(warehouse.fetch_daily_bars(symbol="600000", lookback_days=2)) == 2
    assert (
        warehouse.latest_intraday_date(symbol="600000", interval="1m")
        == pd.Timestamp("2026-03-05").date()
    )
    assert warehouse.latest_intraday_dates(interval="1m", symbols=["600000"]) == {
        "600000": pd.Timestamp("2026-03-05").date()
    }
    assert (
        len(warehouse.fetch_intraday_summary(symbol="600000", interval="1m", lookback_days=2)) == 2
    )

    manifests = warehouse.refresh_package_manifests()
    daily_manifest_path = manifests["daily_manifest_path"]
    intraday_manifest_path = manifests["intraday_manifest_path"]
    assert isinstance(daily_manifest_path, str)
    assert isinstance(intraday_manifest_path, str)
    assert Path(daily_manifest_path).exists() is True
    assert Path(intraday_manifest_path).exists() is True


def test_market_warehouse_read_paths_return_empty_when_database_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warehouse = MarketWarehouse(
        db_path=tmp_path / "warehouse" / "market.duckdb",
        package_root=tmp_path / "package",
    )

    def _fail_ensure_schema() -> None:
        raise AssertionError("missing database reads should not create schema")

    def _fail_connect_write() -> None:
        raise AssertionError("missing database reads should not open write connections")

    monkeypatch.setattr(warehouse, "ensure_schema", _fail_ensure_schema)
    monkeypatch.setattr(warehouse, "_connect_write", _fail_connect_write)

    assert warehouse.has_daily_data() is False
    assert warehouse.list_symbols() == []
    assert warehouse.latest_daily_date(symbol="600000") is None
    assert warehouse.latest_daily_dates(symbols=["600000"]) == {}
    assert warehouse.fetch_daily_bars(symbol="600000", lookback_days=10).empty
    assert warehouse.fetch_all_daily_bars(symbol="600000").empty
    assert warehouse.latest_intraday_date(symbol="600000", interval="1m") is None
    assert warehouse.latest_intraday_dates(interval="1m", symbols=["600000"]) == {}
    assert warehouse.fetch_intraday_summary(symbol="600000", interval="1m", lookback_days=10).empty
    assert warehouse.fetch_all_intraday_summary(symbol="600000", interval="1m").empty

    manifests = warehouse.refresh_package_manifests()
    daily_manifest_path = manifests["daily_manifest_path"]
    intraday_manifest_path = manifests["intraday_manifest_path"]
    assert isinstance(daily_manifest_path, str)
    assert isinstance(intraday_manifest_path, str)
    assert Path(daily_manifest_path).exists() is True
    assert Path(intraday_manifest_path).exists() is True


def test_market_warehouse_read_connection_stays_compatible_with_open_write_connection(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    _build_sample_package(package_root)
    warehouse = MarketWarehouse(
        db_path=tmp_path / "warehouse" / "market.duckdb",
        package_root=package_root,
    )
    warehouse.bootstrap_from_offline_package(source_root=package_root)

    with warehouse._connect_write() as connection:
        connection.execute("SELECT 1")
        assert warehouse._table_exists("daily_bars") is True


def test_market_warehouse_materialize_runtime_package_exports_database_rows(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source_package"
    runtime_package_root = tmp_path / "runtime_package"
    _build_sample_package(source_root)
    warehouse = MarketWarehouse(
        db_path=tmp_path / "warehouse" / "market.duckdb",
        package_root=runtime_package_root,
    )
    warehouse.bootstrap_from_offline_package(source_root=source_root)

    report = warehouse.materialize_runtime_package()

    assert report["status"] == "ok"
    assert report["symbols_total"] == 1
    assert report["daily_written"] == 1
    assert warehouse.has_materialized_package() is True
    exported = load_package_daily_bars(source_root=runtime_package_root, symbol="600000")
    assert len(exported) == 3
