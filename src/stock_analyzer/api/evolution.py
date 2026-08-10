"""Off-hours evolution and release governance endpoints."""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Query

from stock_analyzer.api.deps import get_service, get_verify_api_auth
from stock_analyzer.api.models import (
    EvolutionDrillRequest,
    EvolutionM3MaintenanceRequest,
    EvolutionM3SearchRequest,
    EvolutionM8SuggestRequest,
    EvolutionReleaseApprovalRequest,
    EvolutionReleaseAttemptRequest,
    EvolutionReleaseConfirmationWatchdogRequest,
    EvolutionReleaseTicketConfirmRequest,
    EvolutionReleaseTicketExecuteRequest,
    EvolutionReleaseTicketRequest,
    EvolutionReleaseTicketRollbackRequest,
    EvolutionRunRequest,
)
from stock_analyzer.ops.background_tasks import submit_background_task

router = APIRouter()


@router.post("/evolution/run", status_code=202)
def evolution_run(
    request: EvolutionRunRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    symbols = request.symbols if request.symbols else None
    return submit_background_task(
        background_tasks,
        name="evolution_run",
        fn=lambda: get_service().run_evolution_offhours(
            symbols=symbols,
            timestamp=now_dt,
            dry_run=request.dry_run,
            source_trace_id=request.source_trace_id,
        ),
    )


@router.post("/evolution/drill")
def evolution_drill(
    request: EvolutionDrillRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return get_service().run_evolution_drill(
        timestamp=now_dt,
        source_trace_id=request.source_trace_id,
    )


@router.get("/evolution/latest")
def evolution_latest() -> dict[str, object]:
    return {"report": get_service().latest_evolution_report()}


@router.get("/evolution/history")
def evolution_history(limit: int = Query(default=20, ge=1, le=500)) -> dict[str, object]:
    return get_service().evolution_history(limit=limit)


@router.get("/evolution/preflight")
def evolution_preflight() -> dict[str, object]:
    return get_service().evolution_preflight()


@router.get("/evolution/window_report")
def evolution_window_report(
    days: int = Query(default=10, ge=1, le=60),
    min_runs: int = Query(default=5, ge=1, le=1000),
) -> dict[str, object]:
    return get_service().evolution_window_report(days=days, min_runs=min_runs)


@router.post("/evolution/m3/maintenance")
def evolution_m3_maintenance(
    request: EvolutionM3MaintenanceRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return get_service().run_evolution_m3_maintenance(
        timestamp=now_dt,
        source_trace_id=request.source_trace_id,
    )


@router.post("/evolution/m3/search")
def evolution_m3_search(
    request: EvolutionM3SearchRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().run_evolution_m3_search(
        vector=request.vector,
        top_k=request.top_k,
        source_trace_id=request.source_trace_id,
    )


@router.post("/evolution/m8/suggest")
def evolution_m8_suggest(
    request: EvolutionM8SuggestRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    symbols = request.symbols if request.symbols else None
    return get_service().run_evolution_m8_suggest(
        symbols=symbols,
        top_k=request.top_k,
        timestamp=now_dt,
        source_trace_id=request.source_trace_id,
    )


@router.post("/evolution/release/attempt", status_code=202)
def evolution_release_attempt(
    request: EvolutionReleaseAttemptRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return submit_background_task(
        background_tasks,
        name="evolution_release_attempt",
        fn=lambda: get_service().attempt_evolution_release(
            days=request.days,
            min_runs=request.min_runs,
            now=now_dt,
            source_trace_id=request.source_trace_id,
        ),
    )


@router.get("/evolution/release/latest")
def evolution_release_latest() -> dict[str, object]:
    decision = get_service().latest_evolution_release_gate()
    if decision is None:
        return {"status": "no_decision"}
    return {"decision": decision}


@router.get("/evolution/release/history")
def evolution_release_history(limit: int = Query(default=20, ge=1, le=500)) -> dict[str, object]:
    return get_service().evolution_release_gate_history(limit=limit)


@router.post("/evolution/release/approval")
def evolution_release_approval(
    request: EvolutionReleaseApprovalRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return get_service().record_evolution_release_approval(
        approver=request.approver,
        approved=request.approved,
        note=request.note,
        timestamp=now_dt,
        source_trace_id=request.source_trace_id,
    )


@router.get("/evolution/release/approval/latest")
def evolution_release_approval_latest() -> dict[str, object]:
    record = get_service().latest_evolution_release_approval()
    if record is None:
        return {"status": "no_record"}
    return {"record": record}


@router.get("/evolution/release/approval/history")
def evolution_release_approval_history(
    limit: int = Query(default=20, ge=1, le=500),
) -> dict[str, object]:
    return get_service().evolution_release_approval_history(limit=limit)


@router.post("/evolution/release/ticket")
def evolution_release_ticket(
    request: EvolutionReleaseTicketRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return get_service().issue_evolution_release_ticket(
        operator=request.operator,
        note=request.note,
        timestamp=now_dt,
        source_trace_id=request.source_trace_id,
    )


@router.post("/evolution/release/ticket/execute", status_code=202)
def evolution_release_ticket_execute(
    request: EvolutionReleaseTicketExecuteRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return submit_background_task(
        background_tasks,
        name="evolution_release_ticket_execute",
        fn=lambda: get_service().execute_evolution_release_ticket(
            executor=request.executor,
            ticket_id=request.ticket_id,
            note=request.note,
            confirm_window=request.confirm_window,
            timestamp=now_dt,
            source_trace_id=request.source_trace_id,
        ),
    )


@router.post("/evolution/release/ticket/confirm")
def evolution_release_ticket_confirm(
    request: EvolutionReleaseTicketConfirmRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return get_service().confirm_evolution_release_ticket(
        confirmer=request.confirmer,
        ticket_id=request.ticket_id,
        note=request.note,
        timestamp=now_dt,
        source_trace_id=request.source_trace_id,
    )


@router.post("/evolution/release/ticket/rollback")
def evolution_release_ticket_rollback(
    request: EvolutionReleaseTicketRollbackRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return get_service().rollback_evolution_release_ticket(
        rollback_by=request.rollback_by,
        ticket_id=request.ticket_id,
        note=request.note,
        timestamp=now_dt,
        source_trace_id=request.source_trace_id,
    )


@router.post("/evolution/release/confirmation/watchdog")
def evolution_release_confirmation_watchdog(
    request: EvolutionReleaseConfirmationWatchdogRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return get_service().run_evolution_release_confirmation_watchdog(
        now=now_dt,
        source_trace_id=request.source_trace_id,
    )


@router.get("/evolution/release/ticket/latest")
def evolution_release_ticket_latest() -> dict[str, object]:
    ticket = get_service().latest_evolution_release_ticket()
    if ticket is None:
        return {"status": "no_ticket"}
    return {"ticket": ticket}


@router.get("/evolution/release/ticket/history")
def evolution_release_ticket_history(
    limit: int = Query(default=20, ge=1, le=500),
) -> dict[str, object]:
    return get_service().evolution_release_ticket_history(limit=limit)


@router.get("/evolution/release/ticket/timeline")
def evolution_release_ticket_timeline(
    ticket_id: str = Query(default=""),
    status: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, object]:
    return get_service().evolution_release_ticket_timeline(
        ticket_id=ticket_id,
        status=status,
        limit=limit,
    )
