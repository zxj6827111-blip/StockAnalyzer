"""Runtime settings endpoints (blacklist management).

The blacklist lives on the shared config singleton, so in-place mutations
here are visible to live pipeline instances without a restart. Mutations are
runtime-only: they are not persisted back to the YAML config files.
"""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from stock_analyzer.api.deps import (
    ensure_params_not_frozen,
    get_config,
    get_verify_api_auth,
)
from stock_analyzer.api.models import BlacklistSymbolRequest

router = APIRouter()


def _blacklist_payload() -> dict[str, object]:
    blacklist = get_config().blacklist
    symbols = list(blacklist.symbols)
    return {
        "enabled": blacklist.enabled,
        "symbols": symbols,
        "count": len(symbols),
    }


@router.get("/settings/blacklist")
def settings_blacklist_get() -> dict[str, object]:
    return _blacklist_payload()


@router.post("/settings/blacklist/add")
def settings_blacklist_add(
    request: BlacklistSymbolRequest,
    _auth: None = Depends(get_verify_api_auth()),
    _frozen: None = Depends(ensure_params_not_frozen),
) -> dict[str, object]:
    symbol = request.symbol.strip()
    if not symbol:
        raise HTTPException(status_code=422, detail="empty_symbol")
    blacklist = get_config().blacklist
    added = symbol not in blacklist.symbols
    if added:
        blacklist.symbols.append(symbol)
    payload = _blacklist_payload()
    payload["added"] = added
    return payload


@router.post("/settings/blacklist/remove")
def settings_blacklist_remove(
    request: BlacklistSymbolRequest,
    _auth: None = Depends(get_verify_api_auth()),
    _frozen: None = Depends(ensure_params_not_frozen),
) -> dict[str, object]:
    symbol = request.symbol.strip()
    if not symbol:
        raise HTTPException(status_code=422, detail="empty_symbol")
    blacklist = get_config().blacklist
    removed = symbol in blacklist.symbols
    if removed:
        blacklist.symbols.remove(symbol)
    payload = _blacklist_payload()
    payload["removed"] = removed
    return payload
