"""M4-L §28：alpha_v2_shadow_cycle 调度专项测试。

覆盖：注册条件（enabled 才注册、不影响既有 job）、无 epoch safe skip、前置未就绪
快速返回（不重复计算）、窗口末尾明确落账、顺序（data_health → capture → mature →
report）、失败审计、幂等（重复 tick 不重复写）。
"""

from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

from _alpha_v2_m3_fixtures import open_epoch_for_manifest, write_freeze_manifest

from stock_analyzer.alpha_v2.validation.production_funnel import (
    emit_funnel_snapshot,
    extract_funnel_from_scan_report,
    funnel_snapshot_hash,
)
from stock_analyzer.config import load_config
from stock_analyzer.runtime.scheduler_supervisor import scheduler_group_for_job
from stock_analyzer.runtime.service import StockAnalyzerService
from stock_analyzer.runtime.services.live_shadow_cycle_service import (
    LiveShadowCycleService,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 9, 21)


class _CaptureScheduler:
    def __init__(self) -> None:
        self.jobs: list[str] = []
        self.interval_jobs: list[tuple[str, dict[str, object]]] = []

    def register(self, *, name: str, **kwargs: object) -> None:
        self.jobs.append(name)

    def register_interval(self, *, name: str, **kwargs: object) -> None:
        self.interval_jobs.append((name, kwargs))


def _config(*, alpha_enabled: bool):
    config = load_config(REPO_ROOT / "config" / "default.yaml")
    config.alpha_v2.enabled = alpha_enabled
    # 保留 week5 夜扫族与 theme 族（证明 Alpha V2 不改动既有注册面）
    config.week5.full_market_automation_enabled = True
    config.theme.enabled = True
    # 关掉无关任务族，避免注册清单噪声
    config.market_warehouse.enabled = False
    config.market_warehouse.auto_run = False
    config.tdx_sync.enabled = False
    config.acceptance.enabled = False
    config.week6.enabled = False
    config.evolution.enabled = False
    config.idle_queue.enabled = False
    config.cloud_backup.enabled = False
    config.factor_lifecycle.enabled = False
    config.monthly_review.enabled = False
    config.sim_broker_weekly.enabled = False
    return config


def _register_jobs(config) -> set[str]:
    service = StockAnalyzerService.__new__(StockAnalyzerService)
    scheduler = _CaptureScheduler()
    service._config = config
    service._scheduler = scheduler
    service._live_shadow_cycle = LiveShadowCycleService(service)
    service._resolve_idle_queue_enabled = lambda: (False, "")
    service._resolve_idle_queue_auto_run = lambda: (False, "")
    service._register_default_jobs()
    return {name for name, _ in scheduler.interval_jobs} | set(scheduler.jobs)


def test_cycle_job_registered_only_when_alpha_v2_enabled():
    disabled = _register_jobs(_config(alpha_enabled=False))
    assert "alpha_v2_shadow_cycle" not in disabled
    enabled = _register_jobs(_config(alpha_enabled=True))
    assert "alpha_v2_shadow_cycle" in enabled
    # 既有调度族一个不少（Alpha V2 disabled 时行为与当前 main 完全一致）
    expected_families = {
        "week5_night_scan",
        "close_reconcile",
        "midday_news_brief",
        "theme_daily_sync",
    }
    assert expected_families <= disabled
    assert expected_families <= enabled
    # heavy 组（与夜扫同组串行；绝不挤占 critical）
    assert scheduler_group_for_job("alpha_v2_shadow_cycle") == "heavy"


class _StubAutomation:
    def __init__(self, *, allowed: bool, reason: str = "") -> None:
        self.allowed = allowed
        self.reason = reason

    def probe_nightly_readiness(self) -> dict[str, object]:
        return {
            "status": "ready" if self.allowed else "blocked",
            "allowed": self.allowed,
            "reason": self.reason,
        }


def _service(monkeypatch, tmp_path, *, now: datetime, readiness_allowed: bool, epoch_root: Path):
    config = _config(alpha_enabled=True)
    config.alpha_v2.artifact_root = str(epoch_root)
    config.alpha_v2.production_funnel_root = str(tmp_path / "runtime" / "production_funnel")
    service = StockAnalyzerService.__new__(StockAnalyzerService)
    service._config = config
    audits: list[dict[str, object]] = []
    service._record_audit_event = lambda **kwargs: audits.append(kwargs)
    service._job_now = lambda: now
    service._week5_automation_service = _StubAutomation(allowed=readiness_allowed)
    calls: list[tuple[str, list[str]]] = []

    def _fake_run(script: str, argv: list[str], *, timeout_sec: int):
        calls.append((script, list(argv)))
        # 模拟真实副作用：capture 写快照、report 写 KPI（供后续幂等判定）
        if script == "alpha_v2_shadow_capture.py":
            shadow_dir = (
                epoch_root / "validation" / "alpha_v2_epoch_001" / "shadow"
                / f"{DAY.year:04d}" / f"{DAY.month:02d}"
            )
            shadow_dir.mkdir(parents=True, exist_ok=True)
            (shadow_dir / f"shadow_{DAY.strftime('%Y%m%d')}.jsonl").write_text(
                '{"symbol":"600001"}\n', encoding="utf-8"
            )
        if script == "alpha_v2_validation_report.py":
            reports = epoch_root / "validation" / "alpha_v2_epoch_001" / "reports"
            reports.mkdir(parents=True, exist_ok=True)
            report_file = reports / (
                f"validation_kpi_alpha_v2_epoch_001_{DAY.strftime('%Y%m%d')}.json"
            )
            report_file.write_text(
                "{}", encoding="utf-8"
            )
        return 0, "ok"

    cycle = LiveShadowCycleService(service)
    cycle._run_cli = _fake_run  # noqa: SLF001 - 测试注入子进程替身
    service._live_shadow_cycle = cycle
    return service, calls, audits


def _write_epoch(root: Path):
    manifest = write_freeze_manifest(root, validation_mode="rehearsal")
    return open_epoch_for_manifest(root, manifest, opened_on_date="2026-09-18")


def _funnel_payload(*, deep: list[str]) -> dict[str, object]:
    report = {
        "funnel": {
            "policy": "snapshot_funnel",
            "deep_stage_ran": True,
            "selection_contract": {"selection_contract_id": "night_alpha_v2_v1"},
        },
        "prefilter": {
            "universe_quality_selection": {
                "selector_mode": "quality",
                "selected": [{"symbol": s, "score": 1.0} for s in deep],
            },
            "shortlisted": [{"symbol": s, "baseline_score": 1.0} for s in deep],
            "deep_stage": {"selected": [{"symbol": s, "funnel_score": 1.0} for s in deep]},
            "pinned_symbols": [],
        },
    }
    payload = extract_funnel_from_scan_report(
        source_report=report, trace_id="t", scan_status="night_scan_completed", created_at="t"
    )
    payload["signal_date"] = DAY.isoformat()
    payload["trade_date"] = DAY.isoformat()
    payload["night_scan_report_id"] = "nr-20260921-01"  # 已链接
    payload["funnel_snapshot_hash"] = funnel_snapshot_hash(payload)
    return payload


def test_no_active_epoch_is_safe_skip(monkeypatch, tmp_path):
    root = tmp_path / "alpha_v2"
    service, calls, audits = _service(
        monkeypatch,
        tmp_path,
        now=datetime.combine(DAY, time(22, 30)),
        readiness_allowed=True,
        epoch_root=root,
    )
    result = service._live_shadow_cycle.run_daily_cycle()
    assert result["_scheduler_success"] is True
    assert result["_scheduler_detail"] == "alpha_v2_no_active_epoch"
    assert calls == [] and audits == []


def test_waiting_before_deadline_when_funnel_missing(monkeypatch, tmp_path):
    root = tmp_path / "alpha_v2"
    _write_epoch(root)
    service, calls, audits = _service(
        monkeypatch,
        tmp_path,
        now=datetime.combine(DAY, time(22, 30)),
        readiness_allowed=True,
        epoch_root=root,
    )
    result = service._live_shadow_cycle.run_daily_cycle()
    assert result["_scheduler_detail"].startswith("alpha_v2_waiting:")
    assert calls == []  # 未就绪时不起重活
    assert audits == []


def test_blocked_day_records_missing_after_deadline(monkeypatch, tmp_path):
    root = tmp_path / "alpha_v2"
    epoch = _write_epoch(root)
    service, calls, audits = _service(
        monkeypatch,
        tmp_path,
        now=datetime.combine(DAY, time(23, 56)),
        readiness_allowed=True,
        epoch_root=root,
    )
    result = service._live_shadow_cycle.run_daily_cycle()
    assert result["missing_recorded"] is True
    assert result["_scheduler_detail"].startswith("alpha_v2_blocked_recorded_missing")
    assert calls == []
    from stock_analyzer.alpha_v2.validation.shadow_capture import list_missing_days

    missing = list_missing_days(root, epoch.epoch_id)
    assert [item["signal_date"] for item in missing] == [DAY.isoformat()]
    assert "production_funnel_unavailable" in missing[0]["reason"]
    assert any(item.get("event_type") == "alpha_v2_cycle_blocked_day" for item in audits)


def test_ready_funnel_runs_steps_in_order(monkeypatch, tmp_path):
    root = tmp_path / "alpha_v2"
    _write_epoch(root)
    funnel_root = tmp_path / "runtime" / "production_funnel"
    emit_funnel_snapshot(funnel_root=funnel_root, payload=_funnel_payload(deep=["600001"]))
    service, calls, audits = _service(
        monkeypatch,
        tmp_path,
        now=datetime.combine(DAY, time(22, 40)),
        readiness_allowed=True,
        epoch_root=root,
    )
    result = service._live_shadow_cycle.run_daily_cycle()
    assert result["_scheduler_success"] is True
    assert [item["step"] for item in result["steps"]] == [
        "data_health",
        "capture",
        "mature",
        "report",
    ]
    assert [script for script, _ in calls] == [
        "alpha_v2_data_health_snapshot.py",
        "alpha_v2_shadow_capture.py",
        "alpha_v2_shadow_mature.py",
        "alpha_v2_validation_report.py",
    ]
    capture_argv = calls[1][1]
    assert "--cohort-source" in capture_argv
    assert capture_argv[capture_argv.index("--cohort-source") + 1] == "production_funnel"
    assert any(item.get("event_type") == "alpha_v2_cycle_completed" for item in audits)


def test_rerun_is_idempotent(monkeypatch, tmp_path):
    """Attack E：同一天跑两次，第二次直接幂等返回、不再起任何重活。"""
    root = tmp_path / "alpha_v2"
    _write_epoch(root)
    funnel_root = tmp_path / "runtime" / "production_funnel"
    emit_funnel_snapshot(funnel_root=funnel_root, payload=_funnel_payload(deep=["600001"]))
    service, calls, _ = _service(
        monkeypatch,
        tmp_path,
        now=datetime.combine(DAY, time(22, 40)),
        readiness_allowed=True,
        epoch_root=root,
    )
    first = service._live_shadow_cycle.run_daily_cycle()
    assert first["_scheduler_detail"] == "alpha_v2_cycle_completed"
    calls.clear()
    second = service._live_shadow_cycle.run_daily_cycle()
    assert second["_scheduler_detail"] == "alpha_v2_already_completed"
    assert calls == []


def test_step_failure_returns_failure_with_audit(monkeypatch, tmp_path):
    root = tmp_path / "alpha_v2"
    _write_epoch(root)
    funnel_root = tmp_path / "runtime" / "production_funnel"
    emit_funnel_snapshot(funnel_root=funnel_root, payload=_funnel_payload(deep=["600001"]))
    service, calls, audits = _service(
        monkeypatch,
        tmp_path,
        now=datetime.combine(DAY, time(22, 40)),
        readiness_allowed=True,
        epoch_root=root,
    )

    def _failing_run(script: str, argv: list[str], *, timeout_sec: int):
        calls.append((script, list(argv)))
        if script == "alpha_v2_shadow_capture.py":
            return 10, "生产漏斗硬门未通过"
        return 0, "ok"

    service._live_shadow_cycle._run_cli = _failing_run  # noqa: SLF001
    result = service._live_shadow_cycle.run_daily_cycle()
    assert result["_scheduler_success"] is False
    assert result["_scheduler_detail"].startswith("alpha_v2_step_failed:capture")
    assert any(item.get("event_type") == "alpha_v2_cycle_step_failed" for item in audits)
    # 失败后没有 KPI 报告产物
    reports = root / "validation" / "alpha_v2_epoch_001" / "reports"
    assert not list(reports.glob("*.json")) if reports.exists() else True


def test_research_proxy_funnel_artifact_is_rejected_as_not_ready(monkeypatch, tmp_path):
    """未链接（无 report_id）的 funnel 不构成"生产就绪"，调度层不起捕获。"""
    root = tmp_path / "alpha_v2"
    _write_epoch(root)
    payload = _funnel_payload(deep=["600001"])
    payload["night_scan_report_id"] = ""
    payload["funnel_snapshot_hash"] = funnel_snapshot_hash(payload)
    emit_funnel_snapshot(funnel_root=tmp_path / "runtime" / "production_funnel", payload=payload)
    service, calls, _ = _service(
        monkeypatch,
        tmp_path,
        now=datetime.combine(DAY, time(22, 40)),
        readiness_allowed=True,
        epoch_root=root,
    )
    result = service._live_shadow_cycle.run_daily_cycle()
    assert result["_scheduler_detail"] == "alpha_v2_waiting:production_funnel_not_linked_to_report"
    assert calls == []


def test_readiness_blocked_waits_without_heavy_work(monkeypatch, tmp_path):
    root = tmp_path / "alpha_v2"
    _write_epoch(root)
    funnel_root = tmp_path / "runtime" / "production_funnel"
    emit_funnel_snapshot(funnel_root=funnel_root, payload=_funnel_payload(deep=["600001"]))
    service, calls, _ = _service(
        monkeypatch,
        tmp_path,
        now=datetime.combine(DAY, time(22, 40)),
        readiness_allowed=False,
        epoch_root=root,
    )
    result = service._live_shadow_cycle.run_daily_cycle()
    assert result["_scheduler_detail"].startswith("alpha_v2_waiting:")
    assert calls == []
