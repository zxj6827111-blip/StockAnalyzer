from __future__ import annotations

import tempfile
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any, cast

import pytest

from stock_analyzer.command.channel import CommandEnvelope, SignedCommandProcessor
from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.runtime import service as runtime_service_module
from stock_analyzer.runtime.service import StockAnalyzerService


def _load_test_config() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.secret_key = "test-secret"
    config.command_channel.state_persist_enabled = False
    config.scheduler.premarket_time = "23:59"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "15:30"
    config.scheduler.week4_acceptance_time = "20:35"
    config.scheduler.week6_daily_time = "08:50"
    config.acceptance.export_enabled = False
    config.acceptance.auto_notify = False
    config.acceptance.notify_on_pass = False
    config.acceptance.sla_recent_runs = 20
    config.week5.auto_run = False
    config.week5.auto_notify = False
    config.week5.first_board_window_intervals = []
    config.week5.first_board_windows = ["09:30-09:31"]
    config.week5.first_board_interval_min = 1
    config.week6.auto_run = False
    config.week6.auto_notify = False
    config.week6.run_time = "08:50"
    config.market_warehouse.enabled = False
    config.market_warehouse.auto_run = False
    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = False
    config.training.bootstrap_auto_seed_watchlist = False
    config.training.bootstrap_retry_enabled = False
    temp_root = _make_test_temp_root()
    offline_root = temp_root / "missing_offline_package"
    offline_root.mkdir(parents=True, exist_ok=True)
    config.data_source.local_data_root = str(offline_root)
    config.tdx_sync.vipdoc_root = str(offline_root)
    config.command_channel.state_persist_path = str(temp_root / "runtime_state.json")
    config.command_channel.history_archive_dir = str(temp_root / "runtime_history")
    config.market_warehouse.db_path = str(temp_root / "market_warehouse.duckdb")
    config.market_warehouse.package_root = str(temp_root / "market_warehouse_package")
    config.market_warehouse.bootstrap_source_root = str(offline_root)
    config.training.artifact_path = str(temp_root / "test_model_scheduler.json")
    config.training.bootstrap_state_path = str(temp_root / "test_bootstrap_state_scheduler.json")
    config.acceptance.export_dir = str(temp_root / "acceptance")
    config.evolution.m3_store_dir = str(temp_root / "evolution_m3")
    config.evolution.report_dir = str(temp_root / "evolution_history")
    config.evolution.suggestions_dir = str(temp_root / "suggestions")
    config.evolution.compliance_db_path = str(temp_root / "evolution_compliance.duckdb")
    config.idle_queue.history_persist_path = str(temp_root / "idle_history.jsonl")
    config.idle_queue.universe_cache_path = str(temp_root / "universe_cache.json")
    config.evolution.auto_run = False
    config.evolution.dry_run = True
    return config


def _make_test_temp_root() -> Path:
    try:
        candidate = Path(tempfile.mkdtemp(prefix="stock_analyzer_tests_"))
        probe = candidate / ".write_probe"
        probe.mkdir(parents=True, exist_ok=True)
        return candidate
    except PermissionError:
        root = Path(__file__).resolve().parents[1]
        candidate = root / "manual_test_tmp" / f"stock_analyzer_tests_{uuid.uuid4().hex}"
        probe = candidate / ".write_probe"
        probe.mkdir(parents=True, exist_ok=True)
        return candidate


def _load_test_config_with_workspace_temp() -> StockAnalyzerConfig:
    config = _load_test_config()
    root = Path(__file__).resolve().parents[1]
    temp_root = root / "manual_test_tmp" / f"stock_analyzer_holiday_{uuid.uuid4().hex}"
    offline_root = temp_root / "missing_offline_package"
    offline_root.mkdir(parents=True, exist_ok=True)
    config.data_source.local_data_root = str(offline_root)
    config.tdx_sync.vipdoc_root = str(offline_root)
    config.command_channel.state_persist_path = str(temp_root / "runtime_state.json")
    config.command_channel.history_archive_dir = str(temp_root / "runtime_history")
    config.market_warehouse.db_path = str(temp_root / "market_warehouse.duckdb")
    config.market_warehouse.package_root = str(temp_root / "market_warehouse_package")
    config.market_warehouse.bootstrap_source_root = str(offline_root)
    config.training.artifact_path = str(temp_root / "test_model_scheduler.json")
    config.training.bootstrap_state_path = str(temp_root / "test_bootstrap_state_scheduler.json")
    config.acceptance.export_dir = str(temp_root / "acceptance")
    config.evolution.m3_store_dir = str(temp_root / "evolution_m3")
    config.evolution.report_dir = str(temp_root / "evolution_history")
    config.evolution.suggestions_dir = str(temp_root / "suggestions")
    config.evolution.compliance_db_path = str(temp_root / "evolution_compliance.duckdb")
    config.idle_queue.history_persist_path = str(temp_root / "idle_history.jsonl")
    config.idle_queue.universe_cache_path = str(temp_root / "universe_cache.json")
    return config


def _sign(
    action: str,
    command_id: str,
    payload: dict[str, object],
    secret: str,
) -> CommandEnvelope:
    ts = int(time.time())
    signature = SignedCommandProcessor.build_signature(
        secret_key=secret,
        command_id=command_id,
        timestamp=ts,
        action=action,
        payload=payload,
    )
    return CommandEnvelope(
        command_id=command_id,
        timestamp=ts,
        action=action,
        payload=payload,
        signature=signature,
    )


def _job_result(results: list[dict[str, object]], name: str) -> dict[str, object]:
    for item in results:
        if str(item.get("job", "")) == name:
            return item
    raise AssertionError(f"job not found: {name}")


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    raise AssertionError(f"Expected mapping, got {type(value).__name__}")


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    raise AssertionError(f"Expected bool value, got {value!r}")


def _as_int(value: object) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    raise AssertionError(f"Expected numeric value, got {value!r}")


def _patch_attr(target: object, name: str, value: object) -> None:
    setattr(cast(Any, target), name, value)


def _new_service(config: StockAnalyzerConfig) -> StockAnalyzerService:
    service = StockAnalyzerService(config=config)
    provider = SyntheticProvider(seed_offset=2029)
    temp_root = Path(config.training.artifact_path).resolve().parent
    runtime_root = temp_root / "runtime_artifacts"
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
    _patch_attr(
        service,
        "_post_market_warehouse_followup_state_path",
        runtime_root / "post_warehouse_followup_state.json",
    )
    _patch_attr(
        service,
        "_post_market_warehouse_followup_result_path",
        runtime_root / "post_warehouse_followup_result.json",
    )
    _patch_attr(service, "_provider", provider)
    _patch_attr(service._pipeline, "_provider", provider)
    return service


def test_service_defers_evolution_orchestrator_until_first_access() -> None:
    config = _load_test_config()
    config.evolution.auto_run = True
    created: list[tuple[object, object | None]] = []

    class _FakeOrchestrator:
        def __init__(self, *, config: object, project_root: object | None = None) -> None:
            created.append((config, project_root))

    original = runtime_service_module.OffhoursEvolutionOrchestrator
    runtime_service_module.OffhoursEvolutionOrchestrator = _FakeOrchestrator
    try:
        service = StockAnalyzerService(config=config)
        assert created == []

        first = service._evolution_orchestrator
        second = service._evolution_orchestrator

        assert first is second
        assert len(created) == 1
    finally:
        runtime_service_module.OffhoursEvolutionOrchestrator = original


def test_close_reconcile_job_reports_missing_snapshot() -> None:
    config = _load_test_config()
    service = _new_service(config)

    set_cmd = _sign(
        action="SET_POSITION",
        command_id="cmd-close-job-missing",
        payload={"symbol": "600000", "strategy": "manual", "target_position": 0.2},
        secret=config.command_channel.secret_key,
    )
    set_result = service.execute_command(set_cmd)
    assert set_result["accepted"] is True

    results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T15:30:00"))
    close_result = _job_result(results, "close_reconcile")
    assert _as_bool(close_result["ran"]) is True
    assert _as_bool(close_result["success"]) is True

    payload = _as_mapping(close_result["payload"])
    report = _as_mapping(payload["report"])
    daily_digest = _as_mapping(payload["daily_digest"])
    digest_summary = _as_mapping(daily_digest["summary"])
    assert report["status"] == "missing_snapshot"
    assert _as_bool(daily_digest["sent"]) is True
    assert digest_summary["reconcile_status"] == "missing_snapshot"
    assert _as_bool(payload["reconcile_required"]) is True
    assert service.state.reconcile_required is True


def test_close_reconcile_job_reports_position_mismatch() -> None:
    config = _load_test_config()
    service = _new_service(config)

    set_cmd = _sign(
        action="SET_POSITION",
        command_id="cmd-close-job-mismatch",
        payload={"symbol": "600000", "strategy": "manual", "target_position": 0.2},
        secret=config.command_channel.secret_key,
    )
    set_result = service.execute_command(set_cmd)
    assert set_result["accepted"] is True

    _ = service.update_broker_snapshot(
        positions=[{"symbol": "600000", "target_position": 0.05}],
        source_trace_id="snapshot-close-job-mismatch",
    )

    results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T15:30:00"))
    close_result = _job_result(results, "close_reconcile")
    assert _as_bool(close_result["ran"]) is True
    assert _as_bool(close_result["success"]) is True

    payload = _as_mapping(close_result["payload"])
    report = _as_mapping(payload["report"])
    daily_digest = _as_mapping(payload["daily_digest"])
    digest_summary = _as_mapping(daily_digest["summary"])
    assert report["status"] == "mismatch"
    assert _as_int(report["mismatch_count"]) == 1
    assert _as_bool(daily_digest["sent"]) is True
    assert digest_summary["reconcile_status"] == "mismatch"
    assert _as_bool(payload["reconcile_required"]) is True
    assert service.state.reconcile_required is True


def test_close_reconcile_daily_digest_is_deduplicated_same_day() -> None:
    config = _load_test_config()
    service = _new_service(config)
    now = datetime.fromisoformat("2026-03-02T15:30:00")
    reconcile_report = {"status": "ok", "mismatch_count": 0}
    first = _as_mapping(
        service._notify_daily_digest_if_needed(now=now, reconcile_report=reconcile_report)
    )
    second = _as_mapping(
        service._notify_daily_digest_if_needed(now=now, reconcile_report=reconcile_report)
    )
    assert _as_bool(first["sent"]) is True
    assert _as_bool(second["sent"]) is False
    assert second["reason"] == "dedup"


def test_close_reconcile_daily_digest_includes_top_signal_candidates() -> None:
    config = _load_test_config()
    service = _new_service(config)
    notifications: list[dict[str, object]] = []
    service.notify = lambda **kwargs: notifications.append(dict(kwargs))  # type: ignore[method-assign]
    _patch_attr(
        service,
        "_last_signal_payload",
        [
            {"symbol": "600000", "action": "watch", "score": 72.0, "grade": "A"},
            {"symbol": "000001", "action": "buy", "score": 81.5, "grade": "S"},
            {"symbol": "300750", "action": "buy", "score": 79.2, "grade": "A"},
            {"symbol": "688001", "action": "hold", "score": 40.0, "grade": "C"},
        ],
    )
    _patch_attr(
        service,
        "holding_alerts",
        lambda now=None: {
            "summary": {"warn": 1, "info": 1},
            "items": [
                {"symbol": "600000", "severity": "warn"},
                {"symbol": "300750", "severity": "info"},
            ],
        },
    )
    _patch_attr(
        service,
        "execution_bias_report",
        lambda days=7, limit=200: {
            "records": 2,
            "summary": {"better_price_rate": 0.25, "worse_price_rate": 0.50},
        },
    )

    payload = _as_mapping(
        service._notify_daily_digest_if_needed(
            now=datetime.fromisoformat("2026-03-02T15:30:00"),
            reconcile_report={"status": "ok", "mismatch_count": 0},
        )
    )
    top_candidates = cast(list[object], payload["top_signal_candidates"])
    research_focus = cast(list[object], payload["research_focus"])
    research_overview = _as_mapping(payload["research_overview"])

    assert _as_bool(payload["sent"]) is True
    assert payload["recommend_buy_symbols"] == ["000001", "300750"]
    assert payload["holding_warn_symbols"] == ["600000"]
    assert [str(_as_mapping(item)["symbol"]) for item in top_candidates] == [
        "000001",
        "300750",
        "600000",
        "688001",
    ]
    assert len(notifications) == 1
    assert notifications[0]["title"] == runtime_service_module._push_title(
        priority="P2",
        category="close",
        summary="post-market research digest",
    )
    assert "000001" in str(notifications[0]["content"])
    assert "重点标的" in str(notifications[0]["content"])
    assert "多空焦点" in str(notifications[0]["content"])
    assert len(research_focus) == 3
    assert str(_as_mapping(research_focus[0])["rating"]) in {"BUY", "WATCH-BUY"}
    assert str(research_overview["conclusion"])


def test_week4_acceptance_job_runs_at_configured_time() -> None:
    config = _load_test_config()
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "20:35"
    service = _new_service(config)

    _ = service.run_pipeline(symbols=["600000"], strategy="trend", current_equity=1.0)
    results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T20:35:00"))
    acceptance_result = _job_result(results, "week4_acceptance")
    assert _as_bool(acceptance_result["ran"]) is True
    assert _as_bool(acceptance_result["success"]) is True
    acceptance_payload = _as_mapping(acceptance_result["payload"])
    report = _as_mapping(acceptance_payload["report"])
    summary = _as_mapping(report["summary"])
    assert _as_int(summary["total"]) >= 6
    assert report["overall"] in {"pass", "pass_with_warnings"}


def test_evening_jobs_run_in_trigger_time_order_after_late_restart() -> None:
    config = _load_test_config()
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "20:35"
    config.market_warehouse.enabled = True
    config.market_warehouse.auto_run = True
    config.market_warehouse.run_time = "21:45"
    config.evolution.auto_run = True
    config.evolution.offhours_time = "20:30"
    service = _new_service(config)

    call_order: list[str] = []
    sync_called = Event()
    followup_called = Event()

    def _fake_evolution_job(now: datetime | None = None) -> dict[str, object]:
        _ = now
        call_order.append("evolution_offhours")
        return {"report": {"status": "ok"}}

    def _fake_acceptance_job() -> dict[str, object]:
        call_order.append("week4_acceptance")
        return {"report": {"overall": "pass"}}

    def _fake_market_warehouse(
        *,
        timestamp: datetime | None = None,
        notify_enabled: bool | None = None,
        force: bool = False,
        source_trace_id: str = "",
        symbols: list[str] | None = None,
        scheduler_lock_path: object = None,
        scheduler_lock_owner_token: str = "",
    ) -> dict[str, object]:
        _ = (
            timestamp,
            notify_enabled,
            force,
            symbols,
            scheduler_lock_path,
            scheduler_lock_owner_token,
        )
        call_order.append("market_warehouse_sync")
        sync_called.set()
        return {"status": "ok", "trace_id": source_trace_id}

    def _fake_followup(
        *,
        market_warehouse_report: Mapping[str, object] | None = None,
        trigger: str = "manual",
        timestamp: datetime | None = None,
    ) -> dict[str, object]:
        _ = market_warehouse_report, trigger, timestamp
        followup_called.set()
        return {"ok": True, "skipped": False}

    _patch_attr(service._evolution_core_service, "_job_evolution_offhours", _fake_evolution_job)
    _patch_attr(service._acceptance_service, "_job_week4_acceptance", _fake_acceptance_job)
    _patch_attr(service, "run_market_warehouse_sync", _fake_market_warehouse)
    _patch_attr(service, "run_post_market_warehouse_followup", _fake_followup)

    results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T22:00:00"))

    assert sync_called.wait(timeout=1.0)
    assert followup_called.wait(timeout=1.0)
    assert call_order == [
        "evolution_offhours",
        "week4_acceptance",
        "market_warehouse_sync",
    ]
    assert _as_bool(_job_result(results, "evolution_offhours")["ran"]) is True
    assert _as_bool(_job_result(results, "week4_acceptance")["ran"]) is True
    assert _as_bool(_job_result(results, "market_warehouse_sync")["ran"]) is True


def test_week5_first_board_interval_job_runs_in_window() -> None:
    config = _load_test_config()
    config.scheduler.premarket_time = "23:59"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "23:59"
    config.week5.auto_run = True
    config.week5.first_board_windows = ["09:30-09:31"]
    service = _new_service(config)
    service.state.watchlist = ["600000", "000001"]

    first_results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T09:30:00"))
    first_board = _job_result(first_results, "week5_first_board_1")
    assert _as_bool(first_board["ran"]) is True
    assert _as_bool(first_board["success"]) is True
    assert "report" in _as_mapping(first_board["payload"])

    duplicate_results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T09:30:30"))
    duplicate_board = _job_result(duplicate_results, "week5_first_board_1")
    assert _as_bool(duplicate_board["ran"]) is False

    next_slot_results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T09:31:00"))
    next_slot = _job_result(next_slot_results, "week5_first_board_1")
    assert _as_bool(next_slot["ran"]) is True


def test_week5_live_runtime_interval_job_uses_watchlist_and_live_runtime() -> None:
    config = _load_test_config()
    config.scheduler.premarket_time = "23:59"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "23:59"
    config.week5.auto_run = True
    config.week5.first_board_windows = ["09:30-09:31"]
    service = _new_service(config)
    service.state.watchlist = ["600000", "000001"]

    captured: dict[str, object] = {}
    _patch_attr(service, "run_week5_scan", lambda **_: {"status": "ok"})

    def _fake_run_pipeline(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "trace_id": "live-runtime-test",
            "actionable_signals": [],
            "portfolio_update": {"executions": []},
        }

    _patch_attr(service, "run_pipeline", _fake_run_pipeline)
    _patch_attr(
        service,
        "_notify_actionable_signals",
        lambda *args, **kwargs: pytest.fail("live runtime scheduler must stay silent"),
    )

    results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T09:30:00"))
    live_job = _job_result(results, "week5_live_runtime_1")
    assert _as_bool(live_job["ran"]) is True
    assert _as_bool(live_job["success"]) is True
    assert captured["symbols"] == ["600000", "000001"]
    assert captured["strategy"] == "trend"
    assert _as_bool(captured["use_live_runtime"]) is True
    assert _as_bool(captured["dry_run_execution"]) is True
    assert _as_bool(captured["notify_enabled"]) is False
    assert captured["job_name"] == "week5_live_runtime"
    assert _as_int(_as_mapping(live_job["payload"])["symbol_count"]) == 2


def test_run_due_jobs_selector_only_runs_matching_live_runtime_job() -> None:
    config = _load_test_config()
    config.scheduler.premarket_time = "09:30"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "23:59"
    config.week5.auto_run = True
    config.week5.first_board_windows = ["09:30-09:31"]
    config.week5.live_runtime_window_intervals = ["09:30-09:31@1"]
    config.week5.market_radar_enabled = True
    config.week5.market_radar_window_intervals = ["09:30-09:31@1"]
    service = _new_service(config)
    service.state.watchlist = ["600000", "000001"]

    called: list[str] = []
    _patch_attr(
        service,
        "run_week5_scan",
        lambda **_: called.append("week5_scan") or {"status": "ok"},
    )
    _patch_attr(
        service,
        "run_week5_market_radar",
        lambda **_: called.append("market_radar") or {"status": "ok"},
    )

    def _fake_run_pipeline(**kwargs: object) -> dict[str, object]:
        called.append(str(kwargs.get("job_name", "")))
        return {
            "trace_id": "selector-live-runtime-test",
            "actionable_signals": [],
            "portfolio_update": {"executions": []},
        }

    _patch_attr(service, "run_pipeline", _fake_run_pipeline)

    results = service.run_due_jobs(
        now=datetime.fromisoformat("2026-03-02T09:30:00"),
        only_jobs=["week5_live_runtime"],
    )

    assert called == ["week5_live_runtime"]
    assert [item["job"] for item in results] == ["week5_live_runtime_1"]
    live_job = results[0]
    assert _as_bool(live_job["ran"]) is True
    assert _as_bool(live_job["success"]) is True


def test_run_due_jobs_unknown_selector_reports_error_without_running_others() -> None:
    config = _load_test_config()
    config.scheduler.premarket_time = "09:30"
    config.week5.auto_run = True
    config.week5.first_board_windows = ["09:30-09:31"]
    service = _new_service(config)
    called: list[str] = []
    _patch_attr(
        service,
        "run_week5_scan",
        lambda **_: called.append("week5_scan") or {"status": "ok"},
    )

    results = service.run_due_jobs(
        now=datetime.fromisoformat("2026-03-02T09:30:00"),
        only_jobs=["not_a_real_job"],
    )

    assert called == []
    assert len(results) == 1
    assert results[0]["job"] == "not_a_real_job"
    assert _as_bool(results[0]["ran"]) is False
    assert _as_bool(results[0]["success"]) is False
    assert results[0]["detail"] == "unknown_job"


def test_run_due_jobs_rejects_broad_prefix_selector_without_running_week5_jobs() -> None:
    config = _load_test_config()
    config.scheduler.premarket_time = "09:30"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "23:59"
    config.week5.auto_run = True
    config.week5.first_board_windows = ["09:30-09:31"]
    config.week5.live_runtime_window_intervals = ["09:30-09:31@1"]
    config.week5.market_radar_enabled = True
    config.week5.market_radar_window_intervals = ["09:30-09:31@1"]
    service = _new_service(config)
    service.state.watchlist = ["600000", "000001"]

    called: list[str] = []
    _patch_attr(
        service,
        "run_week5_scan",
        lambda **_: called.append("week5_scan") or {"status": "ok"},
    )
    _patch_attr(
        service,
        "run_week5_market_radar",
        lambda **_: called.append("market_radar") or {"status": "ok"},
    )
    _patch_attr(
        service,
        "run_pipeline",
        lambda **kwargs: called.append(str(kwargs.get("job_name", ""))) or {},
    )

    results = service.run_due_jobs(
        now=datetime.fromisoformat("2026-03-02T09:30:00"),
        only_jobs=["week5"],
    )

    assert called == []
    assert results[0]["job"] == "week5"
    assert _as_bool(results[0]["success"]) is False
    assert results[0]["detail"] == "unknown_job"


def test_week5_live_runtime_limits_symbols_and_prioritizes_active_pools() -> None:
    config = _load_test_config()
    config.scheduler.premarket_time = "23:59"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "23:59"
    config.week5.auto_run = True
    config.week5.first_board_windows = ["09:30-09:31"]
    config.week5.live_runtime_window_intervals = ["09:30-09:31@1"]
    config.week5.live_runtime_max_symbols = 3
    service = _new_service(config)
    service.state.watchlist = ["000001", "000002", "000003"]
    _patch_attr(service, "run_week5_scan", lambda **_: {"status": "ok"})
    _patch_attr(
        service,
        "_last_week5_scan_report",
        {
            "first_board": {
                "candidates": [
                    {"symbol": "600000"},
                    {"symbol": "300001"},
                ],
            },
        },
    )
    _patch_attr(service, "_market_radar_review_pool", [{"symbol": "688001"}])

    captured: dict[str, object] = {}

    def _fake_run_pipeline(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "trace_id": "live-runtime-priority",
            "actionable_signals": [],
            "portfolio_update": {"executions": []},
        }

    _patch_attr(service, "run_pipeline", _fake_run_pipeline)
    _patch_attr(service, "_notify_actionable_signals", lambda report, trace_id, title_prefix: None)

    results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T09:30:00"))
    live_job = _job_result(results, "week5_live_runtime_1")
    payload = _as_mapping(live_job["payload"])
    selection = _as_mapping(payload["selection"])

    assert _as_bool(live_job["ran"]) is True
    assert captured["symbols"] == ["600000", "300001", "688001"]
    assert _as_int(selection["limit"]) == 3
    assert _as_int(selection["selected_count"]) == 3


def test_week5_live_runtime_auto_caps_symbols_after_slow_runs() -> None:
    config = _load_test_config()
    config.week5.live_runtime_max_symbols = 8
    config.week5.live_runtime_auto_cap_min_symbols = 6
    config.week5.live_runtime_auto_cap_window_runs = 5
    config.week5.live_runtime_auto_cap_safety_ratio = 0.75
    service = _new_service(config)
    service.state.watchlist = [
        "600000",
        "000001",
        "600519",
        "300750",
        "002594",
        "000333",
        "601318",
        "600036",
    ]
    service._latency_history_ms = [  # noqa: SLF001
        {
            "timestamp": "2026-03-02T10:05:00",
            "duration_ms": 105_000,
            "job_name": "week5_live_runtime",
            "symbol_count": 15,
            "use_live_runtime": True,
        }
    ]

    symbols, selection = service._week5_live_runtime_symbols()  # noqa: SLF001
    auto_cap = _as_mapping(selection["auto_cap"])

    assert len(symbols) == 6
    assert _as_int(selection["configured_limit"]) == 8
    assert _as_int(selection["limit"]) == 6
    assert auto_cap["applied"] is True
    assert _as_int(auto_cap["effective_limit"]) == 6


def test_week5_live_runtime_skips_next_slot_after_sla_backpressure() -> None:
    config = _load_test_config()
    config.scheduler.premarket_time = "23:59"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "23:59"
    config.week5.auto_run = True
    config.week5.first_board_windows = ["09:30-09:31"]
    config.week5.live_runtime_window_intervals = ["09:30-09:31@1"]
    config.week5.live_runtime_backpressure_threshold_ms = 60_000
    config.week5.live_runtime_backpressure_cooldown_min = 5
    service = _new_service(config)
    service.state.watchlist = ["600000"]
    service._run_summaries.append(  # noqa: SLF001
        {
            "timestamp": "2026-03-02T09:29:30",
            "job_name": "week5_live_runtime",
            "duration_ms": 120_000,
        }
    )
    _patch_attr(service, "run_week5_scan", lambda **_: {"status": "ok"})
    called: list[bool] = []
    _patch_attr(service, "run_pipeline", lambda **_: called.append(True) or {})

    results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T09:30:00"))
    live_job = _job_result(results, "week5_live_runtime_1")
    payload = _as_mapping(live_job["payload"])

    assert _as_bool(live_job["ran"]) is True
    assert live_job["detail"] == "skipped_sla_backpressure"
    assert payload["status"] == "skipped"
    assert payload["reason"] == "sla_backpressure"
    assert called == []


def test_week5_market_radar_interval_job_runs_in_configured_window() -> None:
    config = _load_test_config()
    config.scheduler.premarket_time = "23:59"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "23:59"
    config.week5.auto_run = True
    config.week5.market_radar_notify = False
    config.week5.first_board_windows = ["09:30-09:31"]
    config.week5.market_radar_window_intervals = ["09:35-09:36@1"]
    service = _new_service(config)

    captured: dict[str, object] = {}

    def _fake_run_week5_market_radar(
        *,
        timestamp: datetime | None = None,
        notify_enabled: bool | None = None,
    ) -> dict[str, object]:
        captured["timestamp"] = timestamp
        captured["notify_enabled"] = notify_enabled
        return {"status": "ok", "radar_hits": []}

    _patch_attr(service, "run_week5_market_radar", _fake_run_week5_market_radar)

    results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T09:35:00"))
    radar_job = _job_result(results, "week5_market_radar_1")
    assert _as_bool(radar_job["ran"]) is True
    assert _as_bool(radar_job["success"]) is True
    assert isinstance(captured["timestamp"], datetime)
    assert _as_bool(captured["notify_enabled"]) is False


def test_week6_daily_job_runs_at_configured_time() -> None:
    config = _load_test_config()
    config.week6.auto_run = True
    config.scheduler.premarket_time = "23:59"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "23:59"
    config.week5.first_board_windows = ["23:58-23:59"]
    config.week6.run_time = "08:50"
    service = _new_service(config)

    results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T08:50:00"))
    week6_result = _job_result(results, "week6_daily")
    assert _as_bool(week6_result["ran"]) is True
    assert _as_bool(week6_result["success"]) is True
    assert "report" in _as_mapping(week6_result["payload"])


def test_week6_data_prewarm_job_runs_at_configured_time() -> None:
    config = _load_test_config()
    config.week6.auto_run = True
    config.week6.data_prewarm_enabled = True
    config.week6.data_prewarm_time = "20:20"
    config.week6.data_quality_notify = False
    config.scheduler.premarket_time = "23:59"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "23:59"
    config.week5.auto_run = False
    service = _new_service(config)

    results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T20:20:00"))
    prewarm_result = _job_result(results, "week6_data_prewarm")
    assert _as_bool(prewarm_result["ran"]) is True
    assert _as_bool(prewarm_result["success"]) is True
    prewarm_payload = _as_mapping(prewarm_result["payload"])
    assert "report" in prewarm_payload
    report = _as_mapping(prewarm_payload["report"])
    assert "status" in report
    assert "overall_coverage_ratio" in report


def test_premarket_job_collects_global_snapshot() -> None:
    config = _load_test_config()
    config.scheduler.premarket_time = "08:30"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "23:59"
    config.week5.auto_run = False
    service = _new_service(config)

    results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T08:30:00"))
    premarket_result = _job_result(results, "premarket_scan")
    assert _as_bool(premarket_result["ran"]) is True
    assert _as_bool(premarket_result["success"]) is True
    payload = _as_mapping(premarket_result["payload"])
    assert "global_snapshot" in payload
    assert "news_watchlist" in payload
    news_payload = _as_mapping(payload["news_watchlist"])
    assert "records" in news_payload
    assert "summary" in news_payload
    global_snapshot = _as_mapping(payload["global_snapshot"])
    snapshot_payload = _as_mapping(global_snapshot["snapshot"])
    assert "a_share_correlation" in snapshot_payload

    latest_snapshot = _as_mapping(service.global_market_snapshot())
    latest = _as_mapping(latest_snapshot["snapshot"])
    assert "a_share_correlation" in latest
    history = _as_mapping(service.global_market_history(limit=10))
    assert _as_int(history["records"]) >= 1


def test_week7_cloud_backup_watchdog_interval_job_runs() -> None:
    config = _load_test_config()
    config.scheduler.premarket_time = "23:59"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "23:59"
    config.week5.auto_run = False
    config.week6.auto_run = False
    config.cloud_backup.enabled = True
    config.cloud_backup.ping_interval_min = 10
    config.cloud_backup.alert_after_offline_min = 120
    service = _new_service(config)

    first = service.run_due_jobs(now=datetime.fromisoformat("2026-03-01T08:20:00"))
    first_job = _job_result(first, "week7_cloud_backup_watchdog")
    assert _as_bool(first_job["ran"]) is True
    assert _as_bool(first_job["success"]) is True
    first_payload = _as_mapping(first_job["payload"])
    first_status = _as_mapping(first_payload["status"])
    assert first_payload["alerted"] is False
    assert first_status["is_offline"] is False
    assert first_status["armed"] is False

    duplicate = service.run_due_jobs(now=datetime.fromisoformat("2026-03-01T08:20:30"))
    duplicate_job = _job_result(duplicate, "week7_cloud_backup_watchdog")
    assert _as_bool(duplicate_job["ran"]) is False


def test_week7_cloud_backup_watchdog_reads_shared_runtime_state() -> None:
    config = _load_test_config()
    config.command_channel.state_persist_enabled = True
    config.scheduler.premarket_time = "23:59"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "23:59"
    config.week5.auto_run = False
    config.week6.auto_run = False
    config.cloud_backup.enabled = True
    config.cloud_backup.ping_interval_min = 10
    config.cloud_backup.alert_after_offline_min = 120

    scheduler_service = _new_service(config)
    api_service = _new_service(config)
    base = datetime.now() - timedelta(minutes=5)
    _ = api_service.cloud_backup_ping(source="api", timestamp=base)

    results = scheduler_service.run_due_jobs(now=datetime.fromisoformat("2026-03-01T08:20:00"))
    job = _job_result(results, "week7_cloud_backup_watchdog")
    payload = _as_mapping(job["payload"])
    status = _as_mapping(payload["status"])

    assert _as_bool(job["ran"]) is True
    assert payload["alerted"] is False
    assert status["last_ping_at"] == base.isoformat()
    assert status["last_ping_source"] == "api"
    assert status["armed"] is True
    assert status["is_offline"] is False


def test_scheduler_blocked_when_bootstrap_required_and_incomplete(tmp_path: Path) -> None:
    config = _load_test_config()
    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = True
    config.training.bootstrap_retry_enabled = False
    config.training.artifact_path = str(tmp_path / "missing_model.json")
    config.training.bootstrap_state_path = str(tmp_path / "bootstrap_state.json")
    service = _new_service(config)

    results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T09:30:00"))
    assert len(results) == 1
    blocked = results[0]
    assert blocked["job"] == "bootstrap_gate"
    assert _as_bool(blocked["ran"]) is False
    assert _as_bool(blocked["success"]) is False
    assert blocked["detail"] == "blocked_bootstrap_required"


def test_scheduler_bootstrap_retry_unblocks_runtime(tmp_path: Path) -> None:
    config = _load_test_config()
    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = True
    config.training.bootstrap_retry_enabled = True
    config.training.bootstrap_retry_interval_min = 1
    config.training.bootstrap_retry_notify = False
    config.training.bootstrap_lookback_days = 120
    config.training.bootstrap_max_symbols = 2
    config.training.artifact_path = str(tmp_path / "retry_model.json")
    config.training.bootstrap_state_path = str(tmp_path / "bootstrap_state_retry.json")
    config.week5.auto_run = False
    config.week6.auto_run = False
    service = _new_service(config)

    results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-01T00:00:00"))
    retry = _job_result(results, "bootstrap_retry")
    assert _as_bool(retry["ran"]) is True
    assert _as_bool(retry["success"]) is True
    status = _as_mapping(service.training_bootstrap_status())
    assert _as_bool(status["completed"]) is True
    assert _as_bool(status["runtime_blocked"]) is False


def test_scheduler_bootstrap_retry_auto_seeds_watchlist_when_enabled(tmp_path: Path) -> None:
    config = _load_test_config()
    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = True
    config.training.bootstrap_retry_enabled = True
    config.training.bootstrap_retry_interval_min = 1
    config.training.bootstrap_retry_notify = False
    config.training.bootstrap_lookback_days = 120
    config.training.bootstrap_max_symbols = 2
    config.training.bootstrap_auto_seed_watchlist = True
    config.training.bootstrap_seed_watchlist_size = 5
    config.training.bootstrap_seed_symbols = ["600000", "000001", "600519"]
    config.training.artifact_path = str(tmp_path / "seed_retry_model.json")
    config.training.bootstrap_state_path = str(tmp_path / "bootstrap_state_seed.json")
    config.week5.auto_run = False
    config.week6.auto_run = False
    service = _new_service(config)
    service.state.watchlist = []

    results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-01T00:00:00"))
    retry = _job_result(results, "bootstrap_retry")
    assert _as_bool(retry["ran"]) is True
    assert _as_bool(retry["success"]) is True
    status = _as_mapping(service.training_bootstrap_status())
    assert _as_bool(status["completed"]) is True
    assert len(service.state.watchlist) > 0


def test_existing_training_artifact_unblocks_runtime_on_startup(tmp_path: Path) -> None:
    config = _load_test_config()
    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = True
    config.training.bootstrap_retry_enabled = False
    config.training.artifact_path = str(tmp_path / "seeded_model.json")
    config.training.bootstrap_state_path = str(tmp_path / "bootstrap_state_existing.json")

    source_artifact = Path(__file__).resolve().parents[1] / "artifacts" / "model_v1.json"
    assert source_artifact.exists()
    Path(config.training.artifact_path).write_text(
        source_artifact.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    service = _new_service(config)

    status = _as_mapping(service.training_bootstrap_status())
    assert _as_bool(status["completed"]) is True
    assert _as_bool(status["runtime_blocked"]) is False
    assert str(status["artifact_path"]).endswith("seeded_model.json")


def test_week5_interval_profiles_support_mixed_frequencies() -> None:
    config = _load_test_config()
    config.scheduler.premarket_time = "23:59"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "23:59"
    config.week5.auto_run = True
    config.week5.first_board_window_intervals = ["09:30-09:35@5", "09:36-09:37@1"]
    config.week5.first_board_windows = []
    service = _new_service(config)
    service.state.watchlist = ["600000", "000001"]

    at_0930 = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T09:30:00"))
    j1 = _job_result(at_0930, "week5_first_board_1")
    j2 = _job_result(at_0930, "week5_first_board_2")
    assert _as_bool(j1["ran"]) is True
    assert _as_bool(j2["ran"]) is False

    at_0931 = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T09:31:00"))
    j1_2 = _job_result(at_0931, "week5_first_board_1")
    j2_2 = _job_result(at_0931, "week5_first_board_2")
    assert _as_bool(j1_2["ran"]) is False
    assert _as_bool(j2_2["ran"]) is False

    at_0936 = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T09:36:00"))
    j1_3 = _job_result(at_0936, "week5_first_board_1")
    j2_3 = _job_result(at_0936, "week5_first_board_2")
    assert _as_bool(j1_3["ran"]) is False
    assert _as_bool(j2_3["ran"]) is True


def test_idle_queue_auto_policy_registers_scheduler_job_in_staging_mode() -> None:
    config = _load_test_config()
    config.app.mode = "staging"
    config.idle_queue.enabled = False
    config.idle_queue.auto_run = False
    config.idle_queue.enabled_policy = "auto"
    config.idle_queue.auto_run_policy = "auto"
    config.idle_queue.enabled_modes = ["staging"]
    config.idle_queue.auto_run_modes = ["staging"]
    config.idle_queue.production_canary_ratio = 0.0
    config.scheduler.premarket_time = "23:59"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "23:59"
    service = _new_service(config)
    service.state.watchlist = ["600000.SH"]

    results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T20:40:00"))
    idle_jobs = [item for item in results if str(item.get("job", "")) == "idle_queue_tick"]
    assert len(idle_jobs) == 1
    assert _as_bool(idle_jobs[0]["ran"]) is True
    state = _as_mapping(service.idle_queue_state())
    assert _as_bool(state["enabled"]) is True
    assert _as_bool(state["auto_run"]) is True


def test_idle_queue_auto_policy_keeps_scheduler_job_disabled_in_simulation_mode() -> None:
    config = _load_test_config()
    config.app.mode = "simulation"
    config.idle_queue.enabled = False
    config.idle_queue.auto_run = False
    config.idle_queue.enabled_policy = "auto"
    config.idle_queue.auto_run_policy = "auto"
    config.idle_queue.enabled_modes = ["staging"]
    config.idle_queue.auto_run_modes = ["staging"]
    config.scheduler.premarket_time = "23:59"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "23:59"
    service = _new_service(config)
    service.state.watchlist = ["600000.SH"]

    results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T20:40:00"))
    idle_jobs = [item for item in results if str(item.get("job", "")) == "idle_queue_tick"]
    assert len(idle_jobs) == 0
    state = _as_mapping(service.idle_queue_state())
    assert _as_bool(state["enabled"]) is False
    assert _as_bool(state["auto_run"]) is False


def test_tdx_sync_job_runs_at_configured_time() -> None:
    config = _load_test_config()
    config.scheduler.premarket_time = "23:59"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "23:59"
    config.tdx_sync.enabled = True
    config.tdx_sync.auto_run = True
    config.tdx_sync.run_time = "18:20"
    service = _new_service(config)

    calls: list[dict[str, object]] = []

    def _fake_sync(**kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        return {"status": "ok", "reason": "force"}

    _patch_attr(service, "run_tdx_offline_sync", _fake_sync)

    results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T18:20:00"))
    tdx_result = _job_result(results, "tdx_offline_sync")
    assert _as_bool(tdx_result["ran"]) is True
    assert _as_bool(tdx_result["success"]) is True
    assert calls[0]["source_trace_id"] == "scheduler-tdx-sync"


def test_market_warehouse_sync_job_runs_at_configured_time() -> None:
    config = _load_test_config()
    config.scheduler.premarket_time = "23:59"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "23:59"
    config.market_warehouse.enabled = True
    config.market_warehouse.auto_run = True
    config.market_warehouse.run_time = "18:20"
    service = _new_service(config)

    calls: list[dict[str, object]] = []
    followup_called = Event()

    def _fake_sync(**kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        return {"status": "ok", "reason": "online_incremental"}

    def _fake_followup(
        *,
        market_warehouse_report: Mapping[str, object] | None = None,
        trigger: str = "manual",
        timestamp: datetime | None = None,
    ) -> dict[str, object]:
        _ = market_warehouse_report, trigger, timestamp
        followup_called.set()
        return {"ok": True, "skipped": True, "reason": "test"}

    _patch_attr(service, "run_market_warehouse_sync", _fake_sync)
    _patch_attr(service, "run_post_market_warehouse_followup", _fake_followup)

    results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T18:20:00"))
    warehouse_result = _job_result(results, "market_warehouse_sync")
    payload = _as_mapping(warehouse_result["payload"])
    assert _as_bool(warehouse_result["ran"]) is True
    assert _as_bool(warehouse_result["success"]) is True
    assert payload["status"] == "launched"
    assert _as_bool(payload["launched"]) is True
    assert followup_called.wait(timeout=1.0)
    assert calls[0]["source_trace_id"] == "scheduler-market-warehouse"


def test_market_warehouse_scheduler_job_launches_background_worker_and_runs_post_followup() -> None:
    config = _load_test_config()
    config.market_warehouse.enabled = True
    config.market_warehouse.auto_run = True
    config.market_warehouse.run_time = "21:45"
    service = _new_service(config)
    captured: list[dict[str, object]] = []
    followup_called = Event()

    def _fake_market_warehouse(
        *,
        timestamp: datetime | None = None,
        notify_enabled: bool | None = None,
        force: bool = False,
        source_trace_id: str = "",
        symbols: list[str] | None = None,
        scheduler_lock_path: object = None,
        scheduler_lock_owner_token: str = "",
    ) -> dict[str, object]:
        _ = (
            timestamp,
            notify_enabled,
            force,
            symbols,
            scheduler_lock_path,
            scheduler_lock_owner_token,
        )
        return {
            "status": "ok",
            "trace_id": source_trace_id,
            "target_trade_date": "2026-03-01",
            "background_data": {
                "status": "ok",
                "latest_trade_date": "2026-03-01",
                "latest_trade_date_coverage_ratio": 0.99,
            },
        }

    def _fake_followup(
        *,
        market_warehouse_report: Mapping[str, object] | None = None,
        trigger: str = "manual",
        timestamp: datetime | None = None,
    ) -> dict[str, object]:
        captured.append(
            {
                "report": dict(market_warehouse_report or {}),
                "trigger": trigger,
                "timestamp_is_set": timestamp is not None,
            }
        )
        followup_called.set()
        return {"ok": True, "skipped": False, "trigger": trigger}

    _patch_attr(service, "run_market_warehouse_sync", _fake_market_warehouse)
    _patch_attr(service, "run_post_market_warehouse_followup", _fake_followup)

    payload = _as_mapping(service._job_market_warehouse_sync())

    assert payload["status"] == "launched"
    assert _as_bool(payload["launched"]) is True
    assert followup_called.wait(timeout=1.0)
    assert len(captured) == 1
    assert captured[0]["trigger"] == "scheduler_market_warehouse"
    assert _as_bool(captured[0]["timestamp_is_set"]) is True
    assert captured[0]["report"]["target_trade_date"] == "2026-03-01"


def test_market_warehouse_scheduler_job_does_not_block_following_due_jobs() -> None:
    config = _load_test_config()
    config.scheduler.premarket_time = "23:59"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.market_warehouse.enabled = True
    config.market_warehouse.auto_run = True
    config.market_warehouse.run_time = "18:20"
    config.acceptance.enabled = True
    config.acceptance.auto_run = True
    config.scheduler.week4_acceptance_time = "18:21"
    service = _new_service(config)

    sync_started = Event()
    release_sync = Event()
    sync_finished = Event()
    followup_called = Event()

    def _fake_market_warehouse(
        *,
        timestamp: datetime | None = None,
        notify_enabled: bool | None = None,
        force: bool = False,
        source_trace_id: str = "",
        symbols: list[str] | None = None,
        scheduler_lock_path: object = None,
        scheduler_lock_owner_token: str = "",
    ) -> dict[str, object]:
        _ = (
            timestamp,
            notify_enabled,
            force,
            source_trace_id,
            symbols,
            scheduler_lock_path,
            scheduler_lock_owner_token,
        )
        sync_started.set()
        release_sync.wait(timeout=5.0)
        sync_finished.set()
        return {"status": "ok", "trace_id": "scheduler-market-warehouse"}

    def _fake_followup(
        *,
        market_warehouse_report: Mapping[str, object] | None = None,
        trigger: str = "manual",
        timestamp: datetime | None = None,
    ) -> dict[str, object]:
        _ = market_warehouse_report, trigger, timestamp
        followup_called.set()
        return {"ok": True, "skipped": False}

    def _fake_acceptance_job() -> dict[str, object]:
        return {"report": {"overall": "pass"}}

    _patch_attr(service, "run_market_warehouse_sync", _fake_market_warehouse)
    _patch_attr(service, "run_post_market_warehouse_followup", _fake_followup)
    _patch_attr(service._acceptance_service, "_job_week4_acceptance", _fake_acceptance_job)

    started = time.perf_counter()
    results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T18:21:00"))
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0
    assert sync_started.wait(timeout=1.0)
    assert sync_finished.is_set() is False
    assert followup_called.is_set() is False
    assert _as_bool(_job_result(results, "market_warehouse_sync")["ran"]) is True
    assert _as_bool(_job_result(results, "week4_acceptance")["ran"]) is True

    release_sync.set()
    assert sync_finished.wait(timeout=1.0)
    assert followup_called.wait(timeout=1.0)


def test_market_warehouse_scheduler_job_does_not_launch_when_lock_is_active() -> None:
    config = _load_test_config()
    config.market_warehouse.enabled = True
    config.market_warehouse.auto_run = True
    config.market_warehouse.run_time = "21:45"
    service = _new_service(config)

    calls: list[dict[str, object]] = []

    def _unexpected_sync(**kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        return {"status": "ok"}

    _patch_attr(service, "run_market_warehouse_sync", _unexpected_sync)

    lock_path, owner_token, _ = service._market_sync_service._acquire_market_warehouse_sync_lock(
        timestamp=datetime.fromisoformat("2026-03-01T21:45:00"),
        source_trace_id="existing-market-warehouse-sync",
        force=False,
    )
    assert lock_path is not None

    try:
        payload = _as_mapping(service._job_market_warehouse_sync())
        lock_payload = _as_mapping(payload["lock"])

        assert payload["status"] == "already_running"
        assert _as_bool(payload["launched"]) is False
        assert payload["reason"] == "market_warehouse_sync_lock_active"
        assert _as_bool(lock_payload["running"]) is True
        assert calls == []
    finally:
        service._market_sync_service._release_market_warehouse_sync_lock(
            lock_path=lock_path,
            owner_token=owner_token,
        )


def test_market_warehouse_scheduler_job_retries_after_lock_conflict_without_consuming_daily_slot(
) -> None:
    config = _load_test_config()
    config.scheduler.premarket_time = "23:59"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "23:59"
    config.market_warehouse.enabled = True
    config.market_warehouse.auto_run = True
    config.market_warehouse.run_time = "18:20"
    service = _new_service(config)

    sync_calls: list[dict[str, object]] = []
    followup_called = Event()

    def _fake_sync(**kwargs: object) -> dict[str, object]:
        sync_calls.append(dict(kwargs))
        return {"status": "ok", "trace_id": str(kwargs.get("source_trace_id", ""))}

    def _fake_followup(
        *,
        market_warehouse_report: Mapping[str, object] | None = None,
        trigger: str = "manual",
        timestamp: datetime | None = None,
    ) -> dict[str, object]:
        _ = market_warehouse_report, trigger, timestamp
        followup_called.set()
        return {"ok": True, "skipped": False}

    _patch_attr(service, "run_market_warehouse_sync", _fake_sync)
    _patch_attr(service, "run_post_market_warehouse_followup", _fake_followup)

    lock_path, owner_token, _ = service._market_sync_service._acquire_market_warehouse_sync_lock(
        timestamp=datetime.fromisoformat("2026-03-01T18:20:00"),
        source_trace_id="existing-market-warehouse-sync",
        force=False,
    )
    assert lock_path is not None

    try:
        blocked_results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T18:20:00"))
        blocked_job = _job_result(blocked_results, "market_warehouse_sync")

        assert _as_bool(blocked_job["ran"]) is False
        assert _as_bool(blocked_job["success"]) is True
        assert blocked_job["detail"] == "already_running"
        assert sync_calls == []
    finally:
        service._market_sync_service._release_market_warehouse_sync_lock(
            lock_path=lock_path,
            owner_token=owner_token,
        )

    launched_results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T18:21:00"))
    launched_job = _job_result(launched_results, "market_warehouse_sync")
    launched_payload = _as_mapping(launched_job["payload"])

    assert _as_bool(launched_job["ran"]) is True
    assert _as_bool(launched_job["success"]) is True
    assert launched_job["detail"] == "launched"
    assert launched_payload["status"] == "launched"
    assert followup_called.wait(timeout=1.0)
    assert len(sync_calls) == 1


def test_weekend_keeps_offhours_learning_but_skips_trading_jobs() -> None:
    config = _load_test_config()
    config.scheduler.premarket_time = "08:30"
    config.scheduler.close_reconcile_time = "15:30"
    config.scheduler.week4_acceptance_time = "20:35"
    config.market_warehouse.enabled = True
    config.market_warehouse.auto_run = True
    config.market_warehouse.run_time = "18:20"
    config.week5.auto_run = True
    config.week5.first_board_windows = ["09:30-09:31"]
    config.week6.auto_run = True
    config.week6.run_time = "20:35"
    config.evolution.auto_run = True
    config.evolution.offhours_time = "20:30"
    service = _new_service(config)

    evolution_calls: list[str] = []

    def _fake_evolution_job(now: datetime | None = None) -> dict[str, object]:
        _ = now
        evolution_calls.append("ok")
        return {"report": {"status": "ok"}}

    _patch_attr(service._evolution_core_service, "_job_evolution_offhours", _fake_evolution_job)

    morning = service.run_due_jobs(now=datetime.fromisoformat("2026-03-01T08:30:00"))
    intraday = service.run_due_jobs(now=datetime.fromisoformat("2026-03-01T09:30:00"))
    evening = service.run_due_jobs(now=datetime.fromisoformat("2026-03-01T20:35:00"))

    assert _job_result(morning, "premarket_scan")["detail"] == "not_scheduled_today"
    assert _job_result(intraday, "week5_first_board_1")["detail"] == "not_scheduled_today"
    assert _job_result(evening, "market_warehouse_sync")["detail"] == "not_scheduled_today"
    assert _job_result(evening, "week4_acceptance")["detail"] == "not_scheduled_today"
    assert _job_result(evening, "week6_daily")["detail"] == "not_scheduled_today"
    assert _as_bool(_job_result(evening, "evolution_offhours")["ran"]) is True
    assert evolution_calls == ["ok"]


def test_exchange_holiday_keeps_offhours_learning_but_skips_trading_jobs() -> None:
    config = _load_test_config_with_workspace_temp()
    config.scheduler.premarket_time = "08:30"
    config.scheduler.close_reconcile_time = "15:30"
    config.scheduler.week4_acceptance_time = "20:35"
    config.market_warehouse.enabled = True
    config.market_warehouse.auto_run = True
    config.market_warehouse.run_time = "18:20"
    config.week5.auto_run = True
    config.week5.first_board_windows = ["09:30-09:31"]
    config.week5.live_runtime_window_intervals = ["09:30-09:31@1"]
    config.week6.auto_run = True
    config.week6.run_time = "20:35"
    config.evolution.auto_run = True
    config.evolution.offhours_time = "20:30"
    service = _new_service(config)

    evolution_calls: list[str] = []

    def _fake_evolution_job(now: datetime | None = None) -> dict[str, object]:
        _ = now
        evolution_calls.append("ok")
        return {"report": {"status": "ok"}}

    _patch_attr(service._evolution_core_service, "_job_evolution_offhours", _fake_evolution_job)

    morning = service.run_due_jobs(now=datetime.fromisoformat("2026-05-01T08:30:00"))
    intraday = service.run_due_jobs(now=datetime.fromisoformat("2026-05-01T09:30:00"))
    evening = service.run_due_jobs(now=datetime.fromisoformat("2026-05-01T20:35:00"))

    assert _job_result(morning, "premarket_scan")["detail"] == "not_scheduled_today"
    assert _job_result(intraday, "week5_first_board_1")["detail"] == "not_scheduled_today"
    assert _job_result(intraday, "week5_live_runtime_1")["detail"] == "not_scheduled_today"
    assert _job_result(evening, "market_warehouse_sync")["detail"] == "not_scheduled_today"
    assert _job_result(evening, "week4_acceptance")["detail"] == "not_scheduled_today"
    assert _job_result(evening, "week6_daily")["detail"] == "not_scheduled_today"
    assert _as_bool(_job_result(evening, "evolution_offhours")["ran"]) is True
    assert evolution_calls == ["ok"]


def test_intraday_sla_excludes_exchange_holiday_latency() -> None:
    config = _load_test_config_with_workspace_temp()
    service = _new_service(config)
    _patch_attr(
        service,
        "_latency_history_ms",
        [
            {
                "timestamp": "2026-05-01T14:22:49",
                "duration_ms": 120000,
                "job_name": "week5_live_runtime",
                "runtime_role": "live_runtime",
                "symbol_count": 6,
                "use_live_runtime": True,
            },
            {
                "timestamp": "2026-05-06T14:22:49",
                "duration_ms": 30000,
                "job_name": "week5_live_runtime",
                "runtime_role": "live_runtime",
                "symbol_count": 6,
                "use_live_runtime": True,
            },
        ],
    )

    report = service.sla_report(
        recent_runs=10,
        session_scope="intraday",
        job_scope="live_runtime",
        max_symbol_count=8,
    )

    assert report["recent_runs"] == 1
    assert report["excluded_by_session_scope"] == 1
    assert report["compliance_rate"] == 1.0


def test_post_market_warehouse_followup_skips_when_coverage_gate_fails() -> None:
    config = _load_test_config()
    config.market_warehouse.post_followup_enabled = True
    config.market_warehouse.post_followup_min_latest_trade_date_coverage_ratio = 0.95
    service = _new_service(config)

    def _unexpected_week5(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("week5 should not run when followup gate blocks")

    _patch_attr(service, "run_week5_scan", _unexpected_week5)

    payload = service.run_post_market_warehouse_followup(
        market_warehouse_report={
            "status": "ok",
            "trace_id": "mw-test-gate",
            "target_trade_date": "2026-03-01",
            "background_data": {
                "status": "partial",
                "latest_trade_date": "2026-03-01",
                "latest_trade_date_coverage_ratio": 0.80,
            },
        },
        trigger="test",
        timestamp=datetime.fromisoformat("2026-03-01T21:45:00"),
    )

    assert _as_bool(payload["ok"]) is True
    assert _as_bool(payload["skipped"]) is True
    assert (
        str(payload.get("reason", ""))
        == "latest_trade_date_coverage_ratio_below_threshold"
    )
    gate = _as_mapping(payload["gate"])
    assert _as_bool(gate["allowed"]) is False


def test_post_market_warehouse_followup_retries_failed_symbols_before_gate() -> None:
    config = _load_test_config()
    config.market_warehouse.post_followup_enabled = True
    config.market_warehouse.post_followup_retry_failed_enabled = True
    config.market_warehouse.post_followup_min_latest_trade_date_coverage_ratio = 0.95
    config.market_warehouse.post_followup_run_learning_backfill = False
    config.market_warehouse.post_followup_run_training = False
    config.market_warehouse.post_followup_run_phase_d_tabular_deep = False
    service = _new_service(config)

    retry_calls: list[dict[str, object]] = []
    week5_calls: list[dict[str, object]] = []

    def _fake_retry_sync(**kwargs: object) -> dict[str, object]:
        retry_calls.append(dict(kwargs))
        return {
            "status": "ok",
            "trace_id": str(kwargs.get("source_trace_id", "")),
            "symbol_source": "retry_failed_only",
            "target_trade_date": "2026-03-01",
            "failed_symbols_total": 0,
            "background_data": {
                "status": "ok",
                "latest_trade_date": "2026-03-01",
                "latest_trade_date_coverage_ratio": 0.97,
            },
        }

    def _fake_week5(**kwargs: object) -> dict[str, object]:
        week5_calls.append(dict(kwargs))
        return {"signals": [], "ok": True}

    _patch_attr(service, "run_market_warehouse_sync", _fake_retry_sync)
    _patch_attr(service, "run_week5_scan", _fake_week5)

    payload = service.run_post_market_warehouse_followup(
        market_warehouse_report={
            "status": "partial",
            "trace_id": "manual-refresh-20260301-01",
            "symbol_source": "full_universe",
            "target_trade_date": "2026-03-01",
            "failed_symbols_total": 2,
            "failed_symbols": ["000001", "000002"],
            "background_data": {
                "status": "partial",
                "latest_trade_date": "2026-03-01",
                "latest_trade_date_coverage_ratio": 0.91,
            },
        },
        trigger="test",
        timestamp=datetime.fromisoformat("2026-03-01T21:45:00"),
    )

    retry_step = _as_mapping(_as_mapping(payload["steps"])["retry_failed_only"])
    gate = _as_mapping(payload["gate"])

    assert len(retry_calls) == 1
    assert _as_bool(retry_calls[0]["retry_failed_only"]) is True
    assert retry_calls[0]["retry_report_trace_id"] == "manual-refresh-20260301-01"
    assert _as_bool(retry_step["skipped"]) is False
    assert retry_step["retry_trace_id"] == "manual-refresh-20260301-01-retry"
    assert _as_bool(gate["allowed"]) is True
    assert payload["effective_market_warehouse_trace_id"] == "manual-refresh-20260301-01-retry"
    assert len(week5_calls) == 1


def test_post_market_warehouse_followup_skips_retry_when_no_failed_symbols() -> None:
    config = _load_test_config()
    config.market_warehouse.post_followup_enabled = True
    config.market_warehouse.post_followup_retry_failed_enabled = True
    config.market_warehouse.post_followup_min_latest_trade_date_coverage_ratio = 0.95
    config.market_warehouse.post_followup_run_learning_backfill = False
    config.market_warehouse.post_followup_run_training = False
    config.market_warehouse.post_followup_run_phase_d_tabular_deep = False
    service = _new_service(config)

    def _unexpected_retry(**kwargs: object) -> dict[str, object]:
        raise AssertionError("retry sync should not run when there are no failed symbols")

    week5_calls: list[dict[str, object]] = []

    def _fake_week5(**kwargs: object) -> dict[str, object]:
        week5_calls.append(dict(kwargs))
        return {"signals": [], "ok": True}

    _patch_attr(service, "run_market_warehouse_sync", _unexpected_retry)
    _patch_attr(service, "run_week5_scan", _fake_week5)

    payload = service.run_post_market_warehouse_followup(
        market_warehouse_report={
            "status": "ok",
            "trace_id": "manual-refresh-20260301-02",
            "symbol_source": "full_universe",
            "target_trade_date": "2026-03-01",
            "failed_symbols_total": 0,
            "failed_symbols": [],
            "background_data": {
                "status": "ok",
                "latest_trade_date": "2026-03-01",
                "latest_trade_date_coverage_ratio": 0.98,
            },
        },
        trigger="test",
        timestamp=datetime.fromisoformat("2026-03-01T21:45:00"),
    )

    retry_step = _as_mapping(_as_mapping(payload["steps"])["retry_failed_only"])
    gate = _as_mapping(payload["gate"])

    assert _as_bool(retry_step["skipped"]) is True
    assert retry_step["reason"] == "no_failed_symbols_to_retry"
    assert _as_bool(gate["allowed"]) is True
    assert payload["effective_market_warehouse_trace_id"] == "manual-refresh-20260301-02"
    assert len(week5_calls) == 1


def test_post_market_warehouse_followup_maps_shadow_proposal_into_compatibility_fields() -> None:
    config = _load_test_config()
    config.market_warehouse.post_followup_enabled = True
    config.market_warehouse.post_followup_run_week5 = False
    config.market_warehouse.post_followup_run_learning_backfill = False
    config.market_warehouse.post_followup_run_training = True
    config.market_warehouse.post_followup_run_phase_d_tabular_deep = False
    config.auto_promotion.notify_on_training_summary = False
    service = _new_service(config)

    _patch_attr(
        service,
        "build_learning_trainable_manifest",
        lambda: {
            "ok": True,
            "dataset_manifest_id": "dataset_manifest_v1_test",
            "included_snapshot_count": 12,
            "included_outcome_count": 11,
        },
    )
    _patch_attr(
        service,
        "run_learning_manifest_shadow_proposal",
        lambda **kwargs: {
            "ok": True,
            "mode": "learning_manifest_shadow_proposal",
            "status": "generated",
            "accepted": True,
            "dataset_manifest_id": str(kwargs.get("dataset_manifest_id", "")),
            "shadow_model_id": "model_shadow_test",
            "champion_model_id": "model_champion_test",
            "evaluation_split_names": ["test"],
            "workflow": {
                "ok": True,
                "mode": "learning_manifest_shadow_promotion_gate",
                "status": "pass",
                "accepted": True,
                "dataset_manifest_id": str(kwargs.get("dataset_manifest_id", "")),
                "shadow_model_id": "model_shadow_test",
                "champion_model_id": "model_champion_test",
                "shadow_validation_ok": True,
                "promotion_gate_ok": True,
                "shadow_validation": {
                    "ok": True,
                    "training": {
                        "ok": True,
                        "artifact_path": "tmp/manifest_model.json",
                        "predictor_loaded": False,
                        "model_registry": {"model_id": "model_shadow_test"},
                    },
                },
                "promotion_gate": {
                    "ok": True,
                    "status": "pass",
                    "accepted": True,
                },
            },
            "proposal": {
                "proposal_id": "LRN-PRP-0099",
                "status": "released",
                "gate_status": "pass",
            },
            "proposal_result": {"accepted": True, "errors": []},
            "auto_promotion": {
                "enabled": True,
                "proposal_id": "LRN-PRP-0099",
                "approval_id": "LRN-APR-0099",
                "ticket_id": "LRN-TKT-0099",
                "auto_approve": True,
                "auto_release": True,
                "predictor_loaded": True,
                "rejection_notified": False,
                "status": "released",
                "errors": [],
            },
            "errors": [],
        },
    )

    payload = _as_mapping(
        service.run_post_market_warehouse_followup(
            market_warehouse_report={
                "status": "ok",
                "trace_id": "manual-refresh-20260301-03",
                "symbol_source": "full_universe",
                "target_trade_date": "2026-03-01",
                "failed_symbols_total": 0,
                "failed_symbols": [],
                "background_data": {
                    "status": "ok",
                    "latest_trade_date": "2026-03-01",
                    "latest_trade_date_coverage_ratio": 0.99,
                },
            },
            trigger="test",
            timestamp=datetime.fromisoformat("2026-03-01T21:45:00"),
        )
    )
    steps = _as_mapping(payload["steps"])
    training_step = _as_mapping(steps["train_learning_manifest"])
    proposal_step = _as_mapping(steps["learning_shadow_proposal"])
    auto_promotion_step = _as_mapping(steps["auto_promotion"])
    summary_notification = _as_mapping(steps["learning_summary_notification"])

    assert _as_bool(payload["ok"]) is True
    assert payload["dataset_manifest_id"] == "dataset_manifest_v1_test"
    assert payload["model_id"] == "model_shadow_test"
    assert payload["learning_proposal_id"] == "LRN-PRP-0099"
    assert payload["learning_release_ticket_id"] == "LRN-TKT-0099"
    assert payload["learning_release_status"] == "released"
    assert training_step["artifact_path"] == "tmp/manifest_model.json"
    assert proposal_step["shadow_model_id"] == "model_shadow_test"
    assert auto_promotion_step["ticket_id"] == "LRN-TKT-0099"
    assert summary_notification["reason"] == "training_summary_notification_disabled"
