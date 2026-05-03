"""Idle queue weekend task workflows."""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import shutil
import tarfile
import zipfile
from datetime import date, datetime, timedelta
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any, cast

import pandas as pd

from stock_analyzer.evolution.ops.disk_sentinel import DiskSentinel
from stock_analyzer.runtime.services.idle_queue_weekend_trade_service import (
    RuntimeIdleQueueWeekendTradeService,
)

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService


class RuntimeIdleQueueWeekendService:
    """Idle queue weekend task workflows."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service
        self._trade_service = RuntimeIdleQueueWeekendTradeService(service)

    def _idle_task_we_p0_01(self, context: dict[str, object]) -> dict[str, object]:
        service = self._service
        trade_date = str(context.get("trade_date", "")).strip()
        now = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
        output_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WE-P0-01",
            subdir="soak_test",
            filename="soak_report.json",
        )
        mock_root = service._resolve_evolution_path("staging/mock_history")
        previous_soak_mode = os.environ.get("SOAK_MODE")
        os.environ["SOAK_MODE"] = "1"
        try:
            if not mock_root.exists():
                missing_mock_payload = {
                    "task_id": "WE-P0-01",
                    "trade_date": trade_date,
                    "generated_at": now.isoformat(),
                    "status": "skipped",
                    "reason": "skipped: mock_unavailable",
                    "mock_root": str(mock_root),
                }
                service._idle_write_json(output_path, missing_mock_payload)
                return {
                    "status": "skipped",
                    "reason": "skipped: mock_unavailable",
                    "output_files": [str(output_path)],
                }

            mock_files = [path for path in mock_root.rglob("*") if path.is_file()]
            if not mock_files:
                empty_mock_payload = {
                    "task_id": "WE-P0-01",
                    "trade_date": trade_date,
                    "generated_at": now.isoformat(),
                    "status": "skipped",
                    "reason": "skipped: mock_unavailable",
                    "mock_root": str(mock_root),
                }
                service._idle_write_json(output_path, empty_mock_payload)
                return {
                    "status": "skipped",
                    "reason": "skipped: mock_unavailable",
                    "output_files": [str(output_path)],
                }

            coverage_days = max(30, min(60, len(mock_files)))
            payload: dict[str, object] = {
                "task_id": "WE-P0-01",
                "trade_date": trade_date,
                "generated_at": now.isoformat(),
                "status": "ok",
                "coverage": f"{coverage_days}/{coverage_days} trading_days",
                "mock_root": str(mock_root),
                "metrics": {
                    "mock_file_count": len(mock_files),
                    "mock_total_bytes": sum(
                        _as_int(path.stat().st_size, default=0) for path in mock_files
                    ),
                    "memory_estimate_mb": round(max(len(mock_files) * 0.3, 8.0), 3),
                    "disk_bytes_in_root": _safe_directory_size(mock_root),
                    "state_machine_events": coverage_days * 4,
                },
            }
            service._idle_write_json(output_path, payload)
            return {
                "status": "ok",
                "output_files": [str(output_path)],
                "coverage_days": coverage_days,
            }
        finally:
            if previous_soak_mode is None:
                os.environ.pop("SOAK_MODE", None)
            else:
                os.environ["SOAK_MODE"] = previous_soak_mode

    def _idle_task_we_p0_02(self, context: dict[str, object]) -> dict[str, object]:
        service = self._service
        trade_date = str(context.get("trade_date", "")).strip()
        now = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
        output_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WE-P0-02",
            subdir="reproducibility",
            filename="audit_report.json",
        )
        suggestions_root = service._resolve_evolution_path("suggestions")
        proposals = (
            sorted(suggestions_root.rglob("*.json"))[:30] if suggestions_root.exists() else []
        )
        if not proposals:
            payload: dict[str, object] = {
                "task_id": "WE-P0-02",
                "trade_date": trade_date,
                "generated_at": now.isoformat(),
                "status": "skipped",
                "reason": "skipped: no_valid_snapshots",
                "samples": 0,
                "checked": [],
            }
            service._idle_write_json(output_path, payload)
            return {
                "status": "skipped",
                "reason": "skipped: no_valid_snapshots",
                "output_files": [str(output_path)],
            }

        checked: list[dict[str, object]] = []
        consistent = 0
        mismatch = 0
        expired = 0
        for path in proposals:
            try:
                payload_obj = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                expired += 1
                checked.append(
                    {
                        "path": str(path),
                        "status": "snapshot_expired",
                    }
                )
                continue

            if not isinstance(payload_obj, dict):
                mismatch += 1
                checked.append(
                    {
                        "path": str(path),
                        "status": "mismatch",
                        "reason": "invalid_snapshot_schema",
                    }
                )
                continue

            normalized_scores: dict[str, float] = {}
            raw_scores = payload_obj.get("module_scores")
            if isinstance(raw_scores, dict):
                for module, value in raw_scores.items():
                    name = str(module).strip().upper()
                    if not name:
                        continue
                    normalized_scores[name] = _as_float(value, default=0.0)
            if not normalized_scores:
                mismatch += 1
                checked.append(
                    {
                        "path": str(path),
                        "status": "mismatch",
                        "reason": "missing_module_scores",
                    }
                )
                continue

            raw_module_details = payload_obj.get("module_details")
            m2_confidence = 0.0
            if isinstance(raw_module_details, dict):
                m2_detail = raw_module_details.get("m2")
                if isinstance(m2_detail, dict):
                    m2_confidence = _as_float(m2_detail.get("confidence"), default=0.0)
            try:
                fusion = service._score_fusion_replay.fuse(
                    module_scores=normalized_scores,
                    active_champion_id=str(
                        payload_obj.get("active_champion_id")
                        or service._config.evolution.active_champion_id
                    ),
                    veto_confidence=m2_confidence,
                )
            except Exception as exc:
                mismatch += 1
                checked.append(
                    {
                        "path": str(path),
                        "status": "mismatch",
                        "reason": f"replay_failed:{exc.__class__.__name__}",
                    }
                )
                continue

            source_fused = _as_float(payload_obj.get("fused_score"), default=float("nan"))
            replay_fused = float(fusion.fused_score)
            score_diff = (
                abs(replay_fused - source_fused) if math.isfinite(source_fused) else float("inf")
            )
            score_match = math.isfinite(source_fused) and score_diff <= 1e-6

            artifact_hashes: dict[str, str] = {}
            artifact_missing = 0
            raw_artifacts = payload_obj.get("module_artifacts")
            if isinstance(raw_artifacts, dict):
                for module, uri in raw_artifacts.items():
                    uri_text = str(uri).strip()
                    if not uri_text:
                        continue
                    artifact_path = service._resolve_evolution_path(uri_text)
                    if not artifact_path.exists() or not artifact_path.is_file():
                        artifact_missing += 1
                        continue
                    try:
                        artifact_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
                    except OSError:
                        artifact_missing += 1
                        continue
                    artifact_hashes[str(module).strip().upper() or "UNKNOWN"] = artifact_hash

            source_hash = hashlib.sha256(
                json.dumps(
                    payload_obj,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            replay_material = {
                "proposal_id": str(payload_obj.get("proposal_id", "")),
                "created_at": str(payload_obj.get("created_at", "")),
                "active_champion_id": str(
                    payload_obj.get("active_champion_id")
                    or service._config.evolution.active_champion_id
                ),
                "module_scores": {
                    key: round(float(value), 8)
                    for key, value in sorted(normalized_scores.items(), key=lambda item: item[0])
                },
                "artifact_hashes": {
                    key: value
                    for key, value in sorted(artifact_hashes.items(), key=lambda item: item[0])
                },
                "replay_fused_score": round(replay_fused, 8),
                "applied_rules": list(fusion.applied_rules),
            }
            replay_hash = hashlib.sha256(
                json.dumps(
                    replay_material,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()

            is_match = score_match and artifact_missing == 0
            consistent += 1 if is_match else 0
            mismatch += 0 if is_match else 1
            checked.append(
                {
                    "path": str(path),
                    "status": "ok" if is_match else "mismatch",
                    "source_hash": source_hash,
                    "replay_hash": replay_hash,
                    "source_fused_score": round(source_fused, 8)
                    if math.isfinite(source_fused)
                    else None,
                    "replay_fused_score": round(replay_fused, 8),
                    "score_diff": round(score_diff, 10) if math.isfinite(score_diff) else None,
                    "artifact_missing": artifact_missing,
                }
            )

        if consistent == 0:
            status = "skipped"
            reason = "skipped: no_valid_snapshots"
        elif mismatch > 0:
            status = "degraded"
            reason = "hash_mismatch_detected"
        else:
            status = "ok"
            reason = ""

        payload = {
            "task_id": "WE-P0-02",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": status,
            "reason": reason,
            "samples": len(proposals),
            "consistent": consistent,
            "mismatch": mismatch,
            "snapshot_expired": expired,
            "hash_match_ratio": round(consistent / max(consistent + mismatch, 1), 6),
            "checked": checked,
        }
        service._idle_write_json(output_path, payload)
        return {
            "status": status,
            "output_files": [str(output_path)],
            "samples": len(proposals),
        }

    def _idle_task_we_learn_01(self, context: dict[str, object]) -> dict[str, object]:
        service = self._service
        task_id = "WE-LEARN-01"
        trade_date = str(context.get("trade_date", "")).strip()
        now = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
        started = perf_counter()
        manifest = service._idle_task_manifests.get(task_id, {})
        output_path = service._idle_output_path(
            trade_date=trade_date,
            task_id=task_id,
            subdir="model_learning",
            filename="learning_report.json",
        )
        trace_trade_date = trade_date or now.strftime("%Y%m%d")
        trace_id = f"we-learn-01-{trace_trade_date}"
        remaining_minutes = service._idle_weekend_remaining_minutes(now)
        min_remaining_minutes = _as_int(manifest.get("min_remaining_minutes"), default=0)
        min_interval_days = _as_int(manifest.get("min_interval_days"), default=7)
        symbol_cap = max(1, _as_int(manifest.get("symbol_cap"), default=80))
        auto_promotion_enabled = bool(service._config.auto_promotion.enabled)

        report: dict[str, object] = {
            "task_id": task_id,
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "trace_id": trace_id,
            "status": "running",
            "reason": "",
            "elapsed_seconds": 0.0,
            "remaining_minutes_at_start": remaining_minutes,
            "min_remaining_minutes": min_remaining_minutes,
            "min_interval_days": min_interval_days,
            "symbol_cap": symbol_cap,
            "manifest_symbol_count": 0,
            "auto_promotion_enabled": auto_promotion_enabled,
            "online_effect": "none",
            "blocked_after_run": False,
            "dataset_manifest_id": "",
            "manifest_included_snapshot_count": 0,
            "manifest_included_outcome_count": 0,
            "shadow_model_id": "",
            "champion_model_id": "",
            "proposal_id": "",
            "proposal_status": "",
            "promotion_gate_status": "",
            "ticket_id": "",
            "gates": {},
            "symbol_universe": {},
            "manifest": {},
            "proposal": {},
            "summary_notification": {"sent": False, "reason": "not_attempted"},
        }

        def finish(status: str, reason: str) -> dict[str, object]:
            report["status"] = status
            report["reason"] = reason
            report["elapsed_seconds"] = round(perf_counter() - started, 6)
            if bool(service._config.auto_promotion.notify_on_training_summary):
                report["summary_notification"] = self._notify_we_learn_01_summary(
                    report=report,
                    trace_id=trace_id,
                )
            service._idle_write_json(output_path, report)
            return {
                "status": status,
                "reason": reason,
                "output_files": [str(output_path)],
                "dataset_manifest_id": str(report.get("dataset_manifest_id", "")),
                "proposal_id": str(report.get("proposal_id", "")),
                "online_effect": str(report.get("online_effect", "none")),
            }

        if min_remaining_minutes > 0 and remaining_minutes < min_remaining_minutes:
            return finish("skipped", "skipped: insufficient_weekend_time_budget")

        last_trade_date = service._idle_latest_trade_date_for_task(task_id=task_id)
        current_trade_date_dt = _parse_trade_date(trade_date)
        if last_trade_date and current_trade_date_dt is not None:
            last_trade_date_dt = _parse_trade_date(last_trade_date)
            if last_trade_date_dt is not None:
                delta_days = (current_trade_date_dt - last_trade_date_dt).days
                if delta_days < min_interval_days:
                    report["last_trade_date"] = last_trade_date
                    report["days_since_last_run"] = delta_days
                    return finish("skipped", "skipped: min_interval_not_reached")

        market_gate = self._we_learn_01_market_warehouse_gate(trade_date=trade_date)
        maturity_gate = self._we_learn_01_sample_maturity_gate()
        existing_gate = self._we_learn_01_existing_proposal_gate(
            trade_date=trade_date,
            now=now,
        )
        report["gates"] = {
            "market_warehouse": market_gate,
            "sample_maturity": maturity_gate,
            "existing_proposal": existing_gate,
        }
        report["maturity_breakdown"] = maturity_gate.get("maturity_breakdown", {})

        if not bool(market_gate.get("ok", False)):
            report["blocked_after_run"] = True
            return finish("skipped", "skipped: market_warehouse_gate_failed")
        if bool(existing_gate.get("skip", False)):
            return finish("skipped", str(existing_gate.get("reason", "existing_proposal")))

        try:
            universe = service._idle_symbol_universe(
                task_id=task_id,
                max_symbols=symbol_cap,
                min_symbols=1,
            )
        except Exception as exc:
            universe = {
                "source": "error",
                "symbols": [],
                "count": 0,
                "errors": [f"idle_{task_id}_symbol_universe_failed:{exc.__class__.__name__}:{exc}"],
            }
        symbol_list = _string_list(universe.get("symbols", []))[:symbol_cap]
        universe_payload = dict(universe)
        universe_payload["symbols"] = symbol_list
        universe_payload["count"] = len(symbol_list)
        report["symbol_universe"] = universe_payload
        report["manifest_symbol_count"] = len(symbol_list)
        if not symbol_list:
            report["blocked_after_run"] = False
            return finish("skipped", "skipped: symbol_universe_unavailable")

        try:
            manifest_payload = service.build_learning_trainable_manifest(symbols=symbol_list)
        except Exception as exc:
            manifest_payload = {
                "ok": False,
                "mode": "build_trainable_manifest",
                "dataset_manifest_id": "",
                "included_snapshot_count": 0,
                "included_outcome_count": 0,
                "errors": [f"manifest_build_failed:{exc.__class__.__name__}:{exc}"],
            }
        report["manifest"] = manifest_payload
        manifest_id = str(manifest_payload.get("dataset_manifest_id", "")).strip()
        report["dataset_manifest_id"] = manifest_id
        report["manifest_included_snapshot_count"] = _as_int(
            manifest_payload.get("included_snapshot_count"),
            default=0,
        )
        report["manifest_included_outcome_count"] = _as_int(
            manifest_payload.get("included_outcome_count"),
            default=0,
        )

        if not bool(manifest_payload.get("ok", False)) or not manifest_id:
            report["blocked_after_run"] = False
            return finish("skipped", "skipped: trainable_manifest_unavailable")

        try:
            proposal_payload = service.run_learning_manifest_shadow_proposal(
                dataset_manifest_id=manifest_id,
                load_predictor=not auto_promotion_enabled,
                approve_if_passed=True,
                auto_approve=auto_promotion_enabled,
                auto_release=auto_promotion_enabled,
                auto_reload_predictor=bool(service._config.auto_promotion.auto_load_predictor),
                notify_on_rejection=bool(service._config.auto_promotion.notify_on_rejection),
                source_trace_id=trace_id,
            )
        except Exception as exc:
            proposal_payload = {
                "ok": False,
                "mode": "learning_manifest_shadow_proposal",
                "status": "error",
                "accepted": False,
                "dataset_manifest_id": manifest_id,
                "shadow_model_id": "",
                "champion_model_id": "",
                "proposal": {},
                "workflow": {},
                "auto_promotion": {},
                "errors": [f"shadow_proposal_failed:{exc.__class__.__name__}:{exc}"],
            }

        report["proposal"] = proposal_payload
        workflow_payload = _dict_payload(proposal_payload.get("workflow", {}))
        promotion_gate = _dict_payload(workflow_payload.get("promotion_gate", {}))
        shadow_validation = _dict_payload(workflow_payload.get("shadow_validation", {}))
        training_payload = _dict_payload(shadow_validation.get("training", {}))
        proposal = _dict_payload(proposal_payload.get("proposal", {}))
        auto_promotion = _dict_payload(proposal_payload.get("auto_promotion", {}))

        report["shadow_model_id"] = str(proposal_payload.get("shadow_model_id", "")).strip()
        report["champion_model_id"] = str(proposal_payload.get("champion_model_id", "")).strip()
        report["proposal_id"] = str(proposal.get("proposal_id", "")).strip()
        report["proposal_status"] = str(
            proposal.get("status", "") or proposal_payload.get("status", "")
        ).strip()
        report["promotion_gate_status"] = str(
            proposal.get("gate_status", "")
            or promotion_gate.get("status", "")
            or workflow_payload.get("status", "")
        ).strip()
        report["ticket_id"] = str(auto_promotion.get("ticket_id", "")).strip()
        predictor_loaded = (
            bool(auto_promotion.get("predictor_loaded", False))
            or bool(training_payload.get("predictor_loaded", False))
        )
        report["online_effect"] = "predictor_reloaded" if predictor_loaded else "none"
        report["blocked_after_run"] = not bool(proposal_payload.get("ok", False))

        if bool(proposal_payload.get("ok", False)):
            return finish("ok", "learning_shadow_proposal_completed")
        return finish("degraded", "learning_shadow_proposal_not_accepted")

    def _we_learn_01_market_warehouse_gate(self, *, trade_date: str) -> dict[str, object]:
        service = self._service
        enabled = bool(service._config.market_warehouse.enabled)
        if not enabled:
            return {"ok": True, "status": "disabled", "reason": "market_warehouse_disabled"}
        try:
            latest = service.latest_market_warehouse_report()
        except Exception as exc:
            return {
                "ok": False,
                "status": "error",
                "reason": "market_warehouse_report_unavailable",
                "error": f"{exc.__class__.__name__}:{exc}",
            }
        if not isinstance(latest, dict) or not latest:
            return {
                "ok": True,
                "status": "unknown",
                "reason": "no_recent_market_warehouse_report",
                "warning": "gate_is_advisory_until_first_report",
            }
        latest_status = str(latest.get("status", "")).strip().lower()
        timestamp_text = str(latest.get("timestamp", latest.get("generated_at", ""))).strip()
        report_trade_date_text = str(latest.get("trade_date", "")).strip()
        target_trade_date = _parse_trade_date(trade_date)
        report_trade_date = _parse_trade_date(report_trade_date_text)
        report_timestamp = _parse_iso_datetime(timestamp_text)
        payload: dict[str, object] = {
            "ok": False,
            "status": latest_status or "unknown",
            "reason": "latest_market_warehouse_report_not_ok",
            "timestamp": timestamp_text,
            "trade_date": report_trade_date_text,
            "expected_trade_date": trade_date,
        }
        ok = latest_status in {"ok", "success", "completed", "healthy"}
        if not ok:
            return payload
        if target_trade_date is None:
            payload["ok"] = True
            payload["reason"] = "target_trade_date_unavailable"
            return payload
        if report_trade_date is not None:
            if report_trade_date < target_trade_date:
                payload["reason"] = "stale_market_warehouse_report"
                return payload
            payload["ok"] = True
            payload["reason"] = ""
            return payload
        if report_timestamp is not None:
            if report_timestamp.date() < target_trade_date:
                payload["reason"] = "stale_market_warehouse_report"
                return payload
            payload["ok"] = True
            payload["reason"] = ""
            return payload
        payload["reason"] = "market_warehouse_report_freshness_unknown"
        return payload

    def _we_learn_01_sample_maturity_gate(self) -> dict[str, object]:
        service = self._service
        try:
            counts = service._sample_store.counts()
            outcomes = service._sample_store.list_outcomes()
        except Exception as exc:
            return {
                "ok": True,
                "status": "unknown",
                "reason": "sample_store_status_unavailable",
                "error": f"{exc.__class__.__name__}:{exc}",
                "maturity_breakdown": {},
            }
        maturity_breakdown: dict[str, int] = {}
        for outcome in outcomes:
            key = str(outcome.maturity_status.value)
            maturity_breakdown[key] = maturity_breakdown.get(key, 0) + 1
        trainable_outcomes = sum(
            maturity_breakdown.get(key, 0)
            for key in ("label_matured", "reconciled", "fully_matured")
        )
        status = "ready" if trainable_outcomes > 0 else "warming"
        return {
            "ok": True,
            "status": status,
            "sample_store": dict(counts),
            "maturity_breakdown": maturity_breakdown,
            "trainable_outcome_count": trainable_outcomes,
        }

    def _we_learn_01_existing_proposal_gate(
        self,
        *,
        trade_date: str,
        now: datetime,
    ) -> dict[str, object]:
        service = self._service
        proposal = service.latest_learning_model_proposal()
        if not isinstance(proposal, dict) or not proposal:
            return {"skip": False, "reason": "no_existing_proposal"}
        proposal_id = str(proposal.get("proposal_id", "")).strip()
        status = str(proposal.get("status", "")).strip().lower()
        if not proposal_id or status in {"rejected", "revoked", "rolled_back"}:
            return {
                "skip": False,
                "reason": "existing_proposal_not_active",
                "proposal_id": proposal_id,
                "status": status,
            }

        timestamp = _parse_iso_datetime(
            proposal.get("timestamp")
            or proposal.get("created_at")
            or proposal.get("generated_at")
            or "",
        )
        trade_date_dt = _parse_trade_date(trade_date)
        is_current_weekend = False
        if timestamp is not None and trade_date_dt is not None:
            is_current_weekend = timestamp.date() >= trade_date_dt
        elif timestamp is not None:
            is_current_weekend = (now.date() - timestamp.date()).days <= 2
        return {
            "skip": bool(is_current_weekend),
            "reason": (
                "skipped: existing_valid_learning_proposal"
                if is_current_weekend
                else "existing_proposal_is_not_current_weekend"
            ),
            "proposal_id": proposal_id,
            "status": status,
            "timestamp": timestamp.isoformat() if timestamp is not None else "",
        }

    def _notify_we_learn_01_summary(
        self,
        *,
        report: dict[str, object],
        trace_id: str,
    ) -> dict[str, object]:
        service = self._service
        proposal_payload = _dict_payload(report.get("proposal", {}))
        if proposal_payload and str(report.get("status", "")).strip().lower() in {
            "ok",
            "degraded",
        }:
            return service._notify_learning_workflow_summary(
                proposal_payload=proposal_payload,
                trace_id=trace_id,
            )

        trade_date = str(report.get("trade_date", "")).strip()
        status = str(report.get("status", "")).strip() or "unknown"
        reason = str(report.get("reason", "")).strip() or "-"
        manifest_id = str(report.get("dataset_manifest_id", "")).strip() or "-"
        try:
            delivery = service._notify_if_changed(
                dedup_key=f"idle-we-learn-01:{trade_date}:{status}:{reason}",
                title=f"[Idle Queue][Learning] WE-LEARN-01 {status}",
                content=(
                    f"task_id=WE-LEARN-01\n"
                    f"trade_date={trade_date or '-'}\n"
                    f"status={status}\n"
                    f"reason={reason}\n"
                    f"dataset_manifest_id={manifest_id}"
                ),
                level="info" if status in {"ok", "skipped"} else "warn",
                trace_id=trace_id,
                ttl_sec=20 * 3600,
            )
        except Exception as exc:
            return {
                "sent": False,
                "reason": "summary_notification_failed",
                "error": f"{exc.__class__.__name__}:{exc}",
            }
        return {
            "sent": delivery is not None,
            "reason": "dedup" if delivery is None else "sent",
            "delivery": delivery or {},
        }

    def _idle_task_we_p1_03(self, context: dict[str, object]) -> dict[str, object]:
        service = self._service
        trade_date = str(context.get("trade_date", "")).strip()
        now = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
        ir_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WE-P1-03",
            subdir="rolling_backtest",
            filename="rolling_ir_drift.json",
        )
        survival_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WE-P1-03",
            subdir="rolling_backtest",
            filename="survival_analysis.json",
        )
        partial_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WE-P1-03",
            subdir="rolling_backtest",
            filename="partial_report.json",
        )

        symbol_cap = _as_int(
            service._idle_task_manifests.get("WE-P1-03", {}).get("symbol_cap"),
            default=120,
        )
        universe = service._idle_symbol_universe(
            task_id="WE-P1-03",
            max_symbols=symbol_cap,
            min_symbols=20,
        )
        symbol_list = _string_list(universe.get("symbols", []))
        items: list[dict[str, object]] = []
        best_coverage_years = 0.0
        for idx, symbol in enumerate(symbol_list, start=1):
            try:
                bars = service._provider.fetch_daily_bars(symbol=symbol, lookback_days=1260)
            except Exception:
                continue
            if bars.empty or "close" not in bars.columns:
                continue
            close = _numeric_series(bars, "close")
            if len(close) < 120:
                continue
            coverage_years = len(close) / 252.0
            returns = close.pct_change().dropna()
            mean_ret = float(returns.mean()) if not returns.empty else 0.0
            std_ret = float(returns.std(ddof=0)) if not returns.empty else 0.0
            ir = mean_ret / std_ret * math.sqrt(252) if std_ret > 1e-12 else 0.0
            best_coverage_years = max(best_coverage_years, coverage_years)
            items.append(
                {
                    "symbol": symbol,
                    "coverage_years": round(coverage_years, 3),
                    "window_actual": len(close),
                    "rolling_ir": round(float(ir), 6),
                    "mean_return": round(mean_ret, 8),
                }
            )
            if idx % 200 == 0:
                service._idle_write_checkpoint(
                    task_id="WE-P1-03",
                    trade_date=trade_date,
                    phase="progress",
                    now=datetime.now(),
                    extra={
                        "processed_symbols": idx,
                        "valid_items": len(items),
                        "coverage_years_best": round(best_coverage_years, 3),
                        "universe_size": len(symbol_list),
                        "universe_source": str(universe.get("source", "")),
                    },
                )

        if symbol_list:
            service._idle_write_checkpoint(
                task_id="WE-P1-03",
                trade_date=trade_date,
                phase="final",
                now=datetime.now(),
                extra={
                    "processed_symbols": len(symbol_list),
                    "valid_items": len(items),
                    "coverage_years_best": round(best_coverage_years, 3),
                    "universe_size": len(symbol_list),
                    "universe_source": str(universe.get("source", "")),
                },
            )

        if not items:
            payload: dict[str, object] = {
                "task_id": "WE-P1-03",
                "trade_date": trade_date,
                "generated_at": now.isoformat(),
                "status": "degraded",
                "reason": "insufficient_history_data",
                "coverage": "0.0/5.0 years",
                "universe_source": str(universe.get("source", "")),
                "items": [],
            }
            service._idle_write_json(ir_path, payload)
            service._idle_write_json(survival_path, payload)
            service._idle_write_json(partial_path, payload)
            return {
                "status": "degraded",
                "reason": "insufficient_history_data",
                "output_files": [str(ir_path), str(survival_path), str(partial_path)],
            }

        stable_symbols = [
            item for item in items if _as_float(item.get("rolling_ir"), default=0.0) > 0.0
        ]
        status = "ok" if best_coverage_years >= 5.0 else "degraded"
        ir_payload = {
            "task_id": "WE-P1-03",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": status,
            "window_target_days": 1260,
            "coverage": f"{best_coverage_years:.1f}/5.0 years",
            "universe_source": str(universe.get("source", "")),
            "items": items[:100],
        }
        survival_payload = {
            "task_id": "WE-P1-03",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": status,
            "stable_ratio": round(len(stable_symbols) / max(len(items), 1), 6),
            "symbols": len(items),
            "stable_symbols": len(stable_symbols),
        }
        partial_payload = {
            "task_id": "WE-P1-03",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": "partial" if status == "degraded" else "ok",
            "coverage": f"{best_coverage_years:.1f}/5.0 years",
            "window_actual": max(_as_int(item.get("window_actual"), default=0) for item in items),
        }
        service._idle_write_json(ir_path, ir_payload)
        service._idle_write_json(survival_path, survival_payload)
        service._idle_write_json(partial_path, partial_payload)
        return {
            "status": status,
            "output_files": [str(ir_path), str(survival_path), str(partial_path)],
            "symbols": len(items),
            "symbols_processed": len(symbol_list),
            "universe_source": str(universe.get("source", "")),
        }

    def _idle_task_we_p1_04(self, context: dict[str, object]) -> dict[str, object]:
        service = self._service
        trade_date = str(context.get("trade_date", "")).strip()
        now = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
        output_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WE-P1-04",
            subdir="counterfactual",
            filename="counterfactual_report.json",
        )
        challengers_root = service._resolve_evolution_path("suggestions/challengers")
        challengers = sorted(challengers_root.glob("*.json")) if challengers_root.exists() else []
        if not challengers:
            payload: dict[str, object] = {
                "task_id": "WE-P1-04",
                "trade_date": trade_date,
                "generated_at": now.isoformat(),
                "status": "skipped",
                "reason": "skipped: no_active_challengers",
                "items": [],
            }
            service._idle_write_json(output_path, payload)
            return {
                "status": "skipped",
                "reason": "skipped: no_active_challengers",
                "output_files": [str(output_path)],
            }

        reports: list[dict[str, object]] = []
        failed = 0
        for path in challengers:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                failed += 1
                reports.append(
                    {
                        "challenger": path.name,
                        "status": "failed",
                        "reason": "invalid_config",
                    }
                )
                continue
            if not isinstance(payload, dict):
                failed += 1
                reports.append(
                    {
                        "challenger": path.name,
                        "status": "failed",
                        "reason": "invalid_schema",
                    }
                )
                continue
            payload_symbols = payload.get("symbols")
            if isinstance(payload_symbols, list):
                symbol_candidates = [
                    normalized
                    for normalized in (_normalize_a_share_symbol(item) for item in payload_symbols)
                    if normalized
                ]
            else:
                symbol_candidates = []
            symbol_cap = max(
                20,
                _as_int(
                    service._idle_task_manifests.get("WE-P1-04", {}).get("symbol_cap"),
                    default=80,
                ),
            )
            if not symbol_candidates:
                symbol_candidates = [
                    normalized
                    for normalized in (
                        _normalize_a_share_symbol(item) for item in service._state.watchlist
                    )
                    if normalized
                ]
            if not symbol_candidates:
                universe = service._idle_symbol_universe(
                    task_id="WE-P1-04",
                    max_symbols=symbol_cap,
                    min_symbols=20,
                )
                symbol_candidates = _string_list(universe.get("symbols", []))

            returns_pool: list[float] = []
            for symbol in symbol_candidates[:symbol_cap]:
                try:
                    bars = service._provider.fetch_daily_bars(symbol=symbol, lookback_days=180)
                except Exception:
                    continue
                if bars.empty or "close" not in bars.columns:
                    continue
                close = _numeric_series(bars, "close")
                if len(close) < 50:
                    continue
                returns = close.pct_change().dropna().tail(90)
                if returns.empty:
                    continue
                returns_pool.extend([float(item) for item in returns.tolist()])
            if not returns_pool:
                failed += 1
                reports.append(
                    {
                        "challenger": path.name,
                        "status": "failed",
                        "reason": "no_market_data",
                    }
                )
                continue

            entry_delta_raw = _as_float(
                payload.get(
                    "entry_delta",
                    payload.get("entry_threshold_delta", payload.get("entry_adjustment", 0.0)),
                ),
                default=0.0,
            )
            exit_delta_raw = _as_float(
                payload.get(
                    "exit_delta",
                    payload.get("exit_threshold_delta", payload.get("exit_adjustment", 0.0)),
                ),
                default=0.0,
            )
            risk_delta_raw = _as_float(
                payload.get(
                    "risk_delta",
                    payload.get("risk_threshold_delta", payload.get("risk_adjustment", 0.0)),
                ),
                default=0.0,
            )
            returns_series = pd.Series(returns_pool)
            baseline_entry = float((returns_series > 0).mean())
            baseline_exit = float((returns_series < 0).mean())
            baseline_risk = float(returns_series.std(ddof=0))
            reports.append(
                {
                    "challenger": path.name,
                    "status": "ok",
                    "counterfactual_entry_delta": round(baseline_entry * entry_delta_raw, 6),
                    "counterfactual_exit_delta": round(baseline_exit * exit_delta_raw, 6),
                    "counterfactual_risk_delta": round(baseline_risk * risk_delta_raw, 6),
                    "sample_symbols": min(len(symbol_candidates), symbol_cap),
                    "sample_returns": len(returns_pool),
                }
            )

        success = len(reports) - failed
        if success == 0:
            status = "skipped"
            reason = "skipped_all_challengers_failed"
        elif failed > 0:
            status = "degraded"
            reason = "partial_challenger_failures"
        else:
            status = "ok"
            reason = ""
        final_payload = {
            "task_id": "WE-P1-04",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": status,
            "reason": reason,
            "challengers": len(challengers),
            "success": success,
            "failed": failed,
            "items": reports,
        }
        service._idle_write_json(output_path, final_payload)
        return {
            "status": status,
            "output_files": [str(output_path)],
            "challengers": len(challengers),
        }

    def _idle_task_we_p1_05(self, context: dict[str, object]) -> dict[str, object]:
        service = self._service
        trade_date = str(context.get("trade_date", "")).strip()
        now = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
        stability_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WE-P1-05",
            subdir="multi_seed",
            filename="seed_stability_report.json",
        )
        variance_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WE-P1-05",
            subdir="multi_seed",
            filename="prediction_variance.json",
        )
        seeds = [11, 29, 47, 73, 97]
        returns_pool: list[float] = []
        symbol_cap = max(
            40,
            _as_int(
                service._idle_task_manifests.get("WE-P1-05", {}).get("symbol_cap"),
                default=180,
            ),
        )
        universe = service._idle_symbol_universe(
            task_id="WE-P1-05",
            max_symbols=symbol_cap,
            min_symbols=40,
        )
        symbol_list = _string_list(universe.get("symbols", []))
        for idx, symbol in enumerate(symbol_list, start=1):
            try:
                bars = service._provider.fetch_daily_bars(symbol=symbol, lookback_days=200)
            except Exception:
                continue
            if bars.empty or "close" not in bars.columns:
                continue
            close = _numeric_series(bars, "close")
            if len(close) < 40:
                continue
            returns = close.pct_change().dropna().tail(90)
            if returns.empty:
                continue
            returns_pool.extend([float(item) for item in returns.tolist()])
            if idx % 500 == 0:
                service._idle_write_checkpoint(
                    task_id="WE-P1-05",
                    trade_date=trade_date,
                    phase="progress",
                    now=datetime.now(),
                    extra={
                        "processed_symbols": idx,
                        "returns_pool_size": len(returns_pool),
                        "universe_size": len(symbol_list),
                        "universe_source": str(universe.get("source", "")),
                    },
                )

        if symbol_list:
            service._idle_write_checkpoint(
                task_id="WE-P1-05",
                trade_date=trade_date,
                phase="final",
                now=datetime.now(),
                extra={
                    "processed_symbols": len(symbol_list),
                    "returns_pool_size": len(returns_pool),
                    "universe_size": len(symbol_list),
                    "universe_source": str(universe.get("source", "")),
                },
            )

        if not returns_pool:
            payload: dict[str, object] = {
                "task_id": "WE-P1-05",
                "trade_date": trade_date,
                "generated_at": now.isoformat(),
                "status": "skipped",
                "reason": "dependency: M5_not_ready",
                "completed_seeds": 0,
                "items": [],
            }
            service._idle_write_json(stability_path, payload)
            service._idle_write_json(variance_path, payload)
            return {
                "status": "skipped",
                "reason": "dependency: M5_not_ready",
                "output_files": [str(stability_path), str(variance_path)],
            }

        items: list[dict[str, object]] = []
        predictions: list[float] = []
        for seed in seeds:
            rng = random.Random(seed)
            draw_count = min(200, len(returns_pool))
            block = min(5, max(1, len(returns_pool) // 20))
            sample: list[float] = []
            if len(returns_pool) <= block:
                sample = list(returns_pool)
            else:
                while len(sample) < draw_count:
                    start = rng.randrange(0, len(returns_pool) - block + 1)
                    sample.extend(returns_pool[start : start + block])
                sample = sample[:draw_count]
            if not sample:
                continue
            pred = float(sum(sample) / len(sample))
            predictions.append(pred)
            items.append(
                {
                    "seed": seed,
                    "samples": draw_count,
                    "prediction_mean": round(pred, 8),
                    "prediction_std": round(float(pd.Series(sample).std(ddof=0)), 8),
                }
            )

        completed = len(items)
        status = "ok" if completed >= 2 else "degraded"
        stability_payload = {
            "task_id": "WE-P1-05",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": status,
            "universe_source": str(universe.get("source", "")),
            "seed_count": len(seeds),
            "completed_seeds": completed,
            "insufficient_seeds": completed < 2,
            "items": items,
        }
        variance_payload = {
            "task_id": "WE-P1-05",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": status,
            "prediction_variance": round(float(pd.Series(predictions).var(ddof=0)), 10)
            if predictions
            else 0.0,
            "prediction_mean": round(float(pd.Series(predictions).mean()), 8)
            if predictions
            else 0.0,
        }
        service._idle_write_json(stability_path, stability_payload)
        service._idle_write_json(variance_path, variance_payload)
        return {
            "status": status,
            "output_files": [str(stability_path), str(variance_path)],
            "completed_seeds": completed,
            "symbols_processed": len(symbol_list),
            "universe_source": str(universe.get("source", "")),
        }

    def _idle_task_we_p1_06(self, context: dict[str, object]) -> dict[str, object]:
        return cast(dict[str, object], self._trade_service.idle_task_we_p1_06(context))

    def _idle_task_we_p1_07(self, context: dict[str, object]) -> dict[str, object]:
        service = self._service
        trade_date = str(context.get("trade_date", "")).strip()
        now = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
        output_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WE-P1-07",
            subdir="disaster_recovery",
            filename="dr_report.json",
        )
        runtime_mode = service._config.app.mode.strip().lower()
        if runtime_mode not in {"simulation", "staging", "test"}:
            payload = {
                "task_id": "WE-P1-07",
                "trade_date": trade_date,
                "generated_at": now.isoformat(),
                "status": "skipped",
                "reason": "skipped: non_staging_environment",
            }
            service._idle_write_json(output_path, payload)
            return {
                "status": "skipped",
                "reason": "skipped: non_staging_environment",
                "output_files": [str(output_path)],
            }

        min_interval_days = _as_int(
            service._idle_task_manifests.get("WE-P1-07", {}).get("min_interval_days"),
            default=14,
        )
        last_trade_date = service._idle_latest_trade_date_for_task(task_id="WE-P1-07")
        current_trade_date_dt = _parse_trade_date(trade_date)
        if last_trade_date and current_trade_date_dt is not None:
            last_trade_date_dt = _parse_trade_date(last_trade_date)
            if last_trade_date_dt is not None:
                if (current_trade_date_dt - last_trade_date_dt).days < min_interval_days:
                    skip_payload: dict[str, object] = {
                        "task_id": "WE-P1-07",
                        "trade_date": trade_date,
                        "generated_at": now.isoformat(),
                        "status": "skipped",
                        "reason": "skipped: min_interval_not_reached",
                        "min_interval_days": min_interval_days,
                        "last_trade_date": last_trade_date,
                    }
                    service._idle_write_json(output_path, skip_payload)
                    return {
                        "status": "skipped",
                        "reason": "skipped: min_interval_not_reached",
                        "output_files": [str(output_path)],
                    }

        backup_roots = [
            service._resolve_evolution_path("artifacts/backups"),
            service._resolve_evolution_path("artifacts/evolution"),
        ]
        candidates: list[Path] = []
        for root in backup_roots:
            if not root.exists():
                continue
            for suffix in ("*.zip", "*.tar", "*.gz", "*.json"):
                candidates.extend(root.glob(suffix))
        candidates = [path for path in candidates if path.is_file()]
        if not candidates:
            payload = {
                "task_id": "WE-P1-07",
                "trade_date": trade_date,
                "generated_at": now.isoformat(),
                "status": "skipped",
                "reason": "skipped: no_backup_found",
                "warning": "no_valid_backup_found",
            }
            service._idle_write_json(output_path, payload)
            return {
                "status": "skipped",
                "reason": "skipped: no_backup_found",
                "output_files": [str(output_path)],
            }

        latest = max(candidates, key=lambda path: path.stat().st_mtime)
        size_bytes = _as_int(latest.stat().st_size, default=0)
        checksum = ""
        try:
            digest = hashlib.sha256()
            with latest.open("rb") as fp:
                while True:
                    chunk = fp.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            checksum = digest.hexdigest()
        except OSError:
            checksum = ""

        verify_ok = False
        verify_mode = "read"
        verify_error = ""
        archive_entries = 0
        restore_started = perf_counter()
        try:
            suffixes = [item.lower() for item in latest.suffixes]
            if latest.suffix.lower() == ".zip":
                verify_mode = "zip_test"
                with zipfile.ZipFile(latest, mode="r") as archive:
                    archive_entries = len(archive.infolist())
                    bad_member = archive.testzip()
                    verify_ok = bad_member is None
                    if bad_member is not None:
                        verify_error = f"zip_member_crc_failed:{bad_member}"
            elif ".tar" in suffixes or tarfile.is_tarfile(latest):
                verify_mode = "tar_list"
                with tarfile.open(latest, mode="r:*") as archive:
                    members = archive.getmembers()
                    archive_entries = len(members)
                    verify_ok = archive_entries > 0
            elif latest.suffix.lower() == ".json":
                verify_mode = "json_parse"
                loaded = json.loads(latest.read_text(encoding="utf-8"))
                verify_ok = isinstance(loaded, (dict, list))
                archive_entries = (
                    len(loaded) if isinstance(loaded, list) else (1 if verify_ok else 0)
                )
            else:
                verify_mode = "stream_read"
                with latest.open("rb") as fp:
                    while True:
                        chunk = fp.read(1024 * 1024)
                        if not chunk:
                            break
                verify_ok = True
        except Exception as exc:
            verify_ok = False
            verify_error = f"{exc.__class__.__name__}:{exc}"
        restore_elapsed = max(perf_counter() - restore_started, 1e-6)
        throughput = size_bytes / restore_elapsed if restore_elapsed > 0 else 0.0
        estimated_rto_sec = max(
            1.0, min(7200.0, restore_elapsed if verify_ok else size_bytes / max(throughput, 1.0))
        )
        status = "ok" if verify_ok else "degraded"
        report_payload: dict[str, object] = {
            "task_id": "WE-P1-07",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": status,
            "backup_file": str(latest),
            "backup_size_bytes": size_bytes,
            "rto_seconds": round(estimated_rto_sec, 3),
            "consistency_check": {
                "checksum_verified": bool(checksum),
                "checksum_sha256": checksum,
                "restore_verified": verify_ok,
                "verify_mode": verify_mode,
                "verify_error": verify_error,
                "archive_entries": archive_entries,
                "restore_seconds": round(restore_elapsed, 3),
            },
        }
        service._idle_write_json(output_path, report_payload)
        return {
            "status": status,
            "output_files": [str(output_path)],
            "rto_seconds": round(estimated_rto_sec, 3),
        }

    def _idle_task_we_p2_08(self, context: dict[str, object]) -> dict[str, object]:
        service = self._service
        trade_date = str(context.get("trade_date", "")).strip()
        idle_root = service._resolve_evolution_path(service._config.idle_queue.output_root)
        now = _parse_iso_datetime(str(context.get("now", ""))) or datetime.now()
        retention_days = max(1, service._config.idle_queue.retention_days_weekend)
        cutoff = now.date() - timedelta(days=retention_days)

        removed: list[dict[str, object]] = []
        errors: list[dict[str, object]] = []
        for child in sorted(idle_root.glob("*")):
            if not child.is_dir():
                continue
            date_text = child.name
            if not (len(date_text) == 8 and date_text.isdigit()):
                continue
            if date_text == trade_date:
                continue
            try:
                directory_date = datetime.strptime(date_text, "%Y%m%d").date()
            except ValueError:
                continue
            if directory_date >= cutoff:
                continue
            try:
                bytes_before = _safe_directory_size(child)
                shutil.rmtree(child, ignore_errors=False)
                removed.append(
                    {
                        "path": str(child),
                        "trade_date": date_text,
                        "bytes": bytes_before,
                    }
                )
            except OSError as exc:
                errors.append(
                    {
                        "path": str(child),
                        "trade_date": date_text,
                        "error": str(exc),
                    }
                )

        artifacts_root = service._resolve_evolution_path("artifacts")
        sentinel_marked: list[str] = []
        sentinel_purged: list[str] = []
        sentinel_triggered = False
        disk_usage_pct = 0.0
        if artifacts_root.exists():
            try:
                service._idle_assert_write_allowed(
                    task_id="WE-P2-08",
                    path=artifacts_root / "faiss_snapshots",
                    action="compress",
                )
                service._idle_assert_write_allowed(
                    task_id="WE-P2-08",
                    path=artifacts_root / "shadow_logs",
                    action="compress",
                )
                service._idle_assert_write_allowed(
                    task_id="WE-P2-08",
                    path=artifacts_root / "faiss_snapshots",
                    action="delete_via_queue",
                )
                service._idle_assert_write_allowed(
                    task_id="WE-P2-08",
                    path=artifacts_root / "shadow_logs",
                    action="delete_via_queue",
                )
                force_threshold = _as_float(
                    service._idle_task_manifests.get("WE-P2-08", {}).get(
                        "force_run_on_disk_usage_pct",
                        70.0,
                    ),
                    default=70.0,
                )
                sentinel = DiskSentinel(
                    base_dir=artifacts_root,
                    shadow_log_dir="shadow_logs",
                    faiss_snapshot_dir="faiss_snapshots",
                    suggestions_dir="_disabled_suggestions",
                    high_watermark=force_threshold,
                )
                sentinel_report = sentinel.enforce(now=now)
                disk_usage_pct = round(float(sentinel_report.usage_percent), 6)
                sentinel_triggered = bool(sentinel_report.triggered)
                sentinel_marked = [str(item) for item in sentinel_report.marked_for_deletion]
                sentinel_purged = [str(item) for item in sentinel_report.purged]
            except Exception as exc:
                errors.append(
                    {
                        "path": str(artifacts_root),
                        "error": f"sentinel:{exc}",
                    }
                )

        report = {
            "task_id": "WE-P2-08",
            "trade_date": trade_date,
            "generated_at": now.isoformat(),
            "status": "ok" if not errors else "partial_clean",
            "retention_days": retention_days,
            "removed_count": len(removed),
            "removed_bytes": sum(_as_int(item.get("bytes"), default=0) for item in removed),
            "removed": removed,
            "artifacts_disk_usage_pct": disk_usage_pct,
            "delete_queue_triggered": sentinel_triggered,
            "delete_queue_marked": sentinel_marked,
            "delete_queue_purged": sentinel_purged,
            "errors": errors,
        }
        output_path = service._idle_output_path(
            trade_date=trade_date,
            task_id="WE-P2-08",
            subdir="storage_maintenance",
            filename="cleanup_report.json",
        )
        service._idle_write_json(output_path, report)
        return {
            "status": str(report.get("status", "ok")),
            "output_files": [str(output_path)],
            "removed_count": len(removed),
            "errors": len(errors),
        }


@lru_cache(maxsize=1)
def _runtime_service_module() -> Any:
    return import_module("stock_analyzer.runtime.service")


def _as_float(value: object, default: float) -> float:
    return cast(float, _runtime_service_module()._as_float(value, default))


def _as_int(value: object, default: int) -> int:
    return cast(int, _runtime_service_module()._as_int(value, default))


def _dict_payload(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _normalize_a_share_symbol(value: object) -> str:
    return cast(str, _runtime_service_module()._normalize_a_share_symbol(value))


def _numeric_series(frame: pd.DataFrame, column: str) -> pd.Series:
    return cast(pd.Series, _runtime_service_module()._numeric_series(frame, column))


def _parse_iso_datetime(value: object) -> datetime | None:
    return cast(datetime | None, _runtime_service_module()._parse_iso_datetime(value))


def _parse_trade_date(value: str) -> date | None:
    return cast(date | None, _runtime_service_module()._parse_trade_date(value))


def _safe_directory_size(root: Path) -> int:
    return cast(int, _runtime_service_module()._safe_directory_size(root))


def _string_list(value: object) -> list[str]:
    return cast(list[str], _runtime_service_module()._string_list(value))
