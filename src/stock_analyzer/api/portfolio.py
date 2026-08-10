"""Portfolio, recommendation lifecycle and reconciliation endpoints."""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query

from stock_analyzer.api.deps import get_service, get_verify_api_auth
from stock_analyzer.api.models import (
    BrokerSnapshotRequest,
    ReconcileRunRequest,
)

router = APIRouter()


@router.get("/portfolio/positions")
def portfolio_positions() -> dict[str, object]:
    return {"positions": get_service().portfolio_positions()}


@router.get("/portfolio/trades")
def portfolio_trades(limit: int = Query(default=100, ge=1, le=1000)) -> dict[str, object]:
    return {"trades": get_service().portfolio_trades(limit=limit)}


@router.get("/recommendations/lifecycle")
def recommendations_lifecycle(
    status: str = Query(default=""),
    limit: int = Query(default=120, ge=1, le=1000),
) -> dict[str, object]:
    return get_service().recommendation_lifecycle(status=status, limit=limit)


@router.get("/portfolio/execution_bias")
def portfolio_execution_bias(
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, object]:
    return get_service().execution_bias_report(days=days, limit=limit)


@router.post("/portfolio/broker_snapshot")
def portfolio_broker_snapshot(
    request: BrokerSnapshotRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    positions = [item.model_dump() for item in request.positions]
    return get_service().update_broker_snapshot(
        positions=positions,
        source_trace_id=request.source_trace_id,
    )


@router.post("/portfolio/reconcile/run")
def portfolio_reconcile_run(
    request: ReconcileRunRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    now_dt = datetime.fromisoformat(request.now) if request.now else None
    return {"report": get_service().run_reconciliation(timestamp=now_dt)}


@router.get("/portfolio/reconcile/latest")
def portfolio_reconcile_latest() -> dict[str, object]:
    report = get_service().latest_reconcile_report()
    if report is None:
        return {"status": "no_reconcile"}
    return {"report": report}


@router.get("/portfolio/reconcile/weekly")
def portfolio_reconcile_weekly(days: int = Query(default=7, ge=1, le=30)) -> dict[str, object]:
    return get_service().reconcile_weekly_report(days=days)
