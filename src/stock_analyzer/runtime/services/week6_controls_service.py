"""Week6 execution-control helpers extracted from the runtime service."""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from importlib import import_module
from typing import TYPE_CHECKING, Any, cast

from stock_analyzer.types import PipelineSignal

if TYPE_CHECKING:
    from stock_analyzer.runtime.service import StockAnalyzerService


class RuntimeWeek6ControlsService:
    """Build and apply week6 execution controls."""

    def __init__(self, service: StockAnalyzerService) -> None:
        self._service = service

    def build_week6_execution_controls(
        self,
        strategy: str,
        symbols: list[str],
        drawdown_pct: float,
    ) -> dict[str, object]:
        service = self._service
        now = datetime.now()
        calendar_factor = service._calendar_factor_engine.evaluate(now.date())
        global_factor = service._global_market_factor_engine.evaluate(
            service._global_market_snapshot
        )
        evolution_controls = service._resolve_evolution_runtime_controls()
        evolution_risk_delta = _as_float(evolution_controls.get("global_risk_delta"), default=0.0)
        global_risk_score = _clamp(
            _as_float(global_factor.get("risk_score"), default=50.0) + evolution_risk_delta,
            0.0,
            100.0,
        )
        latest_week5 = service._last_week5_scan_report
        empty_signal_triggered = False
        if isinstance(latest_week5, dict):
            empty_signal = latest_week5.get("empty_signal")
            if isinstance(empty_signal, dict):
                empty_signal_triggered = bool(empty_signal.get("triggered", False))

        regime = service._strategy_allocation_engine.infer_regime(
            drawdown_pct=drawdown_pct,
            global_risk_score=global_risk_score,
            empty_signal_triggered=empty_signal_triggered,
        )
        allocation_weights = service._strategy_allocation_engine.allocation(regime=regime)
        calendar_threshold_shift = _as_float(
            calendar_factor.get("threshold_adjust"),
            default=0.0,
        )
        global_threshold_shift = _as_float(
            global_factor.get("threshold_adjust"),
            default=0.0,
        )
        evolution_threshold_shift = _as_float(
            evolution_controls.get("threshold_shift"),
            default=0.0,
        )
        threshold_shift = (
            calendar_threshold_shift + global_threshold_shift + evolution_threshold_shift
        )
        calendar_position_multiplier = _as_float(
            calendar_factor.get("position_multiplier"),
            default=1.0,
        )
        global_position_multiplier = 1.0 + _as_float(
            global_factor.get("position_adjust_pct"),
            default=0.0,
        )
        evolution_position_multiplier = _as_float(
            evolution_controls.get("position_multiplier"),
            default=1.0,
        )
        position_multiplier = (
            calendar_position_multiplier
            * global_position_multiplier
            * evolution_position_multiplier
        )
        position_multiplier = _clamp(position_multiplier, 0.20, 1.00)

        regulatory = service._regulatory_factor_engine.evaluate(
            symbols=symbols,
            watchlist=service._regulatory_watchlist,
        )
        action_map: dict[str, dict[str, object]] = {}
        raw_actions = regulatory.get("actions")
        if isinstance(raw_actions, list):
            for item in raw_actions:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol", "")).strip()
                if symbol:
                    action_map[symbol] = item

        return {
            "strategy": strategy,
            "regime": regime,
            "allocation_weights": allocation_weights,
            "threshold_shift": threshold_shift,
            "position_multiplier": position_multiplier,
            "global_risk_score": global_risk_score,
            "calendar_factor": calendar_factor,
            "global_factor": global_factor,
            "evolution": evolution_controls,
            "threshold_shift_components": {
                "calendar": calendar_threshold_shift,
                "global": global_threshold_shift,
                "evolution": evolution_threshold_shift,
            },
            "position_multiplier_components": {
                "calendar": calendar_position_multiplier,
                "global": global_position_multiplier,
                "evolution": evolution_position_multiplier,
            },
            "regulatory": regulatory,
            "regulatory_action_map": action_map,
        }

    def apply_week6_execution_controls(
        self,
        signals: list[PipelineSignal],
        strategy: str,
        controls: dict[str, object],
    ) -> dict[str, object]:
        thresholds = self.strategy_thresholds(strategy=strategy)
        threshold_shift = _as_float(controls.get("threshold_shift"), default=0.0)
        position_multiplier = _as_float(controls.get("position_multiplier"), default=1.0)
        action_map_raw = controls.get("regulatory_action_map")
        action_map: dict[str, dict[str, object]] = {}
        if isinstance(action_map_raw, dict):
            for key, value in action_map_raw.items():
                symbol = str(key).strip()
                if symbol and isinstance(value, dict):
                    action_map[symbol] = value

        buy_min = thresholds["a"] - threshold_shift
        watch_min = thresholds["b"] - threshold_shift
        changed = 0
        excluded = 0
        degraded = 0
        threshold_downgraded = 0
        scaled = 0
        evolution_raw = controls.get("evolution", {})
        evolution = evolution_raw if isinstance(evolution_raw, dict) else {}

        for signal in signals:
            before_action = signal.action
            before_position = signal.target_position
            before_score = signal.score
            action_info = action_map.get(signal.symbol, {})
            regulatory_action = str(action_info.get("action", "normal")).strip().lower()

            if regulatory_action == "exclude":
                excluded += 1
                signal.action = "hold"
                signal.target_position = 0.0
                signal.grade = "C"
                if "regulatory_exclude" not in signal.reasons:
                    signal.reasons.append("regulatory_exclude")
            else:
                if regulatory_action == "degrade":
                    degraded += 1
                    penalty = _as_float(action_info.get("penalty_score"), default=0.0)
                    signal.score = max(0.0, signal.score - penalty)
                    if "regulatory_degrade" not in signal.reasons:
                        signal.reasons.append("regulatory_degrade")
                    signal.grade = _grade_by_threshold(score=signal.score, thresholds=thresholds)

                if signal.action == "buy":
                    if signal.score < buy_min:
                        threshold_downgraded += 1
                        if signal.score >= watch_min:
                            signal.action = "watch"
                        else:
                            signal.action = "hold"
                        signal.target_position = 0.0
                        if "week6_threshold_gate" not in signal.reasons:
                            signal.reasons.append("week6_threshold_gate")
                elif signal.action == "watch" and signal.score < watch_min:
                    threshold_downgraded += 1
                    signal.action = "hold"
                    signal.target_position = 0.0
                    if "week6_threshold_gate" not in signal.reasons:
                        signal.reasons.append("week6_threshold_gate")

                if signal.action == "buy":
                    scaled_position = _clamp(signal.target_position * position_multiplier, 0.0, 1.0)
                    if abs(scaled_position - signal.target_position) > 1e-9:
                        scaled += 1
                        signal.target_position = round(scaled_position, 4)
                        if "week6_position_scaled" not in signal.reasons:
                            signal.reasons.append("week6_position_scaled")

            if (
                signal.action != before_action
                or abs(signal.target_position - before_position) > 1e-9
                or abs(signal.score - before_score) > 1e-9
            ):
                changed += 1

        return {
            "signals": len(signals),
            "changed": changed,
            "excluded": excluded,
            "degraded": degraded,
            "threshold_downgraded": threshold_downgraded,
            "scaled": scaled,
            "buy_min_effective": round(buy_min, 4),
            "watch_min_effective": round(watch_min, 4),
            "evolution_applied": bool(evolution),
            "evolution_conservative_mode": bool(evolution.get("conservative_mode", False)),
            "evolution_reasons": [
                str(item).strip() for item in evolution.get("reasons", []) if str(item).strip()
            ][:8],
        }

    def strategy_thresholds(self, strategy: str) -> dict[str, float]:
        service = self._service
        if strategy in service._config.strategy_scores:
            profile = service._config.strategy_scores[strategy]
            return {
                "s": float(profile.thresholds.s),
                "a": float(profile.thresholds.a),
                "b": float(profile.thresholds.b),
            }
        base = service._config.score.thresholds
        return {
            "s": float(base.s),
            "a": float(base.a),
            "b": float(base.b),
        }


@lru_cache(maxsize=1)
def _runtime_service_module() -> Any:
    return import_module("stock_analyzer.runtime.service")


def _as_float(value: object, default: float) -> float:
    return cast(float, _runtime_service_module()._as_float(value, default))


def _clamp(value: float, min_value: float, max_value: float) -> float:
    return cast(float, _runtime_service_module()._clamp(value, min_value, max_value))


def _grade_by_threshold(score: float, thresholds: dict[str, float]) -> str:
    return cast(
        str,
        _runtime_service_module()._grade_by_threshold(
            score=score,
            thresholds=thresholds,
        ),
    )
