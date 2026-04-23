"""Idle queue main cycle orchestration workflow."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService


class RuntimeIdleQueueCycleService:
    """Run one idle queue dispatch cycle through existing service wrappers."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service

    def run_idle_queue_cycle(
        self,
        now: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        service = self._service
        enabled_effective, enabled_reason = service._resolve_idle_queue_enabled()
        if not enabled_effective:
            report = {
                "timestamp": (now or datetime.now()).isoformat(),
                "status": "disabled",
                "detail": f"idle_queue disabled by policy:{enabled_reason}",
                "enabled_config": bool(service._config.idle_queue.enabled),
            }
            service._store_idle_report(report)
            return report

        current = now or datetime.now()
        context = service._build_idle_context(current)
        sync_check = service._idle_check_time_guard_sync(context=context)
        if not bool(sync_check.get("ok", False)):
            service._config.idle_queue.enabled = False
            report = {
                "timestamp": current.isoformat(),
                "status": "disabled",
                "detail": "time_guard_sync_mismatch",
                "context": context,
                "sync_check": sync_check,
            }
            service._store_idle_report(report)
            service._record_audit_event(
                event_type="idle_queue_time_guard_sync_mismatch",
                level="error",
                payload=sync_check,
            )
            return report

        capacity_metrics = service._idle_collect_capacity_metrics(context=context, now=current)
        if str(context.get("window", "off")) == "off":
            report = {
                "timestamp": current.isoformat(),
                "status": "not_in_window",
                "context": context,
                "capacity_metrics": capacity_metrics,
            }
            service._store_idle_report(report)
            return report

        trade_date = str(context.get("trade_date", "")).strip()
        due_tasks = service._idle_due_tasks(context)
        pause_state = service._idle_refresh_pause_state(
            now=current,
            capacity_metrics=capacity_metrics,
            context=context,
        )
        if not due_tasks:
            report = {
                "timestamp": current.isoformat(),
                "status": "idle",
                "detail": "no_due_task",
                "context": context,
                "capacity_metrics": capacity_metrics,
                "pause_state": pause_state,
            }
            service._store_idle_report(report)
            return report

        if bool(pause_state.get("active", False)):
            if "WD-REPORT" in due_tasks:
                due_tasks = ["WD-REPORT"]
            else:
                report = {
                    "timestamp": current.isoformat(),
                    "status": "paused",
                    "detail": "resource_pause_active",
                    "context": context,
                    "capacity_metrics": capacity_metrics,
                    "pause_state": pause_state,
                    "due_tasks": due_tasks,
                }
                service._store_idle_report(report)
                return report

        skipped_tasks: list[dict[str, str]] = []
        for task_id in due_tasks:
            if service._idle_already_ran(task_id=task_id, trade_date=trade_date):
                skipped_tasks.append({"task_id": task_id, "reason": "already_ran"})
                continue
            blocked_reason = service._idle_blocked_tasks.get(task_id, "")
            if blocked_reason:
                ack_granted = task_id in service._idle_manual_ack_grants
                if service._config.idle_queue.manual_ack_required and not ack_granted:
                    skipped_tasks.append(
                        {"task_id": task_id, "reason": f"blocked:{blocked_reason}"}
                    )
                    continue

            service._idle_write_checkpoint(
                task_id=task_id,
                trade_date=trade_date,
                phase="start",
                now=current,
                extra={"context": context},
            )
            result = service._idle_execute_task_with_policy(task_id=task_id, context=context)
            service._idle_write_checkpoint(
                task_id=task_id,
                trade_date=trade_date,
                phase="end",
                now=datetime.now(),
                extra={"result": result},
            )

            status = str(result.get("status", "error")).strip().lower() or "error"
            reason = str(result.get("reason", "")).strip().lower()
            should_mark_ran = status not in {"error", "timeout"} and not reason.startswith(
                "gate_failed"
            )
            if should_mark_ran:
                service._idle_mark_ran(task_id=task_id, trade_date=trade_date)
                if task_id.startswith("WE-P1-"):
                    service._idle_weekend_rotation_scores[task_id] = 0
                if task_id.startswith("WE-"):
                    service._idle_weekend_defer_runs.pop(task_id, None)
            service._idle_set_task_status(task_id=task_id, trade_date=trade_date, status=status)
            service._idle_update_task_health(task_id=task_id, status=status, now=current)
            if task_id == "WD-REPORT":
                service._idle_update_wd_report_kpi(context=context, result=result)

            report = {
                "timestamp": datetime.now().isoformat(),
                "status": "ran",
                "task_id": task_id,
                "task_status": status,
                "context": context,
                "result": result,
                "skipped": skipped_tasks,
                "capacity_metrics": capacity_metrics,
                "pause_state": pause_state,
            }
            service._store_idle_report(report)
            service._record_audit_event(
                event_type="idle_queue_run",
                trace_id=source_trace_id,
                level="warn" if status in {"fallback", "error"} else "info",
                payload={
                    "task_id": task_id,
                    "task_status": status,
                    "trade_date": trade_date,
                    "window": context.get("window", ""),
                },
            )
            return report

        report = {
            "timestamp": current.isoformat(),
            "status": "idle",
            "detail": "all_due_tasks_skipped",
            "context": context,
            "skipped": skipped_tasks,
            "capacity_metrics": capacity_metrics,
            "pause_state": pause_state,
        }
        service._store_idle_report(report)
        return report

    def job_idle_queue_tick(self) -> dict[str, object]:
        service = self._service
        report = service.run_idle_queue_cycle(
            now=datetime.now(),
            source_trace_id="scheduler-idle-queue",
        )
        return {"report": report}
