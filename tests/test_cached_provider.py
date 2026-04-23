from __future__ import annotations

import pandas as pd

from stock_analyzer.data.cached_provider import CachedProvider
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.infra.cache import InMemoryCache


class _ToggleProvider:
    def __init__(self) -> None:
        self.fail = False
        self.calls = 0
        self._provider = SyntheticProvider(seed_offset=123)

    def fetch_daily_bars(self, symbol: str, lookback_days: int = 120) -> pd.DataFrame:
        self.calls += 1
        if self.fail:
            raise RuntimeError("upstream failed")
        return self._provider.fetch_daily_bars(symbol=symbol, lookback_days=lookback_days)

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        if self.fail:
            raise RuntimeError("upstream failed")
        return self._provider.fetch_intraday_summary(
            symbol=symbol,
            interval=interval,
            lookback_days=lookback_days,
        )


def test_cached_provider_reads_from_cache_after_first_fetch() -> None:
    upstream = _ToggleProvider()
    provider = CachedProvider(inner=upstream, cache=InMemoryCache(), ttl_sec=3600)

    first = provider.fetch_daily_bars(symbol="600000", lookback_days=60)
    second = provider.fetch_daily_bars(symbol="600000", lookback_days=60)

    assert not first.empty
    assert not second.empty
    assert upstream.calls == 1
    assert provider.cache_hits == 1
