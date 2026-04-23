from __future__ import annotations

import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.runtime import service as runtime_service_module
from stock_analyzer.runtime.service import StockAnalyzerService


def _as_mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def _as_mapping_list(value: object) -> list[Mapping[str, object]]:
    assert isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    items = [_as_mapping(item) for item in value]
    assert len(items) == len(value)
    return items


def _as_int(value: object) -> int:
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


def _as_float(value: object) -> float:
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    return float(value)


def _as_text(value: object) -> str:
    assert isinstance(value, str)
    return value


def _as_text_list(value: object) -> list[str]:
    assert isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    items = [_as_text(item) for item in value]
    assert len(items) == len(value)
    return items


def _patch_attr(target: object, name: str, value: object) -> None:
    object.__setattr__(target, name, value)


class _CachedSyntheticProvider:
    def __init__(self, seed_offset: int = 0) -> None:
        self._delegate = SyntheticProvider(seed_offset=seed_offset)
        self._daily_cache: dict[tuple[str, int], Any] = {}
        self._intraday_cache: dict[tuple[str, str, int], Any] = {}

    def fetch_daily_bars(self, symbol: str, lookback_days: int = 120) -> Any:
        cache_key = (symbol, lookback_days)
        frame = self._daily_cache.get(cache_key)
        if frame is None:
            frame = self._delegate.fetch_daily_bars(symbol=symbol, lookback_days=lookback_days)
            self._daily_cache[cache_key] = frame
        return frame.copy()

    def fetch_intraday_summary(
        self,
        symbol: str,
        interval: str,
        lookback_days: int = 120,
    ) -> Any:
        cache_key = (symbol, interval, lookback_days)
        frame = self._intraday_cache.get(cache_key)
        if frame is None:
            frame = self._delegate.fetch_intraday_summary(
                symbol=symbol,
                interval=interval,
                lookback_days=lookback_days,
            )
            self._intraday_cache[cache_key] = frame
        return frame.copy()


def _seed_week5_report(service: StockAnalyzerService) -> None:
    _patch_attr(
        service,
        "_last_week5_scan_report",
        {
            "timestamp": "2026-03-10T10:18:00",
            "summary": {"prefilter_applied": False, "prefilter_shortlisted": 1},
            "empty_signal": {"triggered": False, "reasons": []},
            "signal_pool": {
                "candidate_count": 1,
                "candidates": [
                    {
                        "symbol": "600000",
                        "score": 80.0,
                        "shortlist_score": 80.0,
                        "action": "buy",
                    }
                ],
            },
        },
    )


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
    config.liquidity_filter_monster.min_daily_turnover = 0.0
    config.liquidity_filter_monster.min_float_market_cap = 0.0
    config.liquidity_filter_monster.max_turnover_rate = 1.0
    config.week5.auto_notify = False
    config.week6.auto_notify = False
    config.training.artifact_path = str(root / "artifacts" / "nonexistent_test_model.json")
    config.training.bootstrap_auto_seed_watchlist = False
    temp_root = Path(tempfile.gettempdir()) / "stock_analyzer_tests"
    config.training.bootstrap_state_path = str(temp_root / "test_bootstrap_state_week6.json")
    return config


def _new_service(config: StockAnalyzerConfig) -> StockAnalyzerService:
    provider = _CachedSyntheticProvider(seed_offset=2028)
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
    _patch_attr(service, "_refresh_runtime_state_from_disk_if_changed", lambda: None)
    _seed_week5_report(service)
    return service


def _reset_shared_week6_service(service: StockAnalyzerService) -> None:
    service.state.current_equity = 1.0
    service.state.watchlist = []
    service.state.pause_new_buy = False
    service.state.reconcile_required = False
    _patch_attr(service, "_last_week6_report", None)
    service._week6_history.clear()
    service._run_summaries.clear()
    service._latency_history_ms.clear()
    service._audit_events.clear()
    _patch_attr(service, "_audit_seq", 0)
    _patch_attr(service, "_global_market_snapshot", {})
    _patch_attr(service, "_global_market_history", [])
    _patch_attr(service, "_regulatory_watchlist", {})
    _seed_week5_report(service)


_SHARED_WEEK6_SERVICE = _new_service(_load_test_config())


def test_service_week6_analysis_generates_report_and_history() -> None:
    service = _SHARED_WEEK6_SERVICE
    _reset_shared_week6_service(service)

    report = service.run_week6_analysis(symbols=["600000", "000001"], notify_enabled=False)
    assert "main_force" in report
    assert "strategy_allocation" in report
    assert "calendar_factor" in report
    assert "global_market_factor" in report
    assert "regulatory_factor" in report
    assert "execution_adjustment" in report

    latest = service.latest_week6_report()
    assert latest is not None
    history = service.week6_history(limit=10)
    assert _as_int(history["records"]) >= 1


def test_service_week6_analysis_applies_regulatory_exclusion() -> None:
    service = _SHARED_WEEK6_SERVICE
    _reset_shared_week6_service(service)
    _ = service.set_regulatory_watchlist(
        entries=[{"symbol": "600000", "tag": "inquiry", "note": "test"}]
    )

    report = service.run_week6_analysis(symbols=["600000"], notify_enabled=False)
    regulatory = _as_mapping(report["regulatory_factor"])
    assert "600000" in _as_text_list(regulatory["excluded_symbols"])
    main_force = _as_mapping_list(_as_mapping(report["main_force"])["items"])
    item = next(entry for entry in main_force if entry["symbol"] == "600000")
    assert item["eligible"] is False


def test_service_week6_analysis_uses_global_snapshot_and_crash_regime() -> None:
    service = _SHARED_WEEK6_SERVICE
    _reset_shared_week6_service(service)
    service.state.current_equity = 0.84
    _ = service.update_global_market_snapshot(
        snapshot={
            "us_index_change_pct": -2.0,
            "a50_change_pct": -1.5,
            "usd_cnh_change_pct": 0.8,
            "commodity_change_pct": -1.0,
            "a_share_correlation": 0.65,
        }
    )

    report = service.run_week6_analysis(symbols=["600000"], notify_enabled=False)
    allocation = _as_mapping(report["strategy_allocation"])
    assert _as_text(allocation["regime"]) == "crash"
    global_market_factor = _as_mapping(report["global_market_factor"])
    assert _as_float(global_market_factor["risk_score"]) < 50


def test_service_week6_analysis_notify_enabled_emits_notification() -> None:
    service = _SHARED_WEEK6_SERVICE
    _reset_shared_week6_service(service)
    calls: list[dict[str, object]] = []
    original_notify = service.notify

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
    try:
        _ = service.run_week6_analysis(symbols=["600000", "000001"], notify_enabled=True)
    finally:
        _patch_attr(service, "notify", original_notify)
    assert len(calls) >= 1
