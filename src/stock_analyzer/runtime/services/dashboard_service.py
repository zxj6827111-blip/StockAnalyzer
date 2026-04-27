"""Dashboard aggregation workflows extracted from the main runtime service."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Any, cast


_TRAINING_OVERVIEW_CACHE_TTL_SEC = 15.0


class RuntimeDashboardService:
    """Delegated dashboard aggregation and evolution dashboard helpers."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def dashboard_portfolio(self, days: int = 7, trade_limit: int = 120) -> dict[str, object]:
        service = self._service
        service._refresh_runtime_state_from_disk_if_changed()
        capped_days = max(1, days)
        capped_trade_limit = max(1, trade_limit)
        now = datetime.now()
        cutoff = now.timestamp() - capped_days * 86400
        positions = service.portfolio_positions()
        trades = service.portfolio_trades(limit=capped_trade_limit)
        latest = service.latest_report()

        recent_runs = [item for item in service._run_summaries if _report_timestamp(item) >= cutoff]
        equity_curve = [
            {
                "timestamp": str(item.get("timestamp", "")),
                "equity": _as_float(item.get("equity"), default=service._state.current_equity),
                "drawdown_pct": _as_float(item.get("drawdown_pct"), default=0.0),
                "risk_action": str(item.get("risk_action", "")),
            }
            for item in recent_runs
        ]
        if not equity_curve:
            equity_curve = [
                {
                    "timestamp": now.isoformat(),
                    "equity": service._state.current_equity,
                    "drawdown_pct": 0.0,
                    "risk_action": "unknown",
                }
            ]

        positions_panel = _build_positions_panel(positions, now=now)
        execution_quality = cast(
            dict[str, object],
            service._execution_quality_snapshot(
                days=capped_days,
                trades=trades,
            ),
        )
        latest_trace_id = ""
        latest_risk_action = ""
        latest_degraded_mode = False
        if latest is not None:
            latest_trace_id = str(latest.get("trace_id", ""))
            latest_degraded_mode = bool(latest.get("degraded_mode", False))
            latest_risk = latest.get("risk")
            if isinstance(latest_risk, dict):
                latest_risk_action = str(latest_risk.get("action", ""))
        evolution_m8_latest = service._build_dashboard_evolution_m8_latest()
        evolution_m10_latest = service._build_dashboard_evolution_m10_latest()
        evolution_m11_latest = service._build_dashboard_evolution_m11_latest()
        news_watchlist_preview = service.preview_news_watchlist(
            strategy="trend",
            limit=10,
            record_audit=False,
        )
        recommendation_panel = service.recommendation_lifecycle(limit=60)
        holding_alerts = service.holding_alerts(now=now)
        execution_bias = service.execution_bias_report(days=min(30, capped_days * 3), limit=60)
        return {
            "summary": {
                "days": capped_days,
                "open_positions": len(positions),
                "recent_trades": len(trades),
                "recent_runs": len(recent_runs),
                "current_equity": service._state.current_equity,
                "latest_trace_id": latest_trace_id,
                "latest_risk_action": latest_risk_action,
                "degraded_mode": latest_degraded_mode,
            },
            "positions_panel": positions_panel,
            "recommendation_panel": recommendation_panel,
            "holding_alerts": holding_alerts,
            "execution_bias": execution_bias,
            "recent_trades": trades,
            "equity_curve": equity_curve,
            "execution_quality": execution_quality,
            "reconcile_weekly": service.reconcile_weekly_report(days=capped_days),
            "sla": service.sla_report(),
            "recent_events": service.audit_events(limit=20).get("events", []),
            "acceptance_week4_latest": service._last_week4_acceptance_report,
            "week5_latest": service._last_week5_scan_report,
            "week6_latest": service._last_week6_report,
            "week6_data_quality_latest": service._last_week6_data_quality_report,
            "week7_kill_switch": service.strategy_kill_switch_status(),
            "week7_cloud_backup": service.cloud_backup_status(),
            "week7_factor_lifecycle": service.factor_lifecycle_status(),
            "week7_sim_broker_latest": service._last_week7_sim_broker_report,
            "news_watchlist_preview": news_watchlist_preview,
            "evolution_latest": service._last_evolution_report,
            "evolution_m8_latest": evolution_m8_latest,
            "evolution_m10_latest": evolution_m10_latest,
            "evolution_m11_latest": evolution_m11_latest,
            "evolution_history_count": len(service._evolution_history),
            "evolution_release_gate_latest": service._last_evolution_release_gate,
            "evolution_release_approval_latest": service._last_evolution_release_approval,
            "evolution_release_ticket_latest": service._last_evolution_release_ticket,
            "evolution_release_confirmation_required": (
                service._config.evolution.release_confirmation_required
            ),
            "evolution_release_confirmation_ttl_days": (
                service._config.evolution.release_confirmation_ttl_days
            ),
            "evolution_release_confirmation_pending_count": (
                service._evolution_pending_confirmation_count()
            ),
        }

    def training_overview(self, history_limit: int = 6) -> dict[str, object]:
        service = self._service
        capped_history_limit = max(1, min(history_limit, 20))
        cached = _load_training_overview_cache(
            service,
            history_limit=capped_history_limit,
        )
        if cached is not None:
            return cached

        bootstrap = service.training_bootstrap_status()
        latest_evolution = service.latest_evolution_report()
        evolution_history = service.evolution_history(limit=capped_history_limit)
        evolution_window = service.evolution_window_report(days=10, min_runs=5)
        latest_acceptance = service.latest_week4_acceptance_report()
        runtime_stage = service.runtime_stage_snapshot()
        warehouse_background = _as_dict(runtime_stage.get("market_warehouse_background_data"))
        if not warehouse_background:
            warehouse_background = service.market_warehouse_background_data_status()
        warehouse_context = _resolve_training_overview_warehouse_context(
            current_background=warehouse_background,
            latest_report=service.latest_market_warehouse_report(),
            progress=_as_dict(runtime_stage.get("market_warehouse_progress")),
            lock=_as_dict(runtime_stage.get("market_warehouse_lock")),
        )
        warehouse_background = _as_dict(warehouse_context.get("background"))

        project_root = service._evolution_project_root
        model_artifact_path = service._resolve_evolution_path(str(service._config.training.artifact_path))
        baseline_report_path = service._resolve_evolution_path(
            str(service._config.training.baseline_report_path)
        )
        training_eval_path = service._resolve_evolution_path(
            "artifacts/acceptance/training_evaluation_report.json"
        )

        model_artifact = _load_json_mapping(model_artifact_path)
        baseline_report = _load_json_mapping(baseline_report_path)
        training_eval_report = _load_json_mapping(training_eval_path)

        bootstrap_payload = dict(bootstrap)
        bootstrap_payload["last_bootstrap_age_hours"] = _age_hours(
            bootstrap_payload.get("last_bootstrap_at")
        )
        bootstrap_payload["artifact_file"] = _file_summary(
            model_artifact_path,
            project_root=project_root,
        )

        model_metadata = _as_dict(model_artifact.get("metadata"))
        model_training_metrics = _as_dict(model_artifact.get("training_metrics"))

        evolution_items = evolution_history.get("items") if isinstance(evolution_history, dict) else []
        recent_evolution_runs: list[dict[str, object]] = []
        if isinstance(evolution_items, list):
            for raw_item in reversed(evolution_items[-capped_history_limit:]):
                if not isinstance(raw_item, dict):
                    continue
                recent_evolution_runs.append(_summarize_evolution_history_item(raw_item))

        runtime_summary = {
            "as_of": str(runtime_stage.get("as_of", "")),
            "mode": str(_as_dict(runtime_stage.get("summary")).get("mode", "")),
            "phase": _as_dict(runtime_stage.get("runtime_phase")),
            "health": _as_dict(runtime_stage.get("health")),
            "next_task": _as_dict(_as_dict(runtime_stage.get("summary")).get("pending_next")),
            "latest_activity": _as_dict(runtime_stage.get("latest_activity")),
        }

        payload = {
            "generated_at": datetime.now().isoformat(),
            "bootstrap": bootstrap_payload,
            "model_artifact": {
                **_file_summary(model_artifact_path, project_root=project_root),
                "created_at": str(model_artifact.get("created_at", "")),
                "feature_count": _list_length(model_artifact.get("feature_columns")),
                "metadata": {
                    "artifact_created_at": str(model_metadata.get("artifact_created_at", "")),
                    "lgbm_backend": str(model_metadata.get("lgbm_backend", "")),
                    "xgb_backend": str(model_metadata.get("xgb_backend", "")),
                    "degraded_model_mode": bool(
                        model_metadata.get("degraded_model_mode", False)
                    ),
                    "calibration_method": str(model_metadata.get("calibration_method", "")),
                    "train_samples": _as_int(model_metadata.get("train_samples"), default=0),
                    "calibration_samples": _as_int(
                        model_metadata.get("calibration_samples"),
                        default=0,
                    ),
                    "test_samples": _as_int(model_metadata.get("test_samples"), default=0),
                    "embargo_days": _as_int(model_metadata.get("embargo_days"), default=0),
                    "label_conflict_policy": str(
                        model_metadata.get("label_conflict_policy", "")
                    ),
                    "dependency_status": _as_dict(model_metadata.get("dependency_status")),
                },
                "training_metrics": _pick_mapping_fields(
                    model_training_metrics,
                    (
                        "accuracy",
                        "auc",
                        "brier",
                        "precision_at_k",
                        "recall_at_k",
                        "mean_prob_spread",
                        "validation_samples",
                    ),
                ),
            },
            "training_evaluation": {
                **_file_summary(training_eval_path, project_root=project_root),
                "generated_at": str(training_eval_report.get("generated_at", "")),
                "symbol": str(training_eval_report.get("symbol", "")),
                "lookback_days": _as_int(training_eval_report.get("lookback_days"), default=0),
                "dataset": _as_dict(training_eval_report.get("dataset")),
                "strict_temporal": _summarize_training_regime(
                    _as_dict(_as_dict(training_eval_report.get("split_regimes")).get("strict_temporal"))
                ),
                "legacy_validation_only": _summarize_training_regime(
                    _as_dict(
                        _as_dict(training_eval_report.get("split_regimes")).get(
                            "legacy_validation_only"
                        )
                    )
                ),
            },
            "baseline": {
                **_file_summary(baseline_report_path, project_root=project_root),
                "generated_at": str(baseline_report.get("generated_at", "")),
                "symbol": str(baseline_report.get("symbol", "")),
                "lookback_days": _as_int(baseline_report.get("lookback_days"), default=0),
                "baseline_type": str(baseline_report.get("baseline_type", "")),
                "model_status": _as_dict(baseline_report.get("model_status")),
                "dependency_status": _as_dict(baseline_report.get("dependency_status")),
                "walk_forward_summary": _as_dict(
                    _as_dict(baseline_report.get("walk_forward")).get("summary")
                ),
                "background_factor_coverage": _as_dict(
                    baseline_report.get("background_factor_coverage")
                ),
            },
            "acceptance": _summarize_acceptance_report(latest_acceptance),
            "evolution": {
                "latest": _summarize_evolution_report(latest_evolution),
                "window": evolution_window,
                "recent_runs": recent_evolution_runs,
            },
            "warehouse": {
                "background": warehouse_background,
                "background_source": str(warehouse_context.get("background_source", "")),
                "active_sync": _as_dict(warehouse_context.get("active_sync")),
                "latest_completed_sync": _as_dict(
                    warehouse_context.get("latest_completed_sync")
                ),
                "raw_background": _as_dict(warehouse_context.get("raw_background")),
            },
            "runtime": runtime_summary,
        }
        _store_training_overview_cache(
            service,
            history_limit=capped_history_limit,
            payload=payload,
        )
        return payload

    def _latest_evolution_modules(self) -> dict[str, object]:
        latest = self._service._last_evolution_report
        if not isinstance(latest, dict):
            return {}
        modules = latest.get("modules")
        if not isinstance(modules, dict):
            return {}
        return cast(dict[str, object], modules)

    def _build_dashboard_evolution_m8_latest(self) -> dict[str, object] | None:
        service = self._service
        modules = service._latest_evolution_modules()
        raw_m8 = modules.get("m8")
        if not isinstance(raw_m8, dict):
            return None

        summary = raw_m8.get("summary")
        if not isinstance(summary, dict):
            summary = {}
        artifact_uri = str(raw_m8.get("artifact_uri", "")).strip()
        items = service._load_dashboard_m8_items(artifact_uri=artifact_uri)
        return {
            "summary": summary,
            "artifact_uri": artifact_uri,
            "items": items,
        }

    def _load_dashboard_m8_items(self, artifact_uri: str) -> list[dict[str, object]]:
        service = self._service
        if not artifact_uri:
            return []
        path = service._resolve_evolution_path(artifact_uri)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, dict):
            return []

        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            return []

        items: list[dict[str, object]] = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue

            gate_checks: list[dict[str, object]] = []
            raw_gate_checks = raw_item.get("gate_checks")
            if isinstance(raw_gate_checks, list):
                for raw_gate in raw_gate_checks:
                    if not isinstance(raw_gate, dict):
                        continue
                    gate_checks.append(
                        {
                            "name": str(raw_gate.get("name", "")),
                            "passed": bool(raw_gate.get("passed", False)),
                            "value": raw_gate.get("value"),
                            "threshold": raw_gate.get("threshold"),
                            "detail": str(raw_gate.get("detail", "")),
                            "provenance": str(raw_gate.get("provenance", "computed")),
                        }
                    )

            failed_gates: list[str] = []
            raw_failed_gates = raw_item.get("failed_gates")
            if isinstance(raw_failed_gates, list):
                failed_gates = [str(item) for item in raw_failed_gates]
            missing_gate_inputs: list[str] = []
            raw_missing_gate_inputs = raw_item.get("missing_gate_inputs")
            if isinstance(raw_missing_gate_inputs, list):
                missing_gate_inputs = [str(item) for item in raw_missing_gate_inputs]
            derived_gate_inputs: list[str] = []
            raw_derived_gate_inputs = raw_item.get("derived_gate_inputs")
            if isinstance(raw_derived_gate_inputs, list):
                derived_gate_inputs = [str(item) for item in raw_derived_gate_inputs]

            gate_total = _as_int(raw_item.get("gate_total"), default=len(gate_checks))
            if gate_total <= 0 and gate_checks:
                gate_total = len(gate_checks)
            passed_gates = _as_int(
                raw_item.get("passed_gates"),
                default=max(gate_total - len(failed_gates), 0),
            )

            items.append(
                {
                    "symbol": str(raw_item.get("symbol", "")),
                    "recommendation": str(raw_item.get("recommendation", "")),
                    "best_similarity": _as_float(raw_item.get("best_similarity"), default=0.0),
                    "passed_gates": max(0, passed_gates),
                    "gate_total": max(0, gate_total),
                    "failed_gates": failed_gates,
                    "missing_gate_inputs": missing_gate_inputs,
                    "derived_gate_inputs": derived_gate_inputs,
                    "registry_signature": str(raw_item.get("registry_signature", "")),
                    "gate_checks": gate_checks,
                }
            )
        return items

    def _build_dashboard_evolution_m10_latest(self) -> dict[str, object] | None:
        service = self._service
        modules = service._latest_evolution_modules()
        raw_m10 = modules.get("m10")
        if not isinstance(raw_m10, dict):
            return None
        raw_metrics = raw_m10.get("metrics")
        metrics = raw_metrics if isinstance(raw_metrics, dict) else {}
        return {
            "status": str(raw_m10.get("status", "")),
            "score": _as_float(raw_m10.get("score"), default=0.0),
            "metrics": {
                "valid_symbols": _as_int(metrics.get("valid_symbols"), default=0),
                "prediction_coverage_ratio": _as_float(
                    metrics.get("prediction_coverage_ratio"),
                    default=0.0,
                ),
                "mean_model_spread": _as_float(metrics.get("mean_model_spread"), default=0.0),
                "high_conflict_ratio": _as_float(metrics.get("high_conflict_ratio"), default=0.0),
                "calibration_gap": _as_float(metrics.get("calibration_gap"), default=0.0),
                "return_volatility": _as_float(metrics.get("return_volatility"), default=0.0),
            },
        }

    def _build_dashboard_evolution_m11_latest(self) -> dict[str, object] | None:
        service = self._service
        modules = service._latest_evolution_modules()
        raw_m11 = modules.get("m11")
        if not isinstance(raw_m11, dict):
            return None
        raw_metrics = raw_m11.get("metrics")
        metrics = raw_metrics if isinstance(raw_metrics, dict) else {}
        raw_redlines = raw_m11.get("redlines")
        redlines_raw = raw_redlines if isinstance(raw_redlines, dict) else {}
        redlines = {str(name): bool(value) for name, value in redlines_raw.items()}

        attribution: list[dict[str, object]] = []
        raw_attribution = raw_m11.get("attribution")
        if isinstance(raw_attribution, list):
            for raw_item in raw_attribution:
                if not isinstance(raw_item, dict):
                    continue
                attribution.append(
                    {
                        "name": str(raw_item.get("name", "")),
                        "value": _as_float(raw_item.get("value"), default=0.0),
                        "threshold": _as_float(raw_item.get("threshold"), default=0.0),
                        "breached": bool(raw_item.get("breached", False)),
                        "impact": _as_float(raw_item.get("impact"), default=0.0),
                    }
                )

        return {
            "status": str(raw_m11.get("status", "")),
            "score": _as_float(raw_m11.get("score"), default=0.0),
            "redlines": redlines,
            "metrics": {
                "valid_samples": _as_int(metrics.get("valid_samples"), default=0),
                "champion_cum_return": _as_float(
                    metrics.get("champion_cum_return"),
                    default=0.0,
                ),
                "challenger_cum_return": _as_float(
                    metrics.get("challenger_cum_return"),
                    default=0.0,
                ),
                "champion_max_drawdown": _as_float(
                    metrics.get("champion_max_drawdown"),
                    default=0.0,
                ),
                "challenger_max_drawdown": _as_float(
                    metrics.get("challenger_max_drawdown"),
                    default=0.0,
                ),
                "drawdown_delta": _as_float(metrics.get("drawdown_delta"), default=0.0),
                "tail_loss_delta": _as_float(metrics.get("tail_loss_delta"), default=0.0),
                "execution_divergence_ratio": _as_float(
                    metrics.get("execution_divergence_ratio"),
                    default=0.0,
                ),
                "champion_win_rate": _as_float(metrics.get("champion_win_rate"), default=0.0),
                "challenger_win_rate": _as_float(
                    metrics.get("challenger_win_rate"),
                    default=0.0,
                ),
            },
            "attribution": attribution,
        }


def _report_timestamp(report: dict[str, object]) -> float:
    raw = report.get("timestamp")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _build_positions_panel(
    positions: list[dict[str, object]],
    now: datetime,
) -> list[dict[str, object]]:
    panel: list[dict[str, object]] = []
    for item in positions:
        opened_raw = item.get("opened_at")
        opened_at = _parse_iso_datetime(opened_raw)
        hold_days = 0
        if opened_at is not None:
            hold_days = max(0, (now.date() - opened_at.date()).days)
        panel.append(
            {
                "symbol": str(item.get("symbol", "")),
                "strategy": str(item.get("strategy", "")),
                "target_position": _as_float(item.get("target_position"), default=0.0),
                "entry_price": _as_float(item.get("entry_price"), default=0.0),
                "quantity": _as_int(item.get("quantity"), default=0),
                "fee": _as_float(item.get("fee"), default=0.0),
                "account": str(item.get("account", "")),
                "manual_trade_time": str(item.get("manual_trade_time", "")),
                "note": str(item.get("note", "")),
                "status": str(item.get("status", "")),
                "hold_days": hold_days,
                "opened_at": str(item.get("opened_at", "")),
                "updated_at": str(item.get("updated_at", "")),
            }
        )
    return panel


def _parse_iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


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


def _as_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _list_length(value: object) -> int:
    if not isinstance(value, list):
        return 0
    return len(value)


def _resolve_relative_path(path: Path, project_root: Path) -> str:
    try:
        return str(path.relative_to(project_root)).replace("\\", "/")
    except ValueError:
        return str(path)


def _file_summary(path: Path, *, project_root: Path) -> dict[str, object]:
    exists = path.exists()
    updated_at = ""
    size_bytes = 0
    if exists:
        stats = path.stat()
        updated_at = datetime.fromtimestamp(stats.st_mtime).isoformat()
        size_bytes = int(stats.st_size)
    return {
        "path": _resolve_relative_path(path, project_root),
        "exists": exists,
        "updated_at": updated_at,
        "size_bytes": size_bytes,
        "age_hours": _age_hours(updated_at),
    }


def _load_json_mapping(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return _as_dict(payload)


def _age_hours(value: object) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    delta_seconds = datetime.now().timestamp() - parsed.timestamp()
    return round(delta_seconds / 3600, 2)


def _pick_mapping_fields(
    payload: dict[str, object],
    names: tuple[str, ...],
) -> dict[str, object]:
    picked: dict[str, object] = {}
    for name in names:
        if name in payload:
            picked[name] = payload[name]
    return picked


def _resolve_training_overview_warehouse_context(
    *,
    current_background: dict[str, object],
    latest_report: object,
    progress: dict[str, object],
    lock: dict[str, object],
) -> dict[str, object]:
    active_sync = _summarize_active_warehouse_sync(progress=progress, lock=lock)
    latest_report_payload = _as_dict(latest_report)
    latest_background = _as_dict(latest_report_payload.get("background_data"))
    latest_completed_sync = _summarize_latest_completed_warehouse_sync(latest_report_payload)

    background_source = "current_snapshot"
    display_background = dict(current_background)
    if bool(active_sync.get("running", False)) and latest_background:
        background_source = "latest_completed_sync"
        display_background = dict(latest_background)
        display_background["display_source"] = background_source
        display_background["display_reason"] = "active_market_warehouse_sync_in_progress"
        display_background["active_sync_target_trade_date"] = str(
            active_sync.get("target_trade_date", "")
        )
    elif display_background:
        display_background.setdefault("display_source", background_source)

    return {
        "background": display_background,
        "background_source": background_source,
        "active_sync": active_sync,
        "latest_completed_sync": latest_completed_sync,
        "raw_background": dict(current_background),
    }


def _summarize_active_warehouse_sync(
    *,
    progress: dict[str, object],
    lock: dict[str, object],
) -> dict[str, object]:
    active_progress = _as_dict(lock.get("active_progress")) or dict(progress)
    lock_running = bool(lock.get("running", False))
    progress_running = str(active_progress.get("status", "")).strip().lower() == "running"
    if not lock_running and not progress_running:
        return {"running": False}

    trace_id = str(active_progress.get("trace_id", "")).strip() or str(
        lock.get("trace_id", "")
    ).strip()
    return {
        "running": True,
        "trace_id": trace_id,
        "status": str(active_progress.get("status", "")).strip() or "running",
        "phase": str(active_progress.get("phase", "")).strip(),
        "current_symbol": str(active_progress.get("current_symbol", "")).strip(),
        "current_stage": str(active_progress.get("current_stage", "")).strip(),
        "target_trade_date": str(active_progress.get("target_trade_date", "")).strip(),
        "symbols_completed": _as_int(active_progress.get("symbols_completed"), default=0),
        "symbols_total": _as_int(active_progress.get("symbols_total"), default=0),
        "progress_ratio": _as_float(active_progress.get("progress_ratio"), default=0.0),
        "failed_symbols_total": _as_int(
            active_progress.get("failed_symbols_total"),
            default=0,
        ),
        "started_at": str(active_progress.get("started_at", "")).strip(),
        "updated_at": str(active_progress.get("updated_at", "")).strip()
        or str(lock.get("last_heartbeat_at", "")).strip(),
    }


def _summarize_latest_completed_warehouse_sync(
    latest_report: dict[str, object],
) -> dict[str, object]:
    if not latest_report:
        return {}
    background = _as_dict(latest_report.get("background_data"))
    return {
        "timestamp": str(latest_report.get("timestamp", "")).strip(),
        "trace_id": str(latest_report.get("trace_id", "")).strip(),
        "status": str(latest_report.get("status", "")).strip(),
        "symbol_source": str(latest_report.get("symbol_source", "")).strip(),
        "target_trade_date": str(latest_report.get("target_trade_date", "")).strip()
        or str(background.get("latest_trade_date", "")).strip(),
        "symbols_total": _as_int(latest_report.get("symbols_total"), default=0),
        "symbols_completed": _as_int(latest_report.get("symbols_completed"), default=0),
        "failed_symbols_total": _as_int(
            latest_report.get("failed_symbols_total"),
            default=0,
        ),
        "latest_trade_date_coverage_ratio": _as_float(
            background.get("latest_trade_date_coverage_ratio"),
            default=0.0,
        ),
    }


def _load_training_overview_cache(
    service: Any,
    *,
    history_limit: int,
) -> dict[str, object] | None:
    raw_cache = getattr(service, "_dashboard_training_overview_cache", None)
    if not isinstance(raw_cache, dict):
        return None
    if _as_int(raw_cache.get("history_limit"), default=0) != history_limit:
        return None
    cached_at = _as_float(raw_cache.get("cached_at_monotonic"), default=0.0)
    if cached_at <= 0.0:
        return None
    if monotonic() - cached_at > _TRAINING_OVERVIEW_CACHE_TTL_SEC:
        return None
    payload = raw_cache.get("payload")
    if not isinstance(payload, dict):
        return None
    return cast(dict[str, object], deepcopy(payload))


def _store_training_overview_cache(
    service: Any,
    *,
    history_limit: int,
    payload: dict[str, object],
) -> None:
    setattr(
        service,
        "_dashboard_training_overview_cache",
        {
            "history_limit": history_limit,
            "cached_at_monotonic": monotonic(),
            "payload": deepcopy(payload),
        },
    )


def _summarize_training_regime(payload: dict[str, object]) -> dict[str, object]:
    metrics = _as_dict(payload.get("metrics"))
    return {
        "regime": str(payload.get("regime", "")),
        "uses_distinct_calibration_and_test": bool(
            payload.get("uses_distinct_calibration_and_test", False)
        ),
        "train_samples": _as_int(payload.get("train_samples"), default=0),
        "calibration_samples": _as_int(payload.get("calibration_samples"), default=0),
        "test_samples": _as_int(payload.get("test_samples"), default=0),
        "embargo_days": _as_int(payload.get("embargo_days"), default=0),
        "warning": str(payload.get("warning", "")),
        "metrics": _pick_mapping_fields(
            metrics,
            (
                "accuracy",
                "auc",
                "brier",
                "precision_at_k",
                "recall_at_k",
                "mean_prob_spread",
                "validation_samples",
                "meta_mean_prob",
                "lgbm_mean_prob",
                "xgb_mean_prob",
            ),
        ),
    }


def _summarize_acceptance_report(report: object) -> dict[str, object]:
    payload = _as_dict(report)
    if not payload:
        return {}
    checks: list[dict[str, object]] = []
    raw_checks = payload.get("checks")
    if isinstance(raw_checks, list):
        for raw_item in raw_checks:
            item = _as_dict(raw_item)
            if not item:
                continue
            checks.append(
                {
                    "name": str(item.get("name", "")),
                    "status": str(item.get("status", "")),
                    "detail": str(item.get("detail", "")),
                    "scope": str(item.get("scope", "")),
                }
            )
    return {
        "timestamp": str(payload.get("timestamp", "")),
        "age_hours": _age_hours(payload.get("timestamp")),
        "overall": str(payload.get("overall", "")),
        "summary": _as_dict(payload.get("summary")),
        "acceptance_summary": _as_dict(payload.get("acceptance_summary")),
        "stress_summary": _as_dict(payload.get("stress_summary")),
        "sla": _as_dict(payload.get("sla")),
        "runtime_sla": _as_dict(payload.get("runtime_sla")),
        "artifact": _as_dict(payload.get("artifact")),
        "checks": checks,
    }


def _summarize_evolution_report(report: object) -> dict[str, object]:
    payload = _as_dict(report)
    if not payload:
        return {}

    modules = _as_dict(payload.get("modules"))
    m2 = _as_dict(modules.get("m2"))
    m5 = _as_dict(modules.get("m5"))
    m10 = _as_dict(modules.get("m10"))
    m11 = _as_dict(modules.get("m11"))
    shadow_online = _as_dict(modules.get("shadow_online_model"))
    shadow_online_v2 = _as_dict(modules.get("shadow_online_model_v2"))
    runtime_controls = _as_dict(payload.get("runtime_controls"))
    market_sync = _as_dict(payload.get("market_warehouse_sync"))
    daily_sync = _as_dict(market_sync.get("daily_sync"))
    intraday_sync = _as_dict(market_sync.get("intraday_sync"))
    universe = _as_dict(payload.get("universe_snapshot"))
    loader_inputs_raw = _as_dict(payload.get("loader_inputs"))
    loader_inputs: list[dict[str, object]] = []
    for name, raw_value in loader_inputs_raw.items():
        item = _as_dict(raw_value)
        if not item:
            continue
        loader_inputs.append(
            {
                "module": name,
                "records": _as_int(item.get("records"), default=0),
                "fresh": bool(item.get("fresh", False)),
                "generated": bool(item.get("generated", False)),
                "path": str(item.get("path", "")),
            }
        )

    return {
        "run_id": str(payload.get("run_id", "")),
        "timestamp": str(payload.get("timestamp", "")),
        "age_hours": _age_hours(payload.get("timestamp")),
        "dry_run": bool(payload.get("dry_run", False)),
        "dependencies_ok": bool(_as_dict(payload.get("dependencies")).get("all_available", False)),
        "m9": {
            "success": bool(_as_dict(payload.get("m9")).get("success", False)),
            "retry_pending": bool(_as_dict(payload.get("m9")).get("retry_pending", False)),
            "degraded": bool(_as_dict(payload.get("m9")).get("degraded", False)),
            "blackout_day": bool(_as_dict(payload.get("m9")).get("blackout_day", False)),
            "frozen_symbols": _list_length(_as_dict(payload.get("m9")).get("frozen_symbols")),
        },
        "runtime_controls": {
            "degraded_mode": bool(runtime_controls.get("degraded_mode", False)),
            "conservative_mode": bool(runtime_controls.get("conservative_mode", False)),
            "threshold_shift": _as_float(runtime_controls.get("threshold_shift"), default=0.0),
            "position_multiplier": _as_float(
                runtime_controls.get("position_multiplier"),
                default=0.0,
            ),
            "global_risk_delta": _as_float(
                runtime_controls.get("global_risk_delta"),
                default=0.0,
            ),
            "regime_hint": str(runtime_controls.get("regime_hint", "")),
            "reasons": runtime_controls.get("reasons", []),
        },
        "universe": {
            "snapshot_id": str(universe.get("universe_snapshot_id", "")),
            "count": _as_int(universe.get("count"), default=0),
            "ruleset": str(universe.get("universe_ruleset_id", "")),
        },
        "market_sync": {
            "status": str(market_sync.get("status", "")),
            "target_trade_date": str(market_sync.get("target_trade_date", "")),
            "symbols_total": _as_int(market_sync.get("symbols_total"), default=0),
            "symbols_completed": _as_int(market_sync.get("symbols_completed"), default=0),
            "daily_ok": _as_int(daily_sync.get("ok"), default=0),
            "daily_failed": _as_int(daily_sync.get("failed"), default=0),
            "intraday_enabled": bool(intraday_sync.get("enabled", False)),
            "intraday_targeted": _as_int(intraday_sync.get("symbols_targeted"), default=0),
            "intraday_ok": _as_int(intraday_sync.get("ok"), default=0),
            "intraday_failed": _as_int(intraday_sync.get("failed"), default=0),
        },
        "modules": {
            "m2": {
                "status": str(m2.get("status", "")),
                "score": _as_float(m2.get("score"), default=0.0),
                "active_state": str(m2.get("active_state", "")),
                "confidence": _as_float(m2.get("confidence"), default=0.0),
                "optuna_improvement": _as_float(
                    _as_dict(m2.get("optuna")).get("improvement"),
                    default=0.0,
                ),
            },
            "m5": {
                "status": str(m5.get("status", "")),
                "score": _as_float(m5.get("score"), default=0.0),
                "label_coverage_ratio": _as_float(
                    _as_dict(m5.get("metrics")).get("label_coverage_ratio"),
                    default=0.0,
                ),
                "positive_label_ratio": _as_float(
                    _as_dict(m5.get("metrics")).get("positive_label_ratio"),
                    default=0.0,
                ),
                "alignment": _as_float(
                    _as_dict(m5.get("metrics")).get("return_alignment"),
                    default=0.0,
                ),
            },
            "m10": {
                "status": str(m10.get("status", "")),
                "score": _as_float(m10.get("score"), default=0.0),
                "execution_sensitivity_alert": bool(
                    m10.get("execution_sensitivity_alert", False)
                ),
            },
            "m11": {
                "status": str(m11.get("status", "")),
                "score": _as_float(m11.get("score"), default=0.0),
                "valid_samples": _as_int(
                    _as_dict(m11.get("metrics")).get("valid_samples"),
                    default=0,
                ),
                "drawdown_delta": _as_float(
                    _as_dict(m11.get("metrics")).get("drawdown_delta"),
                    default=0.0,
                ),
                "tail_loss_delta": _as_float(
                    _as_dict(m11.get("metrics")).get("tail_loss_delta"),
                    default=0.0,
                ),
            },
            "shadow_online_model": {
                "status": str(shadow_online.get("status", "")),
                "samples_used": _as_int(shadow_online.get("samples_used"), default=0),
                "delta_logloss": _as_float(
                    _as_dict(shadow_online.get("metrics")).get("delta_logloss"),
                    default=0.0,
                ),
            },
            "shadow_online_model_v2": {
                "status": str(shadow_online_v2.get("status", "")),
                "samples_used": _as_int(shadow_online_v2.get("samples_used"), default=0),
                "delta_logloss": _as_float(
                    _as_dict(_as_dict(shadow_online_v2.get("run_result")).get("metrics")).get(
                        "delta_logloss"
                    ),
                    default=0.0,
                ),
                "signal_divergence_ratio": _as_float(
                    _as_dict(shadow_online_v2.get("execution_summary")).get(
                        "shadow_v2_signal_divergence_ratio"
                    ),
                    default=0.0,
                ),
            },
        },
        "loader_inputs": loader_inputs,
        "compliance": _as_dict(payload.get("compliance")),
    }


def _summarize_evolution_history_item(report: dict[str, object]) -> dict[str, object]:
    runtime_controls = _as_dict(report.get("runtime_controls"))
    modules = _as_dict(report.get("modules"))
    m2 = _as_dict(modules.get("m2"))
    m10 = _as_dict(modules.get("m10"))
    m11 = _as_dict(modules.get("m11"))
    return {
        "run_id": str(report.get("run_id", "")),
        "timestamp": str(report.get("timestamp", "")),
        "age_hours": _age_hours(report.get("timestamp")),
        "dry_run": bool(report.get("dry_run", False)),
        "m9_success": bool(_as_dict(report.get("m9")).get("success", False)),
        "degraded_mode": bool(runtime_controls.get("degraded_mode", False)),
        "regime_hint": str(runtime_controls.get("regime_hint", "")),
        "m2_state": str(m2.get("active_state", "")),
        "m10_status": str(m10.get("status", "")),
        "m11_score": _as_float(m11.get("score"), default=0.0),
        "runtime_reasons": runtime_controls.get("reasons", []),
    }
