"""Evolution release workflows extracted from the runtime service."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, cast

from stock_analyzer.evolution.governance.compliance import (
    ComplianceEvent,
    ComplianceLogger,
    ComplianceState,
)
from stock_analyzer.evolution.ops.preflight import run_evolution_preflight

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService


_EVOLUTION_WINDOW_REPORT_CACHE_TTL_SEC = 300.0


class RuntimeEvolutionReleaseService:
    """Delegated evolution release, approval, and watchdog workflows."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service

    def evolution_preflight(self) -> dict[str, object]:
        """Run evolution production preflight checks."""
        service = self._service
        report = run_evolution_preflight(
            config=service._config.evolution,
            project_root=service._evolution_project_root,
        )
        payload = report.model_dump(mode="json")
        service._record_audit_event(
            event_type="evolution_preflight",
            payload={
                "ready": report.ready,
                "strict_dependency_check": report.strict_dependency_check,
                "blockers": report.blockers,
            },
        )
        return cast(dict[str, object], payload)

    def evolution_window_report(
        self,
        days: int = 10,
        min_runs: int = 5,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Build validation report for pre-production dry-run window."""
        service = self._service
        current = now or datetime.now()
        window_days = max(1, days)
        required_runs = max(1, min_runs)
        cutoff_ts = current.timestamp() - window_days * 86400
        cache_key = _evolution_window_report_cache_key(
            window_days=window_days,
            min_runs=required_runs,
            current=current,
        )
        cache_fingerprint = _evolution_window_report_cache_fingerprint(
            service._resolve_evolution_path(service._config.evolution.report_dir)
        )
        cached = _load_evolution_window_report_cache(
            service,
            cache_key=cache_key,
            fingerprint=cache_fingerprint,
        )
        if cached is not None:
            return cached

        memory_reports = [
            report
            for report in service._evolution_history
            if _report_timestamp(report) >= cutoff_ts
        ]
        disk_reports = service._load_evolution_reports_from_disk(cutoff_ts=cutoff_ts)
        merged = _merge_evolution_reports(memory_reports=memory_reports, disk_reports=disk_reports)

        checks: list[dict[str, object]] = []
        checks.append(
            _make_check(
                name="window_runs_count",
                status="pass" if len(merged) >= required_runs else "fail",
                detail=f"runs={len(merged)}, required>={required_runs}",
            )
        )

        require_dry_run = bool(service._config.evolution.validation_require_dry_run)
        dry_run_values = [bool(item.get("dry_run", False)) for item in merged]
        all_dry_run = all(dry_run_values) if merged else True
        all_live_run = all(not value for value in dry_run_values) if merged else False
        dry_run_consistent = len(set(dry_run_values)) <= 1 if merged else True
        if require_dry_run:
            dry_run_status = "pass" if all_dry_run else "fail"
            dry_run_detail = "all reports must keep dry_run=true during validation window"
        else:
            dry_run_status = "pass" if dry_run_consistent else "fail"
            if dry_run_consistent:
                mode_text = (
                    "dry_run=true"
                    if all_dry_run
                    else ("dry_run=false" if all_live_run else "dry_run=mixed")
                )
                dry_run_detail = (
                    "validation_require_dry_run=false; reports must remain dry_run-consistent "
                    f"within window ({mode_text})"
                )
            else:
                dry_run_detail = (
                    "validation_require_dry_run=false; mixed dry_run modes in validation window"
                )
        checks.append(
            _make_check(
                name="dry_run_consistency",
                status=dry_run_status,
                detail=dry_run_detail,
            )
        )

        missing_payload = 0
        missing_manifest = 0
        missing_states = 0
        invalid_flow = 0
        retry_pending = 0
        invalidated = 0
        for item in merged:
            proposal = item.get("proposal", {})
            if isinstance(proposal, dict):
                payload_uri = proposal.get("payload_uri")
                if isinstance(payload_uri, str) and payload_uri.strip():
                    payload_path = service._resolve_evolution_path(payload_uri)
                    if not payload_path.exists():
                        missing_payload += 1
                else:
                    missing_payload += 1
            else:
                missing_payload += 1

            manifest_raw = item.get("manifest_path")
            if isinstance(manifest_raw, str) and manifest_raw.strip():
                manifest_path = Path(manifest_raw)
                if not manifest_path.is_absolute():
                    manifest_path = service._resolve_evolution_path(manifest_raw)
                if not manifest_path.exists():
                    missing_manifest += 1
            else:
                missing_manifest += 1

            compliance = item.get("compliance", {})
            states: list[str] = []
            if isinstance(compliance, dict):
                raw_states = compliance.get("states", [])
                if isinstance(raw_states, list):
                    states = [str(state) for state in raw_states]
            if not states:
                missing_states += 1
            else:
                if "generated" not in states or "validated" not in states:
                    invalid_flow += 1
                if "retry_pending" in states:
                    retry_pending += 1
                if "invalidated" in states:
                    invalidated += 1

        artifact_ok = missing_payload == 0 and missing_manifest == 0
        checks.append(
            _make_check(
                name="artifact_integrity",
                status="pass" if artifact_ok else "fail",
                detail=f"missing_payload={missing_payload}, missing_manifest={missing_manifest}",
            )
        )

        flow_ok = missing_states == 0 and invalid_flow == 0
        checks.append(
            _make_check(
                name="compliance_flow",
                status="pass" if flow_ok else "fail",
                detail=f"missing_states={missing_states}, invalid_flow={invalid_flow}",
            )
        )

        retry_ratio = (retry_pending + invalidated) / len(merged) if merged else 0.0
        checks.append(
            _make_check(
                name="stability_signal",
                status="warn" if retry_ratio > 0.30 else "pass",
                detail=(
                    f"retry_or_invalidated={retry_pending + invalidated}, "
                    f"total_runs={len(merged)}, ratio={retry_ratio:.2f}"
                ),
            )
        )

        fail_count = sum(1 for item in checks if str(item.get("status", "")) == "fail")
        warn_count = sum(1 for item in checks if str(item.get("status", "")) == "warn")
        overall = "fail" if fail_count > 0 else ("pass_with_warnings" if warn_count > 0 else "pass")

        payload = {
            "window_days": window_days,
            "min_runs": required_runs,
            "records": len(merged),
            "overall": overall,
            "checks": checks,
            "summary": {
                "fail_count": fail_count,
                "warn_count": warn_count,
                "retry_pending_runs": retry_pending,
                "invalidated_runs": invalidated,
            },
        }
        _store_evolution_window_report_cache(
            service,
            cache_key=cache_key,
            fingerprint=cache_fingerprint,
            payload=payload,
        )
        return payload

    def attempt_evolution_release(
        self,
        days: int = 10,
        min_runs: int = 5,
        now: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        """Run release gate checks and return blocking decision."""
        service = self._service
        current = now or datetime.now()
        window_days = max(1, days)
        required_runs = max(1, min_runs)
        preflight = service.evolution_preflight()
        window_report = service.evolution_window_report(
            days=window_days,
            min_runs=required_runs,
            now=current,
        )
        latest = service.latest_evolution_report()

        blockers: list[str] = []
        if not bool(preflight.get("ready", False)):
            blockers.append("preflight_not_ready")
        if str(window_report.get("overall", "")) == "fail":
            blockers.append("window_report_fail")
        if latest is None:
            blockers.append("missing_latest_evolution_run")

        accepted = len(blockers) == 0
        status = "approved" if accepted else "blocked"
        code = "ok" if accepted else blockers[0]
        message = (
            "release gate approved: ready for manual governance review"
            if accepted
            else f"release gate blocked: {', '.join(blockers)}"
        )

        latest_snapshot: dict[str, object] = {
            "run_id": "",
            "timestamp": "",
            "dry_run": True,
            "proposal_id": "",
            "authorization_level": "",
        }
        if isinstance(latest, dict):
            proposal = latest.get("proposal", {})
            if not isinstance(proposal, dict):
                proposal = {}
            latest_snapshot = {
                "run_id": str(latest.get("run_id", "")),
                "timestamp": str(latest.get("timestamp", "")),
                "dry_run": bool(latest.get("dry_run", True)),
                "proposal_id": str(proposal.get("proposal_id", "")),
                "authorization_level": str(proposal.get("authorization_level", "")),
            }

        preflight_dependency = preflight.get("dependency", {})
        if not isinstance(preflight_dependency, dict):
            preflight_dependency = {}
        preflight_blockers = preflight.get("blockers", [])
        if not isinstance(preflight_blockers, list):
            preflight_blockers = []
        preflight_paths = preflight.get("path_checks", [])
        if not isinstance(preflight_paths, list):
            preflight_paths = []

        window_checks = window_report.get("checks", [])
        if not isinstance(window_checks, list):
            window_checks = []
        window_summary = window_report.get("summary", {})
        if not isinstance(window_summary, dict):
            window_summary = {}

        decision = {
            "timestamp": current.isoformat(),
            "accepted": accepted,
            "status": status,
            "code": code,
            "message": message,
            "source_trace_id": source_trace_id,
            "gate": {
                "window_days": window_days,
                "min_runs": required_runs,
                "preflight_ready": bool(preflight.get("ready", False)),
                "window_overall": str(window_report.get("overall", "")),
                "window_records": _as_int(window_report.get("records"), default=0),
                "blockers": blockers,
            },
            "latest": latest_snapshot,
            "preflight": {
                "ready": bool(preflight.get("ready", False)),
                "blockers": preflight_blockers,
                "dependency": preflight_dependency,
                "path_checks": preflight_paths,
            },
            "window_report": {
                "overall": str(window_report.get("overall", "")),
                "records": _as_int(window_report.get("records"), default=0),
                "summary": window_summary,
                "checks": window_checks,
            },
        }

        service._last_evolution_release_gate = decision
        service._evolution_release_gate_history.append(decision)
        history_limit = max(1, service._config.evolution.history_limit)
        if len(service._evolution_release_gate_history) > history_limit:
            overflow = len(service._evolution_release_gate_history) - history_limit
            if overflow > 0:
                service._evolution_release_gate_history = service._evolution_release_gate_history[
                    overflow:
                ]

        service._record_audit_event(
            event_type="evolution_release_gate",
            trace_id=source_trace_id,
            level="warn" if not accepted else "info",
            payload={
                "accepted": accepted,
                "code": code,
                "blockers": blockers,
                "latest": latest_snapshot,
            },
        )
        return decision

    def latest_evolution_release_gate(self) -> dict[str, object] | None:
        service = self._service
        report = service._last_evolution_release_gate
        return report if isinstance(report, dict) else None

    def evolution_release_gate_history(self, limit: int = 20) -> dict[str, object]:
        service = self._service
        capped = max(1, min(limit, max(1, service._config.evolution.history_limit)))
        recent = service._evolution_release_gate_history[-capped:]
        return {"records": len(recent), "items": recent}

    def record_evolution_release_approval(
        self,
        approver: str,
        approved: bool,
        note: str = "",
        timestamp: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        service = self._service
        now = timestamp or datetime.now()
        normalized_approver = approver.strip()
        if not normalized_approver:
            return {
                "accepted": False,
                "code": "invalid_approver",
                "message": "approver is required",
            }

        latest_gate = service._last_evolution_release_gate
        if latest_gate is None:
            return {
                "accepted": False,
                "code": "missing_release_gate",
                "message": "run evolution release gate attempt before approval",
            }
        gate_passed = bool(latest_gate.get("accepted", False))
        gate_info = latest_gate.get("gate", {})
        if not isinstance(gate_info, dict):
            gate_info = {}
        if approved and not gate_passed:
            return {
                "accepted": False,
                "code": "gate_not_passed",
                "message": "cannot approve release when gate decision is blocked",
                "gate": gate_info,
            }

        approval_id = (
            f"EVO-APR-{now.strftime('%Y%m%d%H%M%S')}-"
            f"{len(service._evolution_release_approval_history) + 1:04d}"
        )
        latest = service._last_evolution_report
        latest_snapshot: dict[str, object] = {
            "run_id": "",
            "proposal_id": "",
            "authorization_level": "",
            "timestamp": "",
            "dry_run": True,
        }
        if isinstance(latest, dict):
            proposal = latest.get("proposal", {})
            if not isinstance(proposal, dict):
                proposal = {}
            latest_snapshot = {
                "run_id": str(latest.get("run_id", "")),
                "proposal_id": str(proposal.get("proposal_id", "")),
                "authorization_level": str(proposal.get("authorization_level", "")),
                "timestamp": str(latest.get("timestamp", "")),
                "dry_run": bool(latest.get("dry_run", True)),
            }

        record = {
            "approval_id": approval_id,
            "timestamp": now.isoformat(),
            "approved": approved,
            "approver": normalized_approver,
            "note": note.strip(),
            "source_trace_id": source_trace_id,
            "gate": {
                "accepted": gate_passed,
                "status": str(latest_gate.get("status", "")),
                "code": str(latest_gate.get("code", "")),
                "window_days": _as_int(gate_info.get("window_days"), default=0),
                "min_runs": _as_int(gate_info.get("min_runs"), default=0),
            },
            "latest": latest_snapshot,
        }
        service._last_evolution_release_approval = record
        service._evolution_release_approval_history.append(record)
        history_limit = max(1, service._config.evolution.history_limit)
        if len(service._evolution_release_approval_history) > history_limit:
            overflow = len(service._evolution_release_approval_history) - history_limit
            if overflow > 0:
                service._evolution_release_approval_history = (
                    service._evolution_release_approval_history[overflow:]
                )

        service._record_audit_event(
            event_type="evolution_release_approval",
            trace_id=source_trace_id,
            level="warn" if not approved else "info",
            payload={
                "approval_id": approval_id,
                "approved": approved,
                "approver": normalized_approver,
                "gate_passed": gate_passed,
            },
        )
        return {"accepted": True, "record": record}

    def latest_evolution_release_approval(self) -> dict[str, object] | None:
        service = self._service
        report = service._last_evolution_release_approval
        return report if isinstance(report, dict) else None

    def evolution_release_approval_history(self, limit: int = 20) -> dict[str, object]:
        service = self._service
        capped = max(1, min(limit, max(1, service._config.evolution.history_limit)))
        recent = service._evolution_release_approval_history[-capped:]
        return {"records": len(recent), "items": recent}

    def issue_evolution_release_ticket(
        self,
        operator: str,
        note: str = "",
        timestamp: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        service = self._service
        now = timestamp or datetime.now()
        normalized_operator = operator.strip()
        if not normalized_operator:
            return {
                "accepted": False,
                "code": "invalid_operator",
                "message": "operator is required",
            }

        latest_gate = service._last_evolution_release_gate
        if latest_gate is None or not bool(latest_gate.get("accepted", False)):
            return {
                "accepted": False,
                "code": "gate_not_passed",
                "message": "release gate must be approved before issuing ticket",
            }

        latest_approval = service._last_evolution_release_approval
        if latest_approval is None:
            return {
                "accepted": False,
                "code": "missing_manual_approval",
                "message": "manual approval record is required before issuing ticket",
            }
        if not bool(latest_approval.get("approved", False)):
            return {
                "accepted": False,
                "code": "manual_approval_rejected",
                "message": "latest manual approval is rejected",
            }

        latest = service._last_evolution_report
        if latest is None:
            return {
                "accepted": False,
                "code": "missing_latest_evolution_run",
                "message": "no evolution run available for release ticket",
            }

        proposal = latest.get("proposal", {})
        if not isinstance(proposal, dict):
            proposal = {}
        payload_uri = str(proposal.get("payload_uri", ""))
        latest_symbol = str(latest.get("symbol", "")).strip()
        if not latest_symbol:
            raw_symbols = latest.get("symbols", [])
            if isinstance(raw_symbols, list) and raw_symbols:
                candidate = raw_symbols[0]
                if isinstance(candidate, str):
                    latest_symbol = candidate.strip()
        if not latest_symbol:
            latest_symbol = "UNKNOWN"

        ticket_id = (
            f"EVO-TKT-{now.strftime('%Y%m%d%H%M%S')}-"
            f"{len(service._evolution_release_ticket_history) + 1:04d}"
        )
        checklist = [
            {"name": "release_gate_passed", "done": True},
            {"name": "manual_approval_recorded", "done": True},
            {
                "name": "proposal_artifact_ready",
                "done": bool(payload_uri),
            },
            {
                "name": "execution_window_confirmed",
                "done": False,
            },
        ]
        ticket = {
            "ticket_id": ticket_id,
            "timestamp": now.isoformat(),
            "status": "issued",
            "manual_execution_required": True,
            "operator": normalized_operator,
            "note": note.strip(),
            "source_trace_id": source_trace_id,
            "gate": {
                "status": str(latest_gate.get("status", "")),
                "code": str(latest_gate.get("code", "")),
                "accepted": True,
            },
            "approval": {
                "approval_id": str(latest_approval.get("approval_id", "")),
                "approver": str(latest_approval.get("approver", "")),
                "approved": True,
                "timestamp": str(latest_approval.get("timestamp", "")),
            },
            "release_payload": {
                "run_id": str(latest.get("run_id", "")),
                "timestamp": str(latest.get("timestamp", "")),
                "proposal_id": str(proposal.get("proposal_id", "")),
                "payload_uri": payload_uri,
                "symbol": latest_symbol,
                "authorization_level": str(proposal.get("authorization_level", "")),
                "dry_run": bool(latest.get("dry_run", True)),
            },
            "checklist": checklist,
        }
        service._last_evolution_release_ticket = ticket
        service._evolution_release_ticket_history.append(ticket)
        history_limit = max(1, service._config.evolution.history_limit)
        if len(service._evolution_release_ticket_history) > history_limit:
            overflow = len(service._evolution_release_ticket_history) - history_limit
            if overflow > 0:
                service._evolution_release_ticket_history = (
                    service._evolution_release_ticket_history[overflow:]
                )

        service._record_audit_event(
            event_type="evolution_release_ticket",
            trace_id=source_trace_id,
            payload={
                "ticket_id": ticket_id,
                "operator": normalized_operator,
                "proposal_id": str(proposal.get("proposal_id", "")),
            },
        )
        return {"accepted": True, "ticket": ticket}

    def execute_evolution_release_ticket(
        self,
        executor: str,
        ticket_id: str = "",
        note: str = "",
        confirm_window: bool = True,
        timestamp: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        service = self._service
        now = timestamp or datetime.now()
        normalized_executor = executor.strip()
        if not normalized_executor:
            return {
                "accepted": False,
                "code": "invalid_executor",
                "message": "executor is required",
            }

        if not confirm_window:
            return {
                "accepted": False,
                "code": "execution_window_not_confirmed",
                "message": "execution window must be confirmed before closing ticket",
            }

        normalized_ticket_id = ticket_id.strip()
        target_ticket: dict[str, object] | None = None
        if normalized_ticket_id:
            for item in reversed(service._evolution_release_ticket_history):
                if str(item.get("ticket_id", "")) == normalized_ticket_id:
                    target_ticket = item
                    break
            if target_ticket is None:
                return {
                    "accepted": False,
                    "code": "ticket_not_found",
                    "message": f"ticket_id={normalized_ticket_id} not found",
                }
        else:
            target_ticket = service._last_evolution_release_ticket
            if target_ticket is None:
                return {
                    "accepted": False,
                    "code": "missing_release_ticket",
                    "message": "issue release ticket before execution close-out",
                }

        current_status = str(target_ticket.get("status", "")).strip().lower()
        if current_status != "issued":
            return {
                "accepted": False,
                "code": "ticket_not_issued",
                "message": (
                    "release ticket is not in issued status; "
                    f"current_status={current_status or 'unknown'}"
                ),
                "ticket_id": str(target_ticket.get("ticket_id", "")),
            }

        ticket = deepcopy(target_ticket)
        raw_checklist = ticket.get("checklist", [])
        checklist: list[dict[str, object]] = []
        if isinstance(raw_checklist, list):
            for item in raw_checklist:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                checklist.append(
                    {
                        "name": name,
                        "done": bool(item.get("done", False)),
                    }
                )

        found_window = False
        found_completed = False
        found_manual_confirmation = False
        for item in checklist:
            name = str(item.get("name", ""))
            if name == "execution_window_confirmed":
                item["done"] = True
                found_window = True
            if name == "manual_execution_completed":
                item["done"] = True
                found_completed = True
            if name == "manual_confirmation_received":
                item["done"] = False
                found_manual_confirmation = True

        if not found_window:
            checklist.append({"name": "execution_window_confirmed", "done": True})
        if not found_completed:
            checklist.append({"name": "manual_execution_completed", "done": True})
        if not found_manual_confirmation:
            checklist.append({"name": "manual_confirmation_received", "done": False})

        executed_at = now.isoformat()
        execution_note = note.strip()
        ticket["status"] = "executed"
        ticket["executed_at"] = executed_at
        ticket["checklist"] = checklist
        ticket["execution"] = {
            "executor": normalized_executor,
            "timestamp": executed_at,
            "note": execution_note,
            "source_trace_id": source_trace_id,
        }
        confirmation_required = bool(service._config.evolution.release_confirmation_required)
        confirmation_ttl_days = max(1, service._config.evolution.release_confirmation_ttl_days)
        if confirmation_required:
            due_at = (now + timedelta(days=confirmation_ttl_days)).isoformat()
            ticket["pending_confirmation"] = {
                "required": True,
                "state": "pending",
                "pending_since": executed_at,
                "ttl_days": confirmation_ttl_days,
                "due_at": due_at,
                "confirmed_by": "",
                "confirmed_at": "",
                "confirmation_note": "",
            }
        else:
            ticket["pending_confirmation"] = {
                "required": False,
                "state": "not_required",
                "pending_since": "",
                "ttl_days": confirmation_ttl_days,
                "due_at": "",
                "confirmed_by": "",
                "confirmed_at": "",
                "confirmation_note": "",
            }
        release_payload = ticket.get("release_payload", {})
        if not isinstance(release_payload, dict):
            release_payload = {}
        proposal_id = str(release_payload.get("proposal_id", "")).strip()
        symbol = str(release_payload.get("symbol", "UNKNOWN")).strip() or "UNKNOWN"
        dry_run = bool(release_payload.get("dry_run", True))
        if proposal_id and not dry_run:
            compliance_update = service._write_evolution_compliance_event(
                state=ComplianceState.PROMOTED,
                proposal_id=proposal_id,
                symbol=symbol,
                event_time=now,
                trace_id=source_trace_id,
                code_commit_id=str(service._config.evolution.code_commit_id),
                metadata={
                    "ticket_id": str(ticket.get("ticket_id", "")),
                    "executor": normalized_executor,
                    "confirmation_required": confirmation_required,
                },
            )
        else:
            compliance_update = {
                "state": ComplianceState.PROMOTED.value,
                "written": False,
                "skipped": True,
                "reason": "dry_run_release_payload" if dry_run else "missing_proposal_id",
            }
        ticket["compliance_update"] = compliance_update

        service._last_evolution_release_ticket = ticket
        service._evolution_release_ticket_history.append(ticket)
        history_limit = max(1, service._config.evolution.history_limit)
        if len(service._evolution_release_ticket_history) > history_limit:
            overflow = len(service._evolution_release_ticket_history) - history_limit
            if overflow > 0:
                service._evolution_release_ticket_history = (
                    service._evolution_release_ticket_history[overflow:]
                )

        service._record_audit_event(
            event_type="evolution_release_ticket_execute",
            trace_id=source_trace_id,
            payload={
                "ticket_id": str(ticket.get("ticket_id", "")),
                "executor": normalized_executor,
                "executed_at": executed_at,
                "compliance": compliance_update,
            },
        )
        proposal_id_text = proposal_id or "-"
        if confirmation_required:
            pending_confirmation = ticket.get("pending_confirmation", {})
            if not isinstance(pending_confirmation, dict):
                pending_confirmation = {}
            due_at = _format_notification_time_zh(str(pending_confirmation.get("due_at", "")))
            service.notify(
                title=_push_title(priority="P2", category="evolution", summary="release executed"),
                content=_notification_message_zh(
                    trigger="升级发布单已执行完成，系统进入人工确认等待阶段。",
                    impact="新版本已经落地，但在人工确认通过前仍处于受监控状态，必要时可立即回滚。",
                    action="请在确认截止时间前完成发布后复核；若发现异常，请直接执行回滚并保留记录。",
                    details=[
                        f"票据编号：{ticket.get('ticket_id', '')}",
                        f"提案编号：{proposal_id_text}",
                        f"执行人：{_translate_evolution_actor_zh(normalized_executor)}",
                        f"确认截止：{due_at or '-'}",
                    ],
                ),
                level="info",
                trace_id=source_trace_id,
            )
        else:
            service.notify(
                title=_push_title(priority="P1", category="evolution", summary="release confirmed"),
                content=_notification_message_zh(
                    trigger="升级发布单已执行完成，且本次流程无需额外人工确认。",
                    impact="新版本已经直接生效，当前升级链路已进入正常运行状态。",
                    action="建议在方便时查看本地监控雷达与运行日志，确认核心功能、推送链路与风控状态正常。",
                    details=[
                        f"票据编号：{ticket.get('ticket_id', '')}",
                        f"提案编号：{proposal_id_text}",
                        f"执行人：{_translate_evolution_actor_zh(normalized_executor)}",
                    ],
                ),
                level="info",
                trace_id=source_trace_id,
            )
        return {"accepted": True, "ticket": ticket}

    def rollback_evolution_release_ticket(
        self,
        rollback_by: str,
        ticket_id: str = "",
        note: str = "",
        timestamp: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        service = self._service
        now = timestamp or datetime.now()
        normalized_rollback_by = rollback_by.strip()
        if not normalized_rollback_by:
            return {
                "accepted": False,
                "code": "invalid_rollback_by",
                "message": "rollback_by is required",
            }

        normalized_ticket_id = ticket_id.strip()
        target_ticket: dict[str, object] | None = None
        if normalized_ticket_id:
            for item in reversed(service._evolution_release_ticket_history):
                if str(item.get("ticket_id", "")) == normalized_ticket_id:
                    target_ticket = item
                    break
            if target_ticket is None:
                return {
                    "accepted": False,
                    "code": "ticket_not_found",
                    "message": f"ticket_id={normalized_ticket_id} not found",
                }
        else:
            target_ticket = service._last_evolution_release_ticket
            if target_ticket is None:
                return {
                    "accepted": False,
                    "code": "missing_release_ticket",
                    "message": "issue release ticket before rollback",
                }

        current_status = str(target_ticket.get("status", "")).strip().lower()
        if current_status == "rolled_back":
            return {
                "accepted": False,
                "code": "already_rolled_back",
                "message": "ticket is already rolled back",
                "ticket_id": str(target_ticket.get("ticket_id", "")),
            }
        if current_status not in {"issued", "executed"}:
            return {
                "accepted": False,
                "code": "ticket_not_rollbackable",
                "message": f"ticket status={current_status or 'unknown'} cannot rollback",
                "ticket_id": str(target_ticket.get("ticket_id", "")),
            }

        ticket = deepcopy(target_ticket)
        raw_checklist = ticket.get("checklist", [])
        checklist: list[dict[str, object]] = []
        if isinstance(raw_checklist, list):
            for item in raw_checklist:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                checklist.append(
                    {
                        "name": name,
                        "done": bool(item.get("done", False)),
                    }
                )

        found_rollback = False
        for item in checklist:
            name = str(item.get("name", ""))
            if name == "manual_rollback_confirmed":
                item["done"] = True
                found_rollback = True
        if not found_rollback:
            checklist.append({"name": "manual_rollback_confirmed", "done": True})

        rolled_back_at = now.isoformat()
        rollback_note = note.strip()
        ticket["status"] = "rolled_back"
        ticket["rolled_back_at"] = rolled_back_at
        ticket["checklist"] = checklist
        ticket["rollback"] = {
            "rollback_by": normalized_rollback_by,
            "timestamp": rolled_back_at,
            "note": rollback_note,
            "source_trace_id": source_trace_id,
            "from_status": current_status,
        }
        pending_confirmation = ticket.get("pending_confirmation", {})
        if isinstance(pending_confirmation, dict):
            pending_confirmation["state"] = "rolled_back"
            pending_confirmation["rolled_back_at"] = rolled_back_at
            pending_confirmation["rollback_by"] = normalized_rollback_by
            ticket["pending_confirmation"] = pending_confirmation
        release_payload = ticket.get("release_payload", {})
        if not isinstance(release_payload, dict):
            release_payload = {}
        proposal_id = str(release_payload.get("proposal_id", "")).strip()
        symbol = str(release_payload.get("symbol", "UNKNOWN")).strip() or "UNKNOWN"
        if proposal_id:
            compliance_update = service._write_evolution_compliance_event(
                state=ComplianceState.ROLLED_BACK,
                proposal_id=proposal_id,
                symbol=symbol,
                event_time=now,
                trace_id=source_trace_id,
                code_commit_id=str(service._config.evolution.code_commit_id),
                metadata={
                    "ticket_id": str(ticket.get("ticket_id", "")),
                    "rollback_by": normalized_rollback_by,
                    "from_status": current_status,
                },
            )
        else:
            compliance_update = {
                "state": ComplianceState.ROLLED_BACK.value,
                "written": False,
                "skipped": True,
                "reason": "missing_proposal_id",
            }
        ticket["compliance_update"] = compliance_update

        service._last_evolution_release_ticket = ticket
        service._evolution_release_ticket_history.append(ticket)
        history_limit = max(1, service._config.evolution.history_limit)
        if len(service._evolution_release_ticket_history) > history_limit:
            overflow = len(service._evolution_release_ticket_history) - history_limit
            if overflow > 0:
                service._evolution_release_ticket_history = (
                    service._evolution_release_ticket_history[overflow:]
                )

        service._record_audit_event(
            event_type="evolution_release_ticket_rollback",
            trace_id=source_trace_id,
            level="warn",
            payload={
                "ticket_id": str(ticket.get("ticket_id", "")),
                "rollback_by": normalized_rollback_by,
                "rolled_back_at": rolled_back_at,
                "from_status": current_status,
                "compliance": compliance_update,
            },
        )
        service.notify(
            title=_push_title(priority="P1", category="evolution", summary="release rolled back"),
            content=_notification_message_zh(
                trigger="升级发布在复核阶段发现异常，系统已执行回滚处理。",
                impact="当前版本已退出生效状态，系统恢复到回滚前的稳定版本。",
                action="请复核回滚后的关键功能是否恢复正常，并保留本次异常原因与处理记录，避免重复发布。",
                details=[
                    f"票据编号：{ticket.get('ticket_id', '')}",
                    f"回滚人：{_translate_evolution_actor_zh(normalized_rollback_by)}",
                    f"原状态：{_translate_evolution_ticket_status_zh(current_status)}",
                    f"回滚说明：{_translate_evolution_note_zh(note)}",
                ],
            ),
            level="warn",
            trace_id=source_trace_id,
        )
        return {"accepted": True, "ticket": ticket}

    def confirm_evolution_release_ticket(
        self,
        confirmer: str,
        ticket_id: str = "",
        note: str = "",
        timestamp: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        service = self._service
        now = timestamp or datetime.now()
        normalized_confirmer = confirmer.strip()
        if not normalized_confirmer:
            return {
                "accepted": False,
                "code": "invalid_confirmer",
                "message": "confirmer is required",
            }

        normalized_ticket_id = ticket_id.strip()
        target_ticket: dict[str, object] | None = None
        if normalized_ticket_id:
            for item in reversed(service._evolution_release_ticket_history):
                if str(item.get("ticket_id", "")) == normalized_ticket_id:
                    target_ticket = item
                    break
            if target_ticket is None:
                return {
                    "accepted": False,
                    "code": "ticket_not_found",
                    "message": f"ticket_id={normalized_ticket_id} not found",
                }
        else:
            target_ticket = service._last_evolution_release_ticket
            if target_ticket is None:
                return {
                    "accepted": False,
                    "code": "missing_release_ticket",
                    "message": "issue and execute release ticket before confirmation",
                }

        current_status = str(target_ticket.get("status", "")).strip().lower()
        if current_status != "executed":
            return {
                "accepted": False,
                "code": "ticket_not_executed",
                "message": (
                    "ticket confirmation requires executed status; "
                    f"current_status={current_status or 'unknown'}"
                ),
                "ticket_id": str(target_ticket.get("ticket_id", "")),
            }

        ticket = deepcopy(target_ticket)
        pending_confirmation = ticket.get("pending_confirmation", {})
        if not isinstance(pending_confirmation, dict):
            pending_confirmation = {}

        if not bool(pending_confirmation.get("required", False)):
            return {
                "accepted": False,
                "code": "confirmation_not_required",
                "message": "ticket confirmation is disabled by configuration",
                "ticket_id": str(ticket.get("ticket_id", "")),
            }

        current_confirmation_state = str(pending_confirmation.get("state", "")).strip().lower()
        if current_confirmation_state not in {"pending", ""}:
            return {
                "accepted": False,
                "code": "confirmation_not_pending",
                "message": (
                    "ticket confirmation is not pending; "
                    f"current_state={current_confirmation_state or 'unknown'}"
                ),
                "ticket_id": str(ticket.get("ticket_id", "")),
            }

        confirmed_at = now.isoformat()
        pending_confirmation["state"] = "confirmed"
        pending_confirmation["confirmed_by"] = normalized_confirmer
        pending_confirmation["confirmed_at"] = confirmed_at
        pending_confirmation["confirmation_note"] = note.strip()
        ticket["pending_confirmation"] = pending_confirmation
        ticket["confirmation"] = {
            "confirmer": normalized_confirmer,
            "timestamp": confirmed_at,
            "note": note.strip(),
            "source_trace_id": source_trace_id,
        }

        raw_checklist = ticket.get("checklist", [])
        checklist: list[dict[str, object]] = []
        if isinstance(raw_checklist, list):
            for item in raw_checklist:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                done = bool(item.get("done", False))
                if name == "manual_confirmation_received":
                    done = True
                checklist.append({"name": name, "done": done})

        has_manual_confirmation = any(
            str(item.get("name", "")) == "manual_confirmation_received" for item in checklist
        )
        if not has_manual_confirmation:
            checklist.append({"name": "manual_confirmation_received", "done": True})
        ticket["checklist"] = checklist

        service._last_evolution_release_ticket = ticket
        service._evolution_release_ticket_history.append(ticket)
        history_limit = max(1, service._config.evolution.history_limit)
        if len(service._evolution_release_ticket_history) > history_limit:
            overflow = len(service._evolution_release_ticket_history) - history_limit
            if overflow > 0:
                service._evolution_release_ticket_history = (
                    service._evolution_release_ticket_history[overflow:]
                )

        service._record_audit_event(
            event_type="evolution_release_ticket_confirm",
            trace_id=source_trace_id,
            payload={
                "ticket_id": str(ticket.get("ticket_id", "")),
                "confirmer": normalized_confirmer,
                "confirmed_at": confirmed_at,
            },
        )
        release_payload = ticket.get("release_payload", {})
        if not isinstance(release_payload, dict):
            release_payload = {}
        service.notify(
            title=_push_title(priority="P1", category="evolution", summary="release confirmed"),
            content=_notification_message_zh(
                trigger="升级发布已通过人工复核确认，版本正式转入生效状态。",
                impact="当前新版本已成为正式运行版本，后续策略、监控与推送都会按新版本逻辑继续执行。",
                action="建议继续观察一段时间的关键运行指标；若后续出现异常，可按发布票据记录继续追踪或回滚。",
                details=[
                    f"票据编号：{ticket.get('ticket_id', '')}",
                    f"提案编号：{str(release_payload.get('proposal_id', '')).strip() or '-'}",
                    f"确认人：{_translate_evolution_actor_zh(normalized_confirmer)}",
                ],
            ),
            level="info",
            trace_id=source_trace_id,
        )
        return {"accepted": True, "ticket": ticket}

    def run_evolution_release_confirmation_watchdog(
        self,
        now: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        service = self._service
        run_now = now or datetime.now()
        latest_by_ticket: dict[str, dict[str, object]] = {}
        for item in service._evolution_release_ticket_history:
            ticket_id = str(item.get("ticket_id", "")).strip()
            if not ticket_id:
                continue
            latest_by_ticket[ticket_id] = item

        overdue_tickets: list[str] = []
        for ticket_id, ticket in latest_by_ticket.items():
            if str(ticket.get("status", "")).strip().lower() != "executed":
                continue
            pending_confirmation = ticket.get("pending_confirmation", {})
            if not isinstance(pending_confirmation, dict):
                continue
            if str(pending_confirmation.get("state", "")).strip().lower() != "pending":
                continue
            due_at_raw = str(pending_confirmation.get("due_at", "")).strip()
            if not due_at_raw:
                continue
            try:
                due_at = datetime.fromisoformat(due_at_raw)
            except ValueError:
                continue
            if run_now >= due_at:
                overdue_tickets.append(ticket_id)

        rollback_results: list[dict[str, object]] = []
        rolled_back = 0
        for ticket_id in overdue_tickets:
            rollback = service.rollback_evolution_release_ticket(
                rollback_by="system_watchdog",
                ticket_id=ticket_id,
                note="auto rollback: pending confirmation ttl exceeded",
                timestamp=run_now,
                source_trace_id=source_trace_id or f"evolution-release-watchdog-{ticket_id}",
            )
            rollback_results.append(rollback)
            if bool(rollback.get("accepted", False)):
                rolled_back += 1

        payload = {
            "timestamp": run_now.isoformat(),
            "checked": len(latest_by_ticket),
            "overdue": len(overdue_tickets),
            "rolled_back": rolled_back,
            "results": rollback_results,
        }
        service._record_audit_event(
            event_type="evolution_release_confirmation_watchdog",
            trace_id=source_trace_id,
            level="warn" if rolled_back > 0 else "info",
            payload=payload,
        )
        return payload

    def latest_evolution_release_ticket(self) -> dict[str, object] | None:
        service = self._service
        report = service._last_evolution_release_ticket
        return report if isinstance(report, dict) else None

    def evolution_release_ticket_history(self, limit: int = 20) -> dict[str, object]:
        service = self._service
        capped = max(1, min(limit, max(1, service._config.evolution.history_limit)))
        recent = service._evolution_release_ticket_history[-capped:]
        return {"records": len(recent), "items": recent}

    def evolution_release_ticket_timeline(
        self,
        ticket_id: str = "",
        status: str = "",
        limit: int = 200,
    ) -> dict[str, object]:
        service = self._service
        normalized_ticket_id = ticket_id.strip()
        normalized_status = status.strip().lower()
        max_limit = max(1, service._config.evolution.history_limit)
        capped_limit = max(1, min(limit, max_limit))
        recent = service._evolution_release_ticket_history[-capped_limit:]

        grouped: dict[str, dict[str, object]] = {}
        for snapshot in recent:
            current_ticket_id = str(snapshot.get("ticket_id", "")).strip()
            if not current_ticket_id:
                continue
            if normalized_ticket_id and current_ticket_id != normalized_ticket_id:
                continue

            current_status = str(snapshot.get("status", "")).strip().lower()
            item = grouped.get(current_ticket_id)
            if item is None:
                item = {
                    "ticket_id": current_ticket_id,
                    "latest_status": current_status,
                    "latest_timestamp": str(snapshot.get("timestamp", "")),
                    "operator": str(snapshot.get("operator", "")),
                    "pending_confirmation_state": "",
                    "events": [],
                }
                grouped[current_ticket_id] = item
            else:
                item["latest_status"] = current_status
                item["latest_timestamp"] = str(snapshot.get("timestamp", ""))

            pending_confirmation = snapshot.get("pending_confirmation", {})
            if isinstance(pending_confirmation, dict):
                item["pending_confirmation_state"] = str(pending_confirmation.get("state", ""))

            event_name = "issued"
            event_timestamp = str(snapshot.get("timestamp", ""))
            event_actor = str(snapshot.get("operator", ""))
            event_note = str(snapshot.get("note", ""))

            rollback = snapshot.get("rollback", {})
            confirmation = snapshot.get("confirmation", {})
            execution = snapshot.get("execution", {})

            if isinstance(rollback, dict) and str(rollback.get("timestamp", "")):
                event_name = "rolled_back"
                event_timestamp = str(rollback.get("timestamp", ""))
                event_actor = str(rollback.get("rollback_by", ""))
                event_note = str(rollback.get("note", ""))
            elif isinstance(confirmation, dict) and str(confirmation.get("timestamp", "")):
                event_name = "confirmed"
                event_timestamp = str(confirmation.get("timestamp", ""))
                event_actor = str(confirmation.get("confirmer", ""))
                event_note = str(confirmation.get("note", ""))
            elif isinstance(execution, dict) and str(execution.get("timestamp", "")):
                event_name = "executed"
                event_timestamp = str(execution.get("timestamp", ""))
                event_actor = str(execution.get("executor", ""))
                event_note = str(execution.get("note", ""))

            raw_events = item.get("events", [])
            events: list[dict[str, object]]
            if isinstance(raw_events, list):
                events = [evt for evt in raw_events if isinstance(evt, dict)]
            else:
                events = []
            events.append(
                {
                    "event": event_name,
                    "timestamp": event_timestamp,
                    "status": current_status,
                    "actor": event_actor,
                    "note": event_note,
                }
            )
            item["events"] = events

        tickets = list(grouped.values())
        if normalized_status:
            tickets = [
                item
                for item in tickets
                if str(item.get("latest_status", "")).strip().lower() == normalized_status
            ]

        def _latest_ts(ticket: dict[str, object]) -> str:
            raw_events = ticket.get("events", [])
            if isinstance(raw_events, list) and raw_events:
                last = raw_events[-1]
                if isinstance(last, dict):
                    return str(last.get("timestamp", ""))
            return str(ticket.get("latest_timestamp", ""))

        tickets.sort(key=_latest_ts, reverse=True)
        return {
            "records": len(tickets),
            "tickets": tickets,
            "filters": {
                "ticket_id": normalized_ticket_id,
                "status": normalized_status,
                "limit": capped_limit,
            },
        }

    def _evolution_pending_confirmation_count(self) -> int:
        service = self._service
        latest_by_ticket: dict[str, dict[str, object]] = {}
        for item in service._evolution_release_ticket_history:
            ticket_id = str(item.get("ticket_id", "")).strip()
            if not ticket_id:
                continue
            latest_by_ticket[ticket_id] = item

        pending = 0
        for ticket in latest_by_ticket.values():
            if str(ticket.get("status", "")).strip().lower() != "executed":
                continue
            pending_confirmation = ticket.get("pending_confirmation", {})
            if not isinstance(pending_confirmation, dict):
                continue
            state = str(pending_confirmation.get("state", "")).strip().lower()
            required = bool(pending_confirmation.get("required", False))
            if required and state == "pending":
                pending += 1
        return pending

    def _write_evolution_compliance_event(
        self,
        state: ComplianceState,
        proposal_id: str,
        symbol: str,
        event_time: datetime,
        trace_id: str,
        code_commit_id: str,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        service = self._service
        db_path = service._resolve_evolution_path(service._config.evolution.compliance_db_path)
        event = ComplianceEvent(
            trace_id=trace_id or f"release-{proposal_id}",
            proposal_id=proposal_id,
            state=state,
            active_champion_id=service._config.evolution.active_champion_id,
            symbol=symbol,
            event_time=event_time,
            code_commit_id=code_commit_id,
            metadata=metadata or {},
        )
        logger = ComplianceLogger(db_path=db_path)
        try:
            table_name = logger.log_event(event)
            return {
                "state": state.value,
                "written": True,
                "table_name": table_name,
                "db_path": str(db_path),
            }
        except Exception as exc:
            fallback_path = service._resolve_evolution_path(
                "artifacts/evolution/compliance_fallback.jsonl"
            )
            fallback_path.parent.mkdir(parents=True, exist_ok=True)
            with fallback_path.open("a", encoding="utf-8") as fp:
                payload = {
                    "trace_id": event.trace_id,
                    "proposal_id": proposal_id,
                    "state": state.value,
                    "active_champion_id": service._config.evolution.active_champion_id,
                    "symbol": symbol,
                    "event_time": event_time.isoformat(),
                    "code_commit_id": code_commit_id,
                    "metadata": metadata or {},
                }
                fp.write(json.dumps(payload, ensure_ascii=True) + "\n")
            return {
                "state": state.value,
                "written": False,
                "fallback_path": str(fallback_path),
                "error": exc.__class__.__name__,
            }

    def _job_evolution_release_confirmation_watchdog(self) -> dict[str, object]:
        service = self._service
        report = service.run_evolution_release_confirmation_watchdog(
            now=datetime.now(),
            source_trace_id="scheduler-evolution-release-watchdog",
        )
        return {"report": report}


@lru_cache(maxsize=1)
def _runtime_service_module() -> Any:
    return import_module("stock_analyzer.runtime.service")


def _as_int(value: object, default: int) -> int:
    return cast(int, _runtime_service_module()._as_int(value, default))


def _format_notification_time_zh(value: str) -> str:
    return cast(str, _runtime_service_module()._format_notification_time_zh(value))


def _make_check(name: str, status: str, detail: str) -> dict[str, object]:
    return cast(dict[str, object], _runtime_service_module()._make_check(name, status, detail))


def _merge_evolution_reports(
    memory_reports: list[dict[str, object]],
    disk_reports: list[dict[str, object]],
) -> list[dict[str, object]]:
    return cast(
        list[dict[str, object]],
        _runtime_service_module()._merge_evolution_reports(memory_reports, disk_reports),
    )


def _notification_message_zh(
    *,
    trigger: str,
    impact: str,
    action: str,
    details: list[str] | tuple[str, ...] | None = None,
    detail_title: str = "??????",
) -> str:
    return cast(
        str,
        _runtime_service_module()._notification_message_zh(
            trigger=trigger,
            impact=impact,
            action=action,
            details=details,
            detail_title=detail_title,
        ),
    )


def _push_title(priority: str, category: str, summary: str) -> str:
    return cast(str, _runtime_service_module()._push_title(priority, category, summary))


def _report_timestamp(report: dict[str, object]) -> float:
    return cast(float, _runtime_service_module()._report_timestamp(report))


def _translate_evolution_actor_zh(actor: str) -> str:
    return cast(str, _runtime_service_module()._translate_evolution_actor_zh(actor))


def _translate_evolution_note_zh(note: str) -> str:
    return cast(str, _runtime_service_module()._translate_evolution_note_zh(note))


def _translate_evolution_ticket_status_zh(status: str) -> str:
    return cast(
        str,
        _runtime_service_module()._translate_evolution_ticket_status_zh(status),
    )


def _evolution_window_report_cache_key(
    *,
    window_days: int,
    min_runs: int,
    current: datetime,
) -> str:
    return (
        f"window_days={window_days};"
        f"min_runs={min_runs};"
        f"date={current.date().isoformat()}"
    )


def _evolution_window_report_cache_fingerprint(report_dir: Path) -> dict[str, object]:
    if not report_dir.exists():
        return {
            "report_dir_mtime_ns": 0,
            "report_count": 0,
        }
    return {
        "report_dir_mtime_ns": int(report_dir.stat().st_mtime_ns),
        "report_count": len(list(report_dir.glob("*.json"))),
    }


def _evolution_window_report_cache_path(service: Any) -> Path:
    return service._resolve_evolution_path("artifacts/evolution/window_report_cache.json")


def _load_evolution_window_report_cache(
    service: Any,
    *,
    cache_key: str,
    fingerprint: dict[str, object],
) -> dict[str, object] | None:
    raw_cache = getattr(service, "_evolution_window_report_cache", None)
    if _matches_evolution_window_report_cache(
        raw_cache,
        cache_key=cache_key,
        fingerprint=fingerprint,
        require_fresh=True,
    ):
        payload = raw_cache.get("payload")
        if isinstance(payload, dict):
            return cast(dict[str, object], deepcopy(payload))

    cache_path = _evolution_window_report_cache_path(service)
    if not cache_path.exists():
        return None
    try:
        persisted = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not _matches_evolution_window_report_cache(
        persisted,
        cache_key=cache_key,
        fingerprint=fingerprint,
        require_fresh=False,
    ):
        return None
    payload = persisted.get("payload")
    if not isinstance(payload, dict):
        return None
    _store_evolution_window_report_cache(
        service,
        cache_key=cache_key,
        fingerprint=fingerprint,
        payload=cast(dict[str, object], payload),
    )
    return cast(dict[str, object], deepcopy(payload))


def _store_evolution_window_report_cache(
    service: Any,
    *,
    cache_key: str,
    fingerprint: dict[str, object],
    payload: dict[str, object],
) -> None:
    cache_record = {
        "cache_key": cache_key,
        "fingerprint": dict(fingerprint),
        "cached_at_monotonic": monotonic(),
        "payload": deepcopy(payload),
    }
    setattr(service, "_evolution_window_report_cache", cache_record)
    cache_path = _evolution_window_report_cache_path(service)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {
                    "cache_key": cache_key,
                    "fingerprint": dict(fingerprint),
                    "payload": payload,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        return


def _matches_evolution_window_report_cache(
    raw_cache: object,
    *,
    cache_key: str,
    fingerprint: dict[str, object],
    require_fresh: bool,
) -> bool:
    if not isinstance(raw_cache, dict):
        return False
    if str(raw_cache.get("cache_key", "")) != cache_key:
        return False
    raw_fingerprint = raw_cache.get("fingerprint")
    if not isinstance(raw_fingerprint, dict):
        return False
    if raw_fingerprint != fingerprint:
        return False
    payload = raw_cache.get("payload")
    if not isinstance(payload, dict):
        return False
    if not require_fresh:
        return True
    cached_at = raw_cache.get("cached_at_monotonic")
    if not isinstance(cached_at, (int, float)) or float(cached_at) <= 0.0:
        return False
    return monotonic() - float(cached_at) <= _EVOLUTION_WINDOW_REPORT_CACHE_TTL_SEC
