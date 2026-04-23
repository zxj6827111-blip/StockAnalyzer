"""Evolution core orchestration, loaders, and persistence extracted from the runtime service."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pandas as pd

from stock_analyzer.evolution.modules.m7_news_loader import load_m7_news_records
from stock_analyzer.evolution.specs import build_spec_hash_bundle
from stock_analyzer.labels.soup import build_soup_labels

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService


_M5_INTRADAY_1M_COLUMNS = (
    "session_return",
    "realized_vol",
    "vwap_gap",
    "last30_return",
    "tail30_volume_share",
    "close_position",
)
_M5_INTRADAY_5M_COLUMNS = (
    "session_return",
    "realized_vol",
    "am_pm_diff",
    "close_vwap_stability",
    "intraday_pullback_ratio",
    "close_position",
)


class RuntimeEvolutionCoreService:
    """Delegated evolution runtime, loader, and report workflows."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service

    def _evolution_runtime_mode(self) -> str:
        service = self._service
        return str(service._config.app.mode).strip().lower()

    def _evolution_dry_run_policy(self) -> str:
        service = self._service
        policy = str(service._config.evolution.dry_run_policy).strip().lower()
        if policy in {"fixed", "auto"}:
            return policy
        return "fixed"

    def _evolution_dry_run_live_modes(self) -> set[str]:
        service = self._service
        modes = {
            str(item).strip().lower()
            for item in service._config.evolution.dry_run_live_modes
            if str(item).strip()
        }
        if not modes:
            modes = {"production"}
        return modes

    def _resolve_evolution_dry_run(
        self,
        requested: bool | None = None,
    ) -> tuple[bool, str]:
        service = self._service
        if requested is not None:
            return bool(requested), "request_override"

        policy = self._evolution_dry_run_policy()
        if policy == "fixed":
            return bool(service._config.evolution.dry_run), "config_fixed"

        runtime_mode = self._evolution_runtime_mode()
        if runtime_mode in self._evolution_dry_run_live_modes():
            return False, f"policy_auto_live:{runtime_mode}"
        return True, f"policy_auto_safe:{runtime_mode or 'unknown'}"

    def run_evolution_offhours(
        self,
        symbols: list[str] | None = None,
        timestamp: datetime | None = None,
        dry_run: bool | None = None,
        source_trace_id: str = "",
        records: list[dict[str, object]] | None = None,
        refresh_tdx_before_run: bool | None = None,
    ) -> dict[str, object]:
        """Run one off-hours evolution cycle."""
        service = self._service
        resolved_dry_run, dry_run_resolved_by = self._resolve_evolution_dry_run(requested=dry_run)
        if service._bootstrap_runtime_blocked():
            blocked = {
                "timestamp": (timestamp or datetime.now()).isoformat(),
                "status": "blocked_bootstrap_required",
                "dry_run": resolved_dry_run,
                "dry_run_resolved_by": dry_run_resolved_by,
                "source_trace_id": source_trace_id,
                "bootstrap": service.training_bootstrap_status(),
            }
            service._record_audit_event(
                event_type="evolution_blocked_bootstrap",
                trace_id=source_trace_id,
                level="warn",
                payload={"bootstrap": blocked["bootstrap"]},
            )
            return blocked
        run_now = timestamp or datetime.now()
        if symbols is not None:
            pre_sync_symbols = [str(item).strip() for item in symbols if str(item).strip()]
        elif records is not None:
            pre_sync_symbols = [
                str(item.get("symbol", "")).strip()
                for item in records
                if isinstance(item, dict) and str(item.get("symbol", "")).strip()
            ]
        else:
            pre_sync_symbols = []
        market_warehouse_report: dict[str, object] = {"status": "skipped", "reason": ""}
        refresh_market_warehouse, refresh_market_warehouse_reason = (
            service._resolve_market_warehouse_auto_refresh(requested=refresh_tdx_before_run)
        )
        tdx_sync_report: dict[str, object] = {"status": "skipped", "reason": ""}
        refresh_tdx_reason = "market_warehouse_preferred"
        if refresh_market_warehouse:
            market_warehouse_report = service.run_market_warehouse_sync(
                timestamp=run_now,
                notify_enabled=False,
                force=False,
                source_trace_id=source_trace_id or "evolution-pre-sync",
                symbols=pre_sync_symbols or None,
            )
            if str(market_warehouse_report.get("status", "")) == "failed" and bool(
                service._config.market_warehouse.block_evolution_on_failure
            ):
                blocked = {
                    "timestamp": run_now.isoformat(),
                    "status": "blocked_market_warehouse_sync_failed",
                    "dry_run": resolved_dry_run,
                    "dry_run_resolved_by": dry_run_resolved_by,
                    "source_trace_id": source_trace_id,
                    "market_warehouse_sync": market_warehouse_report,
                    "market_warehouse_sync_auto_refresh_reason": refresh_market_warehouse_reason,
                }
                service._record_audit_event(
                    event_type="evolution_blocked_market_warehouse_sync_failed",
                    trace_id=source_trace_id,
                    level="warn",
                    payload={"market_warehouse_sync": market_warehouse_report},
                )
                return blocked
        else:
            market_warehouse_report = {
                "status": "skipped",
                "reason": refresh_market_warehouse_reason,
            }
            refresh_tdx, refresh_tdx_reason = service._resolve_tdx_sync_auto_refresh(
                requested=refresh_tdx_before_run
            )
            if refresh_tdx:
                tdx_sync_report = service.run_tdx_offline_sync(
                    timestamp=run_now,
                    notify_enabled=False,
                    force=False,
                    source_trace_id=source_trace_id or "evolution-pre-sync",
                )
                if str(tdx_sync_report.get("status", "")) == "failed" and bool(
                    service._config.tdx_sync.block_evolution_on_failure
                ):
                    blocked = {
                        "timestamp": run_now.isoformat(),
                        "status": "blocked_tdx_sync_failed",
                        "dry_run": resolved_dry_run,
                        "dry_run_resolved_by": dry_run_resolved_by,
                        "source_trace_id": source_trace_id,
                        "tdx_sync": tdx_sync_report,
                        "tdx_sync_auto_refresh_reason": refresh_tdx_reason,
                    }
                    service._record_audit_event(
                        event_type="evolution_blocked_tdx_sync_failed",
                        trace_id=source_trace_id,
                        level="warn",
                        payload={"tdx_sync": tdx_sync_report},
                    )
                    return blocked
            else:
                tdx_sync_report = {"status": "skipped", "reason": refresh_tdx_reason}
        if symbols is not None:
            symbol_list = [str(item).strip() for item in symbols if str(item).strip()]
        elif records is not None:
            symbol_list = [
                str(item.get("symbol", "")).strip()
                for item in records
                if isinstance(item, dict) and str(item.get("symbol", "")).strip()
            ]
        else:
            symbol_list = [
                str(item).strip()
                for item in service._state.watchlist
                if str(item).strip()
            ]
            if not symbol_list:
                universe = service._resolve_symbol_universe(max_symbols=800)
                symbol_list = _string_list(universe.get("symbols", []))
        loader_inputs: dict[str, object] = {}
        if records is not None:
            m9_records = [dict(item) for item in records]
        else:
            m9_records = self._build_evolution_m9_records(symbol_list)
            if service._config.evolution.auto_generate_loader_inputs:
                loader_inputs = self._prepare_evolution_loader_inputs(
                    symbols=symbol_list,
                    now=run_now,
                )
        if not symbol_list:
            symbol_list = [
                str(item.get("symbol", "")).strip()
                for item in m9_records
                if isinstance(item, dict) and str(item.get("symbol", "")).strip()
            ]
        universe_snapshot = self._create_universe_snapshot(
            symbols=symbol_list,
            decision_time=run_now,
        )
        m9_records = self._attach_universe_snapshot_metadata(
            records=m9_records,
            universe_snapshot_id=str(universe_snapshot["universe_snapshot_id"]),
            universe_spec_hash=str(universe_snapshot["universe_spec_hash"]),
        )
        report = _as_dict_object(
            service._evolution_orchestrator.run(
                records=m9_records,
                now=timestamp,
                dry_run=resolved_dry_run,
                source_trace_id=source_trace_id,
            )
        )
        report["dry_run_resolved_by"] = dry_run_resolved_by
        report["universe_snapshot"] = universe_snapshot
        report["universe_snapshot_id"] = universe_snapshot["universe_snapshot_id"]
        report["universe_spec_hash"] = universe_snapshot["universe_spec_hash"]
        report["market_warehouse_sync"] = market_warehouse_report
        report["market_warehouse_sync_auto_refresh_reason"] = refresh_market_warehouse_reason
        report["tdx_sync"] = tdx_sync_report
        report["tdx_sync_auto_refresh_reason"] = refresh_tdx_reason
        if loader_inputs:
            report["loader_inputs"] = loader_inputs
        service._last_evolution_report = report
        service._evolution_history.append(report)
        history_limit = max(1, service._config.evolution.history_limit)
        if len(service._evolution_history) > history_limit:
            overflow = len(service._evolution_history) - history_limit
            if overflow > 0:
                service._evolution_history = service._evolution_history[overflow:]
        self._persist_evolution_report(report)
        service._record_audit_event(
            event_type="evolution_offhours_run",
            trace_id=source_trace_id,
            payload={
                "symbols": symbol_list,
                "dry_run": report.get("dry_run", True),
                "dry_run_resolved_by": dry_run_resolved_by,
                "m9": report.get("m9", {}),
                "proposal": report.get("proposal", {}),
                "loader_inputs": loader_inputs,
            },
        )
        return report

    def run_evolution_drill(
        self,
        timestamp: datetime | None = None,
        source_trace_id: str = "evolution-drill",
    ) -> dict[str, object]:
        """Run deterministic end-to-end drill for evolution flow."""
        service = self._service
        report = _as_dict_object(
            service._evolution_orchestrator.run_drill(
                now=timestamp,
                source_trace_id=source_trace_id,
            )
        )
        service._last_evolution_report = report
        service._evolution_history.append(report)
        history_limit = max(1, service._config.evolution.history_limit)
        if len(service._evolution_history) > history_limit:
            overflow = len(service._evolution_history) - history_limit
            if overflow > 0:
                service._evolution_history = service._evolution_history[overflow:]
        self._persist_evolution_report(report)
        service._record_audit_event(
            event_type="evolution_offhours_drill",
            trace_id=source_trace_id,
            payload={"dry_run": report.get("dry_run", True)},
        )
        return report

    def _create_universe_snapshot(
        self,
        *,
        symbols: list[str],
        decision_time: datetime,
    ) -> dict[str, object]:
        service = self._service
        normalized_symbols = [
            item for item in (_normalize_a_share_symbol(symbol) for symbol in symbols) if item
        ]
        frozen_symbols = _dedupe_preserve_order(normalized_symbols)
        spec_bundle = build_spec_hash_bundle(config=service._config.evolution)
        universe_spec_hash = str(spec_bundle["universe_spec_hash"])
        asof_time = decision_time.isoformat()
        source = f"{decision_time.date().isoformat()}|{universe_spec_hash}|" + ",".join(
            frozen_symbols
        )
        snapshot_id = (
            f"univ-{decision_time.strftime('%Y%m%d')}-"
            f"{hashlib.sha256(source.encode('utf-8')).hexdigest()[:12]}"
        )
        payload = {
            "universe_snapshot_id": snapshot_id,
            "asof_time": asof_time,
            "count": len(frozen_symbols),
            "symbols": frozen_symbols,
            "universe_ruleset_id": service._config.evolution.universe_spec.universe_ruleset_id,
            "universe_spec_hash": universe_spec_hash,
        }
        output_path = self._resolve_evolution_path(
            f"artifacts/universe/snapshots/{snapshot_id}.json"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            "universe_snapshot_id": snapshot_id,
            "asof_time": asof_time,
            "count": len(frozen_symbols),
            "snapshot_uri": self._to_evolution_relative(output_path),
            "snapshot_path": str(output_path),
            "universe_ruleset_id": service._config.evolution.universe_spec.universe_ruleset_id,
            "universe_spec_hash": universe_spec_hash,
        }

    def _attach_universe_snapshot_metadata(
        self,
        *,
        records: list[dict[str, object]],
        universe_snapshot_id: str,
        universe_spec_hash: str,
    ) -> list[dict[str, object]]:
        enriched: list[dict[str, object]] = []
        for item in records:
            row = dict(item)
            row["universe_snapshot_id"] = universe_snapshot_id
            row["universe_spec_hash"] = universe_spec_hash
            enriched.append(row)
        return enriched

    def run_evolution_m3_maintenance(
        self,
        timestamp: datetime | None = None,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        """Run M3 pending snapshot maintenance."""
        service = self._service
        report = _as_dict_object(service._evolution_orchestrator.run_m3_maintenance(now=timestamp))
        service._record_audit_event(
            event_type="evolution_m3_maintenance",
            trace_id=source_trace_id,
            payload={"purged_count": report.get("purged_count", 0)},
        )
        return report

    def run_evolution_m3_search(
        self,
        vector: list[float],
        top_k: int = 5,
        source_trace_id: str = "",
    ) -> dict[str, object]:
        """Run M3 nearest-neighbor query."""
        service = self._service
        capped_top_k = max(1, top_k)
        report = _as_dict_object(
            service._evolution_orchestrator.m3_search(
                query_vector=vector,
                top_k=capped_top_k,
            )
        )
        raw_indices = report.get("indices", [])
        hit_count = len(raw_indices) if isinstance(raw_indices, list) else 0
        service._record_audit_event(
            event_type="evolution_m3_search",
            trace_id=source_trace_id,
            payload={"top_k": capped_top_k, "hit_count": hit_count},
        )
        return report

    def run_evolution_m8_suggest(
        self,
        symbols: list[str] | None = None,
        top_k: int | None = None,
        timestamp: datetime | None = None,
        source_trace_id: str = "",
        records: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        """Run M8 suggestions directly backed by M3 search."""
        service = self._service
        if symbols is not None:
            symbol_list = [str(item).strip() for item in symbols if str(item).strip()]
        else:
            symbol_list = [
                str(item).strip()
                for item in service._state.watchlist
                if str(item).strip()
            ]
            if not symbol_list:
                universe = service._resolve_symbol_universe(max_symbols=800)
                symbol_list = _string_list(universe.get("symbols", []))
        m8_records = (
            records if records is not None else self._build_evolution_m9_records(symbol_list)
        )
        capped_top_k = (
            max(1, top_k) if top_k is not None else max(1, service._config.evolution.m8_top_k)
        )
        report = _as_dict_object(
            service._evolution_orchestrator.run_m8_suggestions(
                records=m8_records,
                top_k=capped_top_k,
                now=timestamp,
                source_trace_id=source_trace_id,
            )
        )
        summary = report.get("summary", {})
        service._record_audit_event(
            event_type="evolution_m8_suggest",
            trace_id=source_trace_id,
            payload={
                "symbols": symbol_list,
                "top_k": capped_top_k,
                "summary": summary if isinstance(summary, dict) else {},
                "artifact_uri": report.get("artifact_uri", ""),
            },
        )
        return report

    def latest_evolution_report(self) -> dict[str, object] | None:
        """Return latest evolution run report."""
        service = self._service
        if service._last_evolution_report is not None:
            return _as_dict_object(service._last_evolution_report)
        disk_reports = self._load_evolution_reports_from_disk(cutoff_ts=0.0)
        if not disk_reports:
            return None
        latest = max(disk_reports, key=_report_timestamp)
        service._last_evolution_report = latest
        return latest

    def evolution_history(self, limit: int = 20) -> dict[str, object]:
        """Return recent evolution run reports."""
        service = self._service
        capped = max(1, min(limit, max(1, service._config.evolution.history_limit)))
        recent = service._evolution_history[-capped:]
        return {"records": len(recent), "items": recent}

    def _job_evolution_offhours(self, now: datetime | None = None) -> dict[str, object]:
        service = self._service
        job_now = now or datetime.now()
        watchlist_before = _normalized_symbol_list(service._state.watchlist)
        symbols = [str(item).strip() for item in service._state.watchlist if str(item).strip()]
        symbol_source = "watchlist"
        if not symbols:
            seed_cap = max(
                1,
                _as_int(service._config.training.bootstrap_seed_watchlist_size, default=50),
            )
            symbols = service._bootstrap_seed_symbols(cap=seed_cap)
            symbol_source = "bootstrap_seed" if symbols else "empty"
        report = service.run_evolution_offhours(
            symbols=symbols,
            timestamp=job_now,
            dry_run=None,
            source_trace_id="scheduler-evolution",
        )
        week5_refresh: dict[str, object]
        if bool(service._config.week5.offhours_universe_refresh_enabled):
            week5_refresh = service.run_week5_offhours_refresh(
                timestamp=job_now,
                notify_enabled=False,
                sync_watchlist=True,
                sync_reason="offhours_refresh",
            )
        elif symbols:
            week5_refresh = service.run_week5_scan(
                symbols=symbols,
                timestamp=job_now,
                notify_enabled=False,
                sync_watchlist=True,
                sync_reason="offhours_refresh",
            )
        else:
            week5_refresh = {
                "status": "skipped",
                "reason": "empty_symbols",
                "symbol_source": symbol_source,
            }
        self._notify_evolution_offhours_completion(
            report=report,
            week5_refresh=week5_refresh,
            symbol_source=symbol_source,
            symbol_count=len(symbols),
            watchlist_before=watchlist_before,
            source_trace_id="scheduler-evolution",
            timestamp=job_now,
        )
        return {
            "report": report,
            "tdx_sync": report.get("tdx_sync", {}),
            "week5_refresh": week5_refresh,
            "symbol_source": symbol_source,
            "symbol_count": len(symbols),
        }

    def _notify_evolution_offhours_completion(
        self,
        *,
        report: Mapping[str, object],
        week5_refresh: Mapping[str, object],
        symbol_source: str,
        symbol_count: int,
        watchlist_before: list[str],
        source_trace_id: str,
        timestamp: datetime,
    ) -> dict[str, object] | None:
        service = self._service
        report_status = str(report.get("status", "")).strip().lower()
        if report_status.startswith("blocked") or report_status == "failed":
            return None
        if timestamp.weekday() >= 5:
            service._record_audit_event(
                event_type="evolution_offhours_notify_suppressed_weekend",
                trace_id=source_trace_id.strip() or "scheduler-evolution",
                payload={
                    "timestamp": timestamp.isoformat(),
                    "symbol_source": symbol_source,
                    "symbol_count": symbol_count,
                    "scan_profile": _resolve_offhours_scan_profile(week5_refresh),
                },
            )
            return None

        priority = (
            "P1"
            if self._evolution_offhours_notification_needs_attention(
                report=report,
                week5_refresh=week5_refresh,
            )
            else "P2"
        )
        summary = _evolution_offhours_summary_text(
            _resolve_offhours_scan_profile(week5_refresh)
        )
        title = _push_title(
            priority=priority,
            category="evolution",
            summary=summary,
        )
        content = self._build_evolution_offhours_notification_content(
            report=report,
            week5_refresh=week5_refresh,
            symbol_source=symbol_source,
            symbol_count=symbol_count,
            watchlist_before=watchlist_before,
            timestamp=timestamp,
        )
        dedup_key = (
            "notify:evolution-offhours:"
            f"{timestamp.strftime('%Y%m%d')}:"
            f"{_resolve_offhours_scan_profile(week5_refresh) or 'default'}"
        )
        dedup_value = json.dumps(
            {
                "run_id": str(report.get("run_id", "")).strip(),
                "proposal_id": str(
                    _mapping_object(report.get("proposal")).get("proposal_id", "")
                ).strip(),
                "title": title,
                "content": content,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        trace_id = (
            source_trace_id.strip()
            or str(report.get("source_trace_id", "")).strip()
            or "scheduler-evolution"
        )
        notify_result = service._notify_if_changed(
            dedup_key=dedup_key,
            title=title,
            content=content,
            dedup_value=dedup_value,
            level="warn" if priority == "P1" else "info",
            trace_id=trace_id,
            ttl_sec=20 * 3600,
        )
        if notify_result is None:
            return None
        return _as_dict_object(notify_result)

    def _evolution_offhours_notification_needs_attention(
        self,
        *,
        report: Mapping[str, object],
        week5_refresh: Mapping[str, object],
    ) -> bool:
        m9 = _mapping_object(report.get("m9"))
        refresh_status = str(week5_refresh.get("status", "")).strip().lower()
        market_warehouse_sync = _mapping_object(report.get("market_warehouse_sync"))
        tdx_sync = _mapping_object(report.get("tdx_sync"))
        return (
            bool(m9.get("degraded", False))
            or refresh_status in {"failed", "skipped"}
            or str(market_warehouse_sync.get("status", "")).strip().lower() == "failed"
            or str(tdx_sync.get("status", "")).strip().lower() == "failed"
        )

    def _build_evolution_offhours_notification_content(
        self,
        *,
        report: Mapping[str, object],
        week5_refresh: Mapping[str, object],
        symbol_source: str,
        symbol_count: int,
        watchlist_before: list[str],
        timestamp: datetime,
    ) -> str:
        service = self._service
        proposal = _mapping_object(report.get("proposal"))
        runtime_controls = _mapping_object(report.get("runtime_controls"))
        modules = _mapping_object(report.get("modules"))
        market_warehouse_sync = _mapping_object(report.get("market_warehouse_sync"))
        tdx_sync = _mapping_object(report.get("tdx_sync"))
        m9 = _mapping_object(report.get("m9"))
        watchlist_sync = _mapping_object(week5_refresh.get("watchlist_sync"))
        signal_pool = _mapping_object(week5_refresh.get("signal_pool"))
        ranking = _mapping_object(signal_pool.get("ranking"))
        authorization_level = str(proposal.get("authorization_level", "")).strip().upper() or "-"
        proposal_id = str(proposal.get("proposal_id", "")).strip()
        run_id = str(report.get("run_id", "")).strip()
        completed_at = _format_notification_time_zh(timestamp.isoformat())
        scan_profile = _resolve_offhours_scan_profile(week5_refresh)
        refresh_status = str(week5_refresh.get("status", "")).strip().lower()
        selected_symbols = _normalized_symbol_list(ranking.get("selected_symbols", []))
        watchlist_after = _normalized_symbol_list(watchlist_sync.get("symbols", []))
        if not watchlist_after:
            watchlist_after = _normalized_symbol_list(service._state.watchlist)
        watchlist_before_set = set(_normalized_symbol_list(watchlist_before))
        new_symbols = [symbol for symbol in watchlist_after if symbol not in watchlist_before_set]
        change_keys = _string_list(proposal.get("change_keys", []))
        upgrade_summary = _runtime_control_summary_zh(runtime_controls)
        reason_summary = _runtime_reason_summary_zh(runtime_controls)
        learning_summary = _module_learning_summary_zh(modules)
        watchlist_after_count = _as_int(
            watchlist_sync.get("watchlist_after", len(watchlist_after)),
            default=len(watchlist_after),
        )
        selected_count = _as_int(
            ranking.get("selected_count", len(selected_symbols)),
            default=len(selected_symbols),
        )
        watchlist_result = _watchlist_result_summary_zh(
            watchlist_sync=watchlist_sync,
            watchlist_after=watchlist_after,
            watchlist_after_count=watchlist_after_count,
            new_symbols=new_symbols,
        )
        selected_result = _selected_symbols_summary_zh(
            selected_symbols=selected_symbols,
            selected_count=selected_count,
        )
        data_summary = (
            "market_warehouse="
            f"{_evolution_status_zh(str(market_warehouse_sync.get('status', '')))}"
            "；tdx_sync="
            f"{_evolution_status_zh(str(tdx_sync.get('status', '')))}"
        )
        detail_lines = [
            f"完成时间：{completed_at}",
            f"升级批次：{proposal_id or run_id or '-'}",
            f"输入标的：{symbol_count} 只（来源：{_evolution_symbol_source_zh(symbol_source)}）",
            f"升级方面：{upgrade_summary}",
            f"升级依据：{reason_summary}",
            f"学习结果：{learning_summary}",
            (
                "策略提案：授权级别 "
                f"{authorization_level}；变更项 {', '.join(change_keys[:4]) or '-'}"
            ),
            f"数据补充：{data_summary}",
            (
                "复扫画像："
                f"{_scan_profile_label_zh(scan_profile)}；"
                f"状态={_evolution_status_zh(refresh_status)}"
            ),
            f"观察池结果：{watchlist_result}",
            f"候选股票：{selected_result}",
        ]
        frozen_symbols = _normalized_symbol_list(m9.get("frozen_symbols", []))
        if frozen_symbols:
            detail_lines.append(f"M9 冻结标的：{', '.join(frozen_symbols[:8])}")
        return _notification_message_zh(
            trigger=(
                f"{_evolution_offhours_summary_text(scan_profile)}，"
                f"本轮授权级别 {authorization_level}。"
            ),
            impact=(
                "系统已完成夜间数据补充、学习升级与观察池复核，"
                "次日盘前提示、盘中盯盘和模拟盘会沿用这轮结果继续运行。"
            ),
            action=(
                "建议直接查看这条企业微信摘要；若要复核细节，再打开本地监控页查看"
                " evolution 报告、观察池和候选池。"
            ),
            details=detail_lines,
            detail_title="本轮摘要",
        )

    def _job_evolution_m3_maintenance(self) -> dict[str, object]:
        service = self._service
        report = service.run_evolution_m3_maintenance(
            timestamp=datetime.now(),
            source_trace_id="scheduler-evolution-m3-maintain",
        )
        return {"report": report}

    def _build_evolution_m9_records(self, symbols: list[str]) -> list[dict[str, object]]:
        service = self._service
        records: list[dict[str, object]] = []
        min_list_days = max(1, int(service._config.evolution.universe_spec.min_list_days))
        lookback_days = max(80, min_list_days + 20, service._config.evolution.m9_lookback_days)
        for symbol in symbols:
            bars = service._provider.fetch_daily_bars(symbol=symbol, lookback_days=lookback_days)
            if bars.empty:
                records.append({"symbol": symbol})
                continue
            intraday_1m, intraday_5m = service._fetch_intraday_summaries(
                symbol=symbol,
                lookback_days=max(20, min(lookback_days, 180)),
            )
            latest = bars.iloc[-1]
            listing_days = int(max(0, len(bars)))
            adv60, adv60_available = self._resolve_adv60(bars=bars)
            liquidity_tier, fallback = self._classify_liquidity_tier(
                adv60=adv60,
                adv60_available=adv60_available,
            )
            sparse_history_flag = bool(listing_days < min_list_days or not adv60_available)
            mapping_level_used = (
                "regime_x_liquidity" if sparse_history_flag else "regime_x_liquidity_x_volatility"
            )
            mapping_fallback_steps: list[str] = []
            if sparse_history_flag:
                mapping_fallback_steps.append("fallback_to_regime_x_liquidity")
            if fallback:
                mapping_fallback_steps.append("liquidity_tier_fallback_small")
            intraday_fields = {
                **_extract_intraday_record_fields(
                    frame=intraday_1m,
                    prefix="intraday_1m",
                    columns=(
                        "session_return",
                        "realized_vol",
                        "vwap_gap",
                        "last30_return",
                        "close_position",
                    ),
                ),
                **_extract_intraday_record_fields(
                    frame=intraday_5m,
                    prefix="intraday_5m",
                    columns=(
                        "session_return",
                        "realized_vol",
                        "am_pm_diff",
                        "last30_return",
                        "close_position",
                    ),
                ),
            }
            records.append(
                {
                    "symbol": symbol,
                    "open": _as_float(latest.get("open"), default=0.0),
                    "high": _as_float(latest.get("high"), default=0.0),
                    "low": _as_float(latest.get("low"), default=0.0),
                    "close": _as_float(latest.get("close"), default=0.0),
                    "volume": _as_float(latest.get("volume"), default=0.0),
                    "listing_days": listing_days,
                    "adv60": adv60 if adv60_available else None,
                    "liquidity_tier": liquidity_tier,
                    "liquidity_tier_fallback": fallback,
                    "sparse_history_flag": sparse_history_flag,
                    "mapping_level_used": mapping_level_used,
                    "bucket_sample_count": (
                        listing_days if sparse_history_flag else max(300, listing_days)
                    ),
                    "mapping_fallback_steps": mapping_fallback_steps,
                    **intraday_fields,
                }
            )
        return records

    def _resolve_adv60(self, bars: pd.DataFrame) -> tuple[float, bool]:
        if "turnover" in bars.columns:
            turnover = pd.to_numeric(bars["turnover"], errors="coerce")
        else:
            turnover = pd.Series(index=bars.index, dtype=float)
        if turnover.dropna().empty:
            close = (
                pd.to_numeric(bars["close"], errors="coerce")
                if "close" in bars.columns
                else pd.Series(index=bars.index, dtype=float)
            )
            volume = (
                pd.to_numeric(bars["volume"], errors="coerce")
                if "volume" in bars.columns
                else pd.Series(index=bars.index, dtype=float)
            )
            turnover = close * volume
        turnover = pd.to_numeric(turnover, errors="coerce").dropna()
        if turnover.empty:
            return 0.0, False
        adv60 = float(turnover.tail(60).mean())
        if not math.isfinite(adv60) or adv60 <= 0.0:
            return 0.0, False
        return adv60, True

    def _classify_liquidity_tier(self, *, adv60: float, adv60_available: bool) -> tuple[str, bool]:
        if not adv60_available:
            return "small", True
        if adv60 >= 2_000_000_000.0:
            return "large", False
        if adv60 >= 500_000_000.0:
            return "mid", False
        return "small", False

    def _prepare_evolution_loader_inputs(
        self,
        symbols: list[str],
        now: datetime,
    ) -> dict[str, object]:
        """Prepare independent loader artifacts for M5/M7/M11 and wire config paths."""
        m5 = self._ensure_evolution_loader_artifact(
            symbols=symbols,
            now=now,
            config_attr="m5_label_records_path",
            module_name="m5",
            default_filename="m5_labels_latest.jsonl",
            builder=self._build_evolution_m5_label_records,
        )
        m7 = self._ensure_evolution_loader_artifact(
            symbols=symbols,
            now=now,
            config_attr="m7_news_records_path",
            module_name="m7",
            default_filename="m7_news_latest.jsonl",
            builder=self._build_evolution_m7_news_records,
        )
        m11 = self._ensure_evolution_loader_artifact(
            symbols=symbols,
            now=now,
            config_attr="m11_shadow_results_path",
            module_name="m11",
            default_filename="m11_shadow_latest.jsonl",
            builder=self._build_evolution_m11_shadow_results,
        )
        return {"m5": m5, "m7": m7, "m11": m11}

    def _ensure_evolution_loader_artifact(
        self,
        *,
        symbols: list[str],
        now: datetime,
        config_attr: str,
        module_name: str,
        default_filename: str,
        builder: Callable[[list[str]], list[dict[str, object]]],
    ) -> dict[str, object]:
        service = self._service
        raw_configured = str(getattr(service._config.evolution, config_attr, "")).strip()
        configured_path = self._resolve_evolution_path(raw_configured) if raw_configured else None
        if configured_path is not None and configured_path.exists():
            if self._is_evolution_artifact_fresh(configured_path, now=now):
                configured_relative = self._to_evolution_relative(configured_path)
                artifact_records = _load_json_mapping_records(configured_path)
                return {
                    "module": module_name,
                    "source": "configured",
                    "path": configured_relative,
                    "records": (
                        len(artifact_records)
                        if artifact_records
                        else self._count_evolution_records(configured_path)
                    ),
                    "fresh": True,
                    "generated": False,
                    **_summarize_intraday_loader_records(artifact_records),
                }

        records = builder(symbols)
        if module_name == "m7" and not records:
            return {
                "module": module_name,
                "source": "unavailable",
                "path": raw_configured or "",
                "records": 0,
                "fresh": False,
                "generated": False,
                "reason": "no_valid_news_input",
            }
        target_dir = self._resolve_evolution_path(service._config.evolution.loader_artifact_dir)
        target_path = target_dir / default_filename
        self._write_jsonl_atomic(path=target_path, records=records)
        relative = self._to_evolution_relative(target_path)
        setattr(service._config.evolution, config_attr, relative)
        return {
            "module": module_name,
            "source": "generated",
            "path": relative,
            "records": len(records),
            "fresh": self._is_evolution_artifact_fresh(target_path, now=now),
            "generated": True,
            **_summarize_intraday_loader_records(records),
        }

    def _build_evolution_m5_label_records(self, symbols: list[str]) -> list[dict[str, object]]:
        service = self._service
        records: list[dict[str, object]] = []
        lookback_days = max(
            12,
            service._config.labels.horizon_days + 8,
            service._config.evolution.m9_lookback_days + 8,
        )
        for symbol in symbols:
            bars = service._provider.fetch_daily_bars(symbol=symbol, lookback_days=lookback_days)
            if bars.empty:
                continue
            intraday_1m, intraday_5m = service._fetch_intraday_summaries(
                symbol=symbol,
                lookback_days=max(20, min(lookback_days, 180)),
            )
            try:
                labels = build_soup_labels(
                    bars=bars,
                    take_profit_pct=service._config.labels.take_profit_pct,
                    stop_loss_pct=service._config.labels.stop_loss_pct,
                    horizon_days=service._config.labels.horizon_days,
                    price_basis=service._config.labels.pnl_price_basis,
                    exclude_untradable=service._config.labels.exclude_untradable,
                )
            except ValueError:
                continue
            frame = bars.join(labels.rename("label"), how="left")
            frame = frame.dropna(subset=["label"]).tail(12)
            for _, row in frame.iterrows():
                open_px = _as_float(row.get("open"), default=0.0)
                close_px = _as_float(row.get("close"), default=0.0)
                label = _as_float(row.get("label"), default=-1.0)
                if open_px <= 0.0 or close_px <= 0.0 or label < 0.0:
                    continue
                base_label = 1 if label >= 0.5 else 0
                trade_date = _normalize_trade_date_text(row.name)
                records.append(
                    {
                        "symbol": symbol,
                        "date": trade_date,
                        "open": open_px,
                        "close": close_px,
                        "label": base_label,
                        "label_seed_1": base_label,
                        "label_seed_2": 1 if close_px >= open_px else 0,
                        **_build_intraday_replay_fields(
                            trade_date=trade_date,
                            intraday_1m=intraday_1m,
                            intraday_5m=intraday_5m,
                            intraday_1m_columns=_M5_INTRADAY_1M_COLUMNS,
                            intraday_5m_columns=_M5_INTRADAY_5M_COLUMNS,
                        ),
                    }
                )
        return records

    def _load_m7_news_records_from_local_sources(
        self,
        *,
        symbol_set: set[str],
    ) -> list[dict[str, object]]:
        service = self._service
        default_cost = max(0.01, service._config.evolution.m7_default_event_cost)
        candidate_dirs = [
            self._resolve_evolution_path("data/news"),
            self._resolve_evolution_path("artifacts/news"),
            self._resolve_evolution_path("staging/news"),
            self._resolve_evolution_path("artifacts/evolution/news_feed"),
        ]
        artifact_path = service._resolve_m7_news_artifact_path()
        if artifact_path.parent.exists():
            candidate_dirs.append(artifact_path.parent)
        candidate_files: list[Path] = []
        for directory in candidate_dirs:
            if not directory.exists() or not directory.is_dir():
                continue
            for suffix in ("*.jsonl", "*.json"):
                candidate_files.extend(directory.rglob(suffix))
        if artifact_path.exists():
            candidate_files.append(artifact_path)
        unique_files: dict[str, Path] = {}
        for path in candidate_files:
            if path.is_file():
                unique_files[str(path.resolve())] = path
        ordered_files = sorted(
            unique_files.values(),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )

        normalized_records: list[dict[str, object]] = []
        seen_ids: set[str] = set()
        for file_path in ordered_files[:120]:
            raw_records = load_m7_news_records(path=file_path)
            if not raw_records:
                continue
            for item in raw_records:
                raw_symbol = (
                    item.get("symbol")
                    or item.get("ticker")
                    or item.get("code")
                    or item.get("stock_code")
                )
                symbol = _normalize_a_share_symbol(raw_symbol)
                if not symbol:
                    continue
                if symbol_set and symbol not in symbol_set:
                    continue
                headline = ""
                for key in ("headline", "title", "news_headline", "news_title", "news", "text"):
                    value = str(item.get(key, "")).strip()
                    if value:
                        headline = value
                        break
                if not headline:
                    continue
                sentiment = _clamp(
                    _as_float(
                        item.get(
                            "sentiment", item.get("news_sentiment", item.get("llm_sentiment", 0.0))
                        ),
                        default=0.0,
                    ),
                    -1.0,
                    1.0,
                )
                event_id = str(item.get("event_id", "")).strip()
                if not event_id:
                    event_id = hashlib.sha256(f"{symbol}|{headline}".encode()).hexdigest()[:24]
                if event_id in seen_ids:
                    continue
                seen_ids.add(event_id)
                normalized_records.append(
                    {
                        "event_id": event_id,
                        "symbol": symbol,
                        "headline": headline,
                        "content": str(item.get("content", item.get("summary", ""))).strip(),
                        "published_at": str(
                            item.get("published_at", item.get("event_time", ""))
                        ).strip(),
                        "source": str(item.get("source", item.get("provider", ""))).strip(),
                        "url": str(item.get("url", "")).strip(),
                        "sentiment": sentiment,
                        "llm_sentiment": _clamp(
                            _as_float(item.get("llm_sentiment"), default=sentiment),
                            -1.0,
                            1.0,
                        ),
                        "cost": max(_as_float(item.get("cost"), default=default_cost), 0.000001),
                        "llm_verdict": str(
                            item.get("llm_verdict", _m7_llm_verdict_from_sentiment(sentiment))
                        )
                        .strip()
                        .lower(),
                        "llm_confidence": _clamp(
                            _as_float(
                                item.get("llm_confidence"), default=0.55 + abs(sentiment) * 0.35
                            ),
                            0.0,
                            1.0,
                        ),
                        "source_file": str(file_path),
                        "provider": str(item.get("provider", "")).strip(),
                        "proxy_generated": bool(item.get("proxy_generated", False)),
                    }
                )
                if len(normalized_records) >= 20_000:
                    break
            if len(normalized_records) >= 20_000:
                break
        return normalized_records

    def _build_evolution_m7_news_records(self, symbols: list[str]) -> list[dict[str, object]]:
        service = self._service
        symbol_set = {
            normalized
            for normalized in (_normalize_a_share_symbol(item) for item in symbols)
            if normalized
        }
        normalized_records: list[dict[str, object]] = []
        if bool(service._config.evolution.m7_live_news_enabled):
            live_symbols = (
                sorted(symbol_set)
                if symbol_set
                else service._resolve_m7_live_news_symbols(
                    symbols=None,
                    max_symbols=max(1, int(service._config.evolution.m7_live_news_max_symbols)),
                )
            )
            live_records, _ = service._collect_live_m7_news_records(
                symbols=live_symbols,
                now=datetime.now(),
                max_age_hours=max(1.0, float(service._config.evolution.m7_live_news_max_age_hours)),
                per_symbol_limit=max(
                    1,
                    int(service._config.evolution.m7_live_news_per_symbol_limit),
                ),
                force_refresh=False,
                enable_ai_review=bool(service._config.evolution.m7_ai_review_enabled),
            )
            normalized_records.extend(_as_list_of_dict_objects(live_records))

        local_records = self._load_m7_news_records_from_local_sources(symbol_set=symbol_set)
        if local_records:
            normalized_records = _as_list_of_dict_objects(
                service._merge_m7_news_records(
                    current=normalized_records,
                    existing=local_records,
                    max_records=max(
                        1, int(service._config.evolution.m7_live_news_artifact_max_records)
                    ),
                )
            )
        if normalized_records:
            return normalized_records
        if not bool(service._config.evolution.m7_market_proxy_fallback_enabled):
            return []

        default_cost = max(0.01, service._config.evolution.m7_default_event_cost)
        normalized_records = []
        proxy_symbols = sorted(symbol_set) if symbol_set else []
        lookback_days = max(6, service._config.evolution.m9_lookback_days + 6)
        for symbol in proxy_symbols[:600]:
            try:
                bars = service._provider.fetch_daily_bars(
                    symbol=symbol,
                    lookback_days=lookback_days,
                )
            except Exception:
                continue
            if bars.shape[0] < 2:
                continue
            recent = bars.sort_index().tail(6)
            for pos in range(1, len(recent)):
                prev_close = _as_float(recent["close"].iloc[pos - 1], default=0.0)
                close = _as_float(recent["close"].iloc[pos], default=0.0)
                if prev_close <= 0.0 or close <= 0.0:
                    continue
                ret = close / prev_close - 1.0
                sentiment = _clamp(ret * 6.0, -1.0, 1.0)
                date_value = recent.index[pos]
                date_text = (
                    date_value.strftime("%Y%m%d")
                    if hasattr(date_value, "strftime")
                    else str(date_value)
                )
                normalized_records.append(
                    {
                        "event_id": f"{symbol}_{date_text}_proxy",
                        "symbol": symbol,
                        "headline": f"market_proxy:{symbol}:{date_text}:ret={ret:+.2%}",
                        "sentiment": sentiment,
                        "cost": default_cost,
                        "llm_verdict": (
                            "approve"
                            if sentiment >= 0.05
                            else ("reject" if sentiment <= -0.05 else "review")
                        ),
                        "llm_confidence": _clamp(0.5 + abs(sentiment) * 0.3, 0.0, 1.0),
                        "source_file": "__market_proxy__",
                        "proxy_generated": True,
                    }
                )
            if len(normalized_records) >= 20_000:
                break
        return normalized_records

    def _build_evolution_m11_shadow_results(self, symbols: list[str]) -> list[dict[str, object]]:
        service = self._service
        records: list[dict[str, object]] = []
        lookback_days = max(8, service._config.evolution.m9_lookback_days + 8)
        for symbol in symbols:
            bars = service._provider.fetch_daily_bars(symbol=symbol, lookback_days=lookback_days)
            if bars.shape[0] < 2:
                continue
            intraday_1m, intraday_5m = service._fetch_intraday_summaries(
                symbol=symbol,
                lookback_days=max(20, min(lookback_days, 180)),
            )
            recent = bars.sort_index().tail(12)
            for pos in range(1, len(recent)):
                prev_close = _as_float(recent["close"].iloc[pos - 1], default=0.0)
                close = _as_float(recent["close"].iloc[pos], default=0.0)
                if prev_close <= 0.0 or close <= 0.0:
                    continue
                champion_return = close / prev_close - 1.0
                challenger_return = (
                    champion_return * 0.97 if champion_return >= 0.0 else champion_return * 1.03
                )
                date_value = recent.index[pos]
                date_text = (
                    date_value.strftime("%Y-%m-%d")
                    if hasattr(date_value, "strftime")
                    else str(date_value)
                )
                records.append(
                    {
                        "symbol": symbol,
                        "date": date_text,
                        "champion_shadow_return": champion_return,
                        "challenger_shadow_return": challenger_return,
                        "champion_signal": 1 if champion_return >= 0.0 else 0,
                        "challenger_signal": 1 if challenger_return >= 0.0 else 0,
                        **_build_intraday_replay_fields(
                            trade_date=date_text,
                            intraday_1m=intraday_1m,
                            intraday_5m=intraday_5m,
                            intraday_1m_columns=_M5_INTRADAY_1M_COLUMNS,
                            intraday_5m_columns=_M5_INTRADAY_5M_COLUMNS,
                        ),
                    }
                )
        return records

    def _is_evolution_artifact_fresh(self, path: Path, now: datetime) -> bool:
        service = self._service
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False
        max_age_hours = max(1, service._config.evolution.loader_max_age_hours)
        wall_clock_ts = datetime.now().timestamp()
        reference_ts = min(now.timestamp(), wall_clock_ts)
        age_seconds = max(0.0, reference_ts - mtime)
        return age_seconds <= float(max_age_hours * 3600)

    def _count_evolution_records(self, path: Path) -> int:
        try:
            if path.suffix.lower() == ".jsonl":
                lines = [
                    line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
                ]
                return len(lines)
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                return len(payload)
            if isinstance(payload, dict):
                for key in ("records", "items", "results", "data"):
                    value = payload.get(key)
                    if isinstance(value, list):
                        return len(value)
                return 1
        except (OSError, json.JSONDecodeError, ValueError):
            return 0
        return 0

    def _write_jsonl_atomic(self, path: Path, records: list[dict[str, object]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
        with tmp_path.open("w", encoding="utf-8") as fp:
            for row in records:
                fp.write(json.dumps(row, ensure_ascii=False) + "\n")
        tmp_path.replace(path)

    def _write_json_atomic(self, path: Path, payload: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def _to_evolution_relative(self, path: Path) -> str:
        service = self._service
        try:
            return str(path.relative_to(service._evolution_project_root)).replace("\\", "/")
        except ValueError:
            return str(path)

    def _resolve_evolution_path(self, raw_path: str) -> Path:
        service = self._service
        candidate = Path(raw_path)
        if candidate.is_absolute():
            return candidate
        return _as_path_object(service._evolution_project_root) / candidate

    def _persist_evolution_report(self, report: dict[str, object]) -> None:
        service = self._service
        report_dir = self._resolve_evolution_path(service._config.evolution.report_dir)
        try:
            report_dir.mkdir(parents=True, exist_ok=True)
            timestamp = str(report.get("timestamp", "")).replace("-", "").replace(":", "")
            timestamp = timestamp.replace("T", "_")
            if not timestamp:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            run_id = str(report.get("run_id", "unknown")).replace("/", "_")
            output_path = report_dir / f"{timestamp}_{run_id}.json"
            output_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            service._record_audit_event(
                event_type="evolution_report_persist_failed",
                level="warn",
                message=str(exc),
            )

    def _load_evolution_reports_from_disk(self, cutoff_ts: float) -> list[dict[str, object]]:
        service = self._service
        report_dir = self._resolve_evolution_path(service._config.evolution.report_dir)
        if not report_dir.exists():
            return []
        reports: list[dict[str, object]] = []
        timestamped_paths: list[tuple[float, Path]] = []
        fallback_paths: list[Path] = []
        for path in report_dir.glob("*.json"):
            hinted_ts = _report_path_timestamp_hint(path)
            if hinted_ts is None:
                fallback_paths.append(path)
                continue
            timestamped_paths.append((hinted_ts, path))

        for hinted_ts, path in sorted(timestamped_paths, key=lambda item: item[0], reverse=True):
            if hinted_ts < cutoff_ts:
                break
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            if _report_timestamp(payload) < cutoff_ts:
                continue
            reports.append(payload)

        for path in sorted(fallback_paths):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            if _report_timestamp(payload) < cutoff_ts:
                continue
            reports.append(payload)
        return reports


@lru_cache(maxsize=1)
def _runtime_service_module() -> Any:
    return import_module("stock_analyzer.runtime.service")


_EVOLUTION_REPORT_TIMESTAMP_RE = re.compile(
    r"^(?P<date>\d{8})_(?P<time>\d{6})(?:\.\d+)?(?:_|$)"
)


def _report_path_timestamp_hint(path: Path) -> float | None:
    match = _EVOLUTION_REPORT_TIMESTAMP_RE.match(path.stem)
    if match is None:
        return None
    compact = f"{match.group('date')}{match.group('time')}"
    try:
        return datetime.strptime(compact, "%Y%m%d%H%M%S").timestamp()
    except ValueError:
        return None


def _as_dict_object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    raise TypeError(f"Expected dict[str, object], got {type(value).__name__}")


def _as_list_of_dict_objects(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise TypeError(f"Expected list[dict[str, object]], got {type(value).__name__}")
    return [dict(item) for item in value if isinstance(item, dict)]


def _as_path_object(value: object) -> Path:
    if isinstance(value, Path):
        return value
    raise TypeError(f"Expected Path, got {type(value).__name__}")


def _as_float(value: object, default: float) -> float:
    return cast(float, _runtime_service_module()._as_float(value, default))


def _as_int(value: object, default: int) -> int:
    return cast(int, _runtime_service_module()._as_int(value, default))


def _clamp(value: float, min_value: float, max_value: float) -> float:
    return cast(float, _runtime_service_module()._clamp(value, min_value, max_value))


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    return cast(list[str], _runtime_service_module()._dedupe_preserve_order(values))


def _extract_intraday_record_fields(
    *,
    frame: pd.DataFrame | None,
    prefix: str,
    columns: tuple[str, ...],
) -> dict[str, object]:
    return cast(
        dict[str, object],
        _runtime_service_module()._extract_intraday_record_fields(
            frame=frame,
            prefix=prefix,
            columns=columns,
        ),
    )


def _build_intraday_replay_fields(
    *,
    trade_date: str,
    intraday_1m: pd.DataFrame | None,
    intraday_5m: pd.DataFrame | None,
    intraday_1m_columns: tuple[str, ...],
    intraday_5m_columns: tuple[str, ...],
) -> dict[str, object]:
    return {
        **_extract_intraday_record_fields_for_trade_date(
            frame=intraday_1m,
            prefix="intraday_1m",
            columns=intraday_1m_columns,
            trade_date=trade_date,
        ),
        **_extract_intraday_record_fields_for_trade_date(
            frame=intraday_5m,
            prefix="intraday_5m",
            columns=intraday_5m_columns,
            trade_date=trade_date,
        ),
    }


def _extract_intraday_record_fields_for_trade_date(
    *,
    frame: pd.DataFrame | None,
    prefix: str,
    columns: tuple[str, ...],
    trade_date: str,
) -> dict[str, object]:
    empty_payload = {
        f"{prefix}_latest_date": "",
        **{f"{prefix}_{column}": None for column in columns},
    }
    if frame is None or frame.empty:
        return empty_payload
    normalized_trade_date = _normalize_trade_date_text(trade_date)
    if not normalized_trade_date:
        return empty_payload
    working = frame.copy()
    try:
        if not isinstance(working.index, pd.DatetimeIndex):
            working.index = pd.to_datetime(working.index, errors="coerce")
    except (TypeError, ValueError):
        return empty_payload
    if not isinstance(working.index, pd.DatetimeIndex):
        return empty_payload
    working = working[~working.index.isna()]
    if working.empty:
        return empty_payload
    target_date = pd.Timestamp(normalized_trade_date).normalize()
    matched = working.loc[working.index.normalize() == target_date]
    if matched.empty:
        return empty_payload
    latest = matched.sort_index().iloc[-1]
    payload: dict[str, object] = {f"{prefix}_latest_date": target_date.date().isoformat()}
    for column in columns:
        payload[f"{prefix}_{column}"] = (
            _as_float(latest.get(column), default=0.0) if column in latest.index else None
        )
    return payload


def _normalize_trade_date_text(value: object) -> str:
    if isinstance(value, pd.Timestamp):
        return value.normalize().date().isoformat()
    try:
        parsed = pd.Timestamp(value)
    except (TypeError, ValueError):
        return str(value).strip()
    if pd.isna(parsed):
        return ""
    return parsed.normalize().date().isoformat()


def _load_json_mapping_records(path: Path) -> list[dict[str, object]]:
    try:
        if path.suffix.lower() == ".jsonl":
            rows: list[dict[str, object]] = []
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                if isinstance(payload, Mapping):
                    rows.append(
                        {str(key): value for key, value in payload.items() if isinstance(key, str)}
                    )
            return rows
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    if isinstance(payload, list):
        return [
            {str(key): value for key, value in item.items() if isinstance(key, str)}
            for item in payload
            if isinstance(item, Mapping)
        ]
    if isinstance(payload, Mapping):
        for key in ("records", "items", "results", "data"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                return [
                    {
                        str(item_key): item_value
                        for item_key, item_value in item.items()
                        if isinstance(item_key, str)
                    }
                    for item in candidate
                    if isinstance(item, Mapping)
                ]
        return [{str(key): value for key, value in payload.items() if isinstance(key, str)}]
    return []


def _summarize_intraday_loader_records(records: list[Mapping[str, object]]) -> dict[str, object]:
    total = len(records)
    intraday_1m_records = sum(
        1 for record in records if _record_has_intraday_context(record, prefix="intraday_1m")
    )
    intraday_5m_records = sum(
        1 for record in records if _record_has_intraday_context(record, prefix="intraday_5m")
    )
    denominator = max(total, 1)
    return {
        "intraday_1m_records": intraday_1m_records,
        "intraday_5m_records": intraday_5m_records,
        "intraday_1m_coverage_ratio": round(intraday_1m_records / denominator, 4)
        if total
        else 0.0,
        "intraday_5m_coverage_ratio": round(intraday_5m_records / denominator, 4)
        if total
        else 0.0,
    }


def _record_has_intraday_context(record: Mapping[str, object], *, prefix: str) -> bool:
    latest_date = str(record.get(f"{prefix}_latest_date", "")).strip()
    if latest_date:
        return True
    for key, value in record.items():
        if not str(key).startswith(f"{prefix}_"):
            continue
        if value not in (None, "", []):
            return True
    return False


def _m7_llm_verdict_from_sentiment(sentiment: float) -> str:
    return cast(str, _runtime_service_module()._m7_llm_verdict_from_sentiment(sentiment))


def _normalize_a_share_symbol(symbol: object) -> str:
    return cast(str, _runtime_service_module()._normalize_a_share_symbol(symbol))


def _report_timestamp(report: dict[str, object]) -> float:
    return cast(float, _runtime_service_module()._report_timestamp(report))


def _string_list(values: object) -> list[str]:
    return cast(list[str], _runtime_service_module()._string_list(values))


def _notification_message_zh(
    *,
    trigger: str,
    impact: str,
    action: str,
    details: list[str] | tuple[str, ...] | None = None,
    detail_title: str = "详细追踪",
) -> str:
    return cast(
        str,
        _runtime_service_module()._notification_message_zh(
            trigger=trigger,
            impact=impact,
            action=action,
            details=details,
            detail_title=detail_title,
        ),
    )


def _push_title(priority: str, category: str, summary: str) -> str:
    return cast(str, _runtime_service_module()._push_title(priority, category, summary))


def _format_notification_time_zh(value: str) -> str:
    return cast(str, _runtime_service_module()._format_notification_time_zh(value))


def _mapping_object(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    return {}


def _normalized_symbol_list(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for item in values:
        symbol = _normalize_a_share_symbol(item) or str(item).strip()
        if symbol:
            normalized.append(symbol)
    return _dedupe_preserve_order(normalized)


def _resolve_offhours_scan_profile(week5_refresh: Mapping[str, object]) -> str:
    direct = str(week5_refresh.get("scan_profile", "")).strip()
    if direct:
        return direct
    profile = _mapping_object(week5_refresh.get("offhours_refresh_profile"))
    return str(profile.get("scan_profile", "")).strip()


def _evolution_offhours_summary_text(scan_profile: str) -> str:
    normalized = scan_profile.strip().lower()
    if normalized == "offhours_friday_full_deep":
        return "周五深扫学习完成"
    if normalized == "offhours_weekend_full_deep":
        return "周末深扫学习完成"
    if normalized == "offhours_forced_full_deep":
        return "夜间深扫学习完成"
    return "夜间学习完成"


def _scan_profile_label_zh(scan_profile: str) -> str:
    mapping = {
        "offhours_weekday_light_topk_deep": "工作日轻筛深扫",
        "offhours_weekend_full_deep": "周末全量深扫",
        "offhours_friday_full_deep": "周五深扫",
        "offhours_forced_full_deep": "强制全量深扫",
        "default": "默认",
    }
    normalized = scan_profile.strip().lower()
    return mapping.get(normalized, scan_profile.strip() or "-")


def _evolution_symbol_source_zh(symbol_source: str) -> str:
    mapping = {
        "watchlist": "当前观察池",
        "bootstrap_seed": "训练引导种子",
        "empty": "空观察池",
    }
    normalized = symbol_source.strip().lower()
    return mapping.get(normalized, symbol_source.strip() or "-")


def _evolution_status_zh(status: str) -> str:
    mapping = {
        "ok": "正常",
        "success": "成功",
        "completed": "完成",
        "skipped": "跳过",
        "updated": "已更新",
        "applied": "已应用",
        "failed": "失败",
        "warn": "告警",
        "warning": "告警",
        "healthy": "健康",
        "watch": "观察",
        "degraded": "退化",
        "limited_observability": "可观测性受限",
        "no_data": "无数据",
        "range": "震荡",
        "trend_up": "上升趋势",
        "trend_down": "下降趋势",
        "extreme": "极端风险",
        "outflow_dominant": "净流出主导",
        "inflow_dominant": "净流入主导",
        "heavy_sell_pressure": "抛压偏大",
        "favorable": "偏友好",
        "blocked": "已阻塞",
    }
    normalized = status.strip().lower()
    return mapping.get(normalized, status.strip() or "-")


def _runtime_control_summary_zh(runtime_controls: Mapping[str, object]) -> str:
    if not runtime_controls:
        return "本轮未产出新的运行控制调整"
    items = [
        f"阈值偏移 {_as_float(runtime_controls.get('threshold_shift'), default=0.0):+.1f}",
        f"仓位系数 {_as_float(runtime_controls.get('position_multiplier'), default=1.0):.2f}",
        f"全局风险 {_as_float(runtime_controls.get('global_risk_delta'), default=0.0):+.1f}",
    ]
    regime_hint = str(runtime_controls.get("regime_hint", "")).strip()
    if regime_hint:
        items.append(f"风格提示 {_evolution_status_zh(regime_hint)}")
    if bool(runtime_controls.get("conservative_mode", False)):
        items.append("防守模式已开启")
    if bool(runtime_controls.get("degraded_mode", False)):
        items.append("执行退化保护已开启")
    return "；".join(items)


def _runtime_reason_summary_zh(runtime_controls: Mapping[str, object]) -> str:
    reasons = _string_list(runtime_controls.get("reasons", []))
    if not reasons:
        return "无显著控制因子触发"
    translated = [_runtime_reason_zh(reason) for reason in reasons[:5]]
    return "、".join(item for item in translated if item) or "无显著控制因子触发"


def _runtime_reason_zh(reason: str) -> str:
    mapping = {
        "m2_range": "M2 判定市场偏震荡",
        "m2_trend_down": "M2 判定市场转弱",
        "m2_extreme": "M2 判定进入极端风险区",
        "m2_trend_up": "M2 判定市场转强",
        "m4_outflow_dominant": "M4 检测主力净流出占优",
        "m4_inflow_dominant": "M4 检测主力净流入占优",
        "m6_heavy_sell_pressure": "M6 检测卖压偏大",
        "m6_favorable": "M6 检测对手盘偏友好",
        "m10_watch": "M10 执行链路进入观察",
        "m10_healthy": "M10 执行链路健康",
        "m10_degraded": "M10 执行链路退化",
        "m10_limited_observability": "M10 可观测性受限",
        "m10_no_data": "M10 缺少执行数据",
        "m9_manual_review": "M9 需要人工复核",
    }
    lowered = reason.strip().lower()
    return mapping.get(lowered, reason.strip())


def _module_learning_summary_zh(modules: Mapping[str, object]) -> str:
    highlights: list[str] = []
    for module_name in ("m2", "m4", "m6", "m7", "m10", "m11", "m1", "m3", "m5", "m8"):
        module_payload = _mapping_object(modules.get(module_name))
        summary = _module_learning_item_zh(module_name=module_name, module_payload=module_payload)
        if summary:
            highlights.append(summary)
        if len(highlights) >= 4:
            break
    return "；".join(highlights) or "本轮未产出可摘要的模块学习结果"


def _module_learning_item_zh(
    *,
    module_name: str,
    module_payload: Mapping[str, object],
) -> str:
    if not module_payload:
        return ""
    status = str(module_payload.get("status", "")).strip().lower()
    if status in {"", "skipped", "skipped_by_m9"}:
        return ""
    if module_name == "m2":
        active_state = str(module_payload.get("active_state", "")).strip().lower()
        label = _evolution_status_zh(active_state or status)
        return f"M2 学到市场处于{label}"
    if module_name == "m4":
        return f"M4 学到资金面为{_evolution_status_zh(status)}"
    if module_name == "m6":
        return f"M6 学到对手盘为{_evolution_status_zh(status)}"
    if module_name == "m7":
        return f"M7 学到消息面状态为{_evolution_status_zh(status)}"
    if module_name == "m10":
        health_status = str(module_payload.get("health_status", status)).strip().lower()
        return f"M10 学到执行链路为{_evolution_status_zh(health_status)}"
    if module_name == "m11":
        return f"M11 影子回放状态为{_evolution_status_zh(status)}"
    return f"{module_name.upper()} 状态={_evolution_status_zh(status)}"


def _watchlist_result_summary_zh(
    *,
    watchlist_sync: Mapping[str, object],
    watchlist_after: list[str],
    watchlist_after_count: int,
    new_symbols: list[str],
) -> str:
    updated = bool(watchlist_sync.get("updated", False))
    if new_symbols:
        preview = ", ".join(new_symbols[:10])
        return f"已更新到 {watchlist_after_count} 只，新增 {len(new_symbols)} 只：{preview}"
    if updated:
        preview = ", ".join(watchlist_after[:10])
        if preview:
            return f"已刷新到 {watchlist_after_count} 只，本轮无新增；前序标的：{preview}"
        return f"已刷新到 {watchlist_after_count} 只，本轮无新增"
    return f"未更新，当前保持 {watchlist_after_count} 只"


def _selected_symbols_summary_zh(
    *,
    selected_symbols: list[str],
    selected_count: int,
) -> str:
    if not selected_symbols:
        return "本轮未选出新的候选股票"
    preview = ", ".join(selected_symbols[:10])
    return f"本轮入围 {selected_count} 只；前序候选：{preview}"
