"""Audit event endpoints."""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

from fastapi import APIRouter, Query

from stock_analyzer.api.deps import get_service

router = APIRouter()


@router.get("/audit/events")
def audit_events(
    limit: int = Query(default=200, ge=1, le=2000),
    event_type: str = Query(default=""),
    trace_id: str = Query(default=""),
) -> dict[str, object]:
    return get_service().audit_events(
        limit=limit,
        event_type=event_type,
        trace_id=trace_id,
    )


@router.get("/audit/trace/{trace_id}")
def audit_trace(trace_id: str) -> dict[str, object]:
    return get_service().trace_replay(trace_id=trace_id)
