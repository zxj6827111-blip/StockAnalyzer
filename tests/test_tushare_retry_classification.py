"""P1-3 regression tests: HTTP 4xx is never transient, network errors are.

``HTTPError`` is a subclass of ``URLError`` so classification must check it
first. Client errors (400/401/403/404/…), Tushare business errors wrapped in
``DataSourceError`` are non-transient; 408/425/429/5xx and plain network
errors (DNS, timeout incl. the historical ``socket.timeout`` alias, connect
reset/refused) get a bounded retry; backoff is ``retry_delay_sec * attempt``
capped at an absolute ``max_backoff_sec`` (default 32 s) - never "32x base".
"""

from __future__ import annotations

import socket
import urllib.error

import pytest

from stock_analyzer.data.provider import DataSourceError
from stock_analyzer.data.tushare_provider import TushareProvider


def _provider(**kwargs: object) -> TushareProvider:
    return TushareProvider(token="test-token", pro_api=None, **kwargs)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://api.tushare.pro",
        code=code,
        msg="boom",
        hdrs={},
        fp=None,
    )


@pytest.mark.parametrize(
    "code",
    [400, 401, 403, 404, 405, 406, 409, 410, 413, 422, 418, 451],
)
def test_http_4xx_client_errors_are_non_transient(code: int) -> None:
    provider = _provider()
    assert provider._is_retryable_error(_http_error(code)) is False


@pytest.mark.parametrize("code", [408, 425, 429])
def test_http_throttling_codes_are_transient(code: int) -> None:
    provider = _provider()
    assert provider._is_retryable_error(_http_error(code)) is True


@pytest.mark.parametrize("code", [500, 502, 503, 504, 507])
def test_http_5xx_server_errors_are_transient(code: int) -> None:
    provider = _provider()
    assert provider._is_retryable_error(_http_error(code)) is True


def test_plain_urlerror_is_transient() -> None:
    provider = _provider()
    assert provider._is_retryable_error(urllib.error.URLError(reason="getaddrinfo failed")) is True


def test_timeout_errors_are_transient() -> None:
    provider = _provider()
    assert provider._is_retryable_error(TimeoutError("timed out")) is True
    # socket.timeout is an alias of TimeoutError; asserting on TimeoutError
    # keeps Ruff UP041 clean.
    assert socket.timeout is TimeoutError


def test_connection_reset_and_refused_are_transient() -> None:
    provider = _provider()
    assert provider._is_retryable_error(ConnectionResetError("reset")) is True
    assert provider._is_retryable_error(ConnectionRefusedError("refused")) is True


def test_tushare_business_error_is_non_transient() -> None:
    provider = _provider()
    business = DataSourceError("tushare daily failed: code=40003 msg=抱歉，您没有访问该接口的权限")
    assert provider._is_retryable_error(business) is False


def test_generic_os_errors_are_non_transient() -> None:
    provider = _provider()
    assert provider._is_retryable_error(FileNotFoundError("no such file")) is False
    assert provider._is_retryable_error(PermissionError("denied")) is False
    assert provider._is_retryable_error(OSError("invalid argument")) is False
    assert provider._is_retryable_error(BlockingIOError("busy")) is False


def test_explicit_network_os_errors_are_transient() -> None:
    provider = _provider()
    assert provider._is_retryable_error(socket.gaierror("name or service not known")) is True
    assert provider._is_retryable_error(socket.herror("host not found")) is True
    assert provider._is_retryable_error(BrokenPipeError("broken pipe")) is True
    assert provider._is_retryable_error(ConnectionAbortedError("aborted")) is True


def _requests_response(status_code: int) -> object:
    import requests

    response = requests.Response()
    response.status_code = status_code
    return response


def test_requests_sdk_timeout_and_connection_errors_are_transient() -> None:
    import requests

    provider = _provider()
    assert provider._is_retryable_error(requests.exceptions.Timeout("timed out")) is True
    assert provider._is_retryable_error(requests.exceptions.ConnectionError("refused")) is True
    assert (
        provider._is_retryable_error(requests.exceptions.ConnectTimeout("connect timeout")) is True
    )


@pytest.mark.parametrize(
    "status_code",
    [400, 401, 403, 404, 409, 422],
)
def test_requests_sdk_http_4xx_are_non_transient(status_code: int) -> None:
    import requests

    provider = _provider()
    error = requests.exceptions.HTTPError(
        f"{status_code} Client Error", response=_requests_response(status_code)
    )
    assert provider._is_retryable_error(error) is False


@pytest.mark.parametrize("status_code", [408, 425, 429, 500, 502, 503])
def test_requests_sdk_http_throttling_and_5xx_are_transient(status_code: int) -> None:
    import requests

    provider = _provider()
    error = requests.exceptions.HTTPError(
        f"{status_code} Server Error", response=_requests_response(status_code)
    )
    assert provider._is_retryable_error(error) is True


def test_requests_sdk_other_request_errors_are_non_transient() -> None:
    import requests

    provider = _provider()
    assert (
        provider._is_retryable_error(requests.exceptions.JSONDecodeError("bad json", "doc", 1))
        is False
    )
    assert provider._is_retryable_error(requests.exceptions.RequestException("generic")) is False


def test_requests_sdk_http_error_without_response_is_non_transient() -> None:
    import requests

    provider = _provider()
    error = requests.exceptions.HTTPError("no response attached")
    assert provider._is_retryable_error(error) is False


def test_call_with_retry_never_retries_business_errors() -> None:
    calls = {"n": 0}

    def _failing_fn() -> object:
        calls["n"] += 1
        raise DataSourceError("tushare failed: code=40003 msg=权限不足")

    provider = _provider(max_attempts=5, retry_delay_sec=0.0)
    with pytest.raises(DataSourceError):
        provider._call_with_retry(_failing_fn)
    assert calls["n"] == 1


def test_call_with_retry_never_retries_http_401() -> None:
    calls = {"n": 0}

    def _failing_fn() -> object:
        calls["n"] += 1
        raise _http_error(401)

    provider = _provider(max_attempts=5, retry_delay_sec=0.0)
    with pytest.raises(urllib.error.HTTPError):
        provider._call_with_retry(_failing_fn)
    assert calls["n"] == 1


@pytest.mark.parametrize("code", [429, 503])
def test_call_with_retry_retries_transient_http_up_to_max_attempts(
    code: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def _failing_fn() -> object:
        calls["n"] += 1
        raise _http_error(code)

    monkeypatch.setattr("stock_analyzer.data.tushare_provider.sleep", lambda _: None)
    provider = _provider(max_attempts=3, retry_delay_sec=0.0)
    with pytest.raises(urllib.error.HTTPError):
        provider._call_with_retry(_failing_fn)
    assert calls["n"] == 3


def test_call_with_retry_retries_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def _failing_fn() -> object:
        calls["n"] += 1
        raise TimeoutError("timed out")

    monkeypatch.setattr("stock_analyzer.data.tushare_provider.sleep", lambda _: None)
    provider = _provider(max_attempts=4, retry_delay_sec=0.0)
    with pytest.raises(TimeoutError):
        provider._call_with_retry(_failing_fn)
    assert calls["n"] == 4


def test_backoff_is_capped_at_absolute_max_backoff_sec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    def _capture_sleep(delay: float) -> None:
        sleeps.append(float(delay))

    calls = {"n": 0}

    def _failing_fn() -> object:
        calls["n"] += 1
        raise TimeoutError("timed out")

    monkeypatch.setattr("stock_analyzer.data.tushare_provider.sleep", _capture_sleep)
    provider = _provider(
        max_attempts=4,
        retry_delay_sec=100.0,
        max_backoff_sec=32.0,
        min_request_interval_sec=0.0,
    )
    with pytest.raises(TimeoutError):
        provider._call_with_retry(_failing_fn)
    assert calls["n"] == 4
    assert len(sleeps) == 3
    # absolute cap of 32 s, not "32x base"
    assert all(delay <= 32.0 for delay in sleeps)
    assert max(sleeps) == 32.0
