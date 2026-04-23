from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.runtime import service as runtime_service_module
from stock_analyzer.runtime.service import StockAnalyzerService


def _load_test_config(tmp_path: Path) -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.models.cross_review.p_lgbm_min = 0.0
    config.models.cross_review.p_xgb_min = 0.0
    config.models.cross_review.p_meta_min = 0.0
    config.models.cross_review.max_diff = 1.0
    config.liquidity_filter_trend.min_daily_turnover = 0.0
    config.liquidity_filter_trend.min_float_market_cap = 0.0
    config.liquidity_filter_trend.max_turnover_rate = 1.0
    config.liquidity_filter_monster.min_daily_turnover = 0.0
    config.liquidity_filter_monster.min_float_market_cap = 0.0
    config.liquidity_filter_monster.max_turnover_rate = 1.0
    config.notification_filter.min_score = 0.0
    config.notification_filter.quiet_windows = []
    config.command_channel.secret_key = "test-secret"
    config.command_channel.state_persist_enabled = True
    config.command_channel.state_persist_path = str(tmp_path / "runtime_state.json")
    config.command_channel.history_archive_enabled = False
    config.training.artifact_path = str(root / "artifacts" / "nonexistent_test_model.json")
    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = False
    config.training.bootstrap_auto_seed_watchlist = False
    config.training.bootstrap_retry_enabled = False
    config.training.bootstrap_state_path = str(tmp_path / "bootstrap_state.json")
    config.week5.auto_run = False
    config.week5.auto_notify = False
    config.week5.first_board_windows = ["09:30-09:31"]
    config.week6.auto_run = False
    config.acceptance.auto_run = False
    config.cloud_backup.enabled = False
    config.scheduler.premarket_time = "23:59"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "23:59"
    config.evolution.enabled = True
    config.evolution.auto_run = False
    config.evolution.strict_dependency_check = False
    config.evolution.code_commit_id = "git:test"
    config.evolution.suggestions_dir = str(tmp_path / "suggestions")
    config.evolution.report_dir = str(tmp_path / "evolution_history")
    config.evolution.manifest_path = str(tmp_path / "run_manifest.json")
    config.evolution.compliance_db_path = str(tmp_path / "compliance.duckdb")
    config.tdx_sync.refresh_before_evolution = False
    config.market_warehouse.refresh_before_evolution = False
    return config


def _new_service(config: StockAnalyzerConfig) -> StockAnalyzerService:
    provider = SyntheticProvider(seed_offset=2027)
    original_build_runtime_provider = runtime_service_module.build_runtime_provider
    original_build_realtime_runtime_provider = (
        runtime_service_module.build_realtime_runtime_provider
    )
    original_build_market_depth_provider = runtime_service_module.build_market_depth_provider
    try:
        runtime_service_module.build_runtime_provider = (
            lambda config, synthetic_seed=2026: provider
        )
        runtime_service_module.build_realtime_runtime_provider = (
            lambda config, synthetic_seed=2026, timezone="Asia/Shanghai": provider
        )
        runtime_service_module.build_market_depth_provider = lambda config: None
        service = StockAnalyzerService(config=config)
    finally:
        runtime_service_module.build_runtime_provider = original_build_runtime_provider
        runtime_service_module.build_realtime_runtime_provider = (
            original_build_realtime_runtime_provider
        )
        runtime_service_module.build_market_depth_provider = original_build_market_depth_provider
    _patch_attr(service, "_provider", provider)
    _patch_attr(service._pipeline, "_provider", provider)
    _patch_attr(service, "_realtime_provider", provider)
    if service._realtime_pipeline is not None:
        _patch_attr(service._realtime_pipeline, "_provider", provider)
    runtime_root = (
        Path(config.command_channel.state_persist_path).resolve().parent / "runtime_views"
    )
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
    return service


def _valid_records() -> list[dict[str, object]]:
    return [
        {
            "symbol": "600000.SH",
            "open": 10.0,
            "high": 10.2,
            "low": 9.8,
            "close": 10.1,
            "volume": 2_000_000,
        }
    ]


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


def _seed_persisted_week_views(service: StockAnalyzerService) -> None:
    week4_report = {
        "timestamp": "2026-03-01T20:30:00",
        "overall": "pass",
        "checks": [],
    }
    week5_report = {
        "timestamp": "2026-03-01T20:35:00",
        "trace_id": "runtime-state-week5",
        "summary": {},
        "first_board": {"candidate_count": 0, "candidates": [], "leaders": []},
        "signal_pool": {"candidate_count": 0, "candidates": []},
        "anomalies": {"event_count": 0, "events": []},
        "empty_signal": {"triggered": False, "reasons": []},
        "monster_isolation": {"can_open_new_position": True, "reasons": []},
    }
    week6_report = {
        "timestamp": "2026-03-01T20:40:00",
        "main_force": {},
        "strategy_allocation": {},
        "calendar_factor": {},
        "global_market_factor": {},
        "regulatory_factor": {},
    }
    _patch_attr(service, "_last_week4_acceptance_report", week4_report)
    service._week4_acceptance_history.append(week4_report)
    _patch_attr(service, "_last_week5_scan_report", week5_report)
    service._week5_scan_history.append(week5_report)
    _patch_attr(service, "_last_week6_report", week6_report)
    service._week6_history.append(week6_report)
    service._persist_runtime_state_to_disk()  # noqa: SLF001


def _seed_persisted_market_radar_views(service: StockAnalyzerService) -> None:
    market_radar_report = {
        "timestamp": "2026-03-01T21:00:00",
        "status": "ok",
        "watchlist_count": 50,
        "radar_hits": [
            {"symbol": "300001", "baseline_score": 71.5, "reason_codes": ["gap_up"]},
            {"symbol": "002001", "baseline_score": 68.0, "reason_codes": ["volume_spike"]},
        ],
    }
    review_pool = [
        {
            "symbol": "300001",
            "timestamp": "2026-03-01T14:10:00",
            "baseline_score": 71.5,
            "reason_codes": ["gap_up"],
        },
        {
            "symbol": "002001",
            "timestamp": "2026-03-01T14:20:00",
            "baseline_score": 68.0,
            "reason_codes": ["volume_spike"],
        },
    ]
    _patch_attr(service, "_last_week5_market_radar_report", market_radar_report)
    _patch_attr(service, "_market_radar_review_pool", review_pool)
    service._persist_runtime_state_to_disk()  # noqa: SLF001


def _seed_persisted_evolution_release_views(service: StockAnalyzerService) -> None:
    gate = {
        "timestamp": "2026-03-03T12:00:00",
        "status": "approved",
        "accepted": True,
        "days": 10,
        "min_runs": 2,
    }
    approval = {
        "approval_id": "runtime-state-approval-1",
        "timestamp": "2026-03-03T12:05:00",
        "approved": True,
        "approver": "risk_committee",
        "note": "all checks passed",
    }
    tickets = [
        {
            "ticket_id": "runtime-state-ticket-1",
            "timestamp": "2026-03-03T12:10:00",
            "status": "issued",
            "operator": "release_manager",
        },
        {
            "ticket_id": "runtime-state-ticket-1",
            "timestamp": "2026-03-03T12:15:00",
            "status": "executed",
            "executor": "release_manager",
        },
        {
            "ticket_id": "runtime-state-ticket-1",
            "timestamp": "2026-03-03T12:18:00",
            "status": "confirmed",
            "confirmer": "risk_committee",
        },
        {
            "ticket_id": "runtime-state-ticket-1",
            "timestamp": "2026-03-03T12:20:00",
            "status": "rolled_back",
            "rollback_by": "risk_committee",
        },
    ]
    _patch_attr(service, "_last_evolution_release_gate", gate)
    service._evolution_release_gate_history.append(gate)
    _patch_attr(service, "_last_evolution_release_approval", approval)
    service._evolution_release_approval_history.append(approval)
    _patch_attr(service, "_last_evolution_release_ticket", tickets[-1])
    service._evolution_release_ticket_history.extend(tickets)
    service._persist_runtime_state_to_disk()  # noqa: SLF001


def _seed_persisted_learning_governance_views(service: StockAnalyzerService) -> None:
    proposals = [
        {
            "proposal_id": "runtime-learning-proposal-1",
            "timestamp": "2026-03-03T12:00:00",
            "status": "generated",
            "gate_status": "pass",
            "shadow_model_id": "learning_shadow_v1",
            "champion_model_id": "learning_champion_v0",
        },
        {
            "proposal_id": "runtime-learning-proposal-1",
            "timestamp": "2026-03-03T12:05:00",
            "status": "approved",
            "gate_status": "pass",
            "shadow_model_id": "learning_shadow_v1",
            "champion_model_id": "learning_champion_v0",
        },
    ]
    approval = {
        "approval_id": "runtime-learning-approval-1",
        "timestamp": "2026-03-03T12:05:00",
        "proposal_id": "runtime-learning-proposal-1",
        "approved": True,
        "approver": "risk_committee",
        "note": "approved",
    }
    tickets = [
        {
            "ticket_id": "runtime-learning-ticket-1",
            "timestamp": "2026-03-03T12:10:00",
            "status": "issued",
            "operator": "release_manager",
            "proposal": {"proposal_id": "runtime-learning-proposal-1"},
        },
        {
            "ticket_id": "runtime-learning-ticket-1",
            "timestamp": "2026-03-03T12:15:00",
            "status": "executed",
            "operator": "release_manager",
            "pending_confirmation": {
                "required": True,
                "state": "pending",
                "due_at": "2026-03-06T12:15:00",
            },
        },
        {
            "ticket_id": "runtime-learning-ticket-1",
            "timestamp": "2026-03-03T12:18:00",
            "status": "confirmed",
            "operator": "release_manager",
            "pending_confirmation": {
                "required": True,
                "state": "confirmed",
                "due_at": "2026-03-06T12:15:00",
            },
        },
    ]
    _patch_attr(service, "_last_learning_model_proposal", proposals[-1])
    service._learning_model_proposal_history.extend(proposals)
    _patch_attr(service, "_last_learning_model_approval", approval)
    service._learning_model_approval_history.append(approval)
    _patch_attr(service, "_last_learning_model_release_ticket", tickets[-1])
    service._learning_model_release_ticket_history.extend(tickets)
    service._persist_runtime_state_to_disk()  # noqa: SLF001


def _seed_persisted_execution_learning_views(service: StockAnalyzerService) -> None:
    execution_risk = {
        "timestamp": "2026-03-04T20:00:00",
        "status": "trained",
        "artifact_path": "tmp/execution_risk_artifact.json",
        "dataset_id": "execution_risk_dataset_v1_test",
        "trained_targets": ["can_fill", "likely_slippage_high"],
    }
    execution_aware = {
        "report_id": "execution_aware_report_test",
        "shadow_model_id": "learning_shadow_v1",
        "champion_model_id": "learning_champion_v0",
        "dataset_manifest_id": "dataset_manifest_v1_test",
        "execution_risk_artifact_path": "tmp/execution_risk_artifact.json",
        "summary_metrics": {
            "shadow_mean_can_fill": 0.72,
            "shadow_high_risk_ratio": 0.19,
        },
    }
    _patch_attr(service, "_last_execution_risk_training", execution_risk)
    service._execution_risk_training_history.append(execution_risk)
    _patch_attr(service, "_last_execution_aware_report", execution_aware)
    service._execution_aware_report_history.append(execution_aware)
    service._persist_runtime_state_to_disk()  # noqa: SLF001


def _seed_persisted_post_close_stage_views(service: StockAnalyzerService) -> None:
    market_warehouse_reports = [
        {
            "timestamp": "2026-03-02T18:45:00",
            "status": "ok",
            "trace_id": "scheduler-market-warehouse-older",
            "synced_symbols": 5000,
        },
        {
            "timestamp": "2026-03-03T18:49:07",
            "status": "ok",
            "trace_id": "scheduler-market-warehouse-latest",
            "synced_symbols": 5194,
        },
    ]
    market_warehouse_progress = {
        "timestamp": "2026-03-03T18:49:07",
        "updated_at": "2026-03-03T18:49:22",
        "status": "ok",
        "phase": "completed",
        "symbols_completed": 5194,
        "symbols_total": 5194,
        "progress_ratio": 1.0,
    }
    evolution_reports = [
        {
            "timestamp": "2026-03-02T20:31:57",
            "status": "ok",
            "proposal": {"proposal_id": "proposal-older"},
            "source_trace_id": "scheduler-evolution-older",
        },
        {
            "timestamp": "2026-03-03T20:31:57",
            "status": "ok",
            "proposal": {"proposal_id": "proposal-latest"},
            "source_trace_id": "scheduler-evolution-latest",
        },
    ]
    _patch_attr(service, "_last_market_warehouse_report", market_warehouse_reports[-1])
    _patch_attr(service, "_market_warehouse_history", market_warehouse_reports)
    _patch_attr(service, "_last_market_warehouse_progress", market_warehouse_progress)
    _patch_attr(service, "_last_evolution_report", evolution_reports[-1])
    _patch_attr(service, "_evolution_history", evolution_reports)
    service._persist_runtime_state_to_disk()  # noqa: SLF001


def _seed_runtime_run(service: StockAnalyzerService, *, trace_id: str) -> None:
    report = {
        "trace_id": trace_id,
        "timestamp": "2026-03-01T20:25:00",
        "risk": {"action": "monitor", "drawdown_pct": 0.0},
        "signals": [{"symbol": "600000"}],
        "actionable_signals": [{"symbol": "600000"}],
    }
    service._record_run_summary(  # noqa: SLF001
        report=report,
        current_equity=1.0,
        actionable_count=1,
        duration_ms=12,
    )
    service._record_audit_event(  # noqa: SLF001
        event_type="pipeline_run",
        trace_id=trace_id,
        payload={
            "strategy": "trend",
            "symbols": ["600000"],
            "signals": 1,
            "actionable": 1,
            "duration_ms": 12,
        },
    )


def _clear_persisted_runtime_views(service: StockAnalyzerService) -> None:
    _patch_attr(service, "_run_summaries", [])
    _patch_attr(service, "_latency_history_ms", [])
    _patch_attr(service, "_audit_events", [])
    _patch_attr(service, "_audit_seq", 0)
    _patch_attr(service, "_last_market_warehouse_report", None)
    _patch_attr(service, "_market_warehouse_history", [])
    _patch_attr(service, "_last_market_warehouse_progress", None)
    _patch_attr(service, "_last_week4_acceptance_report", None)
    service._week4_acceptance_history.clear()
    _patch_attr(service, "_last_week5_scan_report", None)
    service._week5_scan_history.clear()
    _patch_attr(service, "_last_week5_market_radar_report", None)
    _patch_attr(service, "_market_radar_review_pool", [])
    _patch_attr(service, "_last_week6_report", None)
    service._week6_history.clear()
    _patch_attr(service, "_last_evolution_report", None)
    _patch_attr(service, "_evolution_history", [])
    _patch_attr(service, "_last_evolution_release_gate", None)
    service._evolution_release_gate_history.clear()
    _patch_attr(service, "_last_evolution_release_approval", None)
    service._evolution_release_approval_history.clear()
    _patch_attr(service, "_last_evolution_release_ticket", None)
    service._evolution_release_ticket_history.clear()
    _patch_attr(service, "_last_learning_model_proposal", None)
    service._learning_model_proposal_history.clear()
    _patch_attr(service, "_last_learning_model_approval", None)
    service._learning_model_approval_history.clear()
    _patch_attr(service, "_last_learning_model_release_ticket", None)
    service._learning_model_release_ticket_history.clear()
    _patch_attr(service, "_last_execution_risk_training", None)
    service._execution_risk_training_history.clear()
    _patch_attr(service, "_last_execution_aware_report", None)
    service._execution_aware_report_history.clear()
    _patch_attr(service, "_runtime_state_loaded_mtime_ns", 0)


def test_runtime_state_persists_sla_audit_and_week_views(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config)
    trace_id = "runtime-state-pipeline"
    _seed_runtime_run(service, trace_id=trace_id)
    _seed_persisted_week_views(service)
    _clear_persisted_runtime_views(service)
    service._load_runtime_state_from_disk()  # noqa: SLF001

    restored = service
    sla = _as_mapping(restored.sla_report(recent_runs=10))
    assert sla["status"] == "ok"
    assert _as_int(sla["recent_runs"]) >= 1

    events = _as_mapping(restored.audit_events(limit=50, trace_id=trace_id))
    assert _as_int(events["records"]) >= 1
    latest_week4 = restored.latest_week4_acceptance_report()
    latest_week5 = restored.latest_week5_scan_report()
    latest_week6 = restored.latest_week6_report()
    assert latest_week4 is not None
    assert latest_week5 is not None
    assert latest_week6 is not None
    assert _as_int(_as_mapping(restored.week4_acceptance_history(limit=10))["records"]) >= 1
    assert _as_int(_as_mapping(restored.week5_scan_history(limit=10))["records"]) >= 1
    assert _as_int(_as_mapping(restored.week6_history(limit=10))["records"]) >= 1


def test_runtime_state_persists_evolution_release_views(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config)
    _seed_persisted_evolution_release_views(service)
    _clear_persisted_runtime_views(service)
    service._load_runtime_state_from_disk()  # noqa: SLF001

    restored = service
    latest_gate = restored.latest_evolution_release_gate()
    latest_approval = restored.latest_evolution_release_approval()
    latest_ticket = restored.latest_evolution_release_ticket()
    assert latest_gate is not None
    latest_gate_view = _as_mapping(latest_gate)
    assert latest_gate_view["status"] == "approved"
    assert latest_approval is not None
    latest_approval_view = _as_mapping(latest_approval)
    assert _as_bool(latest_approval_view["approved"]) is True
    assert latest_ticket is not None
    latest_ticket_view = _as_mapping(latest_ticket)
    assert latest_ticket_view["status"] == "rolled_back"
    assert _as_int(_as_mapping(restored.evolution_release_gate_history(limit=10))["records"]) >= 1
    assert (
        _as_int(_as_mapping(restored.evolution_release_approval_history(limit=10))["records"]) >= 1
    )
    assert _as_int(_as_mapping(restored.evolution_release_ticket_history(limit=10))["records"]) >= 4


def test_runtime_state_persists_learning_governance_views(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config)
    _seed_persisted_learning_governance_views(service)
    _clear_persisted_runtime_views(service)
    service._load_runtime_state_from_disk()  # noqa: SLF001

    restored = service
    latest_proposal = restored.latest_learning_model_proposal()
    latest_approval = restored.latest_learning_model_approval()
    latest_ticket = restored.latest_learning_model_release_ticket()
    assert latest_proposal is not None
    latest_proposal_view = _as_mapping(latest_proposal)
    assert latest_proposal_view["status"] == "approved"
    assert latest_approval is not None
    latest_approval_view = _as_mapping(latest_approval)
    assert _as_bool(latest_approval_view["approved"]) is True
    assert latest_ticket is not None
    latest_ticket_view = _as_mapping(latest_ticket)
    assert latest_ticket_view["status"] == "confirmed"
    assert _as_int(_as_mapping(restored.learning_model_proposal_history(limit=10))["records"]) >= 2
    assert _as_int(_as_mapping(restored.learning_model_approval_history(limit=10))["records"]) >= 1
    assert (
        _as_int(_as_mapping(restored.learning_model_release_ticket_history(limit=10))["records"])
        >= 3
    )
    governance = _as_mapping(restored.learning_model_governance_status())
    assert _as_mapping(governance["proposal_summary"])["records"] >= 1
    assert _as_mapping(governance["ticket_summary"])["records"] >= 1
    assert _as_mapping(governance["monitoring"])["pending_confirmation"] == 0


def test_runtime_state_persists_execution_learning_views(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config)
    _seed_persisted_execution_learning_views(service)
    _clear_persisted_runtime_views(service)
    service._load_runtime_state_from_disk()  # noqa: SLF001

    restored = service
    runtime = _as_mapping(restored.runtime_status())
    execution_risk = _as_mapping(runtime["execution_risk"])
    execution_aware = _as_mapping(runtime["execution_aware"])

    assert execution_risk["artifact_exists"] is False
    assert execution_risk["dataset_id"] == "execution_risk_dataset_v1_test"
    assert _as_int(execution_risk["history_count"]) == 1
    assert _as_mapping(execution_aware["latest"])["report_id"] == "execution_aware_report_test"
    assert _as_int(execution_aware["history_count"]) == 1


def test_runtime_state_persists_post_close_stage_views(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config)
    _seed_persisted_post_close_stage_views(service)
    _clear_persisted_runtime_views(service)
    service._load_runtime_state_from_disk()  # noqa: SLF001

    restored = service
    latest_market_warehouse = restored.latest_market_warehouse_report()
    latest_market_warehouse_progress = restored.latest_market_warehouse_progress()
    latest_evolution = restored.latest_evolution_report()
    assert latest_market_warehouse is not None
    latest_market_warehouse_view = _as_mapping(latest_market_warehouse)
    assert latest_market_warehouse_view["timestamp"] == "2026-03-03T18:49:07"
    assert latest_market_warehouse_progress is not None
    latest_market_warehouse_progress_view = _as_mapping(latest_market_warehouse_progress)
    assert latest_market_warehouse_progress_view["updated_at"] == "2026-03-03T18:49:22"
    assert _as_int(_as_mapping(restored.market_warehouse_history(limit=10))["records"]) >= 2
    assert latest_evolution is not None
    latest_evolution_view = _as_mapping(latest_evolution)
    assert latest_evolution_view["timestamp"] == "2026-03-03T20:31:57"
    assert _as_int(_as_mapping(restored.evolution_history(limit=10))["records"]) >= 2


def test_runtime_state_persists_week5_market_radar_views(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config)
    _seed_persisted_market_radar_views(service)
    _clear_persisted_runtime_views(service)
    service._load_runtime_state_from_disk()  # noqa: SLF001

    restored = service
    latest_market_radar = restored._last_week5_market_radar_report  # noqa: SLF001
    assert latest_market_radar is not None
    latest_market_radar_view = _as_mapping(latest_market_radar)
    assert latest_market_radar_view["status"] == "ok"
    review_pool = restored._market_radar_review_pool  # noqa: SLF001
    assert len(review_pool) == 2
    assert review_pool[0]["symbol"] == "300001"
    assert review_pool[1]["symbol"] == "002001"


def test_runtime_state_merge_deduplicates_market_radar_review_pool_by_symbol(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    service_a = _new_service(config)
    service_b = _new_service(config)

    _patch_attr(
        service_a,
        "_market_radar_review_pool",
        [
            {
                "symbol": "300001",
                "timestamp": "2026-03-01T14:10:00",
                "baseline_score": 60.0,
            }
        ],
    )
    service_a._persist_runtime_state_to_disk()  # noqa: SLF001

    _patch_attr(
        service_b,
        "_market_radar_review_pool",
        [
            {
                "symbol": "300001",
                "timestamp": "2026-03-01T14:30:00",
                "baseline_score": 72.0,
            },
            {
                "symbol": "002001",
                "timestamp": "2026-03-01T14:35:00",
                "baseline_score": 67.0,
            },
        ],
    )
    service_b._persist_runtime_state_to_disk()  # noqa: SLF001

    _patch_attr(service_a, "_market_radar_review_pool", [])
    service_a._load_runtime_state_from_disk()  # noqa: SLF001

    restored_pool = service_a._market_radar_review_pool  # noqa: SLF001
    assert len(restored_pool) == 2
    assert restored_pool[0]["symbol"] == "300001"
    assert restored_pool[0]["baseline_score"] == 72.0
    assert restored_pool[1]["symbol"] == "002001"


def test_runtime_state_merge_preserves_run_history_across_stale_processes(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service_a = _new_service(config)
    service_b = _new_service(config)

    _seed_runtime_run(service_a, trace_id="runtime-state-stale-merge")

    service_b._record_audit_event(  # noqa: SLF001
        event_type="stale_process_write",
        trace_id="stale-process",
        payload={"source": "service_b"},
    )
    _clear_persisted_runtime_views(service_a)
    service_a._load_runtime_state_from_disk()  # noqa: SLF001

    restored = service_a
    sla = _as_mapping(restored.sla_report(recent_runs=10))
    assert _as_int(sla["recent_runs"]) >= 1
    events = _as_mapping(restored.audit_events(limit=50, event_type="stale_process_write"))
    assert _as_int(events["records"]) >= 1


def test_runtime_state_merge_preserves_explicit_broker_snapshot_clear(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service_a = _new_service(config)
    service_a.update_broker_snapshot(
        positions=[
            {
                "symbol": "600000",
                "target_position": 0.2,
                "quantity": 200,
                "account": "STAGING",
            }
        ],
        source_trace_id="runtime-state-broker-seed",
    )
    service_b = _new_service(config)

    service_a.update_broker_snapshot(
        positions=[],
        source_trace_id="runtime-state-broker-clear",
    )
    service_b._record_audit_event(  # noqa: SLF001
        event_type="stale_process_write_after_broker_clear",
        trace_id="stale-broker-clear",
        payload={"source": "service_b"},
    )
    service_b._persist_runtime_state_to_disk()  # noqa: SLF001

    reloaded = _new_service(config)
    assert reloaded._broker_positions == {}  # noqa: SLF001
    assert reloaded._broker_position_details == {}  # noqa: SLF001
