"""Data providers for market data acquisition."""

from stock_analyzer.data.akshare_provider import AkshareProvider
from stock_analyzer.data.background_adapter import AkshareBackgroundAdapter
from stock_analyzer.data.cached_provider import CachedProvider
from stock_analyzer.data.efinance_provider import EfinanceProvider
from stock_analyzer.data.financial_adapter import AkshareFinancialAdapter, FinancialSnapshot
from stock_analyzer.data.provider import DataSourceError, MarketDataProvider, SyntheticProvider
from stock_analyzer.data.provider_factory import (
    build_online_backup_provider,
    build_primary_provider,
    build_runtime_provider,
)
from stock_analyzer.data.resilient_provider import ResilientProvider
from stock_analyzer.data.tdx_offline_provider import TdxOfflineProvider

__all__ = [
    "AkshareProvider",
    "AkshareBackgroundAdapter",
    "AkshareFinancialAdapter",
    "build_online_backup_provider",
    "build_primary_provider",
    "build_runtime_provider",
    "CachedProvider",
    "DataSourceError",
    "EfinanceProvider",
    "FinancialSnapshot",
    "MarketDataProvider",
    "ResilientProvider",
    "SyntheticProvider",
    "TdxOfflineProvider",
]
