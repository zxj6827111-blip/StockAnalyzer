"""Notification endpoints."""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from stock_analyzer.api.deps import get_config, get_service, get_verify_api_auth
from stock_analyzer.api.models import NotificationRequest

router = APIRouter()


@router.post("/notify/test")
def notify_test(
    request: NotificationRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    if not get_config().security.notify_test_enabled:
        raise HTTPException(status_code=403, detail="notify_test_disabled")
    return get_service().notify(
        title=request.title,
        content=request.content,
        level=request.level,
        trace_id=request.trace_id,
    )
