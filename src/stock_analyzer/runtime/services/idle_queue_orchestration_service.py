"""Idle queue orchestration, state, and retry workflows."""

# mypy: disable-error-code=redundant-cast

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from datetime import time as dt_time
from functools import lru_cache
from importlib import import_module
from typing import TYPE_CHECKING, Any, cast

from stock_analyzer.evolution.scheduler.time_guard import TimeGuard
from stock_analyzer.runtime.services.idle_queue_cycle_service import (
    RuntimeIdleQueueCycleService,
)
from stock_analyzer.runtime.services.idle_queue_dispatch_service import (
    RuntimeIdleQueueDispatchService,
)
from stock_analyzer.runtime.services.idle_queue_manifest_service import (
    RuntimeIdleQueueManifestService,
)
from stock_analyzer.runtime.services.idle_queue_notification_service import (
    RuntimeIdleQueueNotificationService,
)
from stock_analyzer.runtime.services.idle_queue_registry_service import (
    RuntimeIdleQueueRegistryService,
)
from stock_analyzer.runtime.services.idle_queue_state_ops_service import (
    RuntimeIdleQueueStateOpsService,
)

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService


class RuntimeIdleQueueOrchestrationService:
    """Idle queue orchestration, state, and retry workflows."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service
        self._cycle_service = RuntimeIdleQueueCycleService(service)
        self._dispatch_service = RuntimeIdleQueueDispatchService(service)
        self._manifest_service = RuntimeIdleQueueManifestService(service)
        self._notification_service = RuntimeIdleQueueNotificationService(service)
        self._registry_service = RuntimeIdleQueueRegistryService(service)
        self._state_ops_service = RuntimeIdleQueueStateOpsService(service)

    def _idle_policy_modes(self, raw_modes: list[str], default_modes: set[str]) -> set[str]:
        modes = {str(item).strip().lower() for item in raw_modes if str(item).strip()}
        return modes or set(default_modes)

    def _idle_production_canary_hit(self) -> tuple[bool, str]:
        service = self._service
        ratio = _as_float(service._config.idle_queue.production_canary_ratio, default=0.0)
        ratio = max(0.0, min(ratio, 1.0))
        if ratio <= 0.0:
            return False, "ratio=0"
        if ratio >= 1.0:
            return True, "ratio=1"
        key = (
            str(service._config.idle_queue.production_canary_key).strip()
            or os.getenv("SA_IDLE_CANARY_KEY", "").strip()
            or os.getenv("COMPUTERNAME", "").strip()
            or os.getenv("HOSTNAME", "").strip()
            or "stock_analyzer"
        )
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        bucket = int(digest[:8], 16) / float(0xFFFFFFFF)
        return bucket < ratio, f"ratio={ratio:.4f},bucket={bucket:.6f}"

    def _idle_policy_switch(
        self,
        *,
        configured: bool,
        policy_raw: str,
        modes: set[str],
        flag_name: str,
    ) -> tuple[bool, str]:
        service = self._service
        if configured:
            return True, f"config_{flag_name}_true"
        policy = policy_raw.strip().lower()
        if policy == "fixed":
            return False, f"config_{flag_name}_false_fixed"
        if policy != "auto":
            return False, f"invalid_{flag_name}_policy:{policy or 'empty'}"
        mode = service._runtime_mode()
        if mode in modes:
            return True, f"policy_mode:{mode}"
        if mode == "production":
            hit, marker = service._idle_production_canary_hit()
            if hit:
                return True, f"policy_production_canary:{marker}"
        return False, f"policy_mode_off:{mode or 'unknown'}"

    def _resolve_idle_queue_enabled(self) -> tuple[bool, str]:
        service = self._service
        modes = self._idle_policy_modes(
            service._config.idle_queue.enabled_modes,
            default_modes={"simulation", "staging"},
        )
        return self._idle_policy_switch(
            configured=bool(service._config.idle_queue.enabled),
            policy_raw=str(service._config.idle_queue.enabled_policy),
            modes=modes,
            flag_name="enabled",
        )

    def _resolve_idle_queue_auto_run(self) -> tuple[bool, str]:
        service = self._service
        enabled, enabled_reason = self._resolve_idle_queue_enabled()
        if not enabled:
            return False, f"disabled_effective:{enabled_reason}"
        modes = self._idle_policy_modes(
            service._config.idle_queue.auto_run_modes,
            default_modes={"simulation", "staging"},
        )
        return self._idle_policy_switch(
            configured=bool(service._config.idle_queue.auto_run),
            policy_raw=str(service._config.idle_queue.auto_run_policy),
            modes=modes,
            flag_name="auto_run",
        )

    def latest_idle_queue_report(self) -> dict[str, object] | None:
        """Return latest idle-queue dispatch report."""
        service = self._service
        return cast(dict[str, object] | None, service._last_idle_report)

    def idle_queue_history(self, limit: int = 20) -> dict[str, object]:
        """Return recent idle-queue dispatch reports."""
        service = self._service
        capped_limit = max(1, min(limit, 500))
        recent = service._idle_history[-capped_limit:]
        return {"records": len(recent), "items": recent}

    def idle_queue_state(self) -> dict[str, object]:
        """Return idle-queue runtime state for operations and troubleshooting."""
        return cast(dict[str, object], self._state_ops_service.idle_queue_state())

    def idle_queue_ack_blocked(
        self,
        task_id: str = "",
        clear_all: bool = False,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Ack blocked idle tasks so they can re-enter probation runs."""
        return cast(
            dict[str, object],
            self._state_ops_service.idle_queue_ack_blocked(
                task_id=task_id,
                clear_all=clear_all,
                now=now,
            ),
        )

    def _idle_task_health_snapshot(self) -> list[dict[str, object]]:
        return cast(list[dict[str, object]], self._state_ops_service.idle_task_health_snapshot())

    def _idle_notification_template(
        self,
        event: str,
        payload: dict[str, object],
    ) -> tuple[str, str, str]:
        return cast(
            tuple[str, str, str],
            self._notification_service.idle_notification_template(
                event=event,
                payload=payload,
            ),
        )

    def _idle_emit_state_notification(
        self,
        title: str,
        content: str,
        level: str = "warn",
        now: datetime | None = None,
    ) -> None:
        self._state_ops_service.idle_emit_state_notification(title, content, level, now=now)

    def run_idle_queue_cycle(
        self,
        now: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        """Run one idle-queue dispatch cycle."""
        return cast(
            dict[str, object],
            self._cycle_service.run_idle_queue_cycle(
                now=now,
                source_trace_id=source_trace_id,
            ),
        )

    def _idle_refresh_pause_state(
        self,
        now: datetime,
        capacity_metrics: dict[str, object],
        context: dict[str, object],
    ) -> dict[str, object]:
        return cast(
            dict[str, object],
            self._state_ops_service.idle_refresh_pause_state(
                now=now,
                capacity_metrics=capacity_metrics,
                context=context,
            ),
        )

    def _idle_task_retry_policy(self, task_id: str) -> dict[str, object]:
        return cast(dict[str, object], self._dispatch_service.idle_task_retry_policy(task_id))

    def _idle_error_code(self, result: dict[str, object], timed_out: bool = False) -> str:
        return cast(str, self._dispatch_service.idle_error_code(result, timed_out=timed_out))

    def _idle_should_retry(
        self,
        status: str,
        error_code: str,
        attempt_index: int,
        retry_policy: dict[str, object],
    ) -> bool:
        return cast(
            bool,
            self._dispatch_service.idle_should_retry(
                status=status,
                error_code=error_code,
                attempt_index=attempt_index,
                retry_policy=retry_policy,
            ),
        )

    def _idle_timeout_partial_report(
        self,
        task_id: str,
        context: dict[str, object],
        elapsed_seconds: float,
        max_wall_minutes: int,
        attempts: list[dict[str, object]],
    ) -> str:
        return cast(
            str,
            self._dispatch_service.idle_timeout_partial_report(
                task_id=task_id,
                context=context,
                elapsed_seconds=elapsed_seconds,
                max_wall_minutes=max_wall_minutes,
                attempts=attempts,
            ),
        )

    def _idle_run_task_with_timeout(
        self,
        task_id: str,
        context: dict[str, object],
        timeout_seconds: float | None,
    ) -> tuple[dict[str, object], bool, float]:
        return cast(
            tuple[dict[str, object], bool, float],
            self._dispatch_service.idle_run_task_with_timeout(
                task_id=task_id,
                context=context,
                timeout_seconds=timeout_seconds,
            ),
        )

    def _idle_execute_task_with_policy(
        self,
        task_id: str,
        context: dict[str, object],
    ) -> dict[str, object]:
        return cast(
            dict[str, object],
            self._dispatch_service.idle_execute_task_with_policy(
                task_id=task_id,
                context=context,
            ),
        )

    def _idle_update_wd_report_kpi(
        self,
        context: dict[str, object],
        result: dict[str, object],
    ) -> None:
        self._dispatch_service.idle_update_wd_report_kpi(
            context=context,
            result=result,
        )

    def _idle_check_time_guard_sync(self, context: dict[str, object]) -> dict[str, object]:
        expected_raw = str(context.get("effective_hard_stop", "")).strip()
        expected = _parse_hhmmss_time(expected_raw).replace(second=0)
        guard = TimeGuard()
        actual = guard.open_auction_start.replace(second=0)
        ok = actual == expected
        return {
            "ok": ok,
            "expected_effective_hard_stop": expected.strftime("%H:%M"),
            "time_guard_open_auction_start": actual.strftime("%H:%M"),
        }

    def _build_idle_context(self, now: datetime) -> dict[str, object]:
        return cast(dict[str, object], self._manifest_service.build_idle_context(now))

    def _build_idle_task_manifests(self) -> dict[str, dict[str, object]]:
        return cast(
            dict[str, dict[str, object]],
            self._manifest_service.build_idle_task_manifests(),
        )

    def _idle_prepare_weekend_cycle_state(self, trade_date: str) -> None:
        self._registry_service.idle_prepare_weekend_cycle_state(trade_date)

    def _idle_weekend_due_tasks(self, trade_date: str, now_clock: datetime) -> list[str]:
        return cast(
            list[str],
            self._registry_service.idle_weekend_due_tasks(
                trade_date=trade_date,
                now_clock=now_clock,
            ),
        )

    def _idle_weekend_sorted_p1_tasks(self) -> list[str]:
        return cast(list[str], self._registry_service.idle_weekend_sorted_p1_tasks())

    def _idle_weekend_remaining_minutes(self, now_clock: datetime) -> int:
        return cast(int, self._registry_service.idle_weekend_remaining_minutes(now_clock))

    def _idle_should_force_weekend_task(self, task_id: str) -> bool:
        return cast(bool, self._registry_service.idle_should_force_weekend_task(task_id))

    def _idle_record_weekend_defer(self, task_id: str, trade_date: str) -> None:
        self._registry_service.idle_record_weekend_defer(task_id, trade_date)

    def _idle_latest_trade_date_for_task(self, task_id: str) -> str:
        return cast(str, self._registry_service.idle_latest_trade_date_for_task(task_id))

    def _idle_due_tasks(self, context: dict[str, object]) -> list[str]:
        return cast(list[str], self._registry_service.idle_due_tasks(context))

    def _idle_already_ran(self, task_id: str, trade_date: str) -> bool:
        return cast(bool, self._registry_service.idle_already_ran(task_id, trade_date))

    def _idle_set_task_status(self, task_id: str, trade_date: str, status: str) -> None:
        self._registry_service.idle_set_task_status(task_id, trade_date, status)

    def _idle_get_task_status(self, task_id: str, trade_date: str) -> str:
        return cast(str, self._registry_service.idle_get_task_status(task_id, trade_date))

    def _idle_mark_ran(self, task_id: str, trade_date: str) -> None:
        self._registry_service.idle_mark_ran(task_id, trade_date)

    def _run_idle_task(self, task_id: str, context: dict[str, object]) -> dict[str, object]:
        return cast(dict[str, object], self._registry_service.run_idle_task(task_id, context))

    def _idle_update_task_health(
        self,
        task_id: str,
        status: str,
        now: datetime | None = None,
    ) -> None:
        self._dispatch_service.idle_update_task_health(task_id, status, now=now)

    def _idle_task_ttl(self, task_id: str) -> int:
        return cast(int, self._registry_service.idle_task_ttl(task_id))

    def _job_idle_queue_tick(self) -> dict[str, object]:
        return cast(dict[str, object], self._cycle_service.job_idle_queue_tick())


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
