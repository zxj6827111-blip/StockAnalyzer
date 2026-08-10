"""Idle queue endpoints."""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query

from stock_analyzer.api.deps import get_service, get_verify_api_auth
from stock_analyzer.api.models import IdleQueueAckRequest, IdleQueueRunRequest

router = APIRouter()


@router.post("/idle/run")
def idle_run(
    request: IdleQueueRunRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return get_service().run_idle_queue_cycle(now=now_dt, source_trace_id=request.source_trace_id)


@router.get("/idle/latest")
def idle_latest() -> dict[str, object]:
    return {"report": get_service().latest_idle_queue_report()}


@router.get("/idle/history")
def idle_history(limit: int = Query(default=20, ge=1, le=500)) -> dict[str, object]:
    return get_service().idle_queue_history(limit=limit)


@router.get("/idle/state")
def idle_state() -> dict[str, object]:
    return get_service().idle_queue_state()


@router.post("/idle/ack")
def idle_ack(
    request: IdleQueueAckRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return get_service().idle_queue_ack_blocked(
        task_id=request.task_id,
        clear_all=request.clear_all,
        now=now_dt,
    )
