"""Week-7 kill-switch, cloud backup, factor lifecycle and sim-broker endpoints."""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query

from stock_analyzer.api.deps import get_service, get_verify_api_auth
from stock_analyzer.api.models import (
    Week7CloudBackupCheckRequest,
    Week7CloudBackupPingRequest,
    Week7FactorLifecycleRecordRequest,
    Week7FactorLifecycleResetRequest,
    Week7KillSwitchResetRequest,
    Week7SimBrokerRunRequest,
    Week7StrategyPerformanceRequest,
)

router = APIRouter()


@router.post("/week7/kill-switch/performance")
def week7_kill_switch_performance(
    request: Week7StrategyPerformanceRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().record_strategy_performance(
        month=request.month,
        strategy=request.strategy,
        strategy_return=request.strategy_return,
        benchmark_return=request.benchmark_return,
        note=request.note,
        source_trace_id=request.source_trace_id,
    )


@router.get("/week7/kill-switch/history")
def week7_kill_switch_history(
    strategy: str = Query(default=""),
    limit: int = Query(default=60, ge=1, le=500),
) -> dict[str, object]:
    return get_service().strategy_kill_switch_history(strategy=strategy, limit=limit)


@router.get("/week7/kill-switch/status")
def week7_kill_switch_status(strategy: str = Query(default="")) -> dict[str, object]:
    return get_service().strategy_kill_switch_status(strategy=strategy)


@router.post("/week7/kill-switch/reset")
def week7_kill_switch_reset(
    request: Week7KillSwitchResetRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().reset_strategy_kill_switch(
        strategy=request.strategy,
        resume_new_buy=request.resume_new_buy,
        source_trace_id=request.source_trace_id,
    )


@router.post("/week7/cloud-backup/ping")
def week7_cloud_backup_ping(
    request: Week7CloudBackupPingRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().cloud_backup_ping(
        source=request.source,
        source_trace_id=request.source_trace_id,
    )


@router.get("/week7/cloud-backup/status")
def week7_cloud_backup_status() -> dict[str, object]:
    return get_service().cloud_backup_status()


@router.post("/week7/cloud-backup/check")
def week7_cloud_backup_check(
    request: Week7CloudBackupCheckRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return get_service().run_cloud_backup_check(
        now=now_dt,
        source_trace_id=request.source_trace_id,
    )


@router.post("/week7/factor-lifecycle/record")
def week7_factor_lifecycle_record(
    request: Week7FactorLifecycleRecordRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().record_factor_lifecycle(
        month=request.month,
        strategy=request.strategy,
        top_features=[item.model_dump() for item in request.top_features],
        psr=request.psr,
        ic_mean=request.ic_mean,
        note=request.note,
        source_trace_id=request.source_trace_id,
    )


@router.get("/week7/factor-lifecycle/status")
def week7_factor_lifecycle_status(strategy: str = Query(default="")) -> dict[str, object]:
    return get_service().factor_lifecycle_status(strategy=strategy)


@router.get("/week7/factor-lifecycle/history")
def week7_factor_lifecycle_history(
    strategy: str = Query(default=""),
    limit: int = Query(default=60, ge=1, le=500),
) -> dict[str, object]:
    return get_service().factor_lifecycle_history(strategy=strategy, limit=limit)


@router.get("/week7/factor-lifecycle/graveyard")
def week7_factor_lifecycle_graveyard(
    strategy: str = Query(default=""),
    limit: int = Query(default=60, ge=1, le=500),
) -> dict[str, object]:
    return get_service().factor_graveyard(strategy=strategy, limit=limit)


@router.post("/week7/factor-lifecycle/reset")
def week7_factor_lifecycle_reset(
    request: Week7FactorLifecycleResetRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().reset_factor_lifecycle(
        strategy=request.strategy,
        source_trace_id=request.source_trace_id,
    )


@router.post("/week7/sim-broker/run")
def week7_sim_broker_run(
    request: Week7SimBrokerRunRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().run_week7_sim_broker_weekly(
        days=request.days,
        export_enabled=request.export_enabled,
        notify_enabled=request.notify_enabled,
        source_trace_id=request.source_trace_id,
    )


@router.get("/week7/sim-broker/latest")
def week7_sim_broker_latest() -> dict[str, object]:
    report = get_service().latest_week7_sim_broker_report()
    if report is None:
        return {"status": "no_report"}
    return {"report": report}


@router.get("/week7/sim-broker/history")
def week7_sim_broker_history(limit: int = Query(default=20, ge=1, le=500)) -> dict[str, object]:
    return get_service().week7_sim_broker_history(limit=limit)
