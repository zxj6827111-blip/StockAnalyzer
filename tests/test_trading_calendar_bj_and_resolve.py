"""BJ symbol and trading calendar resolve tests."""

from __future__ import annotations

from datetime import date

from stock_analyzer.data.trading_calendar import is_bj_symbol, resolve_required_intraday_date


def test_is_bj_symbol_920_cases() -> None:
    assert is_bj_symbol("920002") is True
    assert is_bj_symbol("920002.BJ") is True
    assert is_bj_symbol("430047") is True  # 4 prefix
    assert is_bj_symbol("830001") is True  # 8 prefix
    assert is_bj_symbol("600000") is False
    assert is_bj_symbol("000001") is False


def test_week5_is_bj_symbol_matches_trading_calendar() -> None:
    from stock_analyzer.runtime.services.week5_service import _is_bj_symbol as week5_is_bj

    for sym in ("920002", "920099", "430047", "830001", "600000", "000001", "300750"):
        assert week5_is_bj(sym) == is_bj_symbol(sym), f"mismatch for {sym}"


def test_resolve_via_wrapped_provider_graph() -> None:
    """Wrapped TushareProvider must be discoverable via _resolve_calendar_provider."""

    class _FakeTushare:
        def list_open_trade_dates(self, *, start_date: date, end_date: date, exchange: str = "SSE") -> list[date]:
            # Simulate holiday: 2026-05-01..05-05 closed, previous open is 2026-04-30
            return [date(2026, 4, 29), date(2026, 4, 30), date(2026, 5, 6)]

    class _Inner:
        def __init__(self, inner: object) -> None:
            self.inner = inner

    class _FakeService:
        def __init__(self, provider: object) -> None:
            self._provider = provider

        def _iter_market_data_provider_graph(self) -> list[object]:
            # BFS unwrapping inner
            pending: list[object] = [self._provider]
            out: list[object] = []
            seen: set[int] = set()
            while pending:
                item = pending.pop(0)
                if id(item) in seen:
                    continue
                seen.add(id(item))
                out.append(item)
                nested = getattr(item, "inner", None)
                if nested is not None:
                    pending.append(nested)
            return out

    tushare = _FakeTushare()
    wrapped = _Inner(tushare)
    service = _FakeService(wrapped)

    from stock_analyzer.runtime.services.week5_service import _resolve_calendar_provider

    cal = _resolve_calendar_provider(service)  # type: ignore[arg-type]
    assert cal is tushare
    # Resolve 2026-05-06 -> previous open 2026-04-30 (not 2026-05-05 weekday)
    result = resolve_required_intraday_date("2026-05-06", cal)
    assert result == date(2026, 4, 30)
    # Weekday fallback without provider gives 2026-05-05
    fallback = resolve_required_intraday_date("2026-05-06", None)
    assert fallback == date(2026, 5, 5)


def test_resolve_weekday_fallback_no_provider() -> None:
    # Monday 2026-08-17 -> previous open Friday 2026-08-14
    result = resolve_required_intraday_date("2026-08-17", None)
    assert result == date(2026, 8, 14)
    # Tuesday 2026-08-18 -> Monday
    result2 = resolve_required_intraday_date("2026-08-18", None)
    assert result2 == date(2026, 8, 17)
