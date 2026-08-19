"""Factory helpers for selecting primary market data providers."""

from __future__ import annotations

import os
from pathlib import Path

from stock_analyzer.config import DataSourceConfig, MarketDepthConfig
from stock_analyzer.data.akshare_provider import AkshareProvider
from stock_analyzer.data.efinance_provider import EfinanceProvider
from stock_analyzer.data.hybrid_runtime_provider import HybridRuntimeProvider
from stock_analyzer.data.market_depth import (
    CachedMarketDepthProvider,
    EasyQuotationMarketDepthProvider,
    EmptyMarketDepthProvider,
    FallbackMarketDepthProvider,
    MarketDepthProvider,
    MootdxMarketDepthProvider,
)
from stock_analyzer.data.market_warehouse import MarketWarehouse
from stock_analyzer.data.provider import DataSourceError, MarketDataProvider, SyntheticProvider
from stock_analyzer.data.resilient_provider import ResilientProvider
from stock_analyzer.data.tdx_offline_provider import TdxOfflineProvider
from stock_analyzer.data.tushare_provider import TushareProvider
from stock_analyzer.data.vendor_zip_overlay import VendorZipOverlayProvider

_VENDOR_ZIP_OVERLAY_ALIASES = {
    "vendor_zip_overlay",
    "vendor_overlay",
    "local_vendor_zip",
}


def _runtime_provider_tuning() -> tuple[float, int, bool]:
    containerized = os.environ.get("STOCK_ANALYZER_CONTAINERIZED", "").strip() == "1"
    socket_timeout_sec = 2.5 if containerized else 15.0
    max_attempts = 1 if containerized else 2
    fast_failover = containerized
    return socket_timeout_sec, max_attempts, fast_failover


def build_primary_provider(config: DataSourceConfig) -> MarketDataProvider:
    """Build provider instance by `data_source.primary`."""
    primary = config.primary.strip().lower()
    socket_timeout_sec, max_attempts, _ = _runtime_provider_tuning()
    if primary in {"synthetic", "synthetic_test"}:
        return SyntheticProvider()
    if primary in {"tushare", "ts", "tushare_pro"}:
        return TushareProvider(
            retry_delay_sec=config.request_interval_sec,
            min_request_interval_sec=config.request_interval_sec,
            max_attempts=max_attempts,
            socket_timeout_sec=socket_timeout_sec,
            price_series_mode="qfq",
        )
    if primary in {"akshare", "ak"}:
        return AkshareProvider(
            retry_delay_sec=config.request_interval_sec,
            max_attempts=max_attempts,
            socket_timeout_sec=socket_timeout_sec,
        )
    if primary in {"efinance", "ef"}:
        return EfinanceProvider(
            retry_delay_sec=config.request_interval_sec,
            max_attempts=max_attempts,
            socket_timeout_sec=socket_timeout_sec,
        )
    if primary in {"tdx_offline", "tdx_local", "local_csv"}:
        root = config.local_data_root.strip()
        if not root:
            raise DataSourceError(
                "data_source.local_data_root is required when data_source.primary=tdx_offline"
            )
        return TdxOfflineProvider(data_root=root)
    if primary in _VENDOR_ZIP_OVERLAY_ALIASES:
        root = config.local_data_root.strip()
        if not root:
            raise DataSourceError(
                "data_source.local_data_root is required "
                "when data_source.primary=vendor_zip_overlay"
            )
        index_path = config.vendor_zip_index_path.strip()
        if not index_path:
            raise DataSourceError(
                "data_source.vendor_zip_index_path is required "
                "when data_source.primary=vendor_zip_overlay"
            )
        db_path = config.warehouse_db_path.strip()
        if not db_path:
            raise DataSourceError(
                "data_source.warehouse_db_path is required "
                "when data_source.primary=vendor_zip_overlay"
            )
        return VendorZipOverlayProvider(
            data_root=root,
            index_path=index_path,
            delta_db_path=db_path,
            daily_dir_name=config.vendor_zip_daily_dir,
            price_series_mode=config.vendor_zip_price_series_mode,
            daily_volume_multiplier=config.vendor_zip_daily_volume_multiplier,
            daily_turnover_multiplier=config.vendor_zip_daily_turnover_multiplier,
            minute_volume_multiplier=config.vendor_zip_minute_volume_multiplier,
            minute_amount_multiplier=config.vendor_zip_minute_amount_multiplier,
            intraday_enabled=config.vendor_zip_intraday_enabled,
            intraday_runtime_mode=config.intraday_runtime_mode,
            intraday_summary_path=config.intraday_summary_path,
            intraday_zip_fallback_enabled=config.intraday_zip_fallback_enabled,
            intraday_max_staleness_trading_days=(
                config.intraday_max_staleness_trading_days
            ),
            intraday_query_timeout_sec=config.intraday_query_timeout_sec,
            intraday_max_concurrency=config.intraday_max_concurrency,
            intraday_cache_ttl_sec=config.intraday_cache_ttl_sec,
            memory_cache_symbols=config.vendor_zip_memory_cache_symbols,
            delta_max_staleness_days=config.vendor_zip_delta_max_staleness_days,
        )
    if primary in {
        "market_warehouse",
        "warehouse",
        "warehouse_offline",
        *_VENDOR_ZIP_OVERLAY_ALIASES,
    }:
        db_path = config.warehouse_db_path.strip()
        if not db_path:
            raise DataSourceError(
                "data_source.warehouse_db_path is required "
                "when data_source.primary=market_warehouse"
            )
        package_root = _resolve_market_warehouse_package_root(config)
        if _has_materialized_market_warehouse_package(package_root):
            return TdxOfflineProvider(data_root=package_root)
        return MarketWarehouse(
            db_path=db_path,
            package_root=package_root,
        )
    raise DataSourceError(f"unsupported data_source.primary: {config.primary}")


def build_online_backup_provider(config: DataSourceConfig) -> MarketDataProvider | None:
    """Build online backup provider by primary source."""
    primary = config.primary.strip().lower()
    socket_timeout_sec, max_attempts, fast_failover = _runtime_provider_tuning()
    if fast_failover:
        return None
    if primary in {"synthetic", "synthetic_test"}:
        return None
    if primary in {"akshare", "ak"}:
        return EfinanceProvider(
            retry_delay_sec=config.request_interval_sec,
            max_attempts=max_attempts,
            socket_timeout_sec=socket_timeout_sec,
        )
    if primary in {"efinance", "ef"}:
        return AkshareProvider(
            retry_delay_sec=config.request_interval_sec,
            max_attempts=max_attempts,
            socket_timeout_sec=socket_timeout_sec,
        )
    if primary in {
        "market_warehouse",
        "warehouse",
        "warehouse_offline",
        *_VENDOR_ZIP_OVERLAY_ALIASES,
    }:
        return ResilientProvider(
            primary=EfinanceProvider(
                retry_delay_sec=config.request_interval_sec,
                max_attempts=max_attempts,
                socket_timeout_sec=socket_timeout_sec,
            ),
            backup=AkshareProvider(
                retry_delay_sec=config.request_interval_sec,
                max_attempts=max_attempts,
                socket_timeout_sec=socket_timeout_sec,
            ),
            config=config.model_copy(update={"primary": "efinance"}),
        )
    return None


def build_runtime_provider(
    config: DataSourceConfig,
    *,
    synthetic_seed: int = 2026,
) -> MarketDataProvider:
    """Build runtime provider chain: primary -> online backup -> synthetic."""
    if config.primary.strip().lower() in {"synthetic", "synthetic_test"}:
        return SyntheticProvider(seed_offset=synthetic_seed)
    primary_provider = build_primary_provider(config)
    online_backup = build_online_backup_provider(config)
    if not config.synthetic_fallback_allowed:
        # Production gate: never degrade to synthetic when primary is real.
        return ResilientProvider(
            primary=primary_provider,
            backup=online_backup,
            config=config,
        )
    synthetic_backup = SyntheticProvider(seed_offset=synthetic_seed)
    fallback: MarketDataProvider = synthetic_backup
    if online_backup is not None:
        fallback = ResilientProvider(
            primary=online_backup,
            backup=synthetic_backup,
            config=config,
        )
    return ResilientProvider(
        primary=primary_provider,
        backup=fallback,
        config=config,
    )


def build_realtime_runtime_provider(
    config: DataSourceConfig,
    *,
    synthetic_seed: int = 2026,
    timezone: str = "Asia/Shanghai",
) -> MarketDataProvider:
    """Build daytime runtime provider with live intraday overlay when enabled."""
    if config.primary.strip().lower() in {"synthetic", "synthetic_test"}:
        return SyntheticProvider(seed_offset=synthetic_seed)
    primary_provider = build_primary_provider(config)
    primary_key = config.primary.strip().lower()
    live_provider_key = config.runtime_live_provider.strip().lower()
    if (
        config.runtime_live_enabled
        and primary_key in {
            "tdx_offline",
            "tdx_local",
            "local_csv",
            "market_warehouse",
            "warehouse",
            "warehouse_offline",
            *_VENDOR_ZIP_OVERLAY_ALIASES,
        }
        and live_provider_key in {"sina", "sina_minute"}
    ):
        interval_priority = tuple(config.runtime_live_interval_priority) or ("1m", "5m")
        primary_provider = HybridRuntimeProvider(
            base_provider=primary_provider,
            market_timezone=timezone,
            live_enabled=True,
            live_session_only=config.runtime_live_session_only,
            live_interval_priority=interval_priority,
            live_timeout_sec=max(1, int(config.runtime_live_timeout_sec)),
            live_cache_ttl_sec=max(1.0, float(config.runtime_live_cache_ttl_sec)),
        )
    online_backup = build_online_backup_provider(config)
    if not config.synthetic_fallback_allowed:
        # Production gate: never degrade to synthetic when primary is real.
        return ResilientProvider(
            primary=primary_provider,
            backup=online_backup,
            config=config,
        )
    synthetic_backup = SyntheticProvider(seed_offset=synthetic_seed)
    fallback: MarketDataProvider = synthetic_backup
    if online_backup is not None:
        fallback = ResilientProvider(
            primary=online_backup,
            backup=synthetic_backup,
            config=config,
        )
    return ResilientProvider(
        primary=primary_provider,
        backup=fallback,
        config=config,
    )


def build_market_depth_provider(config: MarketDepthConfig) -> MarketDepthProvider:
    """Build five-level market depth provider chain."""
    if not config.enabled:
        return EmptyMarketDepthProvider()
    primary = _build_single_market_depth_provider(
        source=config.primary,
        timeout_sec=config.timeout_sec,
    )
    backup = _build_single_market_depth_provider(
        source=config.backup,
        timeout_sec=config.timeout_sec,
    )
    provider: MarketDepthProvider = primary
    if not isinstance(backup, EmptyMarketDepthProvider):
        provider = FallbackMarketDepthProvider(primary=primary, backup=backup)
    return CachedMarketDepthProvider(
        inner=provider,
        ttl_sec=max(1.0, float(config.cache_ttl_sec)),
    )


def _build_single_market_depth_provider(
    *,
    source: str,
    timeout_sec: int,
) -> MarketDepthProvider:
    normalized = source.strip().lower()
    if normalized in {"", "none", "disabled", "off"}:
        return EmptyMarketDepthProvider()
    if normalized in {"easyquotation", "easyquotation_sina", "sina"}:
        return EasyQuotationMarketDepthProvider()
    if normalized in {"mootdx", "tdx"}:
        return MootdxMarketDepthProvider(timeout_sec=max(1, int(timeout_sec)))
    raise DataSourceError(f"unsupported market_depth source: {source}")


def _resolve_market_warehouse_package_root(config: DataSourceConfig) -> str:
    configured = config.local_data_root.strip()
    if configured:
        return configured
    db_path = config.warehouse_db_path.strip()
    if db_path:
        return str(Path(db_path).expanduser().resolve().parent / "package")
    return "artifacts/warehouse/package"


def _has_materialized_market_warehouse_package(package_root: str) -> bool:
    root = Path(package_root).expanduser()
    bars_root = root / "bars"
    if not bars_root.exists() or not bars_root.is_dir():
        return False
    for pattern in ("*.csv", "*.csv.gz", "*.parquet"):
        if any(bars_root.glob(pattern)):
            return True
    return False
