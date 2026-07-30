from __future__ import annotations

import zipfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pandas as pd
import pytest

from stock_analyzer.config import DataSourceConfig
from stock_analyzer.data.market_warehouse import MarketWarehouse
from stock_analyzer.data.provider import DataSourceError
from stock_analyzer.data.provider_factory import build_primary_provider
from stock_analyzer.data.vendor_zip_overlay import (
    VendorZipOverlayProvider,
    write_vendor_zip_daily_index,
)
from stock_analyzer.runtime.service import StockAnalyzerService
from stock_analyzer.runtime.services.market_sync_service import RuntimeMarketSyncService


def _write_zip(path: Path, entries: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content.encode("utf-8"))


def _build_daily_fixture(root: Path) -> Path:
    daily_csv = "\n".join(
        [
            "code,datetime,open,high,low,close,volume,amount,circ_mv",
            "600000.SH,2025-12-30,10,11,9,10.5,100,123.4,200000",
            "600000.SH,2025-12-31,10.5,11.5,10,11,200,234.5,210000",
        ]
    )
    _write_zip(root / "全A日K" / "2025.zip", {"2025/600000.SH.csv": daily_csv})
    duplicate_csv = daily_csv.replace("600000.SH", "000001.SZ")
    _write_zip(
        root / "全A日K" / "2025(1).zip",
        {"2025/000001.SZ.csv": duplicate_csv},
    )
    index_path = root / "index" / "daily_index.json"
    write_vendor_zip_daily_index(root=root, output_path=index_path)
    return index_path


def _provider(root: Path, index_path: Path) -> VendorZipOverlayProvider:
    return VendorZipOverlayProvider(
        data_root=str(root),
        index_path=str(index_path),
        delta_db_path=str(root / "delta" / "market_delta.duckdb"),
        delta_package_root=str(root / "delta" / "package"),
    )


def test_daily_index_prefers_canonical_archive_and_records_latest_date(tmp_path: Path) -> None:
    index_path = _build_daily_fixture(tmp_path)
    provider = _provider(tmp_path, index_path)

    assert provider.list_symbols() == ["600000"]
    assert provider.latest_daily_dates() == {"600000": date(2025, 12, 31)}
    status = provider.status()
    assert status["symbols_total"] == 1
    assert status["index_archives_total"] == 1
    assert status["delta_package_writes_enabled"] is False


def test_daily_overlay_scales_vendor_units_and_delta_wins_duplicate_date(
    tmp_path: Path,
) -> None:
    index_path = _build_daily_fixture(tmp_path)
    delta_db = tmp_path / "delta" / "market_delta.duckdb"
    delta_package = tmp_path / "delta" / "package"
    warehouse = MarketWarehouse(db_path=delta_db, package_root=delta_package)
    delta = pd.DataFrame(
        {
            "open": [11.0, 12.5],
            "high": [12.0, 13.5],
            "low": [10.5, 12.0],
            "close": [12.0, 13.0],
            "volume": [9999.0, 8888.0],
            "turnover": [999_900.0, 888_800.0],
            "float_market_cap": [2_200_000_000.0, 2_300_000_000.0],
            "price_series_mode": ["raw", "raw"],
            "adjustment_source": ["tushare_raw", "tushare_raw"],
        },
        index=pd.to_datetime(["2025-12-31", "2026-01-02"]),
    )
    warehouse.replace_daily_bars(symbol="600000", frame=delta)

    provider = VendorZipOverlayProvider(
        data_root=str(tmp_path),
        index_path=str(index_path),
        delta_db_path=str(delta_db),
        delta_package_root=str(delta_package),
    )
    bars = provider.fetch_daily_bars("600000", lookback_days=10)

    assert list(bars.index.strftime("%Y-%m-%d")) == [
        "2025-12-30",
        "2025-12-31",
        "2026-01-02",
    ]
    assert float(bars.loc["2025-12-30", "volume"]) == 10_000.0
    assert float(bars.loc["2025-12-30", "turnover"]) == 123_400.0
    assert float(bars.loc["2025-12-31", "close"]) == 12.0
    assert float(bars.loc["2025-12-31", "volume"]) == 9999.0
    assert provider.latest_daily_dates()["600000"] == date(2026, 1, 2)


def test_vendor_minute_zip_is_loaded_on_demand(tmp_path: Path) -> None:
    index_path = _build_daily_fixture(tmp_path)
    today = date.today()
    interval_dir = tmp_path / "沪深分钟数据" / "Stock_1min_2000-now"
    archive_name = f"{today:%Y-%m}_1min.zip"
    minute_csv = "\n".join(
        [
            "datetime,code,name,open,close,high,low,volume,amount,pct_chg,amplitude",
            f"{today.isoformat()} 09:30:00,sh600000,测试,10,10.1,10.2,9.9,10,10000,1,3",
            f"{today.isoformat()} 09:31:00,sh600000,测试,10.1,10.2,10.3,10,20,20400,1,3",
        ]
    )
    entry_name = f"{today:%Y-%m}_1min/{today:%Y%m%d}_1min/sh600000.csv"
    _write_zip(interval_dir / archive_name, {entry_name: minute_csv})

    summary = _provider(tmp_path, index_path).fetch_intraday_summary(
        "600000",
        "1m",
        lookback_days=5,
    )

    assert len(summary) == 1
    assert float(summary.iloc[-1]["minute_count"]) == 2.0


def test_provider_factory_builds_vendor_overlay(tmp_path: Path) -> None:
    index_path = _build_daily_fixture(tmp_path)
    config = DataSourceConfig(
        primary="vendor_zip_overlay",
        local_data_root=str(tmp_path),
        warehouse_db_path=str(tmp_path / "delta" / "market_delta.duckdb"),
        vendor_zip_index_path=str(index_path),
    )

    provider = build_primary_provider(config)

    assert isinstance(provider, VendorZipOverlayProvider)


def test_vendor_overlay_rejects_unverified_adjusted_price_mode(tmp_path: Path) -> None:
    index_path = _build_daily_fixture(tmp_path)
    with pytest.raises(DataSourceError, match="supports only raw"):
        VendorZipOverlayProvider(
            data_root=str(tmp_path),
            index_path=str(index_path),
            delta_db_path=str(tmp_path / "delta.duckdb"),
            price_series_mode="qfq",
        )


def test_runtime_universe_comes_from_provider_index(tmp_path: Path) -> None:
    index_path = _build_daily_fixture(tmp_path)
    service = object.__new__(StockAnalyzerService)
    object.__setattr__(service, "_provider", _provider(tmp_path, index_path))
    object.__setattr__(service, "_realtime_provider", None)

    assert service._load_symbol_universe_from_provider() == ["600000"]


def test_market_sync_uses_vendor_latest_date_instead_of_bootstrap_window(
    tmp_path: Path,
) -> None:
    index_path = _build_daily_fixture(tmp_path)
    provider = _provider(tmp_path, index_path)
    config = SimpleNamespace(
        market_warehouse=SimpleNamespace(
            daily_lookback_days=120,
            online_bootstrap_lookback_days=750,
            daily_incremental_enabled=True,
            daily_incremental_cushion_days=5,
        )
    )
    service = SimpleNamespace(
        _provider=provider,
        _realtime_provider=None,
        _config=config,
    )
    sync = RuntimeMarketSyncService(service)
    warehouse = MarketWarehouse(
        db_path=tmp_path / "empty_delta.duckdb",
        package_root=tmp_path / "empty_package",
    )

    latest = sync._resolve_market_warehouse_latest_daily_dates(
        warehouse=warehouse,
        symbols=["600000"],
    )
    lookback_days, mode = sync._resolve_market_warehouse_daily_lookback_days(
        latest_date=latest["600000"],
        target_end_date=date(2026, 1, 5),
        force=False,
    )

    assert latest["600000"] == date(2025, 12, 31)
    assert mode == "incremental"
    assert lookback_days == 10
    assert lookback_days < 750


def test_market_sync_builds_tushare_in_raw_mode_for_vendor_overlay() -> None:
    config = SimpleNamespace(
        data_source=DataSourceConfig(primary="vendor_zip_overlay"),
        market_warehouse=SimpleNamespace(tushare_token=""),
        evolution=SimpleNamespace(
            execution_spec=SimpleNamespace(price_series_mode="qfq")
        ),
    )
    service = SimpleNamespace(_config=config)
    sync = RuntimeMarketSyncService(service)

    provider = sync._build_market_warehouse_online_single_provider(
        provider_name="tushare",
        request_interval=0.0,
        socket_timeout_sec=1.0,
        max_attempts=1,
    )

    assert cast(Any, provider)._price_series_mode == "raw"


def test_market_sync_warehouse_disables_package_writes_for_vendor_overlay(
    tmp_path: Path,
) -> None:
    config = SimpleNamespace(
        data_source=DataSourceConfig(primary="vendor_zip_overlay"),
    )
    service = SimpleNamespace(
        _config=config,
        _resolve_market_warehouse_db_path=lambda: tmp_path / "delta.duckdb",
        _resolve_market_warehouse_package_root=lambda: tmp_path / "package",
    )

    warehouse = RuntimeMarketSyncService(service)._market_warehouse()

    assert warehouse.package_writes_enabled is False


def test_vendor_delta_sync_does_not_export_package_bars(tmp_path: Path) -> None:
    warehouse = MarketWarehouse(
        db_path=tmp_path / "delta.duckdb",
        package_root=tmp_path / "package",
        package_writes_enabled=False,
    )
    fresh = pd.DataFrame(
        {
            "open": [11.0],
            "high": [12.0],
            "low": [10.5],
            "close": [11.8],
            "volume": [1000.0],
            "turnover": [11_800.0],
            "float_market_cap": [2_000_000_000.0],
            "price_series_mode": ["raw"],
            "adjustment_source": ["tushare_raw"],
        },
        index=pd.to_datetime(["2026-01-02"]),
    )

    class _OnlineProvider:
        def fetch_daily_bars(
            self,
            symbol: str,
            lookback_days: int = 120,
            *,
            end_date: date | None = None,
        ) -> pd.DataFrame:
            _ = symbol, lookback_days, end_date
            return fresh.copy()

    service = SimpleNamespace(
        _resolve_market_warehouse_daily_lookback_days=lambda **kwargs: (10, "incremental"),
        _extract_price_series_meta_from_frame=lambda frame: {
            "price_series_mode": "raw",
            "adjustment_source": "tushare_raw",
            "adjustment_anchor_date": "",
            "adjustment_anchor_factor": None,
        },
        _resolve_market_warehouse_price_series_action=lambda **kwargs: {
            "action": "incremental",
            "reason": "anchor_compatible",
        },
        _carry_forward_market_warehouse_financial_fields=lambda **kwargs: kwargs[
            "fresh_daily"
        ],
    )
    sync = RuntimeMarketSyncService(service)

    report = sync._sync_market_warehouse_daily_symbol(
        warehouse=warehouse,
        online_provider=cast(Any, _OnlineProvider()),
        symbol="600000",
        force=False,
        target_end_date=date(2026, 1, 2),
        latest_daily=date(2025, 12, 31),
        hard_timeout_sec=2.0,
    )

    assert report["status"] == "ok"
    assert len(warehouse.fetch_all_daily_bars(symbol="600000")) == 1
    assert not (warehouse.package_root / "bars").exists()
    assert not (warehouse.package_root / "manifest.json").exists()
    assert warehouse.refresh_package_manifests()["reason"] == "package_writes_disabled"
