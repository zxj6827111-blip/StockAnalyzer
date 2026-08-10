"""Market data pipeline, risk, signals, scheduler, sync and background-task endpoints."""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from stock_analyzer.api.deps import get_config, get_service, get_verify_api_auth
from stock_analyzer.api.models import (
    PipelineRunRequest,
    SchedulerRunRequest,
    TdxSyncRunRequest,
    WarehouseSyncRunRequest,
)
from stock_analyzer.ops.background_tasks import registry, submit_background_task
from stock_analyzer.ops.file_lock import DistributedFileLock

router = APIRouter()


@router.post("/run/pipeline", status_code=202)
def run_pipeline(
    request: PipelineRunRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return submit_background_task(
        background_tasks,
        name="run_pipeline",
        fn=lambda: get_service().run_pipeline(
            symbols=request.symbols,
            strategy=request.strategy,
            current_equity=request.current_equity,
            use_live_runtime=request.use_live_runtime,
            dry_run_execution=request.dry_run_execution,
            notify_enabled=request.notify_enabled,
        ),
    )


@router.get("/risk/status")
def risk_status() -> dict[str, object]:
    report = get_service().latest_report()
    if report is None:
        return {"status": "no_run"}
    return {
        "trace_id": report.get("trace_id"),
        "degraded_mode": report.get("degraded_mode"),
        "risk": report.get("risk"),
    }


@router.get("/signals/latest")
def latest_signals() -> dict[str, object]:
    payload = get_service().latest_signals_snapshot()
    signals = payload.get("signals")
    if not isinstance(signals, list) or not signals:
        return {"signals": []}
    return payload


@router.post("/scheduler/run_due")
def run_scheduler(
    request: SchedulerRunRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    selected_jobs = [request.job, *request.jobs]
    lock = _scheduler_leader_lock()
    if lock is not None and not lock.acquire():
        raise HTTPException(
            status_code=409,
            detail="another scheduler instance is running; retry later",
        )
    try:
        return {"results": get_service().run_due_jobs(now=now_dt, only_jobs=selected_jobs)}
    finally:
        if lock is not None:
            lock.release()


def _scheduler_leader_lock() -> DistributedFileLock | None:
    scheduler_config = get_config().scheduler
    if not scheduler_config.leader_lock_enabled:
        return None
    return DistributedFileLock(
        scheduler_config.leader_lock_path,
        stale_after_sec=scheduler_config.leader_lock_stale_after_sec,
    )


@router.post("/tdx/sync/run", status_code=202)
def tdx_sync_run(
    request: TdxSyncRunRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return submit_background_task(
        background_tasks,
        name="tdx_sync_run",
        fn=lambda: get_service().run_tdx_offline_sync(
            timestamp=now_dt,
            notify_enabled=request.notify_enabled,
            force=request.force,
            source_trace_id=request.source_trace_id,
        ),
    )


@router.get("/tdx/sync/latest")
def tdx_sync_latest() -> dict[str, object]:
    return {"report": get_service().latest_tdx_sync_report()}


@router.get("/tdx/sync/history")
def tdx_sync_history(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, object]:
    return get_service().tdx_sync_history(limit=limit)


@router.post("/warehouse/sync/run", status_code=202)
def warehouse_sync_run(
    request: WarehouseSyncRunRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return submit_background_task(
        background_tasks,
        name="warehouse_sync_run",
        fn=lambda: get_service().run_market_warehouse_sync(
            timestamp=now_dt,
            notify_enabled=request.notify_enabled,
            force=request.force,
            source_trace_id=request.source_trace_id,
            symbols=request.symbols or None,
            retry_failed_only=request.retry_failed_only,
            retry_report_trace_id=request.retry_report_trace_id,
        ),
    )


@router.get("/warehouse/sync/latest")
def warehouse_sync_latest() -> dict[str, object]:
    return {"report": get_service().latest_market_warehouse_report()}


@router.get("/warehouse/sync/status")
def warehouse_sync_status() -> dict[str, object]:
    return get_service().market_warehouse_runtime_status()


@router.get("/warehouse/background/status")
def warehouse_background_status() -> dict[str, object]:
    return get_service().market_warehouse_background_data_status()


@router.get("/warehouse/sync/history")
def warehouse_sync_history(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, object]:
    return get_service().market_warehouse_history(limit=limit)


@router.get("/tasks")
def tasks_recent(
    limit: int = Query(default=50, ge=1, le=200),
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    """List the most recently submitted background tasks (newest first)."""
    tasks = registry.list_recent(limit=limit)
    return {"tasks": tasks, "count": len(tasks)}


@router.get("/tasks/{task_id}")
def task_status(
    task_id: str,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    """Return the lifecycle entry of a submitted background task."""
    entry = registry.get(task_id)
    if entry is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="task_not_found")
    return entry
