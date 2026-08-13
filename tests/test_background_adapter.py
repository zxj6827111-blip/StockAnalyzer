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


def test_background_adapter_complete_requires_all_core_fields() -> None:
    """分级完整性：五个核心来源（大宗/两融/北向/龙虎榜）齐全才 True；
    holder_count 提供时 missing 清单为空。"""
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

    # 五核心齐全（fake 全量提供），complete=True。
    assert bool(enriched.iloc[-1]["background_data_complete"]) is True
    missing = str(enriched.iloc[-1]["background_missing_fields"])
    assert missing == ""


class _FakeBackgroundAkNoHolder(_FakeBackgroundAk):
    """与 _FakeBackgroundAk 相同，但缺 holder（股东户数）来源。"""

    @staticmethod
    def stock_zh_a_gdhs(symbol: str) -> None:  # noqa: ARG002
        return None


def test_background_adapter_optional_missing_keeps_complete() -> None:
    """仅缺可选字段 holder_count：complete 仍为 True，missing 清单以
    optional: 前缀标注，不翻转核心完整性。"""
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

    adapter = AkshareBackgroundAdapter(
        cache_ttl_sec=3600,
        ak_module=_FakeBackgroundAkNoHolder(),
    )
    enriched = adapter.enrich_bars(symbol="600000", bars=bars)

    assert bool(enriched.iloc[-1]["background_data_complete"]) is True
    missing = str(enriched.iloc[-1]["background_missing_fields"])
    assert "optional:holder_count" in missing
    for core in (
        "block_trade_net",
        "margin_financing_balance",
        "northbound_net",
        "dragon_tiger_flag",
    ):
        assert core not in missing


class _FakeBackgroundAkAllMissing:
    """所有背景来源都不可用：核心与可选字段全部缺失（不触发真实 import）。"""

    @staticmethod
    def stock_zh_a_gdhs(symbol: str) -> None:  # noqa: ARG002
        return None

    @staticmethod
    def stock_dzjy_mrtj(symbol: str) -> None:  # noqa: ARG002
        return None

    @staticmethod
    def stock_margin_detail_sse(symbol: str) -> None:  # noqa: ARG002
        return None

    @staticmethod
    def stock_hsgt_individual_em(symbol: str) -> None:  # noqa: ARG002
        return None

    @staticmethod
    def stock_lhb_detail_em(symbol: str) -> None:  # noqa: ARG002
        return None


def test_background_adapter_core_missing_marks_incomplete() -> None:
    """核心来源全部缺失 => complete=False 且 missing 清单列出缺失核心字段。"""
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

    adapter = AkshareBackgroundAdapter(
        cache_ttl_sec=3600,
        ak_module=_FakeBackgroundAkAllMissing(),
    )
    enriched = adapter.enrich_bars(symbol="600000", bars=bars)

    assert bool(enriched.iloc[-1]["background_data_complete"]) is False
    missing = str(enriched.iloc[-1]["background_missing_fields"])
    for core in (
        "block_trade_net",
        "margin_financing_balance",
        "northbound_net",
        "dragon_tiger_flag",
    ):
        assert core in missing
    assert "optional:holder_count" in missing
