"""Runtime status, SLA and stage endpoints."""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query

from stock_analyzer.api.deps import get_service, get_verify_api_auth, parse_optional_datetime
from stock_analyzer.api.models import RuntimeArchiveRunRequest

router = APIRouter()


@router.get("/runtime/sla")
def runtime_sla(
    recent_runs: int = Query(default=50, ge=1, le=1000),
    session_scope: str = Query(default="all"),
    job_scope: str = Query(default="all"),
    target_ms: int = Query(default=60000, ge=1),
    alert_target_ms: int = Query(default=30000, ge=1),
    max_symbol_count: int | None = Query(default=None, ge=1),
) -> dict[str, object]:
    return get_service().sla_report(
        recent_runs=recent_runs,
        session_scope=session_scope,
        job_scope=job_scope,
        target_ms=target_ms,
        alert_target_ms=alert_target_ms,
        max_symbol_count=max_symbol_count,
    )


@router.get("/runtime/stage")
def runtime_stage(
    now: str = Query(default=""),
    deep: bool = Query(default=False),
) -> dict[str, object]:
    return get_service().runtime_stage_snapshot(now=parse_optional_datetime(now), deep=deep)


@router.get("/runtime/stage/deep")
def runtime_stage_deep(now: str = Query(default="")) -> dict[str, object]:
    return get_service().runtime_stage_snapshot(now=parse_optional_datetime(now), deep=True)


@router.get("/runtime/history/archive/status")
def runtime_history_archive_status(
    limit: int = Query(default=20, ge=1, le=500),
) -> dict[str, object]:
    return get_service().runtime_history_archive_status(limit=limit)


@router.post("/runtime/history/archive/run")
def runtime_history_archive_run(
    request: RuntimeArchiveRunRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return get_service().archive_runtime_history(now=now_dt, force=request.force)


@router.get("/m3/profile/status")
def m3_profile_status() -> dict[str, object]:
    return get_service().m3_profile_status()
