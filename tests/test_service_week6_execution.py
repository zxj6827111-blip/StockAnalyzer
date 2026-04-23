from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.runtime import service as runtime_service_module
from stock_analyzer.runtime.service import StockAnalyzerService
from stock_analyzer.types import PipelineSignal


def _load_test_config() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.notifications.primary = "console"
    config.notifications.backup = "console"
    config.models.cross_review.p_lgbm_min = 0.0
    config.models.cross_review.p_xgb_min = 0.0
    config.models.cross_review.p_meta_min = 0.0
    config.models.cross_review.max_diff = 1.0
    config.liquidity_filter_trend.min_daily_turnover = 0.0
    config.liquidity_filter_trend.min_float_market_cap = 0.0
    config.liquidity_filter_trend.max_turnover_rate = 1.0
    config.training.artifact_path = str(root / "artifacts" / "nonexistent_test_model.json")
    config.week6.auto_notify = False
    config.week5.auto_notify = False
    config.strategy_kill_switch.enabled = False

    if "trend" in config.strategy_scores:
        config.strategy_scores["trend"].thresholds.s = 0.0
        config.strategy_scores["trend"].thresholds.a = 0.0
        config.strategy_scores["trend"].thresholds.b = 0.0
    return config


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    raise AssertionError(f"Expected mapping, got {type(value).__name__}")


def _as_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    raise AssertionError(f"Expected dict, got {type(value).__name__}")


def _as_mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        raise AssertionError(f"Expected list, got {type(value).__name__}")
    return [cast(Mapping[str, object], item) for item in value if isinstance(item, Mapping)]


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    raise AssertionError(f"Expected bool value, got {value!r}")


def _as_int(value: object) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    raise AssertionError(f"Expected numeric value, got {value!r}")


def _as_float(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    raise AssertionError(f"Expected numeric value, got {value!r}")


def _as_text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        raise AssertionError(f"Expected list, got {type(value).__name__}")
    return [str(item) for item in value]


def _patch_attr(target: object, name: str, value: object) -> None:
    setattr(cast(Any, target), name, value)


def _seed_regulatory_watchlist(
    service: StockAnalyzerService,
    entries: list[dict[str, object]],
) -> None:
    _patch_attr(
        service,
        "_regulatory_watchlist",
        {
            str(item.get("symbol", "")).strip(): {
                "symbol": str(item.get("symbol", "")).strip(),
                "tag": str(item.get("tag", "")).strip().lower(),
                "note": str(item.get("note", "")).strip(),
            }
            for item in entries
            if str(item.get("symbol", "")).strip()
        },
    )


def _reset_shared_week6_execution_service(service: StockAnalyzerService) -> None:
    service.state.current_equity = 1.0
    service.state.watchlist = []
    service.state.pause_new_buy = False
    service.state.reconcile_required = False
    _patch_attr(service, "_regulatory_watchlist", {})
    _patch_attr(service, "_global_market_snapshot", {})
    _patch_attr(service, "_global_market_history", [])
    _patch_attr(service, "_last_evolution_report", None)
    _patch_attr(service, "_last_week5_scan_report", None)
    _patch_attr(service, "_last_week6_report", None)
    _patch_attr(service, "_week6_history", [])
    _patch_attr(service, "_last_week6_data_quality_report", None)
    _patch_attr(service, "_week6_data_quality_history", [])
    _patch_attr(service, "_audit_events", [])
    _patch_attr(service, "_audit_seq", 0)


def _new_service(config: StockAnalyzerConfig) -> StockAnalyzerService:
    provider = SyntheticProvider(seed_offset=2028)
    original_build_runtime_provider = runtime_service_module.build_runtime_provider
    original_build_realtime_runtime_provider = (
        runtime_service_module.build_realtime_runtime_provider
    )
    original_build_market_depth_provider = runtime_service_module.build_market_depth_provider
    try:
        runtime_service_module.build_runtime_provider = (
            lambda config, synthetic_seed=2026: provider
        )
        runtime_service_module.build_realtime_runtime_provider = (
            lambda config, synthetic_seed=2026, timezone="Asia/Shanghai": provider
        )
        runtime_service_module.build_market_depth_provider = lambda config: None
        service = StockAnalyzerService(config=config)
    finally:
        runtime_service_module.build_runtime_provider = original_build_runtime_provider
        runtime_service_module.build_realtime_runtime_provider = (
            original_build_realtime_runtime_provider
        )
        runtime_service_module.build_market_depth_provider = original_build_market_depth_provider
    _patch_attr(service, "_provider", provider)
    _patch_attr(service._pipeline, "_provider", provider)
    _patch_attr(service, "_realtime_provider", provider)
    if service._realtime_pipeline is not None:
        _patch_attr(service._realtime_pipeline, "_provider", provider)
    _patch_attr(service, "_record_audit_event", lambda *args, **kwargs: None)
    _patch_attr(service, "_record_run_summary", lambda *args, **kwargs: None)
    _patch_attr(service, "_notify_expired_position_exits_if_needed", lambda *args, **kwargs: None)
    _patch_attr(service, "_notify_risk_status_if_needed", lambda *args, **kwargs: None)
    _patch_attr(service, "_notify_holding_alerts_if_needed", lambda *args, **kwargs: None)
    _patch_attr(service, "_notify_provider_health_if_needed", lambda *args, **kwargs: None)
    _patch_attr(service, "_notify_simulated_trade_updates_if_needed", lambda *args, **kwargs: None)
    _patch_attr(service, "_refresh_runtime_state_from_disk_if_changed", lambda: None)
    return service


_SHARED_DEFAULT_WEEK6_EXECUTION_SERVICE = _new_service(_load_test_config())


def _build_position_multiplier_service(
    max_position_multiplier: float,
) -> StockAnalyzerService:
    config = _load_test_config()
    config.holiday_risk.max_position_multiplier = max_position_multiplier
    return _new_service(config)


def _build_regulatory_degrade_service() -> StockAnalyzerService:
    config = _load_test_config()
    config.regulatory_factor.penalty_score = 12.0
    return _new_service(config)


_BASE_POSITION_MULTIPLIER_SERVICE = _build_position_multiplier_service(1.0)
_SCALED_POSITION_MULTIPLIER_SERVICE = _build_position_multiplier_service(0.5)
_REGULATORY_DEGRADE_SERVICE = _build_regulatory_degrade_service()
_SHARED_M1_NEGATIVE_CASE_SERVICE = _new_service(_load_test_config())


def test_week6_execution_regulatory_exclude_blocks_buy_signal() -> None:
    service = _SHARED_DEFAULT_WEEK6_EXECUTION_SERVICE
    _reset_shared_week6_execution_service(service)
    _seed_regulatory_watchlist(
        service,
        entries=[{"symbol": "600000", "tag": "inquiry", "note": "test"}]
    )
    controls = _as_dict(
        service._build_week6_execution_controls(
            strategy="trend",
            symbols=["600000"],
            drawdown_pct=0.0,
        )
    )
    signals = [
        PipelineSignal(
            symbol="600000",
            strategy="trend",
            score=80.0,
            grade="A",
            action="buy",
            target_position=0.10,
            probabilities={"lgbm": 0.7, "xgb": 0.7, "meta": 0.7},
            reasons=[],
        )
    ]

    summary = _as_mapping(
        service._apply_week6_execution_controls(
            signals=signals,
            strategy="trend",
            controls=controls,
        )
    )
    signal = signals[0]
    assert signal.action == "hold"
    assert float(signal.target_position) == 0.0
    assert "regulatory_exclude" in list(signal.reasons)
    assert _as_int(summary["excluded"]) >= 1


def test_week6_execution_regulatory_degrade_reduces_score() -> None:
    service = _REGULATORY_DEGRADE_SERVICE
    _reset_shared_week6_execution_service(service)
    degraded_signal = PipelineSignal(
        symbol="600000",
        strategy="trend",
        score=80.0,
        grade="A",
        action="buy",
        target_position=0.10,
        probabilities={"lgbm": 0.7, "xgb": 0.7, "meta": 0.7},
        reasons=[],
    )

    _seed_regulatory_watchlist(
        service,
        entries=[{"symbol": "600000", "tag": "manual_risk", "note": "degrade only"}]
    )
    degraded_summary = _as_mapping(
        service._apply_week6_execution_controls(
            signals=[degraded_signal],
            strategy="trend",
            controls=_as_dict(
                service._build_week6_execution_controls(
                    strategy="trend",
                    symbols=["600000"],
                    drawdown_pct=0.0,
                )
            ),
        )
    )

    assert float(degraded_signal.score) <= 68.0
    assert "regulatory_degrade" in list(degraded_signal.reasons)
    assert _as_int(degraded_summary["degraded"]) >= 1


def test_week6_execution_position_multiplier_scales_buy_position() -> None:
    base_service = _BASE_POSITION_MULTIPLIER_SERVICE
    _reset_shared_week6_execution_service(base_service)
    base_controls = _as_dict(
        base_service._build_week6_execution_controls(
            strategy="trend",
            symbols=["600000"],
            drawdown_pct=0.0,
        )
    )
    base_signals = [
        PipelineSignal(
            symbol="600000",
            strategy="trend",
            score=80.0,
            grade="A",
            action="buy",
            target_position=0.10,
            probabilities={"lgbm": 0.7, "xgb": 0.7, "meta": 0.7},
            reasons=[],
        )
    ]
    _ = base_service._apply_week6_execution_controls(
        signals=base_signals,
        strategy="trend",
        controls=base_controls,
    )
    base_target = float(base_signals[0].target_position)

    scaled_service = _SCALED_POSITION_MULTIPLIER_SERVICE
    _reset_shared_week6_execution_service(scaled_service)
    _patch_attr(
        scaled_service,
        "_global_market_snapshot",
        {
            "us_index_change_pct": -1.2,
            "a50_change_pct": -1.0,
            "usd_cnh_change_pct": 0.5,
            "commodity_change_pct": -0.3,
            "a_share_correlation": 0.70,
        },
    )
    scaled_controls = _as_dict(
        scaled_service._build_week6_execution_controls(
            strategy="trend",
            symbols=["600000"],
            drawdown_pct=0.0,
        )
    )
    scaled_signals = [
        PipelineSignal(
            symbol="600000",
            strategy="trend",
            score=80.0,
            grade="A",
            action="buy",
            target_position=0.10,
            probabilities={"lgbm": 0.7, "xgb": 0.7, "meta": 0.7},
            reasons=[],
        )
    ]
    scaled_summary = _as_mapping(
        scaled_service._apply_week6_execution_controls(
            signals=scaled_signals,
            strategy="trend",
            controls=scaled_controls,
        )
    )
    scaled_target = float(scaled_signals[0].target_position)
    assert scaled_target < base_target
    assert "week6_position_scaled" in scaled_signals[0].reasons
    assert _as_int(scaled_summary["scaled"]) >= 1


def test_week6_execution_applies_evolution_runtime_controls() -> None:
    service = _SHARED_DEFAULT_WEEK6_EXECUTION_SERVICE
    _reset_shared_week6_execution_service(service)
    baseline_controls = _as_dict(
        service._build_week6_execution_controls(
            strategy="trend",
            symbols=["600000"],
            drawdown_pct=0.0,
        )
    )

    _patch_attr(
        service,
        "_last_evolution_report",
        {
            "run_id": "evo-runtime-1",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "runtime_controls": {
                "source": "evolution",
                "source_run_id": "evo-runtime-1",
                "source_timestamp": datetime.now().isoformat(timespec="seconds"),
                "degraded_mode": False,
                "conservative_mode": True,
                "threshold_shift": 2.0,
                "position_multiplier": 0.5,
                "global_risk_delta": -20.0,
                "reasons": ["m6_heavy_sell_pressure"],
            },
        },
    )

    adjusted_controls = _as_dict(
        service._build_week6_execution_controls(
            strategy="trend",
            symbols=["600000"],
            drawdown_pct=0.0,
        )
    )
    adjusted_signals = [
        PipelineSignal(
            symbol="600000",
            strategy="trend",
            score=82.0,
            grade="A",
            action="buy",
            target_position=0.10,
            probabilities={"lgbm": 0.7, "xgb": 0.7, "meta": 0.7},
            reasons=[],
        )
    ]
    summary = _as_mapping(
        service._apply_week6_execution_controls(
            signals=adjusted_signals,
            strategy="trend",
            controls=adjusted_controls,
        )
    )
    evolution = _as_mapping(adjusted_controls["evolution"])
    position_multiplier_components = _as_mapping(
        adjusted_controls["position_multiplier_components"]
    )

    assert _as_float(adjusted_controls["position_multiplier"]) < _as_float(
        baseline_controls["position_multiplier"]
    )
    assert evolution["source"] == "evolution"
    assert evolution["source_run_id"] == "evo-runtime-1"
    assert _as_bool(summary["evolution_applied"]) is True
    assert _as_bool(summary["evolution_conservative_mode"]) is True
    assert "m6_heavy_sell_pressure" in _as_text_list(summary["evolution_reasons"])
    assert _as_float(position_multiplier_components["evolution"]) == 0.5
    assert _as_float(adjusted_controls["global_risk_score"]) <= 40.0


def test_run_pipeline_applies_m1_negative_case_constraints() -> None:
    service = _SHARED_M1_NEGATIVE_CASE_SERVICE
    _reset_shared_week6_execution_service(service)

    class _NoopProvider:
        def fetch_daily_bars(self, *, symbol: str, lookback_days: int) -> object:
            return None

    original_resolve_m1_negative_case_library = service._resolve_m1_negative_case_library
    original_select_provider = service._select_provider
    _patch_attr(
        service,
        "_resolve_m1_negative_case_library",
        lambda: {
            "available": True,
            "shared_payload_uri": "",
            "negative_case_count": 1,
            "reason_counts": {"data_incomplete": 1},
            "cases": [
                {
                    "symbol": "600000",
                    "bucket": "severe",
                    "reason_codes": ["data_incomplete"],
                    "realized_return": -0.12,
                }
            ],
        },
    )
    _patch_attr(
        service,
        "_select_provider",
        lambda *, use_live_runtime: cast(Any, _NoopProvider()),
    )
    signal = PipelineSignal(
        symbol="600000",
        strategy="trend",
        score=82.0,
        grade="A",
        action="buy",
        target_position=0.10,
        probabilities={"lgbm": 0.7, "xgb": 0.7, "meta": 0.7},
        reasons=["predictor_reason:missing_feature"],
    )

    try:
        m1_negative_case = _as_mapping(
            service._apply_m1_negative_case_constraints(
                signals=[signal],
                strategy="trend",
                use_live_runtime=False,
            )
        )
    finally:
        _patch_attr(
            service,
            "_resolve_m1_negative_case_library",
            original_resolve_m1_negative_case_library,
        )
        _patch_attr(service, "_select_provider", original_select_provider)

    assert _as_bool(m1_negative_case["available"]) is True
    assert _as_int(m1_negative_case["penalized"]) >= 1
    reasons = list(signal.reasons)
    assert any(
        str(reason).startswith("m1_negative_case_constraint:") for reason in reasons
    )
    assert any(
        str(reason).startswith("m1_negative_case_similarity:") for reason in reasons
    )
    runtime_feedback = _as_mapping(signal.decision_trace["runtime_feedback"])
    m1_feedback = _as_mapping(runtime_feedback["m1"])
    assert m1_feedback["m1_negative_case_bucket"] == "severe"
    assert _as_float(m1_feedback["m1_negative_case_similarity"]) > 0.0
