"""Idle queue state, ack, notification, and pause-state workflows."""

# mypy: disable-error-code=redundant-cast

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from functools import lru_cache
from importlib import import_module
from typing import TYPE_CHECKING, Any, cast

from stock_analyzer.runtime.services.idle_queue_notification_service import (
    describe_idle_block_reason,
)

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService


class RuntimeIdleQueueStateOpsService:
    """Manage idle queue operational state, ack, and pause transitions."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service

    def idle_queue_state(self) -> dict[str, object]:
        service = self._service
        now = datetime.now()
        context = service._build_idle_context(now)
        enabled_effective, enabled_reason = service._resolve_idle_queue_enabled()
        auto_run_effective, auto_run_reason = service._resolve_idle_queue_auto_run()
        capacity = service._idle_collect_capacity_metrics(context=context, now=now)
        pause_until = service._idle_pause_flag_until
        pause_remaining_seconds = 0
        if pause_until is not None:
            pause_remaining_seconds = max(int((pause_until - now).total_seconds()), 0)
        deadline_hit_rate = (
            service._idle_wd_report_deadline_hits / service._idle_wd_report_runs
            if service._idle_wd_report_runs > 0
            else 0.0
        )
        avg_completeness = (
            service._idle_wd_report_completeness_sum / service._idle_wd_report_runs
            if service._idle_wd_report_runs > 0
            else 0.0
        )
        required_success_runs = max(
            1, service._config.idle_queue.unblock_after_consecutive_success_runs
        )
        manual_ack_required = bool(service._config.idle_queue.manual_ack_required)
        blocked = {
            task_id: {
                "reason": reason,
                "reason_detail": describe_idle_block_reason(
                    reason=reason,
                    fallback_streak=service._idle_fallback_streak.get(task_id, 0),
                    ttl_runs=service._idle_task_ttl(task_id),
                ),
                "blocked_since": service._idle_blocked_since.get(task_id, ""),
                "manual_ack_granted": task_id in service._idle_manual_ack_grants,
                "manual_ack_at": service._idle_manual_ack_grants.get(task_id, ""),
                "recovery_progress": {
                    "required_success_runs": required_success_runs,
                    "current_success_streak": service._idle_success_streak.get(task_id, 0),
                    "remaining_success_runs": max(
                        required_success_runs - service._idle_success_streak.get(task_id, 0),
                        0,
                    ),
                    "manual_ack_required": manual_ack_required,
                    "manual_ack_granted": task_id in service._idle_manual_ack_grants,
                    "eligible_to_unblock": (
                        (not manual_ack_required or task_id in service._idle_manual_ack_grants)
                        and service._idle_success_streak.get(task_id, 0) >= required_success_runs
                    ),
                },
            }
            for task_id, reason in sorted(service._idle_blocked_tasks.items())
        }
        pending_manual_ack = [
            task_id
            for task_id in sorted(service._idle_blocked_tasks.keys())
            if task_id not in service._idle_manual_ack_grants
        ]
        task_health = service._idle_task_health_snapshot()
        return {
            "enabled": bool(enabled_effective),
            "auto_run": bool(auto_run_effective),
            "enabled_config": bool(service._config.idle_queue.enabled),
            "auto_run_config": bool(service._config.idle_queue.auto_run),
            "enabled_reason": enabled_reason,
            "auto_run_reason": auto_run_reason,
            "manual_ack_required": manual_ack_required,
            "window_context": context,
            "sync_check": service._idle_check_time_guard_sync(context=context),
            "capacity_metrics": capacity,
            "resource_pause": {
                "active": bool(service._idle_resource_pause_active),
                "reason": service._idle_resource_pause_reason,
                "pause_flag_until": (pause_until.isoformat() if pause_until is not None else ""),
                "pause_seconds_remaining": pause_remaining_seconds,
                "last_change_at": service._idle_resource_pause_last_change_at,
            },
            "wd_report_kpi": {
                "runs": service._idle_wd_report_runs,
                "deadline_hits": service._idle_wd_report_deadline_hits,
                "deadline_hit_rate": round(deadline_hit_rate, 6),
                "avg_completeness_ratio": round(avg_completeness, 6),
            },
            "blocked_tasks": blocked,
            "pending_manual_ack": pending_manual_ack,
            "task_health": task_health,
            "history": {
                "memory_records": len(service._idle_history),
                "persist_path": str(service._idle_history_path),
            },
            "latest_report": service._last_idle_report,
        }

    def idle_queue_ack_blocked(
        self,
        task_id: str = "",
        clear_all: bool = False,
        now: datetime | None = None,
    ) -> dict[str, object]:
        service = self._service
        if not service._config.idle_queue.manual_ack_required:
            return {
                "status": "noop",
                "detail": "manual_ack_not_required",
                "blocked_tasks": sorted(service._idle_blocked_tasks.keys()),
            }

        current = now or datetime.now()
        acked: list[str] = []
        if clear_all:
            target_tasks = sorted(service._idle_blocked_tasks.keys())
        else:
            normalized = task_id.strip()
            if not normalized:
                return {
                    "status": "invalid",
                    "detail": "task_id_required_when_clear_all_false",
                    "blocked_tasks": sorted(service._idle_blocked_tasks.keys()),
                }
            target_tasks = [normalized]

        for target in target_tasks:
            if target not in service._idle_blocked_tasks:
                continue
            service._idle_manual_ack_grants[target] = current.isoformat()
            acked.append(target)

        if acked:
            service._record_audit_event(
                event_type="idle_queue_blocked_ack",
                payload={
                    "acked": acked,
                    "blocked_tasks": sorted(service._idle_blocked_tasks.keys()),
                    "manual_ack_required": bool(service._config.idle_queue.manual_ack_required),
                },
            )
            title, content, level = service._idle_notification_template(
                event="ack",
                payload={
                    "acked": acked,
                    "blocked_tasks": sorted(service._idle_blocked_tasks.keys()),
                },
            )
            service._idle_emit_state_notification(
                title=title,
                content=content,
                level=level,
                now=current,
            )

        return {
            "status": "ok",
            "acked": acked,
            "blocked_tasks": sorted(service._idle_blocked_tasks.keys()),
            "manual_ack_grants": deepcopy(service._idle_manual_ack_grants),
            "timestamp": current.isoformat(),
        }

    def idle_task_health_snapshot(self) -> list[dict[str, object]]:
        service = self._service
        task_ids = set(service._idle_fallback_streak.keys())
        task_ids.update(service._idle_success_streak.keys())
        task_ids.update(service._idle_blocked_tasks.keys())
        task_ids.update(service._idle_task_manifests.keys())
        items: list[dict[str, object]] = []
        for task_id in sorted(task_ids):
            items.append(
                {
                    "task_id": task_id,
                    "fallback_streak": service._idle_fallback_streak.get(task_id, 0),
                    "success_streak": service._idle_success_streak.get(task_id, 0),
                    "blocked": task_id in service._idle_blocked_tasks,
                    "blocked_reason": service._idle_blocked_tasks.get(task_id, ""),
                    "blocked_since": service._idle_blocked_since.get(task_id, ""),
                    "manual_ack_granted": task_id in service._idle_manual_ack_grants,
                    "ttl_runs": service._idle_task_ttl(task_id),
                }
            )
        return items

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
            title = f"[绌洪棽闃熷垪][闃诲] {task_id}"
            content = (
                f"鏃堕棿={now_text}\n"
                f"浠诲姟ID={task_id}\n"
                f"鍘熷洜={reason or '-'}\n"
                f"鍥為€€杩炵画娆℃暟={fallback_streak}\n"
                f"瀛樻椿杞={ttl_runs}\n"
                "澶勭悊寤鸿=闇€瑕佷汉宸ョ‘璁?"
            )
            return title, content, "warn"
        if event == "ack":
            acked = payload.get("acked", [])
            blocked = payload.get("blocked_tasks", [])
            acked_text = ",".join(str(item) for item in acked) if isinstance(acked, list) else "-"
            blocked_text = (
                ",".join(str(item) for item in blocked) if isinstance(blocked, list) else "-"
            )
            title = "[绌洪棽闃熷垪][纭] 浜哄伐纭瀹屾垚"
            content = (
                f"鏃堕棿={now_text}\n"
                f"宸茬‘璁や换鍔?{acked_text}\n"
                f"褰撳墠闃诲浠诲姟={blocked_text}\n"
                "澶勭悊缁撴灉=宸叉仮澶嶈瀵熸湡"
            )
            return title, content, "warn"
        task_id = str(payload.get("task_id", ""))
        success_streak = _as_int(payload.get("success_streak"), default=0)
        unblock_runs = _as_int(payload.get("unblock_runs"), default=0)
        title = f"[绌洪棽闃熷垪][鎭㈠] {task_id}"
        content = (
            f"鏃堕棿={now_text}\n"
            f"浠诲姟ID={task_id}\n"
            f"杩炵画鎴愬姛娆℃暟={success_streak}\n"
            f"瑙ｉ櫎闃诲鎵€闇€杞={unblock_runs}\n"
            "澶勭悊缁撴灉=闃诲宸茬Щ闄?"
        )
        return title, content, "info"

    def idle_emit_state_notification(
        self,
        title: str,
        content: str,
        level: str = "warn",
        now: datetime | None = None,
    ) -> None:
        service = self._service
        current = now or datetime.now()
        if current.weekday() >= 5:
            service._record_audit_event(
                event_type="idle_queue_state_notify_suppressed_weekend",
                trace_id="idle-queue-state",
                payload={
                    "timestamp": current.isoformat(),
                    "title": title,
                    "content": content,
                    "level": level,
                },
            )
            return
        try:
            service.notify(title=title, content=content, level=level, trace_id="idle-queue-state")
        except Exception:
            service._record_audit_event(
                event_type="idle_queue_state_notify_failed",
                level="warn",
                payload={"title": title, "content": content, "level": level},
            )

    def idle_refresh_pause_state(
        self,
        now: datetime,
        capacity_metrics: dict[str, object],
        context: dict[str, object],
    ) -> dict[str, object]:
        service = self._service
        pause_enabled = bool(service._config.idle_queue.resource_pause_enabled)
        metric = (
            str(service._config.idle_queue.resource_pause_metric).strip().lower()
            or "disk_usage_pct"
        )
        usage_pct = _as_float(capacity_metrics.get(metric), default=0.0)
        high = _as_float(
            capacity_metrics.get("resource_pause_high_watermark_pct"),
            default=88.0,
        )
        low = _as_float(
            capacity_metrics.get("resource_pause_low_watermark_pct"),
            default=82.0,
        )
        if low > high:
            low = max(0.0, high - 0.5)
        pause_seconds = max(1, _as_int(service._config.idle_queue.pause_sleep_seconds, default=5))
        previous_active = bool(service._idle_resource_pause_active)
        previous_reason = service._idle_resource_pause_reason
        previous_until = service._idle_pause_flag_until

        if not pause_enabled:
            service._idle_resource_pause_active = False
            service._idle_resource_pause_reason = ""
            service._idle_pause_flag_until = None
        else:
            if service._idle_resource_pause_active:
                if usage_pct <= low:
                    service._idle_resource_pause_active = False
                    service._idle_resource_pause_reason = ""
                    service._idle_pause_flag_until = None
                else:
                    if (
                        service._idle_pause_flag_until is None
                        or service._idle_pause_flag_until <= now
                    ):
                        service._idle_pause_flag_until = now + timedelta(seconds=pause_seconds)
            else:
                if usage_pct >= high:
                    service._idle_resource_pause_active = True
                    service._idle_resource_pause_reason = (
                        f"{metric}>={high:.2f} current={usage_pct:.2f}"
                    )
                    service._idle_pause_flag_until = now + timedelta(seconds=pause_seconds)
                elif (
                    service._idle_pause_flag_until is not None
                    and service._idle_pause_flag_until <= now
                ):
                    service._idle_pause_flag_until = None

        changed = (
            previous_active != service._idle_resource_pause_active
            or previous_reason != service._idle_resource_pause_reason
        )
        if changed:
            service._idle_resource_pause_last_change_at = now.isoformat()
            service._record_audit_event(
                event_type="idle_queue_pause_flag_changed",
                level="warn" if service._idle_resource_pause_active else "info",
                payload={
                    "active": bool(service._idle_resource_pause_active),
                    "reason": service._idle_resource_pause_reason,
                    "metric": metric,
                    "usage_pct": round(usage_pct, 6),
                    "high_watermark_pct": round(high, 6),
                    "low_watermark_pct": round(low, 6),
                    "window": str(context.get("window", "")),
                    "trade_date": str(context.get("trade_date", "")),
                },
            )
        elif (
            service._idle_resource_pause_active
            and previous_until is not None
            and service._idle_pause_flag_until is not None
            and service._idle_pause_flag_until != previous_until
        ):
            service._record_audit_event(
                event_type="idle_queue_pause_flag_refresh",
                level="info",
                payload={
                    "active": True,
                    "metric": metric,
                    "usage_pct": round(usage_pct, 6),
                    "pause_flag_until": service._idle_pause_flag_until.isoformat(),
                },
            )

        pause_until = service._idle_pause_flag_until
        pause_remaining_seconds = 0
        if pause_until is not None:
            pause_remaining_seconds = max(int((pause_until - now).total_seconds()), 0)
        return {
            "active": bool(service._idle_resource_pause_active),
            "reason": service._idle_resource_pause_reason,
            "metric": metric,
            "usage_pct": round(usage_pct, 6),
            "high_watermark_pct": round(high, 6),
            "low_watermark_pct": round(low, 6),
            "pause_flag_until": pause_until.isoformat() if pause_until is not None else "",
            "pause_seconds_remaining": pause_remaining_seconds,
            "pause_sleep_seconds": pause_seconds,
        }


@lru_cache(maxsize=1)
def _runtime_service_module() -> Any:
    return import_module("stock_analyzer.runtime.service")


def _as_float(value: object, default: float) -> float:
    return cast(float, _runtime_service_module()._as_float(value, default))


def _as_int(value: object, default: int) -> int:
    return cast(int, _runtime_service_module()._as_int(value, default))
