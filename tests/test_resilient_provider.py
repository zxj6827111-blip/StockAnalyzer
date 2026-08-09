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
