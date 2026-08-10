"""Shared dependencies for API routers.

Routers must resolve the application singletons (config, service) and the
unified API-auth dependency lazily through :mod:`stock_analyzer.main`: tests
swap ``main._service`` / ``main._config`` and monkeypatch auth-related
globals, so every lookup happens at request time and never binds at import.

Importing ``stock_analyzer.main`` inside the functions (instead of at module
top) also avoids an import cycle: ``main`` imports the routers, the routers
import this module, and this module never imports ``main`` eagerly.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

from stock_analyzer.config import StockAnalyzerConfig
from stock_analyzer.runtime.service import StockAnalyzerService


def main_module() -> Any:
    """Return the main app module via ``sys.modules`` (avoids import cycles)."""
    return sys.modules["stock_analyzer.main"]


def get_config() -> StockAnalyzerConfig:
    """Return the shared config singleton as owned by ``main``."""
    return cast(StockAnalyzerConfig, main_module()._config)


def get_service() -> StockAnalyzerService:
    """Return the shared service singleton as owned by ``main``."""
    return cast(StockAnalyzerService, main_module()._service)


def get_verify_api_auth() -> Callable[[str | None, str | None], None]:
    """Return the unified API-auth dependency defined in ``main``."""
    return cast(Callable[[str | None, str | None], None], main_module()._verify_api_auth)


def as_float(value: object, default: float = 0.0) -> float:
    if not isinstance(value, (int, float, str, bytes, bytearray)):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: object, default: int = 0) -> int:
    if not isinstance(value, (int, float, str, bytes, bytearray)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_optional_datetime(raw: str) -> datetime | None:
    value = raw.strip()
    if not value:
        return None
    return datetime.fromisoformat(value)


def record_service_audit_event(
    *,
    event_type: str,
    trace_id: str,
    level: str = "info",
    message: str = "",
    payload: dict[str, object] | None = None,
) -> None:
    recorder = getattr(get_service(), "_record_audit_event", None)
    if not callable(recorder):
        return
    recorder(
        event_type=event_type,
        trace_id=trace_id,
        level=level,
        message=message,
        payload=payload or {},
    )


_dashboard_ops_enabled: bool | None = None


def dashboard_ops_enabled() -> bool:
    """Whether quick dashboard commands are enabled (lazily initialized)."""
    global _dashboard_ops_enabled
    if _dashboard_ops_enabled is None:
        _dashboard_ops_enabled = get_config().app.mode.strip().lower() == "simulation"
    return _dashboard_ops_enabled


def set_dashboard_ops_enabled(value: bool) -> None:
    global _dashboard_ops_enabled
    _dashboard_ops_enabled = value
