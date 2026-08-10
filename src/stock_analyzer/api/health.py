"""Health-check endpoints."""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

from fastapi import APIRouter

from stock_analyzer.api.deps import get_config, get_service
from stock_analyzer.build_identity import get_build_manifest

router = APIRouter()


@router.get("/health")
def health() -> dict[str, object]:
    config = get_config()
    build_manifest = get_build_manifest()
    return {
        "status": "ok",
        "mode": config.app.mode,
        "build": {**build_manifest, "code_commit_id": config.evolution.code_commit_id},
        "runtime": {
            "advisory_only": bool(config.app.advisory_only),
            "scheduler_enabled": bool(config.scheduler.enabled),
            "week5_enabled": bool(config.week5.enabled),
            "reconcile_enabled": bool(config.reconcile.enabled),
            "training_enabled": bool(config.training.enabled),
        },
        "health_type": "lightweight",
    }


@router.get("/health/deep")
def health_deep() -> dict[str, object]:
    config = get_config()
    build_manifest = get_build_manifest()
    return {
        "status": "ok",
        "mode": config.app.mode,
        "build": {**build_manifest, "code_commit_id": config.evolution.code_commit_id},
        "provider": get_service().provider_status(),
        "runtime": get_service().runtime_status(include_learning_governance=False),
    }
