"""News scoring and briefing endpoints."""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from stock_analyzer.api.deps import get_service, get_verify_api_auth
from stock_analyzer.api.models import NewsScoreCacheClearRequest

router = APIRouter()


@router.get("/news/score")
def news_score_preview(
    symbol: str = Query(min_length=1),
    strategy: str = Query(default="trend"),
) -> dict[str, object]:
    return get_service().preview_news_component(symbol=symbol, strategy=strategy)


@router.get("/news/score/batch")
def news_score_preview_batch(
    symbols: Annotated[list[str], Query(min_length=1)],
    strategy: str = Query(default="trend"),
) -> dict[str, object]:
    return get_service().preview_news_components(symbols=symbols, strategy=strategy)


@router.get("/news/score/watchlist")
def news_score_preview_watchlist(
    strategy: str = Query(default="trend"),
    limit: int = Query(default=20, ge=1, le=200),
) -> dict[str, object]:
    return get_service().preview_news_watchlist(strategy=strategy, limit=limit)


@router.get("/news/briefing/latest")
def news_briefing_latest(
    phase: str = Query(default="premarket"),
    strategy: str = Query(default="trend"),
    max_symbols: int = Query(default=6, ge=1, le=20),
    limit: int = Query(default=6, ge=1, le=20),
    force_refresh: bool = Query(default=False),
) -> dict[str, object]:
    return get_service().build_live_news_briefing(
        phase=phase,
        strategy=strategy,
        max_symbols=max_symbols,
        max_items=limit,
        force_refresh=force_refresh,
    )


@router.get("/news/score/history")
def news_score_history(
    limit: int = Query(default=50, ge=1, le=500),
    symbol: str = Query(default=""),
    strategy: str = Query(default=""),
) -> dict[str, object]:
    return get_service().news_score_history(
        limit=limit,
        symbol=symbol,
        strategy=strategy,
    )


@router.get("/news/score/cache/state")
def news_score_cache_state() -> dict[str, object]:
    return get_service().news_score_cache_state()


@router.post("/news/score/cache/clear")
def news_score_cache_clear(
    request: NewsScoreCacheClearRequest,
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    return get_service().clear_news_score_cache(
        symbol=request.symbol,
        strategy=request.strategy,
    )
