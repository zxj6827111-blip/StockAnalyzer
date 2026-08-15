"""P0 网络韧性：指数退避 jitter、熔断器、传输指标、SDK→HTTP 兜底、主备 host。

覆盖 PLAN「Tushare 网络韧性」：DNS/连接超时/TLS/HTTP 5xx 归为可重试故障，
最多 3 次指数退避 + jitter；SDK 传输失败后进入 HTTP fallback；主备 host 仅
用于传输故障切换（鉴权/额度/参数错误不切换）；指标不含 token。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pandas as pd
import pytest

from stock_analyzer.data.provider import DataSourceError
from stock_analyzer.data.tushare_provider import (
    TushareProvider,
    _HttpTushareProApi,
    _SdkWithHttpFallback,
)


def _provider(**kwargs: object) -> TushareProvider:
    defaults: dict[str, object] = {
        "token": "test-token",
        "pro_api": None,
        "retry_delay_sec": 0.0,
        "min_request_interval_sec": 0.0,
    }
    defaults.update(kwargs)
    return TushareProvider(**defaults)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://api.tushare.pro",
        code=code,
        msg="boom",
        hdrs={},
        fp=None,
    )


def test_backoff_uses_exponential_jitter_within_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """指数退避 0.35*2^(n-1)，±20% jitter，且不超绝对上限。"""
    sleeps: list[float] = []
    monkeypatch.setattr(
        "stock_analyzer.data.tushare_provider.sleep", lambda delay: sleeps.append(float(delay))
    )
    calls = {"n": 0}

    def _failing_fn() -> object:
        calls["n"] += 1
        raise TimeoutError("timed out")

    provider = _provider(max_attempts=4, retry_delay_sec=1.0, max_backoff_sec=8.0)
    with pytest.raises(TimeoutError):
        provider._call_with_retry(_failing_fn)
    assert calls["n"] == 4
    assert len(sleeps) == 3
    # 1.0 * 2^0 / 2^1 / 2^2 = 1 / 2 / 4，jitter ±20%
    expected = [1.0, 2.0, 4.0]
    for delay, base in zip(sleeps, expected, strict=True):
        assert base * 0.8 <= delay <= base * 1.2
    assert max(sleeps) <= 8.0


def test_backoff_cap_has_no_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """触顶（>= max_backoff_sec）的退避不加 jitter，保证绝对上限稳定。"""
    sleeps: list[float] = []
    monkeypatch.setattr(
        "stock_analyzer.data.tushare_provider.sleep", lambda delay: sleeps.append(float(delay))
    )

    def _failing_fn() -> object:
        raise TimeoutError("timed out")

    provider = _provider(max_attempts=4, retry_delay_sec=100.0, max_backoff_sec=32.0)
    with pytest.raises(TimeoutError):
        provider._call_with_retry(_failing_fn)
    assert sleeps == [32.0, 32.0, 32.0]


def test_circuit_breaker_opens_and_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    """连续传输失败达到阈值后熔断；窗口内直接拒绝且不再调用底层。"""
    monkeypatch.setattr(
        "stock_analyzer.data.tushare_provider.sleep", lambda delay: None
    )
    calls = {"n": 0}

    def _failing_fn() -> object:
        calls["n"] += 1
        raise TimeoutError("timed out")

    provider = _provider(
        max_attempts=1,
        circuit_breaker_threshold=3,
        circuit_breaker_open_sec=30.0,
    )
    for _ in range(3):
        with pytest.raises(TimeoutError):
            provider._call_with_retry(_failing_fn)
    assert calls["n"] == 3
    assert provider.network_metrics()["circuit"]["state"] == "open"

    # 熔断打开后：直接 DataSourceError，底层不再调用
    with pytest.raises(DataSourceError, match="circuit breaker open"):
        provider._call_with_retry(_failing_fn)
    assert calls["n"] == 3
    assert provider.network_metrics()["circuit_rejected"] == 1


def test_circuit_breaker_closes_after_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "stock_analyzer.data.tushare_provider.sleep", lambda delay: None
    )
    calls = {"n": 0}

    def _failing_fn() -> object:
        calls["n"] += 1
        raise TimeoutError("timed out")

    provider = _provider(
        max_attempts=1,
        circuit_breaker_threshold=2,
        circuit_breaker_open_sec=0.0,  # 窗口立即到期 → 自动闭合
    )
    with pytest.raises(TimeoutError):
        provider._call_with_retry(_failing_fn)
    with pytest.raises(TimeoutError):
        provider._call_with_retry(_failing_fn)
    assert provider.network_metrics()["circuit"]["state"] == "closed"
    with pytest.raises(TimeoutError):
        provider._call_with_retry(_failing_fn)
    assert calls["n"] == 3  # 第三次未被熔断拒绝


def test_business_errors_do_not_trip_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "stock_analyzer.data.tushare_provider.sleep", lambda delay: None
    )
    provider = _provider(max_attempts=1, circuit_breaker_threshold=2)

    for _ in range(5):
        with pytest.raises(DataSourceError):
            provider._call_with_retry(lambda: (_ for _ in ()).throw(
                DataSourceError("code=40003 权限不足")
            ))
    assert provider.network_metrics()["circuit"]["state"] == "closed"
    assert provider.network_metrics()["circuit"]["consecutive_failures"] == 0


def test_metrics_report_attempts_failures_and_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "stock_analyzer.data.tushare_provider.sleep", lambda delay: None
    )
    calls = {"n": 0}

    def _flaky_fn() -> object:
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("dns timeout")
        return {"ok": True}

    provider = _provider(max_attempts=3, retry_delay_sec=0.1)
    assert provider._call_with_retry(_flaky_fn) == {"ok": True}
    metrics = provider.network_metrics()
    assert metrics["total_attempts"] == 3
    assert metrics["total_failures"] == 2
    assert metrics["retryable_failures"] == 2
    assert "dns timeout" in str(metrics["last_transport_error"])
    assert metrics["last_recovery_ms"] > 0
    assert metrics["circuit"]["state"] == "closed"
    # 指标不得泄露 token
    assert "test-token" not in json.dumps(metrics)


def test_sdk_transport_failure_falls_back_to_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK 传输失败 → 同接口经 HTTP 兜底；非传输错误不兜底。"""
    import requests

    class _Sdk:
        def daily(self, **kwargs: object) -> object:
            raise requests.exceptions.ConnectionError("sdk network down")

        def stock_basic(self, **kwargs: object) -> object:
            raise DataSourceError("code=40003 权限不足")

    calls: list[dict[str, object]] = []

    class _Http:
        def daily(self, **kwargs: object) -> object:
            calls.append(dict(kwargs))
            return pd.DataFrame({"ts_code": ["600000.SH"]})

        def stock_basic(self, **kwargs: object) -> object:
            raise AssertionError("should not fall back on business error")

    composite = _SdkWithHttpFallback(sdk=_Sdk(), http=_Http())  # type: ignore[arg-type]
    assert len(composite.daily(ts_code="600000.SH")) == 1
    assert composite.fallback_calls == 1
    assert calls[0]["ts_code"] == "600000.SH"

    with pytest.raises(DataSourceError, match="40003"):
        composite.stock_basic()
    assert composite.fallback_calls == 1  # 非传输错误不触发兜底


def test_http_fallback_switches_host_on_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主 host 传输故障切备 host；鉴权错误不切。"""
    attempted: list[str] = []

    class _FakeResponse:
        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self) -> bytes:
            payload = {"code": 0, "data": {"fields": ["ts_code"], "items": [["600000.SH"]]}}
            return json.dumps(payload).encode("utf-8")

    def _fake_urlopen(request: urllib.request.Request, timeout: float) -> object:
        attempted.append(request.full_url)
        raise urllib.error.URLError("primary host unreachable")

    monkeypatch.setattr(
        "stock_analyzer.data.tushare_provider.urllib.request.urlopen", _fake_urlopen
    )
    api = _HttpTushareProApi(token="t", timeout_sec=5.0)
    # 主备 host 都失败 → 最终抛出原始 URLError
    with pytest.raises(urllib.error.URLError):
        api._call("daily", ts_code="600000.SH")
    assert attempted == list(api._API_URLS)
    assert api.host_switches == 1


def test_http_fallback_switches_host_on_5xx_not_on_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(request: urllib.request.Request, timeout: float) -> object:
        raise _http_error(503)

    monkeypatch.setattr("stock_analyzer.data.tushare_provider.urllib.request.urlopen", _boom)
    api = _HttpTushareProApi(token="t", timeout_sec=5.0)
    with pytest.raises(urllib.error.HTTPError):
        api._call("daily")
    assert api.host_switches == 1

    # 401 不切 host：直接抛，host_switches 保持 0
    monkeypatch.setattr(
        "stock_analyzer.data.tushare_provider.urllib.request.urlopen",
        lambda request, timeout: (_ for _ in ()).throw(_http_error(401)),
    )
    api2 = _HttpTushareProApi(token="t", timeout_sec=5.0)
    with pytest.raises(urllib.error.HTTPError):
        api2._call("daily")
    assert api2.host_switches == 0


def test_http_fallback_recovers_on_secondary_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeResponse:
        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self) -> bytes:
            payload = {"code": 0, "data": {"fields": ["ts_code"], "items": [["600000.SH"]]}}
            return json.dumps(payload).encode("utf-8")

    def _flaky_urlopen(request: urllib.request.Request, timeout: float) -> object:
        if request.full_url.startswith("https://"):
            raise urllib.error.URLError("tls handshake failed")
        return _FakeResponse()

    monkeypatch.setattr(
        "stock_analyzer.data.tushare_provider.urllib.request.urlopen", _flaky_urlopen
    )
    api = _HttpTushareProApi(token="t", timeout_sec=5.0)
    frame = api._call("daily", ts_code="600000.SH")
    assert list(frame["ts_code"]) == ["600000.SH"]
    assert api.host == "http://api.tushare.pro"
    assert api.host_switches == 1
