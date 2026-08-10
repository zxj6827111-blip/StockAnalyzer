"""Week-5 scan and signal-pool endpoints."""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from stock_analyzer.api.deps import get_service, get_verify_api_auth
from stock_analyzer.api.models import Week5ScanRunRequest

router = APIRouter()


@router.post("/week5/scan/run")
def week5_scan_run(
    request: Week5ScanRunRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    symbols = request.symbols if request.symbols else None
    return get_service().run_week5_scan(
        symbols=symbols,
        notify_enabled=request.notify_enabled,
        sync_watchlist=request.sync_watchlist,
        sync_reason=request.sync_reason,
        recovery_mode=request.recovery_mode,
    )


@router.get("/week5/scan/latest")
def week5_scan_latest() -> dict[str, object]:
    report = get_service().latest_week5_scan_report()
    if report is None:
        return {"status": "no_report"}
    return {"report": report}


@router.get("/week5/scan/history")
def week5_scan_history(limit: int = Query(default=20, ge=1, le=500)) -> dict[str, object]:
    return get_service().week5_scan_history(limit=limit)


@router.get("/week5/signal-pool/live")
def week5_signal_pool_live(
    limit: int = Query(default=30, ge=1, le=100),
    force_refresh: bool = Query(default=False),
) -> dict[str, object]:
    return get_service().week5_signal_pool_live(limit=limit, force_refresh=force_refresh)


@router.get("/week5/signal-pool/symbol/live")
def week5_signal_pool_symbol_live(
    symbol: str = Query(default=""),
    force_refresh: bool = Query(default=False),
) -> dict[str, object]:
    return get_service().week5_signal_pool_symbol_live(symbol=symbol, force_refresh=force_refresh)
