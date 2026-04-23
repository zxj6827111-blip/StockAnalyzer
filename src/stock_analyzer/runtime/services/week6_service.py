"""Week6 analysis, data quality, and persistence workflows extracted from the runtime service."""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from stock_analyzer.runtime.services.week6_controls_service import RuntimeWeek6ControlsService
from stock_analyzer.runtime.services.week6_state_service import RuntimeWeek6StateService

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService
    from stock_analyzer.types import PipelineSignal


class RuntimeWeek6Service:
    """Delegated week6 analysis, regulatory, and state persistence workflows."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service
        self._controls_service = RuntimeWeek6ControlsService(service)
        self._state_service = RuntimeWeek6StateService(service)

    def run_week6_data_prewarm(
        self,
        symbols: list[str] | None = None,
        lookback_days: int | None = None,
        notify_enabled: bool | None = None,
        source_trace_id: str = "",
        timestamp: datetime | None = None,
    ) -> dict[str, object]:
        service = self._service
        now = timestamp or datetime.now()
        raw_symbols = symbols if symbols is not None else list(service._state.watchlist)
        symbol_list = [str(item).strip() for item in raw_symbols if str(item).strip()]
        quality_fields = [
            str(item).strip()
            for item in service._config.week6.data_quality_fields
            if str(item).strip()
        ]
        core_fields = {
            str(item).strip()
            for item in service._config.week6.data_quality_core_fields
            if str(item).strip()
        }
        if not quality_fields:
            quality_fields = [
                "financial_data_complete",
                "roe",
                "debt_ratio",
                "background_data_complete",
            ]
        if not symbol_list:
            empty_report = {
                "timestamp": now.isoformat(),
                "source": "week6_data_prewarm",
                "watchlist_size": 0,
                "lookback_days": max(20, service._config.week6.data_prewarm_lookback_days),
                "status": "no_symbols",
                "overall_coverage_ratio": 0.0,
                "success_symbols": 0,
                "failed_symbols": 0,
                "coverage_by_field": {field: 0.0 for field in quality_fields},
                "core_coverage_min": 0.0,
                "warn_fields": list(quality_fields),
                "critical_fields": list(quality_fields),
                "symbols": [],
            }
            self._state_service.store_week6_data_quality_report(empty_report)
            return empty_report

        lookback = lookback_days
        if lookback is None:
            lookback = service._config.week6.data_prewarm_lookback_days
        effective_lookback = max(20, int(lookback))

        field_valid_counts: dict[str, int] = {field: 0 for field in quality_fields}
        symbol_rows: list[dict[str, object]] = []
        success = 0
        failed = 0
        for symbol in symbol_list:
            try:
                bars = service._provider.fetch_daily_bars(
                    symbol=symbol, lookback_days=effective_lookback
                )
            except Exception as exc:
                failed += 1
                symbol_rows.append(
                    {
                        "symbol": symbol,
                        "ok": False,
                        "missing_fields": list(quality_fields),
                        "coverage_ratio": 0.0,
                        "error": str(exc),
                    }
                )
                continue
            if bars.empty:
                failed += 1
                symbol_rows.append(
                    {
                        "symbol": symbol,
                        "ok": False,
                        "missing_fields": list(quality_fields),
                        "coverage_ratio": 0.0,
                        "error": "empty_bars",
                    }
                )
                continue

            success += 1
            latest = bars.iloc[-1]
            missing_fields: list[str] = []
            valid_fields: list[str] = []
            for field in quality_fields:
                raw_value = latest.get(field)
                if _is_week6_quality_field_valid(field=field, value=raw_value):
                    field_valid_counts[field] = field_valid_counts.get(field, 0) + 1
                    valid_fields.append(field)
                else:
                    missing_fields.append(field)
            symbol_rows.append(
                {
                    "symbol": symbol,
                    "ok": True,
                    "missing_fields": missing_fields,
                    "coverage_ratio": round(len(valid_fields) / max(len(quality_fields), 1), 4),
                    "financial_source": str(latest.get("financial_source", "")),
                    "background_source": str(latest.get("background_data_source", "")),
                    "financial_missing_fields": str(latest.get("financial_missing_fields", "")),
                }
            )

        coverage_by_field = {
            field: round(field_valid_counts.get(field, 0) / max(success, 1), 4)
            for field in quality_fields
        }
        overall_coverage = 0.0
        if coverage_by_field:
            overall_coverage = sum(coverage_by_field.values()) / len(coverage_by_field)
        core_coverage_values = [
            coverage_by_field[field] for field in core_fields if field in coverage_by_field
        ]
        core_coverage_min = min(core_coverage_values) if core_coverage_values else overall_coverage

        warn_threshold = _clamp(service._config.week6.data_quality_warn_threshold, 0.0, 1.0)
        critical_threshold = _clamp(
            service._config.week6.data_quality_critical_threshold, 0.0, warn_threshold
        )
        warn_fields = [
            field for field, ratio in coverage_by_field.items() if ratio < warn_threshold
        ]
        critical_fields = [
            field for field, ratio in coverage_by_field.items() if ratio < critical_threshold
        ]
        if success <= 0:
            status = "critical"
        elif overall_coverage < critical_threshold or core_coverage_min < critical_threshold:
            status = "critical"
        elif overall_coverage < warn_threshold or core_coverage_min < warn_threshold:
            status = "warn"
        else:
            status = "healthy"

        report = {
            "timestamp": now.isoformat(),
            "source": "week6_data_prewarm",
            "watchlist_size": len(symbol_list),
            "lookback_days": effective_lookback,
            "status": status,
            "overall_coverage_ratio": round(overall_coverage, 4),
            "success_symbols": success,
            "failed_symbols": failed,
            "coverage_by_field": coverage_by_field,
            "core_coverage_min": round(core_coverage_min, 4),
            "warn_threshold": round(warn_threshold, 4),
            "critical_threshold": round(critical_threshold, 4),
            "warn_fields": warn_fields,
            "critical_fields": critical_fields,
            "symbols": symbol_rows,
        }
        self._state_service.store_week6_data_quality_report(report)
        level = "info" if status == "healthy" else "warn"
        service._record_audit_event(
            event_type="week6_data_quality_scan",
            trace_id=source_trace_id,
            level=level,
            payload={
                "status": status,
                "overall_coverage_ratio": report["overall_coverage_ratio"],
                "core_coverage_min": report["core_coverage_min"],
                "success_symbols": success,
                "failed_symbols": failed,
                "warn_fields": warn_fields,
                "critical_fields": critical_fields,
            },
        )
        use_notify = service._config.week6.data_quality_notify
        if notify_enabled is not None:
            use_notify = notify_enabled
        if use_notify and status != "healthy":
            service.notify(
                title=_push_title(
                    priority="P1", category="quality", summary="week6 data quality alert"
                ),
                content=(
                    f"状态={_week6_quality_status_zh(status)}；"
                    f"整体覆盖率={report['overall_coverage_ratio']:.2%}；"
                    f"核心最低覆盖率={report['core_coverage_min']:.2%}；"
                    f"告警字段={_week6_quality_fields_zh(warn_fields)}；"
                    f"严重字段={_week6_quality_fields_zh(critical_fields)}"
                ),
                level="warn",
                trace_id=source_trace_id,
            )
        return report

    def latest_week6_data_quality_report(self) -> dict[str, object] | None:
        return self._state_service.latest_week6_data_quality_report()

    def week6_data_quality_history(self, limit: int = 20) -> dict[str, object]:
        return self._state_service.week6_data_quality_history(limit=limit)

    def _store_week6_data_quality_report(self, report: dict[str, object]) -> None:
        self._state_service.store_week6_data_quality_report(report)

    def set_regulatory_watchlist(
        self,
        entries: list[dict[str, object]],
        source_trace_id: str = "",
    ) -> dict[str, object]:
        return self._state_service.set_regulatory_watchlist(
            entries=entries,
            source_trace_id=source_trace_id,
        )

    def regulatory_watchlist(self) -> dict[str, object]:
        return self._state_service.regulatory_watchlist()

    def run_week6_analysis(
        self,
        symbols: list[str] | None = None,
        timestamp: datetime | None = None,
        notify_enabled: bool | None = None,
    ) -> dict[str, object]:
        service = self._service
        if service._bootstrap_runtime_blocked():
            blocked = {
                "timestamp": (timestamp or datetime.now()).isoformat(),
                "status": "blocked_bootstrap_required",
                "watchlist_size": len(service._state.watchlist),
                "main_force": {"records": 0, "strong_count": 0, "items": [], "focus_symbols": []},
                "strategy_allocation": {"regime": "blocked", "weights": {}},
                "calendar_factor": service._calendar_factor_engine.evaluate(
                    (timestamp or datetime.now()).date()
                ),
                "global_market_factor": service._global_market_factor_engine.evaluate(
                    service._global_market_snapshot
                ),
                "regulatory_factor": {
                    "enabled": service._config.regulatory_factor.enabled,
                    "watched_symbols": len(service._regulatory_watchlist),
                    "actions": [],
                    "excluded_symbols": [],
                    "degraded_symbols": [],
                },
                "execution_adjustment": {"score_threshold_shift": 0.0, "position_multiplier": 0.0},
                "summary": {"focus_symbols": 0, "excluded_symbols": 0, "regime": "blocked"},
                "bootstrap": service.training_bootstrap_status(),
            }
            self._state_service.store_week6_report(blocked)
            service._record_audit_event(
                event_type="week6_blocked_bootstrap",
                level="warn",
                payload={"bootstrap": blocked["bootstrap"]},
            )
            return blocked
        now = timestamp or datetime.now()
        raw_symbols = symbols if symbols is not None else list(service._state.watchlist)
        symbol_list = [str(item).strip() for item in raw_symbols if str(item).strip()]
        if not symbol_list:
            empty_report = {
                "timestamp": now.isoformat(),
                "watchlist_size": 0,
                "main_force": {"records": 0, "strong_count": 0, "items": [], "focus_symbols": []},
                "strategy_allocation": {"regime": "range", "weights": {}},
                "calendar_factor": service._calendar_factor_engine.evaluate(now.date()),
                "global_market_factor": service._global_market_factor_engine.evaluate(
                    service._global_market_snapshot
                ),
                "regulatory_factor": {
                    "enabled": service._config.regulatory_factor.enabled,
                    "watched_symbols": len(service._regulatory_watchlist),
                    "actions": [],
                    "excluded_symbols": [],
                    "degraded_symbols": [],
                },
                "execution_adjustment": {"score_threshold_shift": 0.0, "position_multiplier": 0.0},
                "summary": {"focus_symbols": 0, "excluded_symbols": 0, "regime": "range"},
            }
            self._state_service.store_week6_report(empty_report)
            service._record_audit_event(
                event_type="week6_run",
                level="warn",
                payload={"watchlist_size": 0, "reason": "empty_watchlist"},
            )
            return empty_report

        latest_week5 = service._last_week5_scan_report
        if latest_week5 is None:
            latest_week5 = service.run_week5_scan(
                symbols=symbol_list,
                timestamp=now,
                notify_enabled=False,
            )
        empty_signal_data = latest_week5.get("empty_signal", {}) if latest_week5 else {}
        empty_signal_triggered = False
        if isinstance(empty_signal_data, dict):
            empty_signal_triggered = bool(empty_signal_data.get("triggered", False))

        latest_report = service.latest_report()
        drawdown_pct = 0.0
        if isinstance(latest_report, dict):
            raw_risk = latest_report.get("risk")
            if isinstance(raw_risk, dict):
                drawdown_pct = _as_float(raw_risk.get("drawdown_pct"), default=0.0)

        global_factor = service._global_market_factor_engine.evaluate(
            service._global_market_snapshot
        )
        global_risk_score = _as_float(global_factor.get("risk_score"), default=50.0)
        calendar_factor = service._calendar_factor_engine.evaluate(now.date())
        regime = service._strategy_allocation_engine.infer_regime(
            drawdown_pct=drawdown_pct,
            global_risk_score=global_risk_score,
            empty_signal_triggered=empty_signal_triggered,
        )
        allocation_weights = service._strategy_allocation_engine.allocation(regime=regime)
        regulatory_factor = service._regulatory_factor_engine.evaluate(
            symbols=symbol_list,
            watchlist=service._regulatory_watchlist,
        )
        action_map: dict[str, dict[str, object]] = {}
        raw_actions = regulatory_factor.get("actions")
        if isinstance(raw_actions, list):
            for item in raw_actions:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol", "")).strip()
                if symbol:
                    action_map[symbol] = item

        lookback_days = max(20, service._config.week6.main_force.lookback_days)
        main_force_items: list[dict[str, object]] = []
        for symbol in symbol_list:
            try:
                bars = service._provider.fetch_daily_bars(
                    symbol=symbol,
                    lookback_days=lookback_days,
                )
            except Exception as exc:
                main_force_items.append(
                    {
                        "symbol": symbol,
                        "score": 0.0,
                        "adjusted_score": 0.0,
                        "status": "data_error",
                        "eligible": False,
                        "error": str(exc),
                        "regulatory_action": "unknown",
                    }
                )
                continue

            item = service._main_force_tracker.analyze_symbol(symbol=symbol, bars=bars)
            raw_score = _as_float(item.get("score"), default=0.0)
            action_info = action_map.get(symbol, {})
            regulatory_action = str(action_info.get("action", "normal"))
            penalty = _as_float(action_info.get("penalty_score"), default=0.0)
            adjusted_score = raw_score
            eligible = True
            if regulatory_action == "exclude":
                adjusted_score = 0.0
                eligible = False
            elif regulatory_action == "degrade":
                adjusted_score = max(0.0, raw_score - penalty)

            item["regulatory_action"] = regulatory_action
            item["regulatory_tag"] = str(action_info.get("tag", ""))
            item["adjusted_score"] = round(adjusted_score, 2)
            item["eligible"] = eligible
            main_force_items.append(item)

        strong_count = sum(1 for item in main_force_items if str(item.get("status")) == "strong")
        focus_symbols = [
            item
            for item in sorted(
                main_force_items,
                key=lambda item: (
                    -_as_float(item.get("adjusted_score"), default=0.0),
                    str(item.get("symbol", "")),
                ),
            )
            if bool(item.get("eligible", False))
        ][:5]

        threshold_shift = _as_float(
            calendar_factor.get("threshold_adjust"),
            default=0.0,
        ) + _as_float(
            global_factor.get("threshold_adjust"),
            default=0.0,
        )
        position_multiplier = _as_float(
            calendar_factor.get("position_multiplier"),
            default=1.0,
        ) * (1.0 + _as_float(global_factor.get("position_adjust_pct"), default=0.0))
        position_multiplier = _clamp(position_multiplier, 0.20, 1.00)

        excluded_symbols = regulatory_factor.get("excluded_symbols")
        excluded_count = len(excluded_symbols) if isinstance(excluded_symbols, list) else 0
        report: dict[str, object] = {
            "timestamp": now.isoformat(),
            "watchlist_size": len(symbol_list),
            "main_force": {
                "records": len(main_force_items),
                "strong_count": strong_count,
                "items": main_force_items,
                "focus_symbols": focus_symbols,
            },
            "strategy_allocation": {
                "regime": regime,
                "weights": allocation_weights,
                "drawdown_pct": round(drawdown_pct, 4),
                "global_risk_score": round(global_risk_score, 2),
                "empty_signal_triggered": empty_signal_triggered,
            },
            "calendar_factor": calendar_factor,
            "global_market_factor": global_factor,
            "regulatory_factor": regulatory_factor,
            "execution_adjustment": {
                "score_threshold_shift": round(threshold_shift, 4),
                "position_multiplier": round(position_multiplier, 4),
            },
            "data_quality": service._last_week6_data_quality_report,
            "summary": {
                "focus_symbols": len(focus_symbols),
                "excluded_symbols": excluded_count,
                "regime": regime,
            },
        }
        self._state_service.store_week6_report(report)
        warn = regime == "crash" or excluded_count > 0
        service._record_audit_event(
            event_type="week6_run",
            level="warn" if warn else "info",
            payload={
                "watchlist_size": len(symbol_list),
                "strong_count": strong_count,
                "focus_symbols": len(focus_symbols),
                "regime": regime,
                "excluded_symbols": excluded_count,
            },
        )
        use_notify = service._config.week6.auto_notify
        if notify_enabled is not None:
            use_notify = notify_enabled
        if use_notify:
            service.notify(
                title=_push_title(
                    priority="P1" if warn else "P2", category="week6", summary="daily allocation"
                ),
                content=(
                    f"市场状态={_regime_zh(regime)}；"
                    f"观察池数量={len(symbol_list)}；"
                    f"重点标的={len(focus_symbols)}；"
                    f"强势标的={strong_count}；"
                    f"排除标的={excluded_count}；"
                    f"回撤={drawdown_pct:.2%}；"
                    f"全局风险分={global_risk_score:.1f}"
                ),
                level="warn" if warn else "info",
            )

        return report

    def latest_week6_report(self) -> dict[str, object] | None:
        return self._state_service.latest_week6_report()

    def week6_history(self, limit: int = 20) -> dict[str, object]:
        return self._state_service.week6_history(limit=limit)

    def _store_week6_report(self, report: dict[str, object]) -> None:
        self._state_service.store_week6_report(report)

    def _resolve_week6_state_file(self) -> Path:
        return self._state_service.resolve_week6_state_file()

    def _persist_week6_state(self) -> None:
        self._state_service.persist_week6_state()

    def _load_week6_state(self) -> None:
        self._state_service.load_week6_state()

    def _build_week6_execution_controls(
        self,
        strategy: str,
        symbols: list[str],
        drawdown_pct: float,
    ) -> dict[str, object]:
        return self._controls_service.build_week6_execution_controls(
            strategy=strategy,
            symbols=symbols,
            drawdown_pct=drawdown_pct,
        )

    def _apply_week6_execution_controls(
        self,
        signals: list[PipelineSignal],
        strategy: str,
        controls: dict[str, object],
    ) -> dict[str, object]:
        return self._controls_service.apply_week6_execution_controls(
            signals=signals,
            strategy=strategy,
            controls=controls,
        )

    def _strategy_thresholds(self, strategy: str) -> dict[str, float]:
        return self._controls_service.strategy_thresholds(strategy=strategy)

    def update_global_market_snapshot(
        self,
        snapshot: dict[str, object],
        source_trace_id: str = "",
    ) -> dict[str, object]:
        return self._state_service.update_global_market_snapshot(
            snapshot=snapshot,
            source_trace_id=source_trace_id,
        )

    def global_market_snapshot(self) -> dict[str, object]:
        return self._state_service.global_market_snapshot()

    def global_market_history(self, limit: int = 50) -> dict[str, object]:
        return self._state_service.global_market_history(limit=limit)


@lru_cache(maxsize=1)
def _runtime_service_module() -> Any:
    return import_module("stock_analyzer.runtime.service")


def _as_float(value: object, default: float) -> float:
    return cast(float, _runtime_service_module()._as_float(value, default))


def _as_int(value: object, default: int) -> int:
    return cast(int, _runtime_service_module()._as_int(value, default))


def _clamp(value: float, min_value: float, max_value: float) -> float:
    return cast(float, _runtime_service_module()._clamp(value, min_value, max_value))


def _push_title(priority: str, category: str, summary: str) -> str:
    return cast(str, _runtime_service_module()._push_title(priority, category, summary))


def _regime_zh(regime: str) -> str:
    return cast(str, _runtime_service_module()._regime_zh(regime))


def _week6_quality_status_zh(status: str) -> str:
    return cast(str, _runtime_service_module()._week6_quality_status_zh(status))


def _week6_quality_fields_zh(fields: list[str]) -> str:
    return cast(str, _runtime_service_module()._week6_quality_fields_zh(fields))


def _is_week6_quality_field_valid(*, field: str, value: object) -> bool:
    return cast(
        bool,
        _runtime_service_module()._is_week6_quality_field_valid(field=field, value=value),
    )
