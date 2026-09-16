"""Week-5 scan and signal-pool endpoints."""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from stock_analyzer.api.deps import get_service, get_verify_api_auth, parse_optional_datetime
from stock_analyzer.api.models import (
    NightlyRedeliveryRequest,
    Week5AutomationRunRequest,
    Week5ScanRunRequest,
)

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


@router.post("/week5/night-scan/run")
def week5_night_scan_run(
    request: Week5AutomationRunRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().run_week5_night_scan(
        timestamp=parse_optional_datetime(request.now),
        notify_enabled=request.notify_enabled,
        sync_watchlist=request.sync_watchlist,
    )


@router.get("/week5/night-scan/latest")
def week5_night_scan_latest(
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    """旧字段全部保留；晚报开关打开时额外返回 report_id / scan_status / 交付状态。

    只返回目标键名与状态，不含凭据，也不含完整接收人身份。
    """
    return get_service().latest_week5_night_scan()


@router.post("/week5/night-scan/reports/{report_id}/retry-delivery")
def week5_night_scan_retry_delivery(
    report_id: str,
    request: NightlyRedeliveryRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    """补发指定正式报告的未成功目标。仅入队，不阻塞等待网络结果。

    只接受已存在的 report_id（不允许传路径或接收人）；已全部成功时是幂等无操作。
    """
    return get_service().retry_nightly_delivery(
        report_id,
        confirm_unknown=request.confirm_unknown,
    )


@router.post("/week5/auction/run")
def week5_auction_run(
    request: Week5AutomationRunRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().run_week5_auction(
        timestamp=parse_optional_datetime(request.now),
        snapshot_id=request.snapshot_id,
        notify_enabled=request.notify_enabled,
    )


@router.get("/week5/auction/latest")
def week5_auction_latest(
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().latest_week5_auction()


@router.post("/week5/market-radar/run")
def week5_market_radar_automation_run(
    request: Week5AutomationRunRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().run_week5_automation_market_radar(
        timestamp=parse_optional_datetime(request.now),
        snapshot_id=request.snapshot_id,
        notify_enabled=request.notify_enabled,
    )


@router.get("/week5/market-radar/latest")
def week5_market_radar_automation_latest(
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().latest_week5_automation_market_radar()


@router.post("/week5/live-runtime/run")
def week5_live_runtime_automation_run(
    request: Week5AutomationRunRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().run_week5_automation_live_runtime(
        timestamp=parse_optional_datetime(request.now),
        notify_enabled=request.notify_enabled,
    )


@router.get("/week5/live-runtime/latest")
def week5_live_runtime_automation_latest(
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().latest_week5_automation_live_runtime()


@router.get("/week5/candidate-state")
def week5_candidate_state(
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().week5_candidate_state()
