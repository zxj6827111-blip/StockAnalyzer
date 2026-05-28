"""Runtime archive, audit, and status workflows extracted from the runtime service."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, time, timedelta
from functools import lru_cache
from importlib import import_module
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService


logger = logging.getLogger(__name__)


class RuntimeOpsService:
    """Delegated runtime archive, audit, and status operations."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service

    def runtime_history_archive_status(self, limit: int = 20) -> dict[str, object]:
        service = self._service
        archive_dir = service._runtime_history_archive_dir
        files: list[dict[str, object]] = []
        if archive_dir.exists():
            candidates = sorted(
                archive_dir.glob("runtime_history_*.json"),
                key=lambda item: item.name,
                reverse=True,
            )
            capped = max(1, min(limit, 500))
            for path in candidates[:capped]:
                stamp = path.stem.removeprefix("runtime_history_")
                files.append(
                    {
                        "day": stamp,
                        "path": str(path),
                        "size_bytes": int(path.stat().st_size) if path.exists() else 0,
                        "updated_at": (
                            datetime.fromtimestamp(path.stat().st_mtime).isoformat()
                            if path.exists()
                            else ""
                        ),
                    }
                )
        return {
            "enabled": bool(service._config.command_channel.history_archive_enabled),
            "archive_dir": str(archive_dir),
            "retention_days": max(
                1,
                _as_int(
                    service._config.command_channel.history_archive_retention_days,
                    default=30,
                ),
            ),
            "max_records": max(
                100,
                _as_int(
                    service._config.command_channel.history_archive_max_records,
                    default=2000,
                ),
            ),
            "records": len(files),
            "files": files,
            "latest": service._last_runtime_history_archive,
        }

    def archive_runtime_history_if_needed(
        self,
        now: datetime | None = None,
    ) -> dict[str, object]:
        service = self._service
        current = now or datetime.now()
        day_key = current.strftime("%Y%m%d")
        if not bool(service._config.command_channel.history_archive_enabled):
            return {
                "archived": False,
                "reason": "disabled",
                "day": day_key,
            }
        dedup_key = f"runtime-history-archive:{day_key}"
        if service._cache.exists(dedup_key):
            return {
                "archived": False,
                "reason": "dedup",
                "day": day_key,
            }
        report = service.archive_runtime_history(now=current, force=False)
        if bool(report.get("archived", False)):
            service._cache.set(dedup_key, "1", ttl_sec=26 * 3600)
        return report

    def archive_runtime_history(
        self,
        now: datetime | None = None,
        force: bool = False,
    ) -> dict[str, object]:
        service = self._service
        current = now or datetime.now()
        day_key = current.strftime("%Y%m%d")
        archive_dir = service._runtime_history_archive_dir
        archive_path = archive_dir / f"runtime_history_{day_key}.json"
        if not bool(service._config.command_channel.history_archive_enabled):
            report = {
                "archived": False,
                "reason": "disabled",
                "day": day_key,
                "path": str(archive_path),
            }
            service._last_runtime_history_archive = report
            return report
        if archive_path.exists() and not force:
            report = {
                "archived": False,
                "reason": "exists",
                "day": day_key,
                "path": str(archive_path),
            }
            service._last_runtime_history_archive = report
            return report

        max_records = max(
            100,
            min(
                20000,
                _as_int(
                    service._config.command_channel.history_archive_max_records,
                    default=2000,
                ),
            ),
        )
        payload = service._build_runtime_history_archive_payload(
            now=current,
            max_records=max_records,
        )
        try:
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            report = {
                "archived": False,
                "reason": "write_failed",
                "day": day_key,
                "path": str(archive_path),
                "error": str(exc),
            }
            service._record_audit_event(
                event_type="runtime_history_archive_failed",
                level="warn",
                message=str(exc),
                payload={"path": str(archive_path), "day": day_key},
            )
            service._last_runtime_history_archive = report
            return report

        purged_files = service._purge_runtime_history_archives(now=current)
        report = {
            "archived": True,
            "day": day_key,
            "path": str(archive_path),
            "size_bytes": int(archive_path.stat().st_size),
            "retention_days": max(
                1,
                _as_int(
                    service._config.command_channel.history_archive_retention_days,
                    default=30,
                ),
            ),
            "max_records": max_records,
            "purged_count": len(purged_files),
            "purged_files": purged_files,
            "summary": payload.get("summary", {}),
        }
        service._record_audit_event(
            event_type="runtime_history_archived",
            payload={
                "day": day_key,
                "path": str(archive_path),
                "summary": payload.get("summary", {}),
                "purged_count": len(purged_files),
            },
        )
        service._last_runtime_history_archive = report
        return report

    def _build_runtime_history_archive_payload(
        self,
        *,
        now: datetime,
        max_records: int,
    ) -> dict[str, object]:
        service = self._service
        trades = service._portfolio.trades(limit=max_records)
        reconcile_recent = service._reconcile_history[-max_records:]
        run_summaries = service._run_summaries[-max_records:]
        latency_recent = service._latency_history_ms[-max_records:]
        audit_recent = service._audit_events[-max_records:]
        lifecycle = service.recommendation_lifecycle(limit=max_records)
        holding_alerts = service.holding_alerts(now=now)
        execution_bias = service.execution_bias_report(days=30, limit=max_records)
        summary = {
            "watchlist": len(service._state.watchlist),
            "positions": len(service._portfolio.positions()),
            "trades": len(trades),
            "reconcile_history": len(reconcile_recent),
            "run_summaries": len(run_summaries),
            "latency_records": len(latency_recent),
            "audit_events": len(audit_recent),
            "lifecycle_records": _as_int(lifecycle.get("records"), default=0),
            "holding_alert_records": _as_int(holding_alerts.get("records"), default=0),
            "execution_bias_records_30d": _as_int(
                execution_bias.get("records"),
                default=0,
            ),
        }
        return {
            "archive_version": 1,
            "generated_at": now.isoformat(),
            "day": now.strftime("%Y-%m-%d"),
            "summary": summary,
            "runtime_state": service._default_runtime_state_payload(),
            "latest_signals": service.latest_signals_snapshot(),
            "portfolio": {
                "positions": service.portfolio_positions(),
                "trades": trades,
            },
            "recommendation_lifecycle": lifecycle,
            "holding_alerts": holding_alerts,
            "execution_bias_30d": execution_bias,
            "reconcile": {
                "latest": service._last_reconcile_report,
                "history": reconcile_recent,
            },
            "runtime": {
                "run_summaries": run_summaries,
                "latency_history_ms": latency_recent,
                "audit_events": audit_recent,
            },
        }

    def _purge_runtime_history_archives(self, *, now: datetime) -> list[str]:
        service = self._service
        archive_dir = service._runtime_history_archive_dir
        if not archive_dir.exists():
            return []
        retention_days = max(
            1,
            _as_int(
                service._config.command_channel.history_archive_retention_days,
                default=30,
            ),
        )
        cutoff = now.date() - timedelta(days=retention_days)
        purged: list[str] = []
        for path in archive_dir.glob("runtime_history_*.json"):
            day_token = path.stem.removeprefix("runtime_history_")
            if len(day_token) != 8 or not day_token.isdigit():
                continue
            try:
                file_day = datetime.strptime(day_token, "%Y%m%d").date()
            except ValueError:
                continue
            if file_day >= cutoff:
                continue
            try:
                path.unlink(missing_ok=True)
                purged.append(str(path))
            except OSError:
                continue
        purged.sort()
        return purged

    def audit_events(
        self,
        limit: int = 200,
        event_type: str = "",
        trace_id: str = "",
    ) -> dict[str, object]:
        service = self._service
        service._refresh_runtime_state_from_disk_if_changed()
        capped_limit = max(1, min(limit, 2000))
        filtered = service._audit_events
        normalized_type = event_type.strip().lower()
        normalized_trace = trace_id.strip()
        if normalized_type:
            filtered = [
                item
                for item in filtered
                if str(item.get("event_type", "")).strip().lower() == normalized_type
            ]
        if normalized_trace:
            filtered = [
                item
                for item in filtered
                if str(item.get("trace_id", "")).strip() == normalized_trace
            ]

        recent = filtered[-capped_limit:]
        return {
            "records": len(recent),
            "total_matched": len(filtered),
            "events": recent,
        }

    def trace_replay(self, trace_id: str) -> dict[str, object]:
        service = self._service
        normalized_trace = trace_id.strip()
        if not normalized_trace:
            return {"trace_id": "", "records": 0, "events": [], "summary": {}}

        events = [
            item
            for item in service._audit_events
            if str(item.get("trace_id", "")).strip() == normalized_trace
        ]
        ordered = sorted(events, key=_report_timestamp)
        type_breakdown: dict[str, int] = {}
        warning_events = 0
        for item in ordered:
            event_type = str(item.get("event_type", "")).strip() or "unknown"
            type_breakdown[event_type] = type_breakdown.get(event_type, 0) + 1
            if str(item.get("level", "")).lower() == "warn":
                warning_events += 1

        return {
            "trace_id": normalized_trace,
            "records": len(ordered),
            "events": ordered,
            "summary": {
                "event_types": type_breakdown,
                "warning_events": warning_events,
                "first_timestamp": str(ordered[0].get("timestamp", "")) if ordered else "",
                "last_timestamp": str(ordered[-1].get("timestamp", "")) if ordered else "",
            },
        }

    def runtime_status(
        self,
        *,
        include_learning_governance: bool = True,
    ) -> dict[str, object]:
        service = self._service
        service._refresh_runtime_state_from_disk_if_changed()
        latest = service.latest_report()
        learning_governance: dict[str, object]
        if include_learning_governance:
            try:
                learning_governance = service.learning_model_governance_status()
            except Exception as exc:
                logger.warning(
                    "runtime status degraded because learning governance status is unavailable",
                    exc_info=True,
                )
                learning_governance = {
                    "status": "degraded",
                    "reason": "learning_governance_status_unavailable",
                    "error_type": exc.__class__.__name__,
                    "detail": str(exc),
                }
        else:
            learning_governance = {
                "status": "skipped",
                "reason": "omitted_for_lightweight_health",
            }
        return {
            "state": asdict(service._state),
            "state_persistence": {
                "enabled": bool(service._config.command_channel.state_persist_enabled),
                "path": str(service._runtime_state_path),
            },
            "provider": service.provider_status(),
            "portfolio": {
                "open_positions": len(service._portfolio.positions()),
                "recent_trades": len(service._portfolio.trades(limit=100)),
            },
            "reconcile": {
                "broker_positions": len(service._broker_positions),
                "last_report": service._last_reconcile_report,
                "history_count": len(service._reconcile_history),
            },
            "audit": {
                "events_count": len(service._audit_events),
                "latest_event": service._audit_events[-1] if service._audit_events else None,
            },
            "acceptance": {
                "last_week4": service._last_week4_acceptance_report,
                "history_count": len(service._week4_acceptance_history),
            },
            "week5": {
                "last_scan": service._last_week5_scan_report,
                "history_count": len(service._week5_scan_history),
            },
            "week6": {
                "last_report": service._last_week6_report,
                "history_count": len(service._week6_history),
                "last_data_quality_report": service._last_week6_data_quality_report,
                "data_quality_history_count": len(service._week6_data_quality_history),
                "global_market_snapshot": service._global_market_snapshot,
                "global_market_history_count": len(service._global_market_history),
                "regulatory_watchlist_size": len(service._regulatory_watchlist),
            },
            "week7": {
                "strategy_kill_switch": service.strategy_kill_switch_status(),
                "performance_records": len(service._strategy_performance_history),
                "cloud_backup": service.cloud_backup_status(),
                "factor_lifecycle": service.factor_lifecycle_status(),
                "sim_broker_weekly_latest": service._last_week7_sim_broker_report,
                "sim_broker_weekly_history_count": len(service._week7_sim_broker_history),
            },
            "evolution": {
                "enabled": service._config.evolution.enabled,
                "dry_run": service._config.evolution.dry_run,
                "dry_run_policy": service._config.evolution.dry_run_policy,
                "dry_run_live_modes": list(service._config.evolution.dry_run_live_modes),
                "effective_dry_run": service._resolve_evolution_dry_run(requested=None)[0],
                "last_report": service._last_evolution_report,
                "history_count": len(service._evolution_history),
                "release_gate_latest": service._last_evolution_release_gate,
                "release_gate_history_count": len(service._evolution_release_gate_history),
                "release_approval_latest": service._last_evolution_release_approval,
                "release_approval_history_count": len(
                    service._evolution_release_approval_history
                ),
                "release_ticket_latest": service._last_evolution_release_ticket,
                "release_ticket_history_count": len(service._evolution_release_ticket_history),
                "release_confirmation_required": (
                    service._config.evolution.release_confirmation_required
                ),
                "release_confirmation_ttl_days": (
                    service._config.evolution.release_confirmation_ttl_days
                ),
                "release_confirmation_pending_count": (
                    service._evolution_pending_confirmation_count()
                ),
            },
            "learning_governance": learning_governance,
            "execution_risk": service.execution_risk_status(include_artifact_scan=False),
            "execution_aware": {
                "latest": service.latest_execution_aware_report(),
                "history_count": len(service._execution_aware_report_history),
            },
            "idle_queue": {
                "enabled": bool(service._resolve_idle_queue_enabled()[0]),
                "enabled_config": bool(service._config.idle_queue.enabled),
                "auto_run": bool(service._resolve_idle_queue_auto_run()[0]),
                "auto_run_config": bool(service._config.idle_queue.auto_run),
                "blocked_tasks_count": len(service._idle_blocked_tasks),
                "pending_manual_ack_count": sum(
                    1
                    for task_id in service._idle_blocked_tasks.keys()
                    if task_id not in service._idle_manual_ack_grants
                ),
                "pause_flag_active": bool(service._idle_resource_pause_active),
                "wd_report_deadline_hit_rate": round(
                    (
                        service._idle_wd_report_deadline_hits
                        / service._idle_wd_report_runs
                        if service._idle_wd_report_runs > 0
                        else 0.0
                    ),
                    6,
                ),
                "history_records": len(service._idle_history),
                "latest_report": service._last_idle_report,
            },
            "notification_filter_diagnostics": service.latest_notification_filter_diagnostics(),
            "training_bootstrap": service.training_bootstrap_status(),
            "sla": service.sla_report(),
            "latest_report": latest,
        }

    def learning_protocol_status(self, manifest_limit: int = 5) -> dict[str, object]:
        service = self._service
        sample_counts = service._sample_store.counts()
        outcomes = service._sample_store.list_outcomes()
        manifests = service._sample_store.list_manifests(limit=max(1, manifest_limit))

        maturity_breakdown: dict[str, int] = {}
        fidelity_breakdown: dict[str, int] = {}
        for outcome in outcomes:
            maturity = outcome.maturity_status.value
            maturity_breakdown[maturity] = maturity_breakdown.get(maturity, 0) + 1
            fidelity = (
                outcome.backfill_fidelity_tier.value
                if outcome.backfill_fidelity_tier is not None
                else "unknown"
            )
            fidelity_breakdown[fidelity] = fidelity_breakdown.get(fidelity, 0) + 1

        manifest_rows: list[dict[str, object]] = []
        for manifest in manifests:
            manifest_rows.append(
                {
                    "dataset_manifest_id": manifest.dataset_manifest_id,
                    "feature_schema_id": manifest.feature_schema_id,
                    "label_policy_id": manifest.label_policy_id,
                    "included_snapshot_count": manifest.included_snapshot_count,
                    "included_outcome_count": manifest.included_outcome_count,
                    "fidelity_breakdown": dict(manifest.fidelity_breakdown),
                    "generated_at": manifest.generated_at.isoformat(),
                    "split_counts": {
                        item.split_name: int(item.row_count) for item in manifest.split_plan
                    },
                }
            )

        learning_events = [
            item
            for item in reversed(service._audit_events)
            if str(item.get("event_type", "")).strip().lower().startswith("learning_backfill_")
        ][:10]
        latest_event_summary: dict[str, object] | None = None
        if learning_events:
            latest_event = learning_events[0]
            payload = latest_event.get("payload", {})
            if not isinstance(payload, dict):
                payload = {}
            latest_event_summary = {
                "event_type": str(latest_event.get("event_type", "")),
                "timestamp": str(latest_event.get("timestamp", "")),
                "level": str(latest_event.get("level", "")),
                "mode": str(payload.get("mode", "")),
                "ok": bool(payload.get("ok", False)),
                "dataset_manifest_id": str(payload.get("dataset_manifest_id", "")),
                "processed_archives": _as_int(payload.get("processed_archives"), default=0),
                "errors": [
                    str(item).strip() for item in payload.get("errors", []) if str(item).strip()
                ]
                if isinstance(payload.get("errors"), list)
                else [],
            }

        return {
            "sample_store": sample_counts,
            "outcomes": {
                "records": len(outcomes),
                "maturity_breakdown": maturity_breakdown,
                "fidelity_breakdown": fidelity_breakdown,
            },
            "manifests": {
                "records": len(manifest_rows),
                "items": manifest_rows,
                "latest": manifest_rows[0] if manifest_rows else None,
            },
            "latest_learning_backfill": latest_event_summary,
            "governance": service.learning_model_governance_status(
                proposal_limit=max(1, manifest_limit),
                ticket_limit=max(1, manifest_limit),
            ),
            "cold_start_ready": (
                sample_counts.get("signal_snapshots", 0) > 0
                and sample_counts.get("outcome_records", 0) > 0
                and sample_counts.get("dataset_manifests", 0) > 0
            ),
        }

    def learning_store_status(self) -> dict[str, object]:
        service = self._service
        sample_counts = service._sample_store.counts()
        outcomes = service._sample_store.list_outcomes()
        feature_schema_records = service._feature_schema_registry.list_records()
        label_policy_records = service._label_policy_registry.list_records()
        manifests = service._sample_store.list_manifests(limit=1)
        maturity_breakdown, fidelity_breakdown = _outcome_breakdowns(outcomes)
        snapshot_count = sample_counts.get("signal_snapshots", 0)
        outcome_count = sample_counts.get("outcome_records", 0)
        manifest_count = sample_counts.get("dataset_manifests", 0)
        latest_manifest = manifests[0] if manifests else None
        status = "empty"
        if snapshot_count > 0 or outcome_count > 0:
            status = "warming"
        if snapshot_count > 0 and outcome_count > 0 and manifest_count > 0:
            status = "ready"
        return {
            "status": status,
            "db_path": str(service._learning_protocol_db_path),
            "db_exists": service._learning_protocol_db_path.exists(),
            "writer_mode": "single_writer_duckdb",
            "sample_store": sample_counts,
            "outcomes": {
                "records": len(outcomes),
                "maturity_breakdown": maturity_breakdown,
                "fidelity_breakdown": fidelity_breakdown,
            },
            "registries": {
                "feature_schema_records": len(feature_schema_records),
                "label_policy_records": len(label_policy_records),
                "latest_feature_schema_id": (
                    feature_schema_records[-1].feature_schema_id if feature_schema_records else ""
                ),
                "latest_label_policy_id": (
                    label_policy_records[-1].label_policy_id if label_policy_records else ""
                ),
            },
            "latest_manifest": (
                {
                    "dataset_manifest_id": latest_manifest.dataset_manifest_id,
                    "feature_schema_id": latest_manifest.feature_schema_id,
                    "label_policy_id": latest_manifest.label_policy_id,
                    "generated_at": latest_manifest.generated_at.isoformat(),
                }
                if latest_manifest is not None
                else None
            ),
            "cold_start_ready": (
                snapshot_count > 0 and outcome_count > 0 and manifest_count > 0
            ),
        }

    def learning_store_metrics(self) -> dict[str, object]:
        service = self._service
        sample_counts = service._sample_store.counts()
        outcomes = service._sample_store.list_outcomes()
        snapshots = service._sample_store.list_snapshots()
        maturity_breakdown, fidelity_breakdown = _outcome_breakdowns(outcomes)
        snapshot_count = sample_counts.get("signal_snapshots", 0)
        outcome_count = sample_counts.get("outcome_records", 0)
        fully_matured_count = maturity_breakdown.get("fully_matured", 0)
        gold_count = fidelity_breakdown.get("gold", 0)
        data_quality_values = [
            float(snapshot.data_quality_score)
            for snapshot in snapshots
            if snapshot.data_quality_score is not None
        ]
        sample_weight_values = [float(snapshot.sample_weight) for snapshot in snapshots]
        min_train_samples = max(1, _as_int(service._config.training.min_samples, default=1))
        min_bucket_samples = max(
            1,
            _as_int(service._config.evolution.runtime_spec.min_samples_per_bucket, default=1),
        )
        return {
            "coverage": {
                "snapshot_to_outcome_ratio": round(outcome_count / max(snapshot_count, 1), 6),
                "manifest_to_snapshot_ratio": round(
                    sample_counts.get("dataset_manifests", 0) / max(snapshot_count, 1),
                    6,
                ),
            },
            "maturity": {
                "records": outcome_count,
                "fully_matured_count": fully_matured_count,
                "fully_matured_ratio": round(fully_matured_count / max(outcome_count, 1), 6),
                "maturity_breakdown": maturity_breakdown,
            },
            "fidelity": {
                "gold_count": gold_count,
                "gold_ratio": round(gold_count / max(outcome_count, 1), 6),
                "fidelity_breakdown": fidelity_breakdown,
            },
            "snapshots": {
                "records": snapshot_count,
                "mean_data_quality_score": round(
                    sum(data_quality_values) / max(len(data_quality_values), 1),
                    6,
                )
                if data_quality_values
                else 0.0,
                "mean_sample_weight": round(
                    sum(sample_weight_values) / max(len(sample_weight_values), 1),
                    6,
                )
                if sample_weight_values
                else 0.0,
            },
            "promotion_readiness": {
                "min_train_samples": min_train_samples,
                "min_samples_per_bucket": min_bucket_samples,
                "fully_matured_samples": fully_matured_count,
                "meets_min_train_samples": fully_matured_count >= min_train_samples,
                "cold_start_ready": (
                    snapshot_count > 0
                    and outcome_count > 0
                    and sample_counts.get("dataset_manifests", 0) > 0
                ),
            },
        }

    def learning_manifests_status(self, manifest_limit: int = 20) -> dict[str, object]:
        service = self._service
        manifests = service._sample_store.list_manifests(limit=max(1, manifest_limit))
        items: list[dict[str, object]] = []
        for manifest in manifests:
            items.append(
                {
                    "dataset_manifest_id": manifest.dataset_manifest_id,
                    "feature_schema_id": manifest.feature_schema_id,
                    "feature_schema_hash": manifest.feature_schema_hash,
                    "label_policy_id": manifest.label_policy_id,
                    "label_policy_hash": manifest.label_policy_hash,
                    "sample_selection_rule": manifest.sample_selection_rule,
                    "time_window_start": (
                        manifest.time_window_start.isoformat()
                        if manifest.time_window_start is not None
                        else ""
                    ),
                    "time_window_end": (
                        manifest.time_window_end.isoformat()
                        if manifest.time_window_end is not None
                        else ""
                    ),
                    "included_snapshot_count": manifest.included_snapshot_count,
                    "included_outcome_count": manifest.included_outcome_count,
                    "fidelity_breakdown": dict(manifest.fidelity_breakdown),
                    "dropped_reason_breakdown": dict(manifest.dropped_reason_breakdown),
                    "split_plan": [
                        {
                            "split_name": entry.split_name,
                            "selector": entry.selector,
                            "row_count": int(entry.row_count),
                            "start_time": (
                                entry.start_time.isoformat()
                                if entry.start_time is not None
                                else ""
                            ),
                            "end_time": (
                                entry.end_time.isoformat() if entry.end_time is not None else ""
                            ),
                        }
                        for entry in manifest.split_plan
                    ],
                    "generated_at": manifest.generated_at.isoformat(),
                }
            )
        return {
            "records": len(items),
            "items": items,
            "latest": items[0] if items else None,
        }

    def model_registry_status(self, limit: int = 20) -> dict[str, object]:
        service = self._service
        records = service._model_registry.list_records()
        latest_records = service._model_registry.list_records(limit=max(1, limit))
        role_breakdown: dict[str, int] = {}
        lifecycle_breakdown: dict[str, int] = {}
        for record in records:
            role = record.role.value
            lifecycle = record.lifecycle_state.value
            role_breakdown[role] = role_breakdown.get(role, 0) + 1
            lifecycle_breakdown[lifecycle] = lifecycle_breakdown.get(lifecycle, 0) + 1
        champion = service._model_registry.active_champion()
        items = [
            {
                "model_id": record.model_id,
                "role": record.role.value,
                "lifecycle_state": record.lifecycle_state.value,
                "artifact_uri": record.artifact_uri,
                "dataset_manifest_id": record.dataset_manifest_id,
                "feature_schema_id": record.feature_schema_id,
                "label_policy_id": record.label_policy_id,
                "blocked_reason": record.blocked_reason,
                "updated_at": record.updated_at.isoformat(),
            }
            for record in latest_records
        ]
        return {
            "records": len(records),
            "role_breakdown": role_breakdown,
            "lifecycle_breakdown": lifecycle_breakdown,
            "active_champion": (
                {
                    "model_id": champion.model_id,
                    "artifact_uri": champion.artifact_uri,
                    "dataset_manifest_id": champion.dataset_manifest_id,
                    "updated_at": champion.updated_at.isoformat(),
                }
                if champion is not None
                else None
            ),
            "items": items,
            "latest": items[0] if items else None,
        }

    def shadow_v2_status(self, limit: int = 20) -> dict[str, object]:
        service = self._service
        recent_events = [
            item
            for item in reversed(service._audit_events)
            if str(item.get("event_type", "")).strip().lower() == "shadow_online_v2_report_built"
        ][: max(1, limit)]
        items: list[dict[str, object]] = []
        status_breakdown: dict[str, int] = {}
        for event in recent_events:
            payload = event.get("payload", {})
            mapped = payload if isinstance(payload, dict) else {}
            status = str(mapped.get("status", "")).strip() or "unknown"
            status_breakdown[status] = status_breakdown.get(status, 0) + 1
            items.append(
                {
                    "timestamp": str(event.get("timestamp", "")),
                    "report_id": str(mapped.get("report_id", "")),
                    "shadow_model_id": str(mapped.get("shadow_model_id", "")),
                    "champion_model_id": str(mapped.get("champion_model_id", "")),
                    "dataset_manifest_id": str(mapped.get("dataset_manifest_id", "")),
                    "row_count": _as_int(mapped.get("row_count"), default=0),
                    "status": status,
                }
            )
        latest = items[0] if items else None
        return {
            "records": len(items),
            "status": str(latest.get("status", "")) if latest is not None else "no_data",
            "status_breakdown": status_breakdown,
            "latest": latest,
            "items": items,
        }

    def m3_profile_status(self) -> dict[str, object]:
        service = self._service
        orchestrator = service._evolution_orchestrator
        profile_payload = orchestrator._build_m3_profile_payload()
        store = orchestrator._m3_store
        snapshot_count = sum(
            1
            for path in store._snapshot_dir.iterdir()
            if path.is_dir() and ".pending." not in path.name
        )
        pending_delete_count = sum(1 for _ in store._quarantine_dir.iterdir())
        meta_payload: dict[str, object] = {}
        if store._meta_path.exists():
            try:
                loaded = json.loads(store._meta_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    meta_payload = cast(dict[str, object], loaded)
            except (OSError, ValueError, TypeError):
                meta_payload = {}
        return {
            "status": "ready" if store.count() > 0 else "empty",
            "profile": profile_payload,
            "store": {
                "base_dir": str(orchestrator._m3_store_dir),
                "active_store_dir": str(store._store_dir),
                "meta_path": str(store._meta_path),
                "vectors_path": str(store._vectors_path),
                "vector_count": store.count(),
                "snapshot_count": snapshot_count,
                "pending_delete_count": pending_delete_count,
                "meta_exists": store._meta_path.exists(),
                "updated_at": str(meta_payload.get("updated_at", "")),
            },
        }

    def runtime_stage_snapshot(self, now: datetime | None = None) -> dict[str, object]:
        service = self._service
        service._refresh_runtime_state_from_disk_if_changed()
        current = now or datetime.now()
        phase = _resolve_runtime_phase(current)
        scheduler_state = service._scheduler.export_state()
        warehouse_history = service.market_warehouse_history(
            limit=max(
                5,
                min(50, _as_int(service._config.market_warehouse.history_limit, default=30)),
            )
        )
        warehouse_lock = service.market_warehouse_sync_lock_status()
        warehouse_progress = _resolve_market_warehouse_stage_progress(
            latest_progress=service.latest_market_warehouse_progress(),
            lock_status=warehouse_lock,
        )
        latest_warehouse_report = service.latest_market_warehouse_report()
        warehouse_report = _resolve_market_warehouse_stage_report(
            latest_report=latest_warehouse_report,
            history_items=warehouse_history.get("items"),
        )
        warehouse_background_data = service.market_warehouse_background_data_status()
        warehouse_followup_state = service.latest_post_market_warehouse_followup_state()
        warehouse_followup_result = service.latest_post_market_warehouse_followup_result()
        warehouse_resume_action = _resolve_market_warehouse_resume_action(
            service=service,
            latest_report=latest_warehouse_report,
            lock_status=warehouse_lock,
            followup_state=warehouse_followup_state,
        )
        tdx_report = service.latest_tdx_sync_report()
        reconcile_report = service.latest_reconcile_report()
        week6_daily_report = service.latest_week6_report()
        week6_prewarm_report = _resolve_week6_data_quality_report(
            latest_data_quality=service.latest_week6_data_quality_report(),
            latest_week6=week6_daily_report,
        )
        evolution_report = service.latest_evolution_report()
        acceptance_report = service.latest_week4_acceptance_report()
        idle_state = service.idle_queue_state()
        idle_report = service.latest_idle_queue_report()
        cloud_backup = service.cloud_backup_status(now=current)
        provider_status = service.provider_status()
        week5_report = service.latest_week5_scan_report()
        today = current.date().isoformat()
        last_run_state = scheduler_state.get("last_run")
        last_run_map = last_run_state if isinstance(last_run_state, dict) else {}
        last_interval_state = scheduler_state.get("last_interval_slot")
        last_interval_map = (
            last_interval_state if isinstance(last_interval_state, dict) else {}
        )

        tasks = [
            self._build_daily_stage_task(
                name="close_reconcile",
                label="收盘对账",
                scheduled_time=str(service._config.scheduler.close_reconcile_time).strip(),
                latest_time="15:40",
                current=current,
                last_run_map=last_run_map,
                report=reconcile_report,
                enabled=True,
                run_on_weekends=False,
                report_status_keys=("status",),
                report_timestamp_keys=(
                    "timestamp",
                    "generated_at",
                    "completed_at",
                    "updated_at",
                ),
                category="盘后",
            ),
            self._build_daily_stage_task(
                name="market_warehouse_sync",
                label="基础库增量同步",
                scheduled_time=str(service._config.market_warehouse.run_time).strip(),
                latest_time="23:59",
                current=current,
                last_run_map=last_run_map,
                report=warehouse_report,
                run_on_weekends=False,
                enabled=bool(
                    service._config.market_warehouse.enabled
                    and service._config.market_warehouse.auto_run
                ),
                category="盘后",
                report_status_keys=("status",),
                report_timestamp_keys=("timestamp", "updated_at", "completed_at"),
                running_snapshot=warehouse_progress,
                disabled_reason=(
                    "market_warehouse.enabled=false 或 auto_run=false"
                    if not (
                        service._config.market_warehouse.enabled
                        and service._config.market_warehouse.auto_run
                    )
                    else ""
                ),
            ),
            self._build_daily_stage_task(
                name="tdx_offline_sync",
                label="TDX 离线包同步",
                scheduled_time=str(service._config.tdx_sync.run_time).strip(),
                latest_time="23:59",
                current=current,
                last_run_map=last_run_map,
                report=tdx_report,
                run_on_weekends=False,
                enabled=bool(
                    service._config.tdx_sync.enabled
                    and service._config.tdx_sync.auto_run
                    and str(service._config.tdx_sync.vipdoc_root).strip()
                ),
                category="盘后",
                report_status_keys=("status",),
                report_timestamp_keys=("timestamp", "updated_at", "completed_at"),
                disabled_reason=(
                    "当前主流程走 market_warehouse 增量；TDX 仅低频补库/兜底"
                    if not (
                        service._config.tdx_sync.enabled
                        and service._config.tdx_sync.auto_run
                        and str(service._config.tdx_sync.vipdoc_root).strip()
                    )
                    else ""
                ),
            ),
            self._build_daily_stage_task(
                name="week6_daily",
                label="Week6 日报",
                scheduled_time=(
                    str(service._config.week6.run_time).strip()
                    or str(service._config.scheduler.week6_daily_time).strip()
                ),
                latest_time="16:00",
                current=current,
                last_run_map=last_run_map,
                report=week6_daily_report,
                run_on_weekends=False,
                enabled=bool(service._config.week6.enabled and service._config.week6.auto_run),
                category="盘后",
                report_status_keys=("status",),
                report_timestamp_keys=("timestamp", "updated_at", "generated_at"),
                disabled_reason=(
                    "week6.enabled=false 或 auto_run=false"
                    if not (service._config.week6.enabled and service._config.week6.auto_run)
                    else ""
                ),
            ),
            self._build_daily_stage_task(
                name="week6_data_prewarm",
                label="Week6 数据预热",
                scheduled_time=str(service._config.week6.data_prewarm_time).strip(),
                latest_time="23:59",
                current=current,
                last_run_map=last_run_map,
                report=week6_prewarm_report,
                run_on_weekends=False,
                enabled=bool(
                    service._config.week6.enabled
                    and service._config.week6.auto_run
                    and service._config.week6.data_prewarm_enabled
                ),
                category="盘后",
                report_status_keys=("status",),
                report_timestamp_keys=("timestamp", "updated_at", "generated_at"),
                disabled_reason=(
                    "week6.data_prewarm_enabled=false"
                    if not (
                        service._config.week6.enabled
                        and service._config.week6.auto_run
                        and service._config.week6.data_prewarm_enabled
                    )
                    else ""
                ),
            ),
            self._build_daily_stage_task(
                name="evolution_offhours",
                label="Evolution 盘后演化",
                scheduled_time=str(service._config.evolution.offhours_time).strip(),
                latest_time="23:59",
                current=current,
                last_run_map=last_run_map,
                report=evolution_report,
                enabled=bool(
                    service._config.evolution.enabled and service._config.evolution.auto_run
                ),
                category="夜间",
                report_status_keys=("status", "overall"),
                report_timestamp_keys=("timestamp", "updated_at", "completed_at"),
                disabled_reason=(
                    "evolution.enabled=false 或 auto_run=false"
                    if not (
                        service._config.evolution.enabled
                        and service._config.evolution.auto_run
                    )
                    else ""
                ),
            ),
            self._build_daily_stage_task(
                name="week4_acceptance",
                label="Week4 验收",
                scheduled_time=str(service._config.scheduler.week4_acceptance_time).strip(),
                latest_time="23:59",
                current=current,
                last_run_map=last_run_map,
                report=acceptance_report,
                enabled=bool(
                    service._config.acceptance.enabled and service._config.acceptance.auto_run
                ),
                category="夜间",
                report_status_keys=("overall", "status"),
                report_timestamp_keys=("timestamp", "updated_at", "generated_at"),
                disabled_reason=(
                    "acceptance.enabled=false 或 auto_run=false"
                    if not (
                        service._config.acceptance.enabled
                        and service._config.acceptance.auto_run
                    )
                    else ""
                ),
            ),
            self._build_interval_stage_task(
                name="idle_queue_tick",
                label="Idle Queue Tick",
                interval_minutes=max(
                    1,
                    _as_int(service._config.idle_queue.dispatch_interval_minutes, 5),
                ),
                current=current,
                last_interval_map=last_interval_map,
                enabled=bool(
                    idle_state.get("enabled", False)
                    and idle_state.get("auto_run", False)
                ),
                report=idle_report,
                category="后台守护",
                disabled_reason=(
                    str(idle_state.get("enabled_reason", "")).strip()
                    or str(idle_state.get("auto_run_reason", "")).strip()
                ),
            ),
            self._build_interval_stage_task(
                name="week7_cloud_backup_watchdog",
                label="Cloud Backup Watchdog",
                interval_minutes=max(
                    1,
                    _as_int(service._config.cloud_backup.ping_interval_min, 10),
                ),
                current=current,
                last_interval_map=last_interval_map,
                enabled=bool(service._config.cloud_backup.enabled),
                report=cloud_backup,
                category="后台守护",
                disabled_reason=(
                    "cloud_backup.enabled=false"
                    if not service._config.cloud_backup.enabled
                    else ""
                ),
            ),
        ]

        for item in tasks:
            if (
                str(item.get("name", "")).strip() == "week4_acceptance"
                and str(item.get("status", "")).strip().lower() == "disabled"
            ):
                item["detail"] = _disabled_flags_reason(
                    "acceptance",
                    enabled=bool(service._config.acceptance.enabled),
                    auto_run=bool(service._config.acceptance.auto_run),
                ) or str(item.get("detail", "")).strip()

        counts: dict[str, int] = {}
        for item in tasks:
            status = str(item.get("status", "")).strip().lower() or "unknown"
            counts[status] = counts.get(status, 0) + 1

        current_stage = self._resolve_current_stage(
            current=current,
            phase=phase,
            tasks=tasks,
            warehouse_progress=warehouse_progress,
            warehouse_followup_state=warehouse_followup_state,
        )
        next_task = _select_next_task(tasks=tasks, current=current)
        latest_activity = _resolve_latest_stage_activity(
            tasks=tasks,
            warehouse_progress=warehouse_progress,
            warehouse_followup_state=warehouse_followup_state,
        )
        health = _build_runtime_stage_health(
            current=current,
            provider_status=provider_status,
            acceptance_report=acceptance_report,
            week5_report=week5_report,
            pause_new_buy=bool(service._state.pause_new_buy),
            latest_activity=latest_activity,
        )
        return {
            "as_of": current.isoformat(),
            "today": today,
            "runtime_phase": phase,
            "system_stage": current_stage,
            "health": health,
            "summary": {
                "mode": str(service._config.app.mode).strip(),
                "counts": counts,
                "tasks_total": len(tasks),
                "pending_next": next_task,
            },
            "tasks": tasks,
            "latest_activity": latest_activity,
            "market_warehouse_progress": warehouse_progress,
            "market_warehouse_lock": warehouse_lock,
            "market_warehouse_background_data": warehouse_background_data,
            "market_warehouse_followup_state": warehouse_followup_state,
            "market_warehouse_followup_result": warehouse_followup_result,
            "market_warehouse_resume_action": warehouse_resume_action,
            "idle_queue": {
                "enabled": bool(idle_state.get("enabled", False)),
                "auto_run": bool(idle_state.get("auto_run", False)),
                "blocked_tasks": len(
                    idle_state.get("blocked_tasks", {})
                    if isinstance(idle_state.get("blocked_tasks", {}), dict)
                    else {}
                ),
                "pending_manual_ack": len(
                    idle_state.get("pending_manual_ack", [])
                    if isinstance(idle_state.get("pending_manual_ack", []), list)
                    else []
                ),
            },
            "scheduler_state": scheduler_state,
        }

    def _build_daily_stage_task(
        self,
        *,
        name: str,
        label: str,
        scheduled_time: str,
        latest_time: str,
        current: datetime,
        last_run_map: dict[str, object],
        report: dict[str, object] | None,
        enabled: bool,
        category: str,
        report_status_keys: tuple[str, ...] = ("status",),
        report_timestamp_keys: tuple[str, ...] = ("timestamp", "updated_at"),
        running_snapshot: dict[str, object] | None = None,
        disabled_reason: str = "",
        run_on_weekends: bool = True,
    ) -> dict[str, object]:
        report_timestamp = _extract_report_timestamp(report, *report_timestamp_keys)
        report_status = _extract_report_status(report, *report_status_keys)
        report_day = report_timestamp[:10] if len(report_timestamp) >= 10 else ""
        last_run = str(last_run_map.get(name, "")).strip()
        current_hhmm = current.strftime("%H:%M")
        is_running = _snapshot_running(running_snapshot)
        if not enabled:
            status = "disabled"
            detail = disabled_reason or "未启用"
        elif is_running:
            status = "running"
            detail = _running_snapshot_detail(running_snapshot)
        elif report_day == current.date().isoformat():
            status = _report_status_to_stage_status(report_status, default="done")
            detail = _report_status_detail(report_status, report)
        elif _is_weekend(current) and not run_on_weekends:
            status = "skipped"
            detail = "周末不自动执行"
        elif current_hhmm < scheduled_time:
            status = "pending"
            detail = "等待计划时间"
        elif latest_time and current_hhmm > latest_time:
            status = "expired"
            detail = "超过最晚执行窗口"
        else:
            status = "due"
            detail = "到点待执行"
        return {
            "name": name,
            "label": label,
            "type": "daily",
            "category": category,
            "status": status,
            "status_label": _stage_status_label(status),
            "scheduled_time": scheduled_time,
            "latest_time": latest_time,
            "current_hhmm": current_hhmm,
            "last_run_day": last_run,
            "report_timestamp": report_timestamp,
            "report_status": report_status,
            "detail": detail,
        }

    def _build_interval_stage_task(
        self,
        *,
        name: str,
        label: str,
        interval_minutes: int,
        current: datetime,
        last_interval_map: dict[str, object],
        enabled: bool,
        report: dict[str, object] | None,
        category: str,
        disabled_reason: str = "",
    ) -> dict[str, object]:
        slot_info = last_interval_map.get(name)
        last_slot_date = ""
        last_slot_value = -1
        if isinstance(slot_info, dict):
            last_slot_date = str(slot_info.get("date", "")).strip()
            last_slot_value = _as_int(slot_info.get("slot"), default=-1)
        report_timestamp = _extract_report_timestamp(
            report,
            "timestamp",
            "updated_at",
            "checked_at",
            "generated_at",
        )
        report_status = _extract_report_status(report, "status", "overall")
        current_slot = current.hour * 60 + current.minute
        last_slot_age = current_slot - last_slot_value if last_slot_value >= 0 else -1
        if not enabled:
            status = "disabled"
            detail = disabled_reason or "未启用"
        elif (
            last_slot_date == current.date().isoformat()
            and 0 <= last_slot_age <= interval_minutes
        ):
            status = "active"
            detail = f"{interval_minutes} 分钟内已执行"
        else:
            status = "pending"
            detail = "等待下一个轮询槽位"
        if report_status and status != "disabled":
            normalized = _report_status_to_stage_status(report_status, default=status)
            if normalized in {"failed", "partial"}:
                status = normalized
                detail = _report_status_detail(report_status, report)
        return {
            "name": name,
            "label": label,
            "type": "interval",
            "category": category,
            "status": status,
            "status_label": _stage_status_label(status),
            "interval_minutes": interval_minutes,
            "last_slot_date": last_slot_date,
            "last_slot_value": last_slot_value,
            "report_timestamp": report_timestamp,
            "report_status": report_status,
            "detail": detail,
        }

    def _resolve_current_stage(
        self,
        *,
        current: datetime,
        phase: dict[str, object],
        tasks: list[dict[str, object]],
        warehouse_progress: dict[str, object] | None,
        warehouse_followup_state: dict[str, object] | None,
    ) -> dict[str, object]:
        if _snapshot_running(warehouse_progress):
            return {
                "code": "market_warehouse_sync",
                "label": "盘后基础库同步中",
                "detail": _running_snapshot_detail(warehouse_progress),
            }
        if _post_market_warehouse_followup_running(warehouse_followup_state):
            return {
                "code": "post_market_warehouse_followup",
                "label": "Post-market warehouse followup running",
                "detail": _post_market_warehouse_followup_detail(warehouse_followup_state),
            }
        for item in tasks:
            if str(item.get("status", "")).strip().lower() == "running":
                return {
                    "code": str(item.get("name", "")).strip(),
                    "label": f"{item.get('label', '')} 进行中",
                    "detail": str(item.get("detail", "")).strip(),
                }
        for item in tasks:
            if str(item.get("status", "")).strip().lower() == "due":
                return {
                    "code": "due_task_waiting",
                    "label": f"待执行：{item.get('label', '')}",
                    "detail": str(item.get("detail", "")).strip(),
                }
        next_task = _select_next_task(tasks=tasks, current=current)
        if next_task is not None:
            return {
                "code": "waiting_next_job",
                "label": f"等待下一阶段：{next_task.get('label', '')}",
                "detail": str(next_task.get("scheduled_time", "")).strip()
                or str(next_task.get("interval_minutes", "")).strip(),
            }
        return {
            "code": str(phase.get("code", "")).strip() or "idle",
            "label": str(phase.get("label", "")).strip() or "空闲",
            "detail": str(phase.get("detail", "")).strip(),
        }


@lru_cache(maxsize=1)
def _runtime_service_module() -> Any:
    return import_module("stock_analyzer.runtime.service")


def _as_int(value: object, default: int) -> int:
    return cast(int, _runtime_service_module()._as_int(value, default))


def _outcome_breakdowns(outcomes: list[object]) -> tuple[dict[str, int], dict[str, int]]:
    maturity_breakdown: dict[str, int] = {}
    fidelity_breakdown: dict[str, int] = {}
    for outcome in outcomes:
        raw_maturity = getattr(outcome, "maturity_status", None)
        maturity = str(getattr(raw_maturity, "value", raw_maturity or "unknown")).strip().lower()
        maturity_breakdown[maturity] = maturity_breakdown.get(maturity, 0) + 1
        raw_fidelity = getattr(outcome, "backfill_fidelity_tier", None)
        fidelity = str(getattr(raw_fidelity, "value", raw_fidelity or "unknown")).strip().lower()
        fidelity_breakdown[fidelity] = fidelity_breakdown.get(fidelity, 0) + 1
    return maturity_breakdown, fidelity_breakdown


def _report_timestamp(report: dict[str, object]) -> float:
    return cast(float, _runtime_service_module()._report_timestamp(report))


def _is_weekend(current: datetime) -> bool:
    return current.weekday() >= 5


def _resolve_runtime_phase(current: datetime) -> dict[str, object]:
    hhmm = current.strftime("%H:%M")
    current_time = current.time()
    if _is_weekend(current):
        if current_time < time(hour=20, minute=30):
            return {
                "code": "weekend_day",
                "label": "周末",
                "detail": "非交易日，以维护、补库和周末扫描为主",
                "hhmm": hhmm,
            }
        return {
            "code": "weekend_night",
            "label": "周末夜间",
            "detail": "非交易日晚间窗口，关注演化、验收与守护任务",
            "hhmm": hhmm,
        }
    if current_time < time(hour=9, minute=15):
        return {
            "code": "premarket",
            "label": "盘前",
            "detail": "开盘前准备阶段",
            "hhmm": hhmm,
        }
    if current_time < time(hour=11, minute=31):
        return {
            "code": "intraday_am",
            "label": "盘中（上午）",
            "detail": "交易时段，偏实时链路",
            "hhmm": hhmm,
        }
    if current_time < time(hour=13, minute=0):
        return {
            "code": "midday",
            "label": "午间",
            "detail": "午间整理阶段",
            "hhmm": hhmm,
        }
    if current_time < time(hour=15, minute=1):
        return {
            "code": "intraday_pm",
            "label": "盘中（下午）",
            "detail": "交易时段，偏实时链路",
            "hhmm": hhmm,
        }
    if current_time < time(hour=20, minute=30):
        return {
            "code": "post_close",
            "label": "盘后",
            "detail": "盘后批处理与增量同步窗口",
            "hhmm": hhmm,
        }
    return {
        "code": "night",
        "label": "夜间",
        "detail": "夜间验收/演化/守护阶段",
        "hhmm": hhmm,
    }


def _extract_report_timestamp(report: dict[str, object] | None, *keys: str) -> str:
    if not isinstance(report, dict):
        return ""
    for key in keys:
        value = report.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_report_status(report: dict[str, object] | None, *keys: str) -> str:
    if not isinstance(report, dict):
        return ""
    for key in keys:
        value = report.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _report_status_to_stage_status(status: str, default: str) -> str:
    normalized = status.strip().lower()
    if normalized in {"ok", "healthy", "pass", "pass_with_warnings", "success"}:
        return "done"
    if normalized in {"partial", "warning", "warn"}:
        return "partial"
    if normalized in {"failed", "error", "blocked"}:
        return "failed"
    if normalized in {"running", "in_progress"}:
        return "running"
    if normalized in {"skipped", "noop"}:
        return "skipped"
    return default


def _disabled_flags_reason(prefix: str, **flags: bool) -> str:
    reasons = [f"{prefix}.{name}=false" for name, value in flags.items() if not value]
    return ", ".join(reasons)


def _report_status_detail(status: str, report: dict[str, object] | None) -> str:
    normalized = status.strip().lower()
    if normalized:
        label = {
            "ok": "执行成功",
            "healthy": "状态健康",
            "pass": "验收通过",
            "pass_with_warnings": "验收通过（含警告）",
            "partial": "部分完成",
            "failed": "执行失败",
            "error": "执行异常",
            "blocked": "被阻断",
            "skipped": "本轮跳过",
        }.get(normalized, status)
    else:
        label = ""
    if not isinstance(report, dict):
        return label
    reason = str(report.get("reason", "")).strip()
    if reason:
        return f"{label}：{reason}" if label else reason
    return label or "已记录报告"


def _snapshot_running(snapshot: dict[str, object] | None) -> bool:
    if not isinstance(snapshot, dict):
        return False
    return str(snapshot.get("status", "")).strip().lower() == "running"


def _running_snapshot_detail(snapshot: dict[str, object] | None) -> str:
    if not isinstance(snapshot, dict):
        return "运行中"
    phase = str(snapshot.get("phase", "")).strip()
    completed = _as_int(snapshot.get("symbols_completed"), default=0)
    total = _as_int(snapshot.get("symbols_total"), default=0)
    current_symbol = str(snapshot.get("current_symbol", "")).strip()
    pieces: list[str] = []
    if phase:
        pieces.append(f"阶段 {phase}")
    if total > 0:
        pieces.append(f"{completed}/{total}")
    if current_symbol:
        pieces.append(current_symbol)
    return " | ".join(pieces) if pieces else "运行中"


def _running_snapshot_detail(snapshot: dict[str, object] | None) -> str:
    if not isinstance(snapshot, dict):
        return "运行中"
    phase = str(snapshot.get("phase", "")).strip()
    completed = _as_int(snapshot.get("symbols_completed"), default=0)
    total = _as_int(snapshot.get("symbols_total"), default=0)
    current_symbol = str(snapshot.get("current_symbol", "")).strip()
    lock = snapshot.get("lock")
    pieces: list[str] = []
    if phase:
        pieces.append(f"阶段 {phase}")
    if total > 0:
        pieces.append(f"{completed}/{total}")
    if current_symbol:
        pieces.append(current_symbol)
    if isinstance(lock, dict) and bool(lock.get("running", False)):
        age_sec = _as_int(lock.get("age_sec"), default=0)
        trace_id = str(lock.get("trace_id", "")).strip()
        pieces.append(f"lock {age_sec}s")
        if trace_id and trace_id != str(snapshot.get("trace_id", "")).strip():
            pieces.append(trace_id)
    return " | ".join(pieces) if pieces else "运行中"


def _stage_status_label(status: str) -> str:
    return {
        "disabled": "已禁用",
        "pending": "待执行",
        "due": "到点待跑",
        "running": "运行中",
        "done": "已完成",
        "partial": "部分完成",
        "failed": "失败",
        "expired": "已过窗口",
        "active": "活跃",
        "skipped": "已跳过",
    }.get(status.strip().lower(), status or "未知")


def _resolve_week6_data_quality_report(
    *,
    latest_data_quality: dict[str, object] | None,
    latest_week6: dict[str, object] | None,
) -> dict[str, object] | None:
    if isinstance(latest_week6, dict):
        embedded = latest_week6.get("data_quality")
        if isinstance(embedded, dict):
            embedded_ts = _extract_report_timestamp(
                embedded,
                "timestamp",
                "updated_at",
                "generated_at",
            )
            latest_ts = _extract_report_timestamp(
                latest_data_quality,
                "timestamp",
                "updated_at",
                "generated_at",
            )
            if embedded_ts and embedded_ts >= latest_ts:
                return embedded
    return latest_data_quality


def _resolve_market_warehouse_stage_report(
    *,
    latest_report: dict[str, object] | None,
    history_items: object,
) -> dict[str, object] | None:
    if _is_market_warehouse_stage_payload(latest_report):
        return latest_report
    if not isinstance(history_items, list):
        return None
    for item in reversed(history_items):
        if isinstance(item, dict) and _is_market_warehouse_stage_payload(item):
            return item
    return None


def _resolve_market_warehouse_stage_progress(
    *,
    latest_progress: dict[str, object] | None,
    lock_status: dict[str, object] | None = None,
) -> dict[str, object] | None:
    if _is_market_warehouse_running_payload(latest_progress):
        progress = dict(latest_progress)
        if isinstance(lock_status, dict):
            progress["lock"] = lock_status
        return progress
    if _is_market_warehouse_stage_payload(latest_progress):
        progress = dict(latest_progress)
        if isinstance(lock_status, dict):
            progress["lock"] = lock_status
        return progress
    if isinstance(lock_status, dict) and bool(lock_status.get("running", False)):
        heartbeat_at = str(lock_status.get("last_heartbeat_at", "")).strip()
        created_at = str(lock_status.get("created_at", "")).strip()
        return {
            "timestamp": created_at or heartbeat_at,
            "trace_id": str(lock_status.get("trace_id", "")).strip(),
            "updated_at": heartbeat_at or created_at,
            "status": "running",
            "phase": "locked",
            "current_symbol": "",
            "current_stage": "lock_guard",
            "symbols_completed": 0,
            "symbols_total": 0,
            "progress_ratio": 0.0,
            "reason": "market_warehouse_sync_lock_active",
            "lock": lock_status,
        }
    return None


def _post_market_warehouse_followup_running(
    state: dict[str, object] | None,
) -> bool:
    if not isinstance(state, dict):
        return False
    return str(state.get("status", "")).strip().lower() == "running"


def _post_market_warehouse_followup_detail(
    state: dict[str, object] | None,
) -> str:
    if not isinstance(state, dict):
        return "followup running"
    stage = str(state.get("stage", "")).strip()
    status = str(state.get("status", "")).strip()
    payload = state.get("payload")
    reason = ""
    if isinstance(payload, dict):
        reason = str(payload.get("reason", "")).strip()
    pieces = [part for part in (stage, status, reason) if part]
    return " | ".join(pieces) if pieces else "followup running"


def _resolve_market_warehouse_resume_action(
    *,
    service: StockAnalyzerService,
    latest_report: dict[str, object] | None,
    lock_status: dict[str, object] | None,
    followup_state: dict[str, object] | None,
) -> dict[str, object]:
    report = latest_report if isinstance(latest_report, dict) else {}
    trace_id = str(report.get("trace_id", "")).strip()
    status = str(report.get("status", "")).strip().lower()
    failed_symbols = service._market_sync_service._extract_market_warehouse_failed_symbols(report)
    failed_total, failed_complete = (
        service._market_sync_service._resolve_market_warehouse_retry_failed_total(
            report,
            extracted_symbols=failed_symbols,
        )
    )
    lock_running = (
        bool(lock_status.get("running", False)) if isinstance(lock_status, dict) else False
    )
    followup_running = _post_market_warehouse_followup_running(followup_state)
    available = (
        bool(trace_id)
        and failed_total > 0
        and failed_complete
        and not lock_running
        and not followup_running
    )
    reason = ""
    if not trace_id:
        reason = "latest_report_missing"
    elif lock_running:
        reason = "sync_running"
    elif followup_running:
        reason = "followup_running"
    elif failed_total <= 0:
        reason = "no_failed_symbols_to_retry"
    elif not failed_complete:
        reason = "failed_symbols_incomplete"
    return {
        "available": available,
        "reason": reason,
        "retry_report_trace_id": trace_id,
        "latest_status": status,
        "latest_timestamp": _extract_report_timestamp(
            report,
            "timestamp",
            "updated_at",
            "completed_at",
        ),
        "target_trade_date": str(report.get("target_trade_date", "")).strip(),
        "failed_symbols_total": failed_total,
        "failed_symbols_complete": failed_complete,
        "failed_symbols_sample": failed_symbols[:10],
    }


def _is_market_warehouse_stage_payload(payload: dict[str, object] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    trace_id = str(payload.get("trace_id", "")).strip().lower()
    return trace_id.startswith("scheduler-market-warehouse")


def _is_market_warehouse_running_payload(payload: dict[str, object] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    return str(payload.get("status", "")).strip().lower() == "running"


def _select_next_task(
    *,
    tasks: list[dict[str, object]],
    current: datetime,
) -> dict[str, object] | None:
    pending_daily: list[tuple[str, dict[str, object]]] = []
    for item in tasks:
        if str(item.get("type", "")).strip().lower() != "daily":
            continue
        if str(item.get("status", "")).strip().lower() not in {"pending", "due"}:
            continue
        scheduled_time = str(item.get("scheduled_time", "")).strip()
        if not scheduled_time:
            continue
        pending_daily.append((scheduled_time, item))
    if pending_daily:
        pending_daily.sort(key=lambda pair: pair[0])
        current_hhmm = current.strftime("%H:%M")
        for scheduled_time, item in pending_daily:
            if scheduled_time >= current_hhmm:
                return item
        return pending_daily[0][1]
    return None


def _resolve_latest_stage_activity(
    *,
    tasks: list[dict[str, object]],
    warehouse_progress: dict[str, object] | None,
    warehouse_followup_state: dict[str, object] | None,
) -> dict[str, object] | None:
    latest: dict[str, object] | None = None
    latest_value = float("-inf")

    if isinstance(warehouse_progress, dict):
        updated_at = str(warehouse_progress.get("updated_at", "")).strip()
        if updated_at:
            latest = {
                "label": "基础库增量同步",
                "detail": str(warehouse_progress.get("phase", "")).strip() or "最近同步进度",
                "timestamp": updated_at,
            }
            latest_value = _parse_iso_timestamp(updated_at)

    if isinstance(warehouse_followup_state, dict):
        updated_at = str(warehouse_followup_state.get("updated_at", "")).strip()
        if updated_at:
            parsed = _parse_iso_timestamp(updated_at)
            if parsed > latest_value:
                latest = {
                    "label": "Post-market warehouse followup",
                    "detail": _post_market_warehouse_followup_detail(warehouse_followup_state),
                    "timestamp": updated_at,
                }
                latest_value = parsed

    for task in tasks:
        timestamp = _resolve_task_report_timestamp(task)
        parsed = _parse_iso_timestamp(timestamp)
        if parsed <= latest_value:
            continue
        latest_value = parsed
        latest = {
            "label": str(task.get("label", "")).strip() or str(task.get("name", "")).strip(),
            "detail": str(task.get("detail", "")).strip()
            or str(task.get("status_label", "")).strip()
            or str(task.get("status", "")).strip(),
            "timestamp": timestamp,
        }

    return latest


def _build_runtime_stage_health(
    *,
    current: datetime,
    provider_status: dict[str, object],
    acceptance_report: dict[str, object] | None,
    week5_report: dict[str, object] | None,
    pause_new_buy: bool,
    latest_activity: dict[str, object] | None,
) -> dict[str, object]:
    provider_degraded = _provider_degraded(provider_status)
    realtime_degraded = _provider_degraded(
        _mapping_object(provider_status.get("realtime_monitoring"))
    )
    risk_degraded = _provider_risk_degraded(provider_status)
    realtime_risk_degraded = _provider_risk_degraded(
        _mapping_object(provider_status.get("realtime_monitoring"))
    )
    week5_watchlist_sync = _mapping_object(
        _mapping_object(week5_report).get("watchlist_sync")
    )
    week5_sync_reason = str(week5_watchlist_sync.get("reason", "")).strip()
    intraday_preserved = week5_sync_reason == "intraday_preserve_existing"
    empty_signal = bool(
        _mapping_object(_mapping_object(week5_report).get("empty_signal")).get("triggered", False)
    )
    week5_timestamp = str(_mapping_object(week5_report).get("timestamp", "")).strip()
    acceptance_failed = False
    acceptance_gate_failed = False
    acceptance_runtime_sla_failed = False
    acceptance_timestamp = ""
    if isinstance(acceptance_report, dict):
        acceptance_timestamp = str(acceptance_report.get("timestamp", "")).strip()
        acceptance_day = acceptance_timestamp[:10] if len(acceptance_timestamp) >= 10 else ""
        acceptance_current_day = acceptance_day == current.date().isoformat()
        acceptance_summary = _mapping_object(acceptance_report.get("acceptance_summary"))
        runtime_sla = _mapping_object(acceptance_report.get("runtime_sla"))
        acceptance_gate_failed = (
            acceptance_current_day
            and (
                str(acceptance_summary.get("overall", "")).strip().lower() == "fail"
                or (
                    not acceptance_summary
                    and str(acceptance_report.get("overall", "")).strip().lower() == "fail"
                )
            )
        )
        acceptance_runtime_sla_failed = (
            acceptance_current_day
            and str(runtime_sla.get("status", "")).strip().lower() == "fail"
        )
        acceptance_failed = acceptance_gate_failed

    issues: list[str] = []
    if provider_degraded or realtime_degraded:
        issues.append("数据源退化")
    if risk_degraded or realtime_risk_degraded:
        issues.append("风控降档")
    if pause_new_buy:
        issues.append("暂停新开仓")
    if intraday_preserved:
        issues.append("盘中保守保池")
    if empty_signal:
        issues.append("空信号保护")
    if acceptance_runtime_sla_failed:
        issues.append("Week4 SLA 失败")
    if acceptance_failed:
        issues.append("Week4 验收失败")

    if (
        provider_degraded
        or realtime_degraded
        or pause_new_buy
        or acceptance_runtime_sla_failed
    ):
        code = "degraded"
        label = "已降级"
        detail = "；".join(issues[:3]) or "关键链路存在异常，请优先值守"
    elif issues:
        code = "warn"
        label = "观察中"
        detail = "；".join(issues[:3])
    else:
        code = "healthy"
        label = "运行正常"
        detail = "关键链路正常，可按当前阶段继续值守"

    latest_task = latest_activity if isinstance(latest_activity, dict) else {}
    return {
        "code": code,
        "label": label,
        "detail": detail,
        "provider_degraded": bool(provider_degraded or realtime_degraded),
        "risk_degraded": bool(risk_degraded or realtime_risk_degraded),
        "pause_new_buy": pause_new_buy,
        "week5_intraday_preserved": intraday_preserved,
        "week5_empty_signal_triggered": empty_signal,
        "week5_watchlist_sync_reason": week5_sync_reason,
        "week5_last_scan_timestamp": week5_timestamp,
        "acceptance_failed": acceptance_failed,
        "acceptance_gate_failed": acceptance_gate_failed,
        "acceptance_runtime_sla_failed": acceptance_runtime_sla_failed,
        "acceptance_timestamp": acceptance_timestamp,
        "latest_task_label": str(latest_task.get("label", "")).strip(),
        "latest_task_detail": str(latest_task.get("detail", "")).strip(),
        "latest_task_timestamp": str(latest_task.get("timestamp", "")).strip(),
    }


def _provider_degraded(status: dict[str, object]) -> bool:
    if not isinstance(status, dict):
        return False
    if "hard_degraded_mode" in status:
        return bool(status.get("hard_degraded_mode", False))
    evolution = _mapping_object(status.get("evolution"))
    health = _mapping_object(status.get("health"))
    if bool(health.get("degraded_mode", False)):
        return True
    return bool(status.get("degraded_mode", False)) and not bool(
        evolution.get("degraded_mode", False)
    )


def _provider_risk_degraded(status: dict[str, object]) -> bool:
    if not isinstance(status, dict):
        return False
    if "soft_degraded_mode" in status:
        return bool(status.get("soft_degraded_mode", False))
    evolution = _mapping_object(status.get("evolution"))
    if not evolution:
        return False
    return bool(evolution.get("degraded_mode", False)) or bool(
        evolution.get("conservative_mode", False)
    )


def _mapping_object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    return {}


def _resolve_task_report_timestamp(task: dict[str, object]) -> str:
    report_timestamp = str(task.get("report_timestamp", "")).strip()
    if report_timestamp:
        return report_timestamp
    last_slot_date = str(task.get("last_slot_date", "")).strip()
    last_slot_value = task.get("last_slot_value")
    if not last_slot_date or not isinstance(last_slot_value, (int, float)):
        return ""
    slot_total = max(0, int(last_slot_value))
    hours = str(slot_total // 60).zfill(2)
    minutes = str(slot_total % 60).zfill(2)
    return f"{last_slot_date}T{hours}:{minutes}:00"


def _parse_iso_timestamp(value: str) -> float:
    if not value:
        return float("-inf")
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return float("-inf")
