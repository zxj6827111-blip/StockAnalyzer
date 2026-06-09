from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient

from stock_analyzer import main as main_module
from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.runtime.service import StockAnalyzerService
from stock_analyzer.runtime.services.runtime_ops_service import _resolve_runtime_phase


def _patch_attr(target: object, name: str, value: object) -> None:
    setattr(cast(Any, target), name, value)


def _load_test_config() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.notifications.primary = "console"
    config.notifications.backup = "console"
    config.scheduler.close_reconcile_time = "15:10"
    config.scheduler.week4_acceptance_time = "20:35"
    config.market_warehouse.enabled = True
    config.market_warehouse.auto_run = True
    config.market_warehouse.run_time = "18:20"
    config.tdx_sync.enabled = False
    config.tdx_sync.auto_run = False
    config.tdx_sync.vipdoc_root = ""
    config.week6.enabled = True
    config.week6.auto_run = True
    config.week6.run_time = "15:25"
    config.week6.data_prewarm_enabled = True
    config.week6.data_prewarm_time = "15:20"
    config.evolution.enabled = True
    config.evolution.auto_run = True
    config.evolution.offhours_time = "20:30"
    config.acceptance.enabled = True
    config.acceptance.auto_run = True
    config.idle_queue.enabled = False
    config.idle_queue.auto_run = False
    config.cloud_backup.enabled = False
    config.week5.auto_run = False
    config.week5.auto_notify = False
    config.week6.auto_notify = False
    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = False
    config.training.bootstrap_auto_seed_watchlist = False
    config.training.bootstrap_retry_enabled = False
    temp_root = Path(tempfile.gettempdir()) / "stock_analyzer_tests"
    offline_root = temp_root / "missing_offline_package_runtime_stage"
    offline_root.mkdir(parents=True, exist_ok=True)
    config.data_source.local_data_root = str(offline_root)
    config.training.artifact_path = str(temp_root / "runtime_stage_model.json")
    config.training.bootstrap_state_path = str(temp_root / "runtime_stage_bootstrap.json")
    return config


def _new_service() -> StockAnalyzerService:
    service = StockAnalyzerService(config=_load_test_config())
    provider = SyntheticProvider(seed_offset=2042)
    _patch_attr(service, "_provider", provider)
    _patch_attr(service._pipeline, "_provider", provider)
    _patch_attr(service, "_realtime_provider", provider)
    if service._realtime_pipeline is not None:
        _patch_attr(service._realtime_pipeline, "_provider", provider)
    runtime_root = Path(tempfile.gettempdir()) / "stock_analyzer_tests" / "runtime_stage_views"
    runtime_root.mkdir(parents=True, exist_ok=True)
    _patch_attr(service, "_tdx_sync_history_path", runtime_root / "tdx_sync_history.jsonl")
    _patch_attr(
        service,
        "_market_warehouse_history_path",
        runtime_root / "market_warehouse_history.jsonl",
    )
    _patch_attr(
        service,
        "_market_warehouse_progress_path",
        runtime_root / "market_warehouse_progress.json",
    )
    _patch_attr(service, "_tdx_sync_history", [])
    _patch_attr(service, "_last_tdx_sync_report", None)
    _patch_attr(service, "_market_warehouse_history", [])
    _patch_attr(service, "_last_market_warehouse_report", None)
    _patch_attr(service, "_last_market_warehouse_progress", None)
    _patch_attr(service, "_record_audit_event", lambda *args, **kwargs: None)
    _patch_attr(service, "_refresh_runtime_state_from_disk_if_changed", lambda: None)
    _patch_attr(service, "_load_tdx_sync_history_from_disk", lambda: None)
    _patch_attr(service, "_load_market_warehouse_history_from_disk", lambda: None)
    _patch_attr(service, "_load_market_warehouse_progress_from_disk", lambda: None)
    return service


def _seed_runtime_stage(service: StockAnalyzerService) -> None:
    _patch_attr(
        service,
        "_last_reconcile_report",
        {
            "timestamp": "2026-03-16T15:10:00",
            "status": "ok",
        },
    )
    _patch_attr(
        service,
        "_last_week6_data_quality_report",
        {
            "timestamp": "2026-03-14T16:07:03",
            "status": "healthy",
            "overall_coverage_ratio": 0.96,
        },
    )
    _patch_attr(
        service,
        "_last_week6_report",
        {
            "timestamp": "2026-03-16T15:25:00",
            "status": "ok",
            "data_quality": {
                "timestamp": "2026-03-16T15:20:00",
                "status": "healthy",
                "overall_coverage_ratio": 1.0,
            },
        },
    )
    _patch_attr(
        service,
        "_last_market_warehouse_progress",
        {
            "timestamp": "2026-03-16T18:20:09",
            "trace_id": "scheduler-market-warehouse",
            "updated_at": "2026-03-16T19:04:34",
            "status": "running",
            "phase": "syncing",
            "current_symbol": "600000.SH",
            "symbols_completed": 2600,
            "symbols_total": 5194,
            "progress_ratio": 0.5006,
        },
    )
    _patch_attr(
        service,
        "_last_market_warehouse_report",
        {
            "timestamp": "2026-03-15T18:50:00",
            "trace_id": "scheduler-market-warehouse",
            "status": "ok",
        },
    )
    _patch_attr(service, "_last_tdx_sync_report", {})
    _patch_attr(service, "_last_evolution_report", {})
    _patch_attr(service, "_last_week5_scan_report", {})
    _patch_attr(service, "_last_week4_acceptance_report", {})
    _patch_attr(service, "_last_idle_report", {})


def _reset_service(service: StockAnalyzerService) -> None:
    _seed_runtime_stage(service)
    service._audit_events.clear()


_SHARED_SERVICE = _new_service()
_seed_runtime_stage(_SHARED_SERVICE)
_SHARED_CLIENT = TestClient(main_module.app)


@contextmanager
def _patched_service(
    service: StockAnalyzerService = _SHARED_SERVICE,
) -> Iterator[TestClient]:
    original_service = main_module._service
    _reset_service(service)
    main_module._service = service
    try:
        yield _SHARED_CLIENT
    finally:
        main_module._service = original_service


def test_runtime_stage_endpoint_returns_current_stage_snapshot() -> None:
    service = _new_service()
    _patch_attr(
        service,
        "provider_status",
        lambda: {
            "degraded_mode": False,
            "health": {"degraded_mode": False},
            "realtime_monitoring": {
                "degraded_mode": False,
                "health": {"degraded_mode": False},
            },
        },
    )

    with _patched_service(service) as client:
        response = client.get("/runtime/stage/deep", params={"now": "2026-03-16T19:06:47"})
        assert response.status_code == 200
        payload = response.json()

    assert payload["runtime_phase"]["code"] == "post_close"
    assert payload["system_stage"]["code"] == "market_warehouse_sync"
    assert payload["system_stage"]["label"] == "盘后基础库同步中"
    assert payload["health"]["code"] == "healthy"
    assert payload["summary"]["mode"] in {"simulation", "staging", "production"}

    tasks = {item["name"]: item for item in payload["tasks"]}
    assert tasks["market_warehouse_sync"]["status"] == "running"
    assert tasks["tdx_offline_sync"]["status"] == "disabled"
    assert tasks["week6_daily"]["status"] == "done"
    assert tasks["week6_data_prewarm"]["status"] == "done"
    assert payload["market_warehouse_progress"]["symbols_completed"] == 2600
    assert payload["latest_activity"]["label"] == "基础库增量同步"


def test_runtime_stage_endpoint_defaults_to_lightweight_snapshot() -> None:
    service = _new_service()

    def _fail_slow_dependency(*args: object, **kwargs: object) -> object:
        raise AssertionError("/runtime/stage must not call deep runtime dependencies")

    _patch_attr(service, "provider_status", _fail_slow_dependency)
    _patch_attr(service, "market_warehouse_history", _fail_slow_dependency)
    _patch_attr(service, "market_warehouse_sync_lock_status", _fail_slow_dependency)
    _patch_attr(service, "market_warehouse_background_data_status", _fail_slow_dependency)
    _patch_attr(service, "latest_post_market_warehouse_followup_state", _fail_slow_dependency)
    _patch_attr(service, "latest_post_market_warehouse_followup_result", _fail_slow_dependency)
    _patch_attr(service, "cloud_backup_status", _fail_slow_dependency)

    with _patched_service(service) as client:
        response = client.get("/runtime/stage", params={"now": "2026-03-16T19:06:47"})
        assert response.status_code == 200
        payload = response.json()

    assert payload["summary"]["stage_type"] == "lightweight"
    assert payload["health"]["code"] == "healthy"
    assert payload["market_warehouse_background_data"]["status"] == "skipped"
    assert payload["market_warehouse_lock"]["running"] is False


def test_runtime_stage_endpoint_exposes_market_warehouse_lock_and_background_status() -> None:
    service = _new_service()
    _patch_attr(
        service,
        "provider_status",
        lambda: {
            "degraded_mode": False,
            "health": {"degraded_mode": False},
            "realtime_monitoring": {
                "degraded_mode": False,
                "health": {"degraded_mode": False},
            },
        },
    )
    _patch_attr(
        service,
        "market_warehouse_sync_lock_status",
        lambda: {
            "exists": True,
            "running": True,
            "is_stale": False,
            "trace_id": "manual-full-universe-sync",
            "age_sec": 12.0,
            "stale_after_sec": 120,
            "last_heartbeat_at": "2026-03-16T19:06:35",
            "lock_path": "artifacts/runtime/market_warehouse_sync.lock",
        },
    )
    _patch_attr(
        service,
        "market_warehouse_background_data_status",
        lambda: {
            "status": "partial",
            "latest_trade_date": "2026-03-16",
            "symbols_total": 5194,
            "symbols_on_latest_trade_date": 5000,
            "symbols_stale": 194,
        },
    )

    with _patched_service(service) as client:
        response = client.get("/runtime/stage/deep", params={"now": "2026-03-16T19:06:47"})
        assert response.status_code == 200
        payload = response.json()

    assert payload["market_warehouse_lock"]["running"] is True
    assert payload["market_warehouse_lock"]["trace_id"] == "manual-full-universe-sync"
    assert payload["market_warehouse_background_data"]["status"] == "partial"
    assert payload["market_warehouse_background_data"]["symbols_stale"] == 194
    assert payload["market_warehouse_progress"]["lock"]["trace_id"] == "manual-full-universe-sync"


def test_runtime_stage_page_redirects_to_new_ui() -> None:
    client = TestClient(main_module.app)
    response = client.get("/dashboard/stage", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers.get("location") == "/ui/runtime-stage"


def test_runtime_phase_supports_weekend_windows() -> None:
    saturday_day = _resolve_runtime_phase(datetime.fromisoformat("2026-03-14T10:00:00"))
    sunday_night = _resolve_runtime_phase(datetime.fromisoformat("2026-03-15T21:00:00"))

    assert saturday_day["code"] == "weekend_day"
    assert saturday_day["label"] == "周末"
    assert sunday_night["code"] == "weekend_night"
    assert sunday_night["label"] == "周末夜间"


def test_runtime_stage_endpoint_skips_weekday_jobs_on_weekend() -> None:
    service = _new_service()

    with _patched_service(service) as client:
        _patch_attr(service, "_last_market_warehouse_progress", None)
        response = client.get("/runtime/stage/deep", params={"now": "2026-03-14T10:00:00"})
        assert response.status_code == 200
        payload = response.json()

    tasks = {item["name"]: item for item in payload["tasks"]}
    assert payload["runtime_phase"]["code"] == "weekend_day"
    assert payload["summary"]["pending_next"]["name"] == "evolution_offhours"
    assert tasks["close_reconcile"]["status"] == "skipped"
    assert tasks["market_warehouse_sync"]["status"] == "skipped"
    assert tasks["week6_daily"]["status"] == "skipped"


def test_runtime_stage_endpoint_reports_exact_week4_disabled_reason() -> None:
    service = _new_service()
    service._config.acceptance.auto_run = False

    with _patched_service(service) as client:
        response = client.get("/runtime/stage/deep", params={"now": "2026-03-16T20:20:00"})
        assert response.status_code == 200
        payload = response.json()

    tasks = {item["name"]: item for item in payload["tasks"]}
    assert tasks["week4_acceptance"]["status"] == "disabled"
    assert tasks["week4_acceptance"]["detail"] == "acceptance.auto_run=false"


def test_runtime_stage_endpoint_ignores_evolution_prefetch_for_nightly_market_warehouse() -> None:
    service = _new_service()
    service._config.market_warehouse.run_time = "21:45"
    _patch_attr(
        service,
        "provider_status",
        lambda: {
            "degraded_mode": False,
            "health": {"degraded_mode": False},
            "realtime_monitoring": {
                "degraded_mode": False,
                "health": {"degraded_mode": False},
            },
        },
    )
    nightly_report = {
        "timestamp": "2026-03-17T18:20:22.788585",
        "trace_id": "scheduler-market-warehouse",
        "status": "ok",
    }
    evolution_prefetch_report = {
        "timestamp": "2026-03-18T21:06:23.288995",
        "trace_id": "scheduler-evolution",
        "status": "ok",
    }

    with _patched_service(service) as client:
        _patch_attr(
            service,
            "_last_market_warehouse_report",
            evolution_prefetch_report,
        )
        _patch_attr(
            service,
            "_market_warehouse_history",
            [nightly_report, evolution_prefetch_report],
        )
        _patch_attr(
            service,
            "_last_market_warehouse_progress",
            {
                "timestamp": "2026-03-18T21:06:23.288995",
                "trace_id": "scheduler-evolution",
                "updated_at": "2026-03-18T21:07:36.225083",
                "status": "ok",
                "phase": "completed",
                "symbols_completed": 50,
                "symbols_total": 50,
            },
        )
        _patch_attr(
            service,
            "_last_evolution_report",
            {
                "timestamp": "2026-03-18T21:06:23.288995",
                "status": "ok",
            },
        )
        _patch_attr(
            service,
            "_last_week4_acceptance_report",
            {
                "timestamp": "2026-03-18T21:15:00.324047",
                "overall": "pass",
                "acceptance_summary": {"overall": "pass"},
                "runtime_sla": {"status": "pass"},
            },
        )
        response = client.get("/runtime/stage/deep", params={"now": "2026-03-18T21:15:28"})
        assert response.status_code == 200
        payload = response.json()

    tasks = {item["name"]: item for item in payload["tasks"]}
    assert tasks["market_warehouse_sync"]["status"] == "pending"
    assert tasks["market_warehouse_sync"]["report_timestamp"] == nightly_report["timestamp"]
    assert payload["summary"]["pending_next"]["name"] == "market_warehouse_sync"
    assert payload["market_warehouse_progress"] is None
    assert payload["latest_activity"]["timestamp"] == "2026-03-18T21:15:00.324047"


def test_runtime_stage_endpoint_surfaces_degraded_health_summary() -> None:
    service = _new_service()
    _patch_attr(
        service,
        "provider_status",
        lambda: {
            "degraded_mode": True,
            "health": {"degraded_mode": True},
        },
    )
    with _patched_service(service) as client:
        service.state.pause_new_buy = True
        _patch_attr(
            service,
            "_last_week5_scan_report",
            {
                "timestamp": "2026-03-16T10:00:00",
                "empty_signal": {"triggered": True},
                "watchlist_sync": {"reason": "intraday_preserve_existing"},
            },
        )
        _patch_attr(
            service,
            "_last_week4_acceptance_report",
            {
                "timestamp": "2026-03-16T20:35:00",
                "overall": "fail",
                "acceptance_summary": {"overall": "fail"},
                "runtime_sla": {"status": "fail"},
            },
        )
        response = client.get("/runtime/stage/deep", params={"now": "2026-03-16T20:40:00"})
        assert response.status_code == 200
        payload = response.json()

    health = payload["health"]
    assert health["code"] == "degraded"
    assert health["provider_degraded"] is True
    assert health["risk_degraded"] is False
    assert health["pause_new_buy"] is True
    assert health["week5_intraday_preserved"] is True
    assert health["week5_empty_signal_triggered"] is True
    assert health["acceptance_failed"] is True
    assert health["acceptance_gate_failed"] is True
    assert health["acceptance_runtime_sla_failed"] is True


def test_runtime_stage_endpoint_treats_week4_gate_failure_as_warn_when_sla_is_healthy() -> None:
    service = _new_service()
    _patch_attr(
        service,
        "provider_status",
        lambda: {
            "degraded_mode": False,
            "health": {"degraded_mode": False},
            "realtime_monitoring": {
                "degraded_mode": False,
                "health": {"degraded_mode": False},
            },
        },
    )

    with _patched_service(service) as client:
        _patch_attr(
            service,
            "_last_week4_acceptance_report",
            {
                "timestamp": "2026-03-16T20:35:00",
                "overall": "fail",
                "acceptance_summary": {"overall": "fail"},
                "runtime_sla": {"status": "pass"},
            },
        )
        response = client.get("/runtime/stage/deep", params={"now": "2026-03-16T20:40:00"})
        assert response.status_code == 200
        payload = response.json()

    health = payload["health"]
    assert health["code"] == "warn"
    assert health["acceptance_failed"] is True
    assert health["acceptance_gate_failed"] is True
    assert health["acceptance_runtime_sla_failed"] is False


def test_runtime_stage_endpoint_treats_evolution_risk_downgrade_as_warn_not_provider_degraded(
) -> None:
    service = _new_service()
    _patch_attr(
        service,
        "provider_status",
        lambda: {
            "degraded_mode": True,
            "health": {"degraded_mode": False},
            "evolution": {
                "degraded_mode": True,
                "conservative_mode": True,
                "degraded_reason": "m2_extreme",
            },
            "realtime_monitoring": {
                "degraded_mode": True,
                "health": {"degraded_mode": False},
                "evolution": {
                    "degraded_mode": True,
                    "conservative_mode": True,
                    "degraded_reason": "m2_extreme",
                },
            },
        },
    )

    with _patched_service(service) as client:
        response = client.get("/runtime/stage/deep", params={"now": "2026-03-16T10:40:00"})
        assert response.status_code == 200
        payload = response.json()

    health = payload["health"]
    assert health["code"] == "warn"
    assert health["provider_degraded"] is False
    assert health["risk_degraded"] is True
    assert "风控降档" in str(health["detail"])
