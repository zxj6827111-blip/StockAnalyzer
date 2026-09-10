"""M12 宏观事件主题层预览端点。"""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from stock_analyzer.api.deps import get_service, get_verify_api_auth

router = APIRouter()


@router.get("/theme/state")
def theme_state() -> dict[str, object]:
    """当前主题状态：激活主题、boost 表、dry-run 注入清单。"""
    return get_service().theme_state()


@router.get("/theme/events")
def theme_events(
    status: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, object]:
    """主题账本事件预览（含 hit_rate_1d/3d/5d 有效性统计）。"""
    return get_service().theme_events(status=status, limit=limit)


@router.get("/theme/shadow/readiness")
def theme_shadow_readiness() -> dict[str, object]:
    """Phase 2 shadow→boost 升级门槛判定（交易日数/确认事件/hit_rate_3d/人工一致率）。"""
    return get_service().theme_shadow_readiness()


@router.post("/theme/sync")
def theme_sync(
    force_refresh: bool = Query(default=False),
    _auth: None = Depends(get_verify_api_auth()),
) -> dict[str, object]:
    """手动触发主题层每日同步（抓取→抽取→确认→账本→theme_state）。

    写侧操作（打网络接口 + 落盘），统一认证门（test_security 动态发现契约）。
    """
    return get_service().run_theme_daily_sync(force_refresh=force_refresh)
