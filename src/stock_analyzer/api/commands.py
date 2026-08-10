"""Signed command channel endpoints."""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

from fastapi import APIRouter

from stock_analyzer.api.deps import get_service
from stock_analyzer.api.models import CommandRequest
from stock_analyzer.command.channel import CommandEnvelope

router = APIRouter()


@router.post("/command/execute")
def execute_command(request: CommandRequest) -> dict[str, object]:
    envelope = CommandEnvelope(
        command_id=request.command_id,
        timestamp=request.timestamp,
        action=request.action,
        payload=request.payload,
        signature=request.signature,
    )
    return get_service().execute_command(envelope)


@router.get("/command/state")
def command_state() -> dict[str, object]:
    return {"state": get_service().runtime_status().get("state", {})}
