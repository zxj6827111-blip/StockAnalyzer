from __future__ import annotations

import json

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


def test_deserialize_frame_legacy_raw_split_json() -> None:
    """复核4 回归：旧缓存值直接是 orient='split' 的 JSON（无 {"frame": ...}
    包裹）时必须可读，不能抛 ValueError；索引名丢失与 HEAD 行为一致。"""
    from stock_analyzer.data.cached_provider import _deserialize_frame

    frame = SyntheticProvider(seed_offset=123).fetch_daily_bars(
        symbol="600000", lookback_days=60
    )
    legacy_raw = frame.to_json(date_format="iso", orient="split")

    decoded = _deserialize_frame(legacy_raw)
    assert isinstance(decoded.index, pd.DatetimeIndex)
    assert decoded.index.name is None
    # JSON round-trip 不保留 index freq（BusinessDay），此处不比较 freq。
    pd.testing.assert_frame_equal(
        decoded, frame, check_names=False, check_freq=False, check_dtype=False
    )


def test_deserialize_frame_head_format_without_index_name() -> None:
    """复核4 回归：HEAD 时代格式 {"frame": split_json}（无 index_name 键）
    仍可读。"""
    from stock_analyzer.data.cached_provider import _deserialize_frame

    frame = SyntheticProvider(seed_offset=123).fetch_daily_bars(
        symbol="600000", lookback_days=60
    )
    head_raw = json.dumps(
        {"frame": frame.to_json(date_format="iso", orient="split")}
    )

    decoded = _deserialize_frame(head_raw)
    assert isinstance(decoded.index, pd.DatetimeIndex)
    # JSON round-trip 不保留 index freq（BusinessDay），此处不比较 freq。
    pd.testing.assert_frame_equal(
        decoded, frame, check_names=False, check_freq=False, check_dtype=False
    )


def test_deserialize_frame_current_format_preserves_index_name() -> None:
    """当前格式 round-trip：index name 必须保留（snapshot 增量拼接依赖）。"""
    from stock_analyzer.data.cached_provider import _deserialize_frame, _serialize_frame

    frame = SyntheticProvider(seed_offset=123).fetch_daily_bars(
        symbol="600000", lookback_days=60
    )
    assert frame.index.name == "date"

    decoded = _deserialize_frame(_serialize_frame(frame))
    assert decoded.index.name == "date"
    pd.testing.assert_frame_equal(decoded, frame, check_freq=False, check_dtype=False)


def test_cached_provider_reads_legacy_cache_payload() -> None:
    """Legacy cache hits must also restore the date index name.
    Otherwise snapshot transform fails after reset_index() produces ``index``.
    """
    upstream = _ToggleProvider()
    cache = InMemoryCache()
    provider = CachedProvider(inner=upstream, cache=cache, ttl_sec=3600)

    frame = upstream._provider.fetch_daily_bars(symbol="600000", lookback_days=60)
    legacy_raw = frame.to_json(date_format="iso", orient="split")
    cache.set("provider:bars:600000:60:latest", legacy_raw, ttl_sec=3600)

    result = provider.fetch_daily_bars(symbol="600000", lookback_days=60)
    assert provider.cache_hits == 1
    assert upstream.calls == 0
    assert isinstance(result.index, pd.DatetimeIndex)
    assert result.index.name == "date"
    pd.testing.assert_frame_equal(
        result, frame, check_freq=False, check_dtype=False
    )
