from __future__ import annotations

from collections.abc import Mapping, Sequence

import pandas as pd

from stock_analyzer.data.market_depth import (
    EasyQuotationMarketDepthProvider,
    FallbackMarketDepthProvider,
    MootdxMarketDepthProvider,
)


def _as_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def _as_mapping_list(value: object) -> list[Mapping[str, object]]:
    assert isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    items = [_as_mapping(item) for item in value]
    assert len(items) == len(value)
    return items


def _patch_attr(target: object, name: str, value: object) -> None:
    object.__setattr__(target, name, value)


class _FakeEasyQuotationClient:
    def real(self, symbols: list[str], prefix: bool = False) -> dict[str, dict[str, object]]:
        assert prefix is True
        assert symbols == ["sh600000"]
        return {
            "sh600000": {
                "name": "浦发银行",
                "open": 10.20,
                "close": 10.10,
                "now": 10.32,
                "date": "2026-03-10",
                "time": "10:21:03",
                "bid1": 10.31,
                "bid1_volume": 1300,
                "bid2": 10.30,
                "bid2_volume": 1200,
                "bid3": 10.29,
                "bid3_volume": 1100,
                "bid4": 10.28,
                "bid4_volume": 1000,
                "bid5": 10.27,
                "bid5_volume": 900,
                "ask1": 10.33,
                "ask1_volume": 1400,
                "ask2": 10.34,
                "ask2_volume": 1500,
                "ask3": 10.35,
                "ask3_volume": 1600,
                "ask4": 10.36,
                "ask4_volume": 1700,
                "ask5": 10.37,
                "ask5_volume": 1800,
            }
        }


class _FailingProvider:
    def fetch_snapshots(
        self,
        symbols: list[str],
        *,
        force_refresh: bool = False,
    ) -> dict[str, dict[str, object]]:
        _ = symbols, force_refresh
        raise RuntimeError("boom")

    def status(self) -> dict[str, object]:
        return {"provider": "failing"}


class _FakeMootdxClient:
    def quotes(self, symbol: list[str]) -> pd.DataFrame:
        assert symbol == ["600000"]
        return pd.DataFrame(
            [
                {
                    "code": "600000",
                    "name": "浦发银行",
                    "price": 10.32,
                    "last_close": 10.10,
                    "open": 10.20,
                    "bid1": 10.31,
                    "bid_vol1": 1300,
                    "bid2": 10.30,
                    "bid_vol2": 1200,
                    "bid3": 10.29,
                    "bid_vol3": 1100,
                    "bid4": 10.28,
                    "bid_vol4": 1000,
                    "bid5": 10.27,
                    "bid_vol5": 900,
                    "ask1": 10.33,
                    "ask_vol1": 1400,
                    "ask2": 10.34,
                    "ask_vol2": 1500,
                    "ask3": 10.35,
                    "ask_vol3": 1600,
                    "ask4": 10.36,
                    "ask_vol4": 1700,
                    "ask5": 10.37,
                    "ask_vol5": 1800,
                    "servertime": "10:22:01",
                }
            ]
        )


def test_easyquotation_market_depth_provider_parses_five_levels() -> None:
    provider = EasyQuotationMarketDepthProvider()
    _patch_attr(provider, "_quotation_factory", lambda: _FakeEasyQuotationClient())

    payload = provider.fetch_snapshots(["600000"], force_refresh=True)

    snapshot = _as_mapping(payload["600000"])
    assert snapshot["available"] is True
    assert snapshot["source"] == "easyquotation_sina"
    assert snapshot["timestamp"] == "2026-03-10 10:21:03"
    assert _as_mapping_list(snapshot["bid_levels"])[0]["price"] == 10.31
    assert _as_mapping_list(snapshot["ask_levels"])[0]["volume"] == 1400
    assert snapshot["spread"] == 0.02


def test_fallback_market_depth_provider_uses_backup_when_primary_fails() -> None:
    backup = MootdxMarketDepthProvider()
    _patch_attr(backup, "_quotes_factory", lambda: _FakeMootdxClient())
    provider = FallbackMarketDepthProvider(primary=_FailingProvider(), backup=backup)

    payload = provider.fetch_snapshots(["600000"], force_refresh=True)

    snapshot = _as_mapping(payload["600000"])
    assert snapshot["available"] is True
    assert snapshot["source"] == "mootdx"
    assert _as_mapping_list(snapshot["bid_levels"])[0]["price"] == 10.31
