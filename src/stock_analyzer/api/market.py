"""Market data pipeline, risk, signals, scheduler and sync endpoints."""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query

from stock_analyzer.api.deps import get_service, get_verify_api_auth
from stock_analyzer.api.models import (
    PipelineRunRequest,
    SchedulerRunRequest,
    TdxSyncRunRequest,
    WarehouseSyncRunRequest,
)

router = APIRouter()


@router.post("/run/pipeline")
def run_pipeline(
    request: PipelineRunRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().run_pipeline(
        symbols=request.symbols,
        strategy=request.strategy,
        current_equity=request.current_equity,
        use_live_runtime=request.use_live_runtime,
        dry_run_execution=request.dry_run_execution,
        notify_enabled=request.notify_enabled,
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
    return {"results": get_service().run_due_jobs(now=now_dt, only_jobs=selected_jobs)}


@router.post("/tdx/sync/run")
def tdx_sync_run(
    request: TdxSyncRunRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return get_service().run_tdx_offline_sync(
        timestamp=now_dt,
        notify_enabled=request.notify_enabled,
        force=request.force,
        source_trace_id=request.source_trace_id,
    )


@router.get("/tdx/sync/latest")
def tdx_sync_latest() -> dict[str, object]:
    return {"report": get_service().latest_tdx_sync_report()}


@router.get("/tdx/sync/history")
def tdx_sync_history(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, object]:
    return get_service().tdx_sync_history(limit=limit)


@router.post("/warehouse/sync/run")
def warehouse_sync_run(
    request: WarehouseSyncRunRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return get_service().run_market_warehouse_sync(
        timestamp=now_dt,
        notify_enabled=request.notify_enabled,
        force=request.force,
        source_trace_id=request.source_trace_id,
        symbols=request.symbols or None,
        retry_failed_only=request.retry_failed_only,
        retry_report_trace_id=request.retry_report_trace_id,
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
