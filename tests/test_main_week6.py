from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient

from stock_analyzer import main as main_module
from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.runtime.service import StockAnalyzerService


def _patch_attr(target: object, name: str, value: object) -> None:
    setattr(cast(Any, target), name, value)


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
    config.training.bootstrap_state_path = str(temp_root / "test_bootstrap_state_main_week6.json")
    return config


def _new_service() -> StockAnalyzerService:
    service = StockAnalyzerService(config=_load_test_config())
    provider = SyntheticProvider(seed_offset=2028)
    _patch_attr(service, "_provider", provider)
    _patch_attr(service._pipeline, "_provider", provider)
    _patch_attr(service, "_realtime_provider", provider)
    if service._realtime_pipeline is not None:
        _patch_attr(service._realtime_pipeline, "_provider", provider)
    _patch_attr(service, "_record_audit_event", lambda *args, **kwargs: None)
    _patch_attr(service, "_refresh_runtime_state_from_disk_if_changed", lambda: None)
    return service


def _seed_week6_run_stub(service: StockAnalyzerService) -> None:
    report = {
        "timestamp": "2026-03-10T10:18:00",
        "main_force": {"records": 1, "strong_count": 1, "items": [{"symbol": "600000"}]},
        "strategy_allocation": {"regime": "neutral", "weights": {"trend": 0.5, "monster": 0.5}},
        "calendar_factor": {"holiday_risk": "normal"},
        "global_market_factor": {"risk_score": 40.0},
        "regulatory_factor": {"excluded_symbols": []},
    }

    def _fake_run_week6_analysis(**kwargs: object) -> dict[str, object]:
        _ = kwargs
        stored_report = dict(report)
        service._last_week6_report = stored_report
        service._week6_history.append(stored_report)
        return stored_report

    _patch_attr(service, "run_week6_analysis", _fake_run_week6_analysis)


def _seed_week6_data_quality_stub(service: StockAnalyzerService) -> None:
    report = {
        "timestamp": "2026-03-10T10:18:00",
        "status": "healthy",
        "success_symbols": 2,
        "overall_coverage_ratio": 0.95,
        "critical_fields": [],
    }

    def _fake_run_week6_data_prewarm(**kwargs: object) -> dict[str, object]:
        _ = kwargs
        stored_report = dict(report)
        service._last_week6_data_quality_report = stored_report
        service._week6_data_quality_history.append(stored_report)
        return stored_report

    _patch_attr(service, "run_week6_data_prewarm", _fake_run_week6_data_prewarm)


def _reset_shared_main_week6_service(service: StockAnalyzerService) -> None:
    _patch_attr(service, "_last_week6_report", None)
    service._week6_history.clear()
    _patch_attr(service, "_last_week6_data_quality_report", None)
    service._week6_data_quality_history.clear()
    service._global_market_snapshot.clear()
    service._global_market_history.clear()
    service._regulatory_watchlist.clear()
    service._audit_events.clear()
    _patch_attr(service, "_audit_seq", 0)


_SHARED_MAIN_WEEK6_SERVICE = _new_service()
_seed_week6_run_stub(_SHARED_MAIN_WEEK6_SERVICE)
_seed_week6_data_quality_stub(_SHARED_MAIN_WEEK6_SERVICE)
_SHARED_MAIN_WEEK6_CLIENT = TestClient(main_module.app)


@contextmanager
def _patched_service(
    service: StockAnalyzerService = _SHARED_MAIN_WEEK6_SERVICE,
) -> Iterator[TestClient]:
    original_service = main_module._service
    _reset_shared_main_week6_service(service)
    main_module._service = service
    try:
        yield _SHARED_MAIN_WEEK6_CLIENT
    finally:
        main_module._service = original_service


def test_week6_endpoints_run_latest_and_history() -> None:
    with _patched_service() as client:
        run_response = client.post(
            "/week6/run",
            json={"symbols": ["600000", "000001"], "notify_enabled": False},
        )
        assert run_response.status_code == 200
        run_payload = run_response.json()
        assert "main_force" in run_payload
        assert "strategy_allocation" in run_payload
        assert "calendar_factor" in run_payload
        assert "global_market_factor" in run_payload
        assert "regulatory_factor" in run_payload

        latest_response = client.get("/week6/latest")
        assert latest_response.status_code == 200
        latest_payload = latest_response.json()
        assert "report" in latest_payload

        history_response = client.get("/week6/history", params={"limit": 10})
        assert history_response.status_code == 200
        history_payload = history_response.json()
        assert history_payload["records"] >= 1


def test_week6_global_snapshot_and_regulatory_watchlist_endpoints() -> None:
    with _patched_service() as client:
        snapshot_set = client.post(
            "/week6/global/snapshot",
            json={
                "us_index_change_pct": 0.8,
                "a50_change_pct": 0.3,
                "usd_cnh_change_pct": -0.1,
                "commodity_change_pct": 0.2,
                "a_share_correlation": 0.55,
            },
        )
        assert snapshot_set.status_code == 200
        assert "snapshot" in snapshot_set.json()

        snapshot_get = client.get("/week6/global/snapshot")
        assert snapshot_get.status_code == 200
        snapshot_payload = snapshot_get.json()
        assert "snapshot" in snapshot_payload

        snapshot_history = client.get("/week6/global/history", params={"limit": 10})
        assert snapshot_history.status_code == 200
        history_payload = snapshot_history.json()
        assert history_payload["records"] >= 1

        watchlist_set = client.post(
            "/week6/regulatory/watchlist",
            json={
                "entries": [
                    {"symbol": "600000", "tag": "inquiry", "note": "test inquiry"},
                    {"symbol": "000001", "tag": "watchlist", "note": "test watchlist"},
                ]
            },
        )
        assert watchlist_set.status_code == 200
        set_payload = watchlist_set.json()
        assert set_payload["records"] >= 2

        watchlist_get = client.get("/week6/regulatory/watchlist")
        assert watchlist_get.status_code == 200
        get_payload = watchlist_get.json()
        assert get_payload["records"] >= 2


def test_week6_data_quality_endpoints() -> None:
    with _patched_service() as client:
        run_response = client.post(
            "/week6/data-quality/run",
            json={
                "symbols": ["600000", "000001"],
                "lookback_days": 60,
                "notify_enabled": False,
                "source_trace_id": "api-week6-data-quality",
            },
        )
        assert run_response.status_code == 200
        run_payload = run_response.json()
        assert "status" in run_payload
        assert "overall_coverage_ratio" in run_payload

        latest_response = client.get("/week6/data-quality/latest")
        assert latest_response.status_code == 200
        latest_payload = latest_response.json()
        assert "report" in latest_payload

        history_response = client.get("/week6/data-quality/history", params={"limit": 10})
        assert history_response.status_code == 200
        history_payload = history_response.json()
        assert history_payload["records"] >= 1
