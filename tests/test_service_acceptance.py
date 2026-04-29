from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.data.provider import SyntheticProvider
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
    if isinstance(value, int):
        return value
    raise AssertionError(f"Expected int, got {value!r}")


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
    return config


def _new_service(config: StockAnalyzerConfig) -> StockAnalyzerService:
    service = StockAnalyzerService(config=config)
    provider = SyntheticProvider(seed_offset=2027)
    service._provider = provider
    service._pipeline._provider = provider
    return service


def test_service_week4_acceptance_generates_report_and_history() -> None:
    service = _new_service(_load_test_config())
    _ = service.run_pipeline(symbols=["600000"], strategy="trend", current_equity=1.0)

    report = _as_mapping(service.run_week4_acceptance(sla_recent_runs=20))
    summary = _as_mapping(report["summary"])
    acceptance_summary = _as_mapping(report["acceptance_summary"])
    runtime_sla = _as_mapping(report["runtime_sla"])
    assert _as_int(summary["total"]) >= 6
    assert _as_int(summary["fail"]) == 0
    assert report["overall"] in {"pass", "pass_with_warnings"}
    assert acceptance_summary["overall"] in {"pass", "pass_with_warnings"}
    assert _as_int(acceptance_summary["total"]) + 1 == _as_int(summary["total"])
    assert runtime_sla["check_name"] == "sla_compliance"
    assert runtime_sla["status"] in {"pass", "warn", "fail"}

    latest = service.latest_week4_acceptance_report()
    assert latest is not None
    history = _as_mapping(service.week4_acceptance_history(limit=10))
    assert _as_int(history["records"]) >= 1
    assert "artifact" in report


def test_service_week4_acceptance_marks_invalid_close_time_as_fail() -> None:
    config = _load_test_config()
    service = _new_service(config)
    config.scheduler.close_reconcile_time = "invalid-time"
    _ = service.run_pipeline(symbols=["600000"], strategy="trend", current_equity=1.0)

    report = _as_mapping(service.run_week4_acceptance(sla_recent_runs=20))
    checks = _as_mapping_list(report["checks"])
    close_check = next(
        item
        for item in checks
        if str(item.get("name", "")) == "scheduler_close_reconcile_configured"
    )
    assert close_check["status"] == "fail"
    assert report["overall"] == "fail"


def test_service_week4_acceptance_uses_intraday_runtime_sla_and_keeps_all_window_sla() -> None:
    service = _new_service(_load_test_config())
    service._latency_history_ms = [
        {
            "timestamp": "2026-03-16T10:05:00",
            "duration_ms": 42_000,
            "job_name": "week5_live_runtime",
            "use_live_runtime": True,
        },
        {"timestamp": "2026-03-16T20:40:00", "duration_ms": 180_000},
        {"timestamp": "2026-03-16T21:10:00", "duration_ms": 210_000},
    ]

    report = _as_mapping(service.run_week4_acceptance(sla_recent_runs=10, notify_enabled=False))
    runtime_sla = _as_mapping(report["runtime_sla"])
    window_sla = _as_mapping(report["sla"])

    assert runtime_sla["scope"] == "intraday"
    assert runtime_sla["job_scope"] == "live_runtime"
    assert _as_int(runtime_sla["recent_runs"]) == 1
    assert runtime_sla["status"] == "pass"
    assert _as_int(runtime_sla["excluded_by_session_scope"]) == 2
    assert _as_int(runtime_sla["excluded_by_job_scope"]) == 0
    assert window_sla["scope"] == "all"
    assert _as_int(window_sla["recent_runs"]) == 3
    assert window_sla["status"] == "degraded"
    assert report["overall"] in {"pass", "pass_with_warnings"}


def test_service_week4_acceptance_ignores_unscoped_intraday_latency_for_runtime_sla() -> None:
    service = _new_service(_load_test_config())
    service._latency_history_ms = [
        {"timestamp": "2026-03-16T10:05:00", "duration_ms": 600_000},
        {"timestamp": "2026-03-16T11:15:00", "duration_ms": 720_000},
    ]

    report = _as_mapping(service.run_week4_acceptance(sla_recent_runs=10, notify_enabled=False))
    runtime_sla = _as_mapping(report["runtime_sla"])
    check = next(
        item
        for item in _as_mapping_list(report["checks"])
        if str(item.get("name", "")) == "sla_compliance"
    )

    assert runtime_sla["scope"] == "intraday"
    assert runtime_sla["job_scope"] == "live_runtime"
    assert _as_int(runtime_sla["recent_runs"]) == 0
    assert _as_int(runtime_sla["excluded_by_job_scope"]) == 2
    assert runtime_sla["status"] == "warn"
    assert "excluded_unscoped_runs=2" in str(runtime_sla["detail"])
    assert check["status"] == "warn"
    assert report["overall"] == "pass_with_warnings"


def test_service_week4_acceptance_excludes_week5_monster_scan_from_live_runtime_sla() -> None:
    service = _new_service(_load_test_config())
    service._latency_history_ms = [
        {
            "timestamp": "2026-03-16T10:05:00",
            "duration_ms": 480_000,
            "job_name": "week5_scan_monster",
            "strategy": "monster",
            "symbol_count": 34,
            "use_live_runtime": True,
        },
        {
            "timestamp": "2026-03-16T10:10:00",
            "duration_ms": 42_000,
            "job_name": "week5_live_runtime",
            "runtime_role": "live_runtime",
            "strategy": "trend",
            "symbol_count": 8,
            "use_live_runtime": True,
        },
    ]

    report = _as_mapping(service.run_week4_acceptance(sla_recent_runs=10, notify_enabled=False))
    runtime_sla = _as_mapping(report["runtime_sla"])
    monster_scan_sla = _as_mapping(report["monster_scan_sla"])

    assert _as_int(runtime_sla["recent_runs"]) == 1
    assert _as_int(runtime_sla["excluded_by_job_scope"]) == 1
    assert runtime_sla["status"] == "pass"
    assert _as_int(monster_scan_sla["recent_runs"]) == 1
    assert monster_scan_sla["status"] == "ok"
    assert report["overall"] in {"pass", "pass_with_warnings"}


def test_service_week4_acceptance_fails_slow_live_runtime_sla() -> None:
    service = _new_service(_load_test_config())
    service._latency_history_ms = [
        {
            "timestamp": "2026-03-16T10:05:00",
            "duration_ms": 120_000,
            "job_name": "week5_live_runtime",
            "use_live_runtime": True,
        }
    ]

    report = _as_mapping(service.run_week4_acceptance(sla_recent_runs=10, notify_enabled=False))
    runtime_sla = _as_mapping(report["runtime_sla"])

    assert _as_int(runtime_sla["recent_runs"]) == 1
    assert runtime_sla["status"] == "fail"
    assert report["overall"] == "fail"


def test_service_week4_acceptance_uses_bounded_runtime_sla_window() -> None:
    config = _load_test_config()
    config.acceptance.runtime_sla_recent_runs = 10
    config.week5.live_runtime_max_symbols = 8
    service = _new_service(config)
    service._latency_history_ms = [
        {
            "timestamp": "2026-03-16T10:05:00",
            "duration_ms": 120_000,
            "job_name": "week5_live_runtime",
            "runtime_role": "live_runtime",
            "symbol_count": 15,
            "use_live_runtime": True,
        },
        {
            "timestamp": "2026-03-16T10:10:00",
            "duration_ms": 42_000,
            "job_name": "week5_live_runtime",
            "runtime_role": "live_runtime",
            "symbol_count": 4,
            "use_live_runtime": True,
        },
    ]

    report = _as_mapping(service.run_week4_acceptance(sla_recent_runs=100, notify_enabled=False))
    runtime_sla = _as_mapping(report["runtime_sla"])

    assert _as_int(runtime_sla["recent_runs"]) == 1
    assert _as_int(runtime_sla["excluded_by_symbol_count"]) == 1
    assert runtime_sla["status"] == "pass"
    assert report["overall"] in {"pass", "pass_with_warnings"}


def test_service_runtime_sla_reports_slowest_run_context() -> None:
    service = _new_service(_load_test_config())
    service._latency_history_ms = [
        {
            "timestamp": "2026-03-16T10:05:00",
            "duration_ms": 42_000,
            "job_name": "week5_live_runtime",
            "runtime_role": "live_runtime",
            "strategy": "trend",
            "symbol_count": 12,
            "use_live_runtime": True,
            "trace_id": "fast-run",
        },
        {
            "timestamp": "2026-03-16T10:10:00",
            "duration_ms": 120_000,
            "job_name": "week5_live_runtime",
            "runtime_role": "live_runtime",
            "strategy": "trend",
            "symbol_count": 15,
            "use_live_runtime": True,
            "trace_id": "slow-run",
        },
    ]

    sla = _as_mapping(service.sla_report(recent_runs=10, session_scope="intraday"))
    slowest = _as_mapping_list(sla["slowest_runs"])

    assert slowest[0]["trace_id"] == "slow-run"
    assert slowest[0]["job_name"] == "week5_live_runtime"
    assert slowest[0]["runtime_role"] == "live_runtime"
    assert _as_int(slowest[0]["duration_ms"]) == 120_000
    assert _as_int(slowest[0]["symbol_count"]) == 15


def test_service_week4_acceptance_exports_json_and_csv(tmp_path: Path) -> None:
    config = _load_test_config()
    config.acceptance.export_dir = str(tmp_path / "acceptance_artifacts")
    service = _new_service(config)
    _ = service.run_pipeline(symbols=["600000"], strategy="trend", current_equity=1.0)

    report = _as_mapping(
        service.run_week4_acceptance(
            sla_recent_runs=20,
            export_enabled=True,
            notify_enabled=False,
        )
    )
    artifact = _as_mapping(report["artifact"])
    json_path = Path(str(artifact["json_path"]))
    csv_path = Path(str(artifact["checks_csv_path"]))
    assert json_path.exists()
    assert csv_path.exists()


def test_service_container_runtime_assets_count_as_docker_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_test_config()
    service = _new_service(config)

    monkeypatch.setenv("STOCK_ANALYZER_CONTAINERIZED", "1")
    original_exists = Path.exists

    def fake_exists(path: Path) -> bool:
        normalized = str(path).replace("\\", "/")
        if normalized.endswith("/Dockerfile") or normalized.endswith("/docker-compose.yml"):
            return False
        if normalized in {
            "/app/scripts/docker-entrypoint.sh",
            "/app/config/default.yaml",
            "/app/artifacts",
        }:
            return True
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", fake_exists)

    ok, detail = service._docker_assets_acceptance_status()  # noqa: SLF001

    assert ok is True
    assert "container_runtime_assets=" in detail
