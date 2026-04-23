from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.data.provider import SyntheticProvider
from stock_analyzer.runtime.service import StockAnalyzerService


def _load_test_config(tmp_path: Path) -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.scheduler.premarket_time = "23:59"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "23:59"
    config.week5.auto_run = False
    config.week5.offhours_universe_refresh_enabled = False
    config.week6.auto_run = False
    config.cloud_backup.enabled = False
    config.acceptance.auto_run = False
    config.evolution.enabled = True
    config.evolution.auto_run = True
    config.evolution.offhours_time = "20:40"
    config.evolution.m3_maintenance_interval_min = 20
    config.evolution.strict_dependency_check = False
    config.evolution.auto_generate_loader_inputs = False
    config.evolution.code_commit_id = "git:test"
    config.market_warehouse.enabled = False
    config.market_warehouse.auto_run = False
    config.evolution.suggestions_dir = str((tmp_path / "suggestions").relative_to(tmp_path))
    config.evolution.manifest_path = str(
        (tmp_path / "artifacts" / "evolution" / "run_manifest.json").relative_to(tmp_path)
    )
    config.evolution.compliance_db_path = str(
        (tmp_path / "artifacts" / "evolution" / "compliance.duckdb").relative_to(tmp_path)
    )
    return config


def _job_result(results: list[dict[str, object]], name: str) -> dict[str, object]:
    for item in results:
        if str(item.get("job", "")) == name:
            return item
    raise AssertionError(f"job not found: {name}")


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    raise AssertionError(f"Expected mapping, got {type(value).__name__}")


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    raise AssertionError(f"Expected bool value, got {value!r}")


def _as_int(value: object) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    raise AssertionError(f"Expected numeric value, got {value!r}")


def _patch_attr(target: object, name: str, value: object) -> None:
    setattr(cast(Any, target), name, value)


def _new_service(config: StockAnalyzerConfig, tmp_path: Path) -> StockAnalyzerService:
    service = StockAnalyzerService(config=config)
    provider = SyntheticProvider(seed_offset=2031)
    _patch_attr(service, "_provider", provider)
    _patch_attr(service._pipeline, "_provider", provider)
    _patch_attr(service, "_evolution_project_root", tmp_path)
    return service


def _reset_evolution_orchestrator(
    service: StockAnalyzerService,
    config: StockAnalyzerConfig,
    tmp_path: Path,
) -> None:
    orchestrator_cls = service._evolution_orchestrator.__class__
    _patch_attr(
        service,
        "_evolution_orchestrator",
        orchestrator_cls(config=config.evolution, project_root=tmp_path),
    )


def test_scheduler_runs_evolution_offhours_job(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config=config, tmp_path=tmp_path)
    _reset_evolution_orchestrator(service=service, config=config, tmp_path=tmp_path)
    _patch_attr(
        service,
        "_build_evolution_m9_records",
        lambda symbols: [
            {
                "symbol": symbols[0] if symbols else "600000.SH",
                "open": 10.0,
                "high": 10.3,
                "low": 9.8,
                "close": 10.1,
                "volume": 2_000_000,
            }
        ],
    )
    service.state.watchlist = ["600000.SH"]

    results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T20:40:00"))
    evo_result = _job_result(results, "evolution_offhours")
    assert _as_bool(evo_result["ran"]) is True
    assert _as_bool(evo_result["success"]) is True
    evo_payload = _as_mapping(evo_result["payload"])
    report = _as_mapping(evo_payload["report"])
    proposal = _as_mapping(report["proposal"])
    assert proposal["authorization_level"] == "C"
    assert service.latest_evolution_report() is not None


def test_evolution_offhours_refreshes_watchlist_from_broader_universe(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    config.week5.offhours_universe_refresh_enabled = True
    service = _new_service(config=config, tmp_path=tmp_path)
    service.state.watchlist = ["600000.SH"]

    def _fake_run_evolution_offhours(**_: object) -> dict[str, object]:
        return {"proposal": {"authorization_level": "C"}, "tdx_sync": {}}

    captured: dict[str, object] = {}

    def _fake_run_week5_scan(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"status": "ok", "watchlist_sync": {"updated": True}}

    _patch_attr(service, "run_evolution_offhours", _fake_run_evolution_offhours)
    _patch_attr(service, "run_week5_scan", _fake_run_week5_scan)

    payload = _as_mapping(service._job_evolution_offhours())

    assert payload["symbol_source"] == "watchlist"
    assert _as_bool(captured["force_universe_scan"]) is True
    assert _as_bool(captured["sync_watchlist"]) is True
    assert captured["sync_reason"] == "offhours_refresh"


def test_evolution_offhours_sends_learning_completion_notification(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config=config, tmp_path=tmp_path)
    service.state.watchlist = ["600000.SH"]

    def _fake_run_evolution_offhours(**_: object) -> dict[str, object]:
        return {
            "run_id": "evo-test-1",
            "source_trace_id": "scheduler-evolution",
            "proposal": {
                "proposal_id": "prop-test-1",
                "authorization_level": "C",
                "change_keys": ["threshold_shift", "watchlist_refresh"],
            },
            "runtime_controls": {
                "threshold_shift": 1.5,
                "position_multiplier": 0.9,
                "global_risk_delta": 2.0,
                "regime_hint": "trend_up",
                "reasons": ["m2_trend_up", "m10_healthy"],
            },
            "modules": {
                "m2": {"status": "ok", "active_state": "trend_up"},
                "m4": {"status": "inflow_dominant"},
                "m10": {"status": "healthy", "health_status": "healthy"},
            },
            "market_warehouse_sync": {"status": "ok"},
            "tdx_sync": {"status": "skipped"},
            "m9": {"success": True, "degraded": False, "frozen_symbols": []},
        }

    def _fake_run_week5_scan(**_: object) -> dict[str, object]:
        return {
            "status": "ok",
            "scan_profile": "offhours_weekday_light_topk_deep",
            "watchlist_sync": {
                "updated": True,
                "watchlist_after": 3,
                "symbols": ["600000.SH", "000001.SZ", "600519.SH"],
            },
            "signal_pool": {
                "ranking": {
                    "selected_count": 3,
                    "selected_symbols": ["600519.SH", "000001.SZ", "300750.SZ"],
                }
            },
        }

    captured: dict[str, object] = {}

    def _fake_notify_if_changed(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"status": "sent"}

    _patch_attr(service, "run_evolution_offhours", _fake_run_evolution_offhours)
    _patch_attr(service, "run_week5_scan", _fake_run_week5_scan)
    _patch_attr(service, "_notify_if_changed", _fake_notify_if_changed)

    payload = _as_mapping(service._job_evolution_offhours())
    content = str(captured.get("content", ""))
    title = str(captured.get("title", ""))
    dedup_key = str(captured.get("dedup_key", ""))

    assert payload["symbol_source"] == "watchlist"
    assert title.endswith("夜间学习完成")
    assert dedup_key.endswith(":offhours_weekday_light_topk_deep")
    assert "升级方面：" in content
    assert "学习结果：" in content
    assert "授权级别 C" in content
    assert "新增 2 只" in content
    assert "000001" in content
    assert "600519" in content
    assert "300750" in content


def test_evolution_offhours_keeps_weekend_silent(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config=config, tmp_path=tmp_path)
    service.state.watchlist = ["600000.SH"]

    def _fake_run_evolution_offhours(**_: object) -> dict[str, object]:
        return {
            "run_id": "evo-test-weekend",
            "source_trace_id": "scheduler-evolution",
            "proposal": {
                "proposal_id": "prop-test-weekend",
                "authorization_level": "C",
            },
        }

    def _fake_run_week5_scan(**_: object) -> dict[str, object]:
        return {
            "status": "ok",
            "scan_profile": "offhours_weekday_light_topk_deep",
            "watchlist_sync": {
                "updated": False,
                "watchlist_after": 1,
                "symbols": ["600000.SH"],
            },
        }

    captured: dict[str, object] = {}

    def _fake_notify_if_changed(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"status": "sent"}

    _patch_attr(service, "run_evolution_offhours", _fake_run_evolution_offhours)
    _patch_attr(service, "run_week5_scan", _fake_run_week5_scan)
    _patch_attr(service, "_notify_if_changed", _fake_notify_if_changed)

    payload = _as_mapping(
        service._job_evolution_offhours(now=datetime.fromisoformat("2026-03-07T20:40:00"))
    )

    assert payload["symbol_source"] == "watchlist"
    assert captured == {}
    events = _as_mapping(
        service.audit_events(limit=20, event_type="evolution_offhours_notify_suppressed_weekend")
    )
    assert _as_int(events["records"]) >= 1


def test_scheduler_runs_evolution_m3_maintenance_job(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = _new_service(config=config, tmp_path=tmp_path)
    _reset_evolution_orchestrator(service=service, config=config, tmp_path=tmp_path)

    results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T20:40:00"))
    offhours_result = _job_result(results, "evolution_offhours")
    assert _as_bool(offhours_result["ran"]) is True
    assert _as_bool(offhours_result["success"]) is True
    offhours_payload = _as_mapping(offhours_result["payload"])
    assert offhours_payload["symbol_source"] in {"bootstrap_seed", "empty"}
    if _as_int(offhours_payload["symbol_count"]) == 0:
        week5_refresh = _as_mapping(offhours_payload["week5_refresh"])
        assert week5_refresh["status"] == "skipped"
        assert week5_refresh["reason"] == "empty_symbols"

    maintenance_result = _job_result(results, "evolution_m3_maintenance")
    assert _as_bool(maintenance_result["ran"]) is True
    assert _as_bool(maintenance_result["success"]) is True
    maintenance_payload = _as_mapping(maintenance_result["payload"])
    payload = _as_mapping(maintenance_payload["report"])
    assert "purged_count" in payload


def test_evolution_dry_run_policy_auto_uses_runtime_mode(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    config.app.mode = "production"
    config.evolution.dry_run = True
    config.evolution.dry_run_policy = "auto"
    config.evolution.auto_run = False
    service = _new_service(config=config, tmp_path=tmp_path)
    _reset_evolution_orchestrator(service=service, config=config, tmp_path=tmp_path)

    report = _as_mapping(
        service.run_evolution_offhours(
            records=[
                {
                    "symbol": "600000.SH",
                    "open": 10.0,
                    "high": 10.2,
                    "low": 9.8,
                    "close": 10.1,
                    "volume": 1_000_000,
                }
            ],
            timestamp=datetime.fromisoformat("2026-03-02T20:40:00"),
            dry_run=None,
            source_trace_id="dry-run-policy-auto",
        )
    )
    assert _as_bool(report["dry_run"]) is False
    assert str(report.get("dry_run_resolved_by", "")).startswith("policy_auto_live:production")


def test_evolution_dry_run_request_override_has_priority(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    config.app.mode = "production"
    config.evolution.dry_run = True
    config.evolution.dry_run_policy = "auto"
    config.evolution.auto_run = False
    service = _new_service(config=config, tmp_path=tmp_path)
    _reset_evolution_orchestrator(service=service, config=config, tmp_path=tmp_path)

    report = _as_mapping(
        service.run_evolution_offhours(
            records=[
                {
                    "symbol": "600000.SH",
                    "open": 10.0,
                    "high": 10.2,
                    "low": 9.8,
                    "close": 10.1,
                    "volume": 1_000_000,
                }
            ],
            timestamp=datetime.fromisoformat("2026-03-02T20:40:00"),
            dry_run=True,
            source_trace_id="dry-run-policy-override",
        )
    )
    assert _as_bool(report["dry_run"]) is True
    assert report["dry_run_resolved_by"] == "request_override"


def test_evolution_offhours_generates_universe_snapshot_and_consistent_metadata(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    config.evolution.auto_run = False
    service = _new_service(config=config, tmp_path=tmp_path)
    _reset_evolution_orchestrator(service=service, config=config, tmp_path=tmp_path)

    report = _as_mapping(
        service.run_evolution_offhours(
            records=[
                {
                    "symbol": "600000.SH",
                    "open": 10.0,
                    "high": 10.2,
                    "low": 9.8,
                    "close": 10.1,
                    "volume": 1_000_000,
                },
                {
                    "symbol": "000001.SZ",
                    "open": 9.9,
                    "high": 10.0,
                    "low": 9.7,
                    "close": 9.8,
                    "volume": 1_200_000,
                },
            ],
            timestamp=datetime.fromisoformat("2026-03-02T20:40:00"),
            dry_run=True,
            source_trace_id="universe-snapshot",
        )
    )
    snapshot = _as_mapping(report["universe_snapshot"])
    snapshot_path = Path(str(snapshot["snapshot_path"]))
    assert snapshot_path.exists() is True
    payload = _as_mapping(json.loads(snapshot_path.read_text(encoding="utf-8")))
    assert payload["universe_snapshot_id"] == report["universe_snapshot_id"]
    assert payload["universe_spec_hash"] == report["universe_spec_hash"]
    assert payload["count"] == 2

    universe = _as_mapping(report["universe"])
    assert universe["status"] == "consistent"
    assert _as_bool(universe["consistent"]) is True
    assert _as_int(universe["missing_snapshot_rows"]) == 0
    assert universe["snapshot_id"] == report["universe_snapshot_id"]
    assert universe["universe_spec_hash"] == report["universe_spec_hash"]


def test_evolution_offhours_refreshes_tdx_before_run_when_enabled(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    config.evolution.auto_run = False
    config.tdx_sync.enabled = True
    config.tdx_sync.refresh_before_evolution = True
    config.tdx_sync.vipdoc_root = "D:/mock-vipdoc"
    service = _new_service(config=config, tmp_path=tmp_path)
    _reset_evolution_orchestrator(service=service, config=config, tmp_path=tmp_path)

    calls: list[dict[str, object]] = []

    def _fake_sync(**kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        return {"status": "ok", "reason": "force", "trace_id": kwargs.get("source_trace_id", "")}

    _patch_attr(service, "run_tdx_offline_sync", _fake_sync)

    report = _as_mapping(
        service.run_evolution_offhours(
            records=[
                {
                    "symbol": "600000.SH",
                    "open": 10.0,
                    "high": 10.2,
                    "low": 9.8,
                    "close": 10.1,
                    "volume": 1_000_000,
                }
            ],
            timestamp=datetime.fromisoformat("2026-03-02T20:40:00"),
            dry_run=True,
            source_trace_id="tdx-pre-sync",
        )
    )
    tdx_sync = _as_mapping(report["tdx_sync"])

    assert len(calls) == 1
    assert calls[0]["source_trace_id"] == "tdx-pre-sync"
    assert tdx_sync["status"] == "ok"


def test_evolution_offhours_refreshes_market_warehouse_before_run_when_enabled(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    config.evolution.auto_run = False
    config.market_warehouse.enabled = True
    config.market_warehouse.refresh_before_evolution = True
    service = _new_service(config=config, tmp_path=tmp_path)
    _reset_evolution_orchestrator(service=service, config=config, tmp_path=tmp_path)

    calls: list[dict[str, object]] = []

    def _fake_sync(**kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        return {
            "status": "ok",
            "reason": "online_incremental",
            "trace_id": kwargs.get("source_trace_id", ""),
        }

    _patch_attr(service, "run_market_warehouse_sync", _fake_sync)

    report = _as_mapping(
        service.run_evolution_offhours(
            records=[
                {
                    "symbol": "600000.SH",
                    "open": 10.0,
                    "high": 10.2,
                    "low": 9.8,
                    "close": 10.1,
                    "volume": 1_000_000,
                }
            ],
            timestamp=datetime.fromisoformat("2026-03-02T20:40:00"),
            dry_run=True,
            source_trace_id="warehouse-pre-sync",
        )
    )
    market_warehouse_sync = _as_mapping(report["market_warehouse_sync"])

    assert len(calls) == 1
    assert calls[0]["source_trace_id"] == "warehouse-pre-sync"
    assert market_warehouse_sync["status"] == "ok"
