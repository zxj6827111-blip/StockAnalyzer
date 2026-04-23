from __future__ import annotations

from stock_analyzer.runtime.service import StockAnalyzerService


def test_merge_runtime_state_watchlist_prefers_current_when_present() -> None:
    service = object.__new__(StockAnalyzerService)

    merged = service._merge_runtime_state_watchlist(
        existing_raw=["000001", "600000"],
        current_raw=["300059", "601231"],
    )

    assert merged == ["300059", "601231"]


def test_merge_runtime_state_watchlist_uses_existing_when_current_empty() -> None:
    service = object.__new__(StockAnalyzerService)

    merged = service._merge_runtime_state_watchlist(
        existing_raw=["000001", "600000"],
        current_raw=[],
    )

    assert merged == ["000001", "600000"]
