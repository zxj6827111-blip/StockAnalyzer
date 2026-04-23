from __future__ import annotations

from datetime import datetime

import pandas as pd

from stock_analyzer.data import hybrid_runtime_provider as hybrid_runtime_provider_module
from stock_analyzer.data.hybrid_runtime_provider import HybridRuntimeProvider


class StaticProvider:
    def __init__(
        self,
        *,
        daily_frame: pd.DataFrame,
        intraday_summary_map: dict[str, pd.DataFrame] | None = None,
    ) -> None:
        self._daily_frame = daily_frame
        self._intraday_summary_map = intraday_summary_map or {}

    def fetch_daily_bars(self, symbol: str, lookback_days: int = 120) -> pd.DataFrame:
        _ = symbol
        return self._daily_frame.tail(max(1, int(lookback_days))).copy()

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        _ = symbol
        frame = self._intraday_summary_map.get(interval, pd.DataFrame())
        return frame.tail(max(1, int(lookback_days))).copy()


def test_hybrid_runtime_provider_overlays_live_daily_session() -> None:
    base_frame = pd.DataFrame(
        {
            "open": [10.0, 10.2],
            "high": [10.3, 10.4],
            "low": [9.9, 10.1],
            "close": [10.1, 10.2],
            "volume": [1_000_000.0, 1_100_000.0],
            "turnover": [10_100_000.0, 11_220_000.0],
            "float_market_cap": [12_000_000_000.0, 12_100_000_000.0],
            "name": ["浦发银行", "浦发银行"],
            "background_data_source": ["tdx_offline", "tdx_offline"],
            "financial_source": ["tdx_offline", "tdx_offline"],
        },
        index=pd.to_datetime(["2026-03-05", "2026-03-06"]),
    )
    live_frame = pd.DataFrame(
        {
            "open": [10.30, 10.34, 10.37],
            "high": [10.35, 10.40, 10.45],
            "low": [10.28, 10.33, 10.35],
            "close": [10.34, 10.37, 10.42],
            "volume": [1000.0, 1200.0, 1500.0],
            "amount": [10340.0, 12444.0, 15630.0],
        },
        index=pd.to_datetime(
            ["2026-03-09 09:31:00", "2026-03-09 09:32:00", "2026-03-09 09:33:00"]
        ),
    )

    provider = HybridRuntimeProvider(
        base_provider=StaticProvider(daily_frame=base_frame),
        now_provider=lambda: datetime.fromisoformat("2026-03-09T10:00:00"),
        minute_fetcher=lambda **_: live_frame,
        live_cache_ttl_sec=1,
    )

    merged = provider.fetch_daily_bars(symbol="600000", lookback_days=10)

    assert merged.index.max() == pd.Timestamp("2026-03-09")
    assert float(merged.iloc[-1]["open"]) == 10.30
    assert float(merged.iloc[-1]["close"]) == 10.42
    assert float(merged.iloc[-1]["volume"]) == 3700.0
    assert float(merged.iloc[-1]["turnover"]) == 38414.0
    assert str(merged.iloc[-1]["background_data_source"]) == "tdx_offline+live"


def test_hybrid_runtime_provider_skips_live_overlay_outside_session() -> None:
    base_frame = pd.DataFrame(
        {
            "open": [10.0],
            "high": [10.3],
            "low": [9.9],
            "close": [10.1],
            "volume": [1_000_000.0],
            "turnover": [10_100_000.0],
            "float_market_cap": [12_000_000_000.0],
        },
        index=pd.to_datetime(["2026-03-06"]),
    )
    called = {"value": False}

    def _unexpected_fetch(**_: object) -> pd.DataFrame:
        called["value"] = True
        return pd.DataFrame()

    provider = HybridRuntimeProvider(
        base_provider=StaticProvider(daily_frame=base_frame),
        now_provider=lambda: datetime.fromisoformat("2026-03-09T20:00:00"),
        minute_fetcher=_unexpected_fetch,
    )

    merged = provider.fetch_daily_bars(symbol="600000", lookback_days=10)

    assert called["value"] is False
    pd.testing.assert_frame_equal(merged, base_frame)


def test_hybrid_runtime_provider_merges_live_intraday_summary() -> None:
    base_daily = pd.DataFrame(
        {
            "open": [10.0],
            "high": [10.3],
            "low": [9.9],
            "close": [10.1],
            "volume": [1_000_000.0],
            "turnover": [10_100_000.0],
            "float_market_cap": [12_000_000_000.0],
        },
        index=pd.to_datetime(["2026-03-06"]),
    )
    base_summary = pd.DataFrame(
        {
            "session_return": [0.01],
            "close_position": [0.7],
        },
        index=pd.to_datetime(["2026-03-06"]),
    )
    live_frame = pd.DataFrame(
        {
            "open": [10.30, 10.34, 10.37],
            "high": [10.35, 10.40, 10.45],
            "low": [10.28, 10.33, 10.35],
            "close": [10.34, 10.37, 10.42],
            "volume": [1000.0, 1200.0, 1500.0],
            "amount": [10340.0, 12444.0, 15630.0],
        },
        index=pd.to_datetime(
            ["2026-03-09 09:31:00", "2026-03-09 09:32:00", "2026-03-09 09:33:00"]
        ),
    )

    provider = HybridRuntimeProvider(
        base_provider=StaticProvider(
            daily_frame=base_daily,
            intraday_summary_map={"1m": base_summary},
        ),
        now_provider=lambda: datetime.fromisoformat("2026-03-09T10:05:00"),
        minute_fetcher=lambda **_: live_frame,
        live_cache_ttl_sec=1,
    )

    merged = provider.fetch_intraday_summary(symbol="600000", interval="1m", lookback_days=10)

    assert pd.Timestamp("2026-03-09") in merged.index
    assert "session_return" in merged.columns
    assert "close_position" in merged.columns


def test_hybrid_runtime_provider_reuses_cached_1m_frame_for_5m_summary() -> None:
    base_daily = pd.DataFrame(
        {
            "open": [10.0],
            "high": [10.3],
            "low": [9.9],
            "close": [10.1],
            "volume": [1_000_000.0],
            "turnover": [10_100_000.0],
            "float_market_cap": [12_000_000_000.0],
        },
        index=pd.to_datetime(["2026-03-06"]),
    )
    base_summary = pd.DataFrame(
        {
            "session_return": [0.01],
            "close_position": [0.7],
        },
        index=pd.to_datetime(["2026-03-06"]),
    )
    live_frame = pd.DataFrame(
        {
            "open": [10.30, 10.34, 10.37, 10.40, 10.42],
            "high": [10.35, 10.36, 10.39, 10.43, 10.45],
            "low": [10.28, 10.33, 10.35, 10.38, 10.40],
            "close": [10.34, 10.35, 10.38, 10.42, 10.44],
            "volume": [1000.0, 1200.0, 1300.0, 1400.0, 1500.0],
            "amount": [10340.0, 12420.0, 13494.0, 14588.0, 15660.0],
        },
        index=pd.to_datetime(
            [
                "2026-03-09 09:31:00",
                "2026-03-09 09:32:00",
                "2026-03-09 09:33:00",
                "2026-03-09 09:34:00",
                "2026-03-09 09:35:00",
            ]
        ),
    )
    calls: list[str] = []

    def _counted_fetch(*, interval: str, **_: object) -> pd.DataFrame:
        calls.append(interval)
        return live_frame

    provider = HybridRuntimeProvider(
        base_provider=StaticProvider(
            daily_frame=base_daily,
            intraday_summary_map={"1m": base_summary, "5m": base_summary},
        ),
        now_provider=lambda: datetime.fromisoformat("2026-03-09T10:05:00"),
        minute_fetcher=_counted_fetch,
        live_cache_ttl_sec=15,
    )

    provider.fetch_daily_bars(symbol="600000", lookback_days=10)
    provider.fetch_intraday_summary(symbol="600000", interval="1m", lookback_days=10)
    merged = provider.fetch_intraday_summary(symbol="600000", interval="5m", lookback_days=10)

    assert calls == ["1m"]
    assert pd.Timestamp("2026-03-09") in merged.index
    assert "session_return" in merged.columns


def test_hybrid_runtime_provider_prefers_5m_fetch_without_cached_1m_frame() -> None:
    base_daily = pd.DataFrame(
        {
            "open": [10.0],
            "high": [10.3],
            "low": [9.9],
            "close": [10.1],
            "volume": [1_000_000.0],
            "turnover": [10_100_000.0],
            "float_market_cap": [12_000_000_000.0],
        },
        index=pd.to_datetime(["2026-03-06"]),
    )
    base_summary = pd.DataFrame(
        {
            "session_return": [0.01],
            "close_position": [0.7],
        },
        index=pd.to_datetime(["2026-03-06"]),
    )
    live_frame = pd.DataFrame(
        {
            "open": [10.30, 10.34, 10.37, 10.40, 10.42],
            "high": [10.35, 10.36, 10.39, 10.43, 10.45],
            "low": [10.28, 10.33, 10.35, 10.38, 10.40],
            "close": [10.34, 10.35, 10.38, 10.42, 10.44],
            "volume": [1000.0, 1200.0, 1300.0, 1400.0, 1500.0],
            "amount": [10340.0, 12420.0, 13494.0, 14588.0, 15660.0],
        },
        index=pd.to_datetime(
            [
                "2026-03-09 09:31:00",
                "2026-03-09 09:32:00",
                "2026-03-09 09:33:00",
                "2026-03-09 09:34:00",
                "2026-03-09 09:35:00",
            ]
        ),
    )
    calls: list[str] = []

    def _counted_fetch(*, interval: str, **_: object) -> pd.DataFrame:
        calls.append(interval)
        return live_frame

    provider = HybridRuntimeProvider(
        base_provider=StaticProvider(
            daily_frame=base_daily,
            intraday_summary_map={"5m": base_summary},
        ),
        now_provider=lambda: datetime.fromisoformat("2026-03-09T10:05:00"),
        minute_fetcher=_counted_fetch,
        live_cache_ttl_sec=15,
    )

    merged = provider.fetch_intraday_summary(symbol="600000", interval="5m", lookback_days=10)

    assert calls == ["5m"]
    assert pd.Timestamp("2026-03-09") in merged.index
    assert "session_return" in merged.columns


def test_hybrid_runtime_provider_reuses_live_frame_within_extended_ttl(
    monkeypatch,
) -> None:
    base_frame = pd.DataFrame(
        {
            "open": [10.0],
            "high": [10.3],
            "low": [9.9],
            "close": [10.1],
            "volume": [1_000_000.0],
            "turnover": [10_100_000.0],
            "float_market_cap": [12_000_000_000.0],
        },
        index=pd.to_datetime(["2026-03-06"]),
    )
    live_frame = pd.DataFrame(
        {
            "open": [10.30, 10.34],
            "high": [10.35, 10.40],
            "low": [10.28, 10.33],
            "close": [10.34, 10.37],
            "volume": [1000.0, 1200.0],
            "amount": [10340.0, 12444.0],
        },
        index=pd.to_datetime(["2026-03-09 09:31:00", "2026-03-09 09:32:00"]),
    )
    calls = {"count": 0}
    perf_values = iter([0.0, 10.0])

    def _counted_fetch(**_: object) -> pd.DataFrame:
        calls["count"] += 1
        return live_frame

    monkeypatch.setattr(
        hybrid_runtime_provider_module,
        "perf_counter",
        lambda: next(perf_values),
    )

    provider = HybridRuntimeProvider(
        base_provider=StaticProvider(daily_frame=base_frame),
        now_provider=lambda: datetime.fromisoformat("2026-03-09T10:00:00"),
        minute_fetcher=_counted_fetch,
        live_cache_ttl_sec=15,
    )

    provider.fetch_daily_bars(symbol="600000", lookback_days=10)
    provider.fetch_daily_bars(symbol="600000", lookback_days=10)

    assert calls["count"] == 1
    assert provider.status()["live_overlay_cache_ttl_sec"] == 15.0


def test_hybrid_runtime_provider_refreshes_live_frame_after_short_ttl_expires(
    monkeypatch,
) -> None:
    base_frame = pd.DataFrame(
        {
            "open": [10.0],
            "high": [10.3],
            "low": [9.9],
            "close": [10.1],
            "volume": [1_000_000.0],
            "turnover": [10_100_000.0],
            "float_market_cap": [12_000_000_000.0],
        },
        index=pd.to_datetime(["2026-03-06"]),
    )
    live_frame = pd.DataFrame(
        {
            "open": [10.30],
            "high": [10.35],
            "low": [10.28],
            "close": [10.34],
            "volume": [1000.0],
            "amount": [10340.0],
        },
        index=pd.to_datetime(["2026-03-09 09:31:00"]),
    )
    calls = {"count": 0}
    perf_values = iter([0.0, 10.0])

    def _counted_fetch(**_: object) -> pd.DataFrame:
        calls["count"] += 1
        return live_frame

    monkeypatch.setattr(
        hybrid_runtime_provider_module,
        "perf_counter",
        lambda: next(perf_values),
    )

    provider = HybridRuntimeProvider(
        base_provider=StaticProvider(daily_frame=base_frame),
        now_provider=lambda: datetime.fromisoformat("2026-03-09T10:00:00"),
        minute_fetcher=_counted_fetch,
        live_cache_ttl_sec=8,
    )

    provider.fetch_daily_bars(symbol="600000", lookback_days=10)
    provider.fetch_daily_bars(symbol="600000", lookback_days=10)

    assert calls["count"] == 2
