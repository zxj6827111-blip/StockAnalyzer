"""Research report endpoints: signal-quality audits and phase-D research."""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Query

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


@router.post("/research/alphalens/report", status_code=202)
def build_phase_d_alphalens_report(
    request: PhaseDAlphalensReportRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return submit_background_task(
        background_tasks,
        name="phase_d_alphalens",
        fn=lambda: get_service().build_phase_d_alphalens_report(
            model_id=request.model_id,
            split_names=request.split_names or None,
            max_rows=request.max_rows,
            factor_columns=request.factor_columns or None,
            horizons=request.horizons or (1, 5, 10),
            quantiles=request.quantiles,
            output_path=request.output_path or None,
        ),
    )


@router.post("/research/shap/report", status_code=202)
def build_phase_d_shap_report(
    request: PhaseDShapReportRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return submit_background_task(
        background_tasks,
        name="phase_d_shap",
        fn=lambda: get_service().build_phase_d_shap_report(
            model_id=request.model_id,
            split_names=request.split_names or None,
            max_rows=request.max_rows,
            prediction_column=request.prediction_column,
            baseline_importance=request.baseline_importance,
            drift_threshold=request.drift_threshold,
            top_k=request.top_k,
            output_path=request.output_path or None,
        ),
    )


@router.post("/research/catboost-shadow/report", status_code=202)
def build_phase_d_catboost_shadow_report(
    request: PhaseDCatBoostShadowReportRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return submit_background_task(
        background_tasks,
        name="phase_d_catboost_shadow",
        fn=lambda: get_service().build_phase_d_catboost_shadow_report(
            model_id=request.model_id,
            split_names=request.split_names or None,
            max_rows=request.max_rows,
            feature_columns=request.feature_columns or None,
            label_column=request.label_column,
            baseline_probability_column=request.baseline_probability_column,
            test_ratio=request.test_ratio,
            random_seed=request.random_seed,
            output_path=request.output_path or None,
        ),
    )


@router.post("/research/finbert/report", status_code=202)
def build_phase_d_finbert_report(
    request: PhaseDFinbertReportRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return submit_background_task(
        background_tasks,
        name="phase_d_finbert",
        fn=lambda: get_service().build_phase_d_finbert_report(
            records=request.records,
            model_path=request.model_path,
            include_neutral=request.include_neutral,
            output_path=request.output_path or None,
        ),
    )


@router.post("/research/qlib-bridge/report", status_code=202)
def build_phase_d_qlib_bridge_report(
    request: PhaseDQlibBridgeReportRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return submit_background_task(
        background_tasks,
        name="phase_d_qlib_bridge",
        fn=lambda: get_service().build_phase_d_qlib_bridge_report(
            model_id=request.model_id,
            split_names=request.split_names or None,
            max_rows=request.max_rows,
            feature_columns=request.feature_columns or None,
            label_column=request.label_column,
            train_ratio=request.train_ratio,
            valid_ratio=request.valid_ratio,
            output_dir=request.output_dir or None,
        ),
    )


@router.post("/research/tabular-deep/report", status_code=202)
def build_phase_d_tabular_deep_report(
    request: PhaseDTabularDeepReportRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return submit_background_task(
        background_tasks,
        name="phase_d_tabular_deep",
        fn=lambda: get_service().build_phase_d_tabular_deep_report(
            model_id=request.model_id,
            split_names=request.split_names or None,
            max_rows=request.max_rows,
            feature_columns=request.feature_columns or None,
            label_column=request.label_column,
            baseline_probability_column=request.baseline_probability_column,
            test_ratio=request.test_ratio,
            random_seed=request.random_seed,
            output_path=request.output_path or None,
        ),
    )


@router.post("/research/tft/report", status_code=202)
def build_phase_d_tft_report(
    request: PhaseDTftReportRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return submit_background_task(
        background_tasks,
        name="phase_d_tft",
        fn=lambda: get_service().build_phase_d_tft_report(
            model_id=request.model_id,
            split_names=request.split_names or None,
            max_rows=request.max_rows,
            horizon=request.horizon,
            encoder_length=request.encoder_length,
            train_ratio=request.train_ratio,
            output_path=request.output_path or None,
        ),
    )


@router.post("/research/finrl/report", status_code=202)
def build_phase_d_finrl_report(
    request: PhaseDFinrlReportRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return submit_background_task(
        background_tasks,
        name="phase_d_finrl",
        fn=lambda: get_service().build_phase_d_finrl_report(
            model_id=request.model_id,
            split_names=request.split_names or None,
            max_rows=request.max_rows,
            feature_columns=request.feature_columns or None,
            reward_column=request.reward_column,
            baseline_probability_column=request.baseline_probability_column,
            test_ratio=request.test_ratio,
            random_seed=request.random_seed,
            action_threshold=request.action_threshold,
            output_path=request.output_path or None,
        ),
    )


@router.post("/research/heavy-ts/report", status_code=202)
def build_phase_d_heavy_ts_report(
    request: PhaseDHeavyTsReportRequest,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return submit_background_task(
        background_tasks,
        name="phase_d_heavy_ts",
        fn=lambda: get_service().build_phase_d_heavy_ts_report(
            model_id=request.model_id,
            split_names=request.split_names or None,
            max_rows=request.max_rows,
            horizon=request.horizon,
            lookback=request.lookback,
            test_ratio=request.test_ratio,
            random_seed=request.random_seed,
            output_path=request.output_path or None,
        ),
    )


@router.get("/research/d6/registry")
def phase_d6_registry(output_path: str = Query(default="")) -> dict[str, object]:
    return get_service().generate_phase_d6_registry_report(output_path=output_path or None)
