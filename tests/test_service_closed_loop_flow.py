from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, cast

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.runtime.service import StockAnalyzerService


def _patch_attr(target: object, name: str, value: object) -> None:
    setattr(cast(Any, target), name, value)


def _load_test_config(tmp_path: Path) -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.state_persist_enabled = False
    config.command_channel.state_persist_path = str(tmp_path / "runtime_state.json")
    config.scheduler.premarket_time = "23:59"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "15:10"
    config.scheduler.week4_acceptance_time = "23:59"
    config.scheduler.week6_daily_time = "15:25"
    config.week5.auto_run = True
    config.week5.auto_notify = False
    config.week5.first_board_window_intervals = ["09:30-09:31@1"]
    config.week5.first_board_windows = []
    config.week5.offhours_universe_refresh_enabled = False
    config.week6.auto_run = True
    config.week6.auto_notify = False
    config.week6.run_time = "15:25"
    config.week6.data_prewarm_enabled = True
    config.week6.data_prewarm_time = "15:20"
    config.week6.data_quality_notify = False
    config.acceptance.auto_run = False
    config.cloud_backup.enabled = False
    config.market_warehouse.auto_run = False
    config.tdx_sync.auto_run = False

    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = True
    config.training.bootstrap_retry_enabled = True
    config.training.bootstrap_retry_interval_min = 1
    config.training.bootstrap_retry_notify = False
    config.training.bootstrap_lookback_days = 120
    config.training.bootstrap_max_symbols = 2
    config.training.bootstrap_auto_seed_watchlist = True
    config.training.bootstrap_seed_watchlist_size = 5
    config.training.bootstrap_seed_symbols = ["600000", "000001", "600519", "300750"]
    config.training.bootstrap_state_path = str(tmp_path / "bootstrap_state.json")
    config.training.artifact_path = str(tmp_path / "model.json")
    offline_root = tmp_path / "missing_offline_package"
    offline_root.mkdir(parents=True, exist_ok=True)
    config.data_source.local_data_root = str(offline_root)

    config.evolution.enabled = True
    config.evolution.auto_run = True
    config.evolution.offhours_time = "20:30"
    config.evolution.strict_dependency_check = False
    config.evolution.auto_generate_loader_inputs = False
    config.evolution.m3_maintenance_interval_min = 1440
    config.evolution.release_confirmation_watchdog_interval_min = 1440
    config.evolution.code_commit_id = "git:test"
    config.evolution.suggestions_dir = str((tmp_path / "suggestions").relative_to(tmp_path))
    config.evolution.manifest_path = str(
        (tmp_path / "artifacts" / "evolution" / "run_manifest.json").relative_to(tmp_path)
    )
    config.evolution.compliance_db_path = str(
        (tmp_path / "artifacts" / "evolution" / "compliance.duckdb").relative_to(tmp_path)
    )

    config.app.mode = "staging"
    config.idle_queue.enabled = False
    config.idle_queue.auto_run = False
    config.idle_queue.enabled_policy = "auto"
    config.idle_queue.auto_run_policy = "auto"
    config.idle_queue.enabled_modes = ["staging"]
    config.idle_queue.auto_run_modes = ["staging"]
    config.idle_queue.dispatch_interval_minutes = 5
    config.idle_queue.output_root = str(tmp_path / "staging" / "idle_cache")
    config.idle_queue.history_persist_path = str(
        tmp_path / "staging" / "idle_cache" / "_meta" / "idle_history.jsonl"
    )
    return config


def _job_result(results: list[dict[str, object]], name: str) -> dict[str, object]:
    for item in results:
        if str(item.get("job", "")) == name:
            return item
    raise AssertionError(f"job not found: {name}")


def _build_closed_loop_smoke_service(tmp_path: Path) -> StockAnalyzerService:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)
    provider = SyntheticProvider(seed_offset=2060)
    _patch_attr(service, "_provider", provider)
    _patch_attr(service._pipeline, "_provider", provider)
    _patch_attr(service, "_realtime_provider", provider)
    if service._realtime_pipeline is not None:
        _patch_attr(service._realtime_pipeline, "_provider", provider)
    _patch_attr(service, "_evolution_project_root", tmp_path)
    _patch_attr(service, "_evolution_orchestrator", service._evolution_orchestrator.__class__(
        config=config.evolution,
        project_root=tmp_path,
    ))

    artifact_path = Path(config.training.artifact_path)

    def _fake_train_models(**_: object) -> dict[str, object]:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("{}", encoding="utf-8")
        service._training_bootstrap_state.update(
            {
                "completed": True,
                "bootstrap_runs": 1,
                "last_bootstrap_at": datetime.now().isoformat(),
                "last_status": "ok",
                "last_symbols": 2,
                "last_error": "",
                "artifact_path": str(artifact_path),
            }
        )
        return {
            "mode": "full_market",
            "ok": True,
            "status": "ok",
            "artifact_path": str(artifact_path),
            "predictor_loaded": True,
            "universe_source": "preferred_symbols",
            "universe_total": 2,
            "symbols_used": 2,
            "dataset_rows": 240,
            "truncated": False,
            "result": {"artifact_path": str(artifact_path)},
            "errors": [],
        }

    def _fake_run_week5_scan(**kwargs: object) -> dict[str, object]:
        if bool(kwargs.get("sync_watchlist", False)):
            service.state.watchlist = ["600000", "000001", "600519"]
        report: dict[str, object] = {
            "timestamp": datetime.now().isoformat(),
            "trace_id": "week5-smoke",
            "watchlist_size": len(service.state.watchlist),
            "symbol_source": "smoke",
            "runtime_source": {"mode": "offline_only", "provider": "synthetic"},
            "prefilter": {"enabled": False, "applied": False},
            "first_board": {
                "candidate_count": 1,
                "candidates": [{"symbol": "600000"}],
                "leaders": [{"symbol": "600000"}],
            },
            "signal_pool": {
                "candidate_count": 1,
                "candidates": [
                    {
                        "symbol": "600000",
                        "action": "buy",
                        "reasons": ["signal_strength", "capital_confirmation"],
                        "shortlist_selected": True,
                        "shortlist_score": 80.0,
                        "shortlist_reasons": ["signal_strength"],
                        "shortlist_components": {
                            "signal": 0.8,
                            "capital_flow": 0.7,
                            "trend": 0.7,
                            "price_volume": 0.6,
                            "execution_liquidity": 0.7,
                            "risk_penalty": 0.1,
                        },
                        "background_completion_score": 0.9,
                        "board_component": 0.7,
                        "completion_component": 0.8,
                    }
                ],
            },
            "anomalies": {"event_count": 0, "events": []},
            "empty_signal": {
                "triggered": False,
                "reasons": [],
                "buy_signals": 1,
                "drawdown_pct": 0.0,
            },
            "monster_isolation": {"can_open_new_position": True, "reasons": []},
            "summary": {
                "first_board_candidates": 1,
                "leaders": 1,
                "anomalies": 0,
                "empty_signal_triggered": False,
                "can_open_monster": True,
                "watchlist_synced": False,
            },
        }
        service._store_week5_scan_report(report)
        return report

    def _fake_run_week6_data_prewarm(**_: object) -> dict[str, object]:
        return {
            "timestamp": datetime.now().isoformat(),
            "source": "week6_data_prewarm",
            "status": "ok",
            "watchlist_size": len(service.state.watchlist),
            "overall_coverage_ratio": 1.0,
            "success_symbols": len(service.state.watchlist),
            "failed_symbols": 0,
            "coverage_by_field": {},
            "core_coverage_min": 1.0,
            "warn_fields": [],
            "critical_fields": [],
            "symbols": list(service.state.watchlist),
        }

    def _fake_run_week6_daily(**_: object) -> dict[str, object]:
        report: dict[str, object] = {
            "timestamp": datetime.now().isoformat(),
            "status": "ok",
            "summary": {"watchlist_size": len(service.state.watchlist)},
            "signals": [],
        }
        service._store_week6_report(report)
        return report

    def _fake_run_evolution_offhours(**_: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "timestamp": datetime.now().isoformat(),
            "proposal": {"authorization_level": "C"},
            "tdx_sync": {},
        }
        _patch_attr(service, "_last_evolution_report", payload)
        return payload

    def _fake_run_idle_queue_cycle(**_: object) -> dict[str, object]:
        report: dict[str, object] = {
            "timestamp": datetime.now().isoformat(),
            "status": "ran",
            "task_id": "WD-REPORT",
            "task_status": "ok",
        }
        service._store_idle_report(report)
        return report

    _patch_attr(service, "train_models", _fake_train_models)
    _patch_attr(service, "run_week5_scan", _fake_run_week5_scan)
    _patch_attr(service, "run_week6_data_prewarm", _fake_run_week6_data_prewarm)
    _patch_attr(service, "run_week6_daily", _fake_run_week6_daily)
    _patch_attr(service, "run_evolution_offhours", _fake_run_evolution_offhours)
    _patch_attr(service, "run_idle_queue_cycle", _fake_run_idle_queue_cycle)
    service.state.watchlist = []
    return service


def test_closed_loop_flow_bootstrap_watchlist_intraday_postclose_offhours(tmp_path: Path) -> None:
    service = _build_closed_loop_smoke_service(tmp_path)

    morning = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T09:30:00"))
    bootstrap_retry = _job_result(morning, "bootstrap_retry")
    assert bootstrap_retry["ran"] is True
    assert bootstrap_retry["success"] is True
    assert service.training_bootstrap_status()["completed"] is True
    assert len(service.state.watchlist) > 0

    week5 = _job_result(morning, "week5_first_board_1")
    assert week5["ran"] is True
    assert week5["success"] is True
    assert service.latest_week5_scan_report() is not None

    close_reconcile = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T15:10:00"))
    close_job = _job_result(close_reconcile, "close_reconcile")
    assert close_job["ran"] is True
    assert close_job["success"] is True

    prewarm = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T15:20:00"))
    prewarm_job = _job_result(prewarm, "week6_data_prewarm")
    assert prewarm_job["ran"] is True
    assert prewarm_job["success"] is True

    week6_daily = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T15:25:00"))
    week6_job = _job_result(week6_daily, "week6_daily")
    assert week6_job["ran"] is True
    assert week6_job["success"] is True
    assert service.latest_week6_report() is not None

    offhours = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T20:30:00"))
    evo_job = _job_result(offhours, "evolution_offhours")
    idle_job = _job_result(offhours, "idle_queue_tick")
    assert evo_job["ran"] is True
    assert evo_job["success"] is True
    assert idle_job["ran"] is True
    assert idle_job["success"] is True
    assert service.latest_evolution_report() is not None
    assert service.latest_idle_queue_report() is not None


def test_closed_loop_flow_morning_smoke_runs_bootstrap_and_week5(tmp_path: Path) -> None:
    service = _build_closed_loop_smoke_service(tmp_path)

    morning = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T09:30:00"))

    bootstrap_retry = _job_result(morning, "bootstrap_retry")
    week5 = _job_result(morning, "week5_first_board_1")

    assert bootstrap_retry["ran"] is True
    assert bootstrap_retry["success"] is True
    assert week5["ran"] is True
    assert week5["success"] is True
    assert len(service.state.watchlist) > 0
    assert service.latest_week5_scan_report() is not None


def test_closed_loop_flow_postclose_smoke_runs_week6_and_offhours(tmp_path: Path) -> None:
    service = _build_closed_loop_smoke_service(tmp_path)
    service._training_bootstrap_state["completed"] = True
    service.state.watchlist = ["600000", "000001"]

    close_reconcile = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T15:10:00"))
    prewarm = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T15:20:00"))
    week6_daily = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T15:25:00"))
    offhours = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T20:30:00"))

    assert _job_result(close_reconcile, "close_reconcile")["success"] is True
    assert _job_result(prewarm, "week6_data_prewarm")["success"] is True
    assert _job_result(week6_daily, "week6_daily")["success"] is True
    assert _job_result(offhours, "evolution_offhours")["success"] is True
    assert _job_result(offhours, "idle_queue_tick")["success"] is True
    assert service.latest_week6_report() is not None
    assert service.latest_evolution_report() is not None
    assert service.latest_idle_queue_report() is not None
