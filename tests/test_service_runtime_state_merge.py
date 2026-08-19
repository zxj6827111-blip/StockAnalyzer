from __future__ import annotations

from types import SimpleNamespace

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


def test_merge_runtime_state_portfolio_prefers_newer_trade_timestamp() -> None:
    state_service = RuntimeStateService(object())

    existing = {
        "trade_seq": 3,
        "positions": [{"symbol": "600000", "updated_at": "2026-08-18T09:00:00"}],
        "trades": [{"timestamp": "2026-08-18T09:00:00"}],
    }
    current = {
        "trade_seq": 2,
        "positions": [{"symbol": "600000", "updated_at": "2026-08-18T10:00:00"}],
        "trades": [{"timestamp": "2026-08-18T10:00:00"}],
    }

    assert state_service._merge_runtime_state_portfolio(existing, current) == current


def test_stale_runtime_snapshot_preserves_newer_scalar_state() -> None:
    service = SimpleNamespace(
        _config=SimpleNamespace(
            acceptance=SimpleNamespace(history_limit=10),
            week5=SimpleNamespace(market_radar_review_pool_max_symbols=10),
            week6=SimpleNamespace(history_limit=10),
            market_warehouse=SimpleNamespace(history_limit=10),
            evolution=SimpleNamespace(history_limit=10),
            cloud_backup=SimpleNamespace(require_first_ping_before_alert=True),
        ),
        _reconcile_history=[],
        _week5_scan_history=[],
    )
    state_service = RuntimeStateService(service)
    for name in (
        "_merge_runtime_state_scheduler",
        "_merge_runtime_state_portfolio",
        "_merge_runtime_state_mapping",
        "_merge_runtime_state_latest",
        "_merge_runtime_state_history",
        "_runtime_state_latest_from_raw",
        "_runtime_state_optional_dict",
        "_runtime_state_dict_list",
        "_load_runtime_state_numeric_mapping",
    ):
        setattr(service, name, getattr(state_service, name))
    service._merge_runtime_state_watchlist = (
        lambda existing, current: list(current)
        if isinstance(current, list) and current
        else list(existing)
        if isinstance(existing, list)
        else []
    )

    existing = {
        "state_revision": 6,
        "current_equity": 1.2,
        "pause_new_buy": True,
        "reconcile_required": True,
        "watchlist": ["600000"],
        "scheduler_state": {
            "jobs": {
                "premarket_scan": {
                    "status": "success",
                    "heartbeat_at": "2026-08-19T08:40:00",
                }
            }
        },
        "portfolio": {
            "trade_seq": 3,
            "positions": [
                {"symbol": "600000", "updated_at": "2026-08-19T08:40:00"}
            ],
            "trades": [{"timestamp": "2026-08-19T08:40:00"}],
        },
    }
    stale_current = {
        "current_equity": 0.8,
        "pause_new_buy": False,
        "reconcile_required": False,
        "watchlist": ["000001"],
        "scheduler_state": {
            "jobs": {
                "evolution_offhours": {
                    "status": "success",
                    "heartbeat_at": "2026-08-19T09:00:00",
                }
            }
        },
        "portfolio": {
            "trade_seq": 2,
            "positions": [
                {"symbol": "000001", "updated_at": "2026-08-19T08:00:00"}
            ],
            "trades": [{"timestamp": "2026-08-19T08:00:00"}],
        },
    }

    merged = state_service._merge_runtime_state_payload(
        existing,
        stale_current,
        base_revision=5,
    )

    assert merged["current_equity"] == 1.2
    assert merged["pause_new_buy"] is True
    assert merged["reconcile_required"] is True
    assert merged["watchlist"] == ["600000"]
    assert merged["portfolio"] == existing["portfolio"]
    assert set(merged["scheduler_state"]["jobs"]) == {
        "premarket_scan",
        "evolution_offhours",
    }
