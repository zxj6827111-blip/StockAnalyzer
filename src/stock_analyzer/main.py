"""FastAPI application entrypoint.

Assembles the app from per-domain router submodules (``stock_analyzer.api``)
and keeps the shared runtime singletons, lifespan, unified API auth and
frontend static routes. Route handlers live in ``stock_analyzer.api.*``.

Import order matters: the routers resolve ``_verify_api_auth`` / ``_config`` /
``_service`` lazily through this module, so the router imports below must come
after those definitions.
"""

# mypy: disable-error-code="untyped-decorator,no-any-return"

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from stock_analyzer.command.feishu_long_connection import (
    FeishuLongConnectionRunner as FeishuLongConnectionRunner,
)
from stock_analyzer.config import get_config
from stock_analyzer.data.provider import RequiredIntradayDataError
from stock_analyzer.notify.channels import FeishuAppNotifier as FeishuAppNotifier
from stock_analyzer.runtime.service import StockAnalyzerService

_config = get_config()
_service = StockAnalyzerService(config=_config)


_SA_AUTH_ENABLED_ENV = "SA__SECURITY__API_AUTH_ENABLED"


def _api_auth_force_enabled() -> bool:
    """Fail-closed default: when the operator never explicitly set
    SA__SECURITY__API_AUTH_ENABLED, dangerous endpoints require auth no matter
    what the config file says."""
    return os.getenv(_SA_AUTH_ENABLED_ENV) is None


def _verify_api_auth(
    authorization: str | None = Header(default=None),
    x_sa_api_key: str | None = Header(default=None),
) -> None:
    """Unified API auth dependency for dangerous POST endpoints.

    Supports ``Authorization: Bearer <token>`` and ``X-SA-API-Key: <token>``.
    Returns 401 when no credential is provided, 403 when the credential is wrong.
    """
    sec = _config.security
    if not (sec.api_auth_enabled or _api_auth_force_enabled()):
        return
    expected = sec.api_token.strip()
    if not expected:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=500,
            detail="api_auth_enabled is true but api_token is empty; refusing all requests",
        )
    provided = ""
    if x_sa_api_key and x_sa_api_key.strip():
        provided = x_sa_api_key.strip()
    elif authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            provided = parts[1].strip()
    if not provided:
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="missing_api_token")
    if provided != expected:
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="invalid_api_token")


def _resolve_frontend_dist_dir(project_root: Path | None = None) -> Path | None:
    root = project_root or Path(__file__).resolve().parents[2]
    candidates = (
        root / "frontend_dist",
        root / "frontend" / "dist",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


_frontend_dist_dir = _resolve_frontend_dist_dir()
_frontend_assets_dir = _frontend_dist_dir / "assets" if _frontend_dist_dir is not None else None


# Router submodules. They must be imported after the singletons and the auth
# dependency above: at import time each router evaluates
# ``Depends(get_verify_api_auth())``, which looks up ``_verify_api_auth`` on
# this (partially initialized) module.
# ruff: noqa: E402, I001
from stock_analyzer.api.acceptance import router as acceptance_router
from stock_analyzer.api.audit import router as audit_router
from stock_analyzer.api.commands import router as commands_router
from stock_analyzer.api.dashboard import router as dashboard_router
from stock_analyzer.api.evolution import router as evolution_router
from stock_analyzer.api.health import router as health_router
from stock_analyzer.api.idle import router as idle_router
from stock_analyzer.api.learning import router as learning_router
from stock_analyzer.api.market import router as market_router
from stock_analyzer.api.messaging import (
    _feishu_long_connection_runner as _feishu_long_connection_runner,
    _feishu_user_allowed as _feishu_user_allowed,
    _launch_feishu_message_final_reply as _launch_feishu_message_final_reply,
    _prewarm_feishu_app_access_token_if_needed as _prewarm_feishu_app_access_token_if_needed,
    _process_feishu_message_event_async as _process_feishu_message_event_async,
    _start_feishu_long_connection_if_needed as _start_feishu_long_connection_if_needed,
    _stop_feishu_long_connection_if_needed as _stop_feishu_long_connection_if_needed,
    _wecom_user_allowed as _wecom_user_allowed,
    router as messaging_router,
)
from stock_analyzer.api.model_registry import router as model_registry_router
from stock_analyzer.api.news import router as news_router
from stock_analyzer.api.notifications import router as notifications_router
from stock_analyzer.api.portfolio import router as portfolio_router
from stock_analyzer.api.research import router as research_router
from stock_analyzer.api.runtime import router as runtime_router
from stock_analyzer.api.settings import router as settings_router
from stock_analyzer.api.training import router as training_router
from stock_analyzer.api.week5 import router as week5_router
from stock_analyzer.api.week6 import router as week6_router
from stock_analyzer.api.week7 import router as week7_router


@asynccontextmanager
async def _app_lifespan(_app: FastAPI) -> Any:
    import logging

    _startup_log = logging.getLogger("stock_analyzer.startup")
    if _config.command_channel.enabled and _config.command_channel.is_secret_weak:
        _startup_log.critical(
            "SECURITY: command_channel.secret_key is weak or default. "
            "All signed commands will be rejected. Set a strong secret via "
            "SA__COMMAND_CHANNEL__SECRET_KEY environment variable."
        )
    if _config.security.api_auth_enabled and not _config.security.api_token.strip():
        _startup_log.critical(
            "SECURITY: security.api_auth_enabled is true but api_token is empty. "
            "API auth will not protect endpoints."
        )
    if _api_auth_force_enabled() and not _config.security.api_auth_enabled:
        _startup_log.critical(
            "SECURITY: SA__SECURITY__API_AUTH_ENABLED was not explicitly set, "
            "so API auth is force-enabled (fail-closed): dangerous POST endpoints "
            "reject unauthenticated requests. Set SA__SECURITY__API_AUTH_ENABLED=true "
            "and a strong SA__SECURITY__API_TOKEN to configure it explicitly."
        )
    _prewarm_feishu_app_access_token_if_needed()
    _start_feishu_long_connection_if_needed()
    try:
        yield
    finally:
        _stop_feishu_long_connection_if_needed()


app = FastAPI(title="StockAnalyzer API", version="0.1.0", lifespan=_app_lifespan)

# 看板 JSON 响应体偏大（/dashboard/portfolio 约 730KB、/week5/scan/latest 约 460KB），
# 未压缩时在局域网外访问明显变慢。GZipMiddleware 只在客户端声明 Accept-Encoding
# 时生效，企微/飞书回调不声明就不压缩，因此不影响既有集成。minimum_size 跳过
# 小响应，避免给 /health 这类高频轻量端点增加无谓的压缩开销。
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Vite 产物是 hash 化文件名（index-<hash>.js），内容变更必然带来文件名变更，
# 因此可以安全地长期强缓存。注意 index.html 不能强缓存，否则前端发版后浏览器
# 会一直拿旧页面 —— 它由下面的 frontend_ui_page 返回，保持默认的协商缓存。
_UI_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"


class _ImmutableAssetStaticFiles(StaticFiles):
    """StaticFiles that marks hashed build assets as immutable."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers.setdefault("Cache-Control", _UI_ASSET_CACHE_CONTROL)
        return response



@app.exception_handler(RequiredIntradayDataError)
async def required_intraday_data_error(
    _request: Request,
    exc: RequiredIntradayDataError,
) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "detail": {
                "code": "required_intraday_data_unavailable",
                "message": str(exc),
            }
        },
    )


if _frontend_assets_dir is not None and _frontend_assets_dir.exists():
    app.mount(
        "/ui/assets",
        _ImmutableAssetStaticFiles(directory=str(_frontend_assets_dir)),
        name="ui-assets",
    )


if _frontend_dist_dir is not None:
    _resolved_frontend_dist_dir = _frontend_dist_dir

    @app.get("/ui", include_in_schema=False)
    @app.get("/ui/", include_in_schema=False)
    @app.get("/ui/{path:path}", include_in_schema=False)
    def frontend_ui_page(path: str = "") -> Response:
        requested = path.strip().lstrip("/")
        if requested:
            candidate = (_resolved_frontend_dist_dir / requested).resolve()
            try:
                candidate.relative_to(_resolved_frontend_dist_dir.resolve())
            except ValueError:
                return FileResponse(str(_resolved_frontend_dist_dir / "index.html"))
            if candidate.is_file():
                return FileResponse(str(candidate))
        return FileResponse(str(_resolved_frontend_dist_dir / "index.html"))


app.include_router(health_router)
app.include_router(dashboard_router)
app.include_router(news_router)
app.include_router(notifications_router)
app.include_router(commands_router)
app.include_router(messaging_router)
app.include_router(market_router)
app.include_router(idle_router)
app.include_router(evolution_router)
app.include_router(training_router)
app.include_router(learning_router)
app.include_router(model_registry_router)
app.include_router(research_router)
app.include_router(acceptance_router)
app.include_router(portfolio_router)
app.include_router(runtime_router)
app.include_router(settings_router)
app.include_router(week5_router)
app.include_router(week6_router)
app.include_router(week7_router)
app.include_router(audit_router)
