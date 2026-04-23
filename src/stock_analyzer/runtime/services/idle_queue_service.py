"""Idle queue service facade that delegates to smaller idle queue modules."""

# mypy: disable-error-code=no-any-return

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from stock_analyzer.runtime.services.idle_queue_orchestration_service import (
    RuntimeIdleQueueOrchestrationService,
)
from stock_analyzer.runtime.services.idle_queue_storage_service import (
    RuntimeIdleQueueStorageService,
)
from stock_analyzer.runtime.services.idle_queue_weekend_service import (
    RuntimeIdleQueueWeekendService,
)
from stock_analyzer.runtime.services.idle_queue_workday_service import (
    RuntimeIdleQueueWorkdayService,
)

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService


class RuntimeIdleQueueService:
    """Facade for idle queue orchestration, tasks, and storage policies."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service
        self._orchestration_service = RuntimeIdleQueueOrchestrationService(service)
        self._workday_service = RuntimeIdleQueueWorkdayService(service)
        self._weekend_service = RuntimeIdleQueueWeekendService(service)
        self._storage_service = RuntimeIdleQueueStorageService(service)

    def _idle_policy_modes(self, raw_modes: list[str], default_modes: set[str]) -> set[str]:
        return self._orchestration_service._idle_policy_modes(
            raw_modes,
            default_modes,
        )

    def _idle_production_canary_hit(self) -> tuple[bool, str]:
        return self._orchestration_service._idle_production_canary_hit()

    def _idle_policy_switch(
        self,
        *,
        configured: bool,
        policy_raw: str,
        modes: set[str],
        flag_name: str,
    ) -> tuple[bool, str]:
        return self._orchestration_service._idle_policy_switch(
            configured=configured,
            policy_raw=policy_raw,
            modes=modes,
            flag_name=flag_name,
        )

    def _resolve_idle_queue_enabled(self) -> tuple[bool, str]:
        return self._orchestration_service._resolve_idle_queue_enabled()

    def _resolve_idle_queue_auto_run(self) -> tuple[bool, str]:
        return self._orchestration_service._resolve_idle_queue_auto_run()

    def latest_idle_queue_report(self) -> dict[str, object] | None:
        return self._orchestration_service.latest_idle_queue_report()

    def idle_queue_history(self, limit: int = 20) -> dict[str, object]:
        return self._orchestration_service.idle_queue_history(limit)

    def idle_queue_state(self) -> dict[str, object]:
        return self._orchestration_service.idle_queue_state()

    def idle_queue_ack_blocked(
        self,
        task_id: str = "",
        clear_all: bool = False,
        now: datetime | None = None,
    ) -> dict[str, object]:
        return self._orchestration_service.idle_queue_ack_blocked(
            task_id,
            clear_all,
            now,
        )

    def _idle_task_health_snapshot(self) -> list[dict[str, object]]:
        return self._orchestration_service._idle_task_health_snapshot()

    def _idle_notification_template(
        self,
        event: str,
        payload: dict[str, object],
    ) -> tuple[str, str, str]:
        return self._orchestration_service._idle_notification_template(
            event,
            payload,
        )

    def _idle_emit_state_notification(
        self,
        title: str,
        content: str,
        level: str = "warn",
        now: datetime | None = None,
    ) -> None:
        return self._orchestration_service._idle_emit_state_notification(
            title,
            content,
            level,
            now,
        )

    def run_idle_queue_cycle(
        self,
        now: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        return self._orchestration_service.run_idle_queue_cycle(
            now,
            source_trace_id,
        )

    def _idle_refresh_pause_state(
        self,
        now: datetime,
        capacity_metrics: dict[str, object],
        context: dict[str, object],
    ) -> dict[str, object]:
        return self._orchestration_service._idle_refresh_pause_state(
            now,
            capacity_metrics,
            context,
        )

    def _idle_task_retry_policy(self, task_id: str) -> dict[str, object]:
        return self._orchestration_service._idle_task_retry_policy(task_id)

    def _idle_error_code(self, result: dict[str, object], timed_out: bool = False) -> str:
        return self._orchestration_service._idle_error_code(
            result,
            timed_out,
        )

    def _idle_should_retry(
        self,
        status: str,
        error_code: str,
        attempt_index: int,
        retry_policy: dict[str, object],
    ) -> bool:
        return self._orchestration_service._idle_should_retry(
            status,
            error_code,
            attempt_index,
            retry_policy,
        )

    def _idle_timeout_partial_report(
        self,
        task_id: str,
        context: dict[str, object],
        elapsed_seconds: float,
        max_wall_minutes: int,
        attempts: list[dict[str, object]],
    ) -> str:
        return self._orchestration_service._idle_timeout_partial_report(
            task_id,
            context,
            elapsed_seconds,
            max_wall_minutes,
            attempts,
        )

    def _idle_run_task_with_timeout(
        self,
        task_id: str,
        context: dict[str, object],
        timeout_seconds: float | None,
    ) -> tuple[dict[str, object], bool, float]:
        return self._orchestration_service._idle_run_task_with_timeout(
            task_id,
            context,
            timeout_seconds,
        )

    def _idle_execute_task_with_policy(
        self,
        task_id: str,
        context: dict[str, object],
    ) -> dict[str, object]:
        return self._orchestration_service._idle_execute_task_with_policy(
            task_id,
            context,
        )

    def _idle_update_wd_report_kpi(
        self,
        context: dict[str, object],
        result: dict[str, object],
    ) -> None:
        return self._orchestration_service._idle_update_wd_report_kpi(
            context,
            result,
        )

    def _store_idle_report(self, report: dict[str, object]) -> None:
        return self._storage_service._store_idle_report(report)

    def _load_idle_history_from_disk(self) -> None:
        return self._storage_service._load_idle_history_from_disk()

    def _persist_idle_report_to_disk(self, report: dict[str, object]) -> None:
        return self._storage_service._persist_idle_report_to_disk(report)

    def _idle_check_time_guard_sync(self, context: dict[str, object]) -> dict[str, object]:
        return self._orchestration_service._idle_check_time_guard_sync(context)

    def _idle_collect_capacity_metrics(
        self,
        context: dict[str, object],
        now: datetime,
    ) -> dict[str, object]:
        return self._storage_service._idle_collect_capacity_metrics(
            context,
            now,
        )

    def _build_idle_context(self, now: datetime) -> dict[str, object]:
        return self._orchestration_service._build_idle_context(now)

    def _build_idle_task_manifests(self) -> dict[str, dict[str, object]]:
        return self._orchestration_service._build_idle_task_manifests()

    def _idle_prepare_weekend_cycle_state(self, trade_date: str) -> None:
        return self._orchestration_service._idle_prepare_weekend_cycle_state(trade_date)

    def _idle_weekend_due_tasks(self, trade_date: str, now_clock: datetime) -> list[str]:
        return self._orchestration_service._idle_weekend_due_tasks(
            trade_date,
            now_clock,
        )

    def _idle_weekend_sorted_p1_tasks(self) -> list[str]:
        return self._orchestration_service._idle_weekend_sorted_p1_tasks()

    def _idle_weekend_remaining_minutes(self, now_clock: datetime) -> int:
        return self._orchestration_service._idle_weekend_remaining_minutes(now_clock)

    def _idle_should_force_weekend_task(self, task_id: str) -> bool:
        return self._orchestration_service._idle_should_force_weekend_task(task_id)

    def _idle_record_weekend_defer(self, task_id: str, trade_date: str) -> None:
        return self._orchestration_service._idle_record_weekend_defer(
            task_id,
            trade_date,
        )

    def _idle_latest_trade_date_for_task(self, task_id: str) -> str:
        return self._orchestration_service._idle_latest_trade_date_for_task(task_id)

    def _idle_due_tasks(self, context: dict[str, object]) -> list[str]:
        return self._orchestration_service._idle_due_tasks(context)

    def _idle_already_ran(self, task_id: str, trade_date: str) -> bool:
        return self._orchestration_service._idle_already_ran(
            task_id,
            trade_date,
        )

    def _idle_set_task_status(self, task_id: str, trade_date: str, status: str) -> None:
        return self._orchestration_service._idle_set_task_status(
            task_id,
            trade_date,
            status,
        )

    def _idle_get_task_status(self, task_id: str, trade_date: str) -> str:
        return self._orchestration_service._idle_get_task_status(
            task_id,
            trade_date,
        )

    def _idle_mark_ran(self, task_id: str, trade_date: str) -> None:
        return self._orchestration_service._idle_mark_ran(
            task_id,
            trade_date,
        )

    def _run_idle_task(self, task_id: str, context: dict[str, object]) -> dict[str, object]:
        return self._orchestration_service._run_idle_task(
            task_id,
            context,
        )

    def _idle_task_wd_p0_01(self, context: dict[str, object]) -> dict[str, object]:
        return self._workday_service._idle_task_wd_p0_01(context)

    def _idle_task_wd_p0_02(self, context: dict[str, object]) -> dict[str, object]:
        return self._workday_service._idle_task_wd_p0_02(context)

    def _idle_task_wd_p0_03(self, context: dict[str, object]) -> dict[str, object]:
        return self._workday_service._idle_task_wd_p0_03(context)

    def _idle_task_wd_p0_04(self, context: dict[str, object]) -> dict[str, object]:
        return self._workday_service._idle_task_wd_p0_04(context)

    def _idle_task_wd_p1_05(self, context: dict[str, object]) -> dict[str, object]:
        return self._workday_service._idle_task_wd_p1_05(context)

    def _idle_task_wd_p1_06(self, context: dict[str, object]) -> dict[str, object]:
        return self._workday_service._idle_task_wd_p1_06(context)

    def _idle_task_wd_p1_07(self, context: dict[str, object]) -> dict[str, object]:
        return self._workday_service._idle_task_wd_p1_07(context)

    def _idle_validate_precompute_cache(
        self,
        *,
        path: Path,
        expected_trade_date: str,
        now: datetime,
    ) -> dict[str, object]:
        return self._workday_service._idle_validate_precompute_cache(
            path=path,
            expected_trade_date=expected_trade_date,
            now=now,
        )

    def _idle_task_wd_report(self, context: dict[str, object]) -> dict[str, object]:
        return self._workday_service._idle_task_wd_report(context)

    def _idle_task_we_p0_01(self, context: dict[str, object]) -> dict[str, object]:
        return self._weekend_service._idle_task_we_p0_01(context)

    def _idle_task_we_p0_02(self, context: dict[str, object]) -> dict[str, object]:
        return self._weekend_service._idle_task_we_p0_02(context)

    def _idle_task_we_p1_03(self, context: dict[str, object]) -> dict[str, object]:
        return self._weekend_service._idle_task_we_p1_03(context)

    def _idle_task_we_p1_04(self, context: dict[str, object]) -> dict[str, object]:
        return self._weekend_service._idle_task_we_p1_04(context)

    def _idle_task_we_p1_05(self, context: dict[str, object]) -> dict[str, object]:
        return self._weekend_service._idle_task_we_p1_05(context)

    def _idle_task_we_p1_06(self, context: dict[str, object]) -> dict[str, object]:
        return self._weekend_service._idle_task_we_p1_06(context)

    def _idle_task_we_p1_07(self, context: dict[str, object]) -> dict[str, object]:
        return self._weekend_service._idle_task_we_p1_07(context)

    def _idle_task_we_p2_08(self, context: dict[str, object]) -> dict[str, object]:
        return self._weekend_service._idle_task_we_p2_08(context)

    def _idle_update_task_health(
        self,
        task_id: str,
        status: str,
        now: datetime | None = None,
    ) -> None:
        return self._orchestration_service._idle_update_task_health(
            task_id,
            status,
            now,
        )

    def _idle_task_ttl(self, task_id: str) -> int:
        return self._orchestration_service._idle_task_ttl(task_id)

    def _idle_effective_write_whitelist(self, task_id: str) -> list[dict[str, object]]:
        return self._storage_service._idle_effective_write_whitelist(task_id)

    def _idle_path_within(self, path: Path, root: Path) -> bool:
        return self._storage_service._idle_path_within(
            path,
            root,
        )

    def _idle_whitelist_hit(self, task_id: str, path: Path, action: str) -> bool:
        return self._storage_service._idle_whitelist_hit(
            task_id,
            path,
            action,
        )

    def _idle_forbidden_hit(self, path: Path) -> bool:
        return self._storage_service._idle_forbidden_hit(path)

    def _idle_assert_write_allowed(self, task_id: str, path: Path, action: str) -> None:
        return self._storage_service._idle_assert_write_allowed(
            task_id,
            path,
            action,
        )

    def _idle_infer_task_id_from_output_path(self, path: Path) -> str:
        return self._storage_service._idle_infer_task_id_from_output_path(path)

    def _idle_validate_relative_fragment(self, fragment: str, label: str) -> str:
        return self._storage_service._idle_validate_relative_fragment(
            fragment,
            label,
        )

    def _idle_output_dir(self, trade_date: str, task_id: str, subdir: str = "") -> Path:
        return self._storage_service._idle_output_dir(
            trade_date,
            task_id,
            subdir,
        )

    def _idle_output_path(
        self,
        trade_date: str,
        task_id: str,
        subdir: str,
        filename: str,
    ) -> Path:
        return self._storage_service._idle_output_path(
            trade_date,
            task_id,
            subdir,
            filename,
        )

    def _idle_write_json(self, path: Path, payload: Mapping[str, object]) -> None:
        return self._storage_service._idle_write_json(
            path,
            payload,
        )

    def _idle_write_text(self, path: Path, payload: str) -> None:
        return self._storage_service._idle_write_text(
            path,
            payload,
        )

    def _idle_write_checkpoint(
        self,
        task_id: str,
        trade_date: str,
        phase: str,
        now: datetime,
        extra: dict[str, object],
    ) -> None:
        return self._storage_service._idle_write_checkpoint(
            task_id,
            trade_date,
            phase,
            now,
            extra,
        )

    def _idle_enforce_checkpoint_retention(self, directory: Path, task_id: str) -> None:
        return self._storage_service._idle_enforce_checkpoint_retention(
            directory,
            task_id,
        )

    def _idle_find_latest_task_report(
        self,
        task_id: str,
        subdir: str,
        filename: str,
        exclude_trade_date: str,
    ) -> dict[str, object] | None:
        return self._storage_service._idle_find_latest_task_report(
            task_id,
            subdir,
            filename,
            exclude_trade_date,
        )

    def _idle_symbol_universe(
        self,
        *,
        task_id: str,
        max_symbols: int,
        min_symbols: int = 1,
    ) -> dict[str, object]:
        return self._storage_service._idle_symbol_universe(
            task_id=task_id,
            max_symbols=max_symbols,
            min_symbols=min_symbols,
        )

    def _job_idle_queue_tick(self) -> dict[str, object]:
        return self._orchestration_service._job_idle_queue_tick()
