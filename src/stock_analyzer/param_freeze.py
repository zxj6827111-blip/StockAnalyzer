"""PRD §8.7 parameter-freeze core: trading-session mutation guard.

The freeze applies to trading-parameter mutation endpoints and interaction
queries (execution-mode toggle, kill-switch reset, model lifecycle/role and
release flows). GET queries, report generation, scheduler tasks and the signed
command channel (``/command/execute``) are deliberately never frozen.

The clock is read through :func:`current_time` so tests can install a
deterministic wall clock with ``monkeypatch``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from stock_analyzer.config import ParamFreezeConfig
from stock_analyzer.market_calendar import is_a_share_trading_day

PARAMS_FROZEN_STATUS = 423
PARAMS_FROZEN_CODE = "params_frozen"

_current_time_fn: Callable[[str], datetime] | None = None


def _default_current_time(timezone: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(timezone))
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return datetime.now()


def current_time(timezone: str = "Asia/Shanghai") -> datetime:
    """Current wall clock in the configured timezone (test-overridable)."""
    if _current_time_fn is not None:
        return _current_time_fn(timezone)
    return _default_current_time(timezone)


def _window_minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":", maxsplit=1)
    return int(hours) * 60 + int(minutes)


def _wall_clock(value: datetime, timezone: str) -> datetime:
    try:
        tz = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return value
    if value.tzinfo is not None:
        return value.astimezone(tz)
    return value.replace(tzinfo=tz)


def is_params_frozen(*, config: ParamFreezeConfig, now: datetime | None = None) -> bool:
    """Whether trading-parameter changes are frozen at ``now``.

    Frozen only when all of the following hold: freeze is enabled, ``now``
    falls on an A-share trading day, and the local wall-clock time is inside
    one of the configured windows (half-open ``[start, end)``).
    """
    if not config.enabled:
        return False
    timestamp = now if now is not None else current_time(config.timezone)
    if not is_a_share_trading_day(timestamp):
        return False
    local = _wall_clock(timestamp, config.timezone)
    minutes = local.hour * 60 + local.minute
    return any(
        _window_minutes(window.start) <= minutes < _window_minutes(window.end)
        for window in config.freeze_windows
    )


def freeze_window_label(config: ParamFreezeConfig) -> str:
    """Human-readable summary of the configured freeze windows."""
    return ", ".join(f"{window.start}-{window.end}" for window in config.freeze_windows)
