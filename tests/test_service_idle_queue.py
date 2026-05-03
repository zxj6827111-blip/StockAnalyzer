from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pandas as pd
import pytest

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.runtime.service import StockAnalyzerService


class _MockProvider:
    def fetch_daily_bars(self, symbol: str, lookback_days: int) -> pd.DataFrame:
        return _mock_fetch_daily_bars(symbol=symbol, lookback_days=lookback_days)


def _load_test_config(tmp_path: Path) -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.scheduler.premarket_time = "08:30"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "23:59"
    config.week5.auto_run = False
    config.week6.auto_run = False
    config.evolution.auto_run = False
    config.cloud_backup.enabled = False
    config.acceptance.auto_run = False
    config.idle_queue.enabled = True
    config.idle_queue.auto_run = False
    config.idle_queue.dispatch_interval_minutes = 5
    config.idle_queue.output_root = str(tmp_path / "staging" / "idle_cache")
    config.idle_queue.history_persist_path = str(
        tmp_path / "staging" / "idle_cache" / "_meta" / "idle_history.jsonl"
    )
    config.idle_queue.retention_days_weekend = 1
    return config


def _load_idle_policy_config(tmp_path: Path) -> StockAnalyzerConfig:
    config = _load_test_config(tmp_path)
    config.idle_queue.enabled = False
    config.idle_queue.auto_run = False
    config.idle_queue.enabled_policy = "auto"
    config.idle_queue.auto_run_policy = "auto"
    return config


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    raise AssertionError(f"Expected mapping, got {type(value).__name__}")


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


def _mock_fetch_daily_bars(symbol: str, lookback_days: int) -> pd.DataFrame:
    _ = symbol
    rows = max(40, int(lookback_days))
    records: list[dict[str, object]] = []
    for idx in range(rows):
        base = 10.0 + idx * 0.01
        records.append(
            {
                "open": base,
                "high": base * 1.01,
                "low": base * 0.99,
                "close": base * 1.002,
                "volume": 2_000_000 + idx * 1000,
                "financial_data_complete": True,
                "roe": 0.12,
                "debt_ratio": 0.42,
                "holder_count": 100_000 - idx * 5,
                "block_trade_net": 1_000_000 + idx * 2000,
                "financing_balance": 3_000_000 + idx * 3000,
                "northbound_net": 2_000_000 + idx * 1000,
                "dragon_tiger_flag": 0,
                "background_data_complete": True,
                "financial_source": "mock",
                "background_data_source": "mock",
                "financial_missing_fields": "",
            }
        )
    return pd.DataFrame(records)


def test_idle_queue_auto_policy_enables_simulation_mode_by_default(tmp_path: Path) -> None:
    config = _load_idle_policy_config(tmp_path)
    service = StockAnalyzerService(config=config)

    enabled, enabled_reason = service._resolve_idle_queue_enabled()
    auto_run, auto_run_reason = service._resolve_idle_queue_auto_run()

    assert enabled is True
    assert enabled_reason == "policy_mode:simulation"
    assert auto_run is True
    assert auto_run_reason == "policy_mode:simulation"


def test_idle_queue_auto_policy_empty_modes_fallback_to_simulation_and_staging(
    tmp_path: Path,
) -> None:
    config = _load_idle_policy_config(tmp_path)
    config.idle_queue.enabled_modes = []
    config.idle_queue.auto_run_modes = []
    service = StockAnalyzerService(config=config)

    enabled, enabled_reason = service._resolve_idle_queue_enabled()
    auto_run, auto_run_reason = service._resolve_idle_queue_auto_run()

    assert enabled is True
    assert enabled_reason == "policy_mode:simulation"
    assert auto_run is True
    assert auto_run_reason == "policy_mode:simulation"


def test_idle_queue_auto_policy_keeps_production_disabled_without_canary(
    tmp_path: Path,
) -> None:
    config = _load_idle_policy_config(tmp_path)
    config.app.mode = "production"
    service = StockAnalyzerService(config=config)

    enabled, enabled_reason = service._resolve_idle_queue_enabled()
    auto_run, auto_run_reason = service._resolve_idle_queue_auto_run()

    assert enabled is False
    assert enabled_reason == "policy_mode_off:production"
    assert auto_run is False
    assert auto_run_reason == "disabled_effective:policy_mode_off:production"


def test_idle_queue_runs_wd_p0_01_and_writes_report(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)
    service.state.watchlist = ["600000.SH"]
    _patch_attr(service, "_provider", _MockProvider())

    report = service.run_idle_queue_cycle(now=datetime.fromisoformat("2026-03-02T20:40:00"))
    assert report["status"] == "ran"
    assert report["task_id"] == "WD-P0-01"
    assert report["task_status"] in {"ok", "degraded"}

    output = (
        Path(config.idle_queue.output_root)
        / "20260302"
        / "WD-P0-01"
        / "data_quality"
        / "report.json"
    )
    assert output.exists()


def test_idle_queue_runs_wd_report_after_deadline_trigger(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)
    service.state.watchlist = ["600000.SH"]
    _patch_attr(service, "_provider", _MockProvider())

    first = service.run_idle_queue_cycle(now=datetime.fromisoformat("2026-03-02T20:40:00"))
    assert first["task_id"] == "WD-P0-01"

    second = service.run_idle_queue_cycle(now=datetime.fromisoformat("2026-03-03T08:24:00"))
    assert second["status"] == "ran"
    assert second["task_id"] == "WD-REPORT"
    assert second["task_status"] in {"ok", "degraded"}

    report_json = (
        Path(config.idle_queue.output_root)
        / "20260302"
        / "WD-REPORT"
        / "morning_brief"
        / "morning_brief.json"
    )
    report_md = (
        Path(config.idle_queue.output_root)
        / "20260302"
        / "WD-REPORT"
        / "morning_brief"
        / "morning_brief.md"
    )
    assert report_json.exists()
    assert report_md.exists()


def test_idle_queue_weekend_cleanup_removes_stale_dirs(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)
    service._evolution_project_root = tmp_path

    stale_dir = Path(config.idle_queue.output_root) / "20250101" / "WD-P0-01"
    stale_dir.mkdir(parents=True, exist_ok=True)
    (stale_dir / "dummy.txt").write_text("x", encoding="utf-8")

    report: dict[str, object] = {}
    for step in range(20):
        report = service.run_idle_queue_cycle(
            now=datetime.fromisoformat("2026-03-07T13:00:00") + timedelta(minutes=step * 5)
        )
        if report.get("task_id") == "WE-P2-08":
            break
    assert report.get("task_id") == "WE-P2-08"
    assert report.get("task_status") in {"ok", "partial_clean"}
    assert not stale_dir.exists()


def test_idle_queue_scheduler_interval_job_runs(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    config.idle_queue.auto_run = True
    config.scheduler.premarket_time = "23:59"
    config.scheduler.auction_report_time = "23:59"
    config.scheduler.close_reconcile_time = "23:59"
    config.scheduler.week4_acceptance_time = "23:59"
    config.market_warehouse.enabled = False
    config.market_warehouse.auto_run = False
    service = StockAnalyzerService(config=config)
    service.state.watchlist = ["600000.SH"]
    _patch_attr(service, "_provider", _MockProvider())

    results = service.run_due_jobs(now=datetime.fromisoformat("2026-03-02T20:40:00"))
    idle_jobs = [item for item in results if str(item.get("job", "")) == "idle_queue_tick"]
    assert len(idle_jobs) == 1
    assert idle_jobs[0]["ran"] is True
    assert idle_jobs[0]["success"] is True


def test_idle_queue_workday_task_sequence_progresses(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)
    service.state.watchlist = ["600000.SH", "000001.SZ"]
    _patch_attr(service, "_provider", _MockProvider())

    r1 = service.run_idle_queue_cycle(now=datetime.fromisoformat("2026-03-02T20:40:00"))
    r2 = service.run_idle_queue_cycle(now=datetime.fromisoformat("2026-03-02T20:45:00"))
    r3 = service.run_idle_queue_cycle(now=datetime.fromisoformat("2026-03-02T20:50:00"))
    r4 = service.run_idle_queue_cycle(now=datetime.fromisoformat("2026-03-02T20:55:00"))
    r5 = service.run_idle_queue_cycle(now=datetime.fromisoformat("2026-03-02T21:00:00"))
    r6 = service.run_idle_queue_cycle(now=datetime.fromisoformat("2026-03-02T21:05:00"))
    r7 = service.run_idle_queue_cycle(now=datetime.fromisoformat("2026-03-02T21:10:00"))

    assert r1.get("task_id") == "WD-P0-01"
    assert r2.get("task_id") == "WD-P0-02"
    assert r3.get("task_id") == "WD-P0-03"
    assert r4.get("task_id") == "WD-P0-04"
    assert r5.get("task_id") == "WD-P1-05"
    assert r6.get("task_id") == "WD-P1-06"
    assert r7.get("task_id") == "WD-P1-07"


def test_idle_queue_weekend_task_sequence_progresses(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)
    service._evolution_project_root = tmp_path
    service._idle_task_manifests["WE-P2-08"]["force_run_on_disk_usage_pct"] = 100.0
    service.state.watchlist = ["600000.SH", "000001.SZ"]
    _patch_attr(service, "_provider", _MockProvider())

    mock_root = tmp_path / "staging" / "mock_history"
    mock_root.mkdir(parents=True, exist_ok=True)
    (mock_root / "day_01.csv").write_text("x", encoding="utf-8")

    suggestions_root = tmp_path / "suggestions"
    suggestions_root.mkdir(parents=True, exist_ok=True)
    (suggestions_root / "proposal_01.json").write_text('{"id":"p1"}', encoding="utf-8")
    challengers_root = suggestions_root / "challengers"
    challengers_root.mkdir(parents=True, exist_ok=True)
    (challengers_root / "c1.json").write_text('{"name":"c1"}', encoding="utf-8")

    backup_root = tmp_path / "artifacts" / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    (backup_root / "backup_01.json").write_text('{"ok":true}', encoding="utf-8")

    reports = [
        service.run_idle_queue_cycle(
            now=datetime.fromisoformat("2026-03-07T13:00:00") + timedelta(minutes=step * 5)
        )
        for step in range(8)
    ]
    task_ids = [str(item.get("task_id", "")) for item in reports]
    assert task_ids == [
        "WE-P0-01",
        "WE-P0-02",
        "WE-LEARN-01",
        "WE-P1-03",
        "WE-P1-04",
        "WE-P1-05",
        "WE-P1-06",
        "WE-P1-07",
    ]

    output_root = Path(config.idle_queue.output_root) / "20260306"
    assert (output_root / "WE-P0-01" / "soak_test" / "soak_report.json").exists()
    assert (output_root / "WE-P0-02" / "reproducibility" / "audit_report.json").exists()
    assert (output_root / "WE-LEARN-01" / "model_learning" / "learning_report.json").exists()
    assert (output_root / "WE-P1-03" / "rolling_backtest" / "rolling_ir_drift.json").exists()
    assert (output_root / "WE-P1-04" / "counterfactual" / "counterfactual_report.json").exists()
    assert (output_root / "WE-P1-05" / "multi_seed" / "seed_stability_report.json").exists()
    assert (output_root / "WE-P1-06" / "cost_sensitivity" / "cost_sensitivity_report.json").exists()
    assert (output_root / "WE-P1-07" / "disaster_recovery" / "dr_report.json").exists()


def test_idle_queue_weekend_rotation_prioritizes_unfinished_p1_tasks(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)
    service._evolution_project_root = tmp_path
    service._idle_task_manifests["WE-P2-08"]["force_run_on_disk_usage_pct"] = 100.0
    service.state.watchlist = ["600000.SH"]
    _patch_attr(service, "_provider", _MockProvider())

    first_weekend = [
        service.run_idle_queue_cycle(
            now=datetime.fromisoformat("2026-03-07T13:00:00") + timedelta(minutes=step * 5)
        )
        for step in range(4)
    ]
    assert [item.get("task_id") for item in first_weekend] == [
        "WE-P0-01",
        "WE-P0-02",
        "WE-LEARN-01",
        "WE-P1-03",
    ]

    second_weekend = [
        service.run_idle_queue_cycle(
            now=datetime.fromisoformat("2026-03-14T13:00:00") + timedelta(minutes=step * 5)
        )
        for step in range(3)
    ]
    assert [item.get("task_id") for item in second_weekend[:2]] == ["WE-P0-01", "WE-P0-02"]
    assert second_weekend[2].get("task_id") == "WE-P1-04"


def test_idle_queue_we_learn_01_dispatch_uses_learning_governance_chain(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    config.auto_promotion.notify_on_training_summary = False
    service = StockAnalyzerService(config=config)
    service._evolution_project_root = tmp_path
    service.state.watchlist = ["600000.SH", "000001.SZ"]
    service._idle_task_manifests["WE-LEARN-01"]["symbol_cap"] = 1
    _patch_attr(service, "_provider", _MockProvider())
    _patch_attr(
        service,
        "latest_market_warehouse_report",
        lambda: {
            "status": "ok",
            "timestamp": "2026-03-06T21:45:00",
            "trade_date": "20260306",
        },
    )

    calls: list[tuple[str, dict[str, object]]] = []

    def _fake_build_learning_trainable_manifest(**kwargs: object) -> dict[str, object]:
        calls.append(("build", dict(kwargs)))
        return {
            "ok": True,
            "mode": "build_trainable_manifest",
            "dataset_manifest_id": "manifest-01",
            "included_snapshot_count": 8,
            "included_outcome_count": 6,
            "errors": [],
        }

    def _fake_run_learning_manifest_shadow_proposal(**kwargs: object) -> dict[str, object]:
        calls.append(("proposal", dict(kwargs)))
        return {
            "ok": True,
            "mode": "learning_manifest_shadow_proposal",
            "status": "generated",
            "accepted": True,
            "dataset_manifest_id": "manifest-01",
            "shadow_model_id": "shadow-01",
            "champion_model_id": "champion-01",
            "workflow": {
                "ok": True,
                "status": "pass",
                "promotion_gate": {"status": "pass"},
                "shadow_validation": {
                    "ok": True,
                    "training": {
                        "ok": True,
                        "predictor_loaded": True,
                        "artifact_path": "artifacts/evolution/shadow.json",
                    },
                },
            },
            "proposal": {
                "proposal_id": "LRN-PROP-01",
                "status": "generated",
                "gate_status": "pass",
            },
            "auto_promotion": {
                "enabled": False,
                "predictor_loaded": True,
                "ticket_id": "",
            },
            "errors": [],
        }

    _patch_attr(service, "build_learning_trainable_manifest", _fake_build_learning_trainable_manifest)
    _patch_attr(
        service,
        "run_learning_manifest_shadow_proposal",
        _fake_run_learning_manifest_shadow_proposal,
    )

    result = service._idle_task_we_learn_01(
        context={
            "trade_date": "20260306",
            "now": "2026-03-07T13:00:00",
        }
    )

    output = (
        Path(config.idle_queue.output_root)
        / "20260306"
        / "WE-LEARN-01"
        / "model_learning"
        / "learning_report.json"
    )
    payload = _as_mapping(json.loads(output.read_text(encoding="utf-8")))

    assert result["status"] == "ok"
    assert result["reason"] == "learning_shadow_proposal_completed"
    assert [name for name, _ in calls] == ["build", "proposal"]
    build_symbols = _as_text_list(calls[0][1].get("symbols"))
    assert len(build_symbols) == 1
    assert build_symbols[0]
    assert payload["dataset_manifest_id"] == "manifest-01"
    assert payload["proposal_id"] == "LRN-PROP-01"
    assert payload["symbol_cap"] == 1
    assert payload["manifest_symbol_count"] == 1
    assert payload["online_effect"] == "predictor_reloaded"
    assert payload["blocked_after_run"] is False
    assert _as_mapping(payload["gates"])["market_warehouse"]["ok"] is True
    assert _as_mapping(payload["gates"])["sample_maturity"]["ok"] is True


def test_idle_queue_we_learn_01_blocks_stale_market_warehouse_report(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    config.auto_promotion.notify_on_training_summary = False
    service = StockAnalyzerService(config=config)
    service._evolution_project_root = tmp_path
    service.state.watchlist = ["600000.SH"]
    _patch_attr(
        service,
        "latest_market_warehouse_report",
        lambda: {
            "status": "ok",
            "timestamp": "2026-03-05T21:45:00",
            "trade_date": "20260305",
        },
    )

    def _unexpected_build_learning_trainable_manifest(**kwargs: object) -> dict[str, object]:
        _ = kwargs
        pytest.fail("stale market warehouse report should block manifest build")

    def _unexpected_run_learning_manifest_shadow_proposal(**kwargs: object) -> dict[str, object]:
        _ = kwargs
        pytest.fail("stale market warehouse report should block proposal generation")

    _patch_attr(
        service,
        "build_learning_trainable_manifest",
        _unexpected_build_learning_trainable_manifest,
    )
    _patch_attr(
        service,
        "run_learning_manifest_shadow_proposal",
        _unexpected_run_learning_manifest_shadow_proposal,
    )

    result = service._idle_task_we_learn_01(
        context={
            "trade_date": "20260306",
            "now": "2026-03-07T13:00:00",
        }
    )

    output = (
        Path(config.idle_queue.output_root)
        / "20260306"
        / "WE-LEARN-01"
        / "model_learning"
        / "learning_report.json"
    )
    payload = _as_mapping(json.loads(output.read_text(encoding="utf-8")))
    gates = _as_mapping(payload["gates"])
    market_gate = _as_mapping(gates["market_warehouse"])

    assert result["status"] == "skipped"
    assert result["reason"] == "skipped: market_warehouse_gate_failed"
    assert payload["blocked_after_run"] is True
    assert market_gate["ok"] is False
    assert market_gate["reason"] == "stale_market_warehouse_report"
    assert market_gate["trade_date"] == "20260305"
    assert market_gate["expected_trade_date"] == "20260306"


def test_idle_queue_we_learn_01_respects_min_interval_and_min_remaining_budget(
    tmp_path: Path,
) -> None:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)
    service._evolution_project_root = tmp_path
    _patch_attr(service, "_provider", _MockProvider())

    service._idle_mark_ran(task_id="WE-LEARN-01", trade_date="20260310")

    skipped_interval = service._idle_task_we_learn_01(
        context={
            "trade_date": "20260313",
            "now": "2026-03-14T13:00:00",
        }
    )

    assert skipped_interval["status"] == "skipped"
    assert skipped_interval["reason"] == "skipped: min_interval_not_reached"

    budget_service = StockAnalyzerService(config=_load_test_config(tmp_path))
    budget_service._evolution_project_root = tmp_path
    _patch_attr(budget_service, "_provider", _MockProvider())

    skipped_budget = budget_service._idle_task_we_learn_01(
        context={
            "trade_date": "20260313",
            "now": "2026-03-13T23:30:00",
        }
    )

    assert skipped_budget["status"] == "skipped"
    assert skipped_budget["reason"] == "skipped: insufficient_weekend_time_budget"


def test_idle_queue_weekend_p2_defer_then_force_run(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)
    service._evolution_project_root = tmp_path

    trade_date = "20260306"
    for task_id in [
        "WE-P0-01",
        "WE-P0-02",
        "WE-LEARN-01",
        "WE-P1-03",
        "WE-P1-04",
        "WE-P1-05",
        "WE-P1-06",
        "WE-P1-07",
    ]:
        service._idle_mark_ran(task_id=task_id, trade_date=trade_date)

    first = service.run_idle_queue_cycle(now=datetime.fromisoformat("2026-03-08T23:30:00"))
    assert first["status"] == "idle"
    assert service._idle_get_task_status("WE-P2-08", trade_date) == "deferred"

    service._idle_weekend_defer_runs["WE-P2-08"] = 2
    second = service.run_idle_queue_cycle(now=datetime.fromisoformat("2026-03-08T23:31:00"))
    assert second["status"] == "ran"
    assert second["task_id"] == "WE-P2-08"


def test_idle_queue_write_policy_blocks_forbidden_and_allows_whitelist(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)
    service._evolution_project_root = tmp_path

    with pytest.raises(ValueError):
        service._idle_assert_write_allowed(
            task_id="WD-P0-01",
            path=tmp_path / "artifacts" / "forbidden.txt",
            action="write",
        )

    service._idle_assert_write_allowed(
        task_id="WE-P2-08",
        path=tmp_path / "artifacts" / "faiss_snapshots" / "archive.bin",
        action="delete_via_queue",
    )


def test_idle_queue_manual_ack_unblocks_after_success(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    config.idle_queue.fallback_ttl_workday_runs = 1
    config.idle_queue.unblock_after_consecutive_success_runs = 1
    service = StockAnalyzerService(config=config)
    service.state.watchlist = ["600000.SH"]
    _patch_attr(service, "_provider", _MockProvider())

    call_count = {"wd_p0_01": 0}

    def _fake_run_idle_task(task_id: str, context: dict[str, object]) -> dict[str, object]:
        _ = context
        if task_id != "WD-P0-01":
            return {"status": "ok", "output_files": []}
        call_count["wd_p0_01"] += 1
        if call_count["wd_p0_01"] == 1:
            return {"status": "fallback", "output_files": []}
        return {"status": "ok", "output_files": []}

    _patch_attr(service, "_run_idle_task", _fake_run_idle_task)

    first = service.run_idle_queue_cycle(now=datetime.fromisoformat("2026-03-02T20:40:00"))
    assert first["task_id"] == "WD-P0-01"
    assert first["task_status"] == "fallback"
    assert "WD-P0-01" in service._idle_blocked_tasks
    blocked_events = _as_mapping(
        service.audit_events(limit=20, event_type="idle_queue_task_blocked")
    )
    assert _as_int(blocked_events["records"]) >= 1

    second = service.run_idle_queue_cycle(now=datetime.fromisoformat("2026-03-03T20:40:00"))
    assert second["status"] == "idle"
    assert second["detail"] == "all_due_tasks_skipped"

    ack = _as_mapping(service.idle_queue_ack_blocked(task_id="WD-P0-01"))
    assert ack["status"] == "ok"
    assert "WD-P0-01" in _as_text_list(ack["acked"])
    ack_events = _as_mapping(service.audit_events(limit=20, event_type="idle_queue_blocked_ack"))
    assert _as_int(ack_events["records"]) >= 1
    state_after_ack = _as_mapping(service.idle_queue_state())
    assert "WD-P0-01" in _as_mapping(state_after_ack["blocked_tasks"])
    assert "WD-P0-01" not in _as_text_list(state_after_ack["pending_manual_ack"])

    third = service.run_idle_queue_cycle(now=datetime.fromisoformat("2026-03-03T20:45:00"))
    assert third["status"] == "ran"
    assert third["task_id"] == "WD-P0-01"
    assert third["task_status"] == "ok"
    assert "WD-P0-01" not in service._idle_blocked_tasks
    unblocked_events = _as_mapping(
        service.audit_events(limit=20, event_type="idle_queue_task_unblocked")
    )
    assert _as_int(unblocked_events["records"]) >= 1


def test_idle_queue_state_notifications_are_suppressed_on_weekend(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    config.idle_queue.manual_ack_required = True
    config.idle_queue.fallback_ttl_workday_runs = 1
    config.idle_queue.unblock_after_consecutive_success_runs = 1
    service = StockAnalyzerService(config=config)

    captured: list[dict[str, object]] = []

    def _fake_notify(**kwargs: object) -> dict[str, object]:
        captured.append(dict(kwargs))
        return {"status": "sent"}

    _patch_attr(service, "notify", _fake_notify)

    weekend_now = datetime.fromisoformat("2026-03-07T20:40:00")
    service._idle_update_task_health(task_id="WD-P0-01", status="fallback", now=weekend_now)
    ack = _as_mapping(service.idle_queue_ack_blocked(task_id="WD-P0-01", now=weekend_now))
    service._idle_update_task_health(task_id="WD-P0-01", status="ok", now=weekend_now)

    assert ack["status"] == "ok"
    assert "WD-P0-01" in _as_text_list(ack["acked"])
    assert captured == []
    events = _as_mapping(
        service.audit_events(limit=20, event_type="idle_queue_state_notify_suppressed_weekend")
    )
    assert _as_int(events["records"]) >= 3


def test_idle_queue_history_is_persisted_and_reloaded(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)
    service.state.watchlist = ["600000.SH"]
    _patch_attr(service, "_provider", _MockProvider())

    run_report = service.run_idle_queue_cycle(now=datetime.fromisoformat("2026-03-02T20:40:00"))
    assert run_report["status"] == "ran"

    service_reloaded = StockAnalyzerService(config=config)
    history = _as_mapping(service_reloaded.idle_queue_history(limit=5))
    assert _as_int(history["records"]) >= 1
    assert isinstance(service_reloaded.latest_idle_queue_report(), dict)


def test_idle_queue_report_includes_capacity_metrics(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)
    service.state.watchlist = ["600000.SH"]
    _patch_attr(service, "_provider", _MockProvider())

    report = service.run_idle_queue_cycle(now=datetime.fromisoformat("2026-03-02T20:40:00"))
    metrics = report.get("capacity_metrics")
    assert isinstance(metrics, dict)
    assert "staging_size_bytes" in metrics
    assert "daily_growth_sla_ok" in metrics


def test_idle_queue_time_guard_mismatch_disables_queue(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    config.scheduler.premarket_time = "08:45"
    service = StockAnalyzerService(config=config)
    service.state.watchlist = ["600000.SH"]
    _patch_attr(service, "_provider", _MockProvider())

    report = service.run_idle_queue_cycle(now=datetime.fromisoformat("2026-03-02T20:40:00"))
    assert report["status"] == "disabled"
    assert report["detail"] == "time_guard_sync_mismatch"
    assert config.idle_queue.enabled is False


def test_idle_queue_state_reports_blocked_metadata(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    config.idle_queue.unblock_after_consecutive_success_runs = 2
    service = StockAnalyzerService(config=config)
    service._idle_blocked_tasks["WD-P0-01"] = "fallback_streak=2"
    service._idle_blocked_since["WD-P0-01"] = "2026-03-02T21:00:00"
    service._idle_success_streak["WD-P0-01"] = 1
    service._idle_manual_ack_grants["WD-P0-01"] = "2026-03-02T21:05:00"

    state = service.idle_queue_state()
    blocked = state.get("blocked_tasks", {})
    assert isinstance(blocked, dict)
    assert "WD-P0-01" in blocked
    item = blocked["WD-P0-01"]
    assert item["reason"] == "fallback_streak=2"
    assert (
        item["reason_detail"]
        == "任务连续 2 轮进入回退/降级结果（触发阈值 2 轮），系统已暂停自动执行，需人工确认后再恢复观察"
    )
    assert item["blocked_since"] == "2026-03-02T21:00:00"
    progress = item["recovery_progress"]
    assert progress["required_success_runs"] == 2
    assert progress["current_success_streak"] == 1
    assert progress["remaining_success_runs"] == 1
    assert progress["manual_ack_granted"] is True
    assert progress["eligible_to_unblock"] is False


def test_idle_queue_blocked_notification_explains_reason(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)

    title, content, level = service._idle_notification_template(  # noqa: SLF001
        event="blocked",
        payload={
            "task_id": "WD-P1-05",
            "reason": "fallback_streak=2",
            "fallback_streak": 2,
            "ttl_runs": 2,
        },
    )

    assert title == "[空闲队列][阻塞] WD-P1-05"
    assert level == "warn"
    assert "原因=fallback_streak=2" in content
    assert (
        "含义=任务连续 2 轮进入回退/降级结果（触发阈值 2 轮），系统已暂停自动执行，需人工确认后再恢复观察"
        in content
    )
    assert "处理建议=需要人工确认" in content


def test_idle_queue_resource_pause_uses_hysteresis(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    config.idle_queue.resource_pause_enabled = True
    config.idle_queue.resource_pause_high_watermark_pct = 88.0
    config.idle_queue.resource_pause_low_watermark_pct = 82.0
    service = StockAnalyzerService(config=config)
    service.state.watchlist = ["600000.SH"]
    _patch_attr(service, "_provider", _MockProvider())

    usage_values = [90.0, 85.0, 81.0]

    def _fake_capacity_metrics(context: dict[str, object], now: datetime) -> dict[str, object]:
        _ = (context, now)
        usage = usage_values.pop(0) if usage_values else 81.0
        return {
            "staging_size_bytes": 0,
            "daily_growth_bytes": 0,
            "daily_growth_sla_bytes": 1,
            "daily_growth_sla_ok": True,
            "weekend_growth_bytes": 0,
            "weekend_growth_sla_bytes": 1,
            "weekend_growth_sla_ok": True,
            "resource_metric": "disk_usage_pct",
            "disk_total_bytes": 100,
            "disk_used_bytes": int(usage),
            "disk_usage_pct": usage,
            "resource_pause_high_watermark_pct": 88.0,
            "resource_pause_low_watermark_pct": 82.0,
        }

    _patch_attr(service, "_idle_collect_capacity_metrics", _fake_capacity_metrics)

    first = service.run_idle_queue_cycle(now=datetime.fromisoformat("2026-03-02T20:40:00"))
    assert first["status"] == "paused"
    second = service.run_idle_queue_cycle(now=datetime.fromisoformat("2026-03-02T20:45:00"))
    assert second["status"] == "paused"
    third = service.run_idle_queue_cycle(now=datetime.fromisoformat("2026-03-02T20:50:00"))
    assert third["status"] == "ran"
    assert third["task_id"] == "WD-P0-01"

    state = _as_mapping(service.idle_queue_state())
    resource_pause = state.get("resource_pause", {})
    assert isinstance(resource_pause, dict)
    assert resource_pause.get("active") is False
    events = _as_mapping(service.audit_events(limit=50, event_type="idle_queue_pause_flag_changed"))
    assert _as_int(events["records"]) >= 2


def test_idle_execute_task_timeout_writes_partial_report(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)
    service._idle_task_manifests["WD-P0-01"]["max_wall_time_minutes"] = 1

    def _always_timeout(
        task_id: str, context: dict[str, object], timeout_seconds: float | None
    ) -> tuple[dict[str, object], bool, float]:
        _ = (task_id, context, timeout_seconds)
        return ({"status": "timeout", "reason": "task_timeout", "output_files": []}, True, 0.01)

    _patch_attr(service, "_idle_run_task_with_timeout", _always_timeout)
    context = service._build_idle_context(datetime.fromisoformat("2026-03-02T20:40:00"))
    result = service._idle_execute_task_with_policy(task_id="WD-P0-01", context=context)
    assert result["status"] == "timeout"
    assert result["error_code"] == "task_timeout"
    outputs = result.get("output_files", [])
    assert isinstance(outputs, list)
    assert any(str(path).endswith("partial_timeout_report.json") for path in outputs)


def test_idle_execute_task_retries_only_on_transient_codes(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)
    service._idle_task_manifests["WD-P0-01"]["retry_policy"] = {
        "max_retries": 1,
        "retry_delay_seconds": 0,
        "retry_only_on": ["network_timeout"],
        "no_retry_on": ["data_unavailable"],
    }
    context = service._build_idle_context(datetime.fromisoformat("2026-03-02T20:40:00"))

    attempts = {"count": 0}

    def _transient_then_ok(
        task_id: str, context: dict[str, object], timeout_seconds: float | None
    ) -> tuple[dict[str, object], bool, float]:
        _ = (task_id, context, timeout_seconds)
        attempts["count"] += 1
        if attempts["count"] == 1:
            return (
                {
                    "status": "error",
                    "reason": "network timeout while reading cache",
                    "output_files": [],
                },
                False,
                0.01,
            )
        return ({"status": "ok", "output_files": []}, False, 0.01)

    _patch_attr(service, "_idle_run_task_with_timeout", _transient_then_ok)
    result = service._idle_execute_task_with_policy(task_id="WD-P0-01", context=context)
    assert result["status"] == "ok"
    assert result["retry_attempts"] == 1
    assert attempts["count"] == 2

    attempts["count"] = 0

    def _deterministic_error(
        task_id: str, context: dict[str, object], timeout_seconds: float | None
    ) -> tuple[dict[str, object], bool, float]:
        _ = (task_id, context, timeout_seconds)
        attempts["count"] += 1
        return (
            {
                "status": "error",
                "reason": "data_unavailable",
                "output_files": [],
            },
            False,
            0.01,
        )

    _patch_attr(service, "_idle_run_task_with_timeout", _deterministic_error)
    result_no_retry = service._idle_execute_task_with_policy(task_id="WD-P0-01", context=context)
    assert result_no_retry["status"] == "error"
    assert result_no_retry["retry_attempts"] == 0
    assert attempts["count"] == 1


def test_idle_wd_report_records_fallback_provenance_and_kpi(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)
    service.state.watchlist = ["600000.SH"]
    _patch_attr(service, "_provider", _MockProvider())

    stale_path = (
        Path(config.idle_queue.output_root)
        / "20260301"
        / "WD-P0-02"
        / "failure_analysis"
        / "loss_attribution.json"
    )
    stale_path.parent.mkdir(parents=True, exist_ok=True)
    stale_path.write_text(
        '{"status":"fallback","reason":"use_last_successful"}',
        encoding="utf-8",
    )

    first = service.run_idle_queue_cycle(now=datetime.fromisoformat("2026-03-02T20:40:00"))
    assert first["task_id"] == "WD-P0-01"
    second = service.run_idle_queue_cycle(now=datetime.fromisoformat("2026-03-03T08:24:00"))
    assert second["task_id"] == "WD-REPORT"

    report_json = (
        Path(config.idle_queue.output_root)
        / "20260302"
        / "WD-REPORT"
        / "morning_brief"
        / "morning_brief.json"
    )
    report_payload = json.loads(report_json.read_text(encoding="utf-8"))
    assert report_payload["sections_total"] >= 1
    assert "completeness_ratio" in report_payload
    fallback_items = report_payload.get("fallback_provenance", [])
    assert isinstance(fallback_items, list)
    assert any(str(item.get("task_id", "")) == "WD-P0-02" for item in fallback_items)

    state = _as_mapping(service.idle_queue_state())
    kpi = state.get("wd_report_kpi", {})
    assert isinstance(kpi, dict)
    assert kpi.get("runs", 0) >= 1
    assert kpi.get("deadline_hits", 0) == 0


def test_idle_policy_blocks_path_traversal_and_non_whitelisted_operation(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    service = StockAnalyzerService(config=config)
    service._evolution_project_root = tmp_path

    with pytest.raises(ValueError):
        service._idle_output_path(
            trade_date="20260302",
            task_id="WD-P0-01",
            subdir="../escape",
            filename="a.json",
        )

    with pytest.raises(ValueError):
        service._idle_output_path(
            trade_date="20260302",
            task_id="WD-P0-01",
            subdir="data_quality",
            filename="../a.json",
        )

    with pytest.raises(ValueError):
        service._idle_assert_write_allowed(
            task_id="WD-P0-01",
            path=tmp_path / "staging" / "idle_cache" / "20260302" / "WD-P0-01" / "x.json",
            action="delete_via_queue",
        )
    events = _as_mapping(service.audit_events(limit=20, event_type="idle_queue_policy_blocked"))
    assert _as_int(events["records"]) >= 1
