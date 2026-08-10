"""Model registry, shadow evaluation and execution-aware report endpoints."""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from stock_analyzer.api.deps import (
    ensure_params_not_frozen,
    get_service,
    get_verify_api_auth,
    parse_optional_datetime,
)
from stock_analyzer.api.models import (
    BootstrapActiveChampionRequest,
    ChampionShadowReportBuildRequest,
    ExecutionAwareReportBuildRequest,
    LearningModelPromotionGateRequest,
    ModelRegistryLifecycleRequest,
    ModelRegistryRoleRequest,
    RegisterModelArtifactRequest,
    ShadowDatasetBuildRequest,
    ShadowOnlineV2ReportBuildRequest,
)

router = APIRouter()


@router.post("/models/registry/promotion-gate")
def model_registry_promotion_gate(
    request: LearningModelPromotionGateRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().evaluate_learning_model_promotion_gate(
        model_id=request.model_id,
        champion_model_id=request.champion_model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        max_samples=request.max_samples,
        min_samples=request.min_samples,
        learning_rate=request.learning_rate,
        signal_threshold=request.signal_threshold,
        preview_limit=request.preview_limit,
        min_shadow_v2_minus_champion_return=request.min_shadow_v2_minus_champion_return,
        max_shadow_v2_brier_delta=request.max_shadow_v2_brier_delta,
        max_shadow_v2_logloss_delta=request.max_shadow_v2_logloss_delta,
        max_signal_divergence_ratio=request.max_signal_divergence_ratio,
        approve_if_passed=request.approve_if_passed,
        block_if_failed=request.block_if_failed,
    )


@router.get("/models/registry")
def model_registry_entries(
    limit: int = Query(default=20, ge=1, le=200),
    role: str = "",
    lifecycle_state: str = "",
) -> dict[str, object]:
    return get_service().model_registry_entries(
        limit=limit,
        role=role,
        lifecycle_state=lifecycle_state,
    )


@router.get("/models/registry/status")
def model_registry_status(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, object]:
    return get_service().model_registry_status(limit=limit)


@router.post("/models/registry/register")
def register_model_artifact(
    request: RegisterModelArtifactRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().register_model_artifact(
        artifact_path=request.artifact_path,
        role=request.role,
        lifecycle_state=request.lifecycle_state,
        source=request.source,
        parent_model_id=request.parent_model_id,
    )


@router.post("/models/registry/bootstrap-active-champion")
def bootstrap_active_champion(
    request: BootstrapActiveChampionRequest,
    _auth: None = Depends(get_verify_api_auth()),
    _frozen: None = Depends(ensure_params_not_frozen),
) -> dict[str, object]:
    return get_service().bootstrap_active_champion_from_artifact(
        artifact_path=request.artifact_path,
        source=request.source,
        allow_legacy_production_artifact=request.allow_legacy_production_artifact,
        model_id=request.model_id,
    )


@router.post("/models/registry/lifecycle")
def update_model_registry_lifecycle(
    request: ModelRegistryLifecycleRequest,
    _auth: None = Depends(get_verify_api_auth()),
    _frozen: None = Depends(ensure_params_not_frozen),
) -> dict[str, object]:
    return get_service().update_model_registry_lifecycle(
        model_id=request.model_id,
        lifecycle_state=request.lifecycle_state,
        blocked_reason=request.blocked_reason,
        timestamp=parse_optional_datetime(request.timestamp or ""),
    )


@router.post("/models/registry/role")
def update_model_registry_role(
    request: ModelRegistryRoleRequest,
    _auth: None = Depends(get_verify_api_auth()),
    _frozen: None = Depends(ensure_params_not_frozen),
) -> dict[str, object]:
    return get_service().update_model_registry_role(
        model_id=request.model_id,
        role=request.role,
        timestamp=parse_optional_datetime(request.timestamp or ""),
    )


@router.get("/models/registry/{model_id}")
def model_registry_entry(model_id: str) -> dict[str, object] | None:
    return get_service().model_registry_entry(model_id=model_id)


@router.post("/models/shadow-dataset")
def build_shadow_dataset(
    request: ShadowDatasetBuildRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().build_shadow_dataset(
        model_id=request.model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        include_rows=request.include_rows,
        preview_limit=request.preview_limit,
    )


@router.post("/models/champion-shadow-report")
def build_champion_shadow_report(
    request: ChampionShadowReportBuildRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().build_champion_shadow_report(
        model_id=request.model_id,
        champion_model_id=request.champion_model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        signal_threshold=request.signal_threshold,
        include_rows=request.include_rows,
        preview_limit=request.preview_limit,
    )


@router.post("/models/shadow-online-v2-report")
def build_shadow_online_v2_report(
    request: ShadowOnlineV2ReportBuildRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().build_shadow_online_v2_report(
        model_id=request.model_id,
        champion_model_id=request.champion_model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        max_samples=request.max_samples,
        min_samples=request.min_samples,
        learning_rate=request.learning_rate,
        signal_threshold=request.signal_threshold,
        include_rows=request.include_rows,
        preview_limit=request.preview_limit,
    )


@router.get("/shadow/v2/status")
def shadow_v2_status(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, object]:
    return get_service().shadow_v2_status(limit=limit)


@router.post("/models/execution-aware-report")
def build_execution_aware_report(
    request: ExecutionAwareReportBuildRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().build_execution_aware_report(
        model_id=request.model_id,
        execution_risk_artifact_path=request.execution_risk_artifact_path,
        champion_model_id=request.champion_model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        include_rows=request.include_rows,
        preview_limit=request.preview_limit,
    )
