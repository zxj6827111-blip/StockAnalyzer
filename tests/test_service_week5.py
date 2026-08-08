from __future__ import annotations

import tempfile
import time
import zipfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
from pytest import fixture

from stock_analyzer.command.channel import CommandEnvelope, SignedCommandProcessor
from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.data.cached_provider import CachedProvider
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.data.vendor_zip_overlay import (
    VendorZipOverlayProvider,
    write_vendor_zip_daily_index,
)
from stock_analyzer.infra.cache import InMemoryCache
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
    # Default to legacy universe path for existing tests; tests that exercise the
    # quality selector enable it explicitly via _enable_universe_quality_selector.
    config.week5.universe_quality_selector_enabled = False
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
        runtime_service_module.build_runtime_provider = lambda config, synthetic_seed=2026: (
            runtime_provider
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


def _build_test_execution_risk_artifact(
    path: Path,
    *,
    qualification_status: str = "qualified",
) -> Path:
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
        qualification_status=qualification_status,
        metadata={"test_artifact": True},
    )
    artifact.save(path)
    return path


def _build_logistic_model(*, weights: list[float], bias: float) -> dict[str, object]:
    model = LogisticProbModel.with_weights(
        weights=weights,
        bias=bias,
        learning_rate=0.05,
        epochs=16,
        l2=0.0,
        seed=7,
    )
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


def _build_week5_execution_rerank_pipeline_without_snapshots() -> object:
    def _fake_run_pipeline(**kwargs: object) -> dict[str, object]:
        _ = kwargs
        return {
            "trace_id": "week5-execution-rerank-fallback",
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
                "decision_trace": {
                    "risk_gate": {"passed": True},
                    "cross_review_gate": {"passed": True},
                    "financial_gate": {"allowed": True},
                },
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
        lambda *, symbol, candidate, force_refresh, prefer_online, depth_snapshot=None: {
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
    _patch_attr(
        service,
        "_resolve_symbol_universe",
        lambda **_: {
            "source": "local_files_primary",
            "symbols": ["600000", "000001", "600519", "300750", "002594", "601318"],
            "count": 6,
            "errors": [],
        },
    )
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
    _patch_attr(
        service,
        "_last_week5_scan_report",
        {
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
        },
    )


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
    _patch_attr(
        service,
        "_resolve_symbol_universe",
        lambda **_: {
            "source": "local_files_primary",
            "symbols": ["600000", "000001", "600519", "300750", "002594", "601318"],
            "count": 6,
            "errors": [],
        },
    )
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
    _patch_attr(
        service,
        "_resolve_symbol_universe",
        lambda **_: {
            "source": "local_files_primary",
            "symbols": ["600000", "000001", "600519", "300750"],
            "count": 4,
            "errors": [],
        },
    )
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
    _patch_attr(
        service,
        "_resolve_symbol_universe",
        lambda **_: {
            "source": "local_files_primary",
            "symbols": ["600000", "000001", "600519", "300750"],
            "count": 4,
            "errors": [],
        },
    )
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
    # Watchlist syncs only from the funnel's final selection, ordered by
    # final score (600000=92 > 000001=79), not from the reranked pool order.
    assert service.state.watchlist == ["600000", "000001"]


def test_service_week5_scan_rerank_falls_back_to_latest_symbol_snapshot(
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
        snapshot_id="snap-history-600000",
        symbol="600000",
        decision_time=datetime(2026, 3, 19, 14, 30, tzinfo=UTC),
        liquidity_score=0.12,
        volatility_score=0.91,
        meta_probability=0.46,
        degraded_mode=True,
        data_quality_score=0.84,
    )
    _write_week5_execution_snapshot(
        service,
        snapshot_id="snap-history-000001",
        symbol="000001",
        decision_time=datetime(2026, 3, 19, 14, 31, tzinfo=UTC),
        liquidity_score=0.94,
        volatility_score=0.12,
        meta_probability=0.73,
        degraded_mode=False,
        data_quality_score=0.98,
    )

    _patch_attr(
        service,
        "run_pipeline",
        _build_week5_execution_rerank_pipeline_without_snapshots(),
    )
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
    assert execution_rerank["applied_count"] == 2
    assert execution_rerank["reason"] == "applied"
    assert execution_rerank["skipped_missing_snapshot"] == 0
    assert execution_rerank["skipped_snapshot_not_found"] == 0
    assert [str(item["symbol"]) for item in candidates[:2]] == ["000001", "600000"]
    assert candidates[0]["execution_rerank_snapshot_fallback"] is True
    assert candidates[0]["snapshot_id"] == "snap-history-000001"
    assert candidates[1]["execution_rerank_snapshot_fallback"] is True
    assert candidates[1]["snapshot_id"] == "snap-history-600000"


def test_service_week5_scan_rerank_symbol_fallback_respects_shadow_only(
    tmp_path: Path,
) -> None:
    config = _load_test_config()
    config.training.bootstrap_state_path = str(tmp_path / "bootstrap_state.json")
    config.week5.auto_sync_watchlist_top_k = 2
    config.week5.auto_sync_watchlist_min_score = 0.0
    service = _new_service(config)

    artifact_path = _build_test_execution_risk_artifact(
        tmp_path / "execution_risk_artifact_shadow.json",
        qualification_status="shadow_only",
    )
    service._last_execution_risk_training = {  # noqa: SLF001
        "artifact_path": str(artifact_path),
        "dataset_id": "execution_risk_dataset_week5_test",
    }
    _write_week5_execution_snapshot(
        service,
        snapshot_id="snap-history-600000",
        symbol="600000",
        decision_time=datetime(2026, 3, 19, 14, 30, tzinfo=UTC),
        liquidity_score=0.12,
        volatility_score=0.91,
        meta_probability=0.46,
        degraded_mode=True,
        data_quality_score=0.84,
    )
    _write_week5_execution_snapshot(
        service,
        snapshot_id="snap-history-000001",
        symbol="000001",
        decision_time=datetime(2026, 3, 19, 14, 31, tzinfo=UTC),
        liquidity_score=0.94,
        volatility_score=0.12,
        meta_probability=0.73,
        degraded_mode=False,
        data_quality_score=0.98,
    )

    _patch_attr(
        service,
        "run_pipeline",
        _build_week5_execution_rerank_pipeline_without_snapshots(),
    )
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
            sync_watchlist=False,
        )
    )

    signal_pool = _as_mapping(report["signal_pool"])
    ranking = _as_mapping(signal_pool["ranking"])
    execution_rerank = _as_mapping(ranking["execution_rerank"])
    candidates = _as_mapping_list(signal_pool["candidates"])

    assert ranking["score_key"] == "shortlist_score"
    assert execution_rerank["applied"] is False
    assert execution_rerank["applied_count"] == 2
    assert execution_rerank["coverage_ratio"] == 1.0
    assert execution_rerank["reason"] == "artifact_shadow_only"
    assert [str(item["symbol"]) for item in candidates[:2]] == ["600000", "000001"]
    assert candidates[0]["execution_rerank_snapshot_fallback"] is True
    assert candidates[0]["snapshot_id"] == "snap-history-600000"
    assert candidates[0]["execution_rerank_applied"] is False
    assert candidates[0]["execution_rerank_reason"] == "artifact_shadow_only"


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
    assert float(candidates[0]["execution_reranked_score"]) == float(
        candidates[0]["shortlist_score"]
    )
    assert service.state.watchlist == ["600000", "000001"]


def test_service_week5_scan_falls_back_when_execution_snapshot_read_fails(
    tmp_path: Path,
) -> None:
    config = _load_test_config()
    config.training.bootstrap_state_path = str(tmp_path / "bootstrap_state.json")
    config.week5.auto_sync_watchlist_top_k = 2
    config.week5.auto_sync_watchlist_min_score = 0.0
    service = _new_service(config)

    artifact_path = _build_test_execution_risk_artifact(tmp_path / "execution_risk_artifact.json")
    service._last_execution_risk_training = {  # noqa: SLF001
        "artifact_path": str(artifact_path),
        "dataset_id": "execution_risk_dataset_week5_test",
    }
    _patch_attr(service, "run_pipeline", _build_week5_execution_rerank_pipeline())
    _patch_attr(service, "_build_first_board_candidate", lambda **_: None)
    _patch_attr(service, "_detect_symbol_anomaly", lambda **_: None)
    _patch_attr(
        service._sample_store,  # noqa: SLF001
        "get_snapshot",
        lambda snapshot_id: (_ for _ in ()).throw(RuntimeError("store unavailable")),
    )

    report = service.run_week5_scan(
        symbols=["600000", "000001"],
        notify_enabled=False,
        sync_watchlist=False,
    )

    ranking = _as_mapping(_as_mapping(report["signal_pool"])["ranking"])
    execution_rerank = _as_mapping(ranking["execution_rerank"])
    candidates = _as_mapping_list(_as_mapping(report["signal_pool"])["candidates"])

    assert ranking["score_key"] == "shortlist_score"
    assert execution_rerank["applied"] is False
    assert execution_rerank["skipped_snapshot_read_failed"] == 2
    assert [str(item["symbol"]) for item in candidates[:2]] == ["600000", "000001"]
    assert all(
        str(item["execution_rerank_reason"]) == "snapshot_read_failed:RuntimeError"
        for item in candidates[:2]
    )


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


def test_service_week5_scan_monster_pipeline_is_dry_run() -> None:
    config = _load_test_config()
    service = _new_service(config)
    captured: dict[str, object] = {}

    def _fake_run_pipeline(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "trace_id": "week5-monster-dry-run",
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

    _ = service.run_week5_scan(
        symbols=["600000"],
        timestamp=datetime(2026, 3, 16, 9, 31),
        notify_enabled=False,
        sync_reason="scheduler_week5",
    )

    assert captured["job_name"] == "week5_scan_monster"
    assert captured["dry_run_execution"] is True
    assert captured["use_live_runtime"] is True


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


def test_week5_offhours_full_deep_preserves_universe_source_provenance() -> None:
    config = _load_test_config()
    config.week5.offhours_weekend_full_deep_scan_enabled = True
    service = _new_service(config)
    expected_source = "provider_index_primary:quality_selector"

    def _fake_run_week5_scan(**kwargs: object) -> dict[str, object]:
        return {
            "timestamp": "2026-03-14T20:30:00",
            "trace_id": "offhours-quality-provenance",
            "watchlist_size": 300,
            "symbol_source": "stale-value",
            "scan_profile": str(kwargs.get("scan_profile", "")),
            "prefilter": {
                "universe_source": expected_source,
                "universe_quality_selection": {
                    "selector_mode": "quality",
                    "selected_count": 300,
                },
            },
            "signal_pool": {},
            "summary": {},
        }

    _patch_attr(service, "run_week5_scan", _fake_run_week5_scan)

    report = _as_mapping(
        service.run_week5_offhours_refresh(
            timestamp=datetime(2026, 3, 14, 20, 30),
            notify_enabled=False,
            sync_watchlist=False,
        )
    )

    prefilter = _as_mapping(report["prefilter"])
    assert report["symbol_source"] == f"{expected_source}:full_deep"
    assert report["universe_quality_selector_mode"] == "quality"
    assert prefilter["universe_source"] == expected_source
    assert prefilter["universe_quality_selector_mode"] == "quality"


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


def test_service_monster_isolation_skips_sentiment_block_when_no_signal_scores() -> None:
    config = _load_test_config()
    service = _new_service(config)

    empty_signal = {
        "triggered": False,
        "reasons": [],
        "no_buy_streak": 0,
        "buy_signals": 0,
        "drawdown_pct": 0.0,
        "risk_action": "monitor",
    }

    isolation = _as_mapping(
        service._monster_isolation_gate(  # noqa: SLF001
            monster_report={
                "signals": [],
                "risk": {"action": "degraded", "drawdown_pct": 0.0},
            },
            empty_signal=empty_signal,
        )
    )

    assert isolation["sentiment_available"] is False
    assert isolation["sentiment_score"] == 50.0
    assert isolation["can_open_new_position"] is True
    assert _as_text_list(isolation["reasons"]) == []
    assert "low_sentiment" not in _as_text_list(isolation["reasons"])
    assert "low_sentiment_recovery_soft" not in _as_text_list(isolation["soft_reasons"])


def test_service_monster_isolation_marks_sentiment_unavailable_without_score_keys() -> None:
    config = _load_test_config()
    service = _new_service(config)

    empty_signal = {
        "triggered": False,
        "reasons": [],
        "no_buy_streak": 0,
        "buy_signals": 0,
        "drawdown_pct": 0.0,
        "risk_action": "monitor",
    }

    isolation = _as_mapping(
        service._monster_isolation_gate(  # noqa: SLF001
            monster_report={
                "signals": [{"action": "hold"}, {"action": "sell"}],
                "risk": {"action": "freeze", "drawdown_pct": 0.0},
            },
            empty_signal=empty_signal,
        )
    )

    assert isolation["sentiment_available"] is False
    assert isolation["sentiment_score"] == 50.0
    assert "low_sentiment" not in _as_text_list(isolation["reasons"])


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


def test_service_week5_auto_sync_watchlist_keeps_old_when_zero_final_signals() -> None:
    """Zero final signals must preserve the old watchlist, never top up from
    the signal pool (stocks there never passed the final gates)."""
    config = _load_test_config()
    config.week5.auto_sync_watchlist = True
    config.week5.auto_sync_watchlist_top_k = 3
    service = _new_service(config)
    service.state.watchlist = ["600000"]

    sync = _as_mapping(
        service._auto_sync_watchlist_from_week5_report(
            {
                "timestamp": "2026-05-26T20:43:59",
                # Signal pool has plenty of candidates, but the funnel's final
                # selection is empty -> nothing may be promoted.
                "signal_pool": {
                    "candidates": [
                        {"symbol": "600000", "action": "hold", "score": 40.0},
                        {"symbol": "000001", "action": "hold", "score": 39.0},
                    ],
                    "ranking": {
                        "selected_symbols": ["600519", "000001", "300750", "002594"],
                    },
                },
                "funnel": {"final_selection": {"final_signals": []}},
            },
            reason="test_zero_final_signals",
            allow_signal_pool_fallback=False,
        )
    )

    assert sync["updated"] is False
    assert "signal_pool_fallback" not in str(sync["reason"])
    # Old watchlist preserved; no pool stock was added.
    assert service.state.watchlist == ["600000"]


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


def test_service_week5_scan_store_persists_latest_report(
    tmp_path: Path,
) -> None:
    config = _load_test_config()
    config.command_channel.state_persist_enabled = True
    config.command_channel.state_persist_path = str(tmp_path / "runtime_state.json")
    service = _new_service(config)

    report = {
        "timestamp": "2026-05-27T02:33:58.553322",
        "trace_id": "latest-persisted",
        "watchlist_sync": {
            "enabled": False,
            "updated": False,
            "reason": "disabled",
            "symbols": [],
        },
    }
    service._week5_service._state_service.store_week5_scan_report(report)

    reloaded = _new_service(config)
    latest = _as_mapping(reloaded.latest_week5_scan_report())
    history = _as_mapping(reloaded.week5_scan_history(limit=5))

    assert latest["trace_id"] == "latest-persisted"
    assert latest["timestamp"] == "2026-05-27T02:33:58.553322"
    assert _as_int(history["records"]) >= 1


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

    report = _as_mapping(service.run_signal_quality_audit(limit=5, include_audit_events=False))

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
                        {
                            "symbol": "600519",
                            "action": "buy",
                            "score": 88.0,
                            "reasons": ["liquidity_failed"],
                        },
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


def test_service_week5_scan_disabled_sync_includes_readonly_diagnostics() -> None:
    config = _load_test_config()
    config.week5.auto_sync_watchlist = True
    config.week5.auto_sync_watchlist_min_score = 65.0
    service = _new_service(config)
    service.state.watchlist = ["001258"]

    def _fake_run_pipeline(**_: object) -> dict[str, object]:
        return {
            "trace_id": "disabled-sync-diagnostics",
            "signals": [
                {
                    "symbol": "001258",
                    "strategy": "week5",
                    "score": 46.0,
                    "grade": "C",
                    "action": "buy",
                    "reasons": [],
                    "execution_rerank_reason": "execution_risk_artifact_unavailable",
                    "decision_trace": {
                        "risk_gate": {"passed": True},
                        "cross_review_gate": {"passed": True},
                        "financial_gate": {"allowed": True},
                    },
                }
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
            symbols=["001258"],
            timestamp=datetime(2026, 5, 27, 2, 33),
            notify_enabled=False,
            sync_watchlist=False,
            sync_reason="diagnostics_validation_no_sync",
        )
    )

    sync = _as_mapping(report["watchlist_sync"])
    diagnostics = _as_mapping(sync["diagnostics"])
    reject_counts = _as_mapping(diagnostics["reject_counts"])
    execution_reasons = _as_mapping(diagnostics["execution_rerank_reason_counts"])

    assert sync["enabled"] is False
    assert sync["updated"] is False
    assert sync["reason"] == "disabled"
    assert service.state.watchlist == ["001258"]
    assert diagnostics["candidate_count"] == 1
    assert diagnostics["eligible_candidate_count"] == 0
    assert reject_counts["score_below_min"] == 1
    assert execution_reasons["execution_risk_artifact_unavailable"] == 1


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


def _build_synthetic_a_share_universe(
    *,
    szse_main: int = 3500,
    szse_gem: int = 0,
    sse_main: int = 2200,
    sse_star: int = 0,
    bse: int = 96,
) -> list[str]:
    """构造模拟 A 股 universe，用于配额抽样测试。生成 6 位标准代码。

    每个前缀最多 1000 个代码（000000-000999），避免跨前缀重叠。
    """
    symbols: list[str] = []

    def _gen_with_prefix(prefix: str, count: int) -> None:
        for index in range(min(count, 1000)):
            symbols.append(f"{prefix}{index:03d}")

    def _distribute(prefixes: tuple[str, ...], total: int) -> None:
        remaining = total
        for prefix in prefixes:
            chunk = min(1000, remaining)
            if chunk <= 0:
                break
            _gen_with_prefix(prefix, chunk)
            remaining -= chunk

    # 深主板 000/001/002
    _distribute(("000", "001", "002"), szse_main)
    # 创业板 300/301
    _distribute(("300", "301"), szse_gem)
    # 沪主板 600/601/603
    _distribute(("600", "601", "603"), sse_main)
    # 科创板 688
    _gen_with_prefix("688", sse_star)
    # 北交所 430
    _gen_with_prefix("430", bse)
    return symbols


def _board_of(symbol: str) -> str:
    return runtime_service_module._board_from_a_share_symbol(symbol)


def test_quota_sample_eliminates_market_bias_for_shanghai() -> None:
    """回归测试：cap=300 截断时沪市不再被字典序前 300 挤掉。"""
    symbols = _build_synthetic_a_share_universe(
        sse_main=2200, szse_main=3500, sse_star=300, szse_gem=500, bse=96
    )
    # 字典序下 000/001/002 排最前，旧逻辑 selected[:300] 几乎全是 SZ_MAIN，SH_MAIN=0
    sampled, meta = runtime_service_module._quota_sample_universe(
        symbols,
        cap=300,
        board_scope=["SSE", "SZSE"],
        universe_ruleset_id="a_share_default_v1",
        seed_trade_date="2026-03-16",
    )

    assert len(sampled) == 300
    from collections import Counter

    board_counts = Counter(_board_of(s) for s in sampled)
    # 沪主板必须有显著代表性（不再是 0）
    assert board_counts["SH_MAIN"] >= 80, f"SH_MAIN underrepresented: {board_counts}"
    # 深主板也必须有代表性
    assert board_counts["SZ_MAIN"] >= 100, f"SZ_MAIN underrepresented: {board_counts}"
    # 科创板和创业板也应出现（独立保底）
    assert board_counts["SH_STAR"] >= 10, f"SH_STAR underrepresented: {board_counts}"
    assert board_counts["SZ_GEM"] >= 10, f"SZ_GEM underrepresented: {board_counts}"
    # BSE 不在 board_scope 内，不应出现
    assert board_counts.get("BSE", 0) == 0
    assert meta["truncation_mode"] == "board_quota_sample"
    assert meta["seed_trade_date"] == "2026-03-16"
    # 观测字段结构验证
    assert "boards" in meta
    for board_name in ("SZ_MAIN", "SZ_GEM", "SH_MAIN", "SH_STAR", "BSE"):
        assert board_name in meta["boards"]
        board_meta = meta["boards"][board_name]
        assert "input_count" in board_meta
        assert "quota" in board_meta
        assert "selected_count" in board_meta


def test_quota_sample_sh_star_not_squeezed_by_sh_main() -> None:
    """SH_STAR(科创板) 不被 SH_MAIN(沪主板) 挤占——五板块独立保底回归测试。

    反例：SH_STAR 输入 10 只、seed=2026-01-01 时，旧交易所级实现选中 0 只 688。
    五板块实现后 SH_STAR 应独立保底，至少选中 min(10, 10)=10 只。
    """
    symbols = _build_synthetic_a_share_universe(sse_main=2000, szse_main=2000, sse_star=10, bse=0)
    sampled, meta = runtime_service_module._quota_sample_universe(
        symbols,
        cap=300,
        board_scope=["SSE", "SZSE"],
        universe_ruleset_id="a_share_default_v1",
        seed_trade_date="2026-01-01",
    )
    sh_star_count = sum(1 for s in sampled if _board_of(s) == "SH_STAR")
    assert sh_star_count >= 10, (
        f"SH_STAR squeezed by SH_MAIN: only {sh_star_count} selected, expected >= 10"
    )
    # 观测字段确认 SH_STAR 独立配额
    assert meta["boards"]["SH_STAR"]["quota"] >= 10
    assert meta["boards"]["SH_STAR"]["selected_count"] >= 10
    assert meta["boards"]["SH_STAR"]["in_scope"] is True


def test_quota_sample_sz_gem_not_squeezed_by_sz_main() -> None:
    """SZ_GEM(创业板) 不被 SZ_MAIN(深主板) 挤占——五板块独立保底。"""
    symbols = _build_synthetic_a_share_universe(sse_main=2000, szse_main=2000, szse_gem=15, bse=0)
    sampled, meta = runtime_service_module._quota_sample_universe(
        symbols,
        cap=300,
        board_scope=["SSE", "SZSE"],
        universe_ruleset_id="a_share_default_v1",
        seed_trade_date="2026-01-01",
    )
    sz_gem_count = sum(1 for s in sampled if _board_of(s) == "SZ_GEM")
    assert sz_gem_count >= 10, (
        f"SZ_GEM squeezed by SZ_MAIN: only {sz_gem_count} selected, expected >= 10"
    )
    assert meta["boards"]["SZ_GEM"]["quota"] >= 10


def test_quota_sample_is_reproducible_with_same_seed() -> None:
    """同 seed_trade_date + 同 ruleset_id 两次调用结果完全一致。"""
    symbols = _build_synthetic_a_share_universe(
        sse_main=2200, szse_main=3500, sse_star=300, szse_gem=500
    )
    kwargs = {
        "cap": 300,
        "board_scope": ["SSE", "SZSE"],
        "universe_ruleset_id": "a_share_default_v1",
        "seed_trade_date": "2026-03-16",
    }
    sampled_a, _ = runtime_service_module._quota_sample_universe(symbols, **kwargs)
    sampled_b, _ = runtime_service_module._quota_sample_universe(symbols, **kwargs)
    assert sampled_a == sampled_b


def test_quota_sample_rotates_across_trade_dates() -> None:
    """不同 seed_trade_date 的结果集合应有差异（跨日轮换）。"""
    symbols = _build_synthetic_a_share_universe(
        sse_main=2200, szse_main=3500, sse_star=300, szse_gem=500
    )
    base = {
        "cap": 300,
        "board_scope": ["SSE", "SZSE"],
        "universe_ruleset_id": "a_share_default_v1",
    }
    sampled_day1, _ = runtime_service_module._quota_sample_universe(
        symbols, seed_trade_date="2026-03-16", **base
    )
    sampled_day2, _ = runtime_service_module._quota_sample_universe(
        symbols, seed_trade_date="2026-03-17", **base
    )
    assert set(sampled_day1) != set(sampled_day2)


def test_quota_sample_excludes_out_of_scope_boards() -> None:
    """board_scope=[SSE,SZSE] 时 BSE 不参与，结果不含 BSE。"""
    symbols = _build_synthetic_a_share_universe(
        sse_main=100, szse_main=100, sse_star=20, szse_gem=20, bse=96
    )
    sampled, meta = runtime_service_module._quota_sample_universe(
        symbols,
        cap=80,
        board_scope=["SSE", "SZSE"],
        universe_ruleset_id="a_share_default_v1",
        seed_trade_date="2026-03-16",
    )
    bse_count = sum(1 for s in sampled if _board_of(s) == "BSE")
    assert bse_count == 0
    # BSE 板块在观测字段中 in_scope=False，但 input_count 保留
    assert meta["boards"]["BSE"]["in_scope"] is False
    assert meta["boards"]["BSE"]["input_count"] == 96
    assert meta["boards"]["BSE"]["quota"] == 0
    assert meta["boards"]["BSE"]["selected_count"] == 0


def test_quota_sample_enforces_minimum_quota_for_small_board() -> None:
    """某板块数量极少时，仍至少拿到 min(实际数量, min_quota)。"""
    symbols = _build_synthetic_a_share_universe(sse_main=2000, szse_main=2000)
    # 仅给沪主板留极少量
    symbols = [s for s in symbols if not s.startswith("60")] + ["600001", "600002", "600003"]
    sampled, meta = runtime_service_module._quota_sample_universe(
        symbols,
        cap=300,
        board_scope=["SSE", "SZSE"],
        universe_ruleset_id="a_share_default_v1",
        seed_trade_date="2026-03-16",
        min_quota_per_in_scope_board=10,
    )
    sh_main_count = sum(1 for s in sampled if _board_of(s) == "SH_MAIN")
    # SH_MAIN 只有 3 只，保底取 min(10, 3) = 3，全部纳入
    assert sh_main_count == 3
    assert meta["boards"]["SH_MAIN"]["quota"] == 3
    assert meta["boards"]["SH_MAIN"]["selected_count"] == 3


def test_quota_sample_no_truncation_when_cap_zero() -> None:
    """cap=0 时走原路径，不抽样，truncation_mode=none。"""
    symbols = _build_synthetic_a_share_universe(sse_main=100, szse_main=100)
    sampled, meta = runtime_service_module._quota_sample_universe(
        symbols,
        cap=0,
        board_scope=["SSE", "SZSE"],
        universe_ruleset_id="a_share_default_v1",
        seed_trade_date="2026-03-16",
    )
    assert sampled == symbols
    assert meta["truncation_mode"] == "none"
    assert meta["cap"] == 0


def test_quota_sample_total_exactly_equals_cap() -> None:
    """抽样总数必须恰等于 cap（循环回流补齐正确）。"""
    symbols = _build_synthetic_a_share_universe(
        sse_main=2200, szse_main=3500, sse_star=300, szse_gem=500, bse=0
    )
    total_in_scope = len(set(symbols))
    for cap in [1, 2, 7, 23, 99, 200, 300, 500, 5700, 9999]:
        sampled, _ = runtime_service_module._quota_sample_universe(
            symbols,
            cap=cap,
            board_scope=["SSE", "SZSE"],
            universe_ruleset_id="a_share_default_v1",
            seed_trade_date="2026-03-16",
        )
        expected = min(cap, total_in_scope)
        assert len(sampled) == expected, f"cap={cap}: got {len(sampled)}, expected {expected}"
        # 无重复
        assert len(set(sampled)) == len(sampled), f"cap={cap}: duplicates found"


def test_quota_sample_circular_backfill_when_multiple_small_boards_exhausted() -> None:
    """多个小板块同时耗尽时，循环补足确保总数仍等于 effective_cap。"""
    # 两个大板块 + 两个极小板块（SH_STAR=5, SZ_GEM=3）
    symbols = _build_synthetic_a_share_universe(
        sse_main=2000, szse_main=2000, sse_star=5, szse_gem=3, bse=0
    )
    sampled, meta = runtime_service_module._quota_sample_universe(
        symbols,
        cap=300,
        board_scope=["SSE", "SZSE"],
        universe_ruleset_id="a_share_default_v1",
        seed_trade_date="2026-03-16",
    )
    # 小板块全部纳入
    sh_star_count = sum(1 for s in sampled if _board_of(s) == "SH_STAR")
    sz_gem_count = sum(1 for s in sampled if _board_of(s) == "SZ_GEM")
    assert sh_star_count == 5
    assert sz_gem_count == 3
    # 总数仍等于 cap（回流给大板块）
    assert len(sampled) == 300


def test_board_classification_covers_all_prefixes_consistent_with_exchange() -> None:
    """板块分类覆盖范围与交易所分类一致，不再有代码被漏入 OTHER。

    回归覆盖审核发现的 157 只漏网代码：003/302/605/689 前缀。
    保证对任意 6 位代码，_board_from_a_share_symbol 返回的板块所属交易所
    与 _exchange_from_a_share_symbol 返回的交易所一致。
    """
    board_to_exchange = runtime_service_module._BOARD_EXCHANGE_MAP
    # 覆盖各前缀首只 + 边界前缀
    probe_symbols = [
        # 深主板 SZ_MAIN
        "000001",
        "001001",
        "002001",
        "003001",
        # 创业板 SZ_GEM
        "300001",
        "301001",
        "302001",
        # 沪主板 SH_MAIN
        "600001",
        "601001",
        "603001",
        "605001",
        # 科创板 SH_STAR
        "688001",
        "689001",
        # 北交所 BSE
        "430001",
        "830001",
    ]
    for symbol in probe_symbols:
        board = runtime_service_module._board_from_a_share_symbol(symbol)
        exchange = runtime_service_module._exchange_from_a_share_symbol(symbol)
        assert board != "OTHER", f"{symbol} should not be OTHER"
        assert board_to_exchange.get(board, "") == exchange, (
            f"{symbol}: board={board} exchange={exchange} inconsistent"
        )

    # 特别验证审核发现的四类前缀不再被漏入 OTHER
    assert runtime_service_module._board_from_a_share_symbol("003001") == "SZ_MAIN"
    assert runtime_service_module._board_from_a_share_symbol("302001") == "SZ_GEM"
    assert runtime_service_module._board_from_a_share_symbol("605001") == "SH_MAIN"
    assert runtime_service_module._board_from_a_share_symbol("689001") == "SH_STAR"


def test_quota_sample_all_out_of_scope_returns_empty_not_fallback_slice() -> None:
    """全部股票都在 scope 外时，返回空结果，不把已排除股票选回。

    回归覆盖审核反例：symbols=430001,430002,830001 / board_scope=SSE,SZSE / cap=2
    旧实现会 fallback_slice 选回 BSE，且 BSE.input_count 错误报告为 0。
    """
    symbols = ["430001", "430002", "830001"]
    sampled, meta = runtime_service_module._quota_sample_universe(
        symbols,
        cap=2,
        board_scope=["SSE", "SZSE"],
        universe_ruleset_id="a_share_default_v1",
        seed_trade_date="2026-03-16",
    )
    # 不选回 scope 外股票
    assert sampled == []
    assert meta["truncation_mode"] == "no_in_scope_symbols"
    assert meta["effective_cap"] == 0
    # BSE 实际输入数为 3，metadata 必须真实报告
    assert meta["boards"]["BSE"]["input_count"] == 3
    assert meta["boards"]["BSE"]["in_scope"] is False
    assert meta["boards"]["BSE"]["quota"] == 0
    assert meta["boards"]["BSE"]["selected_count"] == 0
    # 沪深板块输入为 0
    assert meta["boards"]["SH_MAIN"]["input_count"] == 0
    assert meta["boards"]["SZ_MAIN"]["input_count"] == 0


def test_quota_sample_reproducible_regardless_of_input_order() -> None:
    """同 seed 下，输入顺序不同但符号集合相同时，抽样结果一致。

    因为抽样前对每个 pool 排序，可复现性不依赖 provider 返回顺序。
    """
    symbols_a = _build_synthetic_a_share_universe(
        sse_main=500, szse_main=500, sse_star=50, szse_gem=50, bse=0
    )
    # 打乱顺序
    import random as _random

    rng_shuffle = _random.Random(12345)
    symbols_b = list(symbols_a)
    rng_shuffle.shuffle(symbols_b)
    assert symbols_a != symbols_b  # 确认确实打乱了
    kwargs = {
        "cap": 100,
        "board_scope": ["SSE", "SZSE"],
        "universe_ruleset_id": "a_share_default_v1",
        "seed_trade_date": "2026-03-16",
    }
    sampled_a, _ = runtime_service_module._quota_sample_universe(symbols_a, **kwargs)
    sampled_b, _ = runtime_service_module._quota_sample_universe(symbols_b, **kwargs)
    assert sampled_a == sampled_b


def test_resolve_universe_seed_trade_date_prefers_warehouse_over_provider() -> None:
    """seed_trade_date 优先级：market_warehouse 快照 > provider graph > sentinel。

    warehouse 有值时必须用 warehouse，不降级到 provider。
    """
    config = _load_test_config()
    service = _new_service(config)

    class _FakeWarehouse:
        def background_data_quality_snapshot(self) -> dict[str, object]:
            return {"latest_trade_date": "2026-07-30"}

    class _FakeProvider:
        def latest_daily_dates(self) -> dict[str, object]:
            # provider 的日期更晚，但优先级低，不应被采用
            return {"000001": "2026-07-31"}

    _patch_attr(service, "_market_warehouse", lambda: _FakeWarehouse())
    _patch_attr(service, "_iter_market_data_provider_graph", lambda: [_FakeProvider()])
    result = service._resolve_universe_seed_trade_date()
    assert result == "2026-07-30"


def test_resolve_universe_seed_trade_date_falls_back_to_provider_when_warehouse_empty() -> None:
    """warehouse 快照为空时，降级到 provider graph 的 latest_daily_dates max。"""
    config = _load_test_config()
    service = _new_service(config)

    class _FakeWarehouse:
        def background_data_quality_snapshot(self) -> dict[str, object]:
            return {"latest_trade_date": ""}

    class _FakeProvider:
        def latest_daily_dates(self) -> dict[str, object]:
            return {"000001": "2026-07-29", "600001": "2026-07-28"}

    _patch_attr(service, "_market_warehouse", lambda: _FakeWarehouse())
    _patch_attr(service, "_iter_market_data_provider_graph", lambda: [_FakeProvider()])
    result = service._resolve_universe_seed_trade_date()
    assert result == "2026-07-29"


def test_resolve_universe_seed_trade_date_returns_sentinel_when_all_unavailable() -> None:
    """warehouse 和 provider 都无可用日期时，返回稳定 sentinel，不用 wall-clock。"""
    config = _load_test_config()
    service = _new_service(config)

    class _FakeWarehouse:
        def background_data_quality_snapshot(self) -> dict[str, object]:
            return {}

    class _FakeProvider:
        pass  # 无 latest_daily_dates 方法

    _patch_attr(service, "_market_warehouse", lambda: _FakeWarehouse())
    _patch_attr(service, "_iter_market_data_provider_graph", lambda: [_FakeProvider()])
    result = service._resolve_universe_seed_trade_date()
    assert result == "unresolved"


def test_week5_force_universe_scan_propagates_board_quota_to_prefilter_report() -> None:
    """force_universe_scan 时，_resolve_symbol_universe 返回的 board_quota
    必须透传到 prefilter_report['universe_board_quota']，便于定时任务产物验证配额。
    """
    config = _load_test_config()
    service = _new_service(config)
    service.state.watchlist = ["600000"]

    board_quota_payload = {
        "truncation_mode": "board_quota_sample",
        "cap": 300,
        "effective_cap": 3,
        "board_scope": ["SSE", "SZSE"],
        "boards": {
            "SZ_MAIN": {
                "exchange": "SZSE",
                "in_scope": True,
                "input_count": 1,
                "quota": 1,
                "selected_count": 1,
            },
            "SH_MAIN": {
                "exchange": "SSE",
                "in_scope": True,
                "input_count": 1,
                "quota": 1,
                "selected_count": 1,
            },
        },
        "seed_trade_date": "2026-03-16",
        "ruleset_id": "a_share_default_v1",
    }
    _patch_attr(
        service,
        "_resolve_symbol_universe",
        lambda **_: {
            "source": "test_universe",
            "symbols": ["600000", "000001", "300001"],
            "errors": [],
            "board_quota": board_quota_payload,
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
    _patch_attr(
        service,
        "run_pipeline",
        lambda **kwargs: {
            "trace_id": "board-quota-propagation-test",
            "signals": [],
            "risk": {"action": "monitor", "drawdown_pct": 0.0},
        },
    )
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
        )
    )
    prefilter = _as_mapping(report["prefilter"])
    propagated = prefilter.get("universe_board_quota")
    assert isinstance(propagated, dict)
    assert propagated == board_quota_payload


# ---------------------------------------------------------------------------
# UniverseCandidateSelector integration (Week5 全市场质量选择入口)
# ---------------------------------------------------------------------------
def _enable_universe_quality_selector(config: StockAnalyzerConfig) -> None:
    config.week5.universe_quality_selector_enabled = True
    config.week5.universe_quality_target_size = 300
    config.week5.universe_quality_exploration_ratio = 0.0
    config.week5.universe_quality_min_history_days = 60
    config.week5.universe_quality_min_avg_turnover_20 = 0.0
    config.week5.universe_quality_min_float_market_cap = 0.0
    config.week5.universe_quality_min_batch_coverage_ratio = 0.90
    config.week5.universe_quality_max_staleness_days = 10
    config.week5.universe_quality_require_financial_data = True
    config.week5.universe_quality_min_roe = 0.0
    config.week5.universe_quality_max_debt_ratio = 0.80
    config.week5.universe_quality_snapshot_path = str(
        Path(tempfile.gettempdir()) / "stock_analyzer_tests" / "uqs_snapshot.json"
    )


def _patch_minimal_prefilter_and_pipeline(service: StockAnalyzerService) -> dict[str, object]:
    """Patch prefilter + pipeline so run_week5_scan can complete without real data."""
    captured: dict[str, object] = {}

    def _fake_prefilter(**kwargs: object) -> dict[str, object]:
        captured["prefilter_kwargs"] = kwargs
        symbols_in = _as_text_list(kwargs.get("symbols", []))
        return {
            "enabled": True,
            "applied": True,
            "lookback_days": 240,
            "top_k": 200,
            "universe_count": len(symbols_in),
            "eligible_count": len(symbols_in),
            "shortlisted_count": len(symbols_in),
            "symbols": symbols_in,
            "shortlisted": [{"symbol": s, "baseline_score": 70.0} for s in symbols_in],
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
        }

    _patch_attr(service, "_prefilter_week5_universe_symbols", _fake_prefilter)
    _patch_attr(
        service,
        "run_pipeline",
        lambda **kwargs: {
            "trace_id": "uqs-integration-test",
            "signals": [],
            "risk": {"action": "monitor", "drawdown_pct": 0.0},
        },
    )
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
    return captured


def test_week5_quality_selector_invoked_and_report_propagated() -> None:
    """force_universe_scan + quality selector enabled -> _select_universe_quality_candidates
    is invoked, its selection feeds prefilter, and the audit report lands in
    prefilter_report['universe_quality_selection'] with required fields."""
    config = _load_test_config()
    _enable_universe_quality_selector(config)
    service = _new_service(config)
    service.state.watchlist = []

    universe_symbols = [f"600{i:03d}" for i in range(400)]
    _patch_attr(
        service,
        "_resolve_symbol_universe",
        lambda **_: {
            "source": "test_universe",
            "symbols": universe_symbols,
            "errors": [],
        },
    )

    selected_symbols = universe_symbols[:300]
    selection_report = {
        "input_count": 400,
        "hard_eligible_count": 320,
        "rejected_count_by_reason": {"low_float_market_cap": 80},
        "selected_count": 300,
        "core_selected_count": 300,
        "exploration_selected_count": 0,
        "selected_by_board": {"SH_MAIN": 300},
        "score_distribution": {"min": 40.0, "p50": 65.0, "max": 92.0, "count": 300},
        "selected": [
            {
                "symbol": s,
                "score": 90.0 - i * 0.1,
                "components": {},
                "reason_codes": ["core_quality_selected"],
            }
            for i, s in enumerate(selected_symbols)
        ],
        "trade_date": "2026-03-16",
        "ruleset_id": "a_share_default_v1",
        "selector_mode": "quality",
        "degraded_fallback_reason": "",
        "input_symbol_hash": "abc123",
        "output_symbol_hash": "def456",
        "board_quotas": {
            "SH_MAIN": {
                "exchange": "SSE",
                "in_scope": True,
                "input_count": 320,
                "quota": 300,
                "selected_count": 300,
            }
        },
    }
    select_calls: dict[str, object] = {}

    def _fake_select(**kwargs: object) -> dict[str, object]:
        select_calls.update(kwargs)
        return {"selected": selected_symbols, "report": selection_report}

    _patch_attr(service, "_select_universe_quality_candidates", _fake_select)
    _patch_attr(service, "_resolve_universe_seed_trade_date", lambda: "2026-03-16")
    captured = _patch_minimal_prefilter_and_pipeline(service)

    report = _as_mapping(
        service.run_week5_scan(
            timestamp=datetime(2026, 3, 16, 20, 30),
            notify_enabled=False,
            force_universe_scan=True,
            prefilter_enabled_override=True,
        )
    )

    # Selector invoked with full universe (not pre-truncated) and target_size=300.
    assert select_calls["target_size"] == 300
    assert select_calls["trade_date"] == "2026-03-16"
    assert select_calls["reference_date"] == "2026-03-16"
    assert select_calls["ruleset_id"] == "a_share_default_v1"
    symbols_passed = _as_text_list(cast(list, select_calls["symbols"]))
    assert len(symbols_passed) == 400
    assert symbols_passed == universe_symbols

    prefilter = _as_mapping(report["prefilter"])
    # Selection report propagated.
    propagated_report = prefilter.get("universe_quality_selection")
    assert isinstance(propagated_report, dict)
    assert propagated_report["selector_mode"] == "quality"
    assert propagated_report["selected_count"] == 300
    assert propagated_report["input_count"] == 400
    assert propagated_report["input_symbol_hash"] == "abc123"
    assert propagated_report["output_symbol_hash"] == "def456"
    assert propagated_report["trade_date"] == "2026-03-16"
    # Quality selection exposes the same board-quota audit contract as the
    # legacy quota sampler, while making the quality floor semantics explicit.
    assert prefilter["universe_quality_selector_mode"] == "quality"
    assert prefilter["universe_board_quota"] == {
        "truncation_mode": "quality_ranked_board_floor",
        "cap": 300,
        "effective_cap": 300,
        "board_scope": ["SSE", "SZSE"],
        "boards": selection_report["board_quotas"],
        "seed_trade_date": "2026-03-16",
        "ruleset_id": "a_share_default_v1",
        "selector_mode": "quality",
    }
    # Prefilter received the 300 selected symbols.
    prefilter_kwargs = cast(dict, captured["prefilter_kwargs"])
    prefilter_input = _as_text_list(cast(list, prefilter_kwargs.get("symbols", [])))
    assert len(prefilter_input) == 300
    assert set(prefilter_input) == set(selected_symbols)


def test_week5_quality_selector_disabled_falls_back_to_quota_sample() -> None:
    """When universe_quality_selector_enabled=False, the legacy _quota_sample_universe
    path runs and no universe_quality_selection report is produced (no regression)."""
    config = _load_test_config()
    config.week5.universe_quality_selector_enabled = False
    service = _new_service(config)
    service.state.watchlist = []

    universe_symbols = [f"600{i:03d}" for i in range(400)]
    _patch_attr(
        service,
        "_resolve_symbol_universe",
        lambda **_: {
            "source": "test_universe",
            "symbols": universe_symbols,
            "errors": [],
        },
    )
    select_calls: list[bool] = []

    def _fail_select(**kwargs: object) -> dict[str, object]:
        select_calls.append(True)
        return {"selected": [], "report": {}}

    _patch_attr(service, "_select_universe_quality_candidates", _fail_select)
    _patch_minimal_prefilter_and_pipeline(service)

    report = _as_mapping(
        service.run_week5_scan(
            timestamp=datetime(2026, 3, 16, 20, 30),
            notify_enabled=False,
            force_universe_scan=True,
            prefilter_enabled_override=True,
        )
    )
    # Quality selector must NOT be invoked when disabled.
    assert select_calls == []
    prefilter = _as_mapping(report["prefilter"])
    assert prefilter.get("universe_quality_selection") is None
    # Legacy path still feeds prefilter with universe symbols.
    assert prefilter["reason"] == "universe_scan"


def test_week5_quality_selector_does_not_affect_manual_symbols() -> None:
    """Manual symbols path bypasses the quality selector entirely (no regression)."""
    config = _load_test_config()
    _enable_universe_quality_selector(config)
    service = _new_service(config)
    service.state.watchlist = ["600000"]

    select_calls: list[bool] = []
    pipeline_symbols: list[list[str]] = []

    def _fail_select(**kwargs: object) -> dict[str, object]:
        select_calls.append(True)
        return {"selected": [], "report": {}}

    _patch_attr(service, "_select_universe_quality_candidates", _fail_select)
    _patch_minimal_prefilter_and_pipeline(service)

    def _capture_pipeline(**kwargs: object) -> dict[str, object]:
        pipeline_symbols.append(_as_text_list(cast(list, kwargs.get("symbols", []))))
        return {
            "trace_id": "uqs-manual-test",
            "signals": [],
            "risk": {"action": "monitor", "drawdown_pct": 0.0},
        }

    _patch_attr(service, "run_pipeline", _capture_pipeline)

    report = _as_mapping(
        service.run_week5_scan(
            timestamp=datetime(2026, 3, 16, 20, 30),
            notify_enabled=False,
            symbols=["600000", "000001"],
        )
    )
    # Quality selector never runs for manual symbols.
    assert select_calls == []
    # Manual symbols reach the pipeline directly (prefilter is skipped for manual input).
    assert pipeline_symbols
    assert set(pipeline_symbols[0]) == {"600000", "000001"}
    prefilter = _as_mapping(report["prefilter"])
    assert prefilter["reason"] == "manual_symbols"
    assert prefilter.get("universe_quality_selection") is None


def test_week5_full_deep_monster_cap_uses_global_quality_score() -> None:
    config = _load_test_config()
    _enable_universe_quality_selector(config)
    config.week5.monster_scan_max_symbols = 120
    service = _new_service(config)
    service.state.watchlist = []

    selected_symbols = [
        *[f"000{i:03d}" for i in range(1, 151)],
        *[f"600{i:03d}" for i in range(1, 151)],
    ]
    score_by_symbol = {symbol: float(index) for index, symbol in enumerate(selected_symbols)}
    selection_report = {
        "input_count": 300,
        "hard_eligible_count": 300,
        "selected_count": 300,
        "core_selected_count": 300,
        "exploration_selected_count": 0,
        "selected_by_board": {"SZ_MAIN": 150, "SH_MAIN": 150},
        "score_distribution": {"count": 300},
        "selected": [
            {
                "symbol": symbol,
                "score": score_by_symbol[symbol],
                "components": {},
                "reason_codes": ["core_quality_selected"],
            }
            for symbol in selected_symbols
        ],
        "trade_date": "2026-03-16",
        "ruleset_id": "a_share_default_v1",
        "selector_mode": "quality",
        "fallback_reason": "",
        "input_symbol_hash": "input-hash",
        "output_symbol_hash": "output-hash",
        "board_quotas": {},
    }
    _patch_attr(
        service,
        "_resolve_symbol_universe",
        lambda **_: {
            "source": "test_universe",
            "symbols": selected_symbols,
            "errors": [],
        },
    )
    _patch_attr(
        service,
        "_select_universe_quality_candidates",
        lambda **_: {"selected": selected_symbols, "report": selection_report},
    )
    _patch_attr(service, "_resolve_universe_seed_trade_date", lambda: "2026-03-16")
    _patch_minimal_prefilter_and_pipeline(service)
    pipeline_symbols: list[list[str]] = []

    def _capture_pipeline(**kwargs: object) -> dict[str, object]:
        pipeline_symbols.append(_as_text_list(cast(list, kwargs.get("symbols", []))))
        return {
            "trace_id": "quality-monster-cap-test",
            "signals": [],
            "risk": {"action": "monitor", "drawdown_pct": 0.0},
        }

    _patch_attr(service, "run_pipeline", _capture_pipeline)
    pinned = "000001"
    report = _as_mapping(
        service.run_week5_scan(
            timestamp=datetime(2026, 3, 16, 20, 30),
            notify_enabled=False,
            force_universe_scan=True,
            prefilter_enabled_override=False,
            pinned_symbols=[pinned],
        )
    )

    expected_non_pinned = sorted(
        [symbol for symbol in selected_symbols if symbol != pinned],
        key=lambda symbol: (-score_by_symbol[symbol], symbol),
    )[:119]
    assert pipeline_symbols[0] == [pinned, *expected_non_pinned]
    controls = _as_mapping(report["monster_scan_controls"])
    assert controls["cap"] == 120
    assert controls["cap_applied"] is True
    assert controls["ranking_mode"] == "universe_quality_score"


def test_week5_full_deep_degraded_fallback_scans_pinned_only() -> None:
    config = _load_test_config()
    _enable_universe_quality_selector(config)
    config.week5.monster_scan_max_symbols = 120
    service = _new_service(config)
    service.state.watchlist = []

    degraded_symbols = [f"600{i:03d}" for i in range(300)]
    degraded_boards = {
        "SH_MAIN": {
            "exchange": "SSE",
            "in_scope": True,
            "input_count": 300,
            "quota": 300,
            "selected_count": 300,
        }
    }
    selection_report = {
        "input_count": 5000,
        "target_size": 300,
        "selected_count": 300,
        "selected": [
            {
                "symbol": symbol,
                "score": 0.0,
                "components": {},
                "reason_codes": ["degraded_fallback"],
            }
            for symbol in degraded_symbols
        ],
        "trade_date": "2026-03-16",
        "ruleset_id": "a_share_default_v1",
        "selector_mode": "degraded_fallback",
        "fallback_source": "quota_sampler",
        "fallback_reason": "batch_coverage_below_threshold",
        "board_quotas": degraded_boards,
    }
    _patch_attr(
        service,
        "_resolve_symbol_universe",
        lambda **_: {
            "source": "provider_index_primary",
            "symbols": degraded_symbols,
            "errors": [],
        },
    )
    _patch_attr(
        service,
        "_select_universe_quality_candidates",
        lambda **_: {"selected": degraded_symbols, "report": selection_report},
    )
    _patch_attr(service, "_resolve_universe_seed_trade_date", lambda: "2026-03-16")
    _patch_minimal_prefilter_and_pipeline(service)
    pipeline_symbols: list[list[str]] = []

    def _capture_pipeline(**kwargs: object) -> dict[str, object]:
        pipeline_symbols.append(_as_text_list(cast(list, kwargs.get("symbols", []))))
        return {
            "trace_id": "degraded-fail-closed-test",
            "signals": [],
            "risk": {"action": "monitor", "drawdown_pct": 0.0},
        }

    _patch_attr(service, "run_pipeline", _capture_pipeline)
    pinned = "300999"
    report = _as_mapping(
        service.run_week5_scan(
            timestamp=datetime(2026, 3, 16, 20, 30),
            notify_enabled=False,
            force_universe_scan=True,
            prefilter_enabled_override=False,
            pinned_symbols=[pinned],
        )
    )

    assert pipeline_symbols == [[pinned]]
    prefilter = _as_mapping(report["prefilter"])
    assert prefilter["degraded_fail_closed"] is True
    assert prefilter["degraded_fail_closed_reason"] == "quality_unavailable_without_snapshot"
    quota_report = _as_mapping(prefilter["universe_board_quota"])
    assert quota_report["truncation_mode"] == "quality_ranked_board_floor"
    assert quota_report["selector_mode"] == "degraded_fallback"
    assert quota_report["boards"] == degraded_boards
    assert "truncation_mode" not in _as_mapping(quota_report["boards"])
    controls = _as_mapping(report["monster_scan_controls"])
    assert controls["ranking_mode"] == "degraded_fail_closed_pinned_only"
    assert controls["selected_count"] == 1


def test_quality_snapshot_persist_failure_is_reported(tmp_path: Path) -> None:
    config = _load_test_config()
    _enable_universe_quality_selector(config)
    snapshot_directory = tmp_path / "snapshot-target-is-directory"
    snapshot_directory.mkdir()
    config.week5.universe_quality_snapshot_path = str(snapshot_directory)
    service = _new_service(config)

    days = 80
    dates = pd.bdate_range(end="2026-07-31", periods=days)
    close = np.linspace(10.0, 14.0, days)
    frame = pd.DataFrame(
        {
            "symbol": ["600001"] * days,
            "date": dates,
            "open": close * 0.998,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": [10_000_000.0] * days,
            "turnover": [50_000_000.0] * days,
            "float_market_cap": [4_000_000_000.0] * days,
            "suspended": [False] * days,
            "is_st": [False] * days,
            "is_delisting_risk": [False] * days,
            "roe": [0.12] * days,
            "debt_ratio": [0.30] * days,
            "financial_data_complete": [True] * days,
            "financial_completeness": [0.95] * days,
            "background_data_complete": [True] * days,
            "holder_count": [40_000.0] * days,
            "northbound_net": [0.0] * days,
            "dragon_tiger_flag": [0.0] * days,
        }
    )

    class _Warehouse:
        def fetch_universe_quality_metrics(self, **_kwargs: object) -> pd.DataFrame:
            return frame.copy()

    _patch_attr(service, "_market_warehouse", lambda: _Warehouse())
    result = service._select_universe_quality_candidates(
        symbols=["600001"],
        target_size=1,
        trade_date="2026-07-31",
        reference_date="2026-07-31",
        ruleset_id="a_share_default_v1",
        board_scope=["SSE"],
    )
    report = _as_mapping(result["report"])
    assert report["selector_mode"] == "quality_all_eligible"
    assert report["snapshot_persisted"] is False
    assert "snapshot_persist_error" in report
    assert snapshot_directory.is_dir()


def test_quality_all_eligible_below_target_does_not_overwrite_snapshot(
    tmp_path: Path,
) -> None:
    config = _load_test_config()
    _enable_universe_quality_selector(config)
    snapshot_path = tmp_path / "existing-quality-snapshot.json"
    existing_payload = b'{"sentinel":"existing-success"}'
    snapshot_path.write_bytes(existing_payload)
    config.week5.universe_quality_snapshot_path = str(snapshot_path)
    service = _new_service(config)

    days = 80
    dates = pd.bdate_range(end="2026-07-31", periods=days)
    close = np.linspace(10.0, 14.0, days)
    frame = pd.DataFrame(
        {
            "symbol": ["600001"] * days,
            "date": dates,
            "open": close * 0.998,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": [10_000_000.0] * days,
            "turnover": [50_000_000.0] * days,
            "float_market_cap": [4_000_000_000.0] * days,
            "suspended": [False] * days,
            "is_st": [False] * days,
            "is_delisting_risk": [False] * days,
            "roe": [0.12] * days,
            "debt_ratio": [0.30] * days,
            "financial_data_complete": [True] * days,
            "financial_completeness": [0.95] * days,
            "background_data_complete": [True] * days,
            "holder_count": [40_000.0] * days,
            "northbound_net": [0.0] * days,
            "dragon_tiger_flag": [0.0] * days,
        }
    )

    class _Warehouse:
        def fetch_universe_quality_metrics(self, **_kwargs: object) -> pd.DataFrame:
            return frame.copy()

    _patch_attr(service, "_market_warehouse", lambda: _Warehouse())
    result = service._select_universe_quality_candidates(
        symbols=["600001"],
        target_size=2,
        trade_date="2026-07-31",
        reference_date="2026-07-31",
        ruleset_id="a_share_default_v1",
        board_scope=["SSE"],
    )
    report = _as_mapping(result["report"])
    assert report["selector_mode"] == "quality_all_eligible"
    assert report["target_size"] == 2
    assert report["selected_count"] == 1
    assert report["snapshot_persisted"] is False
    assert report["snapshot_persist_skip_reason"] == "selected_below_target:1<2"
    assert snapshot_path.read_bytes() == existing_payload


def test_degraded_selection_does_not_overwrite_success_snapshot(tmp_path: Path) -> None:
    config = _load_test_config()
    _enable_universe_quality_selector(config)
    snapshot_path = tmp_path / "quality-snapshot.json"
    config.week5.universe_quality_snapshot_path = str(snapshot_path)
    service = _new_service(config)

    dates = pd.bdate_range(end="2026-07-31", periods=80)
    frames: list[pd.DataFrame] = []
    for index, symbol in enumerate(("600001", "600002")):
        close = np.linspace(10.0, 14.0 + index, len(dates))
        frames.append(
            pd.DataFrame(
                {
                    "symbol": [symbol] * len(dates),
                    "date": dates,
                    "open": close * 0.998,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "volume": [10_000_000.0] * len(dates),
                    "turnover": [50_000_000.0] * len(dates),
                    "float_market_cap": [4_000_000_000.0] * len(dates),
                    "suspended": [False] * len(dates),
                    "is_st": [False] * len(dates),
                    "is_delisting_risk": [False] * len(dates),
                    "roe": [0.12 + index * 0.01] * len(dates),
                    "debt_ratio": [0.30] * len(dates),
                    "financial_data_complete": [True] * len(dates),
                    "financial_completeness": [0.95] * len(dates),
                    "background_data_complete": [True] * len(dates),
                    "holder_count": [40_000.0] * len(dates),
                    "northbound_net": [0.0] * len(dates),
                    "dragon_tiger_flag": [0.0] * len(dates),
                }
            )
        )
    full_frame = pd.concat(frames, ignore_index=True)

    class _MutableWarehouse:
        def __init__(self, frame: pd.DataFrame) -> None:
            self.frame = frame

        def fetch_universe_quality_metrics(self, **_kwargs: object) -> pd.DataFrame:
            return self.frame.copy()

    warehouse = _MutableWarehouse(full_frame)
    _patch_attr(service, "_market_warehouse", lambda: warehouse)
    kwargs = {
        "symbols": ["600001", "600002"],
        "target_size": 1,
        "trade_date": "2026-07-31",
        "reference_date": "2026-07-31",
        "ruleset_id": "a_share_default_v1",
        "board_scope": ["SSE"],
    }
    success = service._select_universe_quality_candidates(**kwargs)
    success_report = _as_mapping(success["report"])
    assert success_report["selector_mode"] == "quality"
    assert success_report["snapshot_persisted"] is True
    persisted_before = snapshot_path.read_bytes()

    warehouse.frame = full_frame[full_frame["symbol"] == "600001"].copy()
    degraded = service._select_universe_quality_candidates(**kwargs)
    degraded_report = _as_mapping(degraded["report"])
    assert degraded_report["selector_mode"] == "snapshot_fallback"
    assert degraded_report["snapshot_persisted"] is False
    assert str(degraded_report["snapshot_persist_skip_reason"]).startswith(
        "selector_mode_not_successful"
    )
    assert snapshot_path.read_bytes() == persisted_before


# ---------------------------------------------------------------------------
# Batch source resolution: provider graph before market warehouse
# ---------------------------------------------------------------------------
def _write_vendor_daily_zip(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    daily_csv = "\n".join(
        [
            "code,datetime,open,high,low,close,volume,amount,circ_mv",
            "600000.SH,2025-12-30,10,11,9,10.5,100,123.4,200000",
            "600000.SH,2025-12-31,10.5,11.5,10,11,200,234.5,210000",
        ]
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("2025/600000.SH.csv", daily_csv.encode("utf-8"))


def _build_vendor_overlay_provider(tmp_path: Path) -> VendorZipOverlayProvider:
    _write_vendor_daily_zip(tmp_path / "全A日K" / "2025.zip")
    index_path = tmp_path / "index" / "daily_index.json"
    write_vendor_zip_daily_index(root=tmp_path, output_path=index_path)
    return VendorZipOverlayProvider(
        data_root=str(tmp_path),
        index_path=str(index_path),
        delta_db_path=str(tmp_path / "delta" / "market_delta.duckdb"),
        delta_package_root=str(tmp_path / "delta" / "package"),
    )


def _bare_service_with_provider(
    config: StockAnalyzerConfig, provider: object
) -> StockAnalyzerService:
    service = object.__new__(StockAnalyzerService)
    object.__setattr__(service, "_config", config)
    object.__setattr__(service, "_provider", provider)
    object.__setattr__(service, "_realtime_provider", None)

    def _unexpected_market_warehouse() -> object:
        raise AssertionError("market warehouse must not be reached when a batch provider exists")

    object.__setattr__(service, "_market_warehouse", _unexpected_market_warehouse)
    return service


def test_week5_batch_source_prefers_vendor_overlay_provider_through_wrapper(
    tmp_path: Path,
) -> None:
    """vendor overlay 部署:selector 数据源必须是 provider graph 里的
    VendorZipOverlayProvider,即使外层包了 CachedProvider;绝不允许把
    overlay 内部的 delta MarketWarehouse 当成完整数据源。"""
    config = _load_test_config()
    overlay = _build_vendor_overlay_provider(tmp_path)
    wrapped = CachedProvider(
        inner=overlay,
        cache=InMemoryCache(),
        ttl_sec=60,
        key_prefix="week5_test",
    )
    service = _bare_service_with_provider(config, wrapped)

    source = service._resolve_universe_quality_batch_source()

    assert source is overlay


def test_week5_batch_source_prefers_vendor_overlay_provider_without_wrapper(
    tmp_path: Path,
) -> None:
    """未启用缓存时 provider 直接是 VendorZipOverlayProvider,仍应被选中。"""
    config = _load_test_config()
    overlay = _build_vendor_overlay_provider(tmp_path)
    service = _bare_service_with_provider(config, overlay)

    source = service._resolve_universe_quality_batch_source()

    assert source is overlay


def test_week5_batch_source_falls_back_to_market_warehouse_when_no_provider_batch(
    tmp_path: Path,
) -> None:
    """普通 market_warehouse/离线部署:provider graph 无批量能力时必须回退
    到 _market_warehouse(),保持原行为。"""
    config = _load_test_config()
    service = _new_service(config)

    class _MarketWarehouseFallback:
        pass

    fallback = _MarketWarehouseFallback()
    _patch_attr(service, "_market_warehouse", lambda: fallback)

    source = service._resolve_universe_quality_batch_source()

    assert source is fallback


def test_week5_quality_selector_uses_vendor_overlay_batch_source(tmp_path: Path) -> None:
    """_select_universe_quality_candidates 在 vendor overlay 模式下必须把
    VendorZipOverlayProvider 的批量能力传给 UniverseCandidateSelector。"""
    config = _load_test_config()
    _enable_universe_quality_selector(config)
    overlay = _build_vendor_overlay_provider(tmp_path)
    service = _bare_service_with_provider(config, overlay)
    captured: dict[str, object] = {}

    class _CapturingSelector:
        def __init__(self, **kwargs: object) -> None:
            captured["warehouse"] = kwargs["warehouse"]

        def select(self, **kwargs: object) -> dict[str, object]:
            captured["select_symbols"] = kwargs["symbols"]
            return {
                "selected": ["600000"],
                "report": {
                    "selector_mode": "quality",
                    "target_size": 300,
                    "selected_count": 1,
                },
            }

    from stock_analyzer.runtime import universe_candidate_selector as selector_module_ref

    original_selector = selector_module_ref.UniverseCandidateSelector
    original_persist = selector_module_ref.persist_selection_snapshot
    try:
        selector_module_ref.UniverseCandidateSelector = _CapturingSelector  # type: ignore[assignment]
        selector_module_ref.persist_selection_snapshot = lambda *a, **k: None  # type: ignore[assignment]
        result = service._select_universe_quality_candidates(
            symbols=["600000"],
            target_size=300,
            trade_date="2026-07-31",
            reference_date="2026-07-31",
            ruleset_id="a_share_default_v1",
            board_scope=["SSE", "SZSE", "BSE"],
        )
    finally:
        selector_module_ref.UniverseCandidateSelector = original_selector
        selector_module_ref.persist_selection_snapshot = original_persist

    assert captured["warehouse"] is overlay
    assert captured["select_symbols"] == ["600000"]
    assert result["selected"] == ["600000"]


def _write_vendor_daily_zip_multi_day(
    path: Path,
    *,
    symbol: str = "600000.SH",
    start: str = "2025-01-02",
    end: str = "2025-12-31",
) -> None:
    """A full trading-year vendor CSV so the selector's min-history gate passes."""
    dates = pd.bdate_range(start=start, end=end)
    rows = [f"{symbol},{dt.strftime('%Y-%m-%d')},10,11,9,10.5,200,234.5,210000" for dt in dates]
    daily_csv = "\n".join(["code,datetime,open,high,low,close,volume,amount,circ_mv", *rows])
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("2025/600000.SH.csv", daily_csv.encode("utf-8"))


def test_week5_quality_selector_passes_financial_gate_with_backfilled_snapshots(
    tmp_path: Path,
) -> None:
    """vendor overlay 部署:backfill 的财务快照经 batch PIT join 后,quality
    selector 的财务硬门禁(require_financial_data)必须真实通过,600000 被
    quality 模式选中(而不是 degraded_fallback 或财务剔除)。"""
    config = _load_test_config()
    _enable_universe_quality_selector(config)
    _write_vendor_daily_zip_multi_day(tmp_path / "全A日K" / "2025.zip")
    index_path = tmp_path / "index" / "daily_index.json"
    write_vendor_zip_daily_index(root=tmp_path, output_path=index_path)
    overlay = VendorZipOverlayProvider(
        data_root=str(tmp_path),
        index_path=str(index_path),
        delta_db_path=str(tmp_path / "delta" / "market_delta.duckdb"),
        delta_package_root=str(tmp_path / "delta" / "package"),
    )
    snapshots = pd.DataFrame(
        {
            "symbol": ["600000"],
            "end_date": pd.to_datetime(["2025-09-30"]),
            "ann_date": pd.to_datetime(["2025-11-10"]),
            "roe": [0.15],
            "debt_ratio": [0.45],
            "update_flag": [0],
            "financial_report_date": ["2025-09-30"],
            "financial_as_of": ["2025-11-10"],
            "financial_source": ["tushare_fina_indicator"],
            "financial_trust_level": ["reported"],
            "financial_missing_fields": [""],
            "financial_data_complete": [True],
            "financial_completeness": [1.0],
            "coverage_complete": [True],
            "as_of": ["2025-11-10"],
            "source": ["tushare_fina_indicator"],
        }
    )
    from stock_analyzer.data.market_warehouse import MarketWarehouse

    MarketWarehouse(
        db_path=str(tmp_path / "delta" / "market_delta.duckdb"),
        package_root=str(tmp_path / "delta" / "package"),
    ).upsert_financial_snapshots(symbol="600000", frame=snapshots)
    service = _bare_service_with_provider(config, overlay)

    result = service._select_universe_quality_candidates(
        symbols=["600000"],
        target_size=300,
        trade_date="2025-12-31",
        reference_date="2025-12-31",
        ruleset_id="a_share_default_v1",
        board_scope=["SSE"],
    )

    report = result["report"]
    assert isinstance(report, dict)
    assert report["selector_mode"] in {"quality", "quality_all_eligible"}
    assert report["selected_count"] == 1
    assert result["selected"] == ["600000"]
    assert report.get("fallback_reason") in (None, "")
    rejected = report.get("rejected_count_by_reason", {})
    assert isinstance(rejected, dict)
    assert "financial" not in rejected
    assert rejected.get("stale", 0) == 0

def test_week5_prefilter_symbol_fetch_goes_through_session_bars_cache() -> None:
    config = _load_test_config()
    config.week5.universe_prefilter_lookback_days = 240
    provider = RecordingSyntheticProvider(seed_offset=2027)
    service = _new_service(config, provider=provider)
    lookback = max(120, int(config.week5.universe_prefilter_lookback_days))

    first = service._prefilter_week5_universe_symbol(
        symbol="600000",
        lookback_days=lookback,
        allowed_exchanges=set(),
    )
    second = service._prefilter_week5_universe_symbol(
        symbol="600000",
        lookback_days=lookback,
        allowed_exchanges=set(),
    )
    assert first is not None
    assert second is not None
    # Second call is a cache hit: the provider was asked exactly once.
    assert provider.lookback_requests.count(("600000", lookback)) == 1
    assert len(provider.lookback_requests) == 1
    assert service._week5_bars_cache_hits == 1
    assert service._week5_bars_cache_misses == 1

    # A different lookback is a distinct cache key and refetches.
    service._prefilter_week5_universe_symbol(
        symbol="600000",
        lookback_days=120,
        allowed_exchanges=set(),
    )
    assert provider.lookback_requests.count(("600000", 120)) == 1


def test_week5_prefilter_reports_profiling_and_clears_cache_per_scan() -> None:
    config = _load_test_config()
    config.week5.universe_prefilter_lookback_days = 240
    config.week5.universe_prefilter_top_k = 3
    provider = RecordingSyntheticProvider(seed_offset=2027)
    service = _new_service(config, provider=provider)
    symbols = ["600000", "000001", "600519", "300750", "002594"]

    report = _as_mapping(service._prefilter_week5_universe_symbols(symbols=symbols))
    profile = _as_mapping(report["profile"])
    assert _as_int(profile["timed_symbols"]) == 5
    assert profile["total_seconds"] >= 0.0
    assert profile["per_symbol_avg_ms"] >= 0.0
    slowest = _as_mapping_list(profile["slowest_symbols"])
    assert len(slowest) == 5
    ms_values = [float(item["ms"]) for item in slowest]
    assert ms_values == sorted(ms_values, reverse=True)
    assert {str(item["symbol"]) for item in slowest} == set(symbols)
    assert _as_int(profile["cache_misses"]) == 5
    assert _as_int(profile["cache_hits"]) == 0
    assert _as_int(report["universe_count"]) == 5

    # A second scan refetches everything: the cache is cleared at scan start.
    provider.lookback_requests.clear()
    report2 = _as_mapping(service._prefilter_week5_universe_symbols(symbols=symbols))
    profile2 = _as_mapping(report2["profile"])
    assert _as_int(profile2["cache_misses"]) == 5
    assert _as_int(profile2["cache_hits"]) == 0
    assert len(provider.lookback_requests) == 5


def test_service_run_pipeline_reports_pipeline_stage_ms() -> None:
    config = _load_test_config()
    provider = RecordingSyntheticProvider(seed_offset=2027)
    service = _new_service(config, provider=provider)

    payload = _as_mapping(
        service.run_pipeline(
            symbols=["600000"],
            strategy="trend",
            current_equity=1.0,
            dry_run_execution=True,
            notify_enabled=False,
        )
    )
    runtime = _as_mapping(payload["runtime"])
    stages = _as_mapping(runtime["pipeline_stage_ms"])
    assert set(stages.keys()) == {"fetch_bars_ms", "feature_engine_ms", "inference_ms"}
    assert _as_int(stages["fetch_bars_ms"]) >= 0
    assert _as_int(stages["feature_engine_ms"]) >= 0
    assert _as_int(stages["inference_ms"]) >= 0
