"""Acceptance, baseline, backtest and stress validation endpoints."""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from stock_analyzer.api.deps import get_service, get_verify_api_auth
from stock_analyzer.api.models import (
    BaselineReportRequest,
    PhaseCheckpointRequest,
    V13AcceptanceBundleRequest,
    V13AcceptanceRequest,
    WalkForwardRequest,
    Week4AcceptanceRunRequest,
)

router = APIRouter()


@router.post("/backtest/walk_forward")
def walk_forward(
    request: WalkForwardRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().run_walk_forward(
        symbol=request.symbol,
        lookback_days=request.lookback_days,
    )


@router.post("/acceptance/baseline")
def acceptance_baseline(
    request: BaselineReportRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().generate_baseline_report(
        symbol=request.symbol,
        lookback_days=request.lookback_days,
        output_path=request.output_path,
    )


@router.post("/acceptance/phase_checkpoint")
def acceptance_phase_checkpoint(
    request: PhaseCheckpointRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().generate_phase_checkpoint(
        phase=request.phase,
        baseline_report_path=request.baseline_report_path,
        output_path=request.output_path,
    )


@router.post("/acceptance/v13")
def acceptance_v13(
    request: V13AcceptanceRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().generate_v13_acceptance_report(
        baseline_report_path=request.baseline_report_path,
        output_path=request.output_path,
    )


@router.post("/acceptance/v13/bundle")
def acceptance_v13_bundle(
    request: V13AcceptanceBundleRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().generate_v13_acceptance_bundle(
        symbol=request.symbol,
        lookback_days=request.lookback_days,
        baseline_output_path=request.baseline_output_path,
        v13_output_path=request.v13_output_path,
        run_week5_scan=request.run_week5_scan,
        week5_symbols=request.week5_symbols or None,
    )


@router.post("/stress/run")
def stress_run(
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().run_stress_tests()


@router.post("/acceptance/week4/run")
def acceptance_week4_run(
    request: Week4AcceptanceRunRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().run_week4_acceptance(
        sla_recent_runs=request.sla_recent_runs,
        export_enabled=request.export_enabled,
        notify_enabled=request.notify_enabled,
    )


@router.get("/acceptance/week4/latest")
def acceptance_week4_latest() -> dict[str, object]:
    report = get_service().latest_week4_acceptance_report()
    if report is None:
        return {"status": "no_report"}
    return {"report": report}


@router.get("/acceptance/week4/history")
def acceptance_week4_history(limit: int = Query(default=20, ge=1, le=500)) -> dict[str, object]:
    return get_service().week4_acceptance_history(limit=limit)
