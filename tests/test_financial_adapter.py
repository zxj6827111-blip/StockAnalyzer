from __future__ import annotations

from collections.abc import Sequence

import pandas as pd

from stock_analyzer.data.financial_adapter import AkshareFinancialAdapter


def _as_bool(value: object) -> bool:
    assert isinstance(value, bool)
    return value


def _as_float(value: object) -> float:
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    return float(value)


def _as_text(value: object) -> str:
    assert isinstance(value, str)
    return value


def _as_text_list(value: object) -> list[str]:
    assert isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    items = [_as_text(item) for item in value]
    assert len(items) == len(value)
    return items


class _FakeAkColumnStyle:
    def __init__(self) -> None:
        self.financial_calls = 0

    @staticmethod
    def stock_info_a_code_name() -> pd.DataFrame:
        return pd.DataFrame({"code": ["600000"], "name": ["ST浦发"]})

    def stock_financial_analysis_indicator(self, symbol: str) -> pd.DataFrame:  # noqa: ARG002
        self.financial_calls += 1
        return pd.DataFrame(
            {
                "报告期": ["2024-12-31"],
                "净资产收益率(%)": [8.5],
                "资产负债率(%)": [62.0],
            }
        )


class _FakeAkRowStyle:
    @staticmethod
    def stock_info_a_code_name() -> pd.DataFrame:
        return pd.DataFrame({"代码": ["300001"], "名称": ["特锐德"]})

    @staticmethod
    def stock_financial_analysis_indicator(symbol: str) -> pd.DataFrame:  # noqa: ARG002
        return pd.DataFrame()

    @staticmethod
    def stock_financial_abstract(symbol: str) -> pd.DataFrame:  # noqa: ARG002
        return pd.DataFrame(
            {
                "指标": ["净资产收益率", "资产负债率"],
                "2024-12-31": ["10.2%", "55.1%"],
            }
        )


class _FakeAkMultiSource:
    @staticmethod
    def stock_info_a_code_name() -> pd.DataFrame:
        return pd.DataFrame({"代码": ["000001"], "名称": ["平安银行"]})

    @staticmethod
    def stock_financial_analysis_indicator(symbol: str) -> pd.DataFrame:  # noqa: ARG002
        return pd.DataFrame(
            {
                "报告期": ["2024-12-31"],
                "净资产收益率(%)": [9.3],
            }
        )

    @staticmethod
    def stock_financial_abstract(symbol: str) -> pd.DataFrame:  # noqa: ARG002
        return pd.DataFrame(
            {
                "报告期": ["2024-12-31"],
                "资产负债率(%)": [61.2],
            }
        )


def test_financial_adapter_parses_column_style_metrics_and_st_flag() -> None:
    fake = _FakeAkColumnStyle()
    adapter = AkshareFinancialAdapter(cache_ttl_sec=3600, ak_module=fake)
    snapshot = adapter.fetch_snapshot("600000")
    assert _as_bool(snapshot["is_st"]) is True
    assert _as_bool(snapshot["is_delisting_risk"]) is False
    assert abs(_as_float(snapshot["roe"]) - 0.085) < 1e-9
    assert abs(_as_float(snapshot["debt_ratio"]) - 0.62) < 1e-9
    assert _as_text(snapshot["source"]) == "stock_financial_analysis_indicator"
    assert _as_bool(snapshot["financial_data_complete"]) is True
    assert _as_text_list(snapshot["missing_fields"]) == []


def test_financial_adapter_parses_row_style_metrics() -> None:
    adapter = AkshareFinancialAdapter(cache_ttl_sec=3600, ak_module=_FakeAkRowStyle())
    snapshot = adapter.fetch_snapshot("300001")
    assert _as_bool(snapshot["is_st"]) is False
    assert abs(_as_float(snapshot["roe"]) - 0.102) < 1e-9
    assert abs(_as_float(snapshot["debt_ratio"]) - 0.551) < 1e-9
    assert _as_text(snapshot["source"]) == "stock_financial_abstract"
    assert _as_bool(snapshot["financial_data_complete"]) is True


def test_financial_adapter_uses_cache_within_ttl() -> None:
    fake = _FakeAkColumnStyle()
    adapter = AkshareFinancialAdapter(cache_ttl_sec=3600, ak_module=fake)
    _ = adapter.fetch_snapshot("600000")
    _ = adapter.fetch_snapshot("600000")
    assert fake.financial_calls == 1


def test_financial_adapter_merges_multi_source_metrics() -> None:
    adapter = AkshareFinancialAdapter(cache_ttl_sec=3600, ak_module=_FakeAkMultiSource())
    snapshot = adapter.fetch_snapshot("000001")
    assert abs(_as_float(snapshot["roe"]) - 0.093) < 1e-9
    assert abs(_as_float(snapshot["debt_ratio"]) - 0.612) < 1e-9
    assert _as_bool(snapshot["financial_data_complete"]) is True
    sources = _as_text_list(snapshot["sources"])
    assert "stock_financial_analysis_indicator" in sources
    assert "stock_financial_abstract" in sources


class _FakeAkNameFallback:
    """首个名录源不可用（NAS 上上交所接口被拒），备源代码带交易所前缀。"""

    def __init__(self) -> None:
        self.tx_calls = 0

    def stock_info_a_code_name(self) -> pd.DataFrame:
        raise ConnectionError("query.sse.com.cn blocked")

    def stock_zh_a_spot_tx(self) -> pd.DataFrame:
        self.tx_calls += 1
        return pd.DataFrame(
            [
                {"code": "sh600000", "name": "ST浦发"},
                {"code": "bj920000", "name": "安徽凤凰"},
            ]
        )


def test_financial_adapter_name_lookup_falls_back_and_normalizes_prefixed_code() -> None:
    """名录源逐源降级 + 带前缀代码归一化。

    回归 2026-09-10：名称映射只试 `stock_info_a_code_name`（NAS 上被网络策略
    拒绝），失败即得到空名称表 —— ST/退市识别随之失效。且腾讯/新浪源返回的是
    ``sh600000`` 这类带前缀代码，必须归一化成 6 位数字键才能按 symbol 命中。
    """
    fake = _FakeAkNameFallback()
    adapter = AkshareFinancialAdapter(cache_ttl_sec=3600, ak_module=fake)

    snapshot = adapter.fetch_snapshot("600000")

    assert fake.tx_calls == 1
    assert _as_text(snapshot["name"]) == "ST浦发"
    assert _as_bool(snapshot["is_st"]) is True
    # 同一张表在 TTL 内复用，不重复拉取
    _ = adapter.fetch_snapshot("920000")
    assert fake.tx_calls == 1
