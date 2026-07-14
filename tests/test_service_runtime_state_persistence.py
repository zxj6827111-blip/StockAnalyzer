from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.models.execution_risk_artifact import ExecutionRiskArtifact
from stock_analyzer.runtime import service as runtime_service_module
from stock_analyzer.runtime.service import StockAnalyzerService
from stock_analyzer.types import PipelineReport, PipelineSignal, RiskStatus


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


def test_runtime_state_persists_latest_signals_after_advisory_pipeline(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    config.app.advisory_only = True
    service = _new_service(config)
    signal = PipelineSignal(
        symbol="600000",
        strategy="trend",
        score=82.0,
        grade="A",
        action="buy",
        target_position=0.05,
        probabilities={"lgbm": 0.8, "xgb": 0.7, "meta": 0.75},
        decision_trace={
            "financial_gate": {
                "allowed": True,
                "roe": 0.1234,
                "debt_ratio": 0.4567,
                "financial_data_complete": True,
                "financial_missing_fields": "",
                "financial_source": "unit_test_financials",
                "financial_report_date": "2026-03-31",
            }
        },
    )
    report = PipelineReport(
        trace_id="trace-latest-signals",
        timestamp=datetime.fromisoformat("2026-03-18T10:00:00"),
        degraded_mode=False,
        signals=[signal],
        risk=RiskStatus(
            can_open_new_position=True,
            drawdown_pct=0.0,
            degraded_mode=False,
            action="normal",
            reason="test",
        ),
    )
    _patch_attr(service._pipeline, "run_once", lambda **kwargs: report)

    payload = service.run_pipeline(
        symbols=["600000"],
        strategy="trend",
        current_equity=1.0,
        notify_enabled=False,
    )

    assert "latest_signals" in _as_mapping(payload["runtime"])["runtime_state_persist_reasons"]
    state_path = Path(config.command_channel.state_persist_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    persisted = _as_mapping(state["latest_signals"])
    assert persisted["trace_id"] == "trace-latest-signals"
    assert persisted["signal_count"] == 1

    reloaded = _new_service(config)
    latest = reloaded.latest_signals_snapshot()
    assert latest["trace_id"] == "trace-latest-signals"
    assert latest["source"] == "pipeline_run"
    assert latest["storage_source"] == "runtime_state"
    signals = _as_mapping_list(latest["signals"])
    assert signals[0]["symbol"] == "600000"
    assert "recommendation_id" in signals[0]
    financial_gate = _as_mapping(_as_mapping(signals[0]["decision_trace"])["financial_gate"])
    assert financial_gate["roe"] == 0.1234
    assert financial_gate["debt_ratio"] == 0.4567
    assert financial_gate["financial_data_complete"] is True
    assert financial_gate["financial_source"] == "unit_test_financials"
    assert financial_gate["financial_report_date"] == "2026-03-31"


def test_runtime_state_latest_signals_merge_prefers_newer_timestamp(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    service_a = _new_service(config)
    service_b = _new_service(config)

    _patch_attr(
        service_a,
        "_last_signal_snapshot",
        {
            "trace_id": "older-trace",
            "timestamp": "2026-03-18T09:30:00",
            "source": "pipeline_run",
            "signal_count": 1,
            "signals": [{"symbol": "600000", "action": "hold"}],
        },
    )
    service_a._persist_runtime_state_to_disk()  # noqa: SLF001

    _patch_attr(
        service_b,
        "_last_signal_snapshot",
        {
            "trace_id": "newer-trace",
            "timestamp": "2026-03-18T10:00:00",
            "source": "pipeline_run",
            "signal_count": 1,
            "signals": [{"symbol": "000001", "action": "hold"}],
        },
    )
    service_b._persist_runtime_state_to_disk()  # noqa: SLF001

    service_a._persist_runtime_state_to_disk()  # noqa: SLF001
    state = json.loads(Path(config.command_channel.state_persist_path).read_text())
    latest = _as_mapping(state["latest_signals"])
    assert latest["trace_id"] == "newer-trace"
    assert _as_mapping_list(latest["signals"])[0]["symbol"] == "000001"


def test_runtime_state_latest_signals_cleared_when_bootstrap_blocks(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    config.training.bootstrap_require_completion_for_runtime = True
    service = _new_service(config)
    service._training_bootstrap_state["completed"] = False  # noqa: SLF001
    _patch_attr(service, "_last_signal_payload", [{"symbol": "600000", "action": "buy"}])
    _patch_attr(service, "_last_signal_trace_id", "stale-trace")
    _patch_attr(service, "_last_signal_timestamp", "2026-03-18T10:00:00")
    _patch_attr(service, "_last_signal_source", "pipeline_run")
    _patch_attr(
        service,
        "_last_signal_snapshot",
        {
            "trace_id": "stale-trace",
            "timestamp": "2026-03-18T10:00:00",
            "source": "pipeline_run",
            "signals": [{"symbol": "600000", "action": "buy"}],
        },
    )

    payload = service.run_pipeline(
        symbols=["600000"],
        strategy="trend",
        current_equity=1.0,
        notify_enabled=False,
    )

    assert payload["status"] == "blocked_bootstrap_required"
    latest = service.latest_signals_snapshot()
    assert latest["signals"] == []
    assert latest["source"] == "empty"


def test_runtime_state_load_clears_stale_memory_when_latest_signals_missing(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config)
    _patch_attr(service, "_last_signal_payload", [{"symbol": "600000", "action": "buy"}])
    _patch_attr(service, "_last_signal_trace_id", "stale-trace")
    _patch_attr(service, "_last_signal_timestamp", "2026-03-18T10:00:00")
    _patch_attr(service, "_last_signal_source", "pipeline_run")
    state_path = Path(config.command_channel.state_persist_path)
    state_path.write_text(
        json.dumps(
            {
                "state_version": 7,
                "scheduler_state": {},
                "portfolio": {},
                "broker_positions": {},
                "broker_position_details": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    service._load_runtime_state_from_disk()  # noqa: SLF001

    latest = service.latest_signals_snapshot()
    assert latest["signals"] == []
    assert latest["source"] == "empty"


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    raise AssertionError(f"Expected mapping, got {type(value).__name__}")


def _as_mapping_list(value: object) -> list[Mapping[str, object]]:
    if isinstance(value, list):
        return [cast(Mapping[str, object], item) for item in value if isinstance(item, Mapping)]
    raise AssertionError(f"Expected list, got {type(value).__name__}")


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


def _write_execution_risk_artifact(
    path: Path,
    *,
    dataset_id: str,
    trained_targets: list[str],
    created_at: str,
) -> None:
    ExecutionRiskArtifact(
        version="v1",
        created_at=created_at,
        dataset_id=dataset_id,
        feature_names=["execution_feature"],
        trained_targets=list(trained_targets),
        target_models={target: {"model_type": "test"} for target in trained_targets},
        training_summary={"row_count": 10},
        metadata={"created_at": created_at},
    ).save(path)


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


def test_runtime_state_persists_compact_json(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config)
    _seed_runtime_run(service, trace_id="runtime-state-compact")
    state_path = Path(config.command_channel.state_persist_path)
    raw = state_path.read_text(encoding="utf-8")
    payload = _as_mapping(json.loads(raw))

    assert "\n  " not in raw
    assert payload["state_version"] == 9


def test_runtime_state_writes_large_histories_to_sidecars(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config)
    trace_id = "runtime-state-sidecar"
    _seed_runtime_run(service, trace_id=trace_id)
    _seed_persisted_week_views(service)

    state_path = Path(config.command_channel.state_persist_path)
    payload = _as_mapping(json.loads(state_path.read_text(encoding="utf-8")))
    sidecar_dir = state_path.with_name("runtime_state_history")

    assert "reconcile_history" not in payload
    assert "run_summaries" not in payload
    assert "latency_history_ms" not in payload
    assert "audit_events" not in payload
    assert "week5_scan_history" not in payload
    assert _as_mapping(payload["runtime_history_sidecars"])["format"] == "jsonl"

    run_lines = (sidecar_dir / "run_summaries.jsonl").read_text(encoding="utf-8").splitlines()
    latency_lines = (
        sidecar_dir / "latency_history_ms.jsonl"
    ).read_text(encoding="utf-8").splitlines()
    audit_lines = (sidecar_dir / "audit_events.jsonl").read_text(encoding="utf-8").splitlines()
    week5_lines = (
        sidecar_dir / "week5_scan_history.jsonl"
    ).read_text(encoding="utf-8").splitlines()

    assert any(trace_id in line for line in run_lines)
    assert any(trace_id in line for line in latency_lines)
    assert any(trace_id in line for line in audit_lines)
    assert any("runtime-state-week5" in line for line in week5_lines)


def test_runtime_state_loads_legacy_embedded_histories_and_migrates_sidecars(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    state_path = Path(config.command_channel.state_persist_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_payload = {
        "state_version": 6,
        "updated_at": "2026-03-01T20:25:00",
        "scheduler_state": {},
        "current_equity": 1.0,
        "watchlist": ["600000"],
        "pause_new_buy": False,
        "reconcile_required": False,
        "portfolio": {},
        "recommendation_lifecycle": {},
        "audit_seq": 1,
        "run_summaries": [
            {
                "timestamp": "2026-03-01T20:25:00",
                "trace_id": "legacy-history",
                "duration_ms": 12,
            }
        ],
        "latency_history_ms": [
            {
                "timestamp": "2026-03-01T20:25:00",
                "trace_id": "legacy-history",
                "duration_ms": 12,
            }
        ],
        "audit_events": [
            {
                "event_id": "AUD-00000001",
                "timestamp": "2026-03-01T20:25:01",
                "event_type": "pipeline_run",
                "trace_id": "legacy-history",
                "level": "info",
                "payload": {},
            }
        ],
        "week5_scan_latest": {
            "timestamp": "2026-03-01T20:30:00",
            "trace_id": "legacy-week5",
            "status": "ok",
        },
        "week5_scan_history": [
            {
                "timestamp": "2026-03-01T20:30:00",
                "trace_id": "legacy-week5",
                "status": "ok",
            }
        ],
    }
    state_path.write_text(json.dumps(legacy_payload, ensure_ascii=False), encoding="utf-8")

    service = _new_service(config)
    assert _as_int(_as_mapping(service.sla_report(recent_runs=10))["recent_runs"]) >= 1
    events = _as_mapping(service.audit_events(limit=10, trace_id="legacy-history"))
    assert _as_int(events["records"]) == 1
    assert _as_int(_as_mapping(service.week5_scan_history(limit=10))["records"]) == 1

    service._persist_runtime_state_to_disk()  # noqa: SLF001
    migrated = _as_mapping(json.loads(state_path.read_text(encoding="utf-8")))
    assert "run_summaries" not in migrated
    assert "latency_history_ms" not in migrated
    assert "audit_events" not in migrated
    assert "week5_scan_history" not in migrated
    sidecar_dir = state_path.with_name("runtime_state_history")
    assert "legacy-history" in (sidecar_dir / "run_summaries.jsonl").read_text(
        encoding="utf-8"
    )
    assert "legacy-week5" in (sidecar_dir / "week5_scan_history.jsonl").read_text(
        encoding="utf-8"
    )


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


def test_execution_risk_status_refreshes_runtime_state_from_disk(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service_a = _new_service(config)
    service_b = _new_service(config)

    old_artifact = tmp_path / "execution_risk_old.json"
    old_artifact.write_text(
        json.dumps(
            {
                "artifact_type": "execution_risk",
                "artifact_version": "execution_risk_artifact_v1",
                "dataset_id": "execution_risk_dataset_v1_old",
                "trained_targets": ["can_fill"],
                "targets": {},
                "created_at": "2026-03-04T10:00:00",
            }
        ),
        encoding="utf-8",
    )
    old_record = {
        "timestamp": "2026-03-04T10:00:00",
        "status": "trained",
        "artifact_path": str(old_artifact),
        "dataset_id": "execution_risk_dataset_v1_old",
        "trained_targets": ["can_fill"],
    }
    _patch_attr(service_a, "_last_execution_risk_training", old_record)
    service_a._execution_risk_training_history.append(old_record)  # noqa: SLF001
    service_a._persist_runtime_state_to_disk()  # noqa: SLF001

    stale_status = _as_mapping(service_b.execution_risk_status())
    assert stale_status["dataset_id"] == "execution_risk_dataset_v1_old"

    new_artifact = tmp_path / "execution_risk_new.json"
    new_artifact.write_text(
        json.dumps(
            {
                "artifact_type": "execution_risk",
                "artifact_version": "execution_risk_artifact_v1",
                "dataset_id": "execution_risk_dataset_v1_new",
                "trained_targets": ["can_fill", "likely_slippage_high"],
                "targets": {},
                "created_at": "2026-03-04T15:25:03",
            }
        ),
        encoding="utf-8",
    )
    new_record = {
        "timestamp": "2026-03-04T15:25:03",
        "status": "trained",
        "artifact_path": str(new_artifact),
        "dataset_id": "execution_risk_dataset_v1_new",
        "trained_targets": ["can_fill", "likely_slippage_high"],
    }
    _patch_attr(service_a, "_last_execution_risk_training", new_record)
    service_a._execution_risk_training_history.append(new_record)  # noqa: SLF001
    service_a._persist_runtime_state_to_disk()  # noqa: SLF001

    refreshed_status = _as_mapping(service_b.execution_risk_status())
    assert refreshed_status["dataset_id"] == "execution_risk_dataset_v1_new"
    assert refreshed_status["artifact_path"] == str(new_artifact)
    assert refreshed_status["trained_targets"] == ["can_fill", "likely_slippage_high"]
    assert _as_int(refreshed_status["history_count"]) == 2


def test_execution_risk_status_force_reloads_even_when_mtime_gate_is_stale(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    service_a = _new_service(config)
    service_b = _new_service(config)

    old_artifact = tmp_path / "execution_risk_old_mtime.json"
    old_artifact.write_text(
        json.dumps(
            {
                "artifact_type": "execution_risk",
                "artifact_version": "execution_risk_artifact_v1",
                "dataset_id": "execution_risk_dataset_v1_old_mtime",
                "trained_targets": ["can_fill"],
                "targets": {},
                "created_at": "2026-03-04T10:00:00",
            }
        ),
        encoding="utf-8",
    )
    old_record = {
        "timestamp": "2026-03-04T10:00:00",
        "status": "trained",
        "artifact_path": str(old_artifact),
        "dataset_id": "execution_risk_dataset_v1_old_mtime",
        "trained_targets": ["can_fill"],
    }
    _patch_attr(service_a, "_last_execution_risk_training", old_record)
    service_a._execution_risk_training_history.append(old_record)  # noqa: SLF001
    service_a._persist_runtime_state_to_disk()  # noqa: SLF001
    assert _as_mapping(service_b.execution_risk_status())["dataset_id"].endswith("_old_mtime")

    new_artifact = tmp_path / "execution_risk_new_mtime.json"
    new_artifact.write_text(
        json.dumps(
            {
                "artifact_type": "execution_risk",
                "artifact_version": "execution_risk_artifact_v1",
                "dataset_id": "execution_risk_dataset_v1_new_mtime",
                "trained_targets": ["can_fill", "likely_slippage_high"],
                "targets": {},
                "created_at": "2026-03-04T15:25:03",
            }
        ),
        encoding="utf-8",
    )
    new_record = {
        "timestamp": "2026-03-04T15:25:03",
        "status": "trained",
        "artifact_path": str(new_artifact),
        "dataset_id": "execution_risk_dataset_v1_new_mtime",
        "trained_targets": ["can_fill", "likely_slippage_high"],
    }
    _patch_attr(service_a, "_last_execution_risk_training", new_record)
    service_a._execution_risk_training_history.append(new_record)  # noqa: SLF001
    service_a._persist_runtime_state_to_disk()  # noqa: SLF001
    service_b._runtime_state_loaded_mtime_ns = (  # noqa: SLF001
        Path(config.command_channel.state_persist_path).stat().st_mtime_ns + 1_000_000
    )

    refreshed_status = _as_mapping(service_b.execution_risk_status())
    assert refreshed_status["dataset_id"] == "execution_risk_dataset_v1_new_mtime"
    assert refreshed_status["artifact_path"] == str(new_artifact)
    history = _as_mapping(service_b.execution_risk_training_history(limit=10))
    assert _as_int(history["records"]) == 2


def test_execution_risk_status_discovers_newer_artifact_when_runtime_state_missing_record(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config)
    artifact_root = Path(config.training.bootstrap_state_path).parent / "execution_risk"
    new_artifact = artifact_root / "execution_risk_20260528T152503.json"
    _write_execution_risk_artifact(
        new_artifact,
        dataset_id="execution_risk_dataset_v1_scanned",
        trained_targets=["can_fill", "likely_slippage_high"],
        created_at="2026-05-28T15:25:03",
    )

    status = _as_mapping(service.execution_risk_status())

    assert status["source"] == "artifact_scan"
    assert status["dataset_id"] == "execution_risk_dataset_v1_scanned"
    assert status["artifact_path"] == str(new_artifact)
    assert status["artifact_exists"] is True
    assert status["trained_targets"] == ["can_fill", "likely_slippage_high"]
    assert _as_int(status["history_count"]) == 1
    assert _as_int(status["runtime_state_history_count"]) == 0

    history = _as_mapping(service.execution_risk_training_history(limit=10))
    assert _as_int(history["records"]) == 1
    latest = _as_mapping(cast(list[object], history["items"])[-1])
    assert latest["source"] == "artifact_scan"
    assert latest["dataset_id"] == "execution_risk_dataset_v1_scanned"


def test_execution_risk_history_does_not_promote_older_scanned_artifact(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config)
    artifact_root = Path(config.training.bootstrap_state_path).parent / "execution_risk"
    old_artifact = artifact_root / "execution_risk_20260528T152503.json"
    _write_execution_risk_artifact(
        old_artifact,
        dataset_id="execution_risk_dataset_v1_old_scanned",
        trained_targets=["can_fill"],
        created_at="2026-05-28T15:25:03",
    )
    blocked_record = {
        "timestamp": "2026-05-28T16:00:00",
        "status": "blocked_no_trainable_targets",
        "artifact_path": "",
        "dataset_id": "execution_risk_dataset_v1_blocked",
        "trained_targets": [],
    }
    _patch_attr(service, "_last_execution_risk_training", blocked_record)
    service._execution_risk_training_history.append(blocked_record)  # noqa: SLF001
    service._persist_runtime_state_to_disk()  # noqa: SLF001

    status = _as_mapping(service.execution_risk_status())
    assert status["source"] == "runtime_state"
    assert status["dataset_id"] == "execution_risk_dataset_v1_blocked"

    history = _as_mapping(service.execution_risk_training_history(limit=1))
    assert _as_int(history["records"]) == 1
    latest = _as_mapping(cast(list[object], history["items"])[-1])
    assert latest["status"] == "blocked_no_trainable_targets"
    assert latest["dataset_id"] == "execution_risk_dataset_v1_blocked"


def test_execution_risk_status_reports_latest_scanned_artifact_load_error(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config)
    artifact_root = Path(config.training.bootstrap_state_path).parent / "execution_risk"
    older_artifact = artifact_root / "execution_risk_20260528T152503.json"
    _write_execution_risk_artifact(
        older_artifact,
        dataset_id="execution_risk_dataset_v1_older_valid",
        trained_targets=["can_fill"],
        created_at="2026-05-28T15:25:03",
    )
    latest_broken_artifact = artifact_root / "execution_risk_20260528T160000.json"
    latest_broken_artifact.parent.mkdir(parents=True, exist_ok=True)
    latest_broken_artifact.write_text("{broken", encoding="utf-8")

    status = _as_mapping(service.execution_risk_status())

    assert status["source"] == "artifact_scan"
    assert status["dataset_id"] == "execution_risk_dataset_v1_older_valid"
    assert status["artifact_scan_error_path"] == str(latest_broken_artifact)
    assert str(status["artifact_scan_error"])


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


def test_runtime_state_preserves_portfolio_broker_snapshot_source(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config)
    service._portfolio.restore_state(  # noqa: SLF001
        {
            "positions": [
                {
                    "symbol": "600000",
                    "strategy": "manual",
                    "target_position": 0.2,
                    "opened_at": "2026-03-01T09:30:00",
                    "updated_at": "2026-03-01T09:30:00",
                    "open_trace_id": "runtime-state-sim-snapshot-source",
                    "open_reason": "manual",
                    "quantity": 1000,
                    "account": "sim-main",
                }
            ],
            "trades": [],
        }
    )

    snapshot = service.bootstrap_broker_snapshot_from_portfolio(
        source_trace_id="runtime-state-sim-snapshot-source",
    )
    assert snapshot["status"] == "ok"

    reloaded = _new_service(config)
    assert reloaded._broker_positions == {"600000.SH": 0.2}  # noqa: SLF001
    assert reloaded._broker_snapshot_source == "portfolio"  # noqa: SLF001


def test_runtime_state_treats_legacy_broker_snapshot_source_as_manual(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    runtime_state_path = Path(config.command_channel.state_persist_path)
    runtime_state_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_state_path.write_text(
        json.dumps(
            {
                "state_version": 7,
                "updated_at": "2026-03-01T15:30:00",
                "broker_snapshot_updated_at": "2026-03-01T15:30:00",
                "broker_positions": {"600000": 0.2},
                "broker_position_details": {
                    "600000": {
                        "target_position": 0.2,
                        "quantity": 1000,
                        "account": "manual",
                    }
                },
                "portfolio": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    service = _new_service(config)

    assert service._broker_positions == {"600000.SH": 0.2}  # noqa: SLF001
    assert service._broker_snapshot_source == "manual"  # noqa: SLF001
