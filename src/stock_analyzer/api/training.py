"""Model training and execution-risk endpoints."""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from stock_analyzer.api.deps import get_service, get_verify_api_auth, parse_optional_datetime
from stock_analyzer.api.models import (
    ExecutionRiskTrainRequest,
    LearningManifestShadowPromotionGateRequest,
    LearningManifestShadowProposalRequest,
    LearningManifestShadowValidationRequest,
    LearningManifestTrainingRequest,
    TrainModelsRequest,
)

router = APIRouter()


@router.post("/train/models")
def train_models(
    request: TrainModelsRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().train_models(
        symbol=request.symbol,
        lookback_days=request.lookback_days,
        artifact_path=request.artifact_path,
        full_market=request.full_market,
        max_symbols=request.max_symbols,
    )


@router.post("/train/learning-manifest")
def train_learning_manifest(
    request: LearningManifestTrainingRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().train_learning_manifest(
        dataset_manifest_id=request.dataset_manifest_id,
        artifact_path=request.artifact_path,
        load_predictor=request.load_predictor,
        register_model=request.register_model,
    )


@router.post("/train/learning-manifest/shadow-validate")
def train_learning_manifest_shadow_validate(
    request: LearningManifestShadowValidationRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().run_learning_manifest_shadow_validation(
        dataset_manifest_id=request.dataset_manifest_id,
        artifact_path=request.artifact_path,
        champion_model_id=request.champion_model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        include_rows=request.include_rows,
        preview_limit=request.preview_limit,
        max_samples=request.max_samples,
        min_samples=request.min_samples,
        learning_rate=request.learning_rate,
        signal_threshold=request.signal_threshold,
        load_predictor=request.load_predictor,
        mark_shadow_validated=request.mark_shadow_validated,
    )


@router.post("/train/learning-manifest/shadow-promote")
def train_learning_manifest_shadow_promote(
    request: LearningManifestShadowPromotionGateRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().run_learning_manifest_shadow_promotion_gate(
        dataset_manifest_id=request.dataset_manifest_id,
        artifact_path=request.artifact_path,
        champion_model_id=request.champion_model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        include_rows=request.include_rows,
        preview_limit=request.preview_limit,
        max_samples=request.max_samples,
        min_samples=request.min_samples,
        learning_rate=request.learning_rate,
        signal_threshold=request.signal_threshold,
        load_predictor=request.load_predictor,
        mark_shadow_validated=request.mark_shadow_validated,
        min_shadow_v2_minus_champion_return=request.min_shadow_v2_minus_champion_return,
        max_shadow_v2_brier_delta=request.max_shadow_v2_brier_delta,
        max_shadow_v2_logloss_delta=request.max_shadow_v2_logloss_delta,
        max_signal_divergence_ratio=request.max_signal_divergence_ratio,
        approve_if_passed=request.approve_if_passed,
        block_if_failed=request.block_if_failed,
    )


@router.post("/train/learning-manifest/shadow-proposal")
def train_learning_manifest_shadow_proposal(
    request: LearningManifestShadowProposalRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().run_learning_manifest_shadow_proposal(
        dataset_manifest_id=request.dataset_manifest_id,
        artifact_path=request.artifact_path,
        champion_model_id=request.champion_model_id,
        split_names=request.split_names or None,
        max_rows=request.max_rows,
        include_rows=request.include_rows,
        preview_limit=request.preview_limit,
        max_samples=request.max_samples,
        min_samples=request.min_samples,
        learning_rate=request.learning_rate,
        signal_threshold=request.signal_threshold,
        load_predictor=request.load_predictor,
        mark_shadow_validated=request.mark_shadow_validated,
        min_shadow_v2_minus_champion_return=request.min_shadow_v2_minus_champion_return,
        max_shadow_v2_brier_delta=request.max_shadow_v2_brier_delta,
        max_shadow_v2_logloss_delta=request.max_shadow_v2_logloss_delta,
        max_signal_divergence_ratio=request.max_signal_divergence_ratio,
        approve_if_passed=request.approve_if_passed,
        block_if_failed=request.block_if_failed,
        allow_warn_status=request.allow_warn_status,
        source_trace_id=request.source_trace_id,
    )


@router.post("/train/execution-risk")
def train_execution_risk(
    request: ExecutionRiskTrainRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().train_execution_risk_model(
        artifact_path=request.artifact_path,
        maturity_statuses=request.maturity_statuses or None,
        max_rows=request.max_rows,
        min_samples_per_target=request.min_samples_per_target,
        calibration_ratio=request.calibration_ratio,
        test_ratio=request.test_ratio,
        epochs=request.epochs,
        learning_rate=request.learning_rate,
        l2=request.l2,
        seed=request.seed,
        now=parse_optional_datetime(request.now or ""),
    )


@router.get("/train/execution-risk/status")
def train_execution_risk_status() -> dict[str, object]:
    return get_service().execution_risk_status()


@router.get("/train/execution-risk/history")
def train_execution_risk_history(
    limit: int = Query(default=20, ge=1, le=200),
) -> dict[str, object]:
    return get_service().execution_risk_training_history(limit=limit)


@router.get("/train/bootstrap/status")
def train_bootstrap_status() -> dict[str, object]:
    return get_service().training_bootstrap_status()
