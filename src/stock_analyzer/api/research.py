"""Research report endpoints: signal-quality audits and phase-D research."""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel

from stock_analyzer.api.deps import get_service, get_verify_api_auth
from stock_analyzer.api.models import (
    PhaseDAlphalensReportRequest,
    PhaseDCatBoostShadowReportRequest,
    PhaseDFinbertReportRequest,
    PhaseDFinrlReportRequest,
    PhaseDHeavyTsReportRequest,
    PhaseDQlibBridgeReportRequest,
    PhaseDShapReportRequest,
    PhaseDTabularDeepReportRequest,
    PhaseDTftReportRequest,
    SignalQualityAuditRequest,
)
from stock_analyzer.ops.background_tasks import submit_background_task

router = APIRouter()

_report_kwarg_keys: tuple[str, ...] = (
    "split_names",
    "factor_columns",
    "feature_columns",
)
_path_kwarg_keys: tuple[str, ...] = ("output_path", "output_dir")


def _phase_d_request_kwargs(request: BaseModel) -> dict[str, object]:
    """Translate a phase-D report body into the service method's kwargs.

    Mirrors the historical per-endpoint translation: empty collections and
    empty path strings become ``None``, and an empty ``horizons`` list falls
    back to the default horizon set.
    """
    kwargs = dict(request.model_dump())
    for key in _report_kwarg_keys:
        if key in kwargs and not kwargs[key]:
            kwargs[key] = None
    for key in _path_kwarg_keys:
        if key in kwargs and not kwargs[key]:
            kwargs[key] = None
    if "horizons" in kwargs and not kwargs["horizons"]:
        kwargs["horizons"] = (1, 5, 10)
    return kwargs


def _register_report_endpoint(
    *,
    path: str,
    request_model: type[Any],
    task_name: str,
    service_method: str,
) -> None:
    """Register one 202-async phase-D report endpoint (POST).

    The endpoint defers ``get_service()`` resolution into the background task
    and keeps the historical route name/auth/status-code semantics; only the
    request-model annotation is substituted at registration time.
    """

    def _endpoint(
        request: BaseModel,
        background_tasks: BackgroundTasks,
        _auth: None = Depends(get_verify_api_auth()),
    ) -> dict[str, object]:
        return submit_background_task(
            background_tasks,
            name=task_name,
            fn=lambda: getattr(get_service(), service_method)(**_phase_d_request_kwargs(request)),
        )

    _endpoint.__annotations__["request"] = request_model
    _endpoint.__name__ = service_method
    router.add_api_route(
        path,
        _endpoint,
        methods=["POST"],
        status_code=202,
        name=service_method,
    )


@router.post("/research/signal-quality/run", status_code=202)
def run_signal_quality_audit(
    request: SignalQualityAuditRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return submit_background_task(
        background_tasks,
        name="signal_quality_audit",
        fn=lambda: get_service().run_signal_quality_audit(
            limit=request.limit,
            include_audit_events=request.include_audit_events,
        ),
    )


@router.get("/research/signal-quality/latest")
def latest_signal_quality_audit() -> dict[str, object]:
    return get_service().latest_signal_quality_audit()


@router.get("/research/signal-quality/history")
def signal_quality_audit_history(
    limit: int = Query(default=20, ge=1, le=200),
) -> dict[str, object]:
    return get_service().signal_quality_audit_history(limit=limit)


_register_report_endpoint(
    path="/research/alphalens/report",
    request_model=PhaseDAlphalensReportRequest,
    task_name="phase_d_alphalens",
    service_method="build_phase_d_alphalens_report",
)
_register_report_endpoint(
    path="/research/shap/report",
    request_model=PhaseDShapReportRequest,
    task_name="phase_d_shap",
    service_method="build_phase_d_shap_report",
)
_register_report_endpoint(
    path="/research/catboost-shadow/report",
    request_model=PhaseDCatBoostShadowReportRequest,
    task_name="phase_d_catboost_shadow",
    service_method="build_phase_d_catboost_shadow_report",
)
_register_report_endpoint(
    path="/research/finbert/report",
    request_model=PhaseDFinbertReportRequest,
    task_name="phase_d_finbert",
    service_method="build_phase_d_finbert_report",
)
_register_report_endpoint(
    path="/research/qlib-bridge/report",
    request_model=PhaseDQlibBridgeReportRequest,
    task_name="phase_d_qlib_bridge",
    service_method="build_phase_d_qlib_bridge_report",
)
_register_report_endpoint(
    path="/research/tabular-deep/report",
    request_model=PhaseDTabularDeepReportRequest,
    task_name="phase_d_tabular_deep",
    service_method="build_phase_d_tabular_deep_report",
)
_register_report_endpoint(
    path="/research/tft/report",
    request_model=PhaseDTftReportRequest,
    task_name="phase_d_tft",
    service_method="build_phase_d_tft_report",
)
_register_report_endpoint(
    path="/research/finrl/report",
    request_model=PhaseDFinrlReportRequest,
    task_name="phase_d_finrl",
    service_method="build_phase_d_finrl_report",
)
_register_report_endpoint(
    path="/research/heavy-ts/report",
    request_model=PhaseDHeavyTsReportRequest,
    task_name="phase_d_heavy_ts",
    service_method="build_phase_d_heavy_ts_report",
)


@router.get("/research/d6/registry")
def phase_d6_registry(output_path: str = Query(default="")) -> dict[str, object]:
    return get_service().generate_phase_d6_registry_report(output_path=output_path or None)
