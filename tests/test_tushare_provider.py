from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from stock_analyzer.data import tushare_provider as tushare_provider_module
from stock_analyzer.data.provider import DataSourceError
from stock_analyzer.data.tushare_provider import (
    TushareProvider,
    _apply_price_adjust,
    _to_ts_code,
)


class _FakePro:
    def __init__(
        self,
        daily: pd.DataFrame,
        basic: pd.DataFrame | None = None,
        adj: pd.DataFrame | None = None,
        trade_cal: pd.DataFrame | None = None,
    ) -> None:
        self._daily = daily
        self._basic = basic if basic is not None else pd.DataFrame()
        self._adj = adj if adj is not None else pd.DataFrame()
        self._trade_cal = trade_cal if trade_cal is not None else pd.DataFrame()

    def daily(self, *, ts_code: str = "", start_date: str = "", end_date: str = "") -> object:
        _ = (ts_code, start_date, end_date)
        return self._daily

    def daily_basic(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
        fields: str = "",
    ) -> object:
        _ = (ts_code, start_date, end_date, fields)
        return self._basic

    def adj_factor(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> object:
        _ = (ts_code, start_date, end_date)
        return self._adj

    def trade_cal(
        self,
        *,
        exchange: str = "",
        start_date: str = "",
        end_date: str = "",
        is_open: str = "",
    ) -> object:
        _ = (exchange, start_date, end_date, is_open)
        return self._trade_cal

    def stock_basic(
        self,
        *,
        ts_code: str = "",
        list_status: str = "",
        fields: str = "",
    ) -> object:
        _ = (ts_code, list_status, fields)
        return pd.DataFrame({"ts_code": [ts_code], "name": ["浦发银行"]})


@pytest.mark.parametrize(
    ("code6", "expected"),
    [
        ("920002", "920002.BJ"),
        ("920099", "920099.BJ"),
        ("920000", "920000.BJ"),
        ("900901", "900901.SH"),
        ("900957", "900957.SH"),
        ("600000", "600000.SH"),
        ("688001", "688001.SH"),
        ("510300", "510300.SH"),
        ("430047", "430047.BJ"),
        ("830799", "830799.BJ"),
        ("000001", "000001.SZ"),
        ("300750", "300750.SZ"),
        ("200002", "200002.SZ"),
    ],
)
def test_to_ts_code_mapping(code6: str, expected: str) -> None:
    assert _to_ts_code(code6) == expected


class _RecordingFinaPro:
    def __init__(self) -> None:
        self.fina_ts_codes: list[str] = []

    def fina_indicator(
        self,
        *,
        ts_code: str = "",
        start_date: str = "",
        end_date: str = "",
        fields: str = "",
    ) -> pd.DataFrame:
        self.fina_ts_codes.append(ts_code)
        return pd.DataFrame(
            {
                "ts_code": [ts_code],
                "ann_date": ["20250320"],
                "end_date": ["20241231"],
                "roe": [12.5],
                "debt_to_assets": [40.0],
                "update_flag": [0],
            }
        )


def test_fetch_fina_indicator_sends_920_bj_and_900_sh() -> None:
    pro = _RecordingFinaPro()
    provider = TushareProvider(token="dummy", pro_api=pro, retry_delay_sec=0.0)

    bj_frame = provider.fetch_fina_indicator(
        symbol="920002", start_date=date(2024, 1, 1), end_date=date(2026, 1, 1)
    )
    assert not bj_frame.empty
    assert pro.fina_ts_codes == ["920002.BJ"]
    assert "920002.SH" not in pro.fina_ts_codes

    provider.fetch_fina_indicator(
        symbol="900901", start_date=date(2024, 1, 1), end_date=date(2026, 1, 1)
    )
    assert pro.fina_ts_codes == ["920002.BJ", "900901.SH"]


def test_apply_price_adjust_qfq_scales_to_latest_factor() -> None:
    daily = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-06-01", "2026-06-02", "2026-07-14"]),
            "open": [10.0, 10.0, 10.0],
            "high": [10.0, 10.0, 10.0],
            "low": [10.0, 10.0, 10.0],
            "close": [10.0, 10.0, 10.0],
            "volume": [1000.0, 1000.0, 1000.0],
            "turnover": [1_000_000.0, 1_000_000.0, 1_000_000.0],
        }
    )
    adj = pd.DataFrame(
        {
            "trade_date": ["20260601", "20260602", "20260714"],
            "adj_factor": [10.0, 10.0, 20.0],
        }
    )
    out, meta = _apply_price_adjust(daily, adj=adj, price_series_mode="qfq")
    # qfq: scale = adj/latest => first rows *0.5, last row *1.0
    assert out["close"].iloc[0] == pytest.approx(5.0)
    assert out["close"].iloc[-1] == pytest.approx(10.0)
    # Volume stays actual shares (not reverse-scaled); turnover stays actual amount.
    assert out["volume"].iloc[0] == pytest.approx(1000.0)
    assert out["volume"].iloc[-1] == pytest.approx(1000.0)
    assert out["turnover"].iloc[0] == pytest.approx(1_000_000.0)
    assert meta["price_series_mode"] == "qfq"
    assert meta["adjustment_source"] == "tushare_adj_factor"
    assert meta["adjustment_anchor_factor"] == pytest.approx(20.0)


def test_tushare_provider_applies_qfq_and_marks_incomplete_background() -> None:
    daily = pd.DataFrame(
        {
            "ts_code": ["600000.SH", "600000.SH", "600000.SH"],
            "trade_date": ["20260710", "20260711", "20260714"],
            "open": [10.0, 10.1, 10.2],
            "high": [10.5, 10.6, 10.7],
            "low": [9.8, 9.9, 10.0],
            "close": [10.2, 10.3, 10.4],
            "vol": [1000.0, 1100.0, 1200.0],
            "amount": [1000.0, 1100.0, 1200.0],
        }
    )
    basic = pd.DataFrame(
        {
            "ts_code": ["600000.SH", "600000.SH", "600000.SH"],
            "trade_date": ["20260710", "20260711", "20260714"],
            "turnover_rate": [1.0, 1.0, 1.0],
            "circ_mv": [1_000_000.0, 1_000_000.0, 1_200_000.0],
        }
    )
    adj = pd.DataFrame(
        {
            "ts_code": ["600000.SH", "600000.SH", "600000.SH"],
            "trade_date": ["20260710", "20260711", "20260714"],
            "adj_factor": [16.0, 16.0, 16.0],
        }
    )
    provider = TushareProvider(
        token="dummy",
        pro_api=_FakePro(daily, basic, adj),
        price_series_mode="qfq",
    )
    bars = provider.fetch_daily_bars(symbol="600000", lookback_days=2)
    assert len(bars) == 2
    assert bars.index.name == "date"
    assert bars["volume"].iloc[-1] == pytest.approx(1200.0 * 100.0)
    assert bars["turnover"].iloc[-1] == pytest.approx(1200.0 * 1000.0)
    assert bars["float_market_cap"].iloc[-1] == pytest.approx(1_200_000.0 * 10000.0)
    assert bars["background_data_source"].iloc[-1] == "tushare_pro_qfq"
    assert bool(bars["background_data_complete"].iloc[-1]) is False
    assert bool(bars["financial_data_complete"].iloc[-1]) is False
    assert bars["financial_source"].iloc[-1] == "tushare_pending"
    assert bars["name"].iloc[-1] == "浦发银行"
    assert bars["board"].iloc[-1] == "main"


def test_tushare_provider_requires_adj_factor_for_qfq() -> None:
    daily = pd.DataFrame(
        {
            "ts_code": ["600000.SH"],
            "trade_date": ["20260714"],
            "open": [10.0],
            "high": [10.0],
            "low": [10.0],
            "close": [10.0],
            "vol": [1000.0],
            "amount": [1000.0],
        }
    )
    provider = TushareProvider(
        token="dummy",
        pro_api=_FakePro(daily, adj=pd.DataFrame()),
        price_series_mode="qfq",
    )
    with pytest.raises(DataSourceError, match="adj_factor"):
        provider.fetch_daily_bars(symbol="600000", lookback_days=5)


def test_tushare_provider_requires_token_without_injected_api() -> None:
    provider = TushareProvider(token="")
    provider._token = ""  # noqa: SLF001
    with pytest.raises(DataSourceError, match="token missing"):
        provider.fetch_daily_bars(symbol="600000", lookback_days=5)


def test_tushare_provider_uses_http_fallback_when_sdk_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Response:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            _ = exc_type, exc, tb

        def read(self) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

    calls: list[str] = []

    def _fake_import_module(name: str) -> object:
        if name == "tushare":
            raise ImportError("missing tushare sdk")
        raise AssertionError(f"unexpected import: {name}")

    def _fake_urlopen(request: object, timeout: float) -> _Response:
        _ = timeout
        request_data = request.data  # type: ignore[attr-defined]
        payload = json.loads(request_data.decode("utf-8"))
        api_name = str(payload.get("api_name", ""))
        calls.append(api_name)
        if api_name == "daily":
            fields = ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"]
            items = [
                ["600000.SH", "20260711", 10.0, 10.5, 9.8, 10.2, 1000.0, 1000.0],
                ["600000.SH", "20260714", 10.2, 10.7, 10.0, 10.4, 1200.0, 1200.0],
            ]
        elif api_name == "daily_basic":
            fields = ["ts_code", "trade_date", "turnover_rate", "circ_mv", "total_mv"]
            items = [
                ["600000.SH", "20260711", 1.0, 1_000_000.0, 2_000_000.0],
                ["600000.SH", "20260714", 1.1, 1_200_000.0, 2_200_000.0],
            ]
        elif api_name == "adj_factor":
            fields = ["ts_code", "trade_date", "adj_factor"]
            items = [
                ["600000.SH", "20260711", 16.0],
                ["600000.SH", "20260714", 16.0],
            ]
        elif api_name == "stock_basic":
            fields = ["ts_code", "name"]
            items = [["600000.SH", "浦发银行"]]
        else:
            fields = []
            items = []
        return _Response({"code": 0, "data": {"fields": fields, "items": items}})

    monkeypatch.setattr(tushare_provider_module.importlib, "import_module", _fake_import_module)
    monkeypatch.setattr(tushare_provider_module.urllib.request, "urlopen", _fake_urlopen)

    provider = TushareProvider(token="dummy", price_series_mode="qfq")
    bars = provider.fetch_daily_bars(symbol="600000", lookback_days=2)

    assert {"daily", "daily_basic", "adj_factor", "stock_basic"}.issubset(calls)
    assert bars["background_data_source"].iloc[-1] == "tushare_pro_qfq"
    assert bars.attrs["price_series_meta"]["adjustment_source"] == "tushare_adj_factor"
    assert bars["name"].iloc[-1] == "浦发银行"


def test_tushare_resolve_target_trade_date_skips_holiday() -> None:
    # 2026-10-01..10-07 National Day holiday style: only open before/after.
    trade_cal = pd.DataFrame(
        {
            "exchange": ["SSE"] * 4,
            "cal_date": ["20260930", "20261008", "20261009", "20261010"],
            "is_open": ["1", "1", "1", "1"],
        }
    )
    provider = TushareProvider(token="dummy", pro_api=_FakePro(pd.DataFrame(), trade_cal=trade_cal))
    # Friday 2026-10-02 during holiday, after close → last open 2026-09-30
    assert provider.resolve_target_trade_date(now=date(2026, 10, 2), after_close=True) == date(
        2026, 9, 30
    )
    # Open day after close → same day
    assert provider.resolve_target_trade_date(now=date(2026, 10, 9), after_close=True) == date(
        2026, 10, 9
    )
    # Open day before close → previous open
    assert provider.resolve_target_trade_date(now=date(2026, 10, 9), after_close=False) == date(
        2026, 10, 8
    )


def test_apply_price_adjust_preserves_volume_across_split_sample() -> None:
    """Ex-rights sample: OHLC re-anchored; volume/turnover remain actual."""
    daily = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-05-29", "2026-05-30", "2026-06-02"]),
            "open": [20.0, 20.0, 10.0],
            "high": [20.0, 20.0, 10.0],
            "low": [20.0, 20.0, 10.0],
            "close": [20.0, 20.0, 10.0],
            "volume": [1_000_000.0, 1_000_000.0, 2_000_000.0],
            "turnover": [20_000_000.0, 20_000_000.0, 20_000_000.0],
        }
    )
    adj = pd.DataFrame(
        {
            "trade_date": ["20260529", "20260530", "20260602"],
            "adj_factor": [1.0, 1.0, 2.0],
        }
    )
    out, meta = _apply_price_adjust(daily, adj=adj, price_series_mode="qfq")
    assert out["close"].iloc[0] == pytest.approx(10.0)
    assert out["close"].iloc[-1] == pytest.approx(10.0)
    assert out["volume"].tolist() == [1_000_000.0, 1_000_000.0, 2_000_000.0]
    assert out["turnover"].tolist() == [20_000_000.0, 20_000_000.0, 20_000_000.0]
    assert meta["adjustment_anchor_factor"] == pytest.approx(2.0)


def test_tushare_provider_nan_background_not_zero_filled() -> None:
    daily = pd.DataFrame(
        {
            "ts_code": ["600000.SH", "600000.SH"],
            "trade_date": ["20260711", "20260714"],
            "open": [10.0, 10.2],
            "high": [10.5, 10.7],
            "low": [9.8, 10.0],
            "close": [10.2, 10.4],
            "vol": [1000.0, 1200.0],
            "amount": [1000.0, 1200.0],
        }
    )
    adj = pd.DataFrame(
        {
            "ts_code": ["600000.SH", "600000.SH"],
            "trade_date": ["20260711", "20260714"],
            "adj_factor": [16.0, 16.0],
        }
    )
    provider = TushareProvider(
        token="dummy",
        pro_api=_FakePro(daily, adj=adj),
        price_series_mode="qfq",
    )
    bars = provider.fetch_daily_bars(symbol="600000", lookback_days=2)
    assert pd.isna(bars["holder_count"].iloc[-1])
    assert pd.isna(bars["financing_balance"].iloc[-1])
    assert pd.isna(bars["northbound_net"].iloc[-1])
    assert pd.isna(bars["block_trade_net"].iloc[-1])
    assert bool(bars["background_data_complete"].iloc[-1]) is False
    assert "holder_count" in str(bars["background_missing_fields"].iloc[-1])
    assert bars["price_series_mode"].iloc[-1] == "qfq"
    assert bars.attrs["price_series_meta"]["adjustment_source"] == "tushare_adj_factor"


def test_tushare_qfq_reanchor_when_latest_factor_changes() -> None:
    """Two windows with different latest adj_factor must re-scale history consistently."""
    daily_a = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-06-01", "2026-06-02"]),
            "open": [10.0, 10.0],
            "high": [10.0, 10.0],
            "low": [10.0, 10.0],
            "close": [10.0, 10.0],
            "volume": [1000.0, 1000.0],
            "turnover": [1_000_000.0, 1_000_000.0],
        }
    )
    adj_a = pd.DataFrame({"trade_date": ["20260601", "20260602"], "adj_factor": [10.0, 10.0]})
    out_a, meta_a = _apply_price_adjust(daily_a, adj=adj_a, price_series_mode="qfq")
    assert out_a["close"].iloc[0] == pytest.approx(10.0)
    assert meta_a["adjustment_anchor_factor"] == pytest.approx(10.0)

    daily_b = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-06-01", "2026-06-02", "2026-07-14"]),
            "open": [10.0, 10.0, 10.0],
            "high": [10.0, 10.0, 10.0],
            "low": [10.0, 10.0, 10.0],
            "close": [10.0, 10.0, 10.0],
            "volume": [1000.0, 1000.0, 1000.0],
            "turnover": [1_000_000.0, 1_000_000.0, 1_000_000.0],
        }
    )
    adj_b = pd.DataFrame(
        {
            "trade_date": ["20260601", "20260602", "20260714"],
            "adj_factor": [10.0, 10.0, 20.0],
        }
    )
    out_b, meta_b = _apply_price_adjust(daily_b, adj=adj_b, price_series_mode="qfq")
    # After reanchor to 20, historical close becomes 5 (no mixed-anchor seam).
    assert out_b["close"].iloc[0] == pytest.approx(5.0)
    assert out_b["close"].iloc[-1] == pytest.approx(10.0)
    assert meta_b["adjustment_anchor_factor"] == pytest.approx(20.0)
    assert out_b["volume"].iloc[0] == pytest.approx(1000.0)
