"""Run Week8 production-hardening checks and release-gate rehearsal.

This script is intended for local dry-run validation.
It writes one consolidated report under artifacts/evolution/rehearsal/.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from stock_analyzer.config import get_config
from stock_analyzer.runtime.service import StockAnalyzerService


def _ensure_release_identifiers(config: Any) -> None:
    evolution = config.evolution
    if str(evolution.code_commit_id).strip() in {"", "unknown"}:
        evolution.code_commit_id = f"git:local-rehearsal-{datetime.now().strftime('%Y%m%d')}"
    if not str(evolution.active_champion_id).strip():
        evolution.active_champion_id = "champion_local_rehearsal"


def _safe_ticket_id(payload: dict[str, object]) -> str:
    ticket = payload.get("ticket", {})
    if not isinstance(ticket, dict):
        return ""
    return str(ticket.get("ticket_id", ""))


def main() -> None:
    base_config = get_config()
    _ensure_release_identifiers(base_config)
    sandbox_root = Path("artifacts/evolution/rehearsal/sandbox")
    suggestions_sandbox = Path("suggestions/rehearsal_sandbox")
    if sandbox_root.exists():
        shutil.rmtree(sandbox_root)
    if suggestions_sandbox.exists():
        shutil.rmtree(suggestions_sandbox)
    sandbox_root.mkdir(parents=True, exist_ok=True)
    suggestions_sandbox.mkdir(parents=True, exist_ok=True)
    base_config.evolution.report_dir = str(sandbox_root / "history")
    base_config.evolution.suggestions_dir = str(suggestions_sandbox)
    base_config.evolution.manifest_path = str(sandbox_root / "run_manifest.json")
    base_config.evolution.compliance_db_path = str(sandbox_root / "compliance.duckdb")
    base_config.evolution.m2_state_path = str(sandbox_root / "m2_state.json")
    base_config.evolution.m3_store_dir = str(sandbox_root / "m3")

    strict_config = base_config.model_copy(deep=True)
    strict_config.evolution.strict_dependency_check = True
    strict_service = StockAnalyzerService(config=strict_config)
    strict_preflight = strict_service.evolution_preflight()

    active_mode = "strict"
    fallback_preflight: dict[str, object] | None = None
    active_service = strict_service
    if not bool(strict_preflight.get("ready", False)):
        rehearsal_config = base_config.model_copy(deep=True)
        rehearsal_config.evolution.strict_dependency_check = False
        rehearsal_config.evolution.dependency_required_cli = []
        rehearsal_config.evolution.dependency_required_modules = []
        _ensure_release_identifiers(rehearsal_config)
        active_service = StockAnalyzerService(config=rehearsal_config)
        fallback_preflight = active_service.evolution_preflight()
        active_mode = "fallback_local_rehearsal"

    base_time = datetime.now().replace(hour=20, minute=40, second=0, microsecond=0)
    drill_runs: list[dict[str, object]] = []
    for idx in range(6):
        report = active_service.run_evolution_drill(
            timestamp=base_time + timedelta(minutes=idx),
            source_trace_id=f"week8-rehearsal-drill-{idx + 1}",
        )
        drill_runs.append(
            {
                "run_id": str(report.get("run_id", "")),
                "timestamp": str(report.get("timestamp", "")),
                "dry_run": bool(report.get("dry_run", True)),
            }
        )

    window_report = active_service.evolution_window_report(
        days=10,
        min_runs=5,
        now=base_time + timedelta(minutes=10),
    )

    release_attempt = active_service.attempt_evolution_release(
        days=10,
        min_runs=5,
        now=base_time + timedelta(minutes=11),
        source_trace_id="week8-rehearsal-release-attempt-1",
    )
    approval = active_service.record_evolution_release_approval(
        approver="risk_committee",
        approved=True,
        note="week8 rehearsal approval",
        timestamp=base_time + timedelta(minutes=12),
        source_trace_id="week8-rehearsal-approval-1",
    )
    ticket_issue_confirm = active_service.issue_evolution_release_ticket(
        operator="release_manager",
        note="week8 rehearsal path confirm",
        timestamp=base_time + timedelta(minutes=13),
        source_trace_id="week8-rehearsal-ticket-issue-1",
    )
    ticket_confirm_id = _safe_ticket_id(ticket_issue_confirm)
    ticket_execute_confirm = active_service.execute_evolution_release_ticket(
        executor="release_manager",
        ticket_id=ticket_confirm_id,
        note="week8 rehearsal execute for confirmation path",
        confirm_window=True,
        timestamp=base_time + timedelta(minutes=14),
        source_trace_id="week8-rehearsal-ticket-execute-1",
    )
    ticket_confirm = active_service.confirm_evolution_release_ticket(
        confirmer="risk_committee",
        ticket_id=ticket_confirm_id,
        note="week8 rehearsal manual confirmation",
        timestamp=base_time + timedelta(minutes=15),
        source_trace_id="week8-rehearsal-ticket-confirm-1",
    )

    release_attempt_2 = active_service.attempt_evolution_release(
        days=10,
        min_runs=5,
        now=base_time + timedelta(minutes=16),
        source_trace_id="week8-rehearsal-release-attempt-2",
    )
    approval_2 = active_service.record_evolution_release_approval(
        approver="risk_committee",
        approved=True,
        note="week8 rehearsal approval for rollback path",
        timestamp=base_time + timedelta(minutes=17),
        source_trace_id="week8-rehearsal-approval-2",
    )
    ticket_issue_rollback = active_service.issue_evolution_release_ticket(
        operator="release_manager",
        note="week8 rehearsal path rollback",
        timestamp=base_time + timedelta(minutes=18),
        source_trace_id="week8-rehearsal-ticket-issue-2",
    )
    ticket_rollback_id = _safe_ticket_id(ticket_issue_rollback)
    ticket_execute_rollback = active_service.execute_evolution_release_ticket(
        executor="release_manager",
        ticket_id=ticket_rollback_id,
        note="week8 rehearsal execute before rollback",
        confirm_window=True,
        timestamp=base_time + timedelta(minutes=19),
        source_trace_id="week8-rehearsal-ticket-execute-2",
    )
    ticket_rollback = active_service.rollback_evolution_release_ticket(
        rollback_by="risk_committee",
        ticket_id=ticket_rollback_id,
        note="week8 rehearsal rollback",
        timestamp=base_time + timedelta(minutes=20),
        source_trace_id="week8-rehearsal-ticket-rollback-2",
    )

    watchdog_report = active_service.run_evolution_release_confirmation_watchdog(
        now=base_time + timedelta(minutes=21),
        source_trace_id="week8-rehearsal-watchdog",
    )
    timeline_report = active_service.evolution_release_ticket_timeline(limit=50)

    summary = {
        "active_mode": active_mode,
        "strict_preflight_ready": bool(strict_preflight.get("ready", False)),
        "fallback_preflight_ready": (
            bool(fallback_preflight.get("ready", False))
            if isinstance(fallback_preflight, dict)
            else None
        ),
        "window_overall": str(window_report.get("overall", "")),
        "window_records": int(window_report.get("records", 0)),
        "release_attempt_1_accepted": bool(release_attempt.get("accepted", False)),
        "approval_1_accepted": bool(approval.get("accepted", False)),
        "ticket_confirm_path_issued": bool(ticket_issue_confirm.get("accepted", False)),
        "ticket_confirm_path_executed": bool(ticket_execute_confirm.get("accepted", False)),
        "ticket_confirm_path_confirmed": bool(ticket_confirm.get("accepted", False)),
        "release_attempt_2_accepted": bool(release_attempt_2.get("accepted", False)),
        "approval_2_accepted": bool(approval_2.get("accepted", False)),
        "ticket_rollback_path_issued": bool(ticket_issue_rollback.get("accepted", False)),
        "ticket_rollback_path_executed": bool(ticket_execute_rollback.get("accepted", False)),
        "ticket_rollback_path_rolled_back": bool(ticket_rollback.get("accepted", False)),
        "watchdog_rolled_back": int(watchdog_report.get("rolled_back", 0)),
        "timeline_records": int(timeline_report.get("records", 0)),
    }

    report = {
        "generated_at": datetime.now().isoformat(),
        "summary": summary,
        "hardening": {
            "strict_preflight": strict_preflight,
            "fallback_preflight": fallback_preflight,
            "window_report": window_report,
            "drill_runs": drill_runs,
        },
        "release_rehearsal": {
            "release_attempt_1": release_attempt,
            "approval_1": approval,
            "ticket_issue_confirm_path": ticket_issue_confirm,
            "ticket_execute_confirm_path": ticket_execute_confirm,
            "ticket_confirm": ticket_confirm,
            "release_attempt_2": release_attempt_2,
            "approval_2": approval_2,
            "ticket_issue_rollback_path": ticket_issue_rollback,
            "ticket_execute_rollback_path": ticket_execute_rollback,
            "ticket_rollback": ticket_rollback,
            "watchdog_report": watchdog_report,
            "timeline_report": timeline_report,
        },
    }

    report_dir = Path("artifacts/evolution/rehearsal")
    report_dir.mkdir(parents=True, exist_ok=True)
    report_name = f"week8_hardening_release_rehearsal_{datetime.now():%Y%m%d_%H%M%S}.json"
    report_path = report_dir / report_name
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        json.dumps(
            {"report_path": str(report_path), "summary": summary},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
