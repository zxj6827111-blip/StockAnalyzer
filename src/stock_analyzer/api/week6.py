"""Week-6 analysis, data quality, global snapshot and regulatory endpoints."""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from stock_analyzer.api.deps import get_service, get_verify_api_auth
from stock_analyzer.api.models import (
    Week6DataQualityRunRequest,
    Week6GlobalSnapshotRequest,
    Week6RegulatoryWatchlistRequest,
    Week6RunRequest,
)

router = APIRouter()


@router.post("/week6/run")
def week6_run(
    request: Week6RunRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    symbols = request.symbols if request.symbols else None
    return get_service().run_week6_analysis(symbols=symbols, notify_enabled=request.notify_enabled)


@router.get("/week6/latest")
def week6_latest() -> dict[str, object]:
    report = get_service().latest_week6_report()
    if report is None:
        return {"status": "no_report"}
    return {"report": report}


@router.get("/week6/history")
def week6_history(limit: int = Query(default=20, ge=1, le=500)) -> dict[str, object]:
    return get_service().week6_history(limit=limit)


@router.post("/week6/data-quality/run")
def week6_data_quality_run(
    request: Week6DataQualityRunRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    symbols = request.symbols if request.symbols else None
    return get_service().run_week6_data_prewarm(
        symbols=symbols,
        lookback_days=request.lookback_days,
        notify_enabled=request.notify_enabled,
        source_trace_id=request.source_trace_id,
    )


@router.get("/week6/data-quality/latest")
def week6_data_quality_latest() -> dict[str, object]:
    report = get_service().latest_week6_data_quality_report()
    if report is None:
        return {"status": "no_report"}
    return {"report": report}


@router.get("/week6/data-quality/history")
def week6_data_quality_history(
    limit: int = Query(default=20, ge=1, le=500),
) -> dict[str, object]:
    return get_service().week6_data_quality_history(limit=limit)


@router.post("/week6/global/snapshot")
def week6_global_snapshot(
    request: Week6GlobalSnapshotRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().update_global_market_snapshot(
        snapshot=request.model_dump(exclude={"source_trace_id"}),
        source_trace_id=request.source_trace_id,
    )


@router.get("/week6/global/snapshot")
def week6_global_snapshot_get() -> dict[str, object]:
    return get_service().global_market_snapshot()


@router.get("/week6/global/history")
def week6_global_history(limit: int = Query(default=50, ge=1, le=500)) -> dict[str, object]:
    return get_service().global_market_history(limit=limit)


@router.post("/week6/regulatory/watchlist")
def week6_regulatory_watchlist_set(
    request: Week6RegulatoryWatchlistRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    entries = [item.model_dump() for item in request.entries]
    return get_service().set_regulatory_watchlist(
        entries=entries,
        source_trace_id=request.source_trace_id,
    )


@router.get("/week6/regulatory/watchlist")
def week6_regulatory_watchlist_get() -> dict[str, object]:
    return get_service().regulatory_watchlist()
