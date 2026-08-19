from __future__ import annotations

from stock_analyzer.runtime.service import StockAnalyzerService
from stock_analyzer.runtime.services.runtime_state_service import RuntimeStateService


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


def test_merge_runtime_state_scheduler_preserves_disjoint_job_updates() -> None:
    state_service = RuntimeStateService(object())

    merged = state_service._merge_runtime_state_scheduler(
        existing_raw={
            "last_run": {"premarket_scan": "2026-08-18"},
            "last_interval_slot": {},
            "jobs": {
                "premarket_scan": {
                    "status": "success",
                    "last_success": "2026-08-18T08:35:00",
                }
            },
        },
        current_raw={
            "last_run": {"evolution_offhours": "2026-08-18"},
            "last_interval_slot": {},
            "jobs": {
                "evolution_offhours": {
                    "status": "success",
                    "last_success": "2026-08-18T22:30:00",
                }
            },
        },
    )

    assert merged["last_run"] == {
        "premarket_scan": "2026-08-18",
        "evolution_offhours": "2026-08-18",
    }
    assert merged["jobs"] == {
        "premarket_scan": {
            "status": "success",
            "last_success": "2026-08-18T08:35:00",
        },
        "evolution_offhours": {
            "status": "success",
            "last_success": "2026-08-18T22:30:00",
        },
    }


def test_merge_runtime_state_scheduler_keeps_newer_job_record() -> None:
    state_service = RuntimeStateService(object())

    merged = state_service._merge_runtime_state_scheduler(
        existing_raw={
            "jobs": {
                "premarket_scan": {
                    "status": "success",
                    "heartbeat_at": "2026-08-18T08:40:00",
                }
            }
        },
        current_raw={
            "jobs": {
                "premarket_scan": {
                    "status": "running",
                    "heartbeat_at": "2026-08-18T08:30:00",
                }
            }
        },
    )

    assert merged["jobs"]["premarket_scan"]["status"] == "success"
