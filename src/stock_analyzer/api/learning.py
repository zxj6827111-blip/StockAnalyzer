"""Learning governance, proposal, release and registry endpoints."""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from stock_analyzer.api.deps import get_service, get_verify_api_auth, parse_optional_datetime
from stock_analyzer.api.models import (
    LearningModelProposalApprovalRequest,
    LearningModelProposalRequest,
    LearningModelProposalRevokeRequest,
    LearningModelReleaseConfirmationWatchdogRequest,
    LearningModelReleaseTicketConfirmRequest,
    LearningModelReleaseTicketExecuteRequest,
    LearningModelReleaseTicketRequest,
    LearningModelReleaseTicketRollbackRequest,
    LearningRuntimeHistoryColdStartRequest,
)

router = APIRouter()


@router.post("/learning/models/proposal")
def learning_model_proposal_create(
    request: LearningModelProposalRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().create_learning_model_proposal(
        model_id=request.model_id,
        champion_model_id=request.champion_model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        max_samples=request.max_samples,
        min_samples=request.min_samples,
        learning_rate=request.learning_rate,
        signal_threshold=request.signal_threshold,
        preview_limit=request.preview_limit,
        min_shadow_v2_minus_champion_return=request.min_shadow_v2_minus_champion_return,
        max_shadow_v2_brier_delta=request.max_shadow_v2_brier_delta,
        max_shadow_v2_logloss_delta=request.max_shadow_v2_logloss_delta,
        max_signal_divergence_ratio=request.max_signal_divergence_ratio,
        approve_if_passed=request.approve_if_passed,
        block_if_failed=request.block_if_failed,
        allow_warn_status=request.allow_warn_status,
        source_trace_id=request.source_trace_id,
    )


@router.get("/learning/models/proposal/latest")
def learning_model_proposal_latest() -> dict[str, object]:
    proposal = get_service().latest_learning_model_proposal()
    if proposal is None:
        return {"status": "no_proposal"}
    return {"proposal": proposal}


@router.get("/learning/models/proposal/history")
def learning_model_proposal_history(
    limit: int = Query(default=20, ge=1, le=500),
    proposal_id: str = Query(default=""),
    status: str = Query(default=""),
) -> dict[str, object]:
    return get_service().learning_model_proposal_history(
        limit=limit,
        proposal_id=proposal_id,
        status=status,
    )


@router.post("/learning/models/proposal/approval")
def learning_model_proposal_approval(
    request: LearningModelProposalApprovalRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().record_learning_model_proposal_approval(
        approver=request.approver,
        approved=request.approved,
        proposal_id=request.proposal_id,
        note=request.note,
        timestamp=parse_optional_datetime(request.now or ""),
        source_trace_id=request.source_trace_id,
    )


@router.post("/learning/models/proposal/revoke")
def learning_model_proposal_revoke(
    request: LearningModelProposalRevokeRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().revoke_learning_model_proposal(
        revoked_by=request.revoked_by,
        proposal_id=request.proposal_id,
        note=request.note,
        revoke_model=request.revoke_model,
        timestamp=parse_optional_datetime(request.now or ""),
        source_trace_id=request.source_trace_id,
    )


@router.get("/learning/models/proposal/approval/latest")
def learning_model_proposal_approval_latest() -> dict[str, object]:
    record = get_service().latest_learning_model_approval()
    if record is None:
        return {"status": "no_record"}
    return {"record": record}


@router.get("/learning/models/proposal/approval/history")
def learning_model_proposal_approval_history(
    limit: int = Query(default=20, ge=1, le=500),
) -> dict[str, object]:
    return get_service().learning_model_approval_history(limit=limit)


@router.post("/learning/models/release/ticket")
def learning_model_release_ticket_issue(
    request: LearningModelReleaseTicketRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().issue_learning_model_release_ticket(
        operator=request.operator,
        proposal_id=request.proposal_id,
        note=request.note,
        timestamp=parse_optional_datetime(request.now or ""),
        source_trace_id=request.source_trace_id,
    )


@router.post("/learning/models/release/ticket/execute")
def learning_model_release_ticket_execute(
    request: LearningModelReleaseTicketExecuteRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().execute_learning_model_release_ticket(
        executor=request.executor,
        ticket_id=request.ticket_id,
        note=request.note,
        confirm_window=request.confirm_window,
        timestamp=parse_optional_datetime(request.now or ""),
        source_trace_id=request.source_trace_id,
    )


@router.post("/learning/models/release/ticket/confirm")
def learning_model_release_ticket_confirm(
    request: LearningModelReleaseTicketConfirmRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().confirm_learning_model_release_ticket(
        confirmer=request.confirmer,
        ticket_id=request.ticket_id,
        note=request.note,
        timestamp=parse_optional_datetime(request.now or ""),
        source_trace_id=request.source_trace_id,
    )


@router.post("/learning/models/release/ticket/rollback")
def learning_model_release_ticket_rollback(
    request: LearningModelReleaseTicketRollbackRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().rollback_learning_model_release_ticket(
        rollback_by=request.rollback_by,
        ticket_id=request.ticket_id,
        note=request.note,
        timestamp=parse_optional_datetime(request.now or ""),
        source_trace_id=request.source_trace_id,
    )


@router.post("/learning/models/release/confirmation/watchdog")
def learning_model_release_confirmation_watchdog(
    request: LearningModelReleaseConfirmationWatchdogRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().run_learning_model_release_confirmation_watchdog(
        now=parse_optional_datetime(request.now or ""),
        source_trace_id=request.source_trace_id,
    )


@router.get("/learning/models/release/ticket/latest")
def learning_model_release_ticket_latest() -> dict[str, object]:
    ticket = get_service().latest_learning_model_release_ticket()
    if ticket is None:
        return {"status": "no_ticket"}
    return {"ticket": ticket}


@router.get("/learning/models/release/ticket/history")
def learning_model_release_ticket_history(
    limit: int = Query(default=20, ge=1, le=500),
) -> dict[str, object]:
    return get_service().learning_model_release_ticket_history(limit=limit)


@router.get("/learning/models/release/ticket/timeline")
def learning_model_release_ticket_timeline(
    ticket_id: str = Query(default=""),
    status: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, object]:
    return get_service().learning_model_release_ticket_timeline(
        ticket_id=ticket_id,
        status=status,
        limit=limit,
    )


@router.get("/learning/models/governance/status")
def learning_model_governance_status(
    proposal_limit: int = Query(default=20, ge=1, le=200),
    ticket_limit: int = Query(default=20, ge=1, le=200),
) -> dict[str, object]:
    return get_service().learning_model_governance_status(
        proposal_limit=proposal_limit,
        ticket_limit=ticket_limit,
    )


@router.post("/learning/runtime-history/bootstrap")
def learning_runtime_history_bootstrap(
    request: LearningRuntimeHistoryColdStartRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    symbols = request.symbols if request.symbols else None
    return get_service().bootstrap_learning_from_runtime_history(
        archive_dir=request.archive_dir,
        symbols=symbols,
        build_manifest=request.build_manifest,
        calibration_ratio=request.calibration_ratio,
        test_ratio=request.test_ratio,
    )


@router.get("/learning/status")
def learning_status(manifest_limit: int = Query(default=5, ge=1, le=50)) -> dict[str, object]:
    return get_service().learning_protocol_status(manifest_limit=manifest_limit)


@router.get("/learning/store/status")
def learning_store_status() -> dict[str, object]:
    return get_service().learning_store_status()


@router.get("/learning/store/metrics")
def learning_store_metrics() -> dict[str, object]:
    return get_service().learning_store_metrics()


@router.get("/learning/manifests/status")
def learning_manifests_status(
    manifest_limit: int = Query(default=20, ge=1, le=200),
) -> dict[str, object]:
    return get_service().learning_manifests_status(manifest_limit=manifest_limit)
