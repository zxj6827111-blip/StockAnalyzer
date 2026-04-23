from __future__ import annotations

import pandas as pd

from stock_analyzer.data.background_adapter import AkshareBackgroundAdapter


class _FakeBackgroundAk:
    @staticmethod
    def stock_zh_a_gdhs(symbol: str) -> pd.DataFrame:  # noqa: ARG002
        return pd.DataFrame(
            {
                "代码": ["600000", "600000"],
                "日期": ["2026-01-02", "2026-01-05"],
                "股东户数": [100_000, 95_000],
            }
        )

    @staticmethod
    def stock_dzjy_mrtj(symbol: str) -> pd.DataFrame:  # noqa: ARG002
        return pd.DataFrame(
            {
                "股票代码": ["600000"],
                "日期": ["2026-01-05"],
                "净买额": [2_000_000],
            }
        )

    @staticmethod
    def stock_margin_detail_sse(symbol: str) -> pd.DataFrame:  # noqa: ARG002
        return pd.DataFrame(
            {
                "股票代码": ["600000", "600000"],
                "日期": ["2026-01-02", "2026-01-05"],
                "融资余额": [1_100_000_000, 1_150_000_000],
            }
        )

    @staticmethod
    def stock_hsgt_individual_em(symbol: str) -> pd.DataFrame:  # noqa: ARG002
        return pd.DataFrame(
            {
                "股票代码": ["600000"],
                "日期": ["2026-01-05"],
                "净买入": [12_000_000],
            }
        )

    @staticmethod
    def stock_lhb_detail_em(symbol: str) -> pd.DataFrame:  # noqa: ARG002
        return pd.DataFrame(
            {
                "股票代码": ["600000"],
                "上榜日期": ["2026-01-05"],
            }
        )


def test_background_adapter_enriches_daily_series() -> None:
    index = pd.bdate_range("2026-01-02", periods=3)
    bars = pd.DataFrame(
        {
            "open": [10.0, 10.1, 10.2],
            "high": [10.3, 10.4, 10.5],
            "low": [9.9, 10.0, 10.1],
            "close": [10.2, 10.3, 10.4],
            "volume": [2_000_000, 2_100_000, 2_200_000],
            "turnover": [20_000_000, 21_630_000, 22_880_000],
            "float_market_cap": [12_000_000_000.0] * 3,
        },
        index=index,
    )
    bars.index.name = "date"

    adapter = AkshareBackgroundAdapter(cache_ttl_sec=3600, ak_module=_FakeBackgroundAk())
    enriched = adapter.enrich_bars(symbol="600000", bars=bars)

    assert "holder_count" in enriched.columns
    assert "block_trade_net" in enriched.columns
    assert "financing_balance" in enriched.columns
    assert "margin_financing_balance" in enriched.columns
    assert "northbound_net" in enriched.columns
    assert "dragon_tiger_flag" in enriched.columns
    assert float(enriched.iloc[-1]["holder_count"]) == 95_000
    assert float(enriched.iloc[1]["block_trade_net"]) == 2_000_000
    assert float(enriched.iloc[1]["dragon_tiger_flag"]) == 1.0
    assert bool(enriched.iloc[-1]["background_data_complete"]) is True
