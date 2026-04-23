"""Idle queue dispatch, timeout, and retry helpers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from copy import deepcopy
from datetime import datetime
from datetime import time as dt_time
from functools import lru_cache
from importlib import import_module
from time import perf_counter, sleep
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService


class RuntimeIdleQueueDispatchService:
    """Execute idle-queue tasks with retry and timeout policy."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service

    def idle_task_retry_policy(self, task_id: str) -> dict[str, object]:
        service = self._service
        manifest = service._idle_task_manifests.get(task_id, {})
        raw_policy = manifest.get("retry_policy", {})
        policy = raw_policy if isinstance(raw_policy, dict) else {}
        delay_seconds = _as_int(
            policy.get("retry_delay_seconds"),
            default=_as_int(service._config.idle_queue.default_retry_delay_seconds, default=0),
        )
        if "retry_delay_seconds" not in policy:
            delay_minutes = _as_int(policy.get("retry_delay_minutes"), default=0)
            delay_seconds = max(delay_seconds, delay_minutes * 60)

        retry_only_raw = policy.get(
            "retry_only_on",
            list(service._config.idle_queue.default_retry_only_on),
        )
        no_retry_raw = policy.get(
            "no_retry_on",
            list(service._config.idle_queue.default_no_retry_on),
        )
        retry_only_on = (
            {str(item).strip().lower() for item in retry_only_raw}
            if isinstance(retry_only_raw, list)
            else set()
        )
        no_retry_on = (
            {str(item).strip().lower() for item in no_retry_raw}
            if isinstance(no_retry_raw, list)
            else set()
        )
        return {
            "max_retries": max(
                0,
                _as_int(
                    policy.get("max_retries"),
                    default=service._config.idle_queue.default_retry_max_retries,
                ),
            ),
            "retry_delay_seconds": max(0, delay_seconds),
            "retry_only_on": retry_only_on,
            "no_retry_on": no_retry_on,
        }

    def idle_error_code(self, result: dict[str, object], timed_out: bool = False) -> str:
        if timed_out:
            return "task_timeout"
        explicit = str(result.get("error_code", "")).strip().lower()
        if explicit:
            return explicit
        status = str(result.get("status", "")).strip().lower()
        reason = str(result.get("reason", "")).strip().lower()
        error_name = str(result.get("error", "")).strip().lower()
        merged = f"{status} {reason} {error_name}"
        if "network" in merged:
            return "network_timeout"
        if "timeout" in merged:
            return "task_timeout"
        if "file_handle_busy" in merged or "permission denied" in merged:
            return "file_handle_busy"
        if "transient_io_error" in merged or "temporarily unavailable" in merged:
            return "transient_io_error"
        if "forbidden" in merged:
            return "forbidden_path"
        if "schema" in merged:
            return "schema_mismatch"
        if "data_unavailable" in merged or "no_data" in merged:
            return "data_unavailable"
        return "unknown_error"

    def idle_should_retry(
        self,
        status: str,
        error_code: str,
        attempt_index: int,
        retry_policy: dict[str, object],
    ) -> bool:
        if status not in {"error", "timeout"}:
            return False
        max_retries = _as_int(retry_policy.get("max_retries"), default=0)
        if attempt_index > max_retries:
            return False
        code = error_code.strip().lower()
        no_retry_on = retry_policy.get("no_retry_on", set())
        if isinstance(no_retry_on, set) and code in no_retry_on:
            return False
        retry_only_on = retry_policy.get("retry_only_on", set())
        if isinstance(retry_only_on, set) and retry_only_on and code not in retry_only_on:
            return False
        return True

    def idle_timeout_partial_report(
        self,
        task_id: str,
        context: dict[str, object],
        elapsed_seconds: float,
        max_wall_minutes: int,
        attempts: list[dict[str, object]],
    ) -> str:
        service = self._service
        trade_date = str(context.get("trade_date", "")).strip()
        subdir = str(
            service._idle_task_manifests.get(task_id, {}).get("task_output_subdir", "")
        ).strip()
        payload = {
            "task_id": task_id,
            "trade_date": trade_date,
            "status": "timeout",
            "reason": "task_timeout",
            "generated_at": datetime.now().isoformat(),
            "elapsed_seconds": round(float(elapsed_seconds), 6),
            "max_wall_time_minutes": max_wall_minutes,
            "attempts": attempts,
        }
        try:
            output_path = service._idle_output_path(
                trade_date=trade_date,
                task_id=task_id,
                subdir=subdir,
                filename="partial_timeout_report.json",
            )
            service._idle_write_json(output_path, payload)
            return str(output_path)
        except Exception:
            service._record_audit_event(
                event_type="idle_queue_timeout_partial_write_failed",
                level="warn",
                payload={
                    "task_id": task_id,
                    "trade_date": trade_date,
                    "elapsed_seconds": round(float(elapsed_seconds), 6),
                    "max_wall_time_minutes": max_wall_minutes,
                },
            )
            return ""

    def idle_run_task_with_timeout(
        self,
        task_id: str,
        context: dict[str, object],
        timeout_seconds: float | None,
    ) -> tuple[dict[str, object], bool, float]:
        service = self._service
        started = perf_counter()
        if timeout_seconds is not None and timeout_seconds <= 0:
            elapsed = perf_counter() - started
            return (
                {
                    "status": "timeout",
                    "reason": "task_timeout",
                    "error_code": "task_timeout",
                    "output_files": [],
                },
                True,
                elapsed,
            )

        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"idle-{task_id.lower()}")
        future = pool.submit(service._run_idle_task, task_id=task_id, context=context)
        timed_out = False
        try:
            if timeout_seconds is None:
                raw = future.result()
            else:
                raw = future.result(timeout=max(timeout_seconds, 0.001))
            if isinstance(raw, dict):
                result = deepcopy(raw)
            else:
                result = {
                    "status": "error",
                    "reason": "invalid_task_result",
                    "error_code": "schema_mismatch",
                    "output_files": [],
                }
        except FuturesTimeoutError:
            future.cancel()
            timed_out = True
            result = {
                "status": "timeout",
                "reason": "task_timeout",
                "error_code": "task_timeout",
                "output_files": [],
            }
        except Exception as exc:
            result = {
                "status": "error",
                "error": exc.__class__.__name__,
                "reason": str(exc),
                "output_files": [],
            }
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        elapsed = perf_counter() - started
        return result, timed_out, elapsed

    def idle_execute_task_with_policy(
        self,
        task_id: str,
        context: dict[str, object],
    ) -> dict[str, object]:
        service = self._service
        manifest = service._idle_task_manifests.get(task_id, {})
        max_wall_minutes = max(0, _as_int(manifest.get("max_wall_time_minutes"), default=0))
        total_budget_seconds = float(max_wall_minutes * 60) if max_wall_minutes > 0 else None
        retry_policy = service._idle_task_retry_policy(task_id=task_id)
        started = perf_counter()
        attempts: list[dict[str, object]] = []
        retry_attempts = 0
        final_result: dict[str, object] = {
            "status": "error",
            "reason": "unknown_error",
            "output_files": [],
        }

        attempt_index = 0
        while True:
            attempt_index += 1
            elapsed_before = perf_counter() - started
            if total_budget_seconds is None:
                remaining_seconds: float | None = None
            else:
                remaining_seconds = max(total_budget_seconds - elapsed_before, 0.0)
            task_result, timed_out, elapsed_attempt = service._idle_run_task_with_timeout(
                task_id=task_id,
                context=context,
                timeout_seconds=remaining_seconds,
            )
            status = str(task_result.get("status", "error")).strip().lower() or "error"
            error_code = service._idle_error_code(task_result, timed_out=timed_out)
            task_result["error_code"] = error_code
            attempts.append(
                {
                    "attempt": attempt_index,
                    "status": status,
                    "elapsed_seconds": round(float(elapsed_attempt), 6),
                    "error_code": error_code,
                }
            )
            final_result = task_result

            if timed_out:
                partial_path = service._idle_timeout_partial_report(
                    task_id=task_id,
                    context=context,
                    elapsed_seconds=perf_counter() - started,
                    max_wall_minutes=max_wall_minutes,
                    attempts=attempts,
                )
                outputs = final_result.get("output_files", [])
                output_files = (
                    [str(item) for item in outputs if isinstance(item, str)]
                    if isinstance(outputs, list)
                    else []
                )
                if partial_path:
                    output_files.append(partial_path)
                final_result["output_files"] = output_files
                break

            if not service._idle_should_retry(
                status=status,
                error_code=error_code,
                attempt_index=attempt_index,
                retry_policy=retry_policy,
            ):
                break

            retry_attempts += 1
            delay_seconds = _as_int(retry_policy.get("retry_delay_seconds"), default=0)
            if delay_seconds > 0:
                if total_budget_seconds is not None:
                    remaining_after_attempt = total_budget_seconds - (perf_counter() - started)
                    if remaining_after_attempt <= 1.0:
                        break
                    sleep_for = min(
                        float(delay_seconds),
                        max(remaining_after_attempt - 1.0, 0.0),
                        1.0,
                    )
                else:
                    sleep_for = min(float(delay_seconds), 1.0)
                if sleep_for > 0:
                    sleep(sleep_for)

        total_elapsed = perf_counter() - started
        outputs = final_result.get("output_files", [])
        normalized_outputs = (
            [str(item) for item in outputs if isinstance(item, str)]
            if isinstance(outputs, list)
            else []
        )
        final_result["output_files"] = normalized_outputs
        final_result["attempts"] = attempts
        final_result["retry_attempts"] = retry_attempts
        final_result["elapsed_seconds"] = round(float(total_elapsed), 6)
        final_result["max_wall_time_minutes"] = max_wall_minutes
        retry_only_on = retry_policy.get("retry_only_on", set())
        no_retry_on = retry_policy.get("no_retry_on", set())
        final_result["retry_policy"] = {
            "max_retries": _as_int(retry_policy.get("max_retries"), default=0),
            "retry_delay_seconds": _as_int(retry_policy.get("retry_delay_seconds"), default=0),
            "retry_only_on": sorted(list(retry_only_on) if isinstance(retry_only_on, set) else []),
            "no_retry_on": sorted(list(no_retry_on) if isinstance(no_retry_on, set) else []),
        }
        return final_result

    def idle_update_wd_report_kpi(
        self,
        context: dict[str, object],
        result: dict[str, object],
    ) -> None:
        service = self._service
        status = str(result.get("status", "")).strip().lower()
        if status in {"error", "timeout"}:
            return
        now_clock = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
        deadline_raw = str(context.get("effective_report_deadline", "")).strip()
        deadline = _parse_hhmmss_time(deadline_raw) if deadline_raw else dt_time(8, 23, 0)
        deadline_hit = now_clock.time() <= deadline
        sections_total = _as_int(result.get("sections_total"), default=0)
        completeness_ratio = _as_float(result.get("completeness_ratio"), default=0.0)
        if sections_total <= 0:
            sections_total = max(_as_int(result.get("missing_sections"), default=0), 1)
        if completeness_ratio <= 0.0:
            missing = _as_int(result.get("missing_sections"), default=0)
            completeness_ratio = max(
                min((sections_total - missing) / max(sections_total, 1), 1.0), 0.0
            )
        service._idle_wd_report_runs += 1
        if deadline_hit:
            service._idle_wd_report_deadline_hits += 1
        service._idle_wd_report_completeness_sum += max(min(float(completeness_ratio), 1.0), 0.0)
        service._record_audit_event(
            event_type="idle_queue_wd_report_kpi",
            payload={
                "deadline_hit": deadline_hit,
                "deadline": deadline.strftime("%H:%M:%S"),
                "sections_total": sections_total,
                "completeness_ratio": round(float(completeness_ratio), 6),
                "runs": service._idle_wd_report_runs,
                "deadline_hits": service._idle_wd_report_deadline_hits,
            },
        )

    def idle_update_task_health(
        self,
        task_id: str,
        status: str,
        now: datetime | None = None,
    ) -> None:
        service = self._service
        current = now or datetime.now()
        normalized = status.strip().lower()
        was_blocked = task_id in service._idle_blocked_tasks
        blocked_reason_before = service._idle_blocked_tasks.get(task_id, "")
        if normalized == "fallback":
            service._idle_fallback_streak[task_id] = (
                service._idle_fallback_streak.get(task_id, 0) + 1
            )
        else:
            service._idle_fallback_streak[task_id] = 0

        if normalized == "ok":
            service._idle_success_streak[task_id] = service._idle_success_streak.get(task_id, 0) + 1
        else:
            service._idle_success_streak[task_id] = 0

        ttl = service._idle_task_ttl(task_id)
        fallback_streak = service._idle_fallback_streak.get(task_id, 0)
        if fallback_streak >= ttl:
            service._idle_blocked_tasks[task_id] = f"fallback_streak={fallback_streak}"
            service._idle_manual_ack_grants.pop(task_id, None)
            if task_id not in service._idle_blocked_since:
                service._idle_blocked_since[task_id] = current.isoformat()
            if not was_blocked:
                service._record_audit_event(
                    event_type="idle_queue_task_blocked",
                    level="error",
                    payload={
                        "task_id": task_id,
                        "fallback_streak": fallback_streak,
                        "ttl_runs": ttl,
                        "reason": service._idle_blocked_tasks.get(task_id, ""),
                    },
                )
                title, content, level = service._idle_notification_template(
                    event="blocked",
                    payload={
                        "task_id": task_id,
                        "reason": service._idle_blocked_tasks.get(task_id, ""),
                        "fallback_streak": fallback_streak,
                        "ttl_runs": ttl,
                    },
                )
                service._idle_emit_state_notification(
                    title=title,
                    content=content,
                    level=level,
                    now=current,
                )

        unblock_runs = max(1, service._config.idle_queue.unblock_after_consecutive_success_runs)
        success_streak = service._idle_success_streak.get(task_id, 0)
        ack_ok = (not service._config.idle_queue.manual_ack_required) or (
            task_id in service._idle_manual_ack_grants
        )
        if task_id in service._idle_blocked_tasks and success_streak >= unblock_runs and ack_ok:
            service._idle_blocked_tasks.pop(task_id, None)
            service._idle_fallback_streak[task_id] = 0
            service._idle_manual_ack_grants.pop(task_id, None)
            service._idle_blocked_since.pop(task_id, None)
            service._record_audit_event(
                event_type="idle_queue_task_unblocked",
                payload={
                    "task_id": task_id,
                    "success_streak": success_streak,
                    "unblock_runs": unblock_runs,
                    "manual_ack_required": bool(service._config.idle_queue.manual_ack_required),
                },
            )
            title, content, level = service._idle_notification_template(
                event="recovered",
                payload={
                    "task_id": task_id,
                    "success_streak": success_streak,
                    "unblock_runs": unblock_runs,
                },
            )
            service._idle_emit_state_notification(
                title=title,
                content=content,
                level=level,
                now=current,
            )
        elif was_blocked and blocked_reason_before != service._idle_blocked_tasks.get(task_id, ""):
            service._record_audit_event(
                event_type="idle_queue_task_blocked_refresh",
                level="warn",
                payload={
                    "task_id": task_id,
                    "previous_reason": blocked_reason_before,
                    "current_reason": service._idle_blocked_tasks.get(task_id, ""),
                },
            )


@lru_cache(maxsize=1)
def _runtime_service_module() -> Any:
    return import_module("stock_analyzer.runtime.service")


def _as_float(value: object, default: float) -> float:
    return cast(float, _runtime_service_module()._as_float(value, default))


def _as_int(value: object, default: int) -> int:
    return cast(int, _runtime_service_module()._as_int(value, default))


def _parse_hhmmss_time(raw: str) -> dt_time:
    return cast(dt_time, _runtime_service_module()._parse_hhmmss_time(raw))


def _parse_iso_datetime(value: object) -> datetime | None:
    return cast(datetime | None, _runtime_service_module()._parse_iso_datetime(value))
