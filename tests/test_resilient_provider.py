from __future__ import annotations

import pandas as pd

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
