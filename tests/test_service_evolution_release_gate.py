from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.evolution.orchestrator import OffhoursEvolutionOrchestrator
from stock_analyzer.runtime.service import StockAnalyzerService


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    raise AssertionError(f"Expected mapping, got {type(value).__name__}")


def _as_mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        raise AssertionError(f"Expected list, got {type(value).__name__}")
    return [cast(Mapping[str, object], item) for item in value if isinstance(item, Mapping)]


def _as_text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        raise AssertionError(f"Expected list, got {type(value).__name__}")
    return [str(item) for item in value]


def _as_int(value: object) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    raise AssertionError(f"Expected numeric value, got {value!r}")


def _patch_attr(target: object, name: str, value: object) -> None:
    setattr(cast(Any, target), name, value)


def _build_release_service(
    tmp_path: Path,
    config: StockAnalyzerConfig,
    *,
    init_orchestrator: bool = True,
) -> StockAnalyzerService:
    service = StockAnalyzerService(config=config)
    service._evolution_project_root = tmp_path
    if init_orchestrator:
        service._evolution_orchestrator = OffhoursEvolutionOrchestrator(
            config=config.evolution,
            project_root=tmp_path,
        )
    return service


def _load_test_config(tmp_path: Path) -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.scheduler.premarket_time = "23:59"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "23:59"
    config.week5.auto_run = False
    config.week6.auto_run = False
    config.cloud_backup.enabled = False
    config.acceptance.auto_run = False
    config.evolution.enabled = True
    config.evolution.auto_run = False
    config.evolution.strict_dependency_check = False
    config.evolution.code_commit_id = "git:test"
    config.evolution.suggestions_dir = "suggestions"
    config.evolution.report_dir = "artifacts/evolution/history"
    config.evolution.manifest_path = "artifacts/evolution/run_manifest.json"
    config.evolution.compliance_db_path = "artifacts/evolution/compliance.duckdb"
    config.tdx_sync.refresh_before_evolution = False
    config.market_warehouse.refresh_before_evolution = False
    return config


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


def test_evolution_release_gate_approved_when_preflight_and_window_pass(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = _build_release_service(tmp_path, config)

    service.run_evolution_offhours(
        records=_valid_records(),
        timestamp=datetime.fromisoformat("2026-03-01T20:40:00"),
        dry_run=True,
        source_trace_id="release-gate-1",
    )
    service.run_evolution_offhours(
        records=_valid_records(),
        timestamp=datetime.fromisoformat("2026-03-02T20:40:00"),
        dry_run=True,
        source_trace_id="release-gate-2",
    )
    decision = _as_mapping(
        service.attempt_evolution_release(
            days=10,
            min_runs=2,
            now=datetime.fromisoformat("2026-03-03T12:00:00"),
            source_trace_id="release-gate-check",
        )
    )
    gate = _as_mapping(decision["gate"])
    assert decision["accepted"] is True
    assert decision["status"] == "approved"
    assert gate["window_overall"] in {"pass", "pass_with_warnings"}

    latest = service.latest_evolution_release_gate()
    assert latest is not None
    latest_view = _as_mapping(latest)
    assert latest_view["timestamp"] == decision["timestamp"]
    history = _as_mapping(service.evolution_release_gate_history(limit=10))
    assert _as_int(history["records"]) >= 1


def test_evolution_release_gate_blocked_when_preflight_not_ready(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    config.evolution.code_commit_id = "unknown"
    service = _build_release_service(tmp_path, config, init_orchestrator=False)

    decision = _as_mapping(
        service.attempt_evolution_release(
            days=10,
            min_runs=3,
            now=datetime.fromisoformat("2026-03-03T12:00:00"),
            source_trace_id="release-gate-blocked",
        )
    )
    gate = _as_mapping(decision["gate"])
    assert decision["accepted"] is False
    assert decision["status"] == "blocked"
    blockers = _as_text_list(gate["blockers"])
    assert "preflight_not_ready" in blockers
    runtime = _as_mapping(service.runtime_status())
    evolution = _as_mapping(runtime["evolution"])
    assert _as_int(evolution["release_gate_history_count"]) >= 1


def test_evolution_release_gate_not_blocked_by_preflight_with_git_auto_non_strict(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    config.evolution.code_commit_id = "git:auto"
    config.evolution.strict_dependency_check = False
    service = _build_release_service(tmp_path, config)

    service.run_evolution_offhours(
        records=_valid_records(),
        timestamp=datetime.fromisoformat("2026-03-01T20:40:00"),
        dry_run=True,
        source_trace_id="release-gate-auto-1",
    )
    service.run_evolution_offhours(
        records=_valid_records(),
        timestamp=datetime.fromisoformat("2026-03-02T20:40:00"),
        dry_run=True,
        source_trace_id="release-gate-auto-2",
    )
    decision = _as_mapping(
        service.attempt_evolution_release(
            days=10,
            min_runs=2,
            now=datetime.fromisoformat("2026-03-03T12:00:00"),
            source_trace_id="release-gate-auto-check",
        )
    )
    assert decision["accepted"] is True
    blockers = _as_text_list(_as_mapping(decision["gate"])["blockers"])
    assert "preflight_not_ready" not in blockers


def test_evolution_release_approval_and_ticket_flow(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = _build_release_service(tmp_path, config)

    service.run_evolution_offhours(
        records=_valid_records(),
        timestamp=datetime.fromisoformat("2026-03-01T20:40:00"),
        dry_run=True,
        source_trace_id="release-flow-1",
    )
    service.run_evolution_offhours(
        records=_valid_records(),
        timestamp=datetime.fromisoformat("2026-03-02T20:40:00"),
        dry_run=True,
        source_trace_id="release-flow-2",
    )
    gate = _as_mapping(
        service.attempt_evolution_release(
            days=10,
            min_runs=2,
            now=datetime.fromisoformat("2026-03-03T12:00:00"),
            source_trace_id="release-flow-gate",
        )
    )
    assert gate["accepted"] is True

    approval = _as_mapping(
        service.record_evolution_release_approval(
            approver="risk_committee",
            approved=True,
            note="all checks passed",
            timestamp=datetime.fromisoformat("2026-03-03T12:05:00"),
            source_trace_id="release-flow-approval",
        )
    )
    assert approval["accepted"] is True
    latest_approval = service.latest_evolution_release_approval()
    assert latest_approval is not None
    latest_approval_view = _as_mapping(latest_approval)
    assert latest_approval_view["approved"] is True

    ticket = _as_mapping(
        service.issue_evolution_release_ticket(
            operator="release_manager",
            note="issue manual release order",
            timestamp=datetime.fromisoformat("2026-03-03T12:10:00"),
            source_trace_id="release-flow-ticket",
        )
    )
    ticket_view = _as_mapping(ticket["ticket"])
    assert ticket["accepted"] is True
    assert ticket_view["status"] == "issued"

    executed = _as_mapping(
        service.execute_evolution_release_ticket(
            executor="release_manager",
            note="execution completed",
            confirm_window=True,
            timestamp=datetime.fromisoformat("2026-03-03T12:15:00"),
            source_trace_id="release-flow-ticket-execute",
        )
    )
    executed_ticket = _as_mapping(executed["ticket"])
    assert executed["accepted"] is True
    assert executed_ticket["status"] == "executed"
    execution = _as_mapping(executed_ticket["execution"])
    assert execution["executor"] == "release_manager"
    assert "compliance_update" in executed_ticket
    pending_confirmation = _as_mapping(executed_ticket["pending_confirmation"])
    assert pending_confirmation["state"] == "pending"
    assert pending_confirmation["required"] is True

    confirmed = _as_mapping(
        service.confirm_evolution_release_ticket(
            confirmer="risk_committee",
            note="post-release checks passed",
            timestamp=datetime.fromisoformat("2026-03-03T12:18:00"),
            source_trace_id="release-flow-ticket-confirm",
        )
    )
    assert confirmed["accepted"] is True
    confirmed_state = _as_mapping(_as_mapping(confirmed["ticket"])["pending_confirmation"])
    assert confirmed_state["state"] == "confirmed"
    assert confirmed_state["confirmed_by"] == "risk_committee"

    rollback = _as_mapping(
        service.rollback_evolution_release_ticket(
            rollback_by="risk_committee",
            note="post-check failed",
            timestamp=datetime.fromisoformat("2026-03-03T12:20:00"),
            source_trace_id="release-flow-ticket-rollback",
        )
    )
    rollback_ticket = _as_mapping(rollback["ticket"])
    assert rollback["accepted"] is True
    assert rollback_ticket["status"] == "rolled_back"
    rollback_info = _as_mapping(rollback_ticket["rollback"])
    assert rollback_info["rollback_by"] == "risk_committee"

    latest_ticket = service.latest_evolution_release_ticket()
    assert latest_ticket is not None
    latest_ticket_view = _as_mapping(latest_ticket)
    assert latest_ticket_view["operator"] == "release_manager"
    assert latest_ticket_view["status"] == "rolled_back"

    approval_history = _as_mapping(service.evolution_release_approval_history(limit=10))
    ticket_history = _as_mapping(service.evolution_release_ticket_history(limit=10))
    assert _as_int(approval_history["records"]) >= 1
    assert _as_int(ticket_history["records"]) >= 4
    timeline = _as_mapping(service.evolution_release_ticket_timeline(limit=20))
    assert _as_int(timeline["records"]) >= 1
    timeline_ticket = _as_mapping(_as_mapping_list(timeline["tickets"])[0])
    events = [str(item.get("event", "")) for item in _as_mapping_list(timeline_ticket["events"])]
    assert "issued" in events
    assert "executed" in events
    assert "confirmed" in events
    assert "rolled_back" in events
    rolled_back_timeline = _as_mapping(
        service.evolution_release_ticket_timeline(status="rolled_back", limit=20)
    )
    assert _as_int(rolled_back_timeline["records"]) >= 1


def test_evolution_release_flow_emits_execute_confirm_and_rollback_notifications(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    service = _build_release_service(tmp_path, config)
    notifications: list[dict[str, str]] = []

    def _fake_notify(
        title: str,
        content: str,
        level: str = "info",
        trace_id: str = "",
    ) -> dict[str, object]:
        notifications.append(
            {
                "title": title,
                "content": content,
                "level": level,
                "trace_id": trace_id,
            }
        )
        return {"ok": True}

    _patch_attr(service, "notify", _fake_notify)

    service.run_evolution_offhours(
        records=_valid_records(),
        timestamp=datetime.fromisoformat("2026-03-01T20:40:00"),
        dry_run=True,
        source_trace_id="release-notify-1",
    )
    service.run_evolution_offhours(
        records=_valid_records(),
        timestamp=datetime.fromisoformat("2026-03-02T20:40:00"),
        dry_run=True,
        source_trace_id="release-notify-2",
    )
    gate = _as_mapping(
        service.attempt_evolution_release(
            days=10,
            min_runs=2,
            now=datetime.fromisoformat("2026-03-03T12:00:00"),
            source_trace_id="release-notify-gate",
        )
    )
    assert gate["accepted"] is True
    approval = _as_mapping(
        service.record_evolution_release_approval(
            approver="risk_committee",
            approved=True,
            note="all checks passed",
            timestamp=datetime.fromisoformat("2026-03-03T12:05:00"),
            source_trace_id="release-notify-approval",
        )
    )
    assert approval["accepted"] is True
    ticket = _as_mapping(
        service.issue_evolution_release_ticket(
            operator="release_manager",
            note="issue manual release order",
            timestamp=datetime.fromisoformat("2026-03-03T12:10:00"),
            source_trace_id="release-notify-ticket",
        )
    )
    assert ticket["accepted"] is True

    executed = _as_mapping(
        service.execute_evolution_release_ticket(
            executor="release_manager",
            note="execution completed",
            confirm_window=True,
            timestamp=datetime.fromisoformat("2026-03-03T12:15:00"),
            source_trace_id="release-notify-execute",
        )
    )
    assert executed["accepted"] is True
    confirmed = _as_mapping(
        service.confirm_evolution_release_ticket(
            confirmer="risk_committee",
            note="post-release checks passed",
            timestamp=datetime.fromisoformat("2026-03-03T12:18:00"),
            source_trace_id="release-notify-confirm",
        )
    )
    assert confirmed["accepted"] is True
    rollback = _as_mapping(
        service.rollback_evolution_release_ticket(
            rollback_by="risk_committee",
            note="post-check failed",
            timestamp=datetime.fromisoformat("2026-03-03T12:20:00"),
            source_trace_id="release-notify-rollback",
        )
    )
    assert rollback["accepted"] is True

    assert any("升级已执行" in item["title"] for item in notifications)
    assert any("升级确认成功" in item["title"] for item in notifications)
    assert any("升级已回滚" in item["title"] for item in notifications)
    assert any("执行人：发布管理员" in item["content"] for item in notifications)
    assert any("确认人：风控委员会" in item["content"] for item in notifications)
    assert any("回滚人：风控委员会" in item["content"] for item in notifications)
    assert any("原状态：已执行" in item["content"] for item in notifications)
    assert any("回滚说明：发布后复核未通过" in item["content"] for item in notifications)


def test_evolution_release_approval_blocked_when_gate_not_passed(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    config.evolution.code_commit_id = "unknown"
    service = _build_release_service(tmp_path, config, init_orchestrator=False)

    _ = service.attempt_evolution_release(
        days=10,
        min_runs=3,
        now=datetime.fromisoformat("2026-03-03T12:00:00"),
        source_trace_id="release-flow-blocked-gate",
    )
    approval = service.record_evolution_release_approval(
        approver="risk_committee",
        approved=True,
        note="attempt approve",
        timestamp=datetime.fromisoformat("2026-03-03T12:05:00"),
    )
    assert approval["accepted"] is False
    assert approval["code"] == "gate_not_passed"


def test_evolution_release_ticket_execute_blocked_without_issued_ticket(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)

    report = service.execute_evolution_release_ticket(
        executor="release_manager",
        note="try execute",
        confirm_window=True,
        timestamp=datetime.fromisoformat("2026-03-03T13:00:00"),
    )
    assert report["accepted"] is False
    assert report["code"] == "missing_release_ticket"


def test_evolution_release_ticket_rollback_blocked_without_ticket(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)

    report = service.rollback_evolution_release_ticket(
        rollback_by="risk_committee",
        note="try rollback",
        timestamp=datetime.fromisoformat("2026-03-03T13:05:00"),
    )
    assert report["accepted"] is False
    assert report["code"] == "missing_release_ticket"


def test_evolution_release_confirmation_watchdog_auto_rollbacks_overdue_ticket(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    service = _build_release_service(tmp_path, config)

    service.run_evolution_offhours(
        records=_valid_records(),
        timestamp=datetime.fromisoformat("2026-03-01T20:40:00"),
        dry_run=True,
        source_trace_id="watchdog-flow-1",
    )
    service.run_evolution_offhours(
        records=_valid_records(),
        timestamp=datetime.fromisoformat("2026-03-02T20:40:00"),
        dry_run=True,
        source_trace_id="watchdog-flow-2",
    )
    gate = service.attempt_evolution_release(
        days=10,
        min_runs=2,
        now=datetime.fromisoformat("2026-03-03T12:00:00"),
        source_trace_id="watchdog-flow-gate",
    )
    assert gate["accepted"] is True
    approval = service.record_evolution_release_approval(
        approver="risk_committee",
        approved=True,
        note="approve",
        timestamp=datetime.fromisoformat("2026-03-03T12:05:00"),
        source_trace_id="watchdog-flow-approval",
    )
    assert approval["accepted"] is True
    ticket = service.issue_evolution_release_ticket(
        operator="release_manager",
        note="issue",
        timestamp=datetime.fromisoformat("2026-03-03T12:10:00"),
        source_trace_id="watchdog-flow-ticket",
    )
    assert ticket["accepted"] is True
    execute = service.execute_evolution_release_ticket(
        executor="release_manager",
        note="execute",
        confirm_window=True,
        timestamp=datetime.fromisoformat("2026-03-03T12:15:00"),
        source_trace_id="watchdog-flow-execute",
    )
    assert execute["accepted"] is True

    watchdog = _as_mapping(
        service.run_evolution_release_confirmation_watchdog(
            now=datetime.fromisoformat("2026-03-07T12:16:00"),
            source_trace_id="watchdog-run",
        )
    )
    assert _as_int(watchdog["checked"]) >= 1
    assert _as_int(watchdog["overdue"]) >= 1
    assert _as_int(watchdog["rolled_back"]) >= 1
    latest = service.latest_evolution_release_ticket()
    assert latest is not None
    latest_view = _as_mapping(latest)
    assert latest_view["status"] == "rolled_back"
