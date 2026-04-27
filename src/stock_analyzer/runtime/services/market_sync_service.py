"""Market sync and market-warehouse workflows extracted from the runtime service."""

from __future__ import annotations

import json
import os
import socket
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from typing import Any, cast
from uuid import uuid4

import pandas as pd

from stock_analyzer.data.akshare_provider import AkshareProvider
from stock_analyzer.data.cached_provider import CachedProvider
from stock_analyzer.data.efinance_provider import EfinanceProvider
from stock_analyzer.data.intraday_summary import (
    fetch_sina_minute_bars,
    summarize_minute_bars,
    sync_intraday_summary_bundle,
)
from stock_analyzer.data.market_warehouse import (
    MarketWarehouse,
    list_package_symbols,
    write_package_daily_bars,
    write_package_intraday_summary,
)
from stock_analyzer.data.provider import DataSourceError, MarketDataProvider
from stock_analyzer.data.provider_factory import build_primary_provider
from stock_analyzer.data.resilient_provider import ResilientProvider
from stock_analyzer.data.tdx_sync import (
    TdxSyncError,
    inspect_tdx_source_freshness,
    load_tdx_manifest,
    run_tdx_offline_package_build,
)


class RuntimeMarketSyncService:
    """Delegated market-sync, TongDaXin sync, and market-warehouse workflows."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def latest_tdx_sync_report(self) -> dict[str, object] | None:
        """Return latest TongDaXin offline sync report."""
        service = self._service
        service._load_tdx_sync_history_from_disk()
        report = service._last_tdx_sync_report
        return report if isinstance(report, dict) else None

    def tdx_sync_history(self, limit: int = 20) -> dict[str, object]:
        """Return recent TongDaXin offline sync reports."""
        service = self._service
        service._load_tdx_sync_history_from_disk()
        capped_limit = max(1, min(limit, 200))
        recent = service._tdx_sync_history[-capped_limit:]
        return {"records": len(recent), "items": recent}

    def latest_market_warehouse_report(self) -> dict[str, object] | None:
        """Return latest market-warehouse sync report."""
        service = self._service
        service._load_market_warehouse_history_from_disk()
        report = service._last_market_warehouse_report
        return report if isinstance(report, dict) else None

    def market_warehouse_history(self, limit: int = 20) -> dict[str, object]:
        """Return recent market-warehouse sync reports."""
        service = self._service
        service._load_market_warehouse_history_from_disk()
        capped_limit = max(1, min(limit, 200))
        recent = service._market_warehouse_history[-capped_limit:]
        return {"records": len(recent), "items": recent}

    def latest_market_warehouse_progress(self) -> dict[str, object] | None:
        """Return latest market-warehouse sync progress snapshot."""
        service = self._service
        service._load_market_warehouse_progress_from_disk()
        report = service._last_market_warehouse_progress
        return report if isinstance(report, dict) else None

    def market_warehouse_sync_lock_status(self) -> dict[str, object]:
        """Return current market-warehouse sync lock state."""
        service = self._service
        lock_path = self._resolve_market_warehouse_sync_lock_path()
        payload = self._read_market_warehouse_sync_lock(lock_path)
        if not payload:
            return {
                "exists": False,
                "running": False,
                "is_stale": False,
                "lock_path": service._to_evolution_relative(lock_path),
            }
        payload["running"] = bool(payload.get("exists", False)) and not bool(
            payload.get("is_stale", False)
        )
        active_progress = self.latest_market_warehouse_progress()
        if (
            isinstance(active_progress, dict)
            and str(active_progress.get("trace_id", "")).strip()
            == str(payload.get("trace_id", "")).strip()
        ):
            payload["active_progress"] = active_progress
        return payload

    def market_warehouse_background_data_status(self) -> dict[str, object]:
        """Return latest market-warehouse background-data coverage status."""
        service = self._service
        try:
            snapshot = service._market_warehouse().background_data_quality_snapshot()
        except Exception as exc:
            return {
                "status": "error",
                "reason": str(exc),
                "error_type": exc.__class__.__name__,
            }
        if not isinstance(snapshot, dict):
            return {
                "status": "error",
                "reason": "background_snapshot_not_mapping",
            }

        raw_status = str(snapshot.get("status", "")).strip()
        raw_reason = str(snapshot.get("reason", "")).strip()
        raw_status_reasons = _string_list(snapshot.get("status_reasons"))
        snapshot["raw_status"] = raw_status
        snapshot["raw_reason"] = raw_reason
        snapshot["raw_status_reasons"] = list(raw_status_reasons)

        latest_report = self.latest_market_warehouse_report()
        failed_symbols = self._extract_market_warehouse_failed_symbols(latest_report)
        snapshot["latest_sync_failed_symbols_total"] = len(failed_symbols)
        snapshot["latest_sync_failed_symbols_sample"] = failed_symbols[:20]
        if isinstance(latest_report, dict):
            snapshot["latest_sync_status"] = str(latest_report.get("status", "")).strip()
            snapshot["latest_sync_symbol_source"] = str(
                latest_report.get("symbol_source", "")
            ).strip()
            snapshot["latest_sync_trace_id"] = str(latest_report.get("trace_id", "")).strip()

        full_universe_clean = (
            isinstance(latest_report, dict)
            and str(latest_report.get("symbol_source", "")).strip() == "full_universe"
            and str(latest_report.get("status", "")).strip().lower() in {"ok", "partial"}
            and _as_int(
                latest_report.get("failed_symbols_total"),
                default=len(failed_symbols),
            )
            == 0
        )
        if (
            raw_status == "partial"
            and raw_status_reasons == ["latest_trade_date_not_full_universe"]
            and full_universe_clean
        ):
            snapshot["status"] = "ok"
            snapshot["reason"] = ""
            snapshot["status_reasons"] = []
            snapshot["nonblocking_reasons"] = ["latest_trade_date_not_full_universe"]
            snapshot["nonblocking_stale_symbols_total"] = _as_int(
                snapshot.get("symbols_stale"),
                default=0,
            )
        return snapshot

    def market_warehouse_runtime_status(self) -> dict[str, object]:
        """Return combined market-warehouse runtime state for ops inspection."""
        return {
            "report": self.latest_market_warehouse_report(),
            "progress": self.latest_market_warehouse_progress(),
            "lock": self.market_warehouse_sync_lock_status(),
            "background_data": self.market_warehouse_background_data_status(),
        }

    def _load_tdx_sync_history_from_disk(self) -> None:
        service = self._service
        path = service._tdx_sync_history_path
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
        limit = max(1, _as_int(service._config.tdx_sync.history_limit, default=30))
        service._tdx_sync_history = records[-limit:]
        if service._tdx_sync_history:
            service._last_tdx_sync_report = service._tdx_sync_history[-1]

    def _persist_tdx_sync_history_to_disk(self) -> None:
        service = self._service
        path = service._tdx_sync_history_path
        try:
            service._write_jsonl_atomic(path, service._tdx_sync_history)
        except OSError:
            return

    def _load_market_warehouse_history_from_disk(self) -> None:
        service = self._service
        path = service._market_warehouse_history_path
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
        limit = max(1, _as_int(service._config.market_warehouse.history_limit, default=30))
        service._market_warehouse_history = records[-limit:]
        if service._market_warehouse_history:
            service._last_market_warehouse_report = service._market_warehouse_history[-1]

    def _persist_market_warehouse_history_to_disk(self) -> None:
        service = self._service
        path = service._market_warehouse_history_path
        try:
            service._write_jsonl_atomic(path, service._market_warehouse_history)
        except OSError:
            return

    def _load_market_warehouse_progress_from_disk(self) -> None:
        service = self._service
        path = service._market_warehouse_progress_path
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(payload, dict):
            service._last_market_warehouse_progress = payload

    def _persist_market_warehouse_progress_to_disk(self) -> None:
        service = self._service
        path = service._market_warehouse_progress_path
        snapshot = service._last_market_warehouse_progress
        if not isinstance(snapshot, dict):
            return
        try:
            service._write_json_atomic(path, snapshot)
        except OSError:
            return

    def _resolve_tdx_sync_vipdoc_root(self) -> Path:
        service = self._service
        configured = str(service._config.tdx_sync.vipdoc_root).strip()
        if not configured:
            configured = os.getenv("TDX_VIPDOC_HOST_ROOT", "").strip()
        if not configured:
            raise TdxSyncError("tdx_sync.vipdoc_root is empty")
        candidate = Path(configured).expanduser()
        if candidate.is_absolute():
            return candidate
        return cast(Path, service._resolve_evolution_path(configured))

    def _resolve_tdx_sync_output_root(self) -> Path:
        service = self._service
        configured = str(service._config.tdx_sync.output_root).strip()
        if not configured:
            configured = str(service._config.data_source.local_data_root).strip()
        if not configured:
            raise TdxSyncError("tdx_sync.output_root is empty")
        candidate = Path(configured).expanduser()
        if candidate.is_absolute():
            return candidate
        return cast(Path, service._resolve_evolution_path(configured))

    def _resolve_tdx_sync_auto_refresh(
        self,
        requested: bool | None = None,
    ) -> tuple[bool, str]:
        service = self._service
        if requested is not None:
            return bool(requested), "request_override"
        if not bool(service._config.tdx_sync.enabled):
            return False, "tdx_sync_disabled"
        if not bool(service._config.tdx_sync.refresh_before_evolution):
            return False, "refresh_before_evolution_disabled"
        primary = str(service._config.data_source.primary).strip().lower()
        if primary not in {"market_warehouse", "tdx_offline", "tdx_local", "local_csv"}:
            return False, f"primary_not_local:{primary or 'empty'}"
        if not str(service._config.tdx_sync.vipdoc_root).strip():
            return False, "vipdoc_root_missing"
        return True, "config_auto"

    def _summarize_tdx_manifest(self, manifest: dict[str, object]) -> dict[str, object]:
        if not manifest:
            return {"exists": False}
        keys = [
            "exists",
            "manifest_path",
            "generated_at",
            "vipdoc_root",
            "output_root",
            "symbol_files_total",
            "symbol_files_written",
            "symbol_files_failed",
            "symbol_files_with_gp_factors",
            "date_min",
            "date_max",
            "package_version",
            "tdxfin_summary",
        ]
        return {key: manifest.get(key) for key in keys if key in manifest}

    def _should_run_tdx_sync_build(
        self,
        *,
        source_freshness: dict[str, object],
        manifest: dict[str, object],
        force: bool,
    ) -> tuple[bool, str]:
        if force:
            return True, "force"
        if not bool(manifest.get("exists", False)):
            return True, "manifest_missing"

        daily = source_freshness.get("daily", {})
        if not isinstance(daily, dict):
            daily = {}
        file_count = _as_int(daily.get("file_count"), default=0)
        if file_count <= 0:
            raise TdxSyncError("no .day files found under vipdoc")

        latest_source_ts = _parse_iso_datetime(daily.get("latest_timestamp"))
        latest_source_mtime = _parse_iso_datetime(daily.get("latest_mtime"))
        manifest_generated_at = _parse_iso_datetime(manifest.get("generated_at"))
        manifest_date_max = _parse_iso_datetime(manifest.get("date_max"))

        if latest_source_ts is not None and manifest_date_max is not None:
            if latest_source_ts.date() > manifest_date_max.date():
                return True, "source_daily_newer_than_package"
        elif latest_source_ts is not None and manifest_date_max is None:
            return True, "manifest_date_missing"

        if latest_source_mtime is not None and manifest_generated_at is not None:
            if latest_source_mtime > manifest_generated_at:
                return True, "source_mtime_newer_than_manifest"
        elif latest_source_mtime is not None and manifest_generated_at is None:
            return True, "manifest_generated_at_missing"

        return False, "source_unchanged"

    def _invalidate_market_data_cache(self) -> dict[str, object]:
        service = self._service
        deleted_bar_cache_keys = 0
        deleted_intraday_cache_keys = 0
        delete_prefix = getattr(service._cache, "delete_prefix", None)
        if callable(delete_prefix):
            try:
                deleted_bar_cache_keys = _as_int(delete_prefix("bars:"), default=0)
            except Exception:
                deleted_bar_cache_keys = 0
            try:
                deleted_intraday_cache_keys = _as_int(delete_prefix("intraday:"), default=0)
            except Exception:
                deleted_intraday_cache_keys = 0

        inner_daily_cache_cleared = False
        inner_intraday_cache_cleared = False
        provider = service._provider
        if isinstance(provider, CachedProvider):
            daily_cache_map = getattr(provider.inner, "_cache", None)
            intraday_cache_map = getattr(provider.inner, "_intraday_cache", None)
            if isinstance(daily_cache_map, dict):
                daily_cache_map.clear()
                inner_daily_cache_cleared = True
            if isinstance(intraday_cache_map, dict):
                intraday_cache_map.clear()
                inner_intraday_cache_cleared = True
        else:
            daily_cache_map = getattr(provider, "_cache", None)
            intraday_cache_map = getattr(provider, "_intraday_cache", None)
            if isinstance(daily_cache_map, dict):
                daily_cache_map.clear()
                inner_daily_cache_cleared = True
            if isinstance(intraday_cache_map, dict):
                intraday_cache_map.clear()
                inner_intraday_cache_cleared = True

        return {
            "deleted_bar_cache_keys": deleted_bar_cache_keys,
            "deleted_intraday_cache_keys": deleted_intraday_cache_keys,
            "inner_provider_daily_cache_cleared": inner_daily_cache_cleared,
            "inner_provider_intraday_cache_cleared": inner_intraday_cache_cleared,
        }

    def _list_tdx_package_symbols(self, package_root: Path) -> list[str]:
        bars_root = package_root / "bars"
        if not bars_root.exists() or not bars_root.is_dir():
            return []
        symbols: set[str] = set()
        for pattern in ("*.csv", "*.csv.gz", "*.parquet"):
            for path in bars_root.glob(pattern):
                symbol = _normalize_a_share_symbol(path.stem)
                if symbol and _is_supported_market_warehouse_symbol(symbol):
                    symbols.add(symbol)
        return sorted(symbols)

    def _fetch_intraday_summaries(
        self,
        *,
        symbol: str,
        lookback_days: int,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        service = self._service
        return (
            service._safe_fetch_intraday_summary(
                symbol=symbol,
                interval="1m",
                lookback_days=lookback_days,
            ),
            service._safe_fetch_intraday_summary(
                symbol=symbol,
                interval="5m",
                lookback_days=lookback_days,
            ),
        )

    def _safe_fetch_intraday_summary(
        self,
        *,
        symbol: str,
        interval: str,
        lookback_days: int,
    ) -> pd.DataFrame:
        service = self._service
        try:
            frame = cast(
                pd.DataFrame,
                service._provider.fetch_intraday_summary(
                    symbol=symbol,
                    interval=interval,
                    lookback_days=max(1, int(lookback_days)),
                ),
            )
        except Exception:
            return pd.DataFrame()
        if frame.empty:
            return frame
        if not isinstance(frame.index, pd.DatetimeIndex):
            frame = frame.copy()
            frame.index = pd.to_datetime(frame.index, errors="coerce")
        frame = frame[frame.index.notna()]
        return frame.sort_index()

    def run_tdx_offline_sync(
        self,
        *,
        timestamp: datetime | None = None,
        notify_enabled: bool | None = None,
        force: bool = False,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        service = self._service
        now = timestamp or datetime.now()
        report: dict[str, object] = {
            "timestamp": now.isoformat(),
            "trace_id": source_trace_id,
            "status": "skipped",
            "reason": "",
            "force": force,
            "data_source_primary": str(service._config.data_source.primary).strip().lower(),
        }

        if not bool(service._config.tdx_sync.enabled) and not force:
            report["reason"] = "tdx_sync_disabled"
            service._store_tdx_sync_report(report)
            return report
        if not str(service._config.tdx_sync.vipdoc_root).strip() and not force:
            report["reason"] = "vipdoc_root_missing"
            service._store_tdx_sync_report(report)
            return report

        try:
            vipdoc_root = service._resolve_tdx_sync_vipdoc_root()
            output_root = service._resolve_tdx_sync_output_root()
            source_freshness = inspect_tdx_source_freshness(vipdoc_root)
            manifest = load_tdx_manifest(output_root)
            build_required, build_reason = service._should_run_tdx_sync_build(
                source_freshness=source_freshness,
                manifest=manifest,
                force=force,
            )

            report.update(
                {
                    "vipdoc_root": str(vipdoc_root),
                    "output_root": str(output_root),
                    "source_freshness": source_freshness,
                    "package_manifest": service._summarize_tdx_manifest(manifest),
                    "build_required": build_required,
                    "reason": build_reason,
                }
            )

            if build_required:
                build = run_tdx_offline_package_build(
                    project_root=service._evolution_project_root,
                    vipdoc_root=vipdoc_root,
                    output_root=output_root,
                    include_bj=bool(service._config.tdx_sync.include_bj),
                    skip_gp=bool(service._config.tdx_sync.skip_gp),
                    max_symbols=max(0, _as_int(service._config.tdx_sync.max_symbols, default=0)),
                    timeout_sec=max(
                        30,
                        _as_int(service._config.tdx_sync.timeout_sec, default=7200),
                    ),
                )
                report["build"] = build
            else:
                report["build"] = {}
            refreshed_manifest = load_tdx_manifest(output_root)
            report["package_manifest"] = service._summarize_tdx_manifest(refreshed_manifest)

            package_symbols = service._list_tdx_package_symbols(output_root)
            daily_freshness = source_freshness.get("daily", {})
            target_end_date = now.date()
            if isinstance(daily_freshness, dict):
                daily_latest_ts = _parse_iso_datetime(daily_freshness.get("latest_timestamp"))
                if daily_latest_ts is not None:
                    target_end_date = daily_latest_ts.date()
            intraday_summary: dict[str, object]
            if package_symbols:
                intraday_summary = sync_intraday_summary_bundle(
                    vipdoc_root=vipdoc_root,
                    output_root=output_root,
                    symbols=package_symbols,
                    target_end_date=target_end_date,
                    intervals=("1m", "5m"),
                    max_workers=8,
                    online_delta=True,
                )
            else:
                intraday_summary = {
                    "generated_at": now.isoformat(),
                    "manifest_path": str(output_root / "intraday_summary_manifest.json"),
                    "intervals": {},
                    "reports": [],
                    "reason": "no_package_symbols",
                }
            report["intraday_summary"] = intraday_summary

            intraday_changed = False
            intraday_reports_obj = intraday_summary.get("reports", [])
            if isinstance(intraday_reports_obj, list):
                for item in intraday_reports_obj:
                    if not isinstance(item, dict):
                        continue
                    if _as_int(item.get("files_written"), default=0) > 0:
                        intraday_changed = True
                        break

            if build_required or intraday_changed:
                report["cache_refresh"] = service._invalidate_market_data_cache()
                report["status"] = "ok"
            else:
                report["cache_refresh"] = {
                    "deleted_bar_cache_keys": 0,
                    "deleted_intraday_cache_keys": 0,
                    "inner_provider_daily_cache_cleared": False,
                    "inner_provider_intraday_cache_cleared": False,
                }
                report["status"] = "skipped"
        except Exception as exc:
            report["status"] = "failed"
            report["reason"] = str(exc)
            report["error_type"] = exc.__class__.__name__

        service._store_tdx_sync_report(report)
        source_freshness_payload: dict[str, object] = {}
        source_freshness_obj: object = report.get("source_freshness", {})
        if isinstance(source_freshness_obj, dict):
            source_freshness_payload = source_freshness_obj
        daily_freshness = source_freshness_payload.get("daily", {})
        if not isinstance(daily_freshness, dict):
            daily_freshness = {}
        service._record_audit_event(
            event_type="tdx_offline_sync",
            trace_id=source_trace_id,
            level="warn" if str(report.get("status", "")) == "failed" else "info",
            payload={
                "status": report.get("status", ""),
                "reason": report.get("reason", ""),
                "build_required": report.get("build_required", False),
                "daily_latest_timestamp": daily_freshness.get("latest_timestamp", ""),
                "package_manifest": report.get("package_manifest", {}),
            },
        )
        service._notify_tdx_sync_if_needed(report=report, notify_enabled=notify_enabled)
        return report

    def _notify_tdx_sync_if_needed(
        self,
        *,
        report: dict[str, object],
        notify_enabled: bool | None = None,
    ) -> None:
        service = self._service
        status = str(report.get("status", "")).strip().lower()
        if status not in {"ok", "failed"}:
            return
        should_notify = bool(notify_enabled) if notify_enabled is not None else False
        if notify_enabled is None:
            if status == "ok":
                should_notify = bool(service._config.tdx_sync.notify_on_success)
            elif status == "failed":
                should_notify = bool(service._config.tdx_sync.notify_on_failure)
        if not should_notify:
            return

        source_freshness = report.get("source_freshness", {})
        if not isinstance(source_freshness, dict):
            source_freshness = {}
        daily = source_freshness.get("daily", {})
        if not isinstance(daily, dict):
            daily = {}
        minute_5 = source_freshness.get("minute_5", {})
        if not isinstance(minute_5, dict):
            minute_5 = {}
        manifest = report.get("package_manifest", {})
        if not isinstance(manifest, dict):
            manifest = {}

        if status == "ok":
            title = _push_title(priority="P2", category="数据", summary="离线行情包已刷新")
            content = (
                f"事件：离线行情包刷新完成；"
                f"影响：闲时自学习将读取最新日线到 {manifest.get('date_max', '') or '未知'}；"
                f"5分钟源最新={minute_5.get('latest_timestamp', '') or '未知'}；"
                f"建议动作：无需处理，可继续观察今晚自学习结果。"
            )
            level = "info"
        else:
            title = _push_title(priority="P1", category="运维", summary="离线行情包刷新失败")
            content = (
                f"事件：TongDaXin 离线包刷新失败；"
                f"影响：系统将继续使用现有离线包，最新日线可能未纳入今晚学习；"
                f"当前源日线最新={daily.get('latest_timestamp', '') or '未知'}；"
                f"建议动作：检查 vipdoc 挂载和通达信盘后更新。"
            )
            level = "warn"
        service.notify(
            title=title,
            content=content,
            level=level,
            trace_id=str(report.get("trace_id", "")),
        )

    def _resolve_market_warehouse_db_path(self) -> Path:
        service = self._service
        raw = str(service._config.market_warehouse.db_path).strip()
        if not raw:
            raise ValueError("market_warehouse.db_path is empty")
        return cast(Path, service._resolve_evolution_path(raw))

    def _resolve_market_warehouse_package_root(self) -> Path:
        service = self._service
        raw = str(service._config.market_warehouse.package_root).strip()
        if raw:
            return cast(Path, service._resolve_evolution_path(raw))
        fallback = str(service._config.data_source.local_data_root).strip()
        if not fallback:
            raise ValueError("market_warehouse.package_root is empty")
        return cast(Path, service._resolve_evolution_path(fallback))

    def _resolve_market_warehouse_bootstrap_source_root(self) -> Path:
        service = self._service
        raw = str(service._config.market_warehouse.bootstrap_source_root).strip()
        if raw:
            return cast(Path, service._resolve_evolution_path(raw))
        fallback = str(service._config.data_source.local_data_root).strip()
        if fallback:
            return cast(Path, service._resolve_evolution_path(fallback))
        return cast(Path, service._resolve_market_warehouse_package_root())

    def _can_bootstrap_market_warehouse_from_offline(self, source_root: Path) -> tuple[bool, str]:
        service = self._service
        if not bool(service._config.market_warehouse.offline_bootstrap_enabled):
            return False, "offline_bootstrap_disabled"
        if not source_root.exists():
            return False, "bootstrap_source_missing"
        try:
            symbol_count = len(list_package_symbols(source_root))
        except Exception as exc:
            return False, f"bootstrap_source_error:{exc.__class__.__name__}"
        if symbol_count <= 0:
            return False, "bootstrap_source_empty"
        return True, "offline_package_available"

    def _market_warehouse(self) -> MarketWarehouse:
        service = self._service
        return MarketWarehouse(
            db_path=service._resolve_market_warehouse_db_path(),
            package_root=service._resolve_market_warehouse_package_root(),
        )

    def _resolve_market_warehouse_auto_refresh(
        self,
        *,
        requested: bool | None,
    ) -> tuple[bool, str]:
        service = self._service
        if requested is False:
            return False, "disabled_by_request"
        if not bool(service._config.market_warehouse.enabled):
            return False, "market_warehouse_disabled"
        if not bool(service._config.market_warehouse.refresh_before_evolution):
            return False, "market_warehouse_refresh_disabled"
        return True, "enabled"

    def _build_market_warehouse_online_provider(self) -> MarketDataProvider:
        service = self._service
        primary_name = str(service._config.market_warehouse.online_daily_primary).strip().lower()
        if not primary_name:
            raise ValueError("market_warehouse.online_daily_primary is empty")
        request_interval = max(
            0.0,
            _as_float(
                service._config.market_warehouse.request_interval_sec,
                default=service._config.data_source.request_interval_sec,
            ),
        )
        socket_timeout_sec = max(
            1.0,
            _as_float(service._config.market_warehouse.online_socket_timeout_sec, default=6.0),
        )
        max_attempts = max(
            1,
            _as_int(service._config.market_warehouse.online_max_attempts, default=1),
        )
        primary = self._build_market_warehouse_online_single_provider(
            provider_name=primary_name,
            request_interval=request_interval,
            socket_timeout_sec=socket_timeout_sec,
            max_attempts=max_attempts,
        )
        backup_name = str(service._config.market_warehouse.online_daily_backup).strip().lower()
        if not backup_name or backup_name == primary_name:
            return primary
        backup = self._build_market_warehouse_online_single_provider(
            provider_name=backup_name,
            request_interval=request_interval,
            socket_timeout_sec=socket_timeout_sec,
            max_attempts=max_attempts,
        )
        resilient_config = service._config.data_source.model_copy(
            update={
                "primary": primary_name,
                "local_data_root": "",
                "request_interval_sec": request_interval,
            }
        )
        return ResilientProvider(primary=primary, backup=backup, config=resilient_config)

    def _build_market_warehouse_online_single_provider(
        self,
        *,
        provider_name: str,
        request_interval: float,
        socket_timeout_sec: float,
        max_attempts: int,
    ) -> MarketDataProvider:
        service = self._service
        normalized = provider_name.strip().lower()
        if normalized in {"akshare", "ak"}:
            return AkshareProvider(
                retry_delay_sec=request_interval,
                max_attempts=max_attempts,
                socket_timeout_sec=socket_timeout_sec,
            )
        if normalized in {"efinance", "ef"}:
            return EfinanceProvider(
                retry_delay_sec=request_interval,
                max_attempts=max_attempts,
                socket_timeout_sec=socket_timeout_sec,
            )
        if normalized in {"tdx_offline", "tdx_local", "local_csv"}:
            provider: MarketDataProvider = build_primary_provider(
                service._config.data_source.model_copy(
                    update={
                        "primary": normalized,
                        "local_data_root": "",
                        "request_interval_sec": request_interval,
                    }
                )
            )
            return provider
        raise DataSourceError(f"unsupported market_warehouse provider: {provider_name}")

    def _select_market_warehouse_symbols(
        self,
        *,
        warehouse: MarketWarehouse,
        package_root: Path,
        max_symbols: int,
    ) -> list[str]:
        service = self._service
        symbols = _normalize_market_warehouse_symbols(
            [
                *warehouse.list_symbols(),
                *service._list_tdx_package_symbols(package_root),
            ]
        )
        if not symbols:
            universe = service._resolve_symbol_universe(
                max_symbols=max_symbols,
                allow_online_sources=True,
            )
            symbols = _normalize_market_warehouse_symbols(universe.get("symbols", []))
        if max_symbols > 0:
            return symbols[:max_symbols]
        return symbols

    def _resolve_market_warehouse_target_trade_date(self, *, now: datetime) -> date:
        current = now.date()
        if now.weekday() >= 5:
            return _last_friday(current)
        if now.time() >= dt_time(hour=15, minute=5):
            return current
        previous = current - timedelta(days=1)
        while previous.weekday() >= 5:
            previous -= timedelta(days=1)
        return previous

    def _resolve_market_warehouse_daily_symbol_hard_timeout_sec(
        self,
        *,
        symbol_source: str = "",
    ) -> tuple[float, str]:
        service = self._service
        default_timeout_sec = max(
            0.0,
            _as_float(
                service._config.market_warehouse.daily_symbol_hard_timeout_sec,
                default=20.0,
            ),
        )
        normalized_symbol_source = str(symbol_source).strip().lower()
        if normalized_symbol_source == "full_universe":
            full_universe_timeout_sec = max(
                0.0,
                _as_float(
                    service._config.market_warehouse.daily_symbol_hard_timeout_sec_full_universe,
                    default=0.0,
                ),
            )
            if full_universe_timeout_sec > 0:
                return full_universe_timeout_sec, "full_universe_override"
        return default_timeout_sec, "default"

    def _run_with_hard_timeout(
        self,
        *,
        timeout_sec: float,
        operation: str,
        callback: Callable[[], object],
    ) -> object:
        if timeout_sec <= 0:
            return callback()

        result_queue: Queue[tuple[bool, object]] = Queue(maxsize=1)

        def _worker() -> None:
            try:
                result_queue.put((True, callback()))
            except BaseException as exc:
                result_queue.put((False, exc))

        worker = Thread(
            target=_worker,
            name=f"market-sync-{operation[:40]}",
            daemon=True,
        )
        worker.start()
        worker.join(timeout_sec)
        if worker.is_alive():
            raise TimeoutError(f"{operation}_timeout_after_{timeout_sec:.1f}s")
        try:
            success, payload = result_queue.get_nowait()
        except Empty as exc:
            raise RuntimeError(f"{operation}_completed_without_result") from exc
        if success:
            return payload
        if isinstance(payload, BaseException):
            raise payload
        raise RuntimeError(f"{operation}_failed_without_exception")

    def _resolve_market_warehouse_daily_lookback_days(
        self,
        *,
        latest_date: date | None,
        target_end_date: date,
        force: bool,
    ) -> tuple[int, str]:
        service = self._service
        base_lookback_days = max(
            40,
            _as_int(service._config.market_warehouse.daily_lookback_days, default=120),
        )
        bootstrap_lookback_days = max(
            base_lookback_days,
            _as_int(
                service._config.market_warehouse.online_bootstrap_lookback_days,
                default=750,
            ),
        )
        if latest_date is None:
            return bootstrap_lookback_days, "bootstrap"
        if force:
            return base_lookback_days, "full"
        if latest_date >= target_end_date:
            return 0, "up_to_date"
        if not bool(service._config.market_warehouse.daily_incremental_enabled):
            return base_lookback_days, "full"
        cushion_days = max(
            2,
            _as_int(
                service._config.market_warehouse.daily_incremental_cushion_days,
                default=5,
            ),
        )
        gap_days = max(1, (target_end_date - latest_date).days)
        lookback_days = min(
            base_lookback_days,
            max(cushion_days + gap_days, cushion_days),
        )
        return max(2, lookback_days), "incremental"

    def _collect_market_warehouse_focus_symbols(self, *, max_symbols: int) -> list[str]:
        service = self._service
        candidates: list[str] = []
        candidates.extend(_normalize_a_share_symbol(item) for item in service._state.watchlist)
        try:
            positions = service._portfolio.positions()
        except Exception:
            positions = []
        for item in positions:
            if not isinstance(item, dict):
                continue
            normalized = _normalize_a_share_symbol(item.get("symbol"))
            if normalized:
                candidates.append(normalized)
        latest_week5 = service._last_week5_scan_report
        if isinstance(latest_week5, dict):
            candidates.extend(
                service._derive_watchlist_candidates_from_week5(
                    report=latest_week5,
                    top_k_override=max_symbols if max_symbols > 0 else None,
                )
            )
        deduped = _dedupe_preserve_order([symbol for symbol in candidates if symbol])
        if max_symbols > 0:
            return deduped[:max_symbols]
        return deduped

    def _resolve_market_warehouse_intraday_symbols(
        self,
        *,
        symbol_list: list[str],
    ) -> list[str]:
        service = self._service
        scope = str(service._config.market_warehouse.intraday_sync_scope).strip().lower()
        if scope == "all":
            return list(symbol_list)
        if scope == "focus":
            focus_limit = max(
                1,
                _as_int(service._config.market_warehouse.intraday_focus_max_symbols, default=120),
            )
            symbol_set = set(symbol_list)
            return [
                symbol
                for symbol in service._collect_market_warehouse_focus_symbols(
                    max_symbols=focus_limit
                )
                if symbol in symbol_set
            ]
        return list(symbol_list)

    def _carry_forward_market_warehouse_financial_fields(
        self,
        *,
        existing_daily: pd.DataFrame,
        fresh_daily: pd.DataFrame,
    ) -> pd.DataFrame:
        if existing_daily.empty or fresh_daily.empty:
            return fresh_daily
        adjusted = fresh_daily.copy()
        latest_existing = existing_daily.iloc[-1]
        financial_source = ""
        if "financial_source" in adjusted.columns and len(adjusted.index) > 0:
            financial_source = str(adjusted["financial_source"].iloc[-1]).strip()
        if financial_source.endswith("_default") or not financial_source:
            for column in (
                "roe",
                "debt_ratio",
                "financial_data_complete",
                "financial_missing_fields",
                "financial_source",
                "financial_report_date",
                "holder_count",
            ):
                if column not in adjusted.columns or column not in latest_existing.index:
                    continue
                adjusted[column] = latest_existing[column]
        return adjusted

    def _sync_market_warehouse_daily_symbol(
        self,
        *,
        warehouse: MarketWarehouse,
        online_provider: MarketDataProvider,
        symbol: str,
        force: bool,
        target_end_date: date,
        latest_daily: date | None = None,
        hard_timeout_sec: float | None = None,
    ) -> dict[str, object]:
        service = self._service
        if hard_timeout_sec is None:
            hard_timeout_sec, _ = self._resolve_market_warehouse_daily_symbol_hard_timeout_sec()
        else:
            hard_timeout_sec = max(0.0, float(hard_timeout_sec))
        latest_daily = (
            latest_daily if latest_daily is not None else warehouse.latest_daily_date(symbol=symbol)
        )
        lookback_days, sync_mode = service._resolve_market_warehouse_daily_lookback_days(
            latest_date=latest_daily,
            target_end_date=target_end_date,
            force=force,
        )
        if lookback_days <= 0:
            return {
                "status": "skipped",
                "reason": "up_to_date",
                "latest_date": latest_daily.isoformat() if latest_daily else "",
                "mode": sync_mode,
                "lookback_days": 0,
            }

        fresh_daily = cast(
            pd.DataFrame,
            self._run_with_hard_timeout(
                timeout_sec=hard_timeout_sec,
                operation=f"daily_fetch_{symbol}",
                callback=lambda: online_provider.fetch_daily_bars(
                    symbol=symbol,
                    lookback_days=lookback_days,
                    end_date=target_end_date,
                ),
            ),
        )
        if fresh_daily.empty:
            if latest_daily is None:
                raise ValueError("empty_online_daily_data")
            return {
                "status": "skipped",
                "reason": "no_online_daily_data",
                "latest_date": latest_daily.isoformat(),
                "mode": sync_mode,
                "lookback_days": lookback_days,
            }
        fresh_daily = _filter_frame_by_trade_date(fresh_daily, max_date=target_end_date)
        if latest_daily is not None and not force:
            fresh_daily = _filter_frame_by_trade_date(fresh_daily, min_date=latest_daily)
        if fresh_daily.empty:
            return {
                "status": "skipped",
                "reason": "no_rows_before_target_date",
                "latest_date": latest_daily.isoformat() if latest_daily else "",
                "mode": sync_mode,
                "lookback_days": lookback_days,
            }

        latest_online = fresh_daily.index[-1].date()
        if latest_daily is not None and not force and latest_online <= latest_daily:
            return {
                "status": "skipped",
                "reason": "no_new_trade_date",
                "latest_date": latest_daily.isoformat(),
                "mode": sync_mode,
                "lookback_days": lookback_days,
            }

        existing_daily = warehouse.fetch_all_daily_bars(symbol=symbol)
        fresh_daily = service._carry_forward_market_warehouse_financial_fields(
            existing_daily=existing_daily,
            fresh_daily=fresh_daily,
        )
        merged_daily = (
            fresh_daily
            if existing_daily.empty
            else pd.concat([existing_daily, fresh_daily], axis=0)
        )
        merged_daily = merged_daily[~merged_daily.index.duplicated(keep="last")].sort_index()
        warehouse.replace_daily_bars(symbol=symbol, frame=merged_daily)
        write_package_daily_bars(
            package_root=warehouse.package_root,
            symbol=symbol,
            frame=merged_daily,
        )
        latest_date = (
            merged_daily.index[-1].date().isoformat() if len(merged_daily.index) > 0 else ""
        )
        return {
            "status": "ok",
            "latest_date": latest_date,
            "rows": int(len(merged_daily)),
            "mode": sync_mode,
            "lookback_days": lookback_days,
        }

    def _sync_market_warehouse_intraday_symbol(
        self,
        *,
        warehouse: MarketWarehouse,
        symbol: str,
        interval: str,
        force: bool,
        target_end_date: date,
        existing_latest: date | None = None,
    ) -> dict[str, object]:
        existing_latest = (
            existing_latest
            if existing_latest is not None
            else warehouse.latest_intraday_date(symbol=symbol, interval=interval)
        )
        if not force and existing_latest is not None and existing_latest >= target_end_date:
            return {
                "status": "skipped",
                "interval": interval,
                "reason": "up_to_date",
                "latest_date": existing_latest.isoformat(),
            }

        minute_frame = fetch_sina_minute_bars(symbol=symbol, interval=interval)
        if minute_frame.empty:
            return {
                "status": "skipped",
                "interval": interval,
                "reason": "no_online_intraday_data",
                "latest_date": existing_latest.isoformat() if existing_latest else "",
            }

        online_summary = summarize_minute_bars(minute_frame, interval=interval)
        if online_summary.empty:
            return {
                "status": "skipped",
                "interval": interval,
                "reason": "empty_online_summary",
            }
        online_summary = _filter_frame_by_trade_date(online_summary, max_date=target_end_date)
        if existing_latest is not None and not force:
            online_summary = _filter_frame_by_trade_date(online_summary, min_date=existing_latest)
        if online_summary.empty:
            return {
                "status": "skipped",
                "interval": interval,
                "reason": "no_rows_before_target_date",
                "latest_date": existing_latest.isoformat() if existing_latest else "",
            }
        latest_online = online_summary.index[-1].date()
        if existing_latest is not None and not force and latest_online <= existing_latest:
            return {
                "status": "skipped",
                "interval": interval,
                "reason": "no_new_trade_date",
                "latest_date": existing_latest.isoformat(),
            }
        existing = warehouse.fetch_all_intraday_summary(symbol=symbol, interval=interval)
        merged = online_summary if existing.empty else pd.concat([existing, online_summary], axis=0)
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        warehouse.replace_intraday_summary(symbol=symbol, interval=interval, frame=merged)
        write_package_intraday_summary(
            package_root=warehouse.package_root,
            symbol=symbol,
            interval=interval,
            frame=merged,
        )
        latest_date = merged.index[-1].date().isoformat() if len(merged.index) > 0 else ""
        return {
            "status": "ok",
            "interval": interval,
            "latest_date": latest_date,
            "rows": int(len(merged)),
        }

    def _resolve_market_warehouse_sync_lock_path(self) -> Path:
        service = self._service
        return service._market_warehouse_progress_path.with_name("market_warehouse_sync.lock")

    def _resolve_market_warehouse_retry_source_report(
        self,
        *,
        trace_id: str = "",
    ) -> dict[str, object] | None:
        normalized_trace_id = trace_id.strip()
        latest = self.latest_market_warehouse_report()
        if not normalized_trace_id:
            return latest
        history = self.market_warehouse_history(limit=200)
        items = history.get("items")
        if isinstance(items, list):
            for item in reversed(items):
                if not isinstance(item, dict):
                    continue
                if str(item.get("trace_id", "")).strip() == normalized_trace_id:
                    return item
        if isinstance(latest, dict) and str(latest.get("trace_id", "")).strip() == normalized_trace_id:
            return latest
        return None

    def _extract_market_warehouse_failed_symbols(
        self,
        report: dict[str, object] | None,
    ) -> list[str]:
        if not isinstance(report, dict):
            return []
        raw_symbols: list[str] = []
        failed_symbols = report.get("failed_symbols")
        if isinstance(failed_symbols, list):
            raw_symbols.extend(_string_list(failed_symbols))
        failed_samples = report.get("failed_samples")
        if isinstance(failed_samples, list):
            for item in failed_samples:
                if not isinstance(item, dict):
                    continue
                raw_symbols.extend(_string_list([item.get("symbol", "")]))
        return _normalize_market_warehouse_symbols(raw_symbols)

    def _resolve_market_warehouse_retry_failed_total(
        self,
        report: dict[str, object] | None,
        *,
        extracted_symbols: list[str],
    ) -> tuple[int, bool]:
        if not isinstance(report, dict):
            return 0, True
        failed_symbols = report.get("failed_symbols")
        if isinstance(failed_symbols, list):
            return len(_normalize_market_warehouse_symbols(failed_symbols)), True
        daily_sync = report.get("daily_sync")
        intraday_sync = report.get("intraday_sync")
        failed_event_total = 0
        if isinstance(daily_sync, dict):
            failed_event_total += _as_int(daily_sync.get("failed"), default=0)
        if isinstance(intraday_sync, dict):
            failed_event_total += _as_int(intraday_sync.get("failed"), default=0)
        reported_total = _as_int(report.get("failed_symbols_total"), default=0)
        total = max(reported_total, failed_event_total, len(extracted_symbols))
        is_complete = total <= len(extracted_symbols)
        return total, is_complete

    def _resolve_market_warehouse_sync_lock_stale_after_sec(self) -> int:
        service = self._service
        symbol_timeout_sec = max(
            5.0,
            _as_float(
                service._config.market_warehouse.daily_symbol_hard_timeout_sec,
                default=20.0,
            ),
        )
        return max(120, min(1800, int(symbol_timeout_sec * 6)))

    def _resolve_market_warehouse_sync_lock_heartbeat_interval_sec(self) -> float:
        stale_after_sec = float(self._resolve_market_warehouse_sync_lock_stale_after_sec())
        return max(5.0, min(30.0, stale_after_sec / 4.0))

    def _build_market_warehouse_sync_lock_payload(
        self,
        *,
        owner_token: str,
        timestamp: datetime,
        source_trace_id: str,
        force: bool,
        stale_after_sec: int,
    ) -> dict[str, object]:
        service = self._service
        hostname = os.getenv("HOSTNAME", "").strip()
        if not hostname:
            try:
                hostname = socket.gethostname().strip()
            except OSError:
                hostname = ""
        return {
            "owner_token": owner_token,
            "trace_id": source_trace_id,
            "created_at": timestamp.isoformat(),
            "pid": os.getpid(),
            "hostname": hostname,
            "force": force,
            "stale_after_sec": stale_after_sec,
            "lock_path": service._to_evolution_relative(
                self._resolve_market_warehouse_sync_lock_path()
            ),
        }

    def _read_market_warehouse_sync_lock(self, lock_path: Path) -> dict[str, object]:
        service = self._service
        try:
            stat = lock_path.stat()
        except OSError:
            return {}

        payload: dict[str, object] = {}
        try:
            raw_payload = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw_payload = {}
        if isinstance(raw_payload, dict):
            payload.update(raw_payload)

        stale_after_sec = max(
            1,
            _as_int(
                payload.get("stale_after_sec"),
                default=self._resolve_market_warehouse_sync_lock_stale_after_sec(),
            ),
        )
        age_sec = max(0.0, datetime.now().timestamp() - stat.st_mtime)
        payload["exists"] = True
        payload["lock_path"] = service._to_evolution_relative(lock_path)
        payload["last_heartbeat_at"] = datetime.fromtimestamp(stat.st_mtime).isoformat()
        payload["age_sec"] = round(age_sec, 3)
        payload["stale_after_sec"] = stale_after_sec
        payload["is_stale"] = age_sec >= stale_after_sec
        payload["running"] = not bool(payload["is_stale"])
        return payload

    def _touch_market_warehouse_sync_lock(self, *, lock_path: Path, owner_token: str) -> bool:
        current_lock = self._read_market_warehouse_sync_lock(lock_path)
        if str(current_lock.get("owner_token", "")).strip() != owner_token.strip():
            return False
        try:
            os.utime(lock_path, None)
        except OSError:
            return False
        return True

    def _release_market_warehouse_sync_lock(self, *, lock_path: Path, owner_token: str) -> None:
        current_lock = self._read_market_warehouse_sync_lock(lock_path)
        if current_lock and str(current_lock.get("owner_token", "")).strip() != owner_token.strip():
            return
        try:
            lock_path.unlink()
        except FileNotFoundError:
            return
        except OSError:
            return

    def _acquire_market_warehouse_sync_lock(
        self,
        *,
        timestamp: datetime,
        source_trace_id: str,
        force: bool,
    ) -> tuple[Path | None, str, dict[str, object]]:
        lock_path = self._resolve_market_warehouse_sync_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        stale_after_sec = self._resolve_market_warehouse_sync_lock_stale_after_sec()
        owner_token = f"{os.getpid()}-{uuid4().hex}"
        payload = self._build_market_warehouse_sync_lock_payload(
            owner_token=owner_token,
            timestamp=timestamp,
            source_trace_id=source_trace_id,
            force=force,
            stale_after_sec=stale_after_sec,
        )

        for _ in range(3):
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                active_lock = self._read_market_warehouse_sync_lock(lock_path)
                if not bool(active_lock.get("is_stale", False)):
                    return None, "", active_lock
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    continue
                except OSError:
                    return None, "", active_lock
                continue
            except OSError:
                raise

            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fp:
                    json.dump(payload, fp, ensure_ascii=False, indent=2)
                    fp.write("\n")
            except Exception:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                raise
            return lock_path, owner_token, self._read_market_warehouse_sync_lock(lock_path)

        return None, "", self._read_market_warehouse_sync_lock(lock_path)

    def run_market_warehouse_sync(
        self,
        *,
        timestamp: datetime | None = None,
        notify_enabled: bool | None = None,
        force: bool = False,
        source_trace_id: str = "",
        symbols: list[str] | None = None,
        retry_failed_only: bool = False,
        retry_report_trace_id: str = "",
        scheduler_lock_path: Path | None = None,
        scheduler_lock_owner_token: str = "",
    ) -> dict[str, object]:
        service = self._service
        now = timestamp or datetime.now()
        requested_symbols = _normalize_market_warehouse_symbols(symbols or [])
        report: dict[str, object] = {
            "timestamp": now.isoformat(),
            "trace_id": source_trace_id,
            "status": "skipped",
            "reason": "",
            "force": force,
            "retry_failed_only": retry_failed_only,
            "lock_path": service._to_evolution_relative(
                self._resolve_market_warehouse_sync_lock_path()
            ),
            "progress_path": service._to_evolution_relative(
                service._market_warehouse_progress_path
            ),
        }
        if retry_report_trace_id.strip():
            report["retry_report_trace_id"] = retry_report_trace_id.strip()
        daily_ok = 0
        daily_failed = 0
        daily_skipped = 0
        daily_mode_counts: dict[str, int] = {"bootstrap": 0, "full": 0, "incremental": 0}
        intraday_ok = 0
        intraday_failed = 0
        intraday_skipped = 0
        failed_samples: list[dict[str, str]] = []
        failed_symbols: list[str] = []
        failed_symbol_seen: set[str] = set()
        intervals: list[str] = []
        intraday_symbol_list: list[str] = []
        symbols_completed = 0
        progress_write_every = 1
        lock_path: Path | None = None
        lock_owner_token = ""
        progress_snapshot: dict[str, object] = {
            "timestamp": now.isoformat(),
            "trace_id": source_trace_id,
            "status": "running",
            "phase": "initializing",
            "force": force,
            "started_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "current_symbol": "",
            "current_stage": "",
            "reason": "",
            "db_path": "",
            "package_root": "",
            "bootstrap_source_root": "",
            "target_trade_date": "",
            "symbols_total": 0,
            "symbols_completed": 0,
            "progress_ratio": 0.0,
            "bootstrap": {},
            "daily_sync": {},
            "intraday_sync": {},
            "failed_samples": [],
            "failed_symbols_total": 0,
        }
        last_progress_status = str(progress_snapshot["status"])
        last_progress_phase = str(progress_snapshot["phase"])
        last_progress_stage = str(progress_snapshot["current_stage"])
        last_progress_symbol = str(progress_snapshot["current_symbol"])
        last_progress_completed = -1
        last_progress_failed_symbols_total = 0
        last_progress_failed_sample_signature: tuple[tuple[str, str, str], ...] = ()
        last_progress_write_monotonic = 0.0
        progress_write_interval_sec = 5.0

        def _record_failed_symbol(symbol: str) -> None:
            normalized_symbol = _normalize_a_share_symbol(symbol) or str(symbol).strip()
            if not normalized_symbol or normalized_symbol in failed_symbol_seen:
                return
            failed_symbol_seen.add(normalized_symbol)
            failed_symbols.append(normalized_symbol)

        def _daily_progress_summary() -> dict[str, object]:
            target_trade_date = str(report.get("target_trade_date", "")).strip()
            default_hard_timeout_sec, _ = self._resolve_market_warehouse_daily_symbol_hard_timeout_sec()
            return {
                "ok": daily_ok,
                "skipped": daily_skipped,
                "failed": daily_failed,
                "mode_counts": dict(daily_mode_counts),
                "target_trade_date": target_trade_date,
                "symbol_hard_timeout_sec": _as_float(
                    report.get("effective_daily_symbol_hard_timeout_sec"),
                    default=default_hard_timeout_sec,
                ),
                "symbol_hard_timeout_profile": str(
                    report.get("effective_daily_symbol_hard_timeout_profile", "default")
                ),
                "online_primary": str(service._config.market_warehouse.online_daily_primary),
                "online_backup": str(service._config.market_warehouse.online_daily_backup),
            }

        def _intraday_progress_summary() -> dict[str, object]:
            target_trade_date = str(report.get("target_trade_date", "")).strip()
            return {
                "enabled": bool(service._config.market_warehouse.intraday_sync_enabled),
                "intervals": list(intervals),
                "symbols_targeted": len(intraday_symbol_list),
                "ok": intraday_ok,
                "skipped": intraday_skipped,
                "failed": intraday_failed,
                "target_trade_date": target_trade_date,
            }

        def _publish_progress(
            *,
            status: str | None = None,
            phase: str | None = None,
            current_symbol: str = "",
            current_stage: str = "",
            force_write: bool = False,
        ) -> None:
            nonlocal last_progress_completed
            nonlocal last_progress_phase
            nonlocal last_progress_stage
            nonlocal last_progress_status
            nonlocal last_progress_symbol
            nonlocal last_progress_failed_symbols_total
            nonlocal last_progress_failed_sample_signature
            nonlocal last_progress_write_monotonic
            if lock_path is not None and lock_owner_token:
                self._touch_market_warehouse_sync_lock(
                    lock_path=lock_path,
                    owner_token=lock_owner_token,
                )
            if status is not None:
                progress_snapshot["status"] = status
            if phase is not None:
                progress_snapshot["phase"] = phase
            total = max(0, _as_int(report.get("symbols_total", 0), default=0))
            completed = min(symbols_completed, total) if total > 0 else 0
            progress_snapshot["updated_at"] = datetime.now().isoformat()
            progress_snapshot["current_symbol"] = current_symbol
            progress_snapshot["current_stage"] = current_stage
            progress_snapshot["reason"] = str(report.get("reason", ""))
            progress_snapshot["db_path"] = str(report.get("db_path", ""))
            progress_snapshot["package_root"] = str(report.get("package_root", ""))
            progress_snapshot["bootstrap_source_root"] = str(
                report.get("bootstrap_source_root", "")
            )
            progress_snapshot["target_trade_date"] = str(report.get("target_trade_date", ""))
            progress_snapshot["symbols_total"] = total
            progress_snapshot["symbols_completed"] = completed
            progress_snapshot["progress_ratio"] = round(completed / total, 4) if total > 0 else 0.0
            progress_snapshot["bootstrap"] = report.get("bootstrap", {})
            progress_snapshot["daily_sync"] = _daily_progress_summary()
            progress_snapshot["intraday_sync"] = _intraday_progress_summary()
            progress_snapshot["failed_samples"] = failed_samples[:10]
            progress_snapshot["failed_symbols_total"] = len(failed_symbols)
            progress_snapshot["progress_write_every_symbols"] = progress_write_every
            progress_snapshot["progress_write_interval_sec"] = progress_write_interval_sec
            snapshot_status = str(progress_snapshot.get("status", ""))
            snapshot_phase = str(progress_snapshot.get("phase", ""))
            failed_sample_signature = tuple(
                (
                    str(sample.get("symbol", "")),
                    str(sample.get("stage", "")),
                    str(sample.get("reason", "")),
                )
                for sample in failed_samples[:10]
            )
            now_monotonic = time.monotonic()
            should_write = force_write
            if not should_write and (
                snapshot_status != last_progress_status
                or snapshot_phase != last_progress_phase
                or current_stage != last_progress_stage
                or current_symbol != last_progress_symbol
                or len(failed_symbols) != last_progress_failed_symbols_total
                or failed_sample_signature != last_progress_failed_sample_signature
            ):
                should_write = True
            if not should_write and completed != last_progress_completed:
                should_write = (
                    completed <= 3
                    or total <= 0
                    or completed >= total
                    or completed % progress_write_every == 0
                )
            if (
                not should_write
                and last_progress_write_monotonic > 0.0
                and (now_monotonic - last_progress_write_monotonic) >= progress_write_interval_sec
            ):
                should_write = True
            if not should_write:
                return
            service._store_market_warehouse_progress(dict(progress_snapshot))
            last_progress_status = snapshot_status
            last_progress_phase = snapshot_phase
            last_progress_stage = current_stage
            last_progress_symbol = current_symbol
            last_progress_completed = completed
            last_progress_failed_symbols_total = len(failed_symbols)
            last_progress_failed_sample_signature = failed_sample_signature
            last_progress_write_monotonic = now_monotonic

        if not bool(service._config.market_warehouse.enabled) and not force:
            report["reason"] = "market_warehouse_disabled"
            service._store_market_warehouse_report(report)
            return report

        active_lock: dict[str, object] = {}
        preacquired_lock = bool(scheduler_lock_path is not None and scheduler_lock_owner_token.strip())
        if preacquired_lock:
            lock_path = scheduler_lock_path
            lock_owner_token = scheduler_lock_owner_token.strip()
            active_lock = self._read_market_warehouse_sync_lock(lock_path)
            if str(active_lock.get("owner_token", "")).strip() != lock_owner_token:
                report["status"] = "failed"
                report["reason"] = "market_warehouse_sync_preacquired_lock_lost"
                if active_lock:
                    report["active_lock"] = active_lock
                service._record_audit_event(
                    event_type="market_warehouse_sync_rejected",
                    trace_id=source_trace_id,
                    level="warn",
                    payload={
                        "reason": report["reason"],
                        "active_lock": active_lock,
                    },
                )
                service._store_market_warehouse_report(report)
                service._notify_market_warehouse_if_needed(
                    report=report,
                    notify_enabled=notify_enabled,
                )
                return report
        else:
            lock_path, lock_owner_token, active_lock = self._acquire_market_warehouse_sync_lock(
                timestamp=now,
                source_trace_id=source_trace_id,
                force=force,
            )
            if lock_path is None:
                report["reason"] = "market_warehouse_sync_in_progress"
                if active_lock:
                    report["active_lock"] = active_lock
                    report["lock_stale_after_sec"] = active_lock.get("stale_after_sec", 0)
                active_progress = service.latest_market_warehouse_progress()
                if (
                    isinstance(active_progress, dict)
                    and isinstance(active_lock, dict)
                    and str(active_progress.get("trace_id", "")).strip()
                    == str(active_lock.get("trace_id", "")).strip()
                ):
                    report["active_progress"] = active_progress
                service._record_audit_event(
                    event_type="market_warehouse_sync_rejected",
                    trace_id=source_trace_id,
                    level="info",
                    payload={
                        "reason": report["reason"],
                        "active_lock": active_lock,
                    },
                )
                return report

        heartbeat_stop = Event()
        heartbeat_interval_sec = self._resolve_market_warehouse_sync_lock_heartbeat_interval_sec()

        def _heartbeat_lock() -> None:
            while not heartbeat_stop.wait(timeout=heartbeat_interval_sec):
                if lock_path is not None and lock_owner_token:
                    self._touch_market_warehouse_sync_lock(
                        lock_path=lock_path,
                        owner_token=lock_owner_token,
                    )

        heartbeat_thread = Thread(
            target=_heartbeat_lock,
            name="market-warehouse-lock-heartbeat",
            daemon=True,
        )
        self._touch_market_warehouse_sync_lock(lock_path=lock_path, owner_token=lock_owner_token)
        heartbeat_thread.start()
        lock_cleanup_done = False

        def _release_run_lock() -> None:
            nonlocal lock_cleanup_done
            if lock_cleanup_done:
                return
            heartbeat_stop.set()
            if heartbeat_thread.is_alive():
                heartbeat_thread.join(timeout=max(1.0, heartbeat_interval_sec))
            if lock_path is not None and lock_owner_token:
                self._release_market_warehouse_sync_lock(
                    lock_path=lock_path,
                    owner_token=lock_owner_token,
                )
            lock_cleanup_done = True

        try:
            _publish_progress(force_write=True)
            warehouse = service._market_warehouse()
            package_root = warehouse.package_root
            bootstrap_source_root = service._resolve_market_warehouse_bootstrap_source_root()
            report["db_path"] = str(warehouse.db_path)
            report["package_root"] = str(package_root)
            report["bootstrap_source_root"] = str(bootstrap_source_root)

            warehouse_has_daily_data = warehouse.has_daily_data()
            bootstrap_report: dict[str, object] = {
                "status": "skipped",
                "reason": "already_initialized",
            }
            if bool(service._config.market_warehouse.bootstrap_on_first_sync) and (
                force or not warehouse_has_daily_data
            ):
                offline_bootstrap_allowed, offline_bootstrap_reason = (
                    service._can_bootstrap_market_warehouse_from_offline(bootstrap_source_root)
                )
                if offline_bootstrap_allowed:
                    bootstrap_report = warehouse.bootstrap_from_offline_package(
                        source_root=bootstrap_source_root
                    )
                else:
                    bootstrap_report = {
                        "status": "skipped",
                        "reason": offline_bootstrap_reason,
                        "mode": "online_bootstrap",
                    }
            report["bootstrap"] = bootstrap_report
            _publish_progress(phase="bootstrap", current_stage="bootstrap", force_write=True)

            max_symbols = max(0, _as_int(service._config.market_warehouse.max_symbols, default=0))
            target_trade_date = service._resolve_market_warehouse_target_trade_date(now=now)
            retry_source_report: dict[str, object] | None = None
            symbol_source = "full_universe"
            resolved_symbols: list[str] | None = requested_symbols if requested_symbols else None
            if retry_failed_only:
                retry_source_report = self._resolve_market_warehouse_retry_source_report(
                    trace_id=retry_report_trace_id,
                )
                if retry_source_report is None:
                    report["reason"] = "retry_source_report_not_found"
                    _publish_progress(
                        status="skipped",
                        phase="retry_source_missing",
                        current_stage="retry_failed_only",
                        force_write=True,
                    )
                    _release_run_lock()
                    service._store_market_warehouse_report(report)
                    return report
                retry_failed_symbols = self._extract_market_warehouse_failed_symbols(
                    retry_source_report
                )
                retry_failed_total, retry_source_complete = (
                    self._resolve_market_warehouse_retry_failed_total(
                        retry_source_report,
                        extracted_symbols=retry_failed_symbols,
                    )
                )
                report["retry_source_trace_id"] = str(
                    retry_source_report.get("trace_id", "")
                ).strip()
                report["retry_source_timestamp"] = str(
                    retry_source_report.get("timestamp", "")
                ).strip()
                report["retry_source_status"] = str(
                    retry_source_report.get("status", "")
                ).strip()
                report["retry_source_failed_symbols_total"] = retry_failed_total
                report["retry_source_complete"] = retry_source_complete
                report["retry_source_available_symbols_total"] = len(retry_failed_symbols)
                if not requested_symbols and not retry_source_complete:
                    report["reason"] = "retry_source_failed_symbols_incomplete"
                    _publish_progress(
                        status="skipped",
                        phase="retry_source_incomplete",
                        current_stage="retry_failed_only",
                        force_write=True,
                    )
                    _release_run_lock()
                    service._store_market_warehouse_report(report)
                    return report
                retry_failed_symbol_set = set(retry_failed_symbols)
                if requested_symbols:
                    resolved_symbols = [
                        symbol
                        for symbol in requested_symbols
                        if not retry_failed_symbol_set or symbol in retry_failed_symbol_set
                    ]
                else:
                    resolved_symbols = retry_failed_symbols
                report["retry_symbols_total"] = len(resolved_symbols)
                symbol_source = "retry_failed_only"
                if not resolved_symbols:
                    report["reason"] = "no_failed_symbols_to_retry"
                    _publish_progress(
                        status="skipped",
                        phase="retry_empty",
                        current_stage="retry_failed_only",
                        force_write=True,
                    )
                    _release_run_lock()
                    service._store_market_warehouse_report(report)
                    return report
            elif resolved_symbols is not None:
                symbol_source = "explicit_symbols"
            symbol_list = (
                list(resolved_symbols)
                if resolved_symbols is not None
                else service._select_market_warehouse_symbols(
                    warehouse=warehouse,
                    package_root=package_root,
                    max_symbols=max_symbols,
                )
            )
            report["symbol_source"] = symbol_source
            report["symbols_total"] = len(symbol_list)
            report["target_trade_date"] = target_trade_date.isoformat()
            (
                effective_daily_symbol_hard_timeout_sec,
                effective_daily_symbol_hard_timeout_profile,
            ) = self._resolve_market_warehouse_daily_symbol_hard_timeout_sec(
                symbol_source=symbol_source
            )
            report["effective_daily_symbol_hard_timeout_sec"] = (
                effective_daily_symbol_hard_timeout_sec
            )
            report["effective_daily_symbol_hard_timeout_profile"] = (
                effective_daily_symbol_hard_timeout_profile
            )
            if not symbol_list:
                report["reason"] = "empty_symbol_universe"
                _publish_progress(status="skipped", phase="empty_universe", force_write=True)
                _release_run_lock()
                service._store_market_warehouse_report(report)
                return report

            online_provider = service._build_market_warehouse_online_provider()
            progress_write_every = max(1, min(50, len(symbol_list) // 20 or 1))
            intervals = [
                str(item).strip().lower()
                for item in service._config.market_warehouse.intraday_intervals
                if str(item).strip().lower() in {"1m", "5m"}
            ]
            intraday_symbol_list = service._resolve_market_warehouse_intraday_symbols(
                symbol_list=symbol_list,
            )
            intraday_symbol_set = set(intraday_symbol_list)
            latest_daily_map = warehouse.latest_daily_dates(symbols=symbol_list)
            latest_intraday_maps = {
                interval: warehouse.latest_intraday_dates(
                    interval=interval,
                    symbols=intraday_symbol_list,
                )
                for interval in intervals
            }
            _publish_progress(phase="syncing", current_stage="daily", force_write=True)

            for index, symbol in enumerate(symbol_list, start=1):
                current_stage = "daily"
                _publish_progress(
                    phase="syncing", current_symbol=symbol, current_stage=current_stage
                )
                try:
                    daily_result = service._sync_market_warehouse_daily_symbol(
                        warehouse=warehouse,
                        online_provider=online_provider,
                        symbol=symbol,
                        force=force,
                        target_end_date=target_trade_date,
                        latest_daily=latest_daily_map.get(symbol),
                        hard_timeout_sec=effective_daily_symbol_hard_timeout_sec,
                    )
                    if str(daily_result.get("status", "")) == "ok":
                        daily_ok += 1
                        sync_mode = str(daily_result.get("mode", "")).strip().lower()
                        if sync_mode in daily_mode_counts:
                            daily_mode_counts[sync_mode] += 1
                    else:
                        daily_skipped += 1
                except Exception as exc:
                    daily_failed += 1
                    _record_failed_symbol(symbol)
                    if len(failed_samples) < 20:
                        failed_samples.append(
                            {
                                "symbol": symbol,
                                "stage": "daily",
                                "reason": f"{exc.__class__.__name__}:{exc}",
                            }
                        )
                    symbols_completed = index
                    _publish_progress(
                        phase="syncing",
                        current_symbol=symbol,
                        current_stage=current_stage,
                        force_write=index == len(symbol_list),
                    )
                    continue

                if (
                    bool(service._config.market_warehouse.intraday_sync_enabled)
                    and symbol in intraday_symbol_set
                ):
                    for interval in intervals:
                        current_stage = f"intraday_{interval}"
                        try:
                            intraday_result = service._sync_market_warehouse_intraday_symbol(
                                warehouse=warehouse,
                                symbol=symbol,
                                interval=interval,
                                force=force,
                                target_end_date=target_trade_date,
                                existing_latest=latest_intraday_maps.get(interval, {}).get(symbol),
                            )
                            if str(intraday_result.get("status", "")) == "ok":
                                intraday_ok += 1
                            else:
                                intraday_skipped += 1
                        except Exception as exc:
                            intraday_failed += 1
                            _record_failed_symbol(symbol)
                            if len(failed_samples) < 20:
                                failed_samples.append(
                                    {
                                        "symbol": symbol,
                                        "stage": f"intraday_{interval}",
                                        "reason": f"{exc.__class__.__name__}:{exc}",
                                    }
                                )
                symbols_completed = index
                _publish_progress(
                    phase="syncing",
                    current_symbol=symbol,
                    current_stage=current_stage,
                    force_write=index == len(symbol_list),
                )

            report["daily_sync"] = {
                "ok": daily_ok,
                "skipped": daily_skipped,
                "failed": daily_failed,
                "online_bootstrap_lookback_days": max(
                    max(
                        40,
                        _as_int(service._config.market_warehouse.daily_lookback_days, default=120),
                    ),
                    _as_int(
                        service._config.market_warehouse.online_bootstrap_lookback_days, default=750
                    ),
                ),
                "base_lookback_days": max(
                    40,
                    _as_int(service._config.market_warehouse.daily_lookback_days, default=120),
                ),
                "mode_counts": daily_mode_counts,
                "incremental_enabled": bool(
                    service._config.market_warehouse.daily_incremental_enabled
                ),
                "incremental_cushion_days": max(
                    2,
                    _as_int(
                        service._config.market_warehouse.daily_incremental_cushion_days,
                        default=5,
                    ),
                ),
                "symbol_hard_timeout_sec": effective_daily_symbol_hard_timeout_sec,
                "symbol_hard_timeout_profile": effective_daily_symbol_hard_timeout_profile,
                "target_trade_date": target_trade_date.isoformat(),
                "online_primary": str(service._config.market_warehouse.online_daily_primary),
                "online_backup": str(service._config.market_warehouse.online_daily_backup),
            }
            report["intraday_sync"] = {
                "enabled": bool(service._config.market_warehouse.intraday_sync_enabled),
                "intervals": intervals,
                "scope": str(service._config.market_warehouse.intraday_sync_scope),
                "symbols_targeted": len(intraday_symbol_list),
                "symbols_targeted_sample": intraday_symbol_list[:10],
                "ok": intraday_ok,
                "skipped": intraday_skipped,
                "failed": intraday_failed,
                "target_trade_date": target_trade_date.isoformat(),
            }
            report["failed_samples"] = failed_samples
            report["failed_symbols"] = failed_symbols
            report["failed_symbols_total"] = len(failed_symbols)
            package_materialization: dict[str, object] = {
                "status": "skipped",
                "reason": "package_already_materialized",
            }
            if not warehouse.has_materialized_package():
                package_materialization = warehouse.materialize_runtime_package()
            report["package_materialization"] = package_materialization
            report["manifest_refresh"] = warehouse.refresh_package_manifests()
            report["cache_refresh"] = service._invalidate_market_data_cache()
            report["background_data"] = warehouse.background_data_quality_snapshot()
            report["symbols_completed"] = symbols_completed
            report["progress_write_every_symbols"] = progress_write_every
            if daily_failed == 0 and intraday_failed == 0:
                report["status"] = "ok"
            elif daily_ok > 0 or intraday_ok > 0 or daily_skipped > 0 or intraday_skipped > 0:
                report["status"] = "partial"
            else:
                report["status"] = "failed"
                report["reason"] = "all_symbols_failed"
            _publish_progress(
                status=str(report.get("status", "ok")),
                phase="completed",
                current_stage="finalize",
                force_write=True,
            )
        except Exception as exc:
            report["status"] = "failed"
            report["reason"] = str(exc)
            report["error_type"] = exc.__class__.__name__
            _publish_progress(
                status="failed",
                phase="failed",
                current_stage="exception",
                force_write=True,
            )

        _release_run_lock()
        service._store_market_warehouse_report(report)
        service._record_audit_event(
            event_type="market_warehouse_sync",
            trace_id=source_trace_id,
            level="warn" if str(report.get("status", "")) == "failed" else "info",
            payload={
                "status": report.get("status", ""),
                "reason": report.get("reason", ""),
                "db_path": report.get("db_path", ""),
                "package_root": report.get("package_root", ""),
                "daily_sync": report.get("daily_sync", {}),
                "intraday_sync": report.get("intraday_sync", {}),
            },
        )
        service._notify_market_warehouse_if_needed(
            report=report,
            notify_enabled=notify_enabled,
        )
        return report

    def _notify_market_warehouse_if_needed(
        self,
        *,
        report: dict[str, object],
        notify_enabled: bool | None = None,
    ) -> None:
        service = self._service
        status = str(report.get("status", "")).strip().lower()
        if status not in {"ok", "failed", "partial"}:
            return
        should_notify = bool(notify_enabled) if notify_enabled is not None else False
        if notify_enabled is None:
            if status == "ok":
                should_notify = bool(service._config.market_warehouse.notify_on_success)
            elif status in {"failed", "partial"}:
                should_notify = bool(service._config.market_warehouse.notify_on_failure)
        if not should_notify:
            return

        daily_sync = report.get("daily_sync", {})
        if not isinstance(daily_sync, dict):
            daily_sync = {}
        intraday_sync = report.get("intraday_sync", {})
        if not isinstance(intraday_sync, dict):
            intraday_sync = {}

        if status == "ok":
            title = _push_title(priority="P2", category="数据", summary="基础数据库已增量更新")
            content = (
                f"事件：基础数据库同步完成；"
                f"影响：日线增量成功 {daily_sync.get('ok', 0)} 只，"
                f"分钟摘要成功 {intraday_sync.get('ok', 0)} 项；"
                f"建议动作：无需处理，后续扫描与自学习已读取新数据。"
            )
            level = "info"
        else:
            title = _push_title(priority="P1", category="运维", summary="基础数据库同步异常")
            content = (
                f"事件：基础数据库同步未完全成功；"
                f"影响：日线失败 {daily_sync.get('failed', 0)} 只，"
                f"分钟摘要失败 {intraday_sync.get('failed', 0)} 项；"
                f"建议动作：检查在线数据源可用性，再重跑仓库同步。"
            )
            level = "warn"
        service.notify(
            title=title,
            content=content,
            level=level,
            trace_id=str(report.get("trace_id", "")),
        )


def _parse_iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _filter_frame_by_trade_date(
    frame: pd.DataFrame,
    *,
    max_date: date | None = None,
    min_date: date | None = None,
) -> pd.DataFrame:
    if frame.empty:
        return frame
    filtered = frame.copy()
    datetime_index = pd.DatetimeIndex(pd.to_datetime(filtered.index, errors="coerce"))
    filtered.index = datetime_index
    valid_mask = [not pd.isna(item) for item in datetime_index]
    filtered = filtered.loc[valid_mask]
    if filtered.empty:
        return filtered
    datetime_index = pd.DatetimeIndex(filtered.index)
    if max_date is not None:
        filtered = filtered.loc[datetime_index <= pd.Timestamp(max_date)]
        if filtered.empty:
            return filtered
        datetime_index = pd.DatetimeIndex(filtered.index)
    if min_date is not None:
        filtered = filtered.loc[datetime_index >= pd.Timestamp(min_date)]
    return filtered


def _push_title(priority: str, category: str, summary: str) -> str:
    badge_map = {
        "P0": "【紧急】",
        "P1": "【重要】",
        "P2": "【日常】",
        "P3": "【参考】",
    }
    badge = badge_map.get(priority.strip().upper(), "【日常】")
    category_text = category.strip() or "通知"
    summary_text = summary.strip() or "-"
    return f"{badge}【{category_text}】{summary_text}"


def _as_float(value: object, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _as_int(value: object, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return default
    return default


def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [text for item in value if (text := str(item).strip())]


def _normalize_market_warehouse_symbols(value: object) -> list[str]:
    return _dedupe_preserve_order(
        [
            normalized
            for item in _string_list(value)
            if (normalized := _normalize_a_share_symbol(item))
            and _is_supported_market_warehouse_symbol(normalized)
        ]
    )


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        normalized = item.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _last_friday(current: date) -> date:
    weekday = current.weekday()
    offset = (weekday - 4) % 7
    return current - timedelta(days=offset)


def _normalize_a_share_symbol(value: object) -> str:
    text = str(value).strip().upper()
    if not text:
        return ""
    primary = text.split(".", maxsplit=1)[0]
    digits = "".join(ch for ch in primary if ch.isdigit())
    if len(digits) != 6:
        digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) > 6:
        digits = digits[-6:]
    if len(digits) != 6:
        return ""
    if digits[0] not in {"0", "3", "4", "6", "8"}:
        return ""
    return digits


def _is_supported_market_warehouse_symbol(value: object) -> bool:
    normalized = _normalize_a_share_symbol(value)
    if not normalized:
        return False
    if normalized.startswith(("810", "899")):
        return False
    return True
