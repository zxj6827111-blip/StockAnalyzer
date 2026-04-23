"""Idle queue notification template helpers."""

# mypy: disable-error-code=redundant-cast

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from importlib import import_module
import re
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService


class RuntimeIdleQueueNotificationService:
    """Build idle queue state-notification payloads."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service

    def idle_notification_template(
        self,
        event: str,
        payload: dict[str, object],
    ) -> tuple[str, str, str]:
        now_text = datetime.now().isoformat()
        if event == "blocked":
            task_id = str(payload.get("task_id", ""))
            reason = str(payload.get("reason", ""))
            fallback_streak = _as_int(payload.get("fallback_streak"), default=0)
            ttl_runs = _as_int(payload.get("ttl_runs"), default=0)
            reason_detail = describe_idle_block_reason(
                reason=reason,
                fallback_streak=fallback_streak,
                ttl_runs=ttl_runs,
            )
            title = f"[空闲队列][阻塞] {task_id}"
            content = (
                f"时间={now_text}\n"
                f"任务ID={task_id}\n"
                f"原因={reason or '-'}\n"
                f"回退连续次数={fallback_streak}\n"
                f"存活轮次={ttl_runs}\n"
                f"含义={reason_detail}\n"
                "处理建议=需要人工确认"
            )
            return title, content, "warn"
        if event == "ack":
            acked = payload.get("acked", [])
            blocked = payload.get("blocked_tasks", [])
            acked_text = ",".join(str(item) for item in acked) if isinstance(acked, list) else "-"
            blocked_text = (
                ",".join(str(item) for item in blocked) if isinstance(blocked, list) else "-"
            )
            title = "[空闲队列][确认] 人工确认完成"
            content = (
                f"时间={now_text}\n"
                f"已确认任务={acked_text}\n"
                f"当前阻塞任务={blocked_text}\n"
                "处理结果=已恢复观察期"
            )
            return title, content, "warn"
        task_id = str(payload.get("task_id", ""))
        success_streak = _as_int(payload.get("success_streak"), default=0)
        unblock_runs = _as_int(payload.get("unblock_runs"), default=0)
        title = f"[空闲队列][恢复] {task_id}"
        content = (
            f"时间={now_text}\n"
            f"任务ID={task_id}\n"
            f"连续成功次数={success_streak}\n"
            f"解除阻塞所需轮次={unblock_runs}\n"
            "处理结果=阻塞已移除"
        )
        return title, content, "info"


@lru_cache(maxsize=1)
def _runtime_service_module() -> Any:
    return import_module("stock_analyzer.runtime.service")


def _as_int(value: object, default: int) -> int:
    return cast(int, _runtime_service_module()._as_int(value, default))


_FALLBACK_STREAK_RE = re.compile(r"^fallback_streak=(?P<count>\d+)$")


def describe_idle_block_reason(
    *,
    reason: str,
    fallback_streak: int = 0,
    ttl_runs: int = 0,
) -> str:
    normalized = reason.strip()
    matched = _FALLBACK_STREAK_RE.match(normalized)
    effective_fallback = max(fallback_streak, 0)
    if matched is not None:
        effective_fallback = max(effective_fallback, _as_int(matched.group("count"), default=0))
    if effective_fallback > 0:
        threshold_text = f"（触发阈值 {ttl_runs} 轮）" if ttl_runs > 0 else ""
        return (
            f"任务连续 {effective_fallback} 轮进入回退/降级结果{threshold_text}，"
            "系统已暂停自动执行，需人工确认后再恢复观察"
        )
    if normalized:
        return f"任务因 {normalized} 被暂停自动执行，需人工确认后再恢复观察"
    return "任务已被暂停自动执行，需人工确认后再恢复观察"
