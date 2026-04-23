from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from stock_analyzer.config import DataSourceConfig
from stock_analyzer.data.efinance_provider import EfinanceProvider
from stock_analyzer.data.market_warehouse import MarketWarehouse
from stock_analyzer.data.provider import DataSourceError
from stock_analyzer.data.provider_factory import (
    build_online_backup_provider,
    build_primary_provider,
)
from stock_analyzer.data.tdx_offline_provider import TdxOfflineProvider


def test_tdx_offline_provider_loads_csv_and_fills_defaults(tmp_path: Path) -> None:
    bars_dir = tmp_path / "bars"
    bars_dir.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(
        {
            "date": ["2026-02-27", "2026-02-28", "2026-03-02"],
            "open": [10.0, 10.2, 10.3],
            "high": [10.3, 10.4, 10.5],
            "low": [9.9, 10.1, 10.2],
            "close": [10.2, 10.3, 10.4],
            "volume": [1_000_000, 1_100_000, 1_200_000],
            "turnover": [10_200_000.0, 11_300_000.0, 12_400_000.0],
            "float_market_cap": [12_000_000_000.0, 12_000_000_000.0, 12_000_000_000.0],
        }
    )
    frame.to_csv(bars_dir / "600000.csv", index=False)

    provider = TdxOfflineProvider(data_root=str(tmp_path))
    bars = provider.fetch_daily_bars(symbol="600000", lookback_days=2)
    assert len(bars) == 2
    assert bars.index.name == "date"
    assert bool(bars["financial_data_complete"].iloc[-1]) is True
    assert bars["board"].iloc[-1] == "main"
    assert "northbound_net" in bars.columns


def test_tdx_offline_provider_forward_fills_symbol_name(tmp_path: Path) -> None:
    bars_dir = tmp_path / "bars"
    bars_dir.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(
        {
            "date": ["2026-01-16", "2026-01-17", "2026-01-20"],
            "open": [18.68, 18.82, 18.85],
            "high": [18.68, 18.88, 18.87],
            "low": [18.68, 18.82, 18.84],
            "close": [18.68, 18.86, 18.85],
            "volume": [299632.0, 1184685.0, 706402.0],
            "turnover": [559712576.0, 2234315910.0, 1331567770.0],
            "float_market_cap": [12_000_000_000.0, 12_000_000_000.0, 12_000_000_000.0],
            "name": ["测试股份", "", "nan"],
        }
    )
    frame.to_csv(bars_dir / "603056.csv", index=False)

    provider = TdxOfflineProvider(data_root=str(tmp_path))
    bars = provider.fetch_daily_bars(symbol="603056", lookback_days=3)

    assert bars["name"].tolist() == ["测试股份", "测试股份", "测试股份"]


def test_tdx_offline_provider_missing_symbol_raises(tmp_path: Path) -> None:
    (tmp_path / "bars").mkdir(parents=True, exist_ok=True)
    provider = TdxOfflineProvider(data_root=str(tmp_path))
    with pytest.raises(DataSourceError):
        provider.fetch_daily_bars(symbol="600000", lookback_days=30)


def test_build_primary_provider_uses_tdx_offline(tmp_path: Path) -> None:
    (tmp_path / "bars").mkdir(parents=True, exist_ok=True)
    config = DataSourceConfig(primary="tdx_offline", local_data_root=str(tmp_path))
    provider = build_primary_provider(config)
    assert isinstance(provider, TdxOfflineProvider)


def test_build_primary_provider_supports_efinance() -> None:
    config = DataSourceConfig(primary="efinance", local_data_root="")
    provider = build_primary_provider(config)
    assert isinstance(provider, EfinanceProvider)


def test_build_online_backup_provider_uses_efinance_for_akshare_primary() -> None:
    config = DataSourceConfig(primary="akshare", local_data_root="")
    provider = build_online_backup_provider(config)
    assert isinstance(provider, EfinanceProvider)


def test_build_primary_provider_requires_local_data_root() -> None:
    config = DataSourceConfig(primary="tdx_offline", local_data_root="")
    with pytest.raises(DataSourceError):
        build_primary_provider(config)


def test_build_primary_provider_supports_market_warehouse(tmp_path: Path) -> None:
    config = DataSourceConfig(
        primary="market_warehouse",
        local_data_root=str(tmp_path / "package"),
        warehouse_db_path=str(tmp_path / "warehouse" / "market.duckdb"),
    )
    provider = build_primary_provider(config)
    assert isinstance(provider, MarketWarehouse)


def test_build_primary_provider_requires_warehouse_db_path() -> None:
    config = DataSourceConfig(primary="market_warehouse", local_data_root="")
    with pytest.raises(DataSourceError):
        build_primary_provider(config)
