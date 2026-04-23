"""Idle queue persistence, output, and file-policy workflows."""

# mypy: disable-error-code=redundant-cast

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from datetime import datetime
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from stock_analyzer.runtime.services.idle_queue_file_strategy_service import (
    RuntimeIdleQueueFileStrategyService,
)

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService


class RuntimeIdleQueueStorageService:
    """Idle queue persistence, output, and file-policy workflows."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service
        self._file_strategy_service = RuntimeIdleQueueFileStrategyService(service)

    def _store_idle_report(self, report: dict[str, object]) -> None:
        service = self._service
        service._last_idle_report = report
        service._idle_history.append(report)
        history_limit = max(
            1, _as_int(service._config.idle_queue.history_memory_limit, default=500)
        )
        if len(service._idle_history) > history_limit:
            overflow = len(service._idle_history) - history_limit
            if overflow > 0:
                service._idle_history = service._idle_history[overflow:]
        service._persist_idle_report_to_disk(report)

    def _load_idle_history_from_disk(self) -> None:
        service = self._service
        path = service._idle_history_path
        if not path.exists():
            return
        records: list[dict[str, object]] = []
        try:
            with path.open("r", encoding="utf-8") as fp:
                for line in fp:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        records.append(payload)
        except OSError:
            return
        limit = max(1, _as_int(service._config.idle_queue.history_memory_limit, default=500))
        service._idle_history = records[-limit:]
        if service._idle_history:
            service._last_idle_report = service._idle_history[-1]

    def _persist_idle_report_to_disk(self, report: dict[str, object]) -> None:
        service = self._service
        path = service._idle_history_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(report, ensure_ascii=False) + "\n")
        except OSError:
            return

        disk_limit = max(1, _as_int(service._config.idle_queue.history_disk_limit, default=5000))
        try:
            with path.open("r", encoding="utf-8") as fp:
                lines = fp.readlines()
        except OSError:
            return
        if len(lines) <= disk_limit:
            return
        keep = lines[-disk_limit:]
        try:
            path.write_text("".join(keep), encoding="utf-8")
        except OSError:
            return

    def _idle_collect_capacity_metrics(
        self,
        context: dict[str, object],
        now: datetime,
    ) -> dict[str, object]:
        service = self._service
        output_root = service._resolve_evolution_path(service._config.idle_queue.output_root)
        current_bytes = _safe_directory_size(output_root) if output_root.exists() else 0
        usage_target = output_root if output_root.exists() else output_root.parent
        disk_usage_pct = 0.0
        disk_total_bytes = 0
        disk_used_bytes = 0
        try:
            usage = shutil.disk_usage(usage_target)
            disk_total_bytes = max(_as_int(usage.total, default=0), 0)
            disk_used_bytes = max(_as_int(usage.used, default=0), 0)
            if disk_total_bytes > 0:
                disk_usage_pct = float(disk_used_bytes / disk_total_bytes * 100.0)
        except OSError:
            disk_usage_pct = 0.0
        date_key = now.strftime("%Y%m%d")
        if date_key not in service._idle_staging_size_baseline_by_date:
            service._idle_staging_size_baseline_by_date[date_key] = current_bytes
        daily_baseline = service._idle_staging_size_baseline_by_date[date_key]
        daily_growth = max(current_bytes - daily_baseline, 0)

        weekend_trade_date = str(context.get("trade_date", "")).strip()
        weekend_growth: int | None = None
        if str(context.get("window", "")) == "weekend" and weekend_trade_date:
            if weekend_trade_date not in service._idle_weekend_size_baseline_by_trade_date:
                service._idle_weekend_size_baseline_by_trade_date[weekend_trade_date] = (
                    current_bytes
                )
            weekend_baseline = service._idle_weekend_size_baseline_by_trade_date[weekend_trade_date]
            weekend_growth = max(current_bytes - weekend_baseline, 0)

        workday_sla = max(
            1, _as_int(service._config.idle_queue.staging_growth_sla_workday_mb, default=500)
        )
        weekend_sla = max(
            1,
            _as_int(service._config.idle_queue.staging_growth_sla_weekend_mb, default=5120),
        )
        workday_sla_bytes = workday_sla * 1024 * 1024
        weekend_sla_bytes = weekend_sla * 1024 * 1024
        pause_high = _as_float(
            service._config.idle_queue.resource_pause_high_watermark_pct,
            default=88.0,
        )
        pause_low = _as_float(
            service._config.idle_queue.resource_pause_low_watermark_pct,
            default=82.0,
        )
        if pause_low > pause_high:
            pause_low = max(0.0, pause_high - 0.5)
        return {
            "staging_size_bytes": current_bytes,
            "daily_growth_bytes": daily_growth,
            "daily_growth_sla_bytes": workday_sla_bytes,
            "daily_growth_sla_ok": daily_growth <= workday_sla_bytes,
            "weekend_growth_bytes": weekend_growth,
            "weekend_growth_sla_bytes": weekend_sla_bytes,
            "weekend_growth_sla_ok": True
            if weekend_growth is None
            else weekend_growth <= weekend_sla_bytes,
            "resource_metric": str(service._config.idle_queue.resource_pause_metric)
            .strip()
            .lower(),
            "disk_total_bytes": disk_total_bytes,
            "disk_used_bytes": disk_used_bytes,
            "disk_usage_pct": round(disk_usage_pct, 6),
            "resource_pause_high_watermark_pct": round(pause_high, 6),
            "resource_pause_low_watermark_pct": round(pause_low, 6),
        }

    def _idle_effective_write_whitelist(self, task_id: str) -> list[dict[str, object]]:
        return cast(
            list[dict[str, object]],
            self._file_strategy_service.idle_effective_write_whitelist(task_id),
        )

    def _idle_path_within(self, path: Path, root: Path) -> bool:
        return cast(bool, self._file_strategy_service.idle_path_within(path, root))

    def _idle_whitelist_hit(self, task_id: str, path: Path, action: str) -> bool:
        return cast(
            bool,
            self._file_strategy_service.idle_whitelist_hit(
                task_id=task_id,
                path=path,
                action=action,
            ),
        )

    def _idle_forbidden_hit(self, path: Path) -> bool:
        return cast(bool, self._file_strategy_service.idle_forbidden_hit(path))

    def _idle_assert_write_allowed(self, task_id: str, path: Path, action: str) -> None:
        self._file_strategy_service.idle_assert_write_allowed(task_id, path, action)

    def _idle_infer_task_id_from_output_path(self, path: Path) -> str:
        return cast(str, self._file_strategy_service.idle_infer_task_id_from_output_path(path))

    def _idle_validate_relative_fragment(self, fragment: str, label: str) -> str:
        return cast(
            str,
            self._file_strategy_service.idle_validate_relative_fragment(fragment, label),
        )

    def _idle_output_dir(self, trade_date: str, task_id: str, subdir: str = "") -> Path:
        return cast(
            Path,
            self._file_strategy_service.idle_output_dir(
                trade_date=trade_date,
                task_id=task_id,
                subdir=subdir,
            ),
        )

    def _idle_output_path(
        self,
        trade_date: str,
        task_id: str,
        subdir: str,
        filename: str,
    ) -> Path:
        return cast(
            Path,
            self._file_strategy_service.idle_output_path(
                trade_date=trade_date,
                task_id=task_id,
                subdir=subdir,
                filename=filename,
            ),
        )

    def _idle_write_json(self, path: Path, payload: Mapping[str, object]) -> None:
        self._file_strategy_service.idle_write_json(path, payload)

    def _idle_write_text(self, path: Path, payload: str) -> None:
        self._file_strategy_service.idle_write_text(path, payload)

    def _idle_write_checkpoint(
        self,
        task_id: str,
        trade_date: str,
        phase: str,
        now: datetime,
        extra: dict[str, object],
    ) -> None:
        self._file_strategy_service.idle_write_checkpoint(
            task_id=task_id,
            trade_date=trade_date,
            phase=phase,
            now=now,
            extra=extra,
        )

    def _idle_enforce_checkpoint_retention(self, directory: Path, task_id: str) -> None:
        self._file_strategy_service.idle_enforce_checkpoint_retention(directory, task_id)

    def _idle_find_latest_task_report(
        self,
        task_id: str,
        subdir: str,
        filename: str,
        exclude_trade_date: str,
    ) -> dict[str, object] | None:
        return cast(
            dict[str, object] | None,
            self._file_strategy_service.idle_find_latest_task_report(
                task_id=task_id,
                subdir=subdir,
                filename=filename,
                exclude_trade_date=exclude_trade_date,
            ),
        )

    def _idle_symbol_universe(
        self,
        *,
        task_id: str,
        max_symbols: int,
        min_symbols: int = 1,
    ) -> dict[str, object]:
        service = self._service
        cap = max(1, _as_int(max_symbols, default=1))
        watchlist_symbols = [
            normalized
            for normalized in (_normalize_a_share_symbol(item) for item in service._state.watchlist)
            if normalized
        ]
        if watchlist_symbols:
            selected = _dedupe_preserve_order(watchlist_symbols)[:cap]
            return {
                "source": "watchlist_runtime",
                "symbols": selected,
                "count": len(selected),
                "degraded": True,
                "errors": [f"idle_{task_id}_watchlist_runtime"],
                "cache_path": str(service._universe_cache_path),
            }
        return cast(
            dict[str, object],
            service._resolve_symbol_universe(
                max_symbols=cap,
                min_symbols=min_symbols,
                allow_seed_fallback=True,
                allow_online_sources=False,
            ),
        )


@lru_cache(maxsize=1)
def _runtime_service_module() -> Any:
    return import_module("stock_analyzer.runtime.service")


def _as_float(value: object, default: float) -> float:
    return cast(float, _runtime_service_module()._as_float(value, default))


def _as_int(value: object, default: int) -> int:
    return cast(int, _runtime_service_module()._as_int(value, default))


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    return cast(list[str], _runtime_service_module()._dedupe_preserve_order(items))


def _normalize_a_share_symbol(value: object) -> str:
    return cast(str, _runtime_service_module()._normalize_a_share_symbol(value))


def _safe_directory_size(root: Path) -> int:
    return cast(int, _runtime_service_module()._safe_directory_size(root))
