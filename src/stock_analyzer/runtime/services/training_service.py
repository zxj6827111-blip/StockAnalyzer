"""Training bootstrap and diagnostics workflows extracted from the runtime service."""

from __future__ import annotations

import json
import shutil
from copy import deepcopy
from datetime import datetime, timedelta
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from stock_analyzer.evolution.execution_aware_report import ExecutionAwareReportBuilder
from stock_analyzer.models.execution_risk_artifact import ExecutionRiskArtifact
from stock_analyzer.models.execution_risk_trainer import (
    ExecutionRiskTrainer,
    ExecutionRiskTrainingConfig,
)
from stock_analyzer.training_diagnostics import (
    build_training_evaluation_report,
    persist_diagnostic_report,
)

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService


class RuntimeTrainingService:
    """Delegated training bootstrap state, retry, and diagnostics workflows."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service

    def _default_training_bootstrap_state(self) -> dict[str, object]:
        now_text = datetime.now().isoformat()
        return {
            "state_version": 1,
            "first_seen_at": now_text,
            "completed": False,
            "bootstrap_runs": 0,
            "last_bootstrap_at": "",
            "last_status": "never_run",
            "last_symbols": 0,
            "last_error": "",
            "artifact_path": "",
            "retry_runs": 0,
            "last_retry_at": "",
        }

    def _load_training_bootstrap_state(self) -> dict[str, object]:
        service = self._service
        path = service._training_bootstrap_state_path
        if not path.exists():
            payload = self._default_training_bootstrap_state()
            self._persist_training_bootstrap_state(payload)
            return payload
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = self._default_training_bootstrap_state()
            payload["last_status"] = "state_corrupted_reset"
            self._persist_training_bootstrap_state(payload)
            return payload
        if not isinstance(raw, dict):
            payload = self._default_training_bootstrap_state()
            payload["last_status"] = "state_invalid_reset"
            self._persist_training_bootstrap_state(payload)
            return payload
        payload = self._default_training_bootstrap_state()
        payload.update({str(key): value for key, value in raw.items()})
        return payload

    def _persist_training_bootstrap_state(self, payload: dict[str, object]) -> None:
        service = self._service
        path = service._training_bootstrap_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _reconcile_training_bootstrap_state_with_artifact(self) -> None:
        service = self._service
        payload = dict(service._training_bootstrap_state)
        configured_artifact_text = str(service._config.training.artifact_path).strip()
        configured_artifact_path = service._resolve_evolution_path(configured_artifact_text)
        candidate_paths: list[Path] = [configured_artifact_path]

        payload_artifact_text = str(payload.get("artifact_path", "")).strip()
        if payload_artifact_text:
            payload_artifact_path = service._resolve_evolution_path(payload_artifact_text)
            if payload_artifact_path == configured_artifact_path:
                candidate_paths.append(payload_artifact_path)

        loaded_artifact: Path | None = None
        for candidate in candidate_paths:
            if not candidate.exists():
                continue
            if not service._pipeline.reload_predictor(artifact_path=str(candidate)):
                self._repair_training_artifact_sidecars(candidate)
            if service._pipeline.reload_predictor(artifact_path=str(candidate)):
                loaded_artifact = candidate
                break

        changed = False
        if loaded_artifact is not None:
            artifact_path = service._to_evolution_relative(loaded_artifact)
            if str(payload.get("artifact_path", "")).strip() != artifact_path:
                payload["artifact_path"] = artifact_path
                changed = True
            if not bool(payload.get("completed", False)):
                payload["completed"] = True
                payload["last_status"] = "artifact_recovered"
                payload["last_error"] = ""
                changed = True
        elif bool(payload.get("completed", False)):
            payload["completed"] = False
            payload["last_status"] = "artifact_missing_recheck_required"
            payload["last_error"] = "trained model artifact is missing or unreadable"
            changed = True

        if changed:
            service._training_bootstrap_state = payload
            self._persist_training_bootstrap_state(payload)

    def training_bootstrap_status(self) -> dict[str, object]:
        service = self._service
        payload = dict(service._training_bootstrap_state)
        completed = bool(payload.get("completed", False))
        payload["bootstrap_required"] = not completed
        payload["runtime_blocked"] = self._bootstrap_runtime_blocked()
        payload["require_completion_for_runtime"] = bool(
            service._config.training.bootstrap_require_completion_for_runtime
        )
        payload["auto_run_on_first_start"] = bool(
            service._config.training.bootstrap_auto_run_on_first_start
        )
        payload["retry_enabled"] = bool(service._config.training.bootstrap_retry_enabled)
        payload["retry_interval_min"] = max(
            1,
            _as_int(service._config.training.bootstrap_retry_interval_min, default=15),
        )
        payload["state_path"] = str(service._training_bootstrap_state_path)
        return payload

    def _bootstrap_runtime_blocked(self) -> bool:
        service = self._service
        if not bool(service._config.training.bootstrap_require_completion_for_runtime):
            return False
        return not bool(service._training_bootstrap_state.get("completed", False))

    def _maybe_retry_bootstrap_when_blocked(self, now: datetime) -> dict[str, object] | None:
        service = self._service
        if not bool(service._config.training.bootstrap_retry_enabled):
            return None
        if not self._bootstrap_runtime_blocked():
            return None
        interval_min = max(
            1, _as_int(service._config.training.bootstrap_retry_interval_min, default=15)
        )
        last_retry = _parse_iso_datetime(service._training_bootstrap_state.get("last_retry_at"))
        if last_retry is not None and now - last_retry < timedelta(minutes=interval_min):
            return None
        return self._run_bootstrap_retry(now=now, source="scheduler_gate")

    def _run_bootstrap_retry(self, now: datetime, source: str) -> dict[str, object]:
        service = self._service
        trace_id = f"bootstrap-retry-{int(now.timestamp())}"
        preferred_symbols = service._bootstrap_seed_symbols(
            cap=max(1, service._config.training.bootstrap_max_symbols or 1)
        )
        service._training_bootstrap_state["last_retry_at"] = now.isoformat()
        service._training_bootstrap_state["retry_runs"] = (
            _as_int(service._training_bootstrap_state.get("retry_runs"), default=0) + 1
        )
        self._persist_training_bootstrap_state(service._training_bootstrap_state)

        payload: dict[str, object]
        success = False
        detail = "retry_failed"
        try:
            report = service.train_models(
                full_market=True,
                lookback_days=max(120, service._config.training.bootstrap_lookback_days),
                max_symbols=(
                    service._config.training.bootstrap_max_symbols
                    if service._config.training.bootstrap_max_symbols > 0
                    else None
                ),
                preferred_symbols=preferred_symbols or None,
            )
            success = bool(report.get("ok", False))
            detail = "ok" if success else "retry_report_not_ok"
            if not success:
                service._training_bootstrap_state["last_status"] = str(report.get("status", detail))
                service._training_bootstrap_state["last_error"] = _bootstrap_error_text(
                    report=report
                )
                self._persist_training_bootstrap_state(service._training_bootstrap_state)
            if success:
                self._maybe_seed_watchlist_after_bootstrap()
            payload = {
                "source": source,
                "report": report,
                "bootstrap": self.training_bootstrap_status(),
            }
        except Exception as exc:
            service._training_bootstrap_state["completed"] = False
            service._training_bootstrap_state["bootstrap_runs"] = (
                _as_int(service._training_bootstrap_state.get("bootstrap_runs"), default=0) + 1
            )
            service._training_bootstrap_state["last_bootstrap_at"] = now.isoformat()
            service._training_bootstrap_state["last_status"] = "auto_bootstrap_retry_failed"
            service._training_bootstrap_state["last_symbols"] = 0
            service._training_bootstrap_state["last_error"] = str(exc)
            self._persist_training_bootstrap_state(service._training_bootstrap_state)
            payload = {
                "source": source,
                "error": str(exc),
                "bootstrap": self.training_bootstrap_status(),
            }

        level = "info" if success else "warn"
        service._record_audit_event(
            event_type="bootstrap_retry",
            trace_id=trace_id,
            level=level,
            payload=payload,
        )
        if bool(service._config.training.bootstrap_retry_notify):
            if success:
                service.notify(
                    title=_push_title(
                        priority="P1", category="bootstrap", summary="training bootstrap recovered"
                    ),
                    content="全市场训练引导重试成功，运行门禁已解除",
                    level="info",
                    trace_id=trace_id,
                )
            else:
                error_text = (
                    str(service._training_bootstrap_state.get("last_error", "")).strip() or detail
                )
                service.notify(
                    title=_push_title(
                        priority="P1",
                        category="bootstrap",
                        summary="training bootstrap retry failed",
                    ),
                    content=(
                        "训练引导完成前，运行仍被阻塞\n"
                        f"原因={_notification_error_text_zh(error_text)}"
                    ),
                    level="warn",
                    trace_id=trace_id,
                )
        return {
            "job": "bootstrap_retry",
            "ran": True,
            "success": success,
            "detail": detail,
            "payload": payload,
        }

    def _maybe_auto_bootstrap_training_on_first_start(self) -> None:
        service = self._service
        if not bool(service._config.training.bootstrap_auto_run_on_first_start):
            return
        if bool(service._training_bootstrap_state.get("completed", False)):
            return
        preferred_symbols = service._bootstrap_seed_symbols(
            cap=max(1, service._config.training.bootstrap_max_symbols or 1)
        )
        try:
            report = service.train_models(
                full_market=True,
                lookback_days=max(120, service._config.training.bootstrap_lookback_days),
                max_symbols=(
                    service._config.training.bootstrap_max_symbols
                    if service._config.training.bootstrap_max_symbols > 0
                    else None
                ),
                preferred_symbols=preferred_symbols or None,
            )
            ok = bool(report.get("ok", False))
            service._training_bootstrap_state["last_status"] = str(report.get("status", "ok"))
            service._training_bootstrap_state["last_error"] = (
                "" if ok else _bootstrap_error_text(report=report)
            )
            self._persist_training_bootstrap_state(service._training_bootstrap_state)
        except Exception as exc:
            service._training_bootstrap_state["bootstrap_runs"] = (
                _as_int(service._training_bootstrap_state.get("bootstrap_runs"), default=0) + 1
            )
            service._training_bootstrap_state["last_bootstrap_at"] = datetime.now().isoformat()
            service._training_bootstrap_state["last_status"] = "auto_bootstrap_failed"
            service._training_bootstrap_state["last_error"] = str(exc)
            self._persist_training_bootstrap_state(service._training_bootstrap_state)

    def _maybe_seed_watchlist_after_bootstrap(self) -> None:
        service = self._service
        if not bool(service._config.training.bootstrap_auto_seed_watchlist):
            return
        if service._state.watchlist:
            return
        if not bool(service._training_bootstrap_state.get("completed", False)):
            return
        seed_symbols = service._bootstrap_seed_symbols(
            cap=max(
                1,
                _as_int(service._config.training.bootstrap_seed_watchlist_size, default=50),
            )
        )
        if seed_symbols:
            service._replace_watchlist(seed_symbols, reason="bootstrap_seed")
            return
        try:
            service.run_week5_scan(
                timestamp=datetime.now(),
                notify_enabled=False,
                sync_watchlist=True,
                sync_reason="bootstrap_seed",
                sync_top_k_override=max(
                    1,
                    _as_int(service._config.training.bootstrap_seed_watchlist_size, default=50),
                ),
                force_universe_scan=True,
            )
        except Exception as exc:
            service._record_audit_event(
                event_type="bootstrap_watchlist_seed_failed",
                level="warn",
                message=str(exc),
            )

    def _repair_training_artifact_sidecars(self, artifact_path: Path) -> bool:
        service = self._service
        try:
            payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict):
            return False

        repaired = False
        for raw_path in _collect_sidecar_paths(payload):
            target = artifact_path.parent / raw_path
            if target.exists():
                continue
            source_candidates = [
                service._evolution_project_root / raw_path,
                service._evolution_project_root / "artifacts" / raw_path,
            ]
            source = next((path for path in source_candidates if path.exists()), None)
            if source is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            repaired = True
        return repaired

    def generate_training_evaluation_report(
        self,
        symbol: str,
        lookback_days: int = 800,
        output_path: str | None = None,
    ) -> dict[str, object]:
        service = self._service
        normalized_symbol = str(symbol).strip()
        if not normalized_symbol:
            raise ValueError("symbol is required")
        bars = service._provider.fetch_daily_bars(
            symbol=normalized_symbol, lookback_days=lookback_days
        )
        intraday_1m, intraday_5m = service._fetch_intraday_summaries(
            symbol=normalized_symbol,
            lookback_days=max(lookback_days, len(bars) + 5),
        )
        report = build_training_evaluation_report(
            bars=bars,
            training=service._config.training,
            labels=service._config.labels,
            models=service._config.models,
            settlement_lag_days=service._config.evolution.execution_spec.settlement_lag,
            intraday_1m=intraday_1m,
            intraday_5m=intraday_5m,
            provider=service._provider,
            market_relative_feature=service._config.market_relative_feature,
        )
        report["symbol"] = normalized_symbol
        report["lookback_days"] = lookback_days
        target = Path(output_path or "artifacts/acceptance/training_evaluation_report.json")
        report["output_path"] = persist_diagnostic_report(report=report, output_path=target)
        return cast(dict[str, object], report)

    def _resolve_execution_risk_artifact_path(self, artifact_path: str | None = None) -> Path:
        service = self._service
        normalized = str(artifact_path or "").strip()
        if normalized:
            candidate = Path(normalized).expanduser()
            if candidate.is_absolute():
                return candidate
            return service._resolve_evolution_path(str(candidate))
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        return (
            service._training_bootstrap_state_path.parent
            / "execution_risk"
            / f"execution_risk_{timestamp}.json"
        )

    def train_execution_risk_model(
        self,
        *,
        artifact_path: str | None = None,
        maturity_statuses: list[str] | None = None,
        max_rows: int | None = None,
        min_samples_per_target: int = 24,
        calibration_ratio: float = 0.2,
        test_ratio: float = 0.2,
        epochs: int = 240,
        learning_rate: float = 0.05,
        l2: float = 1e-3,
        seed: int = 42,
        now: datetime | None = None,
    ) -> dict[str, object]:
        service = self._service
        run_now = now or datetime.now()
        resolved_maturity_statuses = (
            list(maturity_statuses)
            if maturity_statuses
            else ["reconciled", "fully_matured"]
        )
        trainer = ExecutionRiskTrainer(
            config=ExecutionRiskTrainingConfig(
                min_samples_per_target=max(1, int(min_samples_per_target)),
                calibration_ratio=max(0.0, float(calibration_ratio)),
                test_ratio=max(0.0, float(test_ratio)),
                learning_rate=max(0.0, float(learning_rate)),
                epochs=max(1, int(epochs)),
                l2=max(0.0, float(l2)),
                seed=int(seed),
            )
        )
        result = trainer.train_from_sample_store(
            store=service._sample_store,
            maturity_statuses=resolved_maturity_statuses,
            max_rows=max_rows,
            now=run_now,
        )
        resolved_artifact_path = self._resolve_execution_risk_artifact_path(artifact_path)
        result.artifact.save(resolved_artifact_path)
        payload = {
            "ok": True,
            "mode": "execution_risk_training",
            "status": "trained",
            "timestamp": run_now.isoformat(),
            "artifact_path": str(resolved_artifact_path),
            "dataset_id": result.artifact.dataset_id,
            "trained_targets": list(result.trained_targets),
            "skipped_targets": dict(result.skipped_targets),
            "target_row_counts": dict(result.target_row_counts),
            "target_metrics": {key: dict(value) for key, value in result.target_metrics.items()},
            "training_summary": dict(result.artifact.training_summary),
            "metadata": {
                **dict(result.artifact.metadata),
                "dataset_id": result.artifact.dataset_id,
                "requested_maturity_statuses": list(resolved_maturity_statuses),
            },
        }
        self._append_history(
            history_attr="_execution_risk_training_history",
            latest_attr="_last_execution_risk_training",
            record=payload,
        )
        service._record_audit_event(
            event_type="execution_risk_model_trained",
            level="info",
            message="execution risk sidecar trained",
            payload={
                "dataset_id": result.artifact.dataset_id,
                "artifact_path": str(resolved_artifact_path),
                "trained_targets": list(result.trained_targets),
            },
        )
        service._persist_runtime_state_to_disk()
        return payload

    def latest_execution_risk_training(self) -> dict[str, object] | None:
        report = self._service._last_execution_risk_training
        return deepcopy(report) if isinstance(report, dict) else None

    def execution_risk_training_history(self, limit: int = 20) -> dict[str, object]:
        service = self._service
        capped = max(1, min(limit, max(1, service._config.evolution.history_limit)))
        recent = [deepcopy(item) for item in service._execution_risk_training_history[-capped:]]
        return {"records": len(recent), "items": recent}

    def execution_risk_status(self) -> dict[str, object]:
        service = self._service
        latest = self.latest_execution_risk_training()
        artifact_text = str((latest or {}).get("artifact_path", "")).strip()
        artifact_path = Path(artifact_text) if artifact_text else None
        artifact_exists = bool(artifact_path and artifact_path.exists())
        trained_targets: list[str] = []
        dataset_id = str((latest or {}).get("dataset_id", "")).strip()
        if artifact_exists and artifact_path is not None:
            try:
                artifact = ExecutionRiskArtifact.load(artifact_path)
                trained_targets = list(artifact.trained_targets)
                dataset_id = dataset_id or artifact.dataset_id
            except Exception:
                artifact_exists = False
        return {
            "latest": latest,
            "history_count": len(service._execution_risk_training_history),
            "artifact_exists": artifact_exists,
            "artifact_path": str(artifact_path) if artifact_path is not None else "",
            "trained_targets": trained_targets,
            "dataset_id": dataset_id,
        }

    def build_execution_aware_report(
        self,
        *,
        model_id: str,
        execution_risk_artifact_path: str = "",
        champion_model_id: str = "",
        split_names: list[str] | None = None,
        max_rows: int | None = None,
        include_rows: bool = True,
        preview_limit: int = 5,
    ) -> dict[str, object]:
        service = self._service
        latest_training = self.latest_execution_risk_training() or {}
        artifact_path = (
            str(execution_risk_artifact_path).strip()
            or str(latest_training.get("artifact_path", "")).strip()
        )
        if not artifact_path:
            raise ValueError("execution risk artifact is not available")
        report = ExecutionAwareReportBuilder(
            store=service._sample_store,
            model_registry=service._model_registry,
            feature_schema_registry=service._feature_schema_registry,
            label_policy_registry=service._label_policy_registry,
        ).build_report(
            shadow_model_id=str(model_id).strip(),
            champion_model_id=str(champion_model_id).strip(),
            split_names=split_names,
            max_rows=max_rows,
            execution_risk_artifact_path=artifact_path,
        )
        payload = report.to_dict(
            include_rows=include_rows,
            preview_limit=max(1, int(preview_limit)),
        )
        self._append_history(
            history_attr="_execution_aware_report_history",
            latest_attr="_last_execution_aware_report",
            record=payload,
        )
        service._record_audit_event(
            event_type="execution_aware_report_built",
            level="info",
            message="execution aware report built",
            payload={
                "report_id": payload["report_id"],
                "shadow_model_id": payload["shadow_model_id"],
                "champion_model_id": payload["champion_model_id"],
                "dataset_manifest_id": payload["dataset_manifest_id"],
                "execution_risk_artifact_path": payload["execution_risk_artifact_path"],
            },
        )
        service._persist_runtime_state_to_disk()
        return payload

    def latest_execution_aware_report(self) -> dict[str, object] | None:
        report = self._service._last_execution_aware_report
        return deepcopy(report) if isinstance(report, dict) else None

    def execution_aware_report_history(self, limit: int = 20) -> dict[str, object]:
        service = self._service
        capped = max(1, min(limit, max(1, service._config.evolution.history_limit)))
        recent = [deepcopy(item) for item in service._execution_aware_report_history[-capped:]]
        return {"records": len(recent), "items": recent}

    def _append_history(
        self,
        *,
        history_attr: str,
        latest_attr: str,
        record: dict[str, object],
    ) -> None:
        service = self._service
        snapshot = deepcopy(record)
        setattr(service, latest_attr, snapshot)
        history = cast(list[dict[str, object]], getattr(service, history_attr))
        history.append(snapshot)
        limit = max(1, service._config.evolution.history_limit)
        if len(history) > limit:
            overflow = len(history) - limit
            if overflow > 0:
                setattr(service, history_attr, history[overflow:])


@lru_cache(maxsize=1)
def _runtime_service_module() -> Any:
    return import_module("stock_analyzer.runtime.service")


def _as_int(value: object, default: int) -> int:
    return cast(int, _runtime_service_module()._as_int(value, default))


def _bootstrap_error_text(report: dict[str, object]) -> str:
    return cast(str, _runtime_service_module()._bootstrap_error_text(report=report))


def _notification_error_text_zh(error_text: str) -> str:
    return cast(str, _runtime_service_module()._notification_error_text_zh(error_text))


def _parse_iso_datetime(value: object) -> datetime | None:
    return cast(datetime | None, _runtime_service_module()._parse_iso_datetime(value))


def _push_title(priority: str, category: str, summary: str) -> str:
    return cast(str, _runtime_service_module()._push_title(priority, category, summary))


def _collect_sidecar_paths(payload: object) -> list[str]:
    paths: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in {"sidecar_path", "fallback_sidecar_path"} and isinstance(value, str):
                cleaned = value.strip().replace("\\", "/")
                if cleaned and not Path(cleaned).is_absolute():
                    paths.append(cleaned)
                continue
            paths.extend(_collect_sidecar_paths(value))
    elif isinstance(payload, list):
        for item in payload:
            paths.extend(_collect_sidecar_paths(item))
    return paths
