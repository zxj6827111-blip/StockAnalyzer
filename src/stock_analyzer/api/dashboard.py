"""Dashboard page redirects, portfolio overview and quick command endpoints."""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

import time
from uuid import uuid4

from fastapi import APIRouter, Depends, Query
from fastapi.responses import RedirectResponse

from stock_analyzer.api.deps import (
    dashboard_ops_enabled,
    get_config,
    get_service,
    get_verify_api_auth,
    set_dashboard_ops_enabled,
)
from stock_analyzer.api.models import (
    DashboardOpsToggleRequest,
    DashboardQuickCommandRequest,
    DashboardQuickReconcileRequest,
)
from stock_analyzer.command.channel import CommandEnvelope, SignedCommandProcessor

router = APIRouter()


@router.get("/dashboard", include_in_schema=False)
@router.get("/dashboard/", include_in_schema=False)
def dashboard_page() -> RedirectResponse:
    return RedirectResponse(url="/ui", status_code=307)


@router.get("/dashboard/recommendations", include_in_schema=False)
def dashboard_recommendations_page() -> RedirectResponse:
    return RedirectResponse(url="/ui/recommendations", status_code=307)


@router.get("/dashboard/stage", include_in_schema=False)
def dashboard_stage_page() -> RedirectResponse:
    return RedirectResponse(url="/ui/runtime-stage", status_code=307)


def _dashboard_quick_enabled() -> bool:
    return get_config().app.mode.strip().lower() == "simulation" and dashboard_ops_enabled()


def _dashboard_ops_state() -> dict[str, object]:
    config = get_config()
    advisory_only = bool(config.app.advisory_only)
    market_warehouse = get_service().market_warehouse_runtime_status()
    return {
        "mode": config.app.mode,
        "simulation_mode": config.app.mode.strip().lower() == "simulation",
        "enabled": _dashboard_quick_enabled(),
        "toggle_enabled": config.app.mode.strip().lower() == "simulation",
        "advisory_only": advisory_only,
        "execution_mode": "advisory_only" if advisory_only else "portfolio_auto_apply",
        "market_warehouse": market_warehouse,
    }


def _build_internal_command(
    action: str,
    payload: dict[str, object],
    command_id: str = "",
) -> CommandEnvelope:
    now_ts = int(time.time())
    normalized_action = action.strip()
    action_code = normalized_action.lower().replace(" ", "_")
    generated_id = command_id.strip() or f"dash-{action_code}-{now_ts}-{uuid4().hex[:8]}"
    signature = SignedCommandProcessor.build_signature(
        secret_key=get_config().command_channel.secret_key,
        command_id=generated_id,
        timestamp=now_ts,
        action=normalized_action,
        payload=payload,
    )
    return CommandEnvelope(
        command_id=generated_id,
        timestamp=now_ts,
        action=normalized_action,
        payload=payload,
        signature=signature,
    )


@router.get("/dashboard/portfolio")
def dashboard_portfolio(
    days: int = Query(default=7, ge=1, le=30),
    trade_limit: int = Query(default=120, ge=1, le=1000),
) -> dict[str, object]:
    return get_service().dashboard_portfolio(days=days, trade_limit=trade_limit)


@router.get("/dashboard/training-overview")
def dashboard_training_overview(
    history_limit: int = Query(default=6, ge=1, le=20),
) -> dict[str, object]:
    return get_service().training_overview(history_limit=history_limit)


@router.get("/dashboard/ops/state")
def dashboard_ops_state() -> dict[str, object]:
    return _dashboard_ops_state()


@router.post("/dashboard/ops/toggle")
def dashboard_ops_toggle(
    request: DashboardOpsToggleRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    if get_config().app.mode.strip().lower() != "simulation":
        return {
            "accepted": False,
            "code": "disabled",
            "message": "dashboard ops toggle only allowed in simulation mode",
            "state": _dashboard_ops_state(),
        }
    set_dashboard_ops_enabled(request.enabled)
    return {
        "accepted": True,
        "state": _dashboard_ops_state(),
    }


@router.post("/dashboard/command/quick")
def dashboard_quick_command(
    request: DashboardQuickCommandRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    if not _dashboard_quick_enabled():
        return {
            "accepted": False,
            "code": "disabled",
            "message": "quick dashboard command only allowed in simulation mode",
        }
    envelope = _build_internal_command(
        action=request.action,
        payload=request.payload,
        command_id=request.command_id,
    )
    result = get_service().execute_command(envelope)
    return {"command_id": envelope.command_id, "result": result}


@router.post("/dashboard/reconcile/quick")
def dashboard_quick_reconcile(
    request: DashboardQuickReconcileRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    if not _dashboard_quick_enabled():
        return {
            "accepted": False,
            "code": "disabled",
            "message": "quick dashboard reconcile only allowed in simulation mode",
        }
    trace_id = request.source_trace_id.strip() or f"dash-reconcile-{int(time.time())}"
    snapshot = get_service().update_broker_snapshot(
        positions=[item.model_dump() for item in request.positions],
        source_trace_id=trace_id,
    )
    if not request.run_reconcile:
        return {"trace_id": trace_id, "snapshot": snapshot}
    report = get_service().run_reconciliation(trace_id=trace_id)
    return {"trace_id": trace_id, "snapshot": snapshot, "report": report}
