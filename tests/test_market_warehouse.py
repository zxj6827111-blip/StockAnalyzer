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


def test_market_warehouse_schema_migration_adds_financial_provenance_columns(
    tmp_path: Path,
) -> None:
    warehouse = MarketWarehouse(
        db_path=tmp_path / "warehouse" / "market.duckdb",
        package_root=tmp_path / "package",
    )
    warehouse.ensure_schema()
    with warehouse._connect_write() as connection:
        connection.execute("ALTER TABLE daily_bars DROP COLUMN financial_as_of")
        connection.execute("ALTER TABLE daily_bars DROP COLUMN financial_trust_level")
        connection.execute("ALTER TABLE daily_bars DROP COLUMN financial_completeness")

    warehouse.ensure_schema()
    warehouse.ensure_schema()

    with warehouse._connect_readonly() as connection:
        rows = connection.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'daily_bars'
            """
        ).fetchall()
    columns = {str(row[0]) for row in rows}
    assert {
        "financial_as_of",
        "financial_trust_level",
        "financial_completeness",
    } <= columns


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


def test_market_warehouse_preserves_nan_roundtrip(tmp_path: Path) -> None:
    warehouse = MarketWarehouse(
        db_path=tmp_path / "warehouse" / "market.duckdb",
        package_root=tmp_path / "package",
    )
    frame = pd.DataFrame(
        {
            "open": [10.0, 10.1],
            "high": [10.2, 10.3],
            "low": [9.9, 10.0],
            "close": [10.1, 10.2],
            "volume": [1_000_000.0, 1_100_000.0],
            "turnover": [10_100_000.0, 11_220_000.0],
            "float_market_cap": [12_000_000_000.0, 12_000_000_000.0],
            "holder_count": [float("nan"), float("nan")],
            "financing_balance": [float("nan"), float("nan")],
            "northbound_net": [float("nan"), float("nan")],
            "block_trade_net": [float("nan"), float("nan")],
            "background_data_complete": [False, False],
            "background_missing_fields": [
                "holder_count,financing_balance",
                "holder_count,financing_balance",
            ],
            "background_data_source": ["tushare_pro_qfq", "tushare_pro_qfq"],
            "price_series_mode": ["qfq", "qfq"],
            "adjustment_source": ["tushare_adj_factor", "tushare_adj_factor"],
            "adjustment_anchor_date": ["2026-07-14", "2026-07-14"],
            "adjustment_anchor_factor": [16.0, 16.0],
            "financial_source": ["tushare_pending", "tushare_pending"],
            "financial_trust_level": ["missing", "missing"],
            "roe": [float("nan"), float("nan")],
            "debt_ratio": [float("nan"), float("nan")],
        },
        index=pd.to_datetime(["2026-07-11", "2026-07-14"]),
    )
    frame.index.name = "date"
    warehouse.replace_daily_bars(symbol="600000", frame=frame)
    warehouse.update_price_series_contract(
        symbol="600000",
        price_series_mode="qfq",
        adjustment_source="tushare_adj_factor",
        adjustment_anchor_date="2026-07-14",
        adjustment_anchor_factor=16.0,
    )
    warehouse.materialize_runtime_package(symbols=["600000"])

    loaded = warehouse.fetch_all_daily_bars(symbol="600000")
    assert pd.isna(loaded["holder_count"].iloc[-1])
    assert pd.isna(loaded["financing_balance"].iloc[-1])
    assert pd.isna(loaded["northbound_net"].iloc[-1])
    assert bool(loaded["background_data_complete"].iloc[-1]) is False

    package_bars = load_package_daily_bars(source_root=tmp_path / "package", symbol="600000")
    assert pd.isna(package_bars["holder_count"].iloc[-1])
    assert package_bars["price_series_mode"].iloc[-1] == "qfq"
    assert package_bars["adjustment_source"].iloc[-1] == "tushare_adj_factor"
    manifest = (tmp_path / "package" / "manifest.json").read_text(encoding="utf-8")
    assert "price_series_mode" in manifest
    assert "tushare_adj_factor" in manifest or "adjustment_source" in manifest


def test_financial_snapshot_incremental_upsert_preserves_prior_history(
    tmp_path: Path,
) -> None:
    warehouse = MarketWarehouse(
        db_path=tmp_path / "warehouse" / "market.duckdb",
        package_root=tmp_path / "package",
    )

    def snapshots(
        rows: list[tuple[str, str, float, float]],
    ) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "symbol": ["600000"] * len(rows),
                "end_date": pd.to_datetime([row[0] for row in rows]),
                "ann_date": pd.to_datetime([row[1] for row in rows]),
                "roe": [row[2] for row in rows],
                "debt_ratio": [row[3] for row in rows],
                "update_flag": [0] * len(rows),
                "financial_report_date": [row[0] for row in rows],
                "financial_as_of": [row[1] for row in rows],
                "financial_source": ["tushare_fina_indicator"] * len(rows),
                "financial_trust_level": ["reported"] * len(rows),
                "financial_missing_fields": [""] * len(rows),
                "financial_data_complete": [True] * len(rows),
                "financial_completeness": [1.0] * len(rows),
                "coverage_complete": [True] * len(rows),
                "as_of": [row[1] for row in rows],
                "source": ["tushare_fina_indicator"] * len(rows),
            }
        )

    # First sync has an older announcement outside the next incremental window.
    warehouse.upsert_financial_snapshots(
        symbol="600000",
        frame=snapshots([("2024-12-31", "2025-03-20", 0.10, 0.40)]),
    )
    # The next sync overlaps the 2025 announcement and brings a newer one.
    warehouse.upsert_financial_snapshots(
        symbol="600000",
        frame=snapshots(
            [
                ("2024-12-31", "2025-03-20", 0.11, 0.39),
                ("2025-03-31", "2025-04-25", 0.12, 0.38),
            ]
        ),
    )

    stored = warehouse.fetch_financial_snapshots(symbol="600000")
    assert len(stored) == 2
    assert float(
        stored.loc[stored["ann_date"] == pd.Timestamp("2025-03-20"), "roe"].iloc[0]
    ) == pytest.approx(0.11)
    assert float(
        stored.loc[stored["ann_date"] == pd.Timestamp("2025-04-25"), "roe"].iloc[0]
    ) == pytest.approx(0.12)


def test_financial_snapshot_batch_fetch_returns_requested_symbols_once(
    tmp_path: Path,
) -> None:
    warehouse = MarketWarehouse(
        db_path=tmp_path / "warehouse" / "market.duckdb",
        package_root=tmp_path / "package",
    )

    def snapshots(
        symbol: str,
        rows: list[tuple[str, str, float]],
    ) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "symbol": [symbol] * len(rows),
                "end_date": pd.to_datetime([row[0] for row in rows]),
                "ann_date": pd.to_datetime([row[1] for row in rows]),
                "roe": [row[2] for row in rows],
                "debt_ratio": [0.4] * len(rows),
                "update_flag": [0] * len(rows),
                "financial_report_date": [row[0] for row in rows],
                "financial_as_of": [row[1] for row in rows],
                "financial_source": ["tushare_fina_indicator"] * len(rows),
                "financial_trust_level": ["reported"] * len(rows),
                "financial_missing_fields": [""] * len(rows),
                "financial_data_complete": [True] * len(rows),
                "financial_completeness": [1.0] * len(rows),
                "coverage_complete": [True] * len(rows),
                "as_of": [row[1] for row in rows],
                "source": ["tushare_fina_indicator"] * len(rows),
            }
        )

    warehouse.upsert_financial_snapshots(
        symbol="600000",
        frame=snapshots("600000", [("2024-12-31", "2025-03-20", 0.10)]),
    )
    warehouse.upsert_financial_snapshots(
        symbol="000001",
        frame=snapshots(
            "000001",
            [("2024-12-31", "2025-03-21", 0.12), ("2025-03-31", "2025-04-25", 0.13)],
        ),
    )
    warehouse.upsert_financial_snapshots(
        symbol="300750",
        frame=snapshots("300750", [("2024-12-31", "2025-03-22", 0.14)]),
    )

    batch = warehouse.fetch_financial_snapshots_batch(symbols=["600000", "000001"])
    assert batch["symbol"].tolist() == ["600000", "000001", "000001"]
    assert len(batch) == 3

    as_of = warehouse.fetch_financial_snapshots_batch(
        symbols=["600000", "000001", "300750"],
        as_of=pd.Timestamp("2025-03-22"),
    )
    assert len(as_of) == 3
    assert set(as_of["symbol"]) == {"600000", "000001", "300750"}

    before = warehouse.fetch_financial_snapshots_batch(
        symbols=["600000", "000001", "300750"],
        as_of=pd.Timestamp("2025-03-20"),
    )
    assert len(before) == 1
    assert before.iloc[0]["symbol"] == "600000"

    empty = warehouse.fetch_financial_snapshots_batch(
        symbols=["999999"],
    )
    assert empty.empty


def test_market_warehouse_price_series_contract_unknown_by_default(tmp_path: Path) -> None:
    warehouse = MarketWarehouse(
        db_path=tmp_path / "warehouse" / "market.duckdb",
        package_root=tmp_path / "package",
    )
    contract = warehouse.price_series_contract(symbol="600000")
    assert contract["known"] is False
    shadow = warehouse.clone_to_shadow_paths(
        shadow_db_path=tmp_path / "shadow" / "market.duckdb",
        shadow_package_root=tmp_path / "shadow_package",
    )
    assert shadow.db_path != warehouse.db_path
    assert shadow.package_root != warehouse.package_root


def test_market_warehouse_price_series_contract_is_isolated_per_symbol(
    tmp_path: Path,
) -> None:
    warehouse = MarketWarehouse(
        db_path=tmp_path / "warehouse" / "market.duckdb",
        package_root=tmp_path / "package",
    )
    for symbol in ("600000", "000001"):
        frame = pd.DataFrame(
            {
                "open": [10.0],
                "high": [10.2],
                "low": [9.9],
                "close": [10.1],
                "volume": [1_000_000.0],
                "turnover": [10_100_000.0],
                "float_market_cap": [12_000_000_000.0],
            },
            index=pd.to_datetime(["2026-07-14"]),
        )
        frame.index.name = "date"
        warehouse.replace_daily_bars(symbol=symbol, frame=frame)

    warehouse.update_price_series_contract(
        symbol="600000",
        price_series_mode="qfq",
        adjustment_source="tushare_adj_factor",
        adjustment_anchor_date="2026-07-14",
        adjustment_anchor_factor=10.0,
    )
    warehouse.update_price_series_contract(
        symbol="000001",
        price_series_mode="qfq",
        adjustment_source="tushare_adj_factor",
        adjustment_anchor_date="2026-07-14",
        adjustment_anchor_factor=20.0,
    )

    assert warehouse.price_series_contract(symbol="600000")["adjustment_anchor_factor"] == 10.0
    assert warehouse.price_series_contract(symbol="000001")["adjustment_anchor_factor"] == 20.0
    assert warehouse.price_series_contract()["mixed"] is True
    assert warehouse.warehouse_meta_path("600000").exists()
    assert warehouse.warehouse_meta_path("000001").exists()
    assert warehouse.warehouse_meta_path().exists() is False


def test_market_warehouse_reanchor_preserves_history_and_traded_units(
    tmp_path: Path,
) -> None:
    warehouse = MarketWarehouse(
        db_path=tmp_path / "warehouse" / "market.duckdb",
        package_root=tmp_path / "package",
    )
    existing = pd.DataFrame(
        {
            "open": [10.0, 11.0, 12.0],
            "high": [10.2, 11.2, 12.2],
            "low": [9.8, 10.8, 11.8],
            "close": [10.1, 11.1, 12.1],
            "volume": [100.0, 200.0, 300.0],
            "turnover": [1_010.0, 2_220.0, 3_630.0],
            "float_market_cap": [12_000_000_000.0] * 3,
            "price_series_mode": ["qfq", "qfq", "qfq"],
            "adjustment_source": ["tushare_adj_factor"] * 3,
            "adjustment_anchor_date": ["2026-07-13"] * 3,
            "adjustment_anchor_factor": [10.0, 10.0, 10.0],
        },
        index=pd.to_datetime(["2026-07-11", "2026-07-12", "2026-07-13"]),
    )
    existing.index.name = "date"
    warehouse.replace_daily_bars(symbol="600000", frame=existing)

    fresh = pd.DataFrame(
        {
            "open": [6.0, 6.2],
            "high": [6.1, 6.3],
            "low": [5.9, 6.1],
            "close": [6.05, 6.25],
            "volume": [300.0, 400.0],
            "turnover": [3_630.0, 5_000.0],
            "float_market_cap": [12_000_000_000.0] * 2,
            "price_series_mode": ["qfq", "qfq"],
            "adjustment_source": ["tushare_adj_factor", "tushare_adj_factor"],
            "adjustment_anchor_date": ["2026-07-14", "2026-07-14"],
            "adjustment_anchor_factor": [20.0, 20.0],
        },
        index=pd.to_datetime(["2026-07-13", "2026-07-14"]),
    )
    fresh.index.name = "date"
    merged = warehouse.handle_reanchor_symbol(
        symbol="600000",
        fresh_daily=fresh,
        old_anchor_factor=10.0,
        new_anchor_factor=20.0,
        fresh_meta={
            "price_series_mode": "qfq",
            "adjustment_source": "tushare_adj_factor",
            "adjustment_anchor_date": "2026-07-14",
            "adjustment_anchor_factor": 20.0,
        },
    )

    assert len(merged) == 4
    assert float(merged.loc[pd.Timestamp("2026-07-11"), "close"]) == pytest.approx(5.05)
    assert float(merged.loc[pd.Timestamp("2026-07-11"), "volume"]) == 100.0
    assert float(merged.loc[pd.Timestamp("2026-07-11"), "turnover"]) == 1_010.0
    assert float(merged.loc[pd.Timestamp("2026-07-13"), "close"]) == 6.05
    assert set(pd.to_numeric(merged["adjustment_anchor_factor"]).dropna()) == {20.0}


def _make_quality_daily_frame(symbol: str, days: int) -> pd.DataFrame:
    dates = pd.bdate_range(end="2026-07-31", periods=days)
    closes = [10.0 + i * 0.01 for i in range(days)]
    frame = pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.1 for c in closes],
            "low": [c - 0.1 for c in closes],
            "close": closes,
            "volume": [1_000_000.0] * days,
            "turnover": [10_000_000.0] * days,
            "float_market_cap": [5_000_000_000.0] * days,
            "roe": [0.12] * days,
            "debt_ratio": [0.35] * days,
            "financial_data_complete": [True] * days,
            "financial_completeness": [0.9] * days,
            "background_data_complete": [True] * days,
        },
        index=dates,
    )
    frame.index.name = "date"
    _ = symbol
    return frame


def test_fetch_universe_quality_metrics_batches_all_symbols_in_one_query(
    tmp_path: Path,
) -> None:
    warehouse = MarketWarehouse(
        db_path=tmp_path / "warehouse" / "market.duckdb",
        package_root=tmp_path / "package",
    )
    symbols = ["600000", "000001", "300001", "688001", "830001"]
    for symbol in symbols:
        warehouse.replace_daily_bars(symbol=symbol, frame=_make_quality_daily_frame(symbol, 80))

    frame = warehouse.fetch_universe_quality_metrics(symbols=symbols, lookback_days=30)
    assert not frame.empty
    # Single batch call returns all requested symbols.
    assert set(frame["symbol"].unique()) == set(symbols)
    # Each symbol capped at lookback_days rows.
    counts = frame.groupby("symbol").size().to_dict()
    for symbol in symbols:
        assert counts[symbol] == 30, f"{symbol} got {counts[symbol]} rows"
    # Required quality-scoring columns are present.
    for col in (
        "symbol",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "turnover",
        "float_market_cap",
        "suspended",
        "is_st",
        "is_delisting_risk",
        "roe",
        "debt_ratio",
        "financial_data_complete",
        "financial_completeness",
        "background_data_complete",
        "holder_count",
        "northbound_net",
        "dragon_tiger_flag",
    ):
        assert col in frame.columns, f"missing column {col}"
    # Rows sorted by symbol then date.
    assert frame.equals(frame.sort_values(["symbol", "date"]).reset_index(drop=True))


def test_fetch_universe_quality_metrics_empty_when_table_missing(
    tmp_path: Path,
) -> None:
    warehouse = MarketWarehouse(
        db_path=tmp_path / "warehouse" / "market.duckdb",
        package_root=tmp_path / "package",
    )
    # No bootstrap -> daily_bars table does not exist.
    frame = warehouse.fetch_universe_quality_metrics(symbols=["600000"], lookback_days=30)
    assert frame.empty


def test_fetch_universe_quality_metrics_filters_to_requested_symbols(
    tmp_path: Path,
) -> None:
    warehouse = MarketWarehouse(
        db_path=tmp_path / "warehouse" / "market.duckdb",
        package_root=tmp_path / "package",
    )
    for symbol in ["600000", "600001", "600002"]:
        warehouse.replace_daily_bars(symbol=symbol, frame=_make_quality_daily_frame(symbol, 50))
    frame = warehouse.fetch_universe_quality_metrics(symbols=["600000", "600002"], lookback_days=20)
    assert set(frame["symbol"].unique()) == {"600000", "600002"}
    assert "600001" not in set(frame["symbol"].unique())
