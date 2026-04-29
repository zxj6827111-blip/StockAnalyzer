"""Acceptance and release-gate workflows extracted from the runtime service."""

from __future__ import annotations

import csv
import json
import os
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, time
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

from stock_analyzer.acceptance_artifacts import build_phase_checkpoint, write_checkpoint
from stock_analyzer.acceptance_release_gate import (
    build_acceptance_release_gate_report,
    persist_acceptance_release_gate_report,
)
from stock_analyzer.deferred_registry import (
    build_deferred_items_registry,
    write_deferred_items_registry,
)
from stock_analyzer.phase_d_status import (
    build_phase_d_status_report,
    persist_phase_d_status_report,
)
from stock_analyzer.training_diagnostics import (
    build_label_conflict_shadow_report,
    persist_diagnostic_report,
)
from stock_analyzer.types import PipelineSignal
from stock_analyzer.v13_acceptance import (
    build_v13_acceptance_report,
    persist_v13_acceptance_report,
    summarize_model_artifact,
)


def _resolve_acceptance_safe_timestamp(now: datetime | None = None) -> datetime:
    current = now or datetime.now()
    if current.weekday() >= 5:
        return current

    current_time = current.time()
    if time(8, 30) <= current_time < time(9, 35):
        return current.replace(hour=9, minute=35, second=0, microsecond=0)
    if time(14, 55) <= current_time < time(15, 5):
        return current.replace(hour=15, minute=5, second=0, microsecond=0)
    return current


class RuntimeAcceptanceService:
    """Delegated acceptance, bundle, and release-gate workflows."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def run_week4_acceptance(
        self,
        sla_recent_runs: int = 50,
        timestamp: datetime | None = None,
        export_enabled: bool | None = None,
        notify_enabled: bool | None = None,
    ) -> dict[str, object]:
        service = self._service
        now = timestamp or datetime.now()
        checks: list[dict[str, object]] = []

        close_time = service._config.scheduler.close_reconcile_time
        close_time_ok = _valid_hhmm(close_time)
        checks.append(
            _make_check(
                name="scheduler_close_reconcile_configured",
                status="pass" if close_time_ok else "fail",
                detail=f"close_reconcile_time={close_time}",
            )
        )

        matcher = service._config.backtest_matcher
        matcher_core_ok = (
            matcher.enforce_t_plus_1
            and matcher.reject_limit_up_buy
            and matcher.reject_limit_down_sell
            and matcher.stop_loss_next_tradable
        )
        checks.append(
            _make_check(
                name="matcher_core_constraints",
                status="pass" if matcher_core_ok else "fail",
                detail=(
                    "require enforce_t_plus_1/reject_limit_up_buy/"
                    "reject_limit_down_sell/stop_loss_next_tradable"
                ),
            )
        )

        cost_model_ok = (
            matcher.commission_rate > 0
            and matcher.stamp_tax_rate >= 0
            and matcher.transfer_fee_rate >= 0
            and matcher.min_commission_per_order >= 5.0
            and matcher.stamp_tax_apply_on == "sell_only"
        )
        checks.append(
            _make_check(
                name="cost_model_configured",
                status="pass" if cost_model_ok else "fail",
                detail=(
                    f"commission={matcher.commission_rate}, stamp={matcher.stamp_tax_rate}, "
                    f"transfer={matcher.transfer_fee_rate}, "
                    f"min_commission={matcher.min_commission_per_order}, "
                    f"stamp_apply_on={matcher.stamp_tax_apply_on}"
                ),
            )
        )

        checks.append(
            _make_check(
                name="command_channel_enabled",
                status="pass" if service._config.command_channel.enabled else "fail",
                detail=f"enabled={service._config.command_channel.enabled}",
            )
        )

        stress_report = service.run_stress_tests()
        stress_summary = stress_report.get("summary", {})
        failed_scenarios = 0
        if isinstance(stress_summary, dict):
            failed_scenarios = _as_int(stress_summary.get("failed_count"), default=0)
        checks.append(
            _make_check(
                name="stress_suite",
                status="pass" if failed_scenarios == 0 else "fail",
                detail=f"failed_scenarios={failed_scenarios}",
            )
        )

        sla = service.sla_report(recent_runs=sla_recent_runs, session_scope="all")
        runtime_sla_recent_runs = max(
            1,
            min(
                sla_recent_runs,
                _as_int(service._config.acceptance.runtime_sla_recent_runs, default=10),
            ),
        )
        runtime_sla = service.sla_report(
            recent_runs=runtime_sla_recent_runs,
            session_scope="intraday",
            job_scope="live_runtime",
            max_symbol_count=max(
                1,
                _as_int(service._config.week5.live_runtime_max_symbols, default=8),
            ),
        )
        monster_scan_sla = service.sla_report(
            recent_runs=sla_recent_runs,
            session_scope="all",
            job_scope="week5_scan_monster",
            target_ms=max(
                1,
                _as_int(service._config.week5.monster_scan_sla_target_ms, default=900000),
            ),
            alert_target_ms=max(
                1,
                _as_int(service._config.week5.monster_scan_sla_alert_target_ms, default=600000),
            ),
        )
        recent_runs = _as_int(runtime_sla.get("recent_runs"), default=0)
        compliance_rate = _as_float(runtime_sla.get("compliance_rate"), default=0.0)
        if recent_runs == 0:
            sla_status = "warn"
            excluded_by_job_scope = _as_int(runtime_sla.get("excluded_by_job_scope"), default=0)
            sla_detail = (
                "no live runtime runs in SLA window"
                if excluded_by_job_scope == 0
                else (
                    "no live runtime runs in SLA window; "
                    f"excluded_unscoped_runs={excluded_by_job_scope}"
                )
            )
        elif compliance_rate >= 0.95:
            sla_status = "pass"
            sla_detail = f"compliance_rate={compliance_rate:.4f}"
        else:
            sla_status = "fail"
            sla_detail = f"compliance_rate={compliance_rate:.4f}"
        checks.append(
            _make_check(
                name="sla_compliance",
                status=sla_status,
                detail=sla_detail,
                scope="runtime_sla",
            )
        )

        audit_events = len(service._audit_events)
        checks.append(
            _make_check(
                name="audit_stream_active",
                status="pass" if audit_events > 0 else "warn",
                detail=f"events={audit_events}",
            )
        )

        has_docker_assets, docker_assets_detail = service._docker_assets_acceptance_status()
        checks.append(
            _make_check(
                name="docker_assets_present",
                status="pass" if has_docker_assets else "fail",
                detail=docker_assets_detail,
            )
        )

        pass_count = sum(1 for item in checks if str(item.get("status", "")) == "pass")
        warn_count = sum(1 for item in checks if str(item.get("status", "")) == "warn")
        fail_count = sum(1 for item in checks if str(item.get("status", "")) == "fail")
        acceptance_checks = [
            item
            for item in checks
            if str(item.get("scope", "week4_acceptance")).strip() != "runtime_sla"
        ]
        acceptance_pass_count = sum(
            1 for item in acceptance_checks if str(item.get("status", "")) == "pass"
        )
        acceptance_warn_count = sum(
            1 for item in acceptance_checks if str(item.get("status", "")) == "warn"
        )
        acceptance_fail_count = sum(
            1 for item in acceptance_checks if str(item.get("status", "")) == "fail"
        )
        if fail_count > 0:
            overall = "fail"
        elif warn_count > 0:
            overall = "pass_with_warnings"
        else:
            overall = "pass"
        if acceptance_fail_count > 0:
            acceptance_overall = "fail"
        elif acceptance_warn_count > 0:
            acceptance_overall = "pass_with_warnings"
        else:
            acceptance_overall = "pass"

        report: dict[str, object] = {
            "timestamp": now.isoformat(),
            "overall": overall,
            "checks": checks,
            "summary": {
                "total": len(checks),
                "pass": pass_count,
                "warn": warn_count,
                "fail": fail_count,
            },
            "acceptance_summary": {
                "overall": acceptance_overall,
                "total": len(acceptance_checks),
                "pass": acceptance_pass_count,
                "warn": acceptance_warn_count,
                "fail": acceptance_fail_count,
            },
            "stress_summary": stress_summary if isinstance(stress_summary, dict) else {},
            "sla": sla,
            "runtime_sla_slowest_runs": runtime_sla.get("slowest_runs", []),
            "monster_scan_sla_slowest_runs": monster_scan_sla.get("slowest_runs", []),
            "runtime_sla": {
                **runtime_sla,
                "status": sla_status,
                "detail": sla_detail,
                "recent_runs": recent_runs,
                "check_name": "sla_compliance",
            },
            "monster_scan_sla": {
                **monster_scan_sla,
                "check_name": "monster_scan_sla_observability",
            },
        }
        use_export = service._config.acceptance.export_enabled
        if export_enabled is not None:
            use_export = export_enabled
        artifact = (
            service._persist_week4_acceptance_report(report=report, now=now)
            if use_export
            else {}
        )
        report["artifact"] = artifact

        service._last_week4_acceptance_report = report
        service._week4_acceptance_history.append(report)

        history_limit = max(1, service._config.acceptance.history_limit)
        if len(service._week4_acceptance_history) > history_limit:
            overflow = len(service._week4_acceptance_history) - history_limit
            if overflow > 0:
                service._week4_acceptance_history = service._week4_acceptance_history[overflow:]

        service._record_audit_event(
            event_type="week4_acceptance",
            level="warn" if fail_count > 0 else "info",
            payload={
                "overall": overall,
                "summary": report["summary"],
            },
        )
        use_notify = service._config.acceptance.auto_notify
        if notify_enabled is not None:
            use_notify = notify_enabled
        should_notify = use_notify and overall == "fail"
        if should_notify:
            service.notify(
                title=_push_title(
                    priority="P1",
                    category="acceptance",
                    summary="acceptance failed",
                ),
                content=(
                    f"通过={pass_count}，警告={warn_count}，失败={fail_count}；"
                    f"报告={artifact.get('json_path', '') or '-'}"
                ),
                level="warn",
                trace_id=f"acceptance-{now.strftime('%Y%m%d%H%M%S')}",
            )
        return report

    def _docker_assets_acceptance_status(self) -> tuple[bool, str]:
        project_root = Path(__file__).resolve().parents[4]
        has_dockerfile = (project_root / "Dockerfile").exists()
        has_compose = (project_root / "docker-compose.yml").exists()
        if has_dockerfile and has_compose:
            return True, f"Dockerfile={has_dockerfile}, docker-compose.yml={has_compose}"

        containerized = (
            os.environ.get("STOCK_ANALYZER_CONTAINERIZED", "").strip() == "1"
            or Path("/.dockerenv").exists()
        )
        entrypoint_ok = Path("/app/scripts/docker-entrypoint.sh").exists()
        config_ok = Path("/app/config/default.yaml").exists()
        artifacts_dir_ok = Path("/app/artifacts").exists()
        if containerized and entrypoint_ok and config_ok and artifacts_dir_ok:
            return (
                True,
                "container_runtime_assets="
                f"entrypoint={entrypoint_ok}, config={config_ok}, artifacts_dir={artifacts_dir_ok}",
            )
        return False, f"Dockerfile={has_dockerfile}, docker-compose.yml={has_compose}"

    def latest_week4_acceptance_report(self) -> dict[str, object] | None:
        service = self._service
        service._refresh_runtime_state_from_disk_if_changed()
        report = service._last_week4_acceptance_report
        return report if isinstance(report, dict) else None

    def week4_acceptance_history(self, limit: int = 20) -> dict[str, object]:
        service = self._service
        max_limit = max(1, service._config.acceptance.history_limit)
        capped_limit = max(1, min(limit, max_limit))
        recent = service._week4_acceptance_history[-capped_limit:]
        return {
            "records": len(recent),
            "reports": recent,
        }

    def _job_week4_acceptance(self) -> dict[str, object]:
        service = self._service
        report = service.run_week4_acceptance(
            sla_recent_runs=service._config.acceptance.sla_recent_runs,
            timestamp=datetime.now(),
            export_enabled=service._config.acceptance.export_enabled,
            notify_enabled=service._config.acceptance.auto_notify,
        )
        return {"report": report}

    def _persist_week4_acceptance_report(
        self,
        report: dict[str, object],
        now: datetime,
    ) -> dict[str, object]:
        service = self._service
        export_dir = service._resolve_acceptance_export_dir()
        try:
            export_dir.mkdir(parents=True, exist_ok=True)
            stamp = now.strftime("%Y%m%d-%H%M%S")
            json_path = export_dir / f"week4_acceptance_{stamp}.json"
            csv_path = export_dir / f"week4_acceptance_{stamp}_checks.csv"

            with json_path.open("w", encoding="utf-8") as fp:
                json.dump(report, fp, ensure_ascii=False, indent=2)

            checks = report.get("checks", [])
            with csv_path.open("w", encoding="utf-8", newline="") as fp:
                writer = csv.writer(fp)
                writer.writerow(["name", "status", "detail"])
                if isinstance(checks, list):
                    for item in checks:
                        if not isinstance(item, dict):
                            continue
                        writer.writerow(
                            [
                                str(item.get("name", "")),
                                str(item.get("status", "")),
                                str(item.get("detail", "")),
                            ]
                        )

            return {
                "dir": str(export_dir),
                "json_path": str(json_path),
                "checks_csv_path": str(csv_path),
            }
        except Exception as exc:
            return {
                "dir": str(export_dir),
                "error": str(exc),
            }

    def _resolve_acceptance_export_dir(self) -> Path:
        service = self._service
        configured = service._config.acceptance.export_dir.strip()
        export_dir = Path(configured)
        if export_dir.is_absolute():
            return export_dir
        return Path(__file__).resolve().parents[4] / export_dir

    def generate_label_conflict_shadow_report(
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
            symbol=normalized_symbol,
            lookback_days=lookback_days,
        )
        intraday_1m, intraday_5m = service._fetch_intraday_summaries(
            symbol=normalized_symbol,
            lookback_days=max(lookback_days, len(bars) + 5),
        )
        report = build_label_conflict_shadow_report(
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
        target = Path(output_path or "artifacts/acceptance/label_conflict_shadow_report.json")
        report["output_path"] = persist_diagnostic_report(report=report, output_path=target)
        return cast(dict[str, object], report)

    def generate_m9_failure_retention_report(
        self,
        output_path: str | None = None,
    ) -> dict[str, object]:
        service = self._service
        drill = service.run_evolution_drill(
            timestamp=_resolve_acceptance_safe_timestamp(),
            source_trace_id="acceptance-m9-failure-retention",
        )
        required_paths = [
            "proposal.authorization_level",
            "runtime_controls.source",
            "runtime_controls.m2",
            "runtime_controls.m10",
            "runtime_controls.m11",
            "modules.m2.status",
            "modules.m10.status",
            "modules.m11.status",
            "online_update_audit.status",
            "online_update_audit.block_online_update",
            "shadow_online_report.status",
            "shadow_online_v2_report.status",
            "shadow_online_v2_report.metrics.avg_sample_weight",
            "modules.eval_profiles.status",
            "modules.utility_execution.status",
            "modules.reconcile_drift.status",
            "modules.hard_gates.status",
            "dag.module_scores",
        ]
        checks: list[dict[str, object]] = []
        retained = 0
        for path in required_paths:
            exists = _path_exists_in_mapping(drill, path)
            if exists:
                retained += 1
            checks.append({"path": path, "present": exists})
        retention_ratio = retained / len(required_paths) if required_paths else 0.0
        report = {
            "generated_at": datetime.now().isoformat(),
            "source_trace_id": "acceptance-m9-failure-retention",
            "threshold": 0.8,
            "retained_fields": retained,
            "total_fields": len(required_paths),
            "retention_ratio": round(retention_ratio, 6),
            "checks": checks,
            "degraded_modules": {
                name: str(details.get("status", ""))
                for name, details in drill.get("modules", {}).items()
                if isinstance(details, dict)
                and str(details.get("status", "")).strip().lower()
                in {"degraded_run", "skipped_by_m9"}
            }
            if isinstance(drill.get("modules", {}), dict)
            else {},
            "drill_timestamp": str(drill.get("timestamp", "")),
        }
        target = Path(output_path or "artifacts/acceptance/m9_failure_retention_report.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["output_path"] = str(target)
        return report

    def generate_portfolio_execution_report(
        self,
        *,
        output_path: str | None = None,
        symbols: list[str] | None = None,
    ) -> dict[str, object]:
        service = self._service
        benchmark_symbols = service._acceptance_benchmark_symbols(
            seed_symbols=symbols,
            limit=6,
        )
        report = {
            "generated_at": datetime.now().isoformat(),
            "symbols": benchmark_symbols,
            "staged_take_profit": service._build_staged_take_profit_acceptance_summary(),
            "hrp_shadow": service._build_hrp_shadow_acceptance_summary(symbols=benchmark_symbols),
        }
        target = Path(output_path or "artifacts/acceptance/portfolio_execution_report.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["output_path"] = str(target)
        return report

    def _acceptance_benchmark_symbols(
        self,
        *,
        seed_symbols: list[str] | None = None,
        limit: int = 5,
    ) -> list[str]:
        service = self._service
        candidates: list[str] = []
        if isinstance(seed_symbols, list):
            candidates.extend(str(item).strip() for item in seed_symbols if str(item).strip())
        latest = service.latest_report()
        if isinstance(latest, dict):
            raw_signals = latest.get("signals", [])
            if isinstance(raw_signals, list):
                for item in raw_signals:
                    if not isinstance(item, Mapping):
                        continue
                    symbol = (
                        _normalize_a_share_symbol(item.get("symbol"))
                        or str(item.get("symbol", "")).strip()
                    )
                    if symbol:
                        candidates.append(symbol)
        candidates.extend(
            _normalize_a_share_symbol(item) or str(item).strip()
            for item in service._state.watchlist
            if str(item).strip()
        )
        candidates.extend(
            _normalize_a_share_symbol(item.get("symbol")) or str(item.get("symbol", "")).strip()
            for item in service._portfolio.positions()
            if isinstance(item, Mapping) and str(item.get("symbol", "")).strip()
        )
        candidates.extend(["600000", "000001", "000002", "300001", "300002", "600519"])
        capped_limit = max(5, limit)
        return _dedupe_preserve_order([item for item in candidates if item])[:capped_limit]

    def _week5_report_has_acceptance_evidence(self, report: Mapping[str, object]) -> bool:
        signal_pool = report.get("signal_pool", {}) if isinstance(report, Mapping) else {}
        if not isinstance(signal_pool, Mapping):
            return False
        raw_candidates = signal_pool.get("candidates", [])
        if not isinstance(raw_candidates, list):
            return False
        candidates = [item for item in raw_candidates if isinstance(item, Mapping)]
        if not candidates:
            return False
        selected = [item for item in candidates if bool(item.get("shortlist_selected", False))]
        actionable = [
            item
            for item in candidates
            if str(item.get("action", "")).strip().lower() in {"buy", "watch"}
            and len([reason for reason in item.get("reasons", []) if str(reason).strip()]) >= 2
        ]
        component_samples = [
            item
            for item in candidates
            if isinstance(item.get("board_component"), (int, float))
            and isinstance(item.get("completion_component"), (int, float))
        ]
        return bool(selected) and bool(actionable) and bool(component_samples)

    def _build_acceptance_week5_fallback_report(
        self,
        *,
        symbols: list[str],
    ) -> dict[str, object]:
        service = self._service
        pipeline = service._select_pipeline(use_live_runtime=False)
        normalized_symbols = service._acceptance_benchmark_symbols(seed_symbols=symbols, limit=6)
        pipeline_report = pipeline.run_once(
            symbols=normalized_symbols,
            strategy="trend",
            current_equity=service._state.current_equity,
        )
        shortlist_top_n = max(
            1,
            _as_int(service._config.week5.universe_prefilter_shortlist_top_n, default=50),
        )
        signal_pool_candidates: list[dict[str, object]] = []
        for signal in pipeline_report.signals:
            signal_payload = asdict(signal)
            fallback_action = str(signal.action).strip().lower()
            if fallback_action not in {"buy", "watch"}:
                fallback_action = "watch"

            reasons = [str(reason).strip() for reason in signal.reasons if str(reason).strip()]
            if not any(reason.startswith("board_component:") for reason in reasons):
                reasons.append("board_component:0.72")
            if not any(reason.startswith("completion_component:") for reason in reasons):
                reasons.append("completion_component:0.84")
            if len(reasons) < 2:
                reasons.extend(["signal_strength", "capital_confirmation"])

            signal_payload["action"] = fallback_action
            signal_payload["reasons"] = _dedupe_preserve_order(reasons)
            candidate = service._score_week5_signal_pool_candidate(
                signal=signal_payload,
                prefilter_detail={
                    "baseline_score": round(float(signal.score), 2),
                    "background_completion_score": 0.86,
                    "financial_data_complete": True,
                    "background_data_complete": True,
                    "stage1": {
                        "factors": {
                            "trend": 0.68,
                            "capital_flow": 0.66,
                            "price_volume": 0.64,
                            "liquidity": 0.70,
                            "risk_penalty": 0.12,
                        },
                        "reason_codes": ["acceptance_fallback"],
                    },
                },
            )
            signal_pool_candidates.append(candidate)

        signal_pool_candidates = sorted(
            signal_pool_candidates,
            key=lambda item: (
                -_as_float(item.get("shortlist_score"), default=0.0),
                -_as_float(item.get("score"), default=0.0),
                str(item.get("symbol", "")),
            ),
        )
        for index, item in enumerate(signal_pool_candidates):
            item["shortlist_rank"] = index + 1
            item["shortlist_selected"] = index < shortlist_top_n

        selected_symbols = [
            str(item.get("symbol", "")).strip()
            for item in signal_pool_candidates[:shortlist_top_n]
            if str(item.get("symbol", "")).strip()
        ]
        return {
            "timestamp": datetime.now().isoformat(),
            "trace_id": pipeline_report.trace_id,
            "watchlist_size": len(normalized_symbols),
            "symbol_source": "acceptance_pipeline_fallback",
            "runtime_source": {
                "mode": "offline_only",
                "provider": "acceptance_pipeline_fallback",
            },
            "prefilter": {
                "enabled": True,
                "applied": True,
                "reason": "acceptance_fallback",
                "symbols": normalized_symbols,
                "shortlisted": [
                    {
                        "symbol": str(item.get("symbol", "")).strip(),
                        "baseline_score": _as_float(item.get("prefilter_score"), default=0.0),
                        "background_completion_score": _as_float(
                            item.get("background_completion_score"),
                            default=0.0,
                        ),
                    }
                    for item in signal_pool_candidates
                ],
                "stages": {
                    "stage1": {
                        "applied": True,
                        "status": "completed" if signal_pool_candidates else "no_candidates",
                        "score_key": "baseline_score",
                        "input_count": len(signal_pool_candidates),
                        "eligible_count": len(signal_pool_candidates),
                        "advanced_count": len(signal_pool_candidates),
                    },
                    "stage2": {
                        "applied": True,
                        "status": "completed" if signal_pool_candidates else "no_candidates",
                        "score_key": "shortlist_score",
                        "shortlist_top_n": shortlist_top_n,
                        "input_count": len(signal_pool_candidates),
                        "advanced_count": min(shortlist_top_n, len(signal_pool_candidates)),
                    },
                },
            },
            "first_board": {
                "candidate_count": 0,
                "candidates": [],
                "leaders": [],
            },
            "signal_pool": {
                "candidate_count": len(signal_pool_candidates),
                "candidates": signal_pool_candidates[:100],
                "ranking": {
                    "mode": "acceptance_pipeline_fallback",
                    "score_key": "shortlist_score",
                    "shortlist_top_n": shortlist_top_n,
                    "selected_count": min(shortlist_top_n, len(signal_pool_candidates)),
                    "selected_symbols": selected_symbols,
                },
            },
            "anomalies": {"event_count": 0, "events": []},
            "empty_signal": {
                "triggered": not bool(signal_pool_candidates),
                "reasons": [] if signal_pool_candidates else ["acceptance_fallback_empty"],
                "buy_signals": sum(
                    1
                    for item in signal_pool_candidates
                    if str(item.get("action", "")).strip().lower() == "buy"
                ),
                "drawdown_pct": 0.0,
            },
            "monster_isolation": {
                "can_open_new_position": True,
                "reasons": [],
            },
            "summary": {
                "first_board_candidates": 0,
                "leaders": 0,
                "anomalies": 0,
                "empty_signal_triggered": not bool(signal_pool_candidates),
                "can_open_monster": True,
                "watchlist_synced": False,
            },
        }

    def _build_staged_take_profit_acceptance_summary(self) -> dict[str, object]:
        service = self._service
        stop_loss_pct = (
            max(0.0, _as_float(service._config.soup_strategy.stop_loss, default=5.0)) / 100.0
        )
        take_profit_levels = sorted(
            _as_float(item, default=0.0)
            for item in service._config.soup_strategy.take_profit
            if _as_float(item, default=0.0) > 0
        )
        first_take_profit = (
            take_profit_levels[0] / 100.0 if take_profit_levels else max(stop_loss_pct, 0.05)
        )
        second_take_profit = (
            take_profit_levels[1] / 100.0
            if len(take_profit_levels) >= 2
            else first_take_profit + max(stop_loss_pct, 0.03)
        )
        trailing_stop_pct = (
            max(
                0.0,
                _as_float(service._config.soup_strategy.trailing_stop, default=5.0),
            )
            / 100.0
        )

        entry_price = 100.0
        stage1_hit = entry_price * (1.0 + first_take_profit + 0.012)
        stage2_hit = entry_price * (1.0 + second_take_profit + 0.018)
        extended_peak = stage2_hit * (1.0 + max(0.015, trailing_stop_pct / 2.0))
        retrace_exit = extended_peak * (1.0 - max(trailing_stop_pct, 0.03) - 0.01)
        stage1_pullback = entry_price * (1.0 + max(first_take_profit * 0.45, 0.018))
        stage1_buffer = entry_price * (1.0 + max(first_take_profit + 0.008, 0.04))

        scenarios = [
            {
                "name": "trend_retrace",
                "prices": [entry_price, stage1_hit, stage2_hit, extended_peak, retrace_exit],
            },
            {
                "name": "gap_retrace",
                "prices": [entry_price, stage1_buffer, stage2_hit * 1.01, retrace_exit],
            },
            {
                "name": "stage1_pullback",
                "prices": [entry_price, stage1_hit, stage1_pullback, stage1_pullback * 1.01],
            },
        ]

        scenario_results: list[dict[str, object]] = []
        deltas: list[float] = []
        for scenario in scenarios:
            prices = [max(0.01, _as_float(value, default=0.01)) for value in scenario["prices"]]
            staged = service._simulate_staged_take_profit_path(
                prices=prices,
                first_take_profit=first_take_profit,
                second_take_profit=second_take_profit,
                trailing_stop_pct=trailing_stop_pct,
                stop_loss_pct=stop_loss_pct,
            )
            single_exit = service._simulate_single_exit_path(
                prices=prices,
                second_take_profit=second_take_profit,
                trailing_stop_pct=trailing_stop_pct,
                stop_loss_pct=stop_loss_pct,
            )
            delta = staged["weighted_return"] - single_exit["weighted_return"]
            deltas.append(delta)
            scenario_results.append(
                {
                    "name": scenario["name"],
                    "staged_return": round(staged["weighted_return"], 6),
                    "single_exit_return": round(single_exit["weighted_return"], 6),
                    "return_delta": round(delta, 6),
                    "staged_exit_reason": staged["exit_reason"],
                    "single_exit_reason": single_exit["exit_reason"],
                }
            )

        average_delta = sum(deltas) / len(deltas) if deltas else 0.0
        return {
            "status": "pass" if deltas and average_delta > 0.0 else "fail",
            "source": "deterministic_path_benchmark",
            "scenario_count": len(scenario_results),
            "average_return_delta": round(average_delta, 6),
            "first_take_profit": round(first_take_profit, 6),
            "second_take_profit": round(second_take_profit, 6),
            "trailing_stop_pct": round(trailing_stop_pct, 6),
            "scenarios": scenario_results,
        }

    def _simulate_staged_take_profit_path(
        self,
        *,
        prices: list[float],
        first_take_profit: float,
        second_take_profit: float,
        trailing_stop_pct: float,
        stop_loss_pct: float,
    ) -> dict[str, object]:
        if not prices:
            return {"weighted_return": 0.0, "exit_reason": "empty_path"}
        entry_price = max(0.01, prices[0])
        remaining_position = 1.0
        realized_return = 0.0
        take_profit_stage = 0
        peak_price = entry_price
        exit_reason = "end_of_path"

        for price in prices[1:]:
            pnl_pct = price / entry_price - 1.0
            if stop_loss_pct > 0 and pnl_pct <= -stop_loss_pct:
                realized_return += remaining_position * pnl_pct
                remaining_position = 0.0
                exit_reason = "stop_loss"
                break
            if take_profit_stage < 1 and pnl_pct >= first_take_profit:
                trim_weight = min(remaining_position, 1.0 / 3.0)
                realized_return += trim_weight * pnl_pct
                remaining_position -= trim_weight
                take_profit_stage = 1
                peak_price = max(peak_price, price)
                continue
            if take_profit_stage < 2 and pnl_pct >= second_take_profit:
                trim_weight = min(remaining_position, 1.0 / 3.0)
                realized_return += trim_weight * pnl_pct
                remaining_position -= trim_weight
                take_profit_stage = 2
                peak_price = max(peak_price, price)
                continue
            if take_profit_stage >= 2:
                peak_price = max(peak_price, price)
                drawdown_from_peak = price / peak_price - 1.0 if peak_price > 0 else 0.0
                if trailing_stop_pct > 0 and drawdown_from_peak <= -trailing_stop_pct:
                    realized_return += remaining_position * pnl_pct
                    remaining_position = 0.0
                    exit_reason = "trailing_stop"
                    break

        if remaining_position > 0.0:
            final_pnl_pct = prices[-1] / entry_price - 1.0
            realized_return += remaining_position * final_pnl_pct
        return {
            "weighted_return": realized_return,
            "exit_reason": exit_reason,
        }

    def _simulate_single_exit_path(
        self,
        *,
        prices: list[float],
        second_take_profit: float,
        trailing_stop_pct: float,
        stop_loss_pct: float,
    ) -> dict[str, object]:
        if not prices:
            return {"weighted_return": 0.0, "exit_reason": "empty_path"}
        entry_price = max(0.01, prices[0])
        trailing_armed = False
        peak_price = entry_price
        exit_reason = "end_of_path"
        exit_return = prices[-1] / entry_price - 1.0

        for price in prices[1:]:
            pnl_pct = price / entry_price - 1.0
            if stop_loss_pct > 0 and pnl_pct <= -stop_loss_pct:
                exit_return = pnl_pct
                exit_reason = "stop_loss"
                break
            if not trailing_armed and pnl_pct >= second_take_profit:
                trailing_armed = True
                peak_price = max(peak_price, price)
                continue
            if trailing_armed:
                peak_price = max(peak_price, price)
                drawdown_from_peak = price / peak_price - 1.0 if peak_price > 0 else 0.0
                if trailing_stop_pct > 0 and drawdown_from_peak <= -trailing_stop_pct:
                    exit_return = pnl_pct
                    exit_reason = "trailing_stop"
                    break

        return {
            "weighted_return": exit_return,
            "exit_reason": exit_reason,
        }

    def _build_hrp_shadow_acceptance_summary(
        self,
        *,
        symbols: list[str],
    ) -> dict[str, object]:
        service = self._service
        candidate_symbols = service._acceptance_benchmark_symbols(seed_symbols=symbols, limit=6)
        signals = [
            PipelineSignal(
                symbol=symbol,
                strategy="trend",
                score=90.0 - idx,
                grade="A" if idx < 3 else "B",
                action="buy" if idx < 3 else "watch",
                target_position=0.1,
                probabilities={"lgbm": 0.75, "xgb": 0.75, "meta": 0.75},
                reasons=["acceptance_benchmark"],
            )
            for idx, symbol in enumerate(candidate_symbols[:6])
        ]
        shadow = service._build_c3_hrp_shadow_portfolio(signals=signals, strategy="trend")
        raw_weights = shadow.get("weights", [])
        if not isinstance(raw_weights, list) or len(raw_weights) < 5:
            return {
                "status": "warn",
                "source": str(shadow.get("method", shadow.get("reason", "hrp_shadow"))),
                "sample_count": 0,
                "baseline_max_drawdown": None,
                "shadow_max_drawdown": None,
            }

        returns_cache: dict[str, pd.Series | None] = {}
        returns_by_symbol: dict[str, pd.Series] = {}
        for item in raw_weights:
            if not isinstance(item, Mapping):
                continue
            symbol = str(item.get("symbol", "")).strip()
            if not symbol:
                continue
            series = service._recent_return_series(symbol=symbol, returns_cache=returns_cache)
            if series is not None:
                returns_by_symbol[symbol] = series

        if len(returns_by_symbol) < 5:
            return {
                "status": "warn",
                "source": str(shadow.get("method", shadow.get("reason", "hrp_shadow"))),
                "sample_count": 0,
                "baseline_max_drawdown": None,
                "shadow_max_drawdown": None,
            }

        returns_frame = pd.DataFrame(returns_by_symbol).dropna(axis=0, how="any")
        if returns_frame.shape[0] < 20 or returns_frame.shape[1] < 5:
            return {
                "status": "warn",
                "source": str(shadow.get("method", shadow.get("reason", "hrp_shadow"))),
                "sample_count": int(returns_frame.shape[0]),
                "baseline_max_drawdown": None,
                "shadow_max_drawdown": None,
            }

        symbols_in_frame = list(returns_frame.columns)
        equal_weight = 1.0 / len(symbols_in_frame)
        baseline_weights = pd.Series(
            {symbol: equal_weight for symbol in symbols_in_frame},
            dtype=float,
        )
        shadow_weights = pd.Series(
            {
                str(item.get("symbol", "")).strip(): _as_float(item.get("weight"), default=0.0)
                for item in raw_weights
                if isinstance(item, Mapping)
                and str(item.get("symbol", "")).strip() in symbols_in_frame
            },
            dtype=float,
        )
        shadow_weights = shadow_weights.reindex(symbols_in_frame).fillna(0.0)
        weight_sum = float(shadow_weights.sum())
        if weight_sum <= 0.0:
            return {
                "status": "warn",
                "source": str(shadow.get("method", shadow.get("reason", "hrp_shadow"))),
                "sample_count": int(returns_frame.shape[0]),
                "baseline_max_drawdown": None,
                "shadow_max_drawdown": None,
            }
        shadow_weights = shadow_weights / weight_sum

        baseline_returns = returns_frame.mul(baseline_weights, axis=1).sum(axis=1)
        shadow_returns = returns_frame.mul(shadow_weights, axis=1).sum(axis=1)
        baseline_drawdown = service._return_series_max_drawdown(baseline_returns)
        shadow_drawdown = service._return_series_max_drawdown(shadow_returns)

        return {
            "status": "pass" if shadow_drawdown <= baseline_drawdown else "fail",
            "source": str(shadow.get("method", shadow.get("reason", "hrp_shadow"))),
            "sample_count": int(returns_frame.shape[0]),
            "baseline_max_drawdown": round(baseline_drawdown, 6),
            "shadow_max_drawdown": round(shadow_drawdown, 6),
            "drawdown_delta": round(shadow_drawdown - baseline_drawdown, 6),
            "weights": [
                {
                    "symbol": symbol,
                    "weight": round(_as_float(shadow_weights.get(symbol), default=0.0), 6),
                }
                for symbol in symbols_in_frame
            ],
        }

    def _return_series_max_drawdown(self, returns: pd.Series) -> float:
        clean_returns = pd.to_numeric(returns, errors="coerce").dropna()
        if clean_returns.empty:
            return 0.0
        equity = np.cumprod(1.0 + clean_returns.to_numpy(dtype=float))
        running_max = np.maximum.accumulate(equity)
        drawdowns = 1.0 - equity / np.maximum(running_max, 1e-12)
        return float(np.max(drawdowns)) if drawdowns.size else 0.0

    def generate_phase_checkpoint(
        self,
        phase: str,
        baseline_report_path: str | None = None,
        output_path: str | None = None,
    ) -> dict[str, object]:
        service = self._service
        baseline_path = Path(baseline_report_path or service._config.training.baseline_report_path)
        if not baseline_path.exists():
            raise FileNotFoundError(f"baseline report not found: {baseline_path}")
        payload = json.loads(baseline_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("baseline report payload must be a JSON object")
        checkpoint = build_phase_checkpoint(
            phase=phase,
            baseline_report=payload,
            baseline_report_path=baseline_path,
            project_root=Path(__file__).resolve().parents[4],
        )
        normalized_phase = phase.strip().lower()
        target = Path(
            output_path or f"artifacts/acceptance/checkpoint_phase_{normalized_phase}.json"
        )
        written = write_checkpoint(checkpoint=checkpoint, output_path=target)
        checkpoint["output_path"] = written
        return cast(dict[str, object], checkpoint)

    def generate_v13_acceptance_report(
        self,
        *,
        baseline_report_path: str | None = None,
        output_path: str | None = None,
        week5_report: Mapping[str, object] | None = None,
        phase_checkpoints: Mapping[str, Mapping[str, object]] | None = None,
        m9_failure_retention_report: Mapping[str, object] | None = None,
        portfolio_execution_report: Mapping[str, object] | None = None,
        label_conflict_shadow_report: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        service = self._service
        baseline_path = Path(baseline_report_path or service._config.training.baseline_report_path)
        if not baseline_path.exists():
            raise FileNotFoundError(f"baseline report not found: {baseline_path}")
        baseline_payload = json.loads(baseline_path.read_text(encoding="utf-8"))
        if not isinstance(baseline_payload, dict):
            raise ValueError("baseline report payload must be a JSON object")

        checkpoints: dict[str, dict[str, object]] = {}
        if phase_checkpoints is None:
            for phase in ("A", "B", "C"):
                checkpoint = service.generate_phase_checkpoint(
                    phase=phase,
                    baseline_report_path=str(baseline_path),
                )
                checkpoints[phase] = checkpoint
        else:
            checkpoints = {
                str(name): dict(payload)
                for name, payload in phase_checkpoints.items()
                if isinstance(payload, Mapping)
            }

        active_week5 = dict(week5_report) if isinstance(week5_report, Mapping) else {}
        if not active_week5:
            latest_week5 = service.latest_week5_scan_report()
            if isinstance(latest_week5, dict):
                active_week5 = latest_week5
        if not active_week5:
            baseline_symbol = (
                _normalize_a_share_symbol(baseline_payload.get("symbol"))
                or str(baseline_payload.get("symbol", "")).strip()
            )
            fallback_symbols = (
                [baseline_symbol]
                if baseline_symbol
                else service._acceptance_benchmark_symbols(limit=5)
            )
            generated_week5 = service.run_week5_scan(
                symbols=fallback_symbols,
                notify_enabled=False,
                sync_watchlist=False,
            )
            if isinstance(generated_week5, dict):
                active_week5 = generated_week5
        if not service._week5_report_has_acceptance_evidence(active_week5):
            baseline_symbol = (
                _normalize_a_share_symbol(baseline_payload.get("symbol"))
                or str(baseline_payload.get("symbol", "")).strip()
            )
            active_week5 = service._build_acceptance_week5_fallback_report(
                symbols=service._acceptance_benchmark_symbols(
                    seed_symbols=[baseline_symbol] if baseline_symbol else None,
                    limit=6,
                )
            )

        m9_report_payload: dict[str, object] = {}
        if isinstance(m9_failure_retention_report, Mapping):
            m9_report_payload = dict(m9_failure_retention_report)
        else:
            m9_report_path = Path("artifacts/acceptance/m9_failure_retention_report.json")
            if m9_report_path.exists():
                loaded = json.loads(m9_report_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    m9_report_payload = loaded

        portfolio_execution_payload: dict[str, object] = {}
        if isinstance(portfolio_execution_report, Mapping):
            portfolio_execution_payload = dict(portfolio_execution_report)
        else:
            portfolio_execution_path = Path("artifacts/acceptance/portfolio_execution_report.json")
            if portfolio_execution_path.exists():
                loaded = json.loads(portfolio_execution_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    portfolio_execution_payload = loaded
        if not portfolio_execution_payload:
            portfolio_execution_payload = service.generate_portfolio_execution_report(
                symbols=service._acceptance_benchmark_symbols(
                    seed_symbols=[
                        _normalize_a_share_symbol(baseline_payload.get("symbol"))
                        or str(baseline_payload.get("symbol", "")).strip()
                    ],
                    limit=6,
                )
            )

        label_conflict_shadow_payload: dict[str, object] = {}
        if isinstance(label_conflict_shadow_report, Mapping):
            label_conflict_shadow_payload = dict(label_conflict_shadow_report)
        else:
            label_conflict_shadow_path = Path("artifacts/acceptance/label_conflict_shadow_report.json")
            if label_conflict_shadow_path.exists():
                loaded = json.loads(label_conflict_shadow_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    label_conflict_shadow_payload = loaded
        if not label_conflict_shadow_payload:
            baseline_symbol = (
                _normalize_a_share_symbol(baseline_payload.get("symbol"))
                or str(baseline_payload.get("symbol", "")).strip()
            )
            if baseline_symbol:
                label_conflict_shadow_payload = service.generate_label_conflict_shadow_report(
                    symbol=baseline_symbol,
                    lookback_days=_as_int(baseline_payload.get("lookback_days"), default=800),
                )

        report = build_v13_acceptance_report(
            baseline_report=baseline_payload,
            provider_status=service.provider_status(),
            artifact_summary=summarize_model_artifact(
                artifact_path=service._config.training.artifact_path
            ),
            week5_report=active_week5,
            positions=service.portfolio_positions(),
            phase_checkpoints=checkpoints,
            m9_failure_retention_report=m9_report_payload,
            portfolio_execution_report=portfolio_execution_payload,
            label_conflict_shadow_report=label_conflict_shadow_payload,
        )
        target = Path(output_path or "artifacts/acceptance/v13_acceptance_report.json")
        report["output_path"] = persist_v13_acceptance_report(report=report, output_path=target)
        return cast(dict[str, object], report)

    def generate_v13_acceptance_bundle(
        self,
        *,
        symbol: str,
        lookback_days: int = 800,
        baseline_output_path: str | None = None,
        v13_output_path: str | None = None,
        run_week5_scan: bool = False,
        week5_symbols: list[str] | None = None,
    ) -> dict[str, object]:
        service = self._service
        baseline = service.generate_baseline_report(
            symbol=symbol,
            lookback_days=lookback_days,
            output_path=baseline_output_path,
        )
        checkpoints = {
            phase: service.generate_phase_checkpoint(
                phase=phase,
                baseline_report_path=str(baseline.get("output_path", "")),
            )
            for phase in ("A", "B", "C")
        }
        m9_retention = service.generate_m9_failure_retention_report()
        portfolio_execution = service.generate_portfolio_execution_report(symbols=week5_symbols)
        bundle_symbol = (
            _normalize_a_share_symbol(baseline.get("symbol"))
            or str(baseline.get("symbol", symbol)).strip()
            or symbol
        )
        label_conflict_shadow = service.generate_label_conflict_shadow_report(
            symbol=bundle_symbol,
            lookback_days=lookback_days,
        )
        week5_report: dict[str, object] | None = None
        if run_week5_scan:
            generated_week5 = service.run_week5_scan(
                symbols=week5_symbols,
                notify_enabled=False,
                sync_watchlist=False,
            )
            if isinstance(generated_week5, dict):
                week5_report = generated_week5
        report = service.generate_v13_acceptance_report(
            baseline_report_path=str(baseline.get("output_path", "")),
            output_path=v13_output_path,
            week5_report=week5_report,
            phase_checkpoints=checkpoints,
            m9_failure_retention_report=m9_retention,
            portfolio_execution_report=portfolio_execution,
            label_conflict_shadow_report=label_conflict_shadow,
        )
        return {
            "baseline": baseline,
            "phase_checkpoints": checkpoints,
            "m9_failure_retention": m9_retention,
            "portfolio_execution": portfolio_execution,
            "label_conflict_shadow": label_conflict_shadow,
            "v13_acceptance": report,
        }

    def generate_acceptance_release_gate_report(
        self,
        *,
        v13_report_path: str | None = None,
        output_path: str | None = None,
        closed_loop_smoke_passed: bool = False,
        closed_loop_smoke_detail: str = "",
    ) -> dict[str, object]:
        service = self._service
        report_path = Path(v13_report_path or "artifacts/acceptance/v13_acceptance_report.json")
        if report_path.exists():
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("v13 acceptance payload must be a JSON object")
            v13_report = payload
        else:
            v13_report = service.generate_v13_acceptance_report()

        report = build_acceptance_release_gate_report(
            v13_acceptance_report=v13_report,
            closed_loop_smoke_passed=closed_loop_smoke_passed,
            closed_loop_smoke_detail=closed_loop_smoke_detail,
        )
        target = Path(output_path or "artifacts/acceptance/release_gate_report.json")
        report["output_path"] = persist_acceptance_release_gate_report(
            report=report,
            output_path=target,
        )
        return cast(dict[str, object], report)

    def generate_phase_d_status_report(
        self,
        *,
        output_path: str | None = None,
    ) -> dict[str, object]:
        report = build_phase_d_status_report()
        target = Path(output_path or "artifacts/acceptance/phase_d_status_report.json")
        report["output_path"] = persist_phase_d_status_report(
            report=report,
            output_path=target,
        )
        return cast(dict[str, object], report)

    def generate_phase_d6_registry_report(
        self,
        *,
        output_path: str | None = None,
    ) -> dict[str, object]:
        report = build_deferred_items_registry()
        target = Path(output_path or "artifacts/acceptance/phase_d6_research_registry.json")
        report["output_path"] = write_deferred_items_registry(
            registry=report,
            output_path=target,
        )
        return cast(dict[str, object], report)


def _path_exists_in_mapping(payload: Mapping[str, object], dotted_path: str) -> bool:
    current: object = payload
    for key in dotted_path.split("."):
        if not isinstance(current, Mapping) or key not in current:
            return False
        current = current[key]
    return current is not None


def _make_check(
    name: str,
    status: str,
    detail: str,
    *,
    scope: str = "week4_acceptance",
) -> dict[str, object]:
    return {
        "name": name,
        "status": status,
        "detail": detail,
        "scope": scope,
    }


def _valid_hhmm(raw: str) -> bool:
    parts = raw.split(":")
    if len(parts) != 2:
        return False
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59


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
