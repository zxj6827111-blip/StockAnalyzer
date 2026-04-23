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
    config.models.cross_review.p_lgbm_min = 0.0
    config.models.cross_review.p_xgb_min = 0.0
    config.models.cross_review.p_meta_min = 0.0
    config.models.cross_review.max_diff = 1.0
    config.liquidity_filter_monster.min_daily_turnover = 0.0
    config.liquidity_filter_monster.min_float_market_cap = 0.0
    config.liquidity_filter_monster.max_turnover_rate = 1.0
    config.command_channel.state_persist_enabled = False
    config.command_channel.history_archive_enabled = False
    config.week5.auto_notify = False
    config.week5.first_board_windows = ["09:30-09:31"]
    config.training.artifact_path = str(root / "artifacts" / "nonexistent_test_model.json")
    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = False
    config.training.bootstrap_auto_seed_watchlist = False
    temp_root = Path(tempfile.gettempdir()) / "stock_analyzer_tests"
    config.training.bootstrap_state_path = str(temp_root / "test_bootstrap_state_main_week5.json")
    return config


def _new_service() -> StockAnalyzerService:
    service = StockAnalyzerService(config=_load_test_config())
    provider = SyntheticProvider(seed_offset=2027)
    _patch_attr(service, "_provider", provider)
    _patch_attr(service._pipeline, "_provider", provider)
    _patch_attr(service, "_realtime_provider", provider)
    if service._realtime_pipeline is not None:
        _patch_attr(service._realtime_pipeline, "_provider", provider)
    _patch_attr(service, "_record_audit_event", lambda *args, **kwargs: None)
    _patch_attr(service, "_refresh_runtime_state_from_disk_if_changed", lambda: None)
    return service


def _seed_week5_run_stub(service: StockAnalyzerService) -> None:
    report = {
        "timestamp": "2026-03-10T10:18:00",
        "summary": {"first_board_candidates": 0, "anomalies": 0},
        "first_board": {"candidate_count": 0, "candidates": [], "leaders": []},
        "anomalies": {"event_count": 0, "events": []},
        "monster_isolation": {"can_open_new_position": True, "reasons": []},
        "signal_pool": {
            "candidate_count": 1,
            "candidates": [{"symbol": "600000", "shortlist_score": 80.0}],
        },
    }

    def _fake_run_week5_scan(**kwargs: object) -> dict[str, object]:
        _ = kwargs
        stored_report = dict(report)
        service._last_week5_scan_report = stored_report
        service._week5_scan_history.append(stored_report)
        return stored_report

    _patch_attr(service, "run_week5_scan", _fake_run_week5_scan)


def _reset_shared_main_week5_service(service: StockAnalyzerService) -> None:
    service.state.watchlist = []
    service.state.current_equity = 1.0
    _patch_attr(service, "_last_week5_scan_report", None)
    service._week5_scan_history.clear()
    service._audit_events.clear()
    _patch_attr(service, "_audit_seq", 0)


_SHARED_MAIN_WEEK5_SERVICE = _new_service()
_seed_week5_run_stub(_SHARED_MAIN_WEEK5_SERVICE)
_SHARED_MAIN_WEEK5_CLIENT = TestClient(main_module.app)


@contextmanager
def _patched_service(
    service: StockAnalyzerService = _SHARED_MAIN_WEEK5_SERVICE,
) -> Iterator[TestClient]:
    original_service = main_module._service
    _reset_shared_main_week5_service(service)
    main_module._service = service
    try:
        yield _SHARED_MAIN_WEEK5_CLIENT
    finally:
        main_module._service = original_service


def test_week5_scan_endpoints_run_latest_and_history() -> None:
    with _patched_service() as client:
        run_response = client.post(
            "/week5/scan/run",
            json={"symbols": ["600000", "000001"], "notify_enabled": False},
        )
        assert run_response.status_code == 200
        run_payload = run_response.json()
        assert "summary" in run_payload
        assert "first_board" in run_payload
        assert "anomalies" in run_payload
        assert "monster_isolation" in run_payload

        latest_response = client.get("/week5/scan/latest")
        assert latest_response.status_code == 200
        latest_payload = latest_response.json()
        assert "report" in latest_payload

        history_response = client.get("/week5/scan/history", params={"limit": 10})
        assert history_response.status_code == 200
        history_payload = history_response.json()
        assert history_payload["records"] >= 1
