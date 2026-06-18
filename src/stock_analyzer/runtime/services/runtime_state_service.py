"""Runtime state persistence and merge workflows extracted from the main service."""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4


class RuntimeStateService:
    """Delegated runtime-state persistence, load, and merge helpers."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def _default_runtime_state_payload(self) -> dict[str, object]:
        service = self._service
        recommendation_lifecycle = {
            symbol: dict(item)
            for symbol, item in sorted(
                service._recommendation_lifecycle.items(),
                key=lambda row: row[0],
            )
        }
        acceptance_limit = max(1, service._config.acceptance.history_limit)
        market_radar_review_pool_limit = max(
            1,
            service._config.week5.market_radar_review_pool_max_symbols,
        )
        week6_limit = max(1, service._config.week6.history_limit)
        market_warehouse_limit = max(1, service._config.market_warehouse.history_limit)
        evolution_limit = max(1, service._config.evolution.history_limit)
        return {
            "state_version": 8,
            "updated_at": datetime.now().isoformat(),
            "scheduler_state": service._scheduler.export_state(),
            "current_equity": service._state.current_equity,
            "watchlist": list(service._state.watchlist),
            "pause_new_buy": bool(service._state.pause_new_buy),
            "reconcile_required": bool(service._state.reconcile_required),
            "portfolio": service._portfolio.export_state(),
            "broker_snapshot_updated_at": str(service._broker_snapshot_updated_at).strip(),
            "broker_snapshot_source": str(service._broker_snapshot_source).strip(),
            "broker_positions": dict(service._broker_positions),
            "broker_position_details": deepcopy(service._broker_position_details),
            "recommendation_lifecycle": recommendation_lifecycle,
            "latest_signals": service._runtime_state_optional_dict(
                service._last_signal_snapshot
            ),
            "last_reconcile_report": service._runtime_state_optional_dict(
                service._last_reconcile_report
            ),
            "audit_seq": service._audit_seq,
            "runtime_history_sidecars": self._runtime_state_history_sidecar_metadata(),
            "week4_acceptance_latest": service._runtime_state_optional_dict(
                service._last_week4_acceptance_report
            ),
            "week4_acceptance_history": service._runtime_state_dict_list(
                service._week4_acceptance_history,
                limit=acceptance_limit,
            ),
            "week5_scan_latest": service._runtime_state_optional_dict(
                service._last_week5_scan_report
            ),
            "week5_market_radar_latest": service._runtime_state_optional_dict(
                service._last_week5_market_radar_report
            ),
            "week5_market_radar_review_pool": service._runtime_state_dict_list(
                service._market_radar_review_pool,
                limit=market_radar_review_pool_limit,
            ),
            "week6_latest": service._runtime_state_optional_dict(service._last_week6_report),
            "week6_history": service._runtime_state_dict_list(
                service._week6_history,
                limit=week6_limit,
            ),
            "market_warehouse_latest": service._runtime_state_optional_dict(
                service._last_market_warehouse_report
            ),
            "market_warehouse_history": service._runtime_state_dict_list(
                service._market_warehouse_history,
                limit=market_warehouse_limit,
            ),
            "market_warehouse_progress": service._runtime_state_optional_dict(
                service._last_market_warehouse_progress
            ),
            "cloud_backup": self._runtime_state_cloud_backup_payload(),
            "evolution_latest": service._runtime_state_optional_dict(
                service._last_evolution_report
            ),
            "evolution_history": service._runtime_state_dict_list(
                service._evolution_history,
                limit=evolution_limit,
            ),
            "evolution_release_gate_latest": service._runtime_state_optional_dict(
                service._last_evolution_release_gate
            ),
            "evolution_release_gate_history": service._runtime_state_dict_list(
                service._evolution_release_gate_history,
                limit=evolution_limit,
            ),
            "evolution_release_approval_latest": service._runtime_state_optional_dict(
                service._last_evolution_release_approval
            ),
            "evolution_release_approval_history": service._runtime_state_dict_list(
                service._evolution_release_approval_history,
                limit=evolution_limit,
            ),
            "evolution_release_ticket_latest": service._runtime_state_optional_dict(
                service._last_evolution_release_ticket
            ),
            "evolution_release_ticket_history": service._runtime_state_dict_list(
                service._evolution_release_ticket_history,
                limit=evolution_limit,
            ),
            "learning_model_proposal_latest": service._runtime_state_optional_dict(
                service._last_learning_model_proposal
            ),
            "learning_model_proposal_history": service._runtime_state_dict_list(
                service._learning_model_proposal_history,
                limit=evolution_limit,
            ),
            "learning_model_approval_latest": service._runtime_state_optional_dict(
                service._last_learning_model_approval
            ),
            "learning_model_approval_history": service._runtime_state_dict_list(
                service._learning_model_approval_history,
                limit=evolution_limit,
            ),
            "learning_model_release_ticket_latest": service._runtime_state_optional_dict(
                service._last_learning_model_release_ticket
            ),
            "learning_model_release_ticket_history": service._runtime_state_dict_list(
                service._learning_model_release_ticket_history,
                limit=evolution_limit,
            ),
            "execution_risk_training_latest": service._runtime_state_optional_dict(
                service._last_execution_risk_training
            ),
            "execution_risk_training_history": service._runtime_state_dict_list(
                service._execution_risk_training_history,
                limit=evolution_limit,
            ),
            "execution_aware_report_latest": service._runtime_state_optional_dict(
                service._last_execution_aware_report
            ),
            "execution_aware_report_history": service._runtime_state_dict_list(
                service._execution_aware_report_history,
                limit=evolution_limit,
            ),
        }

    def _runtime_state_optional_dict(self, raw: object) -> dict[str, object] | None:
        if not isinstance(raw, dict):
            return None
        return deepcopy(raw)

    def _runtime_state_dict_list(
        self,
        raw: object,
        *,
        limit: int,
    ) -> list[dict[str, object]]:
        normalized: list[dict[str, object]] = []
        if not isinstance(raw, list):
            return normalized
        for item in raw:
            if isinstance(item, dict):
                normalized.append(deepcopy(item))
        if len(normalized) > limit:
            normalized = normalized[-limit:]
        return normalized

    def _runtime_state_latest_from_raw(
        self,
        raw_latest: object,
        history: list[dict[str, object]],
    ) -> dict[str, object] | None:
        service = self._service
        latest = service._runtime_state_optional_dict(raw_latest)
        candidates: list[dict[str, object]] = []
        if latest is not None:
            candidates.append(cast(dict[str, object], latest))
        candidates.extend(history)
        if not candidates:
            return None
        return deepcopy(max(candidates, key=_report_timestamp))

    def _runtime_state_cloud_backup_payload(self) -> dict[str, object]:
        service = self._service
        armed = bool(service._cloud_backup_armed or service._cloud_backup_last_ping_at is not None)
        require_first_ping = bool(service._config.cloud_backup.require_first_ping_before_alert)
        return {
            "last_ping_at": (
                service._cloud_backup_last_ping_at.isoformat()
                if service._cloud_backup_last_ping_at is not None
                else ""
            ),
            "last_ping_source": str(service._cloud_backup_last_ping_source).strip(),
            "alert_active": bool(
                service._cloud_backup_alert_active and (armed or not require_first_ping)
            ),
            "last_alert_at": (
                service._cloud_backup_last_alert_at.isoformat()
                if service._cloud_backup_last_alert_at is not None
                else ""
            ),
            "last_recovery_at": (
                service._cloud_backup_last_recovery_at.isoformat()
                if service._cloud_backup_last_recovery_at is not None
                else ""
            ),
            "armed": armed,
            "has_ping_history": armed,
        }

    def _runtime_state_history_sidecar_metadata(self) -> dict[str, object]:
        return {
            "format": "jsonl",
            "base_dir": str(self._runtime_state_sidecar_dir()),
            "records": {
                name: {
                    "path": str(path),
                    "limit": limit,
                    "records": len(records),
                }
                for name, records, limit, path, _identity_keys in (
                    self._runtime_state_sidecar_specs()
                )
            },
        }

    def _runtime_state_sidecar_dir(self) -> Path:
        service = self._service
        return service._runtime_state_path.with_name("runtime_state_history")

    def _runtime_state_sidecar_specs(
        self,
    ) -> list[tuple[str, list[dict[str, object]], int, Path, tuple[str, ...]]]:
        service = self._service
        base_dir = self._runtime_state_sidecar_dir()
        return [
            (
                "reconcile_history",
                service._reconcile_history,
                max(1, service._config.reconcile.history_limit),
                base_dir / "reconcile_history.jsonl",
                ("timestamp", "status", "trace_id"),
            ),
            (
                "run_summaries",
                service._run_summaries,
                2000,
                base_dir / "run_summaries.jsonl",
                ("timestamp", "trace_id"),
            ),
            (
                "latency_history_ms",
                service._latency_history_ms,
                5000,
                base_dir / "latency_history_ms.jsonl",
                ("timestamp", "duration_ms"),
            ),
            (
                "audit_events",
                service._audit_events,
                5000,
                base_dir / "audit_events.jsonl",
                ("event_id", "timestamp", "trace_id", "event_type"),
            ),
            (
                "week5_scan_history",
                service._week5_scan_history,
                max(1, service._config.week5.history_limit),
                base_dir / "week5_scan_history.jsonl",
                ("timestamp", "trace_id"),
            ),
        ]

    def _persist_runtime_state_history_sidecars(self, existing_raw: object) -> None:
        service = self._service
        existing = existing_raw if isinstance(existing_raw, dict) else {}
        base_dir = self._runtime_state_sidecar_dir()
        base_dir.mkdir(parents=True, exist_ok=True)
        for name, records, limit, path, identity_keys in self._runtime_state_sidecar_specs():
            existing_rows = service._merge_runtime_state_history(
                self._load_runtime_state_history_sidecar(name, limit=limit * 2),
                existing.get(name),
                limit=max(1, limit) * 2,
                identity_keys=identity_keys,
            )
            rows = service._merge_runtime_state_history(
                existing_rows,
                records,
                limit=max(1, limit),
                identity_keys=identity_keys,
            )
            temp_path = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
            with temp_path.open("w", encoding="utf-8") as fp:
                for row in rows:
                    fp.write(
                        json.dumps(
                            row,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            default=str,
                        )
                    )
                    fp.write("\n")
            temp_path.replace(path)

    def _load_runtime_state_history_sidecar(
        self,
        name: str,
        *,
        limit: int,
    ) -> list[dict[str, object]]:
        path = self._runtime_state_sidecar_dir() / f"{name}.jsonl"
        rows: list[dict[str, object]] = []
        try:
            with path.open("r", encoding="utf-8") as fp:
                for line in fp:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        item = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict):
                        rows.append(cast(dict[str, object], item))
        except OSError:
            return []
        if len(rows) > limit:
            rows = rows[-limit:]
        return rows

    def _persist_runtime_state_to_disk(self, *, include_history_sidecars: bool = True) -> None:
        service = self._service
        if not bool(service._config.command_channel.state_persist_enabled):
            return
        path = service._runtime_state_path
        payload = service._default_runtime_state_payload()
        existing_raw: object = {}
        if path.exists():
            try:
                existing_raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing_raw = {}
        payload = service._merge_runtime_state_payload(existing_raw, payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        if include_history_sidecars:
            self._persist_runtime_state_history_sidecars(existing_raw)
        temp_path = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        with temp_path.open("w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, separators=(",", ":"), default=str)
        temp_path.replace(path)
        try:
            service._runtime_state_loaded_mtime_ns = path.stat().st_mtime_ns
        except OSError:
            service._runtime_state_loaded_mtime_ns = 0

    def _load_runtime_state_from_disk(self) -> None:
        service = self._service
        if not bool(service._config.command_channel.state_persist_enabled):
            return
        path = service._runtime_state_path
        if not path.exists():
            service._persist_runtime_state_to_disk()
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            service._persist_runtime_state_to_disk()
            return
        if not isinstance(raw, dict):
            service._persist_runtime_state_to_disk()
            return

        service._scheduler.import_state(raw.get("scheduler_state"))
        service._portfolio.restore_state(
            raw.get("portfolio") if isinstance(raw.get("portfolio"), Mapping) else None
        )
        service._broker_positions = service._load_runtime_state_numeric_mapping(
            raw.get("broker_positions"),
        )
        broker_details = raw.get("broker_position_details")
        service._broker_position_details = (
            deepcopy(broker_details) if isinstance(broker_details, dict) else {}
        )
        broker_snapshot_updated_at = str(raw.get("broker_snapshot_updated_at", "")).strip()
        if (
            not broker_snapshot_updated_at
            and (service._broker_positions or service._broker_position_details)
        ):
            broker_snapshot_updated_at = str(raw.get("updated_at", "")).strip()
        service._broker_snapshot_updated_at = broker_snapshot_updated_at
        broker_snapshot_source = str(raw.get("broker_snapshot_source", "")).strip().lower()
        if not broker_snapshot_source and (
            service._broker_positions or service._broker_position_details
        ):
            broker_snapshot_source = "manual"
        service._broker_snapshot_source = (
            broker_snapshot_source if broker_snapshot_source == "portfolio" else "manual"
        ) if broker_snapshot_source else ""
        service._state.current_equity = max(
            0.01,
            _as_float(raw.get("current_equity"), default=service._state.current_equity),
        )
        service._state.pause_new_buy = bool(raw.get("pause_new_buy", service._state.pause_new_buy))
        service._state.reconcile_required = bool(
            raw.get("reconcile_required", service._state.reconcile_required)
        )
        raw_watchlist = raw.get("watchlist")
        if isinstance(raw_watchlist, list):
            service._state.watchlist = service._merge_runtime_state_watchlist([], raw_watchlist)
        service._load_recommendation_lifecycle_from_raw(raw.get("recommendation_lifecycle"))
        latest_signal_snapshot = service._runtime_state_optional_dict(raw.get("latest_signals"))
        service._last_signal_snapshot = latest_signal_snapshot
        if latest_signal_snapshot is not None:
            raw_signals = latest_signal_snapshot.get("signals")
            service._last_signal_payload = [
                dict(item) for item in raw_signals if isinstance(item, Mapping)
            ] if isinstance(raw_signals, list) else []
            service._last_signal_trace_id = str(
                latest_signal_snapshot.get("trace_id", "")
            ).strip()
            service._last_signal_timestamp = str(
                latest_signal_snapshot.get("timestamp", "")
            ).strip()
            service._last_signal_source = "runtime_state"
            service._latest_signal_snapshot_dirty = False
        else:
            service._last_signal_payload = []
            service._last_signal_trace_id = ""
            service._last_signal_timestamp = ""
            service._last_signal_source = ""
            service._latest_signal_snapshot_dirty = False
        service._reconcile_history = service._merge_runtime_state_history(
            self._load_runtime_state_history_sidecar(
                "reconcile_history",
                limit=max(1, service._config.reconcile.history_limit),
            ),
            raw.get("reconcile_history"),
            limit=max(1, service._config.reconcile.history_limit),
            identity_keys=("timestamp", "status", "trace_id"),
        )
        service._last_reconcile_report = service._runtime_state_latest_from_raw(
            raw.get("last_reconcile_report"),
            service._reconcile_history,
        )
        service._run_summaries = service._merge_runtime_state_history(
            self._load_runtime_state_history_sidecar("run_summaries", limit=2000),
            raw.get("run_summaries"),
            limit=2000,
            identity_keys=("timestamp", "trace_id"),
        )
        service._latency_history_ms = service._merge_runtime_state_history(
            self._load_runtime_state_history_sidecar("latency_history_ms", limit=5000),
            raw.get("latency_history_ms"),
            limit=5000,
            identity_keys=("timestamp", "duration_ms"),
        )
        service._audit_events = service._merge_runtime_state_history(
            self._load_runtime_state_history_sidecar("audit_events", limit=5000),
            raw.get("audit_events"),
            limit=5000,
            identity_keys=("event_id", "timestamp", "trace_id", "event_type"),
        )
        service._audit_seq = max(
            _as_int(raw.get("audit_seq"), default=0),
            len(service._audit_events),
        )
        service._week4_acceptance_history = service._runtime_state_dict_list(
            raw.get("week4_acceptance_history"),
            limit=max(1, service._config.acceptance.history_limit),
        )
        service._last_week4_acceptance_report = service._runtime_state_latest_from_raw(
            raw.get("week4_acceptance_latest"),
            service._week4_acceptance_history,
        )
        service._week5_scan_history = service._merge_runtime_state_history(
            self._load_runtime_state_history_sidecar(
                "week5_scan_history",
                limit=max(1, service._config.week5.history_limit),
            ),
            raw.get("week5_scan_history"),
            limit=max(1, service._config.week5.history_limit),
            identity_keys=("timestamp", "trace_id"),
        )
        service._last_week5_scan_report = service._runtime_state_latest_from_raw(
            raw.get("week5_scan_latest"),
            service._week5_scan_history,
        )
        service._last_week5_market_radar_report = service._runtime_state_optional_dict(
            raw.get("week5_market_radar_latest")
        )
        service._market_radar_review_pool = service._runtime_state_dict_list(
            raw.get("week5_market_radar_review_pool"),
            limit=max(1, service._config.week5.market_radar_review_pool_max_symbols),
        )
        service._week6_history = service._runtime_state_dict_list(
            raw.get("week6_history"),
            limit=max(1, service._config.week6.history_limit),
        )
        service._last_week6_report = service._runtime_state_latest_from_raw(
            raw.get("week6_latest"),
            service._week6_history,
        )
        service._market_warehouse_history = service._runtime_state_dict_list(
            raw.get("market_warehouse_history"),
            limit=max(1, service._config.market_warehouse.history_limit),
        )
        service._last_market_warehouse_report = service._runtime_state_latest_from_raw(
            raw.get("market_warehouse_latest"),
            service._market_warehouse_history,
        )
        service._last_market_warehouse_progress = service._runtime_state_optional_dict(
            raw.get("market_warehouse_progress")
        )
        self._load_runtime_state_cloud_backup(raw.get("cloud_backup"))
        service._evolution_history = service._runtime_state_dict_list(
            raw.get("evolution_history"),
            limit=max(1, service._config.evolution.history_limit),
        )
        service._last_evolution_report = service._runtime_state_latest_from_raw(
            raw.get("evolution_latest"),
            service._evolution_history,
        )
        service._evolution_release_gate_history = service._runtime_state_dict_list(
            raw.get("evolution_release_gate_history"),
            limit=max(1, service._config.evolution.history_limit),
        )
        service._last_evolution_release_gate = service._runtime_state_latest_from_raw(
            raw.get("evolution_release_gate_latest"),
            service._evolution_release_gate_history,
        )
        service._evolution_release_approval_history = service._runtime_state_dict_list(
            raw.get("evolution_release_approval_history"),
            limit=max(1, service._config.evolution.history_limit),
        )
        service._last_evolution_release_approval = service._runtime_state_latest_from_raw(
            raw.get("evolution_release_approval_latest"),
            service._evolution_release_approval_history,
        )
        service._evolution_release_ticket_history = service._runtime_state_dict_list(
            raw.get("evolution_release_ticket_history"),
            limit=max(1, service._config.evolution.history_limit),
        )
        service._last_evolution_release_ticket = service._runtime_state_latest_from_raw(
            raw.get("evolution_release_ticket_latest"),
            service._evolution_release_ticket_history,
        )
        service._learning_model_proposal_history = service._runtime_state_dict_list(
            raw.get("learning_model_proposal_history"),
            limit=max(1, service._config.evolution.history_limit),
        )
        service._last_learning_model_proposal = service._runtime_state_latest_from_raw(
            raw.get("learning_model_proposal_latest"),
            service._learning_model_proposal_history,
        )
        service._learning_model_approval_history = service._runtime_state_dict_list(
            raw.get("learning_model_approval_history"),
            limit=max(1, service._config.evolution.history_limit),
        )
        service._last_learning_model_approval = service._runtime_state_latest_from_raw(
            raw.get("learning_model_approval_latest"),
            service._learning_model_approval_history,
        )
        service._learning_model_release_ticket_history = service._runtime_state_dict_list(
            raw.get("learning_model_release_ticket_history"),
            limit=max(1, service._config.evolution.history_limit),
        )
        service._last_learning_model_release_ticket = service._runtime_state_latest_from_raw(
            raw.get("learning_model_release_ticket_latest"),
            service._learning_model_release_ticket_history,
        )
        service._execution_risk_training_history = service._runtime_state_dict_list(
            raw.get("execution_risk_training_history"),
            limit=max(1, service._config.evolution.history_limit),
        )
        service._last_execution_risk_training = service._runtime_state_latest_from_raw(
            raw.get("execution_risk_training_latest"),
            service._execution_risk_training_history,
        )
        service._execution_aware_report_history = service._runtime_state_dict_list(
            raw.get("execution_aware_report_history"),
            limit=max(1, service._config.evolution.history_limit),
        )
        service._last_execution_aware_report = service._runtime_state_latest_from_raw(
            raw.get("execution_aware_report_latest"),
            service._execution_aware_report_history,
        )
        try:
            service._runtime_state_loaded_mtime_ns = path.stat().st_mtime_ns
        except OSError:
            service._runtime_state_loaded_mtime_ns = 0

    def _refresh_runtime_state_from_disk_if_changed(self) -> None:
        service = self._service
        if not bool(service._config.command_channel.state_persist_enabled):
            return
        path = service._runtime_state_path
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            return
        if mtime_ns <= service._runtime_state_loaded_mtime_ns:
            return
        service._load_runtime_state_from_disk()

    def _refresh_cloud_backup_state_from_disk(self) -> None:
        service = self._service
        if not bool(service._config.command_channel.state_persist_enabled):
            return
        path = service._runtime_state_path
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        self._load_runtime_state_cloud_backup(raw.get("cloud_backup"))

    def _load_runtime_state_cloud_backup(self, raw: object) -> None:
        if not isinstance(raw, Mapping):
            return
        service = self._service
        normalized = self._merge_runtime_state_cloud_backup(raw, {})
        last_ping_at = _parse_runtime_state_datetime(normalized.get("last_ping_at"))
        last_alert_at = _parse_runtime_state_datetime(normalized.get("last_alert_at"))
        last_recovery_at = _parse_runtime_state_datetime(normalized.get("last_recovery_at"))
        armed = (
            _as_bool(normalized.get("armed"), default=False)
            or _as_bool(normalized.get("has_ping_history"), default=False)
            or last_ping_at is not None
        )
        service._cloud_backup_last_ping_at = last_ping_at
        service._cloud_backup_last_ping_source = str(
            normalized.get("last_ping_source", "")
        ).strip()
        service._cloud_backup_last_alert_at = last_alert_at
        service._cloud_backup_last_recovery_at = last_recovery_at
        service._cloud_backup_armed = armed
        require_first_ping = bool(service._config.cloud_backup.require_first_ping_before_alert)
        service._cloud_backup_alert_active = bool(
            normalized.get("alert_active", False) and (armed or not require_first_ping)
        )

    def _merge_runtime_state_payload(
        self,
        existing_raw: object,
        current_raw: dict[str, object],
    ) -> dict[str, object]:
        service = self._service
        existing = existing_raw if isinstance(existing_raw, dict) else {}
        merged = dict(current_raw)
        merged["scheduler_state"] = service._merge_runtime_state_scheduler(
            existing.get("scheduler_state"),
            current_raw.get("scheduler_state"),
        )
        merged["watchlist"] = service._merge_runtime_state_watchlist(
            existing.get("watchlist"),
            current_raw.get("watchlist"),
        )
        merged["portfolio"] = service._merge_runtime_state_portfolio(
            existing.get("portfolio"),
            current_raw.get("portfolio"),
        )
        (
            merged["broker_snapshot_updated_at"],
            merged["broker_snapshot_source"],
            merged["broker_positions"],
            merged["broker_position_details"],
        ) = self._merge_runtime_state_broker_snapshot(
            existing_raw=existing,
            current_raw=current_raw,
        )
        merged["recommendation_lifecycle"] = service._merge_runtime_state_mapping(
            existing.get("recommendation_lifecycle"),
            current_raw.get("recommendation_lifecycle"),
        )
        merged["latest_signals"] = service._merge_runtime_state_latest(
            existing.get("latest_signals"),
            current_raw.get("latest_signals"),
        )
        merged["last_reconcile_report"] = service._runtime_state_latest_from_raw(
            None
            if service._reconcile_history
            else service._merge_runtime_state_latest(
                existing.get("last_reconcile_report"),
                current_raw.get("last_reconcile_report"),
            ),
            service._reconcile_history,
        )
        merged["audit_seq"] = max(
            _as_int(existing.get("audit_seq"), default=0),
            _as_int(current_raw.get("audit_seq"), default=0),
        )
        week4_history = service._merge_runtime_state_history(
            existing.get("week4_acceptance_history"),
            current_raw.get("week4_acceptance_history"),
            limit=max(1, service._config.acceptance.history_limit),
            identity_keys=("timestamp", "overall"),
        )
        merged["week4_acceptance_history"] = week4_history
        merged["week4_acceptance_latest"] = service._runtime_state_latest_from_raw(
            None
            if week4_history
            else service._merge_runtime_state_latest(
                existing.get("week4_acceptance_latest"),
                current_raw.get("week4_acceptance_latest"),
            ),
            week4_history,
        )
        merged["week5_scan_latest"] = service._runtime_state_latest_from_raw(
            None
            if service._week5_scan_history
            else service._merge_runtime_state_latest(
                existing.get("week5_scan_latest"),
                current_raw.get("week5_scan_latest"),
            ),
            service._week5_scan_history,
        )
        merged["week5_market_radar_latest"] = service._merge_runtime_state_latest(
            existing.get("week5_market_radar_latest"),
            current_raw.get("week5_market_radar_latest"),
        )
        merged["week5_market_radar_review_pool"] = service._merge_runtime_state_history(
            existing.get("week5_market_radar_review_pool"),
            current_raw.get("week5_market_radar_review_pool"),
            limit=max(1, service._config.week5.market_radar_review_pool_max_symbols),
            identity_keys=("symbol",),
        )
        week6_history = service._merge_runtime_state_history(
            existing.get("week6_history"),
            current_raw.get("week6_history"),
            limit=max(1, service._config.week6.history_limit),
            identity_keys=("timestamp",),
        )
        merged["week6_history"] = week6_history
        merged["week6_latest"] = service._runtime_state_latest_from_raw(
            None
            if week6_history
            else service._merge_runtime_state_latest(
                existing.get("week6_latest"),
                current_raw.get("week6_latest"),
            ),
            week6_history,
        )
        market_warehouse_history = service._merge_runtime_state_history(
            existing.get("market_warehouse_history"),
            current_raw.get("market_warehouse_history"),
            limit=max(1, service._config.market_warehouse.history_limit),
            identity_keys=("timestamp",),
        )
        merged["market_warehouse_history"] = market_warehouse_history
        merged["market_warehouse_latest"] = service._runtime_state_latest_from_raw(
            None
            if market_warehouse_history
            else service._merge_runtime_state_latest(
                existing.get("market_warehouse_latest"),
                current_raw.get("market_warehouse_latest"),
            ),
            market_warehouse_history,
        )
        merged["market_warehouse_progress"] = service._merge_runtime_state_latest(
            existing.get("market_warehouse_progress"),
            current_raw.get("market_warehouse_progress"),
        )
        merged["cloud_backup"] = self._merge_runtime_state_cloud_backup(
            existing.get("cloud_backup"),
            current_raw.get("cloud_backup"),
        )
        evolution_history = service._merge_runtime_state_history(
            existing.get("evolution_history"),
            current_raw.get("evolution_history"),
            limit=max(1, service._config.evolution.history_limit),
            identity_keys=("timestamp",),
        )
        merged["evolution_history"] = evolution_history
        merged["evolution_latest"] = service._runtime_state_latest_from_raw(
            None
            if evolution_history
            else service._merge_runtime_state_latest(
                existing.get("evolution_latest"),
                current_raw.get("evolution_latest"),
            ),
            evolution_history,
        )
        evolution_gate_history = service._merge_runtime_state_history(
            existing.get("evolution_release_gate_history"),
            current_raw.get("evolution_release_gate_history"),
            limit=max(1, service._config.evolution.history_limit),
            identity_keys=("timestamp", "status"),
        )
        merged["evolution_release_gate_history"] = evolution_gate_history
        merged["evolution_release_gate_latest"] = service._runtime_state_latest_from_raw(
            None
            if evolution_gate_history
            else service._merge_runtime_state_latest(
                existing.get("evolution_release_gate_latest"),
                current_raw.get("evolution_release_gate_latest"),
            ),
            evolution_gate_history,
        )
        evolution_approval_history = service._merge_runtime_state_history(
            existing.get("evolution_release_approval_history"),
            current_raw.get("evolution_release_approval_history"),
            limit=max(1, service._config.evolution.history_limit),
            identity_keys=("approval_id", "timestamp"),
        )
        merged["evolution_release_approval_history"] = evolution_approval_history
        merged["evolution_release_approval_latest"] = service._runtime_state_latest_from_raw(
            None
            if evolution_approval_history
            else service._merge_runtime_state_latest(
                existing.get("evolution_release_approval_latest"),
                current_raw.get("evolution_release_approval_latest"),
            ),
            evolution_approval_history,
        )
        evolution_ticket_history = service._merge_runtime_state_history(
            existing.get("evolution_release_ticket_history"),
            current_raw.get("evolution_release_ticket_history"),
            limit=max(1, service._config.evolution.history_limit),
            identity_keys=(),
        )
        merged["evolution_release_ticket_history"] = evolution_ticket_history
        merged["evolution_release_ticket_latest"] = service._runtime_state_latest_from_raw(
            None
            if evolution_ticket_history
            else service._merge_runtime_state_latest(
                existing.get("evolution_release_ticket_latest"),
                current_raw.get("evolution_release_ticket_latest"),
            ),
            evolution_ticket_history,
        )
        learning_proposal_history = service._merge_runtime_state_history(
            existing.get("learning_model_proposal_history"),
            current_raw.get("learning_model_proposal_history"),
            limit=max(1, service._config.evolution.history_limit),
            identity_keys=("proposal_id", "timestamp", "status"),
        )
        merged["learning_model_proposal_history"] = learning_proposal_history
        merged["learning_model_proposal_latest"] = service._runtime_state_latest_from_raw(
            None
            if learning_proposal_history
            else service._merge_runtime_state_latest(
                existing.get("learning_model_proposal_latest"),
                current_raw.get("learning_model_proposal_latest"),
            ),
            learning_proposal_history,
        )
        learning_approval_history = service._merge_runtime_state_history(
            existing.get("learning_model_approval_history"),
            current_raw.get("learning_model_approval_history"),
            limit=max(1, service._config.evolution.history_limit),
            identity_keys=("approval_id", "timestamp"),
        )
        merged["learning_model_approval_history"] = learning_approval_history
        merged["learning_model_approval_latest"] = service._runtime_state_latest_from_raw(
            None
            if learning_approval_history
            else service._merge_runtime_state_latest(
                existing.get("learning_model_approval_latest"),
                current_raw.get("learning_model_approval_latest"),
            ),
            learning_approval_history,
        )
        learning_ticket_history = service._merge_runtime_state_history(
            existing.get("learning_model_release_ticket_history"),
            current_raw.get("learning_model_release_ticket_history"),
            limit=max(1, service._config.evolution.history_limit),
            identity_keys=(),
        )
        merged["learning_model_release_ticket_history"] = learning_ticket_history
        merged["learning_model_release_ticket_latest"] = service._runtime_state_latest_from_raw(
            None
            if learning_ticket_history
            else service._merge_runtime_state_latest(
                existing.get("learning_model_release_ticket_latest"),
                current_raw.get("learning_model_release_ticket_latest"),
            ),
            learning_ticket_history,
        )
        execution_risk_history = service._merge_runtime_state_history(
            existing.get("execution_risk_training_history"),
            current_raw.get("execution_risk_training_history"),
            limit=max(1, service._config.evolution.history_limit),
            identity_keys=("timestamp", "dataset_id", "artifact_path"),
        )
        merged["execution_risk_training_history"] = execution_risk_history
        merged["execution_risk_training_latest"] = service._runtime_state_latest_from_raw(
            None
            if execution_risk_history
            else service._merge_runtime_state_latest(
                existing.get("execution_risk_training_latest"),
                current_raw.get("execution_risk_training_latest"),
            ),
            execution_risk_history,
        )
        execution_aware_history = service._merge_runtime_state_history(
            existing.get("execution_aware_report_history"),
            current_raw.get("execution_aware_report_history"),
            limit=max(1, service._config.evolution.history_limit),
            identity_keys=("report_id", "shadow_model_id", "champion_model_id"),
        )
        merged["execution_aware_report_history"] = execution_aware_history
        merged["execution_aware_report_latest"] = service._runtime_state_latest_from_raw(
            None
            if execution_aware_history
            else service._merge_runtime_state_latest(
                existing.get("execution_aware_report_latest"),
                current_raw.get("execution_aware_report_latest"),
            ),
            execution_aware_history,
        )
        return merged

    def _merge_runtime_state_cloud_backup(
        self,
        existing_raw: object,
        current_raw: object,
    ) -> dict[str, object]:
        states = [
            state
            for state in (
                self._runtime_state_optional_dict(existing_raw),
                self._runtime_state_optional_dict(current_raw),
            )
            if state is not None
        ]
        if not states:
            return {
                "last_ping_at": "",
                "last_ping_source": "",
                "alert_active": False,
                "last_alert_at": "",
                "last_recovery_at": "",
                "armed": False,
                "has_ping_history": False,
            }

        last_ping_at = _latest_runtime_state_datetime(states, "last_ping_at")
        last_alert_at = _latest_runtime_state_datetime(states, "last_alert_at")
        last_recovery_at = _latest_runtime_state_datetime(states, "last_recovery_at")
        last_ping_source = ""
        source_timestamp: datetime | None = None
        for state in states:
            candidate_timestamp = _parse_runtime_state_datetime(state.get("last_ping_at"))
            candidate_source = str(state.get("last_ping_source", "")).strip()
            if candidate_timestamp is None:
                if not last_ping_source and candidate_source:
                    last_ping_source = candidate_source
                continue
            if source_timestamp is None or candidate_timestamp >= source_timestamp:
                source_timestamp = candidate_timestamp
                last_ping_source = candidate_source

        armed = any(
            _as_bool(state.get("armed"), default=False)
            or _as_bool(state.get("has_ping_history"), default=False)
            for state in states
        ) or last_ping_at is not None
        inactive_candidates = [
            timestamp
            for timestamp in (last_ping_at, last_recovery_at)
            if timestamp is not None
        ]
        inactive_at = max(inactive_candidates) if inactive_candidates else None
        if last_alert_at is not None:
            alert_active = inactive_at is None or last_alert_at > inactive_at
        else:
            alert_active = (
                any(_as_bool(state.get("alert_active"), default=False) for state in states)
                and inactive_at is None
            )

        require_first_ping = bool(
            self._service._config.cloud_backup.require_first_ping_before_alert
        )
        return {
            "last_ping_at": last_ping_at.isoformat() if last_ping_at is not None else "",
            "last_ping_source": last_ping_source,
            "alert_active": bool(alert_active and (armed or not require_first_ping)),
            "last_alert_at": last_alert_at.isoformat() if last_alert_at is not None else "",
            "last_recovery_at": (
                last_recovery_at.isoformat() if last_recovery_at is not None else ""
            ),
            "armed": armed,
            "has_ping_history": armed,
        }

    def _merge_runtime_state_scheduler(
        self,
        existing_raw: object,
        current_raw: object,
    ) -> dict[str, object]:
        merged_last_run: dict[str, str] = {}
        for raw in (existing_raw, current_raw):
            if not isinstance(raw, dict):
                continue
            last_run = raw.get("last_run")
            if not isinstance(last_run, dict):
                continue
            for name, value in last_run.items():
                normalized_name = str(name).strip()
                normalized_value = str(value).strip()
                if not normalized_name or not normalized_value:
                    continue
                current_value = merged_last_run.get(normalized_name, "")
                if normalized_value > current_value:
                    merged_last_run[normalized_name] = normalized_value

        merged_interval: dict[str, dict[str, object]] = {}
        for raw in (existing_raw, current_raw):
            if not isinstance(raw, dict):
                continue
            interval = raw.get("last_interval_slot")
            if not isinstance(interval, dict):
                continue
            for name, value in interval.items():
                if not isinstance(value, dict):
                    continue
                normalized_name = str(name).strip()
                slot_date = str(value.get("date", "")).strip()
                slot_value = _as_int(value.get("slot"), default=-1)
                if not normalized_name or not slot_date or slot_value < 0:
                    continue
                current_slot_state = merged_interval.get(normalized_name)
                if current_slot_state is None:
                    merged_interval[normalized_name] = {"date": slot_date, "slot": slot_value}
                    continue
                current_date = str(current_slot_state.get("date", "")).strip()
                current_slot = _as_int(current_slot_state.get("slot"), default=-1)
                if slot_date > current_date or (
                    slot_date == current_date and slot_value > current_slot
                ):
                    merged_interval[normalized_name] = {"date": slot_date, "slot": slot_value}

        return {
            "last_run": merged_last_run,
            "last_interval_slot": merged_interval,
        }

    def _merge_runtime_state_portfolio(
        self,
        existing_raw: object,
        current_raw: object,
    ) -> dict[str, object]:
        existing = deepcopy(existing_raw) if isinstance(existing_raw, dict) else {}
        current = deepcopy(current_raw) if isinstance(current_raw, dict) else {}
        current_positions = current.get("positions")
        current_trades = current.get("trades")
        current_has_state = (
            (isinstance(current_positions, list) and len(current_positions) > 0)
            or (isinstance(current_trades, list) and len(current_trades) > 0)
            or _as_int(current.get("trade_seq"), default=0) > 0
        )
        if current_has_state:
            return current
        return existing

    def _merge_runtime_state_numeric_mapping(
        self,
        existing_raw: object,
        current_raw: object,
    ) -> dict[str, float]:
        service = self._service
        existing = service._load_runtime_state_numeric_mapping(existing_raw)
        current = service._load_runtime_state_numeric_mapping(current_raw)
        return cast(dict[str, float], current if current else existing)

    def _merge_runtime_state_broker_snapshot(
        self,
        *,
        existing_raw: dict[str, object],
        current_raw: dict[str, object],
    ) -> tuple[str, str, dict[str, float], dict[str, dict[str, object]]]:
        service = self._service
        existing_updated_at = str(existing_raw.get("broker_snapshot_updated_at", "")).strip()
        current_updated_at = str(current_raw.get("broker_snapshot_updated_at", "")).strip()
        existing_positions = service._load_runtime_state_numeric_mapping(
            existing_raw.get("broker_positions"),
        )
        current_positions = service._load_runtime_state_numeric_mapping(
            current_raw.get("broker_positions"),
        )
        existing_details = service._merge_runtime_state_mapping(
            {},
            existing_raw.get("broker_position_details"),
        )
        current_details = service._merge_runtime_state_mapping(
            {},
            current_raw.get("broker_position_details"),
        )
        existing_source = _normalize_broker_snapshot_source_for_runtime_state(
            existing_raw.get("broker_snapshot_source"),
            has_snapshot=bool(existing_positions or existing_details),
        )
        current_source = _normalize_broker_snapshot_source_for_runtime_state(
            current_raw.get("broker_snapshot_source"),
            has_snapshot=bool(current_positions or current_details),
        )
        if current_updated_at and (
            not existing_updated_at or current_updated_at >= existing_updated_at
        ):
            return current_updated_at, current_source, current_positions, current_details
        if existing_updated_at:
            return existing_updated_at, existing_source, existing_positions, existing_details
        if current_positions or current_details:
            return "", current_source, current_positions, current_details
        return "", existing_source, existing_positions, existing_details

    def _load_runtime_state_numeric_mapping(
        self,
        raw: object,
    ) -> dict[str, float]:
        normalized: dict[str, float] = {}
        if not isinstance(raw, dict):
            return normalized
        for key, value in raw.items():
            symbol = _normalize_a_share_symbol(key) or str(key).strip()
            if not symbol:
                continue
            normalized[symbol] = _as_float(value, default=0.0)
        return normalized

    def _merge_runtime_state_mapping(
        self,
        existing_raw: object,
        current_raw: object,
    ) -> dict[str, dict[str, object]]:
        merged: dict[str, dict[str, object]] = {}
        for raw in (existing_raw, current_raw):
            if not isinstance(raw, dict):
                continue
            for key, value in raw.items():
                if isinstance(value, dict):
                    merged[str(key)] = deepcopy(value)
        return merged

    def _merge_runtime_state_latest(
        self,
        existing_raw: object,
        current_raw: object,
    ) -> dict[str, object] | None:
        service = self._service
        candidates = [
            item
            for item in (
                service._runtime_state_optional_dict(existing_raw),
                service._runtime_state_optional_dict(current_raw),
            )
            if item is not None
        ]
        if not candidates:
            return None
        latest = max(candidates, key=_report_timestamp)
        return cast(dict[str, object], deepcopy(latest))

    def _merge_runtime_state_history(
        self,
        existing_raw: object,
        current_raw: object,
        *,
        limit: int,
        identity_keys: tuple[str, ...],
    ) -> list[dict[str, object]]:
        service = self._service
        merged: dict[str, dict[str, object]] = {}
        for item in service._runtime_state_dict_list(existing_raw, limit=max(1, limit) * 2):
            merged[service._runtime_state_record_key(item, identity_keys)] = deepcopy(item)
        for item in service._runtime_state_dict_list(current_raw, limit=max(1, limit) * 2):
            merged[service._runtime_state_record_key(item, identity_keys)] = deepcopy(item)
        values = list(merged.values())
        if len(values) > limit:
            values = values[-limit:]
        return values

    def _runtime_state_record_key(
        self,
        item: dict[str, object],
        identity_keys: tuple[str, ...],
    ) -> str:
        if identity_keys:
            parts = [str(item.get(key, "")).strip() for key in identity_keys]
            if any(parts):
                return "|".join(parts)
        return json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)


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


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return default


def _normalize_broker_snapshot_source_for_runtime_state(
    value: object,
    *,
    has_snapshot: bool = False,
) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"portfolio", "simulated", "simulated_portfolio"}:
        return "portfolio"
    if normalized:
        return "manual"
    if has_snapshot:
        return "manual"
    return ""


def _parse_runtime_state_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def _latest_runtime_state_datetime(
    states: list[dict[str, object]],
    key: str,
) -> datetime | None:
    timestamps = [
        timestamp
        for timestamp in (_parse_runtime_state_datetime(state.get(key)) for state in states)
        if timestamp is not None
    ]
    return max(timestamps) if timestamps else None


def _report_timestamp(report: dict[str, object]) -> float:
    raw = report.get("timestamp")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _normalize_a_share_symbol(raw: object) -> str:
    text = str(raw).strip().upper()
    if not text:
        return ""
    if "." in text:
        left, right = text.split(".", 1)
        exchange = right.strip()
        symbol = left.strip()
        if len(symbol) == 6 and exchange in {"SH", "SZ", "BJ"}:
            return f"{symbol}.{exchange}"
        return ""
    if len(text) != 6 or not text.isdigit():
        return ""
    if text.startswith("6") or text.startswith("9"):
        return f"{text}.SH"
    if text.startswith("8") or text.startswith("4"):
        return f"{text}.BJ"
    return f"{text}.SZ"
