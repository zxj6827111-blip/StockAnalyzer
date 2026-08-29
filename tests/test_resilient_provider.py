from __future__ import annotations

import pandas as pd
import pytest

from stock_analyzer.config import DataSourceConfig
from stock_analyzer.data.provider import DataSourceError, SyntheticProvider
from stock_analyzer.data.resilient_provider import ResilientProvider


class AlwaysFailProvider:
    def fetch_daily_bars(self, symbol: str, lookback_days: int = 120) -> pd.DataFrame:
        raise DataSourceError(f"forced failure:{symbol}:{lookback_days}")

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        raise DataSourceError(f"forced failure:{symbol}:{interval}:{lookback_days}")


class FailThenRecoverProvider:
    """Fails for the first `failures_before_success` calls, then succeeds."""

    def __init__(self, failures_before_success: int = 1) -> None:
        self.failures_before_success = failures_before_success
        self.attempts = 0

    def fetch_daily_bars(
        self,
        symbol: str,
        lookback_days: int = 120,
        *,
        end_date=None,
    ) -> pd.DataFrame:
        self.attempts += 1
        if self.attempts <= self.failures_before_success:
            raise DataSourceError(f"forced failure:{symbol}:attempt:{self.attempts}")
        return SyntheticProvider(seed_offset=9).fetch_daily_bars(
            symbol=symbol,
            lookback_days=lookback_days,
            end_date=end_date,
        )

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        _ = symbol, interval, lookback_days
        return pd.DataFrame()


def test_resilient_provider_enters_degraded_mode_and_uses_backup() -> None:
    config = DataSourceConfig(
        primary="akshare",
        enable_cache_fallback=True,
        switch_after_failures=2,
        request_interval_sec=0.5,
        degrade_stops_new_buy=True,
    )
    provider = ResilientProvider(
        primary=AlwaysFailProvider(), backup=SyntheticProvider(), config=config
    )

    first = provider.fetch_daily_bars("600000")
    assert not first.empty
    assert provider.degraded_mode is False

    second = provider.fetch_daily_bars("600000")
    assert not second.empty
    assert provider.degraded_mode is True


def test_resilient_provider_resets_degraded_mode_after_success() -> None:
    config = DataSourceConfig(
        primary="akshare",
        enable_cache_fallback=True,
        switch_after_failures=1,
        request_interval_sec=0.5,
        degrade_stops_new_buy=True,
    )
    provider = ResilientProvider(
        primary=FailThenRecoverProvider(failures_before_success=1),
        backup=SyntheticProvider(),
        config=config,
    )

    # First call fails on primary and falls back to backup -> degraded.
    first = provider.fetch_daily_bars("600000")
    assert not first.empty
    assert provider.degraded_mode is True
    assert provider.consecutive_failures == 1
    assert provider.last_error

    # Second call succeeds on primary -> degraded mode resets.
    second = provider.fetch_daily_bars("600000")
    assert not second.empty
    assert provider.degraded_mode is False
    assert provider.consecutive_failures == 0
    assert provider.last_error == ""


def test_resilient_provider_raises_when_primary_and_backup_fail() -> None:
    config = DataSourceConfig(
        primary="akshare",
        enable_cache_fallback=True,
        switch_after_failures=2,
        request_interval_sec=0.5,
        degrade_stops_new_buy=True,
    )
    provider = ResilientProvider(
        primary=AlwaysFailProvider(),
        backup=AlwaysFailProvider(),
        config=config,
    )

    with pytest.raises(DataSourceError) as exc_info:
        provider.fetch_daily_bars("600000")
    message = str(exc_info.value)
    assert "primary failed" in message
    assert "backup failed" in message
    assert provider.consecutive_failures == 1


class _BatchCapableProvider:
    """模拟 warehouse/vendor_zip_overlay 链：暴露全市场批量接口。"""

    def __init__(self) -> None:
        self.quality_calls: list[dict[str, object]] = []

    def list_symbols(self) -> list[str]:
        return ["600000", "000001"]

    def fetch_universe_quality_metrics(
        self,
        *,
        limit: int,
        end_date: object = None,
        **kwargs: object,
    ) -> pd.DataFrame:
        self.quality_calls.append({"limit": limit, "end_date": end_date, **kwargs})
        return pd.DataFrame({"symbol": ["600000"], "latest_date": [pd.Timestamp("2026-07-31")]})


class _NoBatchProvider:
    """模拟在线 backup 链（efinance/akshare）：不暴露任何批量接口。"""


def _resilient(primary: object) -> ResilientProvider:
    config = DataSourceConfig(primary="akshare", request_interval_sec=0.5)
    return ResilientProvider(primary=primary, config=config)  # type: ignore[arg-type]


def test_resilient_provider_passes_through_week5_historical_batch_surface() -> None:
    """Week5 历史回测依赖的两个批量接口必须穿透韧性包装层。

    回归背景：生产 provider 链最外层是 ResilientProvider（slots dataclass，
    只显式透传列出的方法），week5_daily 回测的股票池解析在
    ``provider.list_symbols()`` 上炸出 AttributeError。这里把透传契约
    （调用转发 + 关键字参数原样传递）固定下来。
    """
    primary = _BatchCapableProvider()
    provider = _resilient(primary)

    assert provider.list_symbols() == ["600000", "000001"]

    frame = provider.fetch_universe_quality_metrics(
        limit=50,
        end_date="2026-07-31",
        min_amount=1.0,
    )
    assert not frame.empty
    # 关键字参数（含 end_date 与额外筛选参数）必须原样到达 primary。
    assert primary.quality_calls == [{"limit": 50, "end_date": "2026-07-31", "min_amount": 1.0}]


def test_resilient_provider_batch_methods_fail_closed_without_primary_support() -> None:
    """primary 没有批量接口时显式报错（fail-closed），绝不静默返回空池。"""
    provider = _resilient(_NoBatchProvider())

    with pytest.raises(DataSourceError, match="list_symbols"):
        provider.list_symbols()
    with pytest.raises(DataSourceError, match="fetch_universe_quality_metrics"):
        provider.fetch_universe_quality_metrics(limit=10)


def test_runtime_provider_chain_exposes_week5_historical_batch_surface() -> None:
    """生产同款工厂链（market_warehouse primary → ResilientProvider 包装）
    必须暴露 Week5 历史回测的批量接口——防「fake provider 直通测试全绿、
    真实链在包装层缺方法」的契约缺口再次出现。"""
    from stock_analyzer.data.provider_factory import build_runtime_provider

    config = DataSourceConfig(
        primary="market_warehouse",
        local_data_root="/tmp/unused",
        warehouse_db_path="/tmp/unused/warehouse.duckdb",
    )
    provider = build_runtime_provider(config)

    assert hasattr(provider, "list_symbols")
    assert callable(provider.list_symbols)
    assert hasattr(provider, "fetch_universe_quality_metrics")
    assert callable(provider.fetch_universe_quality_metrics)
