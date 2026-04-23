"""Learning-model governance workflows layered on top of the learning protocol."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, cast

from stock_analyzer.evolution.governance.compliance import (
    ComplianceEvent,
    ComplianceLogger,
    ComplianceState,
)
from stock_analyzer.models.registry import (
    ModelLifecycleState,
    ModelRegistryReadError,
    ModelRole,
)

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService


logger = logging.getLogger(__name__)


class RuntimeLearningGovernanceService:
    """Govern proposal, approval, release, rollback, and monitoring for learning models."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service

    def create_learning_model_proposal(
        self,
        *,
        model_id: str,
        champion_model_id: str = "",
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
        max_samples: int | None = None,
        min_samples: int = 5,
        learning_rate: float = 0.1,
        signal_threshold: float = 0.5,
        preview_limit: int = 5,
        min_shadow_v2_minus_champion_return: float = -0.02,
        max_shadow_v2_brier_delta: float = 0.05,
        max_shadow_v2_logloss_delta: float = 0.10,
        max_signal_divergence_ratio: float | None = None,
        approve_if_passed: bool = False,
        block_if_failed: bool = False,
        allow_warn_status: bool = True,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        gate_payload = self._service.evaluate_learning_model_promotion_gate(
            model_id=model_id,
            champion_model_id=champion_model_id,
            split_names=split_names,
            max_rows=max_rows,
            max_samples=max_samples,
            min_samples=min_samples,
            learning_rate=learning_rate,
            signal_threshold=signal_threshold,
            preview_limit=preview_limit,
            min_shadow_v2_minus_champion_return=min_shadow_v2_minus_champion_return,
            max_shadow_v2_brier_delta=max_shadow_v2_brier_delta,
            max_shadow_v2_logloss_delta=max_shadow_v2_logloss_delta,
            max_signal_divergence_ratio=max_signal_divergence_ratio,
            approve_if_passed=approve_if_passed,
            block_if_failed=block_if_failed,
        )
        return self._create_learning_model_proposal_from_gate_payload(
            gate_payload=gate_payload,
            allow_warn_status=allow_warn_status,
            source_trace_id=source_trace_id,
        )

    def run_learning_manifest_shadow_proposal(
        self,
        *,
        dataset_manifest_id: str = "",
        artifact_path: str | None = None,
        champion_model_id: str = "",
        split_names: Sequence[str] | None = None,
        max_rows: int | None = None,
        include_rows: bool = False,
        preview_limit: int = 5,
        max_samples: int | None = None,
        min_samples: int = 5,
        learning_rate: float = 0.1,
        signal_threshold: float = 0.5,
        load_predictor: bool = False,
        mark_shadow_validated: bool = True,
        min_shadow_v2_minus_champion_return: float = -0.02,
        max_shadow_v2_brier_delta: float = 0.05,
        max_shadow_v2_logloss_delta: float = 0.10,
        max_signal_divergence_ratio: float | None = None,
        approve_if_passed: bool = False,
        block_if_failed: bool = False,
        allow_warn_status: bool = True,
        source_trace_id: str = "",
        auto_approve: bool = False,
        auto_release: bool = False,
        auto_reload_predictor: bool = True,
        notify_on_rejection: bool = False,
    ) -> dict[str, object]:
        workflow_payload = self._service.run_learning_manifest_shadow_promotion_gate(
            dataset_manifest_id=dataset_manifest_id,
            artifact_path=artifact_path,
            champion_model_id=champion_model_id,
            split_names=split_names,
            max_rows=max_rows,
            include_rows=include_rows,
            preview_limit=preview_limit,
            max_samples=max_samples,
            min_samples=min_samples,
            learning_rate=learning_rate,
            signal_threshold=signal_threshold,
            load_predictor=load_predictor,
            mark_shadow_validated=mark_shadow_validated,
            min_shadow_v2_minus_champion_return=min_shadow_v2_minus_champion_return,
            max_shadow_v2_brier_delta=max_shadow_v2_brier_delta,
            max_shadow_v2_logloss_delta=max_shadow_v2_logloss_delta,
            max_signal_divergence_ratio=max_signal_divergence_ratio,
            approve_if_passed=approve_if_passed,
            block_if_failed=block_if_failed,
        )
        proposal_payload = self._create_learning_model_proposal_from_gate_payload(
            gate_payload=workflow_payload,
            allow_warn_status=allow_warn_status,
            source_trace_id=source_trace_id,
        )
        proposal = _mapping(proposal_payload.get("proposal"))
        proposal_id = str(proposal.get("proposal_id", "")).strip()
        gate_payload = _mapping(workflow_payload.get("promotion_gate", {}))
        gate_status = str(
            proposal.get("gate_status", "")
            or gate_payload.get("status", "")
            or workflow_payload.get("status", "")
        ).strip().lower()
        workflow_errors = _string_list(workflow_payload.get("errors", []))
        proposal_errors = [
            item
            for item in _string_list(proposal_payload.get("errors", []))
            if item not in workflow_errors
        ]
        accepted = bool(proposal_payload.get("accepted", False))
        auto_promotion: dict[str, object] = {
            "enabled": bool(auto_approve or auto_release),
            "proposal_id": proposal_id,
            "approval_id": "",
            "ticket_id": "",
            "auto_approve": False,
            "auto_release": False,
            "predictor_loaded": False,
            "rejection_notified": False,
            "status": (
                "manual_review"
                if proposal_id and gate_status == "pass"
                else "rejected"
                if gate_status
                else "skipped"
            ),
            "errors": [],
        }
        if auto_approve and proposal_id and gate_status == "pass":
            now = datetime.now()
            approval_result = self.record_learning_model_proposal_approval(
                approver="auto_promotion_system",
                approved=True,
                proposal_id=proposal_id,
                note="auto-approved by auto_promotion policy",
                timestamp=now,
                source_trace_id=source_trace_id,
            )
            if bool(approval_result.get("accepted", False)):
                auto_promotion["auto_approve"] = True
                auto_promotion["approval_id"] = str(
                    _mapping(approval_result.get("record")).get("approval_id", "")
                ).strip()
                auto_promotion["status"] = "approved"
            else:
                auto_promotion_errors = cast(list[object], auto_promotion["errors"])
                auto_promotion_errors.append(
                    str(approval_result.get("code", "auto_approval_failed")).strip()
                    or "auto_approval_failed"
                )
                auto_promotion["status"] = "approval_failed"

            if auto_release and bool(auto_promotion["auto_approve"]):
                ticket_result = self.issue_learning_model_release_ticket(
                    operator="auto_promotion_system",
                    proposal_id=proposal_id,
                    note="auto-issued by auto_promotion policy",
                    timestamp=now,
                    source_trace_id=source_trace_id,
                )
                ticket = _mapping(ticket_result.get("ticket"))
                ticket_id = str(ticket.get("ticket_id", "")).strip()
                auto_promotion["ticket_id"] = ticket_id
                if bool(ticket_result.get("accepted", False)) and ticket_id:
                    exec_result = self.execute_learning_model_release_ticket(
                        executor="auto_promotion_system",
                        ticket_id=ticket_id,
                        confirm_window=True,
                        note="auto-executed by auto_promotion policy",
                        timestamp=now,
                        source_trace_id=source_trace_id,
                    )
                    if bool(exec_result.get("accepted", False)):
                        auto_promotion["auto_release"] = True
                        auto_promotion["status"] = "released"
                        if auto_reload_predictor:
                            shadow_model_id = str(
                                _mapping(_mapping(exec_result.get("ticket")).get("release_payload")).get(
                                    "shadow_model_id", ""
                                )
                            ).strip()
                            shadow_entry = self._service._model_registry.get_by_id(shadow_model_id)
                            if shadow_entry is not None and str(shadow_entry.artifact_uri).strip():
                                auto_promotion["predictor_loaded"] = bool(
                                    self._service._pipeline.reload_predictor(
                                        artifact_path=str(shadow_entry.artifact_uri)
                                    )
                                )
                    else:
                        auto_promotion_errors = cast(list[object], auto_promotion["errors"])
                        auto_promotion_errors.append(
                            str(exec_result.get("code", "auto_release_failed")).strip()
                            or "auto_release_failed"
                        )
                        auto_promotion["status"] = "release_failed"
                else:
                    auto_promotion_errors = cast(list[object], auto_promotion["errors"])
                    auto_promotion_errors.append(
                        str(ticket_result.get("code", "auto_ticket_issue_failed")).strip()
                        or "auto_ticket_issue_failed"
                    )
                    auto_promotion["status"] = "ticket_issue_failed"

        if auto_approve and gate_status != "pass" and notify_on_rejection:
            auto_promotion["rejection_notified"] = self._notify_auto_promotion_rejection(
                proposal_id=proposal_id,
                workflow_payload=workflow_payload,
                proposal_payload=proposal_payload,
                source_trace_id=source_trace_id,
            )
        latest_proposal = (
            self._resolve_latest_learning_proposal(proposal_id=proposal_id)
            if proposal_id
            else None
        )
        payload = {
            "ok": bool(workflow_payload.get("ok", False)) and accepted,
            "mode": "learning_manifest_shadow_proposal",
            "status": (
                str(_mapping(latest_proposal or proposal).get("status", ""))
                if accepted
                else str(workflow_payload.get("status", ""))
            ),
            "accepted": accepted,
            "dataset_manifest_id": str(workflow_payload.get("dataset_manifest_id", "")),
            "shadow_model_id": str(workflow_payload.get("shadow_model_id", "")),
            "champion_model_id": str(workflow_payload.get("champion_model_id", "")),
            "evaluation_split_names": (
                cast(list[object], workflow_payload.get("evaluation_split_names", []))
                if isinstance(workflow_payload.get("evaluation_split_names"), list)
                else []
            ),
            "workflow": workflow_payload,
            "proposal": _mapping(latest_proposal or proposal),
            "proposal_result": proposal_payload,
            "auto_promotion": auto_promotion,
            "errors": [*workflow_errors, *proposal_errors],
        }
        self._service._record_audit_event(
            event_type="learning_manifest_shadow_proposal",
            trace_id=source_trace_id,
            level="info" if bool(payload.get("ok", False)) else "warn",
            message=(
                "learning manifest shadow proposal created"
                if bool(payload.get("ok", False))
                else "learning manifest shadow proposal blocked"
            ),
            payload={
                "ok": bool(payload.get("ok", False)),
                "accepted": accepted,
                "dataset_manifest_id": payload["dataset_manifest_id"],
                "shadow_model_id": payload["shadow_model_id"],
                "champion_model_id": payload["champion_model_id"],
                "proposal_id": proposal_id,
                "ticket_id": str(auto_promotion.get("ticket_id", "")),
                "errors": list(cast(list[str], payload["errors"])),
            },
        )
        return payload

    def latest_learning_model_proposal(self) -> dict[str, object] | None:
        report = self._service._last_learning_model_proposal
        return report if isinstance(report, dict) else None

    def learning_model_proposal_history(
        self,
        limit: int = 20,
        proposal_id: str = "",
        status: str = "",
    ) -> dict[str, object]:
        service = self._service
        capped = max(1, min(limit, max(1, service._config.evolution.history_limit)))
        normalized_proposal_id = proposal_id.strip()
        normalized_status = status.strip().lower()
        recent = service._learning_model_proposal_history[-capped:]
        if normalized_proposal_id:
            recent = [
                item
                for item in recent
                if str(item.get("proposal_id", "")).strip() == normalized_proposal_id
            ]
        if normalized_status:
            recent = [
                item
                for item in recent
                if str(item.get("status", "")).strip().lower() == normalized_status
            ]
        return {
            "records": len(recent),
            "items": recent,
            "filters": {
                "proposal_id": normalized_proposal_id,
                "status": normalized_status,
                "limit": capped,
            },
        }

    def record_learning_model_proposal_approval(
        self,
        approver: str,
        approved: bool,
        *,
        proposal_id: str = "",
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

        proposal = self._resolve_latest_learning_proposal(proposal_id=proposal_id)
        if proposal is None:
            return {
                "accepted": False,
                "code": "missing_proposal",
                "message": "create learning model proposal before approval",
            }

        current_status = str(proposal.get("status", "")).strip().lower()
        if current_status in {"revoked", "rolled_back"}:
            return {
                "accepted": False,
                "code": "proposal_not_active",
                "message": f"proposal status={current_status} cannot be approved",
                "proposal_id": str(proposal.get("proposal_id", "")),
            }

        gate_status = str(proposal.get("gate_status", "")).strip().lower()
        if approved and gate_status == "fail":
            return {
                "accepted": False,
                "code": "gate_not_eligible",
                "message": "failed promotion gate proposal cannot be approved",
                "proposal_id": str(proposal.get("proposal_id", "")),
            }

        shadow_model_id = str(proposal.get("shadow_model_id", "")).strip()
        registry_transition = {
            "updated": False,
            "action": "noop",
            "reason": "approval_not_requested",
            "records": [],
        }
        if approved:
            registry_transition = self._ensure_learning_model_approved(model_id=shadow_model_id)
            if not bool(registry_transition.get("accepted", False)):
                return registry_transition

        approval_id = (
            f"LRN-APR-{now.strftime('%Y%m%d%H%M%S')}-"
            f"{len(service._learning_model_approval_history) + 1:04d}"
        )
        record = {
            "approval_id": approval_id,
            "timestamp": now.isoformat(),
            "proposal_id": str(proposal.get("proposal_id", "")),
            "approved": approved,
            "approver": normalized_approver,
            "note": note.strip(),
            "source_trace_id": source_trace_id,
            "gate_status": gate_status,
            "shadow_model_id": shadow_model_id,
            "champion_model_id": str(proposal.get("champion_model_id", "")),
            "registry_transition": {
                "updated": bool(registry_transition.get("updated", False)),
                "action": str(registry_transition.get("action", "")),
                "reason": str(registry_transition.get("reason", "")),
                "records": (
                    cast(list[object], registry_transition.get("records", []))
                    if isinstance(registry_transition.get("records"), list)
                    else []
                ),
            },
        }
        self._append_history(
            history_attr="_learning_model_approval_history",
            latest_attr="_last_learning_model_approval",
            record=record,
        )

        next_status = "approved" if approved else "rejected"
        updated_proposal = self._append_learning_proposal_snapshot(
            proposal=proposal,
            update={
                "status": next_status,
                "approval_state": next_status,
                "release_state": "pending_ticket" if approved else "rejected",
                "approved_at": now.isoformat() if approved else "",
                "rejected_at": now.isoformat() if not approved else "",
                "approval": {
                    "approval_id": approval_id,
                    "timestamp": now.isoformat(),
                    "approver": normalized_approver,
                    "approved": approved,
                    "note": note.strip(),
                },
            },
        )
        compliance_state = ComplianceState.APPROVED if approved else ComplianceState.INVALIDATED
        compliance_update = self._write_learning_compliance_event(
            state=compliance_state,
            proposal=updated_proposal,
            event_time=now,
            trace_id=source_trace_id or f"learning-approval-{approval_id}",
            metadata={
                "approval_id": approval_id,
                "approved": approved,
                "approver": normalized_approver,
            },
        )
        record["compliance_update"] = compliance_update
        updated_proposal["compliance_update"] = compliance_update

        service._record_audit_event(
            event_type="learning_model_proposal_approval",
            trace_id=source_trace_id,
            level="info" if approved else "warn",
            payload={
                "proposal_id": str(proposal.get("proposal_id", "")),
                "approval_id": approval_id,
                "approved": approved,
                "approver": normalized_approver,
                "registry_transition": {
                    "updated": bool(registry_transition.get("updated", False)),
                    "action": str(registry_transition.get("action", "")),
                },
            },
        )
        return {"accepted": True, "record": record, "proposal": updated_proposal}

    def latest_learning_model_approval(self) -> dict[str, object] | None:
        report = self._service._last_learning_model_approval
        return report if isinstance(report, dict) else None

    def learning_model_approval_history(self, limit: int = 20) -> dict[str, object]:
        service = self._service
        capped = max(1, min(limit, max(1, service._config.evolution.history_limit)))
        recent = service._learning_model_approval_history[-capped:]
        return {"records": len(recent), "items": recent}

    def revoke_learning_model_proposal(
        self,
        revoked_by: str,
        *,
        proposal_id: str = "",
        note: str = "",
        revoke_model: bool = True,
        timestamp: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        service = self._service
        now = timestamp or datetime.now()
        normalized_actor = revoked_by.strip()
        if not normalized_actor:
            return {
                "accepted": False,
                "code": "invalid_revoked_by",
                "message": "revoked_by is required",
            }

        proposal = self._resolve_latest_learning_proposal(proposal_id=proposal_id)
        if proposal is None:
            return {
                "accepted": False,
                "code": "missing_proposal",
                "message": "no learning proposal available to revoke",
            }

        current_status = str(proposal.get("status", "")).strip().lower()
        if current_status in {"ticket_issued", "executed", "confirmed"}:
            return {
                "accepted": False,
                "code": "proposal_already_released",
                "message": "use ticket rollback after release issuance/execution",
                "proposal_id": str(proposal.get("proposal_id", "")),
            }
        if current_status == "revoked":
            return {
                "accepted": False,
                "code": "proposal_already_revoked",
                "message": "proposal is already revoked",
                "proposal_id": str(proposal.get("proposal_id", "")),
            }

        registry_transition = {
            "updated": False,
            "action": "noop",
            "reason": "revoke_model_disabled",
            "records": [],
        }
        shadow_model_id = str(proposal.get("shadow_model_id", "")).strip()
        if revoke_model and shadow_model_id:
            target_entry = service._model_registry.get_by_id(shadow_model_id)
            if target_entry is not None and target_entry.lifecycle_state != ModelLifecycleState.REVOKED:
                transition_records: list[dict[str, object]] = []
                if target_entry.role == ModelRole.CHAMPION:
                    transition_records.append(
                        service.update_model_registry_role(
                            model_id=shadow_model_id,
                            role=ModelRole.CHALLENGER.value,
                            timestamp=now,
                        )
                    )
                transition_records.append(
                    service.update_model_registry_lifecycle(
                        model_id=shadow_model_id,
                        lifecycle_state=ModelLifecycleState.REVOKED.value,
                        timestamp=now,
                    )
                )
                registry_transition = {
                    "updated": True,
                    "action": "revoked",
                    "reason": "proposal_revoked",
                    "records": transition_records,
                }

        updated_proposal = self._append_learning_proposal_snapshot(
            proposal=proposal,
            update={
                "status": "revoked",
                "approval_state": "revoked",
                "release_state": "revoked",
                "revoked_at": now.isoformat(),
                "revocation": {
                    "revoked_by": normalized_actor,
                    "timestamp": now.isoformat(),
                    "note": note.strip(),
                },
                "registry_transition": registry_transition,
            },
        )
        compliance_update = self._write_learning_compliance_event(
            state=ComplianceState.INVALIDATED,
            proposal=updated_proposal,
            event_time=now,
            trace_id=source_trace_id or f"learning-revoke-{updated_proposal['proposal_id']}",
            metadata={
                "revoked_by": normalized_actor,
                "note": note.strip(),
                "revoke_model": revoke_model,
            },
        )
        updated_proposal["compliance_update"] = compliance_update

        service._record_audit_event(
            event_type="learning_model_proposal_revoked",
            trace_id=source_trace_id,
            level="warn",
            payload={
                "proposal_id": str(updated_proposal.get("proposal_id", "")),
                "shadow_model_id": shadow_model_id,
                "revoked_by": normalized_actor,
                "registry_transition": {
                    "updated": bool(registry_transition.get("updated", False)),
                    "action": str(registry_transition.get("action", "")),
                },
            },
        )
        return {
            "accepted": True,
            "proposal": updated_proposal,
            "registry_transition": registry_transition,
            "compliance_update": compliance_update,
        }

    def issue_learning_model_release_ticket(
        self,
        operator: str,
        *,
        proposal_id: str = "",
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

        proposal = self._resolve_latest_learning_proposal(proposal_id=proposal_id)
        if proposal is None:
            return {
                "accepted": False,
                "code": "missing_proposal",
                "message": "approve proposal before issuing release ticket",
            }
        if str(proposal.get("status", "")).strip().lower() != "approved":
            return {
                "accepted": False,
                "code": "proposal_not_approved",
                "message": "proposal must be approved before issuing release ticket",
                "proposal_id": str(proposal.get("proposal_id", "")),
            }

        latest_approval = self._latest_learning_approval_for_proposal(
            str(proposal.get("proposal_id", ""))
        )
        if latest_approval is None or not bool(latest_approval.get("approved", False)):
            return {
                "accepted": False,
                "code": "missing_approved_manual_review",
                "message": "approved manual review record is required before ticket issuance",
                "proposal_id": str(proposal.get("proposal_id", "")),
            }

        ticket_id = (
            f"LRN-TKT-{now.strftime('%Y%m%d%H%M%S')}-"
            f"{len(service._learning_model_release_ticket_history) + 1:04d}"
        )
        payload_uri = str(proposal.get("payload_uri", "")).strip()
        ticket = {
            "ticket_id": ticket_id,
            "timestamp": now.isoformat(),
            "status": "issued",
            "manual_execution_required": True,
            "operator": normalized_operator,
            "note": note.strip(),
            "source_trace_id": source_trace_id,
            "proposal": {
                "proposal_id": str(proposal.get("proposal_id", "")),
                "status": str(proposal.get("status", "")),
                "gate_status": str(proposal.get("gate_status", "")),
                "payload_uri": payload_uri,
            },
            "approval": {
                "approval_id": str(latest_approval.get("approval_id", "")),
                "approver": str(latest_approval.get("approver", "")),
                "approved": True,
                "timestamp": str(latest_approval.get("timestamp", "")),
            },
            "release_payload": {
                "shadow_model_id": str(proposal.get("shadow_model_id", "")),
                "champion_model_id": str(proposal.get("champion_model_id", "")),
                "dataset_manifest_id": str(proposal.get("dataset_manifest_id", "")),
                "feature_schema_id": str(proposal.get("feature_schema_id", "")),
                "feature_schema_hash": str(proposal.get("feature_schema_hash", "")),
                "label_policy_id": str(proposal.get("label_policy_id", "")),
                "label_policy_hash": str(proposal.get("label_policy_hash", "")),
                "payload_uri": payload_uri,
                "proposal_id": str(proposal.get("proposal_id", "")),
                "code_commit_id": str(proposal.get("code_commit_id", "")),
            },
            "checklist": [
                {"name": "promotion_proposal_ready", "done": bool(payload_uri)},
                {"name": "manual_approval_recorded", "done": True},
                {"name": "release_window_confirmed", "done": False},
            ],
        }
        self._append_history(
            history_attr="_learning_model_release_ticket_history",
            latest_attr="_last_learning_model_release_ticket",
            record=ticket,
        )
        updated_proposal = self._append_learning_proposal_snapshot(
            proposal=proposal,
            update={
                "status": "ticket_issued",
                "release_state": "ticket_issued",
                "ticket": {
                    "ticket_id": ticket_id,
                    "timestamp": now.isoformat(),
                    "operator": normalized_operator,
                    "status": "issued",
                },
            },
        )
        service._record_audit_event(
            event_type="learning_model_release_ticket",
            trace_id=source_trace_id,
            payload={
                "ticket_id": ticket_id,
                "proposal_id": str(proposal.get("proposal_id", "")),
                "shadow_model_id": str(proposal.get("shadow_model_id", "")),
                "operator": normalized_operator,
            },
        )
        return {"accepted": True, "ticket": ticket, "proposal": updated_proposal}

    def execute_learning_model_release_ticket(
        self,
        executor: str,
        *,
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
                "code": "release_window_not_confirmed",
                "message": "release window must be confirmed before execution",
            }

        target_ticket = self._resolve_latest_learning_ticket(ticket_id=ticket_id)
        if target_ticket is None:
            return {
                "accepted": False,
                "code": "missing_release_ticket",
                "message": "issue release ticket before execution",
            }
        current_status = str(target_ticket.get("status", "")).strip().lower()
        if current_status != "issued":
            return {
                "accepted": False,
                "code": "ticket_not_issued",
                "message": f"ticket status={current_status or 'unknown'} cannot be executed",
                "ticket_id": str(target_ticket.get("ticket_id", "")),
            }

        ticket = deepcopy(target_ticket)
        release_payload = _mapping(ticket.get("release_payload"))
        proposal = self._resolve_latest_learning_proposal(
            proposal_id=str(release_payload.get("proposal_id", ""))
        )
        if proposal is None:
            return {
                "accepted": False,
                "code": "proposal_not_found",
                "message": "proposal linked to ticket is missing",
                "ticket_id": str(ticket.get("ticket_id", "")),
            }

        shadow_model_id = str(release_payload.get("shadow_model_id", "")).strip()
        target_entry = service._model_registry.get_by_id(shadow_model_id)
        if target_entry is None:
            return {
                "accepted": False,
                "code": "shadow_model_not_found",
                "message": f"model_id={shadow_model_id} not found",
                "ticket_id": str(ticket.get("ticket_id", "")),
            }
        if target_entry.lifecycle_state != ModelLifecycleState.APPROVED:
            return {
                "accepted": False,
                "code": "target_model_not_approved",
                "message": "target model lifecycle must be approved before release execution",
                "ticket_id": str(ticket.get("ticket_id", "")),
                "shadow_model_id": shadow_model_id,
            }

        previous_champion = service._model_registry.active_champion()
        previous_champion_id = str(release_payload.get("champion_model_id", "")).strip()
        if previous_champion is not None and previous_champion_id:
            if previous_champion.model_id != previous_champion_id:
                return {
                    "accepted": False,
                    "code": "champion_changed_since_ticket_issue",
                    "message": "active champion changed after ticket issuance; regenerate proposal/ticket",
                    "ticket_id": str(ticket.get("ticket_id", "")),
                    "expected_champion_model_id": previous_champion_id,
                    "active_champion_model_id": previous_champion.model_id,
                }
        if previous_champion is None and previous_champion_id:
            return {
                "accepted": False,
                "code": "champion_missing_since_ticket_issue",
                "message": "expected champion model is no longer available",
                "ticket_id": str(ticket.get("ticket_id", "")),
                "expected_champion_model_id": previous_champion_id,
            }

        transition_records: list[dict[str, object]] = []
        if previous_champion is not None and previous_champion.model_id != shadow_model_id:
            transition_records.append(
                service.update_model_registry_role(
                    model_id=previous_champion.model_id,
                    role=ModelRole.CHALLENGER.value,
                    timestamp=now,
                )
            )
        if target_entry.role != ModelRole.CHAMPION:
            transition_records.append(
                service.update_model_registry_role(
                    model_id=shadow_model_id,
                    role=ModelRole.CHAMPION.value,
                    timestamp=now,
                )
            )
        service._config.evolution.active_champion_id = shadow_model_id

        checklist = _normalize_checklist(ticket.get("checklist", []))
        checklist = _set_checklist_flag(checklist, "release_window_confirmed", True)
        checklist = _set_checklist_flag(checklist, "release_execution_completed", True)
        checklist = _set_checklist_flag(checklist, "manual_confirmation_received", False)

        confirmation_required = bool(service._config.evolution.release_confirmation_required)
        confirmation_ttl_days = max(1, service._config.evolution.release_confirmation_ttl_days)
        pending_confirmation = {
            "required": confirmation_required,
            "state": "pending" if confirmation_required else "not_required",
            "due_at": (
                (now + timedelta(days=confirmation_ttl_days)).isoformat()
                if confirmation_required
                else ""
            ),
            "ttl_days": confirmation_ttl_days if confirmation_required else 0,
            "confirmed_by": "",
            "confirmed_at": "",
            "confirmation_note": "",
        }

        executed_at = now.isoformat()
        ticket["status"] = "executed"
        ticket["executed_at"] = executed_at
        ticket["checklist"] = checklist
        ticket["execution"] = {
            "executor": normalized_executor,
            "timestamp": executed_at,
            "note": note.strip(),
            "source_trace_id": source_trace_id,
        }
        ticket["pending_confirmation"] = pending_confirmation
        ticket["release_transition"] = {
            "updated": bool(transition_records),
            "records": transition_records,
            "previous_champion_model_id": previous_champion_id,
            "active_champion_model_id": shadow_model_id,
        }

        updated_proposal = self._append_learning_proposal_snapshot(
            proposal=proposal,
            update={
                "status": "executed",
                "release_state": (
                    "pending_confirmation" if confirmation_required else "confirmed"
                ),
                "released_at": executed_at,
                "ticket": {
                    "ticket_id": str(ticket.get("ticket_id", "")),
                    "timestamp": executed_at,
                    "operator": str(ticket.get("operator", "")),
                    "status": "executed",
                },
                "release_execution": {
                    "executor": normalized_executor,
                    "timestamp": executed_at,
                    "note": note.strip(),
                    "previous_champion_model_id": previous_champion_id,
                    "active_champion_model_id": shadow_model_id,
                },
            },
        )
        compliance_update = self._write_learning_compliance_event(
            state=ComplianceState.PROMOTED,
            proposal=updated_proposal,
            event_time=now,
            trace_id=source_trace_id or f"learning-release-{ticket['ticket_id']}",
            metadata={
                "ticket_id": str(ticket.get("ticket_id", "")),
                "executor": normalized_executor,
                "previous_champion_model_id": previous_champion_id,
            },
        )
        ticket["compliance_update"] = compliance_update
        updated_proposal["compliance_update"] = compliance_update

        self._append_history(
            history_attr="_learning_model_release_ticket_history",
            latest_attr="_last_learning_model_release_ticket",
            record=ticket,
        )
        service._record_audit_event(
            event_type="learning_model_release_ticket_execute",
            trace_id=source_trace_id,
            payload={
                "ticket_id": str(ticket.get("ticket_id", "")),
                "proposal_id": str(release_payload.get("proposal_id", "")),
                "shadow_model_id": shadow_model_id,
                "previous_champion_model_id": previous_champion_id,
                "confirmation_required": confirmation_required,
            },
        )
        service.notify(
            title="Learning 模型发布已执行",
            content=(
                f"票据 {ticket.get('ticket_id', '')} 已执行，"
                f"新 Champion 模型为 {shadow_model_id}。"
            ),
            level="info",
            trace_id=source_trace_id,
        )
        return {"accepted": True, "ticket": ticket, "proposal": updated_proposal}

    def confirm_learning_model_release_ticket(
        self,
        confirmer: str,
        *,
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

        target_ticket = self._resolve_latest_learning_ticket(ticket_id=ticket_id)
        if target_ticket is None:
            return {
                "accepted": False,
                "code": "missing_release_ticket",
                "message": "execute release ticket before confirmation",
            }
        current_status = str(target_ticket.get("status", "")).strip().lower()
        if current_status != "executed":
            return {
                "accepted": False,
                "code": "ticket_not_executed",
                "message": f"ticket status={current_status or 'unknown'} cannot be confirmed",
                "ticket_id": str(target_ticket.get("ticket_id", "")),
            }
        ticket = deepcopy(target_ticket)
        pending_confirmation = _mapping(ticket.get("pending_confirmation"))
        if not bool(pending_confirmation.get("required", False)):
            return {
                "accepted": False,
                "code": "confirmation_not_required",
                "message": "release confirmation is disabled by configuration",
                "ticket_id": str(ticket.get("ticket_id", "")),
            }
        current_state = str(pending_confirmation.get("state", "")).strip().lower()
        if current_state != "pending":
            return {
                "accepted": False,
                "code": "confirmation_not_pending",
                "message": f"current confirmation state is {current_state or 'unknown'}",
                "ticket_id": str(ticket.get("ticket_id", "")),
            }

        confirmed_at = now.isoformat()
        pending_confirmation["state"] = "confirmed"
        pending_confirmation["confirmed_by"] = normalized_confirmer
        pending_confirmation["confirmed_at"] = confirmed_at
        pending_confirmation["confirmation_note"] = note.strip()
        ticket["pending_confirmation"] = pending_confirmation
        ticket["status"] = "confirmed"
        ticket["confirmation"] = {
            "confirmer": normalized_confirmer,
            "timestamp": confirmed_at,
            "note": note.strip(),
            "source_trace_id": source_trace_id,
        }
        ticket["checklist"] = _set_checklist_flag(
            _normalize_checklist(ticket.get("checklist", [])),
            "manual_confirmation_received",
            True,
        )

        proposal_id = str(_mapping(ticket.get("proposal")).get("proposal_id", ""))
        proposal = self._resolve_latest_learning_proposal(proposal_id=proposal_id)
        updated_proposal = (
            self._append_learning_proposal_snapshot(
                proposal=proposal,
                update={
                    "status": "confirmed",
                    "release_state": "confirmed",
                    "confirmed_at": confirmed_at,
                    "confirmation": {
                        "confirmer": normalized_confirmer,
                        "timestamp": confirmed_at,
                        "note": note.strip(),
                    },
                },
            )
            if proposal is not None
            else {}
        )

        self._append_history(
            history_attr="_learning_model_release_ticket_history",
            latest_attr="_last_learning_model_release_ticket",
            record=ticket,
        )
        service._record_audit_event(
            event_type="learning_model_release_ticket_confirm",
            trace_id=source_trace_id,
            payload={
                "ticket_id": str(ticket.get("ticket_id", "")),
                "proposal_id": proposal_id,
                "confirmer": normalized_confirmer,
                "confirmed_at": confirmed_at,
            },
        )
        service.notify(
            title="Learning 模型发布已确认",
            content=(
                f"票据 {ticket.get('ticket_id', '')} 已完成人工确认，"
                f"Champion 模型保持为 {service._config.evolution.active_champion_id}。"
            ),
            level="info",
            trace_id=source_trace_id,
        )
        return {"accepted": True, "ticket": ticket, "proposal": updated_proposal}

    def rollback_learning_model_release_ticket(
        self,
        rollback_by: str,
        *,
        ticket_id: str = "",
        note: str = "",
        timestamp: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        service = self._service
        now = timestamp or datetime.now()
        normalized_actor = rollback_by.strip()
        if not normalized_actor:
            return {
                "accepted": False,
                "code": "invalid_rollback_by",
                "message": "rollback_by is required",
            }

        target_ticket = self._resolve_latest_learning_ticket(ticket_id=ticket_id)
        if target_ticket is None:
            return {
                "accepted": False,
                "code": "missing_release_ticket",
                "message": "no learning release ticket available to rollback",
            }
        current_status = str(target_ticket.get("status", "")).strip().lower()
        if current_status not in {"executed", "confirmed"}:
            return {
                "accepted": False,
                "code": "ticket_not_rollbackable",
                "message": f"ticket status={current_status or 'unknown'} cannot be rolled back",
                "ticket_id": str(target_ticket.get("ticket_id", "")),
            }

        ticket = deepcopy(target_ticket)
        release_payload = _mapping(ticket.get("release_payload"))
        shadow_model_id = str(release_payload.get("shadow_model_id", "")).strip()
        previous_champion_id = str(release_payload.get("champion_model_id", "")).strip()
        if not previous_champion_id:
            return {
                "accepted": False,
                "code": "previous_champion_missing",
                "message": "rollback requires previous champion metadata",
                "ticket_id": str(ticket.get("ticket_id", "")),
            }

        previous_champion = service._model_registry.get_by_id(previous_champion_id)
        target_entry = service._model_registry.get_by_id(shadow_model_id)
        if previous_champion is None or target_entry is None:
            return {
                "accepted": False,
                "code": "registry_entries_missing",
                "message": "rollback target or previous champion model is missing",
                "ticket_id": str(ticket.get("ticket_id", "")),
            }

        transition_records: list[dict[str, object]] = []
        if target_entry.role == ModelRole.CHAMPION:
            transition_records.append(
                service.update_model_registry_role(
                    model_id=shadow_model_id,
                    role=ModelRole.CHALLENGER.value,
                    timestamp=now,
                )
            )
        if target_entry.lifecycle_state != ModelLifecycleState.REVOKED:
            transition_records.append(
                service.update_model_registry_lifecycle(
                    model_id=shadow_model_id,
                    lifecycle_state=ModelLifecycleState.REVOKED.value,
                    timestamp=now,
                )
            )
        if previous_champion.role != ModelRole.CHAMPION:
            transition_records.append(
                service.update_model_registry_role(
                    model_id=previous_champion_id,
                    role=ModelRole.CHAMPION.value,
                    timestamp=now,
                )
            )
        service._config.evolution.active_champion_id = previous_champion_id

        rolled_back_at = now.isoformat()
        ticket["status"] = "rolled_back"
        ticket["rollback"] = {
            "rollback_by": normalized_actor,
            "timestamp": rolled_back_at,
            "note": note.strip(),
            "source_trace_id": source_trace_id,
        }
        pending_confirmation = _mapping(ticket.get("pending_confirmation"))
        if pending_confirmation:
            pending_confirmation["state"] = "rolled_back"
            pending_confirmation["rollback_by"] = normalized_actor
            pending_confirmation["rolled_back_at"] = rolled_back_at
            ticket["pending_confirmation"] = pending_confirmation
        ticket["release_transition"] = {
            "updated": bool(transition_records),
            "records": transition_records,
            "active_champion_model_id": previous_champion_id,
        }

        proposal_id = str(_mapping(ticket.get("proposal")).get("proposal_id", ""))
        proposal = self._resolve_latest_learning_proposal(proposal_id=proposal_id)
        updated_proposal = (
            self._append_learning_proposal_snapshot(
                proposal=proposal,
                update={
                    "status": "rolled_back",
                    "release_state": "rolled_back",
                    "rolled_back_at": rolled_back_at,
                    "rollback": {
                        "rollback_by": normalized_actor,
                        "timestamp": rolled_back_at,
                        "note": note.strip(),
                    },
                    "registry_transition": {
                        "updated": bool(transition_records),
                        "action": "rolled_back",
                        "reason": "release_ticket_rollback",
                        "records": transition_records,
                    },
                },
            )
            if proposal is not None
            else {}
        )
        compliance_update = (
            self._write_learning_compliance_event(
                state=ComplianceState.ROLLED_BACK,
                proposal=updated_proposal,
                event_time=now,
                trace_id=source_trace_id or f"learning-rollback-{ticket['ticket_id']}",
                metadata={
                    "ticket_id": str(ticket.get("ticket_id", "")),
                    "rollback_by": normalized_actor,
                    "from_status": current_status,
                },
            )
            if updated_proposal
            else {
                "state": ComplianceState.ROLLED_BACK.value,
                "written": False,
                "skipped": True,
                "reason": "missing_proposal_snapshot",
            }
        )
        ticket["compliance_update"] = compliance_update
        if updated_proposal:
            updated_proposal["compliance_update"] = compliance_update

        self._append_history(
            history_attr="_learning_model_release_ticket_history",
            latest_attr="_last_learning_model_release_ticket",
            record=ticket,
        )
        service._record_audit_event(
            event_type="learning_model_release_ticket_rollback",
            trace_id=source_trace_id,
            level="warn",
            payload={
                "ticket_id": str(ticket.get("ticket_id", "")),
                "proposal_id": proposal_id,
                "rollback_by": normalized_actor,
                "from_status": current_status,
                "active_champion_model_id": previous_champion_id,
            },
        )
        service.notify(
            title="Learning 模型发布已回滚",
            content=(
                f"票据 {ticket.get('ticket_id', '')} 已回滚，"
                f"Champion 模型恢复为 {previous_champion_id}。"
            ),
            level="warn",
            trace_id=source_trace_id,
        )
        return {
            "accepted": True,
            "ticket": ticket,
            "proposal": updated_proposal,
            "compliance_update": compliance_update,
        }

    def run_learning_model_release_confirmation_watchdog(
        self,
        now: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        service = self._service
        run_now = now or datetime.now()
        latest_by_ticket = self._latest_learning_tickets_by_id()
        overdue_tickets: list[str] = []
        for ticket_id, ticket in latest_by_ticket.items():
            if str(ticket.get("status", "")).strip().lower() != "executed":
                continue
            pending_confirmation = _mapping(ticket.get("pending_confirmation"))
            if not bool(pending_confirmation.get("required", False)):
                continue
            if str(pending_confirmation.get("state", "")).strip().lower() != "pending":
                continue
            due_at = _parse_iso_datetime(pending_confirmation.get("due_at"))
            if due_at is None:
                continue
            if run_now >= due_at:
                overdue_tickets.append(ticket_id)

        rollback_results: list[dict[str, object]] = []
        rolled_back = 0
        for learning_ticket_id in overdue_tickets:
            rollback = self.rollback_learning_model_release_ticket(
                rollback_by="system_watchdog",
                ticket_id=learning_ticket_id,
                note="auto rollback: learning confirmation ttl exceeded",
                timestamp=run_now,
                source_trace_id=source_trace_id or f"learning-release-watchdog-{learning_ticket_id}",
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
            event_type="learning_model_release_confirmation_watchdog",
            trace_id=source_trace_id,
            level="warn" if rolled_back > 0 else "info",
            payload=payload,
        )
        return payload

    def latest_learning_model_release_ticket(self) -> dict[str, object] | None:
        report = self._service._last_learning_model_release_ticket
        return report if isinstance(report, dict) else None

    def learning_model_release_ticket_history(self, limit: int = 20) -> dict[str, object]:
        service = self._service
        capped = max(1, min(limit, max(1, service._config.evolution.history_limit)))
        recent = service._learning_model_release_ticket_history[-capped:]
        return {"records": len(recent), "items": recent}

    def learning_model_release_ticket_timeline(
        self,
        ticket_id: str = "",
        status: str = "",
        limit: int = 200,
    ) -> dict[str, object]:
        service = self._service
        normalized_ticket_id = ticket_id.strip()
        normalized_status = status.strip().lower()
        capped = max(1, min(limit, max(1, service._config.evolution.history_limit)))
        recent = service._learning_model_release_ticket_history[-capped:]
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

            pending_confirmation = _mapping(snapshot.get("pending_confirmation"))
            if pending_confirmation:
                item["pending_confirmation_state"] = str(
                    pending_confirmation.get("state", "")
                )

            event_name = "issued"
            event_timestamp = str(snapshot.get("timestamp", ""))
            event_actor = str(snapshot.get("operator", ""))
            event_note = str(snapshot.get("note", ""))
            rollback = _mapping(snapshot.get("rollback"))
            confirmation = _mapping(snapshot.get("confirmation"))
            execution = _mapping(snapshot.get("execution"))
            if rollback and str(rollback.get("timestamp", "")):
                event_name = "rolled_back"
                event_timestamp = str(rollback.get("timestamp", ""))
                event_actor = str(rollback.get("rollback_by", ""))
                event_note = str(rollback.get("note", ""))
            elif confirmation and str(confirmation.get("timestamp", "")):
                event_name = "confirmed"
                event_timestamp = str(confirmation.get("timestamp", ""))
                event_actor = str(confirmation.get("confirmer", ""))
                event_note = str(confirmation.get("note", ""))
            elif execution and str(execution.get("timestamp", "")):
                event_name = "executed"
                event_timestamp = str(execution.get("timestamp", ""))
                event_actor = str(execution.get("executor", ""))
                event_note = str(execution.get("note", ""))
            events = cast(list[dict[str, object]], item.get("events", []))
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

        def _latest_ts(item: dict[str, object]) -> str:
            events = item.get("events", [])
            if isinstance(events, list) and events:
                last = events[-1]
                if isinstance(last, dict):
                    return str(last.get("timestamp", ""))
            return str(item.get("latest_timestamp", ""))

        tickets.sort(key=_latest_ts, reverse=True)
        return {
            "records": len(tickets),
            "tickets": tickets,
            "filters": {
                "ticket_id": normalized_ticket_id,
                "status": normalized_status,
                "limit": capped,
            },
        }

    def learning_model_governance_status(
        self,
        *,
        now: datetime | None = None,
        proposal_limit: int = 20,
        ticket_limit: int = 20,
    ) -> dict[str, object]:
        service = self._service
        current = now or datetime.now()
        latest_proposals = self._latest_learning_proposals_by_id()
        latest_tickets = self._latest_learning_tickets_by_id()
        proposal_status_breakdown: dict[str, int] = {}
        ticket_status_breakdown: dict[str, int] = {}
        pending_approval = 0
        approved_pending_ticket = 0
        pending_confirmation = 0
        overdue_confirmation = 0

        for proposal in latest_proposals.values():
            status = str(proposal.get("status", "")).strip().lower() or "unknown"
            proposal_status_breakdown[status] = proposal_status_breakdown.get(status, 0) + 1
            if status == "generated":
                pending_approval += 1
            if status == "approved":
                approved_pending_ticket += 1

        for ticket in latest_tickets.values():
            status = str(ticket.get("status", "")).strip().lower() or "unknown"
            ticket_status_breakdown[status] = ticket_status_breakdown.get(status, 0) + 1
            pending = _mapping(ticket.get("pending_confirmation"))
            if (
                bool(pending.get("required", False))
                and str(pending.get("state", "")).strip().lower() == "pending"
            ):
                pending_confirmation += 1
                due_at = _parse_iso_datetime(pending.get("due_at"))
                if due_at is not None and current >= due_at:
                    overdue_confirmation += 1

        governance_status = "ok"
        governance_reason = ""
        governance_error_type = ""
        blocked_records: list[object] = []
        revoked_records: list[object] = []
        active_champion = None
        try:
            blocked_records = cast(
                list[object],
                service._model_registry.list_records(
                    lifecycle_state=ModelLifecycleState.BLOCKED,
                    limit=max(1, min(200, max(20, proposal_limit + ticket_limit))),
                ),
            )
            revoked_records = cast(
                list[object],
                service._model_registry.list_records(
                    lifecycle_state=ModelLifecycleState.REVOKED,
                    limit=max(1, min(200, max(20, proposal_limit + ticket_limit))),
                ),
            )
            active_champion = service._model_registry.active_champion()
        except ModelRegistryReadError:
            governance_status = "degraded"
            governance_reason = "model_registry_unavailable"
            governance_error_type = "ModelRegistryReadError"
            logger.warning(
                "learning governance status degraded because the model registry is unavailable",
                exc_info=True,
            )
        recent_release_audit = service.audit_events(
            limit=10,
            event_type="learning_model_release_ticket_execute",
        )
        release_events = (
            cast(list[object], recent_release_audit.get("events", []))
            if isinstance(recent_release_audit.get("events"), list)
            else []
        )

        return {
            "status": governance_status,
            "active_champion": (
                active_champion.model_dump(mode="json") if active_champion is not None else None
            ),
            "model_registry": {
                "status": governance_status,
                "reason": governance_reason,
                "error_type": governance_error_type,
            },
            "proposal_latest": self.latest_learning_model_proposal(),
            "proposal_summary": {
                "records": len(latest_proposals),
                "status_breakdown": proposal_status_breakdown,
                "pending_approval": pending_approval,
                "approved_pending_ticket": approved_pending_ticket,
                "history_count": len(service._learning_model_proposal_history),
                "recent": list(latest_proposals.values())[: max(1, min(proposal_limit, 20))],
            },
            "approval_latest": self.latest_learning_model_approval(),
            "approval_history_count": len(service._learning_model_approval_history),
            "ticket_latest": self.latest_learning_model_release_ticket(),
            "ticket_summary": {
                "records": len(latest_tickets),
                "status_breakdown": ticket_status_breakdown,
                "pending_confirmation": pending_confirmation,
                "overdue_confirmation": overdue_confirmation,
                "history_count": len(service._learning_model_release_ticket_history),
                "recent": list(latest_tickets.values())[: max(1, min(ticket_limit, 20))],
            },
            "monitoring": {
                "pending_approval": pending_approval,
                "approved_pending_ticket": approved_pending_ticket,
                "pending_confirmation": pending_confirmation,
                "overdue_confirmation": overdue_confirmation,
                "blocked_model_count": len(blocked_records),
                "revoked_model_count": len(revoked_records),
                "model_registry_status": governance_status,
                "latest_release_audit": release_events[-1] if release_events else None,
            },
            "config": {
                "history_limit": max(1, service._config.evolution.history_limit),
                "release_confirmation_required": bool(
                    service._config.evolution.release_confirmation_required
                ),
                "release_confirmation_ttl_days": max(
                    1,
                    service._config.evolution.release_confirmation_ttl_days,
                ),
                "suggestions_dir": str(self._learning_governance_root()),
                "compliance_db_path": str(
                    service._resolve_evolution_path(service._config.evolution.compliance_db_path)
                ),
                "active_champion_id": str(service._config.evolution.active_champion_id),
            },
        }

    def _learning_pending_confirmation_count(self, now: datetime | None = None) -> int:
        pending = 0
        for ticket in self._latest_learning_tickets_by_id().values():
            if str(ticket.get("status", "")).strip().lower() != "executed":
                continue
            state = _mapping(ticket.get("pending_confirmation"))
            if not bool(state.get("required", False)):
                continue
            if str(state.get("state", "")).strip().lower() != "pending":
                continue
            pending += 1
        return pending

    def _write_learning_compliance_event(
        self,
        *,
        state: ComplianceState,
        proposal: Mapping[str, object],
        event_time: datetime,
        trace_id: str,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        service = self._service
        db_path = service._resolve_evolution_path(service._config.evolution.compliance_db_path)
        proposal_id = str(proposal.get("proposal_id", "")).strip()
        symbol = str(proposal.get("shadow_model_id", "")).strip() or "LEARNING_MODEL"
        event = ComplianceEvent(
            trace_id=trace_id or f"learning-{proposal_id}",
            proposal_id=proposal_id,
            state=state,
            active_champion_id=str(service._config.evolution.active_champion_id).strip(),
            symbol=symbol,
            event_time=event_time,
            code_commit_id=str(service._config.evolution.code_commit_id).strip() or None,
            metadata={
                "workflow": "learning_model_governance",
                "shadow_model_id": str(proposal.get("shadow_model_id", "")),
                "champion_model_id": str(proposal.get("champion_model_id", "")),
                "dataset_manifest_id": str(proposal.get("dataset_manifest_id", "")),
                "feature_schema_id": str(proposal.get("feature_schema_id", "")),
                "label_policy_id": str(proposal.get("label_policy_id", "")),
                **(metadata or {}),
            },
        )
        logger = ComplianceLogger(
            db_path=db_path,
            table_prefix="learning_model_compliance_log",
        )
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
                "artifacts/evolution/learning_compliance_fallback.jsonl"
            )
            fallback_path.parent.mkdir(parents=True, exist_ok=True)
            with fallback_path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n")
            return {
                "state": state.value,
                "written": False,
                "db_path": str(db_path),
                "fallback_path": str(fallback_path),
                "error": str(exc),
            }

    def _create_learning_model_proposal_from_gate_payload(
        self,
        *,
        gate_payload: object,
        allow_warn_status: bool,
        source_trace_id: str,
    ) -> dict[str, object]:
        service = self._service
        gate = _mapping(gate_payload)
        if not gate:
            return {
                "accepted": False,
                "code": "missing_promotion_gate",
                "message": "promotion gate payload is required before proposal creation",
                "errors": ["missing_promotion_gate"],
            }
        if not bool(gate.get("ok", False)):
            return {
                "accepted": False,
                "code": "promotion_gate_not_ready",
                "message": "promotion gate reported errors; proposal artifact not generated",
                "errors": _string_list(gate.get("errors", [])) or ["promotion_gate_not_ready"],
            }

        gate_status = str(gate.get("status", "")).strip().lower()
        if gate_status == "fail":
            return {
                "accepted": False,
                "code": "promotion_gate_failed",
                "message": "failed promotion gate cannot enter proposal review",
                "errors": _string_list(gate.get("reason_codes", [])) or ["promotion_gate_failed"],
                "promotion_gate": gate,
            }
        if gate_status == "warn" and not allow_warn_status:
            return {
                "accepted": False,
                "code": "promotion_gate_warn_not_allowed",
                "message": "warning gate result requires allow_warn_status=true for proposal creation",
                "errors": _string_list(gate.get("reason_codes", []))
                or ["promotion_gate_warn_not_allowed"],
                "promotion_gate": gate,
            }

        shadow_model_id = str(gate.get("shadow_model_id", "")).strip()
        if not shadow_model_id:
            return {
                "accepted": False,
                "code": "shadow_model_missing",
                "message": "promotion gate did not resolve shadow model id",
                "errors": ["shadow_model_missing"],
            }

        shadow_entry = service._model_registry.get_by_id(shadow_model_id)
        if shadow_entry is None:
            return {
                "accepted": False,
                "code": "shadow_model_not_found",
                "message": f"model_id={shadow_model_id} not found in model registry",
                "errors": [f"shadow_model_not_found:{shadow_model_id}"],
            }
        champion_model_id = str(gate.get("champion_model_id", "")).strip()
        champion_entry = (
            service._model_registry.get_by_id(champion_model_id) if champion_model_id else None
        )

        now = datetime.now()
        proposal_id = (
            f"LRN-PRP-{now.strftime('%Y%m%d%H%M%S')}-"
            f"{len(service._learning_model_proposal_history) + 1:04d}"
        )
        artifact_rel_path = Path("learning_model_governance") / "proposals" / f"{proposal_id}.json"
        payload_uri = self._build_learning_payload_uri(artifact_rel_path)
        artifact_payload = {
            "artifact_version": 1,
            "proposal_id": proposal_id,
            "created_at": now.isoformat(),
            "proposal_type": "learning_model_promotion",
            "code_commit_id": str(service._config.evolution.code_commit_id),
            "shadow_model": shadow_entry.model_dump(mode="json"),
            "champion_model": (
                champion_entry.model_dump(mode="json") if champion_entry is not None else None
            ),
            "promotion_gate": deepcopy(gate),
            "protocol_contract": {
                "dataset_manifest_id": shadow_entry.dataset_manifest_id,
                "feature_schema_id": shadow_entry.feature_schema_id,
                "feature_schema_hash": shadow_entry.feature_schema_hash,
                "label_policy_id": shadow_entry.label_policy_id,
                "label_policy_hash": shadow_entry.label_policy_hash,
            },
        }
        artifact_path, payload_sha256 = self._write_learning_payload_artifact(
            payload_uri=payload_uri,
            payload=artifact_payload,
        )

        record = {
            "proposal_id": proposal_id,
            "timestamp": now.isoformat(),
            "status": "generated",
            "gate_status": gate_status,
            "approval_state": "pending",
            "release_state": "pending_approval",
            "shadow_model_id": shadow_model_id,
            "champion_model_id": champion_model_id,
            "dataset_manifest_id": shadow_entry.dataset_manifest_id,
            "feature_schema_id": shadow_entry.feature_schema_id,
            "feature_schema_hash": shadow_entry.feature_schema_hash,
            "label_policy_id": shadow_entry.label_policy_id,
            "label_policy_hash": shadow_entry.label_policy_hash,
            "artifact_uri": shadow_entry.artifact_uri,
            "code_commit_id": str(service._config.evolution.code_commit_id),
            "role": shadow_entry.role.value,
            "lifecycle_state": shadow_entry.lifecycle_state.value,
            "evaluation_split_names": _string_list(gate.get("evaluation_split_names", [])),
            "recommended_action": str(gate.get("recommended_action", "")),
            "reason_codes": _string_list(gate.get("reason_codes", [])),
            "blockers": _string_list(gate.get("blockers", [])),
            "warnings": _string_list(gate.get("warnings", [])),
            "metrics_snapshot": _mapping(gate.get("metrics_snapshot")),
            "gate_thresholds": _mapping(gate.get("gate_thresholds")),
            "payload_uri": payload_uri,
            "payload_path": str(artifact_path),
            "payload_sha256": payload_sha256,
            "source_trace_id": source_trace_id,
        }
        self._append_history(
            history_attr="_learning_model_proposal_history",
            latest_attr="_last_learning_model_proposal",
            record=record,
        )
        record["compliance"] = {
            "generated": self._write_learning_compliance_event(
                state=ComplianceState.GENERATED,
                proposal=record,
                event_time=now,
                trace_id=source_trace_id or f"learning-proposal-generated-{proposal_id}",
                metadata={"payload_uri": payload_uri, "gate_status": gate_status},
            ),
            "validated": self._write_learning_compliance_event(
                state=ComplianceState.VALIDATED,
                proposal=record,
                event_time=now,
                trace_id=source_trace_id or f"learning-proposal-validated-{proposal_id}",
                metadata={"payload_uri": payload_uri, "gate_status": gate_status},
            ),
        }
        service._record_audit_event(
            event_type="learning_model_proposal_created",
            trace_id=source_trace_id,
            payload={
                "proposal_id": proposal_id,
                "shadow_model_id": shadow_model_id,
                "champion_model_id": champion_model_id,
                "payload_uri": payload_uri,
                "gate_status": gate_status,
            },
        )
        return {
            "accepted": True,
            "mode": "learning_model_promotion_proposal",
            "proposal": record,
            "promotion_gate": gate,
            "errors": [],
        }

    def _notify_auto_promotion_rejection(
        self,
        *,
        proposal_id: str,
        workflow_payload: Mapping[str, object],
        proposal_payload: Mapping[str, object],
        source_trace_id: str,
    ) -> bool:
        service = self._service
        gate_payload = _mapping(workflow_payload.get("promotion_gate", {}))
        gate_status = str(
            gate_payload.get("status", "") or workflow_payload.get("status", "")
        ).strip() or "unknown"
        reason_codes = _string_list(gate_payload.get("reason_codes", []))
        proposal_errors = _string_list(proposal_payload.get("errors", []))
        detail_parts = [
            f"gate_status={gate_status}",
            f"proposal_id={proposal_id or '-'}",
            f"shadow_model_id={str(workflow_payload.get('shadow_model_id', '')).strip() or '-'}",
            f"dataset_manifest_id={str(workflow_payload.get('dataset_manifest_id', '')).strip() or '-'}",
        ]
        if reason_codes:
            detail_parts.append(f"reason_codes={','.join(reason_codes)}")
        if proposal_errors:
            detail_parts.append(f"errors={','.join(proposal_errors)}")
        service.notify(
            title="Learning gate rejected",
            content="\n".join(detail_parts),
            level="warn",
            trace_id=source_trace_id,
        )
        return True

    def _ensure_learning_model_approved(self, *, model_id: str) -> dict[str, object]:
        service = self._service
        entry = service._model_registry.get_by_id(model_id)
        if entry is None:
            return {
                "accepted": False,
                "code": "model_not_found",
                "message": f"model_id={model_id} not found",
            }
        if entry.lifecycle_state == ModelLifecycleState.BLOCKED:
            return {
                "accepted": False,
                "code": "model_blocked",
                "message": "blocked model cannot be manually approved",
                "model_id": model_id,
            }
        transition_records: list[dict[str, object]] = []
        current = entry
        if current.lifecycle_state == ModelLifecycleState.TRAINED:
            transition_records.append(
                service.update_model_registry_lifecycle(
                    model_id=model_id,
                    lifecycle_state=ModelLifecycleState.SHADOW_VALIDATED.value,
                )
            )
            current = service._model_registry.get_by_id(model_id) or current
        if current.lifecycle_state == ModelLifecycleState.SHADOW_VALIDATED:
            transition_records.append(
                service.update_model_registry_lifecycle(
                    model_id=model_id,
                    lifecycle_state=ModelLifecycleState.APPROVED.value,
                )
            )
        return {
            "accepted": True,
            "updated": bool(transition_records),
            "action": "approved" if transition_records else "already_approved",
            "reason": "manual_approval_applied" if transition_records else "already_approved",
            "records": transition_records,
        }

    def _resolve_latest_learning_proposal(
        self,
        *,
        proposal_id: str = "",
    ) -> dict[str, object] | None:
        service = self._service
        normalized = proposal_id.strip()
        if normalized:
            for item in reversed(service._learning_model_proposal_history):
                if str(item.get("proposal_id", "")).strip() == normalized:
                    return item
            return None
        latest = service._last_learning_model_proposal
        return latest if isinstance(latest, dict) else None

    def _latest_learning_proposals_by_id(self) -> dict[str, dict[str, object]]:
        latest_by_id: dict[str, dict[str, object]] = {}
        for item in self._service._learning_model_proposal_history:
            proposal_id = str(item.get("proposal_id", "")).strip()
            if proposal_id:
                latest_by_id[proposal_id] = item
        return dict(
            sorted(
                latest_by_id.items(),
                key=lambda row: str(row[1].get("timestamp", "")),
                reverse=True,
            )
        )

    def _latest_learning_approval_for_proposal(
        self,
        proposal_id: str,
    ) -> dict[str, object] | None:
        normalized = proposal_id.strip()
        if not normalized:
            return None
        for item in reversed(self._service._learning_model_approval_history):
            if str(item.get("proposal_id", "")).strip() == normalized:
                return item
        return None

    def _resolve_latest_learning_ticket(
        self,
        *,
        ticket_id: str = "",
    ) -> dict[str, object] | None:
        service = self._service
        normalized = ticket_id.strip()
        if normalized:
            for item in reversed(service._learning_model_release_ticket_history):
                if str(item.get("ticket_id", "")).strip() == normalized:
                    return item
            return None
        latest = service._last_learning_model_release_ticket
        return latest if isinstance(latest, dict) else None

    def _latest_learning_tickets_by_id(self) -> dict[str, dict[str, object]]:
        latest_by_id: dict[str, dict[str, object]] = {}
        for item in self._service._learning_model_release_ticket_history:
            ticket_id = str(item.get("ticket_id", "")).strip()
            if ticket_id:
                latest_by_id[ticket_id] = item
        return dict(
            sorted(
                latest_by_id.items(),
                key=lambda row: str(row[1].get("timestamp", "")),
                reverse=True,
            )
        )

    def _append_learning_proposal_snapshot(
        self,
        *,
        proposal: Mapping[str, object] | None,
        update: Mapping[str, object],
    ) -> dict[str, object]:
        if proposal is None:
            return {}
        merged = deepcopy(dict(proposal))
        merged.update(deepcopy(dict(update)))
        self._append_history(
            history_attr="_learning_model_proposal_history",
            latest_attr="_last_learning_model_proposal",
            record=merged,
        )
        return merged

    def _append_history(
        self,
        *,
        history_attr: str,
        latest_attr: str,
        record: Mapping[str, object],
    ) -> None:
        service = self._service
        snapshot = deepcopy(dict(record))
        setattr(service, latest_attr, snapshot)
        history = cast(list[dict[str, object]], getattr(service, history_attr))
        history.append(snapshot)
        limit = max(1, service._config.evolution.history_limit)
        if len(history) > limit:
            overflow = len(history) - limit
            if overflow > 0:
                setattr(service, history_attr, history[overflow:])

    def _learning_governance_root(self) -> Path:
        return self._service._resolve_evolution_path(self._service._config.evolution.suggestions_dir)

    def _build_learning_payload_uri(self, relative_path: Path) -> str:
        normalized = relative_path.as_posix().strip("/")
        return f"suggestions/{normalized}"

    def _resolve_learning_payload_uri(self, payload_uri: str) -> Path:
        normalized = payload_uri.replace("\\", "/").strip()
        if normalized.startswith("suggestions/"):
            suffix = normalized.removeprefix("suggestions/").strip("/")
            return self._learning_governance_root() / Path(suffix)
        return self._service._resolve_evolution_path(normalized)

    def _write_learning_payload_artifact(
        self,
        *,
        payload_uri: str,
        payload: Mapping[str, object],
    ) -> tuple[Path, str]:
        path = self._resolve_learning_payload_uri(payload_uri)
        path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(dict(payload), ensure_ascii=False, indent=2, default=str)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        path.write_text(serialized, encoding="utf-8")
        return path, digest


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := str(item).strip())]


def _normalize_checklist(value: object) -> list[dict[str, object]]:
    checklist: list[dict[str, object]] = []
    if not isinstance(value, list):
        return checklist
    for item in value:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        checklist.append({"name": name, "done": bool(item.get("done", False))})
    return checklist


def _set_checklist_flag(
    checklist: list[dict[str, object]],
    name: str,
    done: bool,
) -> list[dict[str, object]]:
    matched = False
    next_items: list[dict[str, object]] = []
    for item in checklist:
        if str(item.get("name", "")).strip() == name:
            next_items.append({"name": name, "done": done})
            matched = True
        else:
            next_items.append(dict(item))
    if not matched:
        next_items.append({"name": name, "done": done})
    return next_items


def _parse_iso_datetime(value: object) -> datetime | None:
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
