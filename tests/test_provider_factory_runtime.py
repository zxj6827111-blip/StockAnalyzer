from __future__ import annotations

from pathlib import Path

import pytest

from stock_analyzer.config import DataSourceConfig, MarketDepthConfig
from stock_analyzer.data.akshare_provider import AkshareProvider
from stock_analyzer.data.efinance_provider import EfinanceProvider
from stock_analyzer.data.hybrid_runtime_provider import HybridRuntimeProvider
from stock_analyzer.data.market_depth import CachedMarketDepthProvider
from stock_analyzer.data.market_warehouse import MarketWarehouse
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.data.provider_factory import (
    build_market_depth_provider,
    build_realtime_runtime_provider,
    build_runtime_provider,
)
from stock_analyzer.data.resilient_provider import ResilientProvider
from stock_analyzer.data.tdx_offline_provider import TdxOfflineProvider


def test_build_runtime_provider_uses_efinance_as_online_backup_for_akshare() -> None:
    config = DataSourceConfig(primary="akshare")
    provider = build_runtime_provider(config)
    assert isinstance(provider, ResilientProvider)
    assert isinstance(provider.backup, ResilientProvider)
    online_backup = provider.backup
    assert isinstance(online_backup.primary, EfinanceProvider)
    assert isinstance(online_backup.backup, SyntheticProvider)


def test_build_runtime_provider_uses_akshare_as_online_backup_for_efinance() -> None:
    config = DataSourceConfig(primary="efinance")
    provider = build_runtime_provider(config)
    assert isinstance(provider, ResilientProvider)
    assert isinstance(provider.backup, ResilientProvider)
    online_backup = provider.backup
    assert isinstance(online_backup.primary, AkshareProvider)


def test_build_runtime_provider_container_mode_uses_fast_synthetic_failover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STOCK_ANALYZER_CONTAINERIZED", "1")
    config = DataSourceConfig(primary="efinance")

    provider = build_runtime_provider(config)

    assert isinstance(provider, ResilientProvider)
    assert isinstance(provider.primary, EfinanceProvider)
    assert isinstance(provider.backup, SyntheticProvider)


def test_build_realtime_runtime_provider_wraps_tdx_offline_with_live_overlay(
    tmp_path: Path,
) -> None:
    (tmp_path / "bars").mkdir(parents=True, exist_ok=True)
    config = DataSourceConfig(primary="tdx_offline", local_data_root=str(tmp_path))

    provider = build_realtime_runtime_provider(config, timezone="Asia/Shanghai")

    assert isinstance(provider, ResilientProvider)
    assert isinstance(provider.primary, HybridRuntimeProvider)
    assert isinstance(provider.backup, SyntheticProvider)
    assert provider.primary.live_cache_ttl_sec == 15.0


def test_build_runtime_provider_supports_market_warehouse(tmp_path: Path) -> None:
    config = DataSourceConfig(
        primary="market_warehouse",
        local_data_root=str(tmp_path / "package"),
        warehouse_db_path=str(tmp_path / "warehouse" / "market.duckdb"),
    )

    provider = build_runtime_provider(config)

    assert isinstance(provider, ResilientProvider)
    assert isinstance(provider.primary, MarketWarehouse)
    assert isinstance(provider.backup, ResilientProvider)


def test_build_runtime_provider_prefers_materialized_market_warehouse_package(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    bars_root = package_root / "bars"
    bars_root.mkdir(parents=True, exist_ok=True)
    (bars_root / "600000.csv").write_text(
        "date,open,high,low,close,volume,turnover,float_market_cap\n"
        "2026-03-05,10,10.2,9.9,10.1,1000000,10100000,12000000000\n",
        encoding="utf-8",
    )
    config = DataSourceConfig(
        primary="market_warehouse",
        local_data_root=str(package_root),
        warehouse_db_path=str(tmp_path / "warehouse" / "market.duckdb"),
    )

    provider = build_runtime_provider(config)

    assert isinstance(provider, ResilientProvider)
    assert isinstance(provider.primary, TdxOfflineProvider)


def test_build_realtime_runtime_provider_wraps_market_warehouse_with_live_overlay(
    tmp_path: Path,
) -> None:
    config = DataSourceConfig(
        primary="market_warehouse",
        local_data_root=str(tmp_path / "package"),
        warehouse_db_path=str(tmp_path / "warehouse" / "market.duckdb"),
    )

    provider = build_realtime_runtime_provider(config, timezone="Asia/Shanghai")

    assert isinstance(provider, ResilientProvider)
    assert isinstance(provider.primary, HybridRuntimeProvider)


def test_build_market_depth_provider_uses_cached_chain() -> None:
    config = MarketDepthConfig(primary="easyquotation_sina", backup="mootdx")

    provider = build_market_depth_provider(config)

    assert isinstance(provider, CachedMarketDepthProvider)
