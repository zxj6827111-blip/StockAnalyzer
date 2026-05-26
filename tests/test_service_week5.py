from __future__ import annotations

import tempfile
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
from pytest import fixture

from stock_analyzer.command.channel import CommandEnvelope, SignedCommandProcessor
from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.learning.sample_schema import SignalSnapshot
from stock_analyzer.models.calibration import IsotonicCalibrator
from stock_analyzer.models.execution_risk_artifact import ExecutionRiskArtifact
from stock_analyzer.models.fallback import LogisticProbModel
from stock_analyzer.runtime import service as runtime_service_module
from stock_analyzer.runtime.service import StockAnalyzerService


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    raise AssertionError(f"Expected mapping, got {type(value).__name__}")


def _as_mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        raise AssertionError(f"Expected list, got {type(value).__name__}")
    return [cast(Mapping[str, object], item) for item in value if isinstance(item, Mapping)]


def _as_int(value: object) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    raise AssertionError(f"Expected numeric value, got {value!r}")


def _as_text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        raise AssertionError(f"Expected list, got {type(value).__name__}")
    return [str(item) for item in value]


def _patch_attr(target: object, name: str, value: object) -> None:
    setattr(cast(Any, target), name, value)


class RecordingSyntheticProvider:
    def __init__(self, seed_offset: int = 0) -> None:
        self._delegate = SyntheticProvider(seed_offset=seed_offset)
        self.lookback_requests: list[tuple[str, int]] = []
        self._daily_cache: dict[tuple[str, int], pd.DataFrame] = {}
        self._intraday_cache: dict[tuple[str, str, int], pd.DataFrame] = {}

    def fetch_daily_bars(self, symbol: str, lookback_days: int = 120) -> pd.DataFrame:
        self.lookback_requests.append((symbol, lookback_days))
        cache_key = (symbol, lookback_days)
        frame = self._daily_cache.get(cache_key)
        if frame is None:
            frame = self._delegate.fetch_daily_bars(symbol=symbol, lookback_days=lookback_days)
            self._daily_cache[cache_key] = frame
        assert isinstance(frame, pd.DataFrame)
        return frame.copy()

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> pd.DataFrame:
        cache_key = (symbol, interval, lookback_days)
        frame = self._intraday_cache.get(cache_key)
        if frame is None:
            frame = self._delegate.fetch_intraday_summary(
                symbol=symbol,
                interval=interval,
                lookback_days=lookback_days,
            )
            self._intraday_cache[cache_key] = frame
        assert isinstance(frame, pd.DataFrame)
        return frame.copy()


class RecordingDepthProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bool]] = []

    def fetch_snapshots(
        self,
        symbols: list[str],
        *,
        force_refresh: bool = False,
    ) -> dict[str, dict[str, object]]:
        self.calls.append((list(symbols), force_refresh))
        return {
            symbol: {
                "symbol": symbol,
                "name": f"标的{symbol}",
                "available": True,
                "source": "easyquotation_sina",
                "timestamp": "2026-03-10 10:18:00",
                "spread": 0.01,
                "spread_pct": 0.0009,
                "imbalance": 0.12,
                "bid_total_volume": 5000.0,
                "ask_total_volume": 4500.0,
                "bid_levels": [
                    {"level": 1, "price": 10.01, "volume": 1200.0},
                    {"level": 2, "price": 10.00, "volume": 1100.0},
                ],
                "ask_levels": [
                    {"level": 1, "price": 10.02, "volume": 1300.0},
                    {"level": 2, "price": 10.03, "volume": 1400.0},
                ],
            }
            for symbol in symbols
        }

    def status(self) -> dict[str, object]:
        return {"provider": "recording_depth"}


def _load_test_config() -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.models.cross_review.p_lgbm_min = 0.0
    config.models.cross_review.p_xgb_min = 0.0
    config.models.cross_review.p_meta_min = 0.0
    config.models.cross_review.max_diff = 1.0
    config.liquidity_filter_monster.min_daily_turnover = 0.0
    config.liquidity_filter_monster.min_float_market_cap = 0.0
    config.liquidity_filter_monster.max_turnover_rate = 1.0
    config.command_channel.secret_key = "test-secret"
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.week5.auto_notify = False
    config.week5.first_board_windows = ["09:30-09:31"]
    config.training.artifact_path = str(root / "artifacts" / "nonexistent_test_model.json")
    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = False
    config.training.bootstrap_auto_seed_watchlist = False
    temp_root = Path(tempfile.gettempdir()) / "stock_analyzer_tests"
    config.training.bootstrap_state_path = str(temp_root / "test_bootstrap_state_week5.json")
    return config


def _sign(
    action: str,
    command_id: str,
    payload: dict[str, object],
    secret: str,
) -> CommandEnvelope:
    ts = int(time.time())
    signature = SignedCommandProcessor.build_signature(
        secret_key=secret,
        command_id=command_id,
        timestamp=ts,
        action=action,
        payload=payload,
    )
    return CommandEnvelope(
        command_id=command_id,
        timestamp=ts,
        action=action,
        payload=payload,
        signature=signature,
    )


def _new_service(
    config: StockAnalyzerConfig,
    provider: object | None = None,
) -> StockAnalyzerService:
    runtime_provider = provider or RecordingSyntheticProvider(seed_offset=2027)
    original_build_runtime_provider = runtime_service_module.build_runtime_provider
    original_build_realtime_runtime_provider = (
        runtime_service_module.build_realtime_runtime_provider
    )
    original_build_market_depth_provider = runtime_service_module.build_market_depth_provider
    try:
        runtime_service_module.build_runtime_provider = (
            lambda config, synthetic_seed=2026: runtime_provider
        )
        runtime_service_module.build_realtime_runtime_provider = (
            lambda config, synthetic_seed=2026, timezone="Asia/Shanghai": runtime_provider
        )
        runtime_service_module.build_market_depth_provider = lambda config: None
        service = StockAnalyzerService(config=config)
    finally:
        runtime_service_module.build_runtime_provider = original_build_runtime_provider
        runtime_service_module.build_realtime_runtime_provider = (
            original_build_realtime_runtime_provider
        )
        runtime_service_module.build_market_depth_provider = original_build_market_depth_provider
    _patch_attr(service, "_provider", runtime_provider)
    _patch_attr(service._pipeline, "_provider", runtime_provider)
    _patch_attr(service, "_realtime_provider", runtime_provider)
    if service._realtime_pipeline is not None:
        _patch_attr(service._realtime_pipeline, "_provider", runtime_provider)
    _patch_attr(service, "_record_audit_event", lambda *args, **kwargs: None)
    _patch_attr(service, "_refresh_runtime_state_from_disk_if_changed", lambda: None)
    return service


def _build_test_execution_risk_artifact(path: Path) -> Path:
    feature_names = [
        "liquidity_score",
        "volatility_score",
        "model_output__meta",
        "model_output__p_meta",
        "risk__degraded_mode",
        "meta__data_quality_score",
        "meta__sample_weight",
        "meta__decision_weekday",
        "meta__decision_month",
        "meta__decision_hour",
    ]
    artifact = ExecutionRiskArtifact.create(
        dataset_id="execution_risk_dataset_week5_test",
        feature_names=feature_names,
        target_models={
            "can_fill": {
                "model": _build_logistic_model(
                    weights=[4.6, -3.8, 1.2, 1.2, -1.4, 0.8, 0.1, 0.0, 0.0, 0.0],
                    bias=-0.2,
                ),
                "calibrator": _build_identity_calibrator(),
            },
            "likely_slippage_high": {
                "model": _build_logistic_model(
                    weights=[-2.9, 4.9, -1.1, -1.1, 1.6, -0.4, 0.0, 0.0, 0.0, 0.0],
                    bias=-0.9,
                ),
                "calibrator": _build_identity_calibrator(),
            },
            "sim_broker_divergence_risk": {
                "model": _build_logistic_model(
                    weights=[-2.1, 4.2, -0.7, -0.7, 2.2, -0.6, 0.0, 0.0, 0.0, 0.0],
                    bias=-1.1,
                ),
                "calibrator": _build_identity_calibrator(),
            },
        },
        metadata={"test_artifact": True},
    )
    artifact.save(path)
    return path


def _build_logistic_model(*, weights: list[float], bias: float) -> dict[str, object]:
    model = LogisticProbModel(learning_rate=0.05, epochs=16, l2=0.0, seed=7)
    model.weights = np.asarray(weights, dtype=float)
    model.bias = float(bias)
    return model.to_dict()


def _build_identity_calibrator() -> dict[str, object]:
    calibrator = IsotonicCalibrator()
    calibrator.fit(
        np.asarray([0.0, 0.5, 1.0], dtype=float),
        np.asarray([0.0, 0.5, 1.0], dtype=float),
    )
    return calibrator.to_dict()


def _write_week5_execution_snapshot(
    service: StockAnalyzerService,
    *,
    snapshot_id: str,
    symbol: str,
    decision_time: datetime,
    liquidity_score: float,
    volatility_score: float,
    meta_probability: float,
    degraded_mode: bool,
    data_quality_score: float = 0.95,
) -> None:
    service._sample_store.write_snapshot(  # noqa: SLF001
        SignalSnapshot(
            snapshot_id=snapshot_id,
            code_version="git:test",
            symbol=symbol,
            strategy="monster",
            decision_time=decision_time,
            feature_vector={
                "liquidity_score": liquidity_score,
                "volatility_score": volatility_score,
            },
            feature_schema_id="feature_schema_week5_exec_v1",
            feature_schema_hash="feature_schema_week5_exec_hash_v1",
            model_outputs={"meta": meta_probability},
            risk_context={"degraded_mode": degraded_mode},
            runtime_config_hash="runtime_hash_week5_exec_v1",
            label_policy_id="label_policy_week5_exec_v1",
            label_policy_hash="label_policy_week5_exec_hash_v1",
            data_quality_score=data_quality_score,
            sample_weight=1.0,
        )
    )


def _build_week5_execution_rerank_pipeline() -> object:
    def _fake_run_pipeline(**kwargs: object) -> dict[str, object]:
        _ = kwargs
        return {
            "trace_id": "week5-execution-rerank",
            "signals": [
                {
                    "symbol": "600000",
                    "score": 92.0,
                    "leader_score": 92.5,
                    "action": "buy",
                    "suggested_position": 0.08,
                    "target_position": 0.08,
                    "grade": "A",
                    "reasons": ["high_score"],
                    "probabilities": {"meta": 0.46, "lgbm": 0.45, "xgb": 0.47},
                    "decision_trace": {
                        "learning_protocol": {"snapshot_id": "snap-high-risk"},
                        "risk_gate": {"passed": True},
                        "liquidity_gate": {"passed": True},
                        "cross_review_gate": {"passed": True},
                        "financial_gate": {"allowed": True},
                    },
                },
                {
                    "symbol": "000001",
                    "score": 79.0,
                    "leader_score": 79.5,
                    "action": "buy",
                    "suggested_position": 0.08,
                    "target_position": 0.08,
                    "grade": "A",
                    "reasons": ["high_score"],
                    "probabilities": {"meta": 0.73, "lgbm": 0.71, "xgb": 0.72},
                    "decision_trace": {
                        "learning_protocol": {"snapshot_id": "snap-low-risk"},
                        "risk_gate": {"passed": True},
                        "liquidity_gate": {"passed": True},
                        "cross_review_gate": {"passed": True},
                        "financial_gate": {"allowed": True},
                    },
                },
            ],
            "risk": {
                "action": "monitor",
                "drawdown_pct": 0.0,
            },
        }

    return _fake_run_pipeline


def _build_lightweight_prefilter_scan(
    service: StockAnalyzerService,
    *,
    provider: RecordingSyntheticProvider,
    universe_symbols: list[str],
    shortlisted_symbols: list[str],
) -> object:
    def _fake_run_week5_scan(
        symbols: list[str] | None = None,
        timestamp: datetime | None = None,
        notify_enabled: bool | None = None,
        sync_watchlist: bool | None = None,
        sync_reason: str = "",
        sync_top_k_override: int | None = None,
        force_universe_scan: bool = False,
        prefilter_enabled_override: bool | None = None,
        prefilter_top_k_override: int | None = None,
        universe_max_symbols_override: int | None = None,
        pinned_symbols: list[str] | None = None,
        scan_profile: str = "",
    ) -> dict[str, object]:
        _ = (
            symbols,
            timestamp,
            notify_enabled,
            sync_reason,
            force_universe_scan,
            prefilter_enabled_override,
            prefilter_top_k_override,
            universe_max_symbols_override,
            pinned_symbols,
        )
        effective_shortlisted_symbols = (
            shortlisted_symbols[: max(1, int(prefilter_top_k_override))]
            if prefilter_top_k_override is not None and prefilter_top_k_override > 0
            else shortlisted_symbols
        )
        prefilter_lookback = max(2, int(service._config.week5.universe_prefilter_lookback_days))
        signal_lookback = max(
            2,
            int(service._config.evolution.universe_spec.signal_fetch_lookback_days),
        )
        for symbol in universe_symbols:
            provider.fetch_daily_bars(symbol=symbol, lookback_days=prefilter_lookback)
        for symbol in effective_shortlisted_symbols:
            provider.fetch_daily_bars(symbol=symbol, lookback_days=signal_lookback)
        if sync_watchlist:
            top_k = (
                sync_top_k_override
                if sync_top_k_override is not None
                else int(service._config.week5.auto_sync_watchlist_top_k)
            )
            service.state.watchlist = effective_shortlisted_symbols[: max(0, top_k)]
        return {
            "summary": {
                "prefilter_applied": True,
                "prefilter_shortlisted": len(effective_shortlisted_symbols),
            },
            "first_board": {"candidate_count": 0, "candidates": [], "leaders": []},
            "anomalies": {"event_count": 0, "events": []},
            "empty_signal": {"triggered": False, "reasons": []},
            "monster_isolation": {"can_open_new_position": True, "reasons": []},
            "runtime_source": {"mode": "realtime_overlay"},
            "scan_profile": scan_profile.strip() or "default",
            "watchlist_size": len(effective_shortlisted_symbols),
            "signal_pool": {
                "candidate_count": len(effective_shortlisted_symbols),
                "candidates": [
                    {
                        "symbol": symbol,
                        "shortlist_score": float(100 - index),
                        "shortlist_components": {"baseline_score": float(90 - index)},
                    }
                    for index, symbol in enumerate(effective_shortlisted_symbols, start=1)
                ],
                "ranking": {
                    "mode": "two_stage_funnel",
                    "score_key": "shortlist_score",
                    "shortlist_top_n": int(
                        service._config.week5.universe_prefilter_shortlist_top_n
                    ),
                },
            },
            "prefilter": {
                "enabled": True,
                "applied": True,
                "reason": "force_universe_scan",
                "lookback_days": prefilter_lookback,
                "universe_source": "local_files_primary",
                "universe_count": len(universe_symbols),
                "eligible_count": len(universe_symbols),
                "top_k": (
                    int(prefilter_top_k_override)
                    if prefilter_top_k_override is not None and prefilter_top_k_override > 0
                    else len(effective_shortlisted_symbols)
                ),
                "selected_count": len(effective_shortlisted_symbols),
                "shortlisted_count": len(effective_shortlisted_symbols),
                "scoring_mode": "two_stage_funnel",
                "stages": {
                    "stage1": {"applied": True, "score_key": "baseline_score"},
                    "stage2": {
                        "status": "completed",
                        "shortlist_top_n": int(
                            service._config.week5.universe_prefilter_shortlist_top_n
                        ),
                    },
                },
                "shortlisted": [
                    {
                        "symbol": symbol,
                        "stage1": {"score_key": "baseline_score"},
                    }
                    for symbol in effective_shortlisted_symbols
                ],
            },
            "watchlist_sync": {
                "enabled": bool(sync_watchlist),
                "updated": bool(sync_watchlist),
                "reason": "lightweight_stub",
                "watchlist_before": 0,
                "watchlist_after": len(service.state.watchlist),
                "symbols": list(service.state.watchlist),
            },
        }

    return _fake_run_week5_scan


def _build_lightweight_full_deep_scan(
    service: StockAnalyzerService,
    *,
    provider: RecordingSyntheticProvider,
    universe_symbols: list[str],
) -> object:
    def _fake_run_week5_scan(
        symbols: list[str] | None = None,
        timestamp: datetime | None = None,
        notify_enabled: bool | None = None,
        sync_watchlist: bool | None = None,
        sync_reason: str = "",
        sync_top_k_override: int | None = None,
        force_universe_scan: bool = False,
        prefilter_enabled_override: bool | None = None,
        prefilter_top_k_override: int | None = None,
        universe_max_symbols_override: int | None = None,
        pinned_symbols: list[str] | None = None,
        scan_profile: str = "",
    ) -> dict[str, object]:
        _ = (
            symbols,
            timestamp,
            notify_enabled,
            sync_reason,
            sync_top_k_override,
            force_universe_scan,
            prefilter_enabled_override,
            prefilter_top_k_override,
            universe_max_symbols_override,
            pinned_symbols,
        )
        signal_lookback = max(
            2,
            int(service._config.evolution.universe_spec.signal_fetch_lookback_days),
        )
        for symbol in universe_symbols:
            provider.fetch_daily_bars(symbol=symbol, lookback_days=signal_lookback)
        return {
            "summary": {"prefilter_applied": False, "prefilter_shortlisted": len(universe_symbols)},
            "first_board": {"candidate_count": 0, "candidates": [], "leaders": []},
            "anomalies": {"event_count": 0, "events": []},
            "empty_signal": {"triggered": False, "reasons": []},
            "monster_isolation": {"can_open_new_position": True, "reasons": []},
            "runtime_source": {"mode": "realtime_overlay"},
            "scan_profile": scan_profile.strip() or "default",
            "watchlist_size": len(universe_symbols),
            "signal_pool": {
                "candidate_count": len(universe_symbols),
                "candidates": [],
                "ranking": {"mode": "two_stage_funnel", "score_key": "shortlist_score"},
            },
            "prefilter": {
                "enabled": False,
                "applied": False,
                "reason": "disabled_by_offhours_full_deep_profile",
            },
            "watchlist_sync": {
                "enabled": bool(sync_watchlist),
                "updated": False,
                "reason": "lightweight_stub",
                "watchlist_before": len(service.state.watchlist),
                "watchlist_after": len(service.state.watchlist),
                "symbols": list(service.state.watchlist),
            },
        }

    return _fake_run_week5_scan


def _seed_lightweight_week5_pipeline(service: StockAnalyzerService) -> None:
    _patch_attr(service, "run_pipeline", _build_lightweight_week5_pipeline(service))


def _build_lightweight_week5_pipeline(
    service: StockAnalyzerService,
    *,
    provider: RecordingSyntheticProvider | None = None,
    emitted_symbols: list[str] | None = None,
) -> object:
    def _fake_run_pipeline(
        *,
        symbols: list[str] | None = None,
        strategy: str = "trend",
        current_equity: float | None = None,
        use_live_runtime: bool = False,
        **kwargs: object,
    ) -> dict[str, object]:
        _ = strategy, use_live_runtime, kwargs
        symbol_list = [
            str(item).strip()
            for item in (emitted_symbols if emitted_symbols is not None else (symbols or []))
            if str(item).strip()
        ]
        if not symbol_list:
            symbol_list = ["600000", "000001"]

        if provider is not None:
            signal_lookback = max(
                2,
                int(service._config.evolution.universe_spec.signal_fetch_lookback_days),
            )
            for symbol in symbol_list:
                provider.fetch_daily_bars(symbol=symbol, lookback_days=signal_lookback)

        effective_equity = (
            float(current_equity)
            if isinstance(current_equity, (int, float))
            else float(service.state.current_equity)
        )
        drawdown_pct = max(0.0, round((1.0 - effective_equity) * 100.0, 4))
        signals = [
            {
                "symbol": symbol,
                "score": 80.0 - index * 8.0,
                "leader_score": 81.0 - index * 8.0,
                "action": "buy" if index == 0 else "watch",
                "suggested_position": 0.08 if index == 0 else 0.04,
                "target_position": 0.08 if index == 0 else 0.04,
                "grade": "A" if index == 0 else "B",
                "reasons": ["high_score"] if index == 0 else ["watch_signal"],
            }
            for index, symbol in enumerate(symbol_list[:2])
        ]
        return {
            "trace_id": "test-week5-lightweight",
            "signals": signals,
            "risk": {
                "action": "monitor",
                "drawdown_pct": drawdown_pct,
            },
        }

    return _fake_run_pipeline


def _seed_lightweight_week5_pipeline_with_provider(
    service: StockAnalyzerService,
    provider: RecordingSyntheticProvider,
    *,
    emitted_symbols: list[str] | None = None,
) -> None:
    _patch_attr(
        service,
        "run_pipeline",
        _build_lightweight_week5_pipeline(
            service,
            provider=provider,
            emitted_symbols=emitted_symbols,
        ),
    )


def test_week5_scan_caps_intraday_monster_scan_symbols() -> None:
    config = _load_test_config()
    config.week5.monster_scan_intraday_max_symbols = 3
    provider = RecordingSyntheticProvider(seed_offset=2027)
    service = _new_service(config, provider=provider)
    _seed_lightweight_week5_pipeline_with_provider(service, provider)

    report = _as_mapping(
        service.run_week5_scan(
            symbols=["600000", "000001", "600519", "300750", "002594"],
            timestamp=datetime(2026, 3, 16, 9, 30),
            notify_enabled=False,
            sync_reason="scheduler_week5",
        )
    )
    controls = _as_mapping(report["monster_scan_controls"])
    prefilter = _as_mapping(report["prefilter"])
    summary = _as_mapping(report["summary"])

    assert _as_int(report["watchlist_size"]) == 3
    assert controls["cap_applied"] is True
    assert _as_int(controls["input_count"]) == 5
    assert _as_int(controls["selected_count"]) == 3
    assert _as_int(controls["dropped_count"]) == 2
    assert _as_int(prefilter["selected_count"]) == 3
    assert summary["monster_scan_cap_applied"] is True


def _reset_shared_week5_service(service: StockAnalyzerService) -> None:
    service.state.watchlist = []
    service.state.current_equity = 1.0
    service.state.pause_new_buy = False
    service.state.reconcile_required = False
    service._last_week5_scan_report = None  # noqa: SLF001
    service._week5_scan_history.clear()  # noqa: SLF001
    service._run_summaries.clear()  # noqa: SLF001
    service._latency_history_ms.clear()  # noqa: SLF001
    service._portfolio.restore_state(None)  # noqa: SLF001


_SHARED_DEFAULT_WEEK5_SERVICE = _new_service(_load_test_config())


def _build_shared_week5_signal_pool_live_service() -> StockAnalyzerService:
    config = _load_test_config()
    service = _new_service(config)
    depth_provider = RecordingDepthProvider()
    _patch_attr(service, "test_depth_provider", depth_provider)
    _patch_attr(service, "_market_depth_provider", depth_provider)
    _patch_attr(
        service,
        "_build_week5_signal_pool_live_item",
        lambda *,
        symbol,
        candidate,
        force_refresh,
        prefer_online,
        depth_snapshot=None: {
            "symbol": symbol,
            "name": str((depth_snapshot or {}).get("name", "")),
            "score": float(candidate.get("score", 0.0)),
            "leader_score": float(candidate.get("leader_score", 0.0)),
            "action": str(candidate.get("action", "")),
            "suggested_position": float(candidate.get("suggested_position", 0.0)),
            "reasons": list(candidate.get("reasons", [])),
            "trend_source": "daily",
            "depth_available": bool((depth_snapshot or {}).get("available", False)),
            "depth_source": str((depth_snapshot or {}).get("source", "")),
            "depth_timestamp": str((depth_snapshot or {}).get("timestamp", "")),
            "bid_levels": list((depth_snapshot or {}).get("bid_levels", [])),
            "ask_levels": list((depth_snapshot or {}).get("ask_levels", [])),
            "spread": float((depth_snapshot or {}).get("spread", 0.0)),
            "spread_pct": float((depth_snapshot or {}).get("spread_pct", 0.0)),
            "order_imbalance": float((depth_snapshot or {}).get("imbalance", 0.0)),
            "bid_total_volume": float((depth_snapshot or {}).get("bid_total_volume", 0.0)),
            "ask_total_volume": float((depth_snapshot or {}).get("ask_total_volume", 0.0)),
        },
    )
    return service


def _build_shared_week5_drawdown_service() -> StockAnalyzerService:
    config = _load_test_config()
    provider = RecordingSyntheticProvider(seed_offset=2027)
    service = _new_service(config, provider=provider)
    _patch_attr(service, "test_provider", provider)
    _seed_lightweight_week5_pipeline_with_provider(
        service,
        provider,
        emitted_symbols=["600000"],
    )
    return service


def _build_shared_week5_monster_limit_service() -> StockAnalyzerService:
    config = _load_test_config()
    config.monster_risk.max_total_position = 0.05
    provider = RecordingSyntheticProvider(seed_offset=2027)
    service = _new_service(config, provider=provider)
    _patch_attr(service, "test_provider", provider)
    _patch_attr(service, "test_config", config)
    _seed_lightweight_week5_pipeline_with_provider(
        service,
        provider,
        emitted_symbols=["600000"],
    )
    return service


def _build_shared_week5_lookback_service() -> StockAnalyzerService:
    config = _load_test_config()
    provider = RecordingSyntheticProvider(seed_offset=2027)
    service = _new_service(config, provider=provider)
    _patch_attr(service, "test_provider", provider)
    _patch_attr(service, "test_config", config)
    _seed_lightweight_week5_pipeline_with_provider(
        service,
        provider,
        emitted_symbols=["600000"],
    )
    return service


def _build_shared_week5_prefilter_service() -> StockAnalyzerService:
    config = _load_test_config()
    config.week5.universe_prefilter_lookback_days = 240
    config.week5.universe_prefilter_top_k = 3
    config.week5.auto_sync_watchlist_top_k = 2
    provider = RecordingSyntheticProvider(seed_offset=2027)
    service = _new_service(config, provider=provider)
    _patch_attr(service, "test_provider", provider)
    _patch_attr(service, "test_config", config)
    _patch_attr(service, "_resolve_symbol_universe", lambda **_: {
        "source": "local_files_primary",
        "symbols": ["600000", "000001", "600519", "300750", "002594", "601318"],
        "count": 6,
        "errors": [],
    })
    _patch_attr(
        service,
        "run_week5_scan",
        _build_lightweight_prefilter_scan(
            service,
            provider=provider,
            universe_symbols=["600000", "000001", "600519", "300750", "002594", "601318"],
            shortlisted_symbols=["600000", "000001", "600519"],
        ),
    )
    return service


def _reset_shared_week5_signal_pool_live_service(service: StockAnalyzerService) -> None:
    _reset_shared_week5_service(service)
    depth_provider = getattr(service, "test_depth_provider", None)
    if isinstance(depth_provider, RecordingDepthProvider):
        depth_provider.calls.clear()
    _patch_attr(service, "_last_week5_scan_report", {
        "timestamp": "2026-03-10T10:18:00",
        "signal_pool": {
            "candidate_count": 2,
            "candidates": [
                {
                    "symbol": "600000",
                    "score": 80.0,
                    "leader_score": 81.0,
                    "action": "buy",
                    "suggested_position": 0.08,
                    "reasons": ["high_score"],
                },
                {
                    "symbol": "000001",
                    "score": 72.0,
                    "leader_score": 73.0,
                    "action": "watch",
                    "suggested_position": 0.04,
                    "reasons": ["watch_signal"],
                },
            ],
        },
    })


@fixture(scope="module")
def shared_default_week5_service() -> StockAnalyzerService:
    return _SHARED_DEFAULT_WEEK5_SERVICE


def _build_shared_weekday_offhours_service() -> StockAnalyzerService:
    config = _load_test_config()
    config.week5.universe_prefilter_lookback_days = 240
    config.week5.universe_prefilter_top_k = 3
    config.week5.offhours_research_pool_top_k = 3
    config.week5.auto_sync_watchlist_top_k = 2
    config.week5.offhours_watchlist_sync_top_k = 2
    config.week5.offhours_force_full_deep_scan_on_watchlist_below = 0
    config.week5.offhours_force_full_deep_scan_on_no_buy_streak = 0
    config.week5.offhours_force_full_deep_scan_on_drawdown_pct = 0.0
    provider = RecordingSyntheticProvider(seed_offset=2027)
    service = _new_service(config, provider=provider)
    _patch_attr(service, "test_provider", provider)
    _patch_attr(service, "test_config", config)
    _patch_attr(service, "_resolve_symbol_universe", lambda **_: {
        "source": "local_files_primary",
        "symbols": ["600000", "000001", "600519", "300750", "002594", "601318"],
        "count": 6,
        "errors": [],
    })
    _patch_attr(
        service,
        "run_week5_scan",
        _build_lightweight_prefilter_scan(
            service,
            provider=provider,
            universe_symbols=["600000", "000001", "600519", "300750", "002594", "601318"],
            shortlisted_symbols=["600000", "000001", "600519"],
        ),
    )
    return service


def _build_shared_weekend_offhours_service() -> StockAnalyzerService:
    config = _load_test_config()
    config.week5.auto_sync_watchlist_top_k = 2
    config.week5.offhours_watchlist_sync_top_k = 2
    config.week5.offhours_friday_full_deep_scan_enabled = True
    config.week5.offhours_weekend_full_deep_scan_enabled = True
    config.week5.offhours_force_full_deep_scan_on_watchlist_below = 0
    config.week5.offhours_force_full_deep_scan_on_no_buy_streak = 0
    config.week5.offhours_force_full_deep_scan_on_drawdown_pct = 0.0
    provider = RecordingSyntheticProvider(seed_offset=2027)
    service = _new_service(config, provider=provider)
    _patch_attr(service, "test_provider", provider)
    _patch_attr(service, "test_config", config)
    _patch_attr(service, "_resolve_symbol_universe", lambda **_: {
        "source": "local_files_primary",
        "symbols": ["600000", "000001", "600519", "300750"],
        "count": 4,
        "errors": [],
    })
    _patch_attr(
        service,
        "run_week5_scan",
        _build_lightweight_full_deep_scan(
            service,
            provider=provider,
            universe_symbols=["600000", "000001", "600519", "300750"],
        ),
    )
    return service


def _build_shared_forced_full_deep_offhours_service() -> StockAnalyzerService:
    config = _load_test_config()
    config.week5.auto_sync_watchlist_top_k = 2
    config.week5.offhours_watchlist_sync_top_k = 2
    config.week5.offhours_weekend_full_deep_scan_enabled = False
    config.week5.offhours_force_full_deep_scan_on_watchlist_below = 5
    config.week5.offhours_force_full_deep_scan_on_no_buy_streak = 0
    config.week5.offhours_force_full_deep_scan_on_drawdown_pct = 0.0
    provider = RecordingSyntheticProvider(seed_offset=2027)
    service = _new_service(config, provider=provider)
    _patch_attr(service, "test_provider", provider)
    _patch_attr(service, "test_config", config)
    _patch_attr(service, "_resolve_symbol_universe", lambda **_: {
        "source": "local_files_primary",
        "symbols": ["600000", "000001", "600519", "300750"],
        "count": 4,
        "errors": [],
    })
    _patch_attr(
        service,
        "run_week5_scan",
        _build_lightweight_full_deep_scan(
            service,
            provider=provider,
            universe_symbols=["600000", "000001", "600519", "300750"],
        ),
    )
    return service


def _reset_shared_week5_offhours_service(
    service: StockAnalyzerService,
    *,
    watchlist: list[str] | None = None,
) -> None:
    _reset_shared_week5_service(service)
    provider = getattr(service, "test_provider", None)
    if isinstance(provider, RecordingSyntheticProvider):
        provider.lookback_requests.clear()
    if watchlist is not None:
        service.state.watchlist = list(watchlist)


def _reset_shared_week5_pipeline_service(
    service: StockAnalyzerService,
    *,
    watchlist: list[str] | None = None,
    current_equity: float = 1.0,
) -> None:
    _reset_shared_week5_service(service)
    provider = getattr(service, "test_provider", None)
    if isinstance(provider, RecordingSyntheticProvider):
        provider.lookback_requests.clear()
    service.state.current_equity = current_equity
    if watchlist is not None:
        service.state.watchlist = list(watchlist)


_SHARED_WEEKDAY_OFFHOURS_SERVICE = _build_shared_weekday_offhours_service()
_SHARED_FRIDAY_OFFHOURS_SERVICE = _build_shared_weekend_offhours_service()
_SHARED_WEEKEND_OFFHOURS_SERVICE = _build_shared_weekend_offhours_service()
_SHARED_FORCED_FULL_DEEP_OFFHOURS_SERVICE = _build_shared_forced_full_deep_offhours_service()
_SHARED_WEEK5_SIGNAL_POOL_LIVE_SERVICE = _build_shared_week5_signal_pool_live_service()
_SHARED_WEEK5_DRAWDOWN_SERVICE = _build_shared_week5_drawdown_service()
_SHARED_WEEK5_MONSTER_LIMIT_SERVICE = _build_shared_week5_monster_limit_service()
_SHARED_WEEK5_LOOKBACK_SERVICE = _build_shared_week5_lookback_service()
_SHARED_WEEK5_PREFILTER_SERVICE = _build_shared_week5_prefilter_service()


def test_service_week5_scan_generates_report_and_history(
    shared_default_week5_service: StockAnalyzerService,
) -> None:
    service = shared_default_week5_service
    _reset_shared_week5_service(service)

    report = _as_mapping(service.run_week5_scan(symbols=["600000", "000001"], notify_enabled=False))
    assert "summary" in report
    assert "first_board" in report
    assert "anomalies" in report
    assert "empty_signal" in report
    assert "monster_isolation" in report
    assert _as_mapping(report["runtime_source"])["mode"] == "realtime_overlay"
    signal_pool = _as_mapping(report["signal_pool"])
    assert _as_mapping(signal_pool["ranking"])["score_key"] == "shortlist_score"
    signal_candidates = _as_mapping_list(signal_pool["candidates"])
    if signal_candidates:
        assert "shortlist_score" in signal_candidates[0]
        assert "shortlist_components" in signal_candidates[0]

    latest = service.latest_week5_scan_report()
    assert latest is not None
    history = _as_mapping(service.week5_scan_history(limit=10))
    assert _as_int(history["records"]) >= 1


def test_service_week5_scan_applies_execution_aware_reranker_when_artifact_available(
    tmp_path: Path,
) -> None:
    config = _load_test_config()
    config.training.bootstrap_state_path = str(tmp_path / "bootstrap_state.json")
    config.week5.auto_sync_watchlist = True
    config.week5.auto_sync_watchlist_top_k = 2
    config.week5.auto_sync_watchlist_min_score = 0.0
    service = _new_service(config)

    artifact_path = _build_test_execution_risk_artifact(tmp_path / "execution_risk_artifact.json")
    service._last_execution_risk_training = {  # noqa: SLF001
        "artifact_path": str(artifact_path),
        "dataset_id": "execution_risk_dataset_week5_test",
    }
    _write_week5_execution_snapshot(
        service,
        snapshot_id="snap-high-risk",
        symbol="600000",
        decision_time=datetime(2026, 3, 20, 14, 30, tzinfo=UTC),
        liquidity_score=0.12,
        volatility_score=0.91,
        meta_probability=0.46,
        degraded_mode=True,
        data_quality_score=0.84,
    )
    _write_week5_execution_snapshot(
        service,
        snapshot_id="snap-low-risk",
        symbol="000001",
        decision_time=datetime(2026, 3, 20, 14, 31, tzinfo=UTC),
        liquidity_score=0.94,
        volatility_score=0.12,
        meta_probability=0.73,
        degraded_mode=False,
        data_quality_score=0.98,
    )

    _patch_attr(service, "run_pipeline", _build_week5_execution_rerank_pipeline())
    _patch_attr(service, "_build_first_board_candidate", lambda **_: None)
    _patch_attr(service, "_detect_symbol_anomaly", lambda **_: None)
    _patch_attr(
        service,
        "_monster_isolation_gate",
        lambda **_: {
            "can_open_new_position": True,
            "reasons": [],
            "total_monster_position": 0.0,
            "max_monster_position": 0.0,
            "sentiment_score": 0.0,
        },
    )

    report = _as_mapping(
        service.run_week5_scan(
            symbols=["600000", "000001"],
            notify_enabled=False,
            sync_watchlist=True,
        )
    )

    signal_pool = _as_mapping(report["signal_pool"])
    ranking = _as_mapping(signal_pool["ranking"])
    execution_rerank = _as_mapping(ranking["execution_rerank"])
    candidates = _as_mapping_list(signal_pool["candidates"])

    assert ranking["score_key"] == "execution_reranked_score"
    assert execution_rerank["applied"] is True
    assert [str(item["symbol"]) for item in candidates[:2]] == ["000001", "600000"]
    assert float(candidates[0]["shortlist_score"]) < float(candidates[1]["shortlist_score"])
    assert candidates[0]["execution_rerank_applied"] is True
    assert candidates[1]["execution_high_risk"] is True
    assert float(candidates[0]["execution_reranked_score"]) > float(
        candidates[1]["execution_reranked_score"]
    )
    assert service.state.watchlist == ["000001", "600000"]


def test_service_week5_scan_falls_back_to_shortlist_order_without_execution_risk_artifact(
    tmp_path: Path,
) -> None:
    config = _load_test_config()
    config.training.bootstrap_state_path = str(tmp_path / "bootstrap_state.json")
    config.week5.auto_sync_watchlist = True
    config.week5.auto_sync_watchlist_top_k = 2
    config.week5.auto_sync_watchlist_min_score = 0.0
    service = _new_service(config)

    _patch_attr(service, "run_pipeline", _build_week5_execution_rerank_pipeline())
    _patch_attr(service, "_build_first_board_candidate", lambda **_: None)
    _patch_attr(service, "_detect_symbol_anomaly", lambda **_: None)
    _patch_attr(
        service,
        "_monster_isolation_gate",
        lambda **_: {
            "can_open_new_position": True,
            "reasons": [],
            "total_monster_position": 0.0,
            "max_monster_position": 0.0,
            "sentiment_score": 0.0,
        },
    )

    report = _as_mapping(
        service.run_week5_scan(
            symbols=["600000", "000001"],
            notify_enabled=False,
            sync_watchlist=True,
        )
    )

    signal_pool = _as_mapping(report["signal_pool"])
    ranking = _as_mapping(signal_pool["ranking"])
    execution_rerank = _as_mapping(ranking["execution_rerank"])
    candidates = _as_mapping_list(signal_pool["candidates"])

    assert ranking["score_key"] == "shortlist_score"
    assert execution_rerank["applied"] is False
    assert [str(item["symbol"]) for item in candidates[:2]] == ["600000", "000001"]
    assert candidates[0]["execution_rerank_applied"] is False
    assert float(candidates[0]["execution_reranked_score"]) == float(candidates[0]["shortlist_score"])
    assert service.state.watchlist == ["600000", "000001"]


def test_week5_market_radar_tracks_non_watchlist_anomalies_into_review_pool() -> None:
    config = _load_test_config()
    config.week5.market_radar_scan_top_n = 3
    config.week5.market_radar_notify_top_k = 2
    service = _new_service(config)
    service.state.watchlist = ["600000"]

    notifications: list[dict[str, object]] = []
    _patch_attr(
        service,
        "_resolve_symbol_universe",
        lambda **_: {
            "source": "test_universe",
            "symbols": ["600000", "300001", "002001", "300002"],
            "errors": [],
        },
    )
    _patch_attr(
        service,
        "_prefilter_week5_universe_symbols",
        lambda **_: {
            "eligible_count": 3,
            "shortlisted_count": 3,
            "errors": [],
            "shortlisted": [
                {
                    "symbol": "300001",
                    "baseline_score": 72.0,
                    "stage1": {"reason_codes": ["trend", "capital_flow"]},
                },
                {
                    "symbol": "002001",
                    "baseline_score": 68.0,
                    "stage1": {"reason_codes": ["price_volume"]},
                },
                {
                    "symbol": "300002",
                    "baseline_score": 61.0,
                    "stage1": {"reason_codes": ["liquidity"]},
                },
            ],
        },
    )

    def _fake_detect_symbol_anomaly(symbol: str, bars: object) -> dict[str, object] | None:
        _ = bars
        if symbol == "300001":
            return {"symbol": symbol, "types": ["gap"], "gap_pct": 0.091}
        if symbol == "002001":
            return {
                "symbol": symbol,
                "types": ["volume_spike"],
                "volume_ratio_5d": 3.2,
            }
        return None

    _patch_attr(service, "_detect_symbol_anomaly", _fake_detect_symbol_anomaly)
    _patch_attr(
        service,
        "_notify_if_changed",
        lambda **kwargs: notifications.append(kwargs) or {"sent": True},
    )

    report = _as_mapping(
        service.run_week5_market_radar(
            timestamp=datetime(2026, 3, 16, 10, 0),
            notify_enabled=True,
        )
    )

    assert report["status"] == "ok"
    assert report["watchlist_excluded_count"] == 1
    radar_hits = _as_mapping_list(report["radar_hits"])
    assert [str(item["symbol"]) for item in radar_hits] == ["300001", "002001"]
    review_pool_symbols = [  # noqa: SLF001
        str(item["symbol"]) for item in service._market_radar_review_pool
    ]
    assert sorted(review_pool_symbols) == ["002001", "300001"]
    assert service._last_week5_market_radar_report is not None  # noqa: SLF001
    assert len(notifications) == 1
    assert "不会触发盘中自动买卖" in str(notifications[0]["content"])
    assert "基线分" in str(notifications[0]["content"])
    assert "baseline=" not in str(notifications[0]["content"])
    assert "market radar" not in str(notifications[0]["title"])


def test_week5_market_radar_suppresses_repeated_symbol_type_notifications() -> None:
    config = _load_test_config()
    config.week5.market_radar_scan_top_n = 3
    config.week5.market_radar_notify_top_k = 2
    service = _new_service(config)
    service.state.watchlist = ["600000"]

    notifications: list[dict[str, object]] = []
    _patch_attr(
        service,
        "_resolve_symbol_universe",
        lambda **_: {
            "source": "test_universe",
            "symbols": ["600000", "300001", "002001"],
            "errors": [],
        },
    )
    _patch_attr(
        service,
        "_prefilter_week5_universe_symbols",
        lambda **_: {
            "eligible_count": 2,
            "shortlisted_count": 2,
            "errors": [],
            "shortlisted": [
                {
                    "symbol": "300001",
                    "baseline_score": 72.0,
                    "stage1": {"reason_codes": ["trend_above_ma60", "capital_flow_support"]},
                },
                {
                    "symbol": "002001",
                    "baseline_score": 68.0,
                    "stage1": {"reason_codes": ["price_volume_support"]},
                },
            ],
        },
    )

    def _fake_detect_symbol_anomaly(symbol: str, bars: object) -> dict[str, object] | None:
        _ = bars
        if symbol == "300001":
            return {"symbol": symbol, "types": ["gap"], "gap_pct": 0.091}
        if symbol == "002001":
            return {
                "symbol": symbol,
                "types": ["volume_spike"],
                "volume_ratio_5d": 3.2,
            }
        return None

    _patch_attr(service, "_detect_symbol_anomaly", _fake_detect_symbol_anomaly)
    _patch_attr(
        service,
        "_notify_if_changed",
        lambda **kwargs: notifications.append(kwargs) or {"sent": True},
    )

    first = _as_mapping(
        service.run_week5_market_radar(
            timestamp=datetime(2026, 3, 16, 10, 0),
            notify_enabled=True,
        )
    )
    second = _as_mapping(
        service.run_week5_market_radar(
            timestamp=datetime(2026, 3, 16, 10, 20),
            notify_enabled=True,
        )
    )

    assert len(notifications) == 1
    assert len(_as_mapping_list(first["notification_targets"])) == 2
    assert len(_as_mapping_list(second["notification_targets"])) == 0
    assert _as_int(second["notification_suppressed_count"]) == 2


def test_service_week5_force_universe_scan_preserves_pinned_symbols_after_prefilter() -> None:
    config = _load_test_config()
    service = _new_service(config)
    service.state.watchlist = ["600000"]

    captured: dict[str, object] = {}
    _patch_attr(
        service,
        "_resolve_symbol_universe",
        lambda **_: {
            "source": "test_universe",
            "symbols": ["600000", "000001", "300001"],
            "errors": [],
        },
    )
    _patch_attr(
        service,
        "_prefilter_week5_universe_symbols",
        lambda **_: {
            "enabled": True,
            "applied": True,
            "lookback_days": 240,
            "top_k": 500,
            "universe_count": 3,
            "eligible_count": 3,
            "shortlisted_count": 1,
            "symbols": ["000001"],
            "shortlisted": [{"symbol": "000001", "baseline_score": 70.0}],
            "preview": [],
            "stages": {
                "stage2": {
                    "applied": False,
                    "status": "pending_signal_scan",
                    "shortlist_top_n": 50,
                    "input_count": 0,
                    "advanced_count": 0,
                    "weights": {},
                    "preview": [],
                }
            },
        },
    )

    def _fake_run_pipeline(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "trace_id": "week5-pinned-test",
            "signals": [],
            "risk": {"action": "monitor", "drawdown_pct": 0.0},
        }

    _patch_attr(service, "run_pipeline", _fake_run_pipeline)
    _patch_attr(service, "_build_first_board_candidate", lambda **_: None)
    _patch_attr(service, "_detect_symbol_anomaly", lambda **_: None)
    _patch_attr(
        service,
        "_monster_isolation_gate",
        lambda **_: {
            "can_open_new_position": True,
            "reasons": [],
            "total_monster_position": 0.0,
            "max_monster_position": 0.0,
            "sentiment_score": 0.0,
        },
    )

    report = _as_mapping(
        service.run_week5_scan(
            timestamp=datetime(2026, 3, 16, 20, 30),
            notify_enabled=False,
            force_universe_scan=True,
            prefilter_enabled_override=True,
            pinned_symbols=["300001"],
        )
    )

    assert captured["symbols"] == ["000001", "300001"]
    prefilter = _as_mapping(report["prefilter"])
    assert prefilter["pinned_count"] == 1
    assert _as_text_list(prefilter["pinned_symbols"]) == ["300001"]


def test_week5_offhours_refresh_includes_market_radar_review_pool_and_clears_it() -> None:
    config = _load_test_config()
    config.week5.offhours_force_full_deep_scan_on_watchlist_below = 0
    config.week5.offhours_force_full_deep_scan_on_no_buy_streak = 0
    config.week5.offhours_force_full_deep_scan_on_drawdown_pct = 0.0
    service = _new_service(config)
    service.state.watchlist = ["600000", "000001"]
    _patch_attr(
        service,
        "_market_radar_review_pool",
        [
            {"symbol": "300001", "timestamp": "2026-03-16T14:00:00"},
            {"symbol": "002001", "timestamp": "2026-03-16T14:05:00"},
        ],
    )

    captured: dict[str, object] = {}

    def _fake_run_week5_scan(
        *,
        timestamp: datetime | None = None,
        notify_enabled: bool | None = None,
        sync_watchlist: bool | None = None,
        sync_reason: str = "",
        sync_top_k_override: int | None = None,
        force_universe_scan: bool = False,
        prefilter_enabled_override: bool | None = None,
        prefilter_top_k_override: int | None = None,
        universe_max_symbols_override: int | None = None,
        pinned_symbols: list[str] | None = None,
        scan_profile: str = "",
        symbols: list[str] | None = None,
    ) -> dict[str, object]:
        _ = (
            timestamp,
            notify_enabled,
            sync_watchlist,
            sync_reason,
            sync_top_k_override,
            force_universe_scan,
            prefilter_enabled_override,
            prefilter_top_k_override,
            universe_max_symbols_override,
            symbols,
        )
        captured["pinned_symbols"] = pinned_symbols
        return {
            "timestamp": "2026-03-16T20:30:00",
            "trace_id": "offhours-market-radar",
            "prefilter": {},
            "summary": {},
            "scan_profile": scan_profile,
        }

    _patch_attr(service, "run_week5_scan", _fake_run_week5_scan)

    report = _as_mapping(
        service.run_week5_offhours_refresh(
            timestamp=datetime(2026, 3, 16, 20, 30),
            notify_enabled=False,
            sync_watchlist=False,
        )
    )

    assert captured["pinned_symbols"] == ["300001", "002001"]
    market_radar_review = _as_mapping(report["market_radar_review"])
    assert market_radar_review["requested_count"] == 2
    assert market_radar_review["cleared_count"] == 2
    assert market_radar_review["remaining_count"] == 0
    assert service._market_radar_review_pool == []  # noqa: SLF001


def test_week5_offhours_refresh_uses_explicit_research_pool_and_dynamic_queue() -> None:
    config = _load_test_config()
    config.week5.universe_prefilter_top_k = 3
    config.week5.offhours_research_pool_top_k = 5
    config.week5.auto_sync_watchlist_top_k = 2
    config.week5.offhours_watchlist_sync_top_k = 2
    config.week5.offhours_force_full_deep_scan_on_watchlist_below = 0
    config.week5.offhours_force_full_deep_scan_on_no_buy_streak = 0
    config.week5.offhours_force_full_deep_scan_on_drawdown_pct = 0.0
    service = _new_service(config)
    service.state.watchlist = ["600000"]

    queued = _as_mapping(
        service.queue_week5_research_symbols(
            symbols=["300001", "002001", "300001"],
            source="learning_hot_discovery",
            timestamp=datetime(2026, 3, 16, 14, 5),
            metadata={"trigger": "night_learning"},
        )
    )
    assert queued["queued_count"] == 2
    assert _as_text_list(queued["queued_symbols"]) == ["300001", "002001"]

    captured: dict[str, object] = {}

    def _fake_run_week5_scan(
        *,
        timestamp: datetime | None = None,
        notify_enabled: bool | None = None,
        sync_watchlist: bool | None = None,
        sync_reason: str = "",
        sync_top_k_override: int | None = None,
        force_universe_scan: bool = False,
        prefilter_enabled_override: bool | None = None,
        prefilter_top_k_override: int | None = None,
        universe_max_symbols_override: int | None = None,
        pinned_symbols: list[str] | None = None,
        scan_profile: str = "",
        symbols: list[str] | None = None,
    ) -> dict[str, object]:
        _ = (
            timestamp,
            notify_enabled,
            sync_watchlist,
            sync_reason,
            force_universe_scan,
            prefilter_enabled_override,
            universe_max_symbols_override,
            symbols,
        )
        captured["sync_top_k_override"] = sync_top_k_override
        captured["prefilter_top_k_override"] = prefilter_top_k_override
        captured["pinned_symbols"] = list(pinned_symbols or [])
        return {
            "timestamp": "2026-03-16T20:30:00",
            "trace_id": "offhours-research-pool",
            "watchlist_size": 5,
            "scan_profile": scan_profile,
            "prefilter": {
                "enabled": True,
                "applied": True,
                "top_k": prefilter_top_k_override,
                "selected_count": 5,
                "shortlisted_count": 5,
            },
            "signal_pool": {
                "candidate_count": 5,
                "ranking": {"selected_count": 2},
            },
            "watchlist_sync": {
                "enabled": True,
                "updated": True,
                "watchlist_before": 1,
                "watchlist_after": 2,
                "symbols": ["600000", "000001"],
            },
            "summary": {},
        }

    _patch_attr(service, "run_week5_scan", _fake_run_week5_scan)

    report = _as_mapping(
        service.run_week5_offhours_refresh(
            timestamp=datetime(2026, 3, 16, 20, 30),
            notify_enabled=False,
            sync_watchlist=True,
        )
    )

    assert captured["prefilter_top_k_override"] == 5
    assert captured["sync_top_k_override"] == 2
    assert captured["pinned_symbols"] == ["002001", "300001"]
    research_pool = _as_mapping(report["research_pool"])
    assert research_pool["configured_top_k"] == 5
    assert research_pool["effective_top_k"] == 5
    assert research_pool["scan_symbol_count"] == 5
    assert research_pool["watchlist_sync_top_k"] == 2
    assert research_pool["selected_candidate_count"] == 2
    assert research_pool["supplement_symbol_count"] == 2
    assert _as_text_list(research_pool["supplement_symbols"]) == ["002001", "300001"]
    assert service._market_radar_review_pool == []  # noqa: SLF001


def test_service_week5_scan_blocks_monster_when_position_exceeds_limit() -> None:
    service = _SHARED_WEEK5_MONSTER_LIMIT_SERVICE
    config = cast(StockAnalyzerConfig, service.test_config)
    _reset_shared_week5_pipeline_service(service)

    set_cmd = _sign(
        action="SET_POSITION",
        command_id="cmd-week5-monster-limit",
        payload={"symbol": "600000", "strategy": "monster", "target_position": 0.08},
        secret=config.command_channel.secret_key,
    )
    result = service.execute_command(set_cmd)
    assert result["accepted"] is True

    report = _as_mapping(service.run_week5_scan(symbols=["600000"], notify_enabled=False))
    isolation = _as_mapping(report["monster_isolation"])
    assert isolation["can_open_new_position"] is False
    reason_values = _as_text_list(isolation["reasons"])
    assert "max_total_position" in reason_values or "max_stock_position" in reason_values


def test_service_week5_scan_triggers_empty_signal_on_drawdown(
    shared_default_week5_service: StockAnalyzerService,
) -> None:
    _ = shared_default_week5_service
    service = _SHARED_WEEK5_DRAWDOWN_SERVICE
    _reset_shared_week5_pipeline_service(service, current_equity=0.84)

    report = _as_mapping(service.run_week5_scan(symbols=["600000"], notify_enabled=False))
    empty_signal = _as_mapping(report["empty_signal"])
    assert empty_signal["triggered"] is True
    assert "drawdown_threshold" in _as_text_list(empty_signal["reasons"])


def test_service_monster_isolation_treats_no_buy_streak_as_soft_warning() -> None:
    config = _load_test_config()
    service = _new_service(config)
    service._run_summaries = [  # noqa: SLF001
        {"actionable": 0},
        {"actionable": 0},
        {"actionable": 0},
        {"actionable": 0},
        {"actionable": 0},
    ]
    monster_report = {
        "signals": [
            {"score": 40.0, "action": "hold"},
            {"score": 42.0, "action": "hold"},
        ],
        "risk": {"action": "degraded", "drawdown_pct": 0.0},
    }

    empty_signal = _as_mapping(service._evaluate_empty_signal(monster_report=monster_report))  # noqa: SLF001
    isolation = _as_mapping(
        service._monster_isolation_gate(  # noqa: SLF001
            monster_report=monster_report,
            empty_signal=empty_signal,
        )
    )

    assert empty_signal["triggered"] is True
    assert isolation["can_open_new_position"] is True
    assert isolation["reasons"] == []
    assert "empty_signal_soft" in _as_text_list(isolation["soft_reasons"])
    assert "low_sentiment_recovery_soft" in _as_text_list(isolation["soft_reasons"])


def test_service_week5_scan_auto_syncs_watchlist() -> None:
    config = _load_test_config()
    config.week5.auto_sync_watchlist = True
    config.week5.auto_sync_watchlist_top_k = 2
    service = _new_service(config)
    _seed_lightweight_week5_pipeline(service)
    assert service.state.watchlist == []

    report = _as_mapping(
        service.run_week5_scan(
            symbols=["600000", "000001"],
            notify_enabled=False,
            sync_watchlist=True,
            sync_reason="test_auto_sync",
        )
    )
    sync = _as_mapping(report.get("watchlist_sync", {}))
    assert sync.get("enabled") is True
    assert len(service.state.watchlist) > 0
    assert len(service.state.watchlist) <= 2


def test_service_week5_auto_sync_watchlist_falls_back_to_selected_symbols() -> None:
    config = _load_test_config()
    config.week5.auto_sync_watchlist = True
    config.week5.auto_sync_watchlist_top_k = 3
    service = _new_service(config)
    service.state.watchlist = ["600000"]

    sync = _as_mapping(
        service._auto_sync_watchlist_from_week5_report(
            {
                "signal_pool": {
                    "candidates": [
                        {"symbol": "600000", "action": "hold", "score": 40.0},
                        {"symbol": "000001", "action": "hold", "score": 39.0},
                    ],
                    "ranking": {
                        "selected_symbols": ["600519", "000001", "300750", "002594"],
                    },
                },
            },
            reason="test_selected_symbols_fallback",
        )
    )

    assert sync["updated"] is True
    assert "signal_pool_fallback" in str(sync["reason"])
    assert service.state.watchlist == ["600519", "000001", "300750"]


def test_service_week5_auto_sync_reports_empty_keep_diagnostics() -> None:
    config = _load_test_config()
    config.week5.auto_sync_watchlist = True
    config.week5.auto_sync_watchlist_top_k = 3
    config.week5.auto_sync_watchlist_min_score = 65.0
    service = _new_service(config)
    service.state.watchlist = ["001258", "000159"]

    sync = _as_mapping(
        service._auto_sync_watchlist_from_week5_report(
            {
                "timestamp": "2026-05-26T20:43:59",
                "signal_pool": {
                    "candidates": [
                        {
                            "symbol": "001258",
                            "action": "buy",
                            "score": 46.76,
                            "shortlist_score": 53.31,
                            "execution_rerank_reason": "execution_risk_artifact_unavailable",
                            "decision_trace": {
                                "risk_gate": {"passed": True},
                                "cross_review_gate": {"passed": True},
                                "financial_gate": {"allowed": True},
                            },
                        },
                        {
                            "symbol": "000962",
                            "action": "buy",
                            "score": 45.34,
                            "shortlist_score": 52.0,
                            "execution_rerank_reason": "execution_risk_artifact_unavailable",
                            "decision_trace": {
                                "risk_gate": {"passed": True},
                                "cross_review_gate": {"passed": True},
                                "financial_gate": {"allowed": True},
                            },
                        },
                    ],
                    "ranking": {
                        "score_key": "shortlist_score",
                        "selected_symbols": ["001258", "000962"],
                    },
                },
            },
            reason="test_empty_keep_diagnostics",
            allow_signal_pool_fallback=False,
        )
    )

    diagnostics = _as_mapping(sync["diagnostics"])
    reject_counts = _as_mapping(diagnostics["reject_counts"])
    execution_reasons = _as_mapping(diagnostics["execution_rerank_reason_counts"])

    assert sync["reason"] == "intraday_preserve_existing"
    assert service.state.watchlist == ["001258", "000159"]
    assert diagnostics["candidate_count"] == 2
    assert diagnostics["eligible_candidate_count"] == 0
    assert diagnostics["min_score"] == 65.0
    assert reject_counts["score_below_min"] == 2
    assert execution_reasons["execution_risk_artifact_unavailable"] == 2


def test_signal_quality_audit_falls_back_to_week5_candidates_when_latest_signals_empty() -> None:
    config = _load_test_config()
    service = _new_service(config)
    service.state.watchlist = ["001258"]
    _patch_attr(
        service,
        "_last_week5_scan_report",
        {
            "empty_signal": {"triggered": False, "reasons": []},
            "signal_pool": {
                "candidate_count": 1,
                "candidates": [
                    {
                        "symbol": "001258",
                        "action": "buy",
                        "score": 46.76,
                        "grade": "C",
                        "shortlist_score": 53.31,
                        "execution_rerank_reason": "execution_risk_artifact_unavailable",
                        "reasons": ["model_disagreement_probe"],
                        "decision_trace": {
                            "provider": {
                                "soft_degraded_mode": True,
                                "degrade_reason": "m2_extreme",
                            },
                            "cross_review_gate": {"passed": False},
                            "financial_gate": {"allowed": True},
                        },
                        "probabilities": {"lgbm": 1.0, "xgb": 0.43, "meta": 0.49},
                    }
                ],
            },
            "watchlist_sync": {
                "reason": "empty_candidates_keep_existing",
                "updated": False,
                "symbols": ["001258"],
            },
        },
    )

    report = _as_mapping(
        service.run_signal_quality_audit(limit=5, include_audit_events=False)
    )

    assert report["status"] == "ok"
    assert report["signal_source"] == "week5_latest_candidates"
    assert report["source_signal_count"] == 1
    assert report["summary"]["signal_count"] == 1
    assert service.state.watchlist == ["001258"]


def test_service_week5_auto_sync_skips_hard_blocked_candidates() -> None:
    config = _load_test_config()
    config.week5.auto_sync_watchlist = True
    config.week5.auto_sync_watchlist_top_k = 3
    service = _new_service(config)
    service.state.watchlist = ["600000"]

    sync = _as_mapping(
        service._auto_sync_watchlist_from_week5_report(
            {
                "timestamp": "2026-03-19T15:00:00",
                "signal_pool": {
                    "candidates": [
                        {"symbol": "600519", "action": "buy", "score": 88.0, "reasons": ["liquidity_failed"]},
                        {
                            "symbol": "000001",
                            "action": "watch",
                            "score": 87.0,
                            "reasons": ["financial_filter:low_roe"],
                        },
                        {
                            "symbol": "300750",
                            "action": "buy",
                            "score": 86.0,
                            "reasons": [],
                            "decision_trace": {
                                "risk_gate": {"passed": True},
                                "liquidity_gate": {"passed": True},
                                "cross_review_gate": {"passed": True},
                                "financial_gate": {"allowed": True},
                            },
                        },
                    ],
                    "ranking": {
                        "selected_symbols": ["600519", "000001", "300750"],
                    },
                },
            },
            reason="test_hard_blocked_candidates_filtered",
        )
    )

    assert sync["updated"] is True
    assert service.state.watchlist == ["300750"]


def test_service_week5_auto_sync_expires_stale_watchlist_after_repeated_empty_runs() -> None:
    config = _load_test_config()
    config.week5.auto_sync_watchlist = True
    config.week5.auto_sync_watchlist_keep_if_empty = True
    config.week5.auto_sync_watchlist_empty_grace_runs = 1
    config.week5.auto_sync_watchlist_preserve_max_age_hours = 18.0
    service = _new_service(config)
    service.state.watchlist = ["600000"]
    _patch_attr(
        service,
        "_week5_scan_history",
        [
            {
                "timestamp": "2026-03-17T15:00:00",
                "watchlist_sync": {
                    "reason": "week5_auto_sync",
                    "symbols": ["600000"],
                },
            },
            {
                "timestamp": "2026-03-18T15:00:00",
                "watchlist_sync": {
                    "reason": "empty_candidates_keep_existing",
                    "symbols": ["600000"],
                },
            },
        ],
    )

    sync = _as_mapping(
        service._auto_sync_watchlist_from_week5_report(
            {
                "timestamp": "2026-03-19T15:00:00",
                "signal_pool": {"candidates": []},
            },
            reason="test_expire_stale_watchlist",
        )
    )

    assert sync["reason"] == "empty_candidates_expired_watchlist"
    assert service.state.watchlist == []


def test_service_week5_scan_scheduler_intraday_preserves_existing_watchlist() -> None:
    config = _load_test_config()
    config.week5.auto_sync_watchlist = True
    config.week5.auto_sync_watchlist_top_k = 3
    service = _new_service(config)
    service.state.watchlist = ["600000", "000001"]

    def _fake_run_pipeline(**kwargs: object) -> dict[str, object]:
        _ = kwargs
        return {
            "trace_id": "scheduler-intraday-preserve",
            "signals": [
                {
                    "symbol": "600519",
                    "score": 88.0,
                    "leader_score": 88.5,
                    "action": "hold",
                    "suggested_position": 0.0,
                    "target_position": 0.0,
                    "grade": "B",
                    "reasons": ["watch_only"],
                },
                {
                    "symbol": "300750",
                    "score": 81.0,
                    "leader_score": 81.2,
                    "action": "hold",
                    "suggested_position": 0.0,
                    "target_position": 0.0,
                    "grade": "B",
                    "reasons": ["watch_only"],
                },
            ],
            "risk": {
                "action": "monitor",
                "drawdown_pct": 0.0,
            },
        }

    _patch_attr(service, "run_pipeline", _fake_run_pipeline)
    _patch_attr(service, "_build_first_board_candidate", lambda **_: None)
    _patch_attr(service, "_detect_symbol_anomaly", lambda **_: None)
    _patch_attr(
        service,
        "_monster_isolation_gate",
        lambda **_: {
            "can_open_new_position": True,
            "reasons": [],
            "total_monster_position": 0.0,
            "max_monster_position": 0.0,
            "sentiment_score": 0.0,
        },
    )

    report = _as_mapping(
        service.run_week5_scan(
            symbols=["600000", "000001"],
            timestamp=datetime(2026, 3, 16, 9, 31),
            notify_enabled=False,
            sync_watchlist=True,
            sync_reason="scheduler_week5",
        )
    )

    sync = _as_mapping(report["watchlist_sync"])
    assert sync["enabled"] is True
    assert sync["updated"] is False
    assert sync["reason"] == "intraday_preserve_existing"
    assert service.state.watchlist == ["600000", "000001"]


def test_service_week5_scan_scheduler_intraday_reuses_previous_watchlist_snapshot() -> None:
    config = _load_test_config()
    config.week5.auto_sync_watchlist = True
    service = _new_service(config)
    service.state.watchlist = []
    _patch_attr(
        service,
        "_last_week5_scan_report",
        {
            "timestamp": "2026-03-16T09:25:00",
            "watchlist_sync": {
                "symbols": ["600519", "000001"],
            },
        },
    )

    captured: dict[str, object] = {}

    def _fake_resolve_symbol_universe(**_: object) -> dict[str, object]:
        raise AssertionError("intraday scheduler should not fallback to universe scan")

    def _fake_run_pipeline(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "trace_id": "scheduler-intraday-restored",
            "signals": [],
            "risk": {
                "action": "monitor",
                "drawdown_pct": 0.0,
            },
        }

    _patch_attr(service, "_resolve_symbol_universe", _fake_resolve_symbol_universe)
    _patch_attr(service, "run_pipeline", _fake_run_pipeline)
    _patch_attr(service, "_build_first_board_candidate", lambda **_: None)
    _patch_attr(service, "_detect_symbol_anomaly", lambda **_: None)
    _patch_attr(
        service,
        "_monster_isolation_gate",
        lambda **_: {
            "can_open_new_position": True,
            "reasons": [],
            "total_monster_position": 0.0,
            "max_monster_position": 0.0,
            "sentiment_score": 0.0,
        },
    )

    report = _as_mapping(
        service.run_week5_scan(
            timestamp=datetime(2026, 3, 16, 9, 31),
            notify_enabled=False,
            sync_watchlist=True,
            sync_reason="scheduler_week5",
        )
    )

    assert captured["symbols"] == ["600519", "000001"]
    assert report["symbol_source"] == "intraday_preserved_watchlist"


def test_service_week5_scan_notify_enabled_emits_notification() -> None:
    config = _load_test_config()
    service = _new_service(config)
    _seed_lightweight_week5_pipeline(service)
    calls: list[dict[str, object]] = []

    def _fake_notify(
        title: str,
        content: str,
        level: str = "info",
        trace_id: str = "",
    ) -> dict[str, object]:
        calls.append(
            {
                "title": title,
                "content": content,
                "level": level,
                "trace_id": trace_id,
            }
        )
        return {"sent": True}

    _patch_attr(service, "notify", _fake_notify)

    _ = service.run_week5_scan(symbols=["600000", "000001"], notify_enabled=True)
    assert len(calls) >= 1


def test_service_week5_scan_uses_configured_short_and_long_lookbacks() -> None:
    service = _SHARED_WEEK5_LOOKBACK_SERVICE
    config = cast(StockAnalyzerConfig, service.test_config)
    provider = cast(RecordingSyntheticProvider, service.test_provider)
    _reset_shared_week5_pipeline_service(service)

    _ = service.run_week5_scan(symbols=["600000"], notify_enabled=False)
    requested_lookbacks = [lookback for _, lookback in provider.lookback_requests]
    assert config.evolution.universe_spec.signal_fetch_lookback_days in requested_lookbacks
    assert config.evolution.universe_spec.first_board_scan_lookback_days in requested_lookbacks


def test_service_week5_force_universe_scan_prefilters_to_top_k_before_deep_scan() -> None:
    service = _SHARED_WEEK5_PREFILTER_SERVICE
    config = cast(StockAnalyzerConfig, service.test_config)
    provider = cast(RecordingSyntheticProvider, service.test_provider)
    _reset_shared_week5_pipeline_service(service)

    report = _as_mapping(
        service.run_week5_scan(
            notify_enabled=False,
            sync_watchlist=True,
            force_universe_scan=True,
        )
    )

    prefilter = _as_mapping(report["prefilter"])
    assert prefilter["applied"] is True
    assert prefilter["lookback_days"] == 240
    assert prefilter["universe_count"] == 6
    assert prefilter["shortlisted_count"] == 3
    assert prefilter["scoring_mode"] == "two_stage_funnel"
    stages = _as_mapping(prefilter["stages"])
    assert _as_mapping(stages["stage1"])["applied"] is True
    stage2 = _as_mapping(stages["stage2"])
    assert stage2["status"] == "completed"
    assert stage2["shortlist_top_n"] == 50
    shortlisted = _as_mapping_list(prefilter["shortlisted"])
    assert len(shortlisted) == 3
    assert _as_mapping(shortlisted[0]["stage1"])["score_key"] == "baseline_score"
    requested_240 = [symbol for symbol, lookback in provider.lookback_requests if lookback == 240]
    requested_500 = [
        symbol
        for symbol, lookback in provider.lookback_requests
        if lookback == config.evolution.universe_spec.signal_fetch_lookback_days
    ]
    assert len(requested_240) == 6
    assert len(requested_500) == 3
    assert len(service.state.watchlist) <= 2


def test_week5_signal_pool_live_batches_market_depth_for_signal_pool() -> None:
    service = _SHARED_WEEK5_SIGNAL_POOL_LIVE_SERVICE
    _reset_shared_week5_signal_pool_live_service(service)
    depth_provider = cast(RecordingDepthProvider, service.test_depth_provider)

    payload = _as_mapping(service.week5_signal_pool_live(limit=2, force_refresh=True))

    assert depth_provider.calls == [(["600000", "000001"], True)]
    assert payload["depth_enabled"] is True
    first = _as_mapping(_as_mapping_list(payload["items"])[0])
    assert first["depth_available"] is True
    assert first["depth_source"] == "easyquotation_sina"
    assert _as_mapping(_as_mapping_list(first["bid_levels"])[0])["level"] == 1
    assert _as_mapping(_as_mapping_list(first["ask_levels"])[0])["price"] == 10.02


def test_week5_signal_pool_market_payload_falls_back_when_name_is_nan() -> None:
    service = _new_service(_load_test_config())

    class ProviderWithNanName:
        def fetch_daily_bars(self, symbol: str, lookback_days: int = 120) -> pd.DataFrame:
            _ = (symbol, lookback_days)
            frame = pd.DataFrame(
                {
                    "open": [10.0, 10.2],
                    "high": [10.3, 10.5],
                    "low": [9.9, 10.1],
                    "close": [10.1, 10.4],
                    "volume": [1000.0, 1200.0],
                    "turnover": [10000.0, 12000.0],
                    "name": [np.nan, "nan"],
                },
                index=pd.to_datetime(["2026-03-10", "2026-03-11"]),
            )
            frame.index.name = "date"
            return frame

    provider = ProviderWithNanName()
    _patch_attr(service, "_select_provider", lambda use_live_runtime=False: provider)
    _patch_attr(service, "_load_week5_intraday_frame", lambda **kwargs: (pd.DataFrame(), "", ""))
    _patch_attr(service, "_resolve_symbol_display_name", lambda symbol: "浦发银行")

    payload = _as_mapping(
        service._week5_service._build_week5_symbol_market_payload(
            symbol="600000",
            prefer_online=True,
            depth_snapshot={},
        )
    )

    assert payload["name"] == "浦发银行"


def test_week5_offhours_refresh_uses_weekday_light_topk_deep_profile() -> None:
    service = _SHARED_WEEKDAY_OFFHOURS_SERVICE
    _reset_shared_week5_offhours_service(service)
    provider = cast(RecordingSyntheticProvider, service.test_provider)
    config = cast(StockAnalyzerConfig, service.test_config)

    report = _as_mapping(
        service.run_week5_offhours_refresh(
            timestamp=datetime(2026, 3, 11, 20, 30),
            notify_enabled=False,
            sync_watchlist=True,
        )
    )

    assert report["scan_profile"] == "offhours_weekday_light_topk_deep"
    assert _as_mapping(report["prefilter"])["applied"] is True
    requested_240 = [symbol for symbol, lookback in provider.lookback_requests if lookback == 240]
    requested_500 = [
        symbol
        for symbol, lookback in provider.lookback_requests
        if lookback == config.evolution.universe_spec.signal_fetch_lookback_days
    ]
    assert len(requested_240) == 6
    assert len(requested_500) == 3
    assert len(service.state.watchlist) <= 2


def test_week5_offhours_refresh_uses_weekend_full_deep_profile() -> None:
    service = _SHARED_WEEKEND_OFFHOURS_SERVICE
    _reset_shared_week5_offhours_service(service)
    provider = cast(RecordingSyntheticProvider, service.test_provider)
    config = cast(StockAnalyzerConfig, service.test_config)

    report = _as_mapping(
        service.run_week5_offhours_refresh(
            timestamp=datetime(2026, 3, 14, 20, 30),
            notify_enabled=False,
            sync_watchlist=True,
        )
    )

    assert report["scan_profile"] == "offhours_weekend_full_deep"
    prefilter = _as_mapping(report["prefilter"])
    assert prefilter["applied"] is False
    assert prefilter["reason"] == "disabled_by_offhours_full_deep_profile"
    requested_240 = [symbol for symbol, lookback in provider.lookback_requests if lookback == 240]
    requested_500 = [
        symbol
        for symbol, lookback in provider.lookback_requests
        if lookback == config.evolution.universe_spec.signal_fetch_lookback_days
    ]
    assert len(requested_240) == 0
    assert len(requested_500) == 4
    assert len(service.state.watchlist) <= 2


def test_week5_offhours_refresh_uses_friday_full_deep_profile() -> None:
    service = _SHARED_FRIDAY_OFFHOURS_SERVICE
    _reset_shared_week5_offhours_service(service)
    provider = cast(RecordingSyntheticProvider, service.test_provider)
    config = cast(StockAnalyzerConfig, service.test_config)

    report = _as_mapping(
        service.run_week5_offhours_refresh(
            timestamp=datetime(2026, 3, 13, 20, 30),
            notify_enabled=False,
            sync_watchlist=True,
        )
    )

    assert report["scan_profile"] == "offhours_friday_full_deep"
    prefilter = _as_mapping(report["prefilter"])
    assert prefilter["applied"] is False
    assert prefilter["reason"] == "disabled_by_offhours_full_deep_profile"
    requested_240 = [symbol for symbol, lookback in provider.lookback_requests if lookback == 240]
    requested_500 = [
        symbol
        for symbol, lookback in provider.lookback_requests
        if lookback == config.evolution.universe_spec.signal_fetch_lookback_days
    ]
    assert len(requested_240) == 0
    assert len(requested_500) == 4
    assert len(service.state.watchlist) <= 2


def test_week5_offhours_refresh_forces_full_deep_on_exception_conditions() -> None:
    service = _SHARED_FORCED_FULL_DEEP_OFFHOURS_SERVICE
    _reset_shared_week5_offhours_service(service, watchlist=["600000"])
    provider = cast(RecordingSyntheticProvider, service.test_provider)
    config = cast(StockAnalyzerConfig, service.test_config)

    report = _as_mapping(
        service.run_week5_offhours_refresh(
            timestamp=datetime(2026, 3, 11, 20, 30),
            notify_enabled=False,
            sync_watchlist=True,
        )
    )

    assert report["scan_profile"] == "offhours_forced_full_deep"
    offhours_refresh_profile = _as_mapping(report["offhours_refresh_profile"])
    assert "watchlist_below_5" in [
        str(item) for item in cast(list[object], offhours_refresh_profile["reasons"])
    ]
    requested_240 = [symbol for symbol, lookback in provider.lookback_requests if lookback == 240]
    requested_500 = [
        symbol
        for symbol, lookback in provider.lookback_requests
        if lookback == config.evolution.universe_spec.signal_fetch_lookback_days
    ]
    assert len(requested_240) == 0
    assert len(requested_500) == 4
