"""M4-L R1 §28 + §10(DH)：alpha_v2_shadow_cycle 调度专项测试。

覆盖：注册条件、无 epoch safe skip、**data_health 健康门（degraded 不 capture）**、
funnel/readiness 前置、窗口末尾落账（并继续推进历史成熟）、顺序、失败审计、幂等，
以及 DH-2..DH-7（缺任一 S08 输入 → 不 capture；degraded→healthy 跨槽位恢复）。

DH-1（真实 clean_oos_days=1）在 ``test_alpha_v2_m4l_e2e_rehearsal.py`` 里用真实 CLI
+ 合成数据端到端验证。
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path

from _alpha_v2_m3_fixtures import open_epoch_for_manifest, write_freeze_manifest

from stock_analyzer.alpha_v2.validation.production_funnel import (
    build_source_evidence,
    emit_funnel_snapshot,
    extract_funnel_from_source_evidence,
    file_sha256,
    funnel_snapshot_hash,
    write_source_evidence,
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
    config.week5.full_market_automation_enabled = True
    config.theme.enabled = True
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
    expected_families = {
        "week5_night_scan",
        "close_reconcile",
        "midday_news_brief",
        "theme_daily_sync",
    }
    assert expected_families <= disabled
    assert expected_families <= enabled
    assert scheduler_group_for_job("alpha_v2_shadow_cycle") == "heavy"


class _StubAutomation:
    def __init__(self, *, allowed: bool, reason: str = "") -> None:
        self.allowed = allowed
        self.reason = reason
        # active epoch 下 capture 必须声明严格档（P1 R1）；记录下来供断言。
        self.require_dual_delta_seen: list[bool] = []

    def probe_nightly_readiness(self, *, require_dual_delta: bool = False) -> dict[str, object]:
        self.require_dual_delta_seen.append(bool(require_dual_delta))
        return {
            "status": "ready" if self.allowed else "blocked",
            "allowed": self.allowed,
            "reason": self.reason,
        }


class _RealGateAutomation:
    """readiness 替身，但把判定交给**真实** gate（读真实 readiness 文件）。

    P1 R1 要证明的是"active epoch 下 v2 不放行"这条**判定链**，所以这里不写死
    allowed，而是原样转发 ``require_dual_delta`` 给 ``check_nightly_readiness``：
    cycle 传什么档、gate 判出什么结论，全程可见。
    """

    def __init__(self, *, path: Path, expected_trade_date: date) -> None:
        self.path = path
        self.expected_trade_date = expected_trade_date
        self.require_dual_delta_seen: list[bool] = []

    def probe_nightly_readiness(self, *, require_dual_delta: bool = False) -> dict[str, object]:
        from stock_analyzer.ops.nightly_readiness import check_nightly_readiness

        self.require_dual_delta_seen.append(bool(require_dual_delta))
        gate = check_nightly_readiness(
            expected_trade_date=self.expected_trade_date,
            path=self.path,
            require_dual_delta=require_dual_delta,
        )
        return {
            "status": "ready" if gate.ready else "blocked",
            "allowed": bool(gate.ready),
            "reason": gate.reason,
            "expected_trade_date": gate.expected_trade_date,
            "payload": gate.payload,
        }


class _FakeRunner:
    """替身子进程：模拟各 CLI 的真实副作用（data_health 工件 / 快照 / KPI 报告）。"""

    def __init__(
        self,
        *,
        epoch_root: Path,
        data_health_status: str = "healthy",
        capture_returncode: int = 0,
    ) -> None:
        self.epoch_root = epoch_root
        self.data_health_status = data_health_status
        self.capture_returncode = capture_returncode
        self.calls: list[tuple[str, list[str]]] = []
        self.data_health_runs = 0

    def __call__(self, script: str, argv: list[str], *, timeout_sec: int):
        self.calls.append((script, list(argv)))
        if script == "alpha_v2_data_health_snapshot.py":
            self.data_health_runs += 1
            out = Path(argv[argv.index("--out") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema": "alpha_v2_data_health.v1",
                "as_of": DAY.isoformat(),
                "status": self.data_health_status,
                "checks": [],
                "missing_artifacts": [],
                "generated_at": datetime.combine(DAY, time(22, 30)).isoformat(),
            }
            out.write_text(json.dumps(payload), encoding="utf-8")
            return 0, "ok"
        if script == "alpha_v2_shadow_capture.py":
            if self.capture_returncode != 0:
                return self.capture_returncode, "生产漏斗硬门未通过"
            shadow_dir = (
                self.epoch_root
                / "validation"
                / "alpha_v2_epoch_001"
                / "shadow"
                / f"{DAY.year:04d}"
                / f"{DAY.month:02d}"
            )
            shadow_dir.mkdir(parents=True, exist_ok=True)
            (shadow_dir / f"shadow_{DAY.strftime('%Y%m%d')}.jsonl").write_text(
                '{"symbol":"600001"}\n', encoding="utf-8"
            )
        if script == "alpha_v2_validation_report.py":
            reports = self.epoch_root / "validation" / "alpha_v2_epoch_001" / "reports"
            reports.mkdir(parents=True, exist_ok=True)
            report_file = reports / (
                f"validation_kpi_alpha_v2_epoch_001_{DAY.strftime('%Y%m%d')}.json"
            )
            report_file.write_text("{}", encoding="utf-8")
        return 0, "ok"


def _service(tmp_path, *, now: datetime, readiness_allowed: bool, epoch_root: Path):
    config = _config(alpha_enabled=True)
    config.alpha_v2.artifact_root = str(epoch_root)
    config.alpha_v2.production_funnel_root = str(tmp_path / "runtime" / "production_funnel")
    service = StockAnalyzerService.__new__(StockAnalyzerService)
    service._config = config
    audits: list[dict[str, object]] = []
    service._record_audit_event = lambda **kwargs: audits.append(kwargs)
    service._job_now = lambda: now
    service._week5_automation_service = _StubAutomation(allowed=readiness_allowed)
    cycle = LiveShadowCycleService(service)
    service._live_shadow_cycle = cycle
    return service, cycle, audits


def _write_epoch(root: Path):
    manifest = write_freeze_manifest(root, validation_mode="rehearsal")
    return open_epoch_for_manifest(root, manifest, opened_on_date="2026-09-18")


def _funnel_payload(
    funnel_root: Path, *, deep: list[str], linked: bool = True
) -> dict[str, object]:
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
    evidence = build_source_evidence(
        source_report=report,
        trade_date=DAY.isoformat(),
        trace_id="t",
        created_at="t",
    )
    evidence_path = write_source_evidence(funnel_root=funnel_root, payload=evidence)
    payload = extract_funnel_from_source_evidence(
        evidence,
        source_artifact_path=str(evidence_path),
        source_artifact_sha256=file_sha256(evidence_path),
        signal_date=DAY.isoformat(),
        trade_date=DAY.isoformat(),
    )
    payload["published_report_id"] = "nr-20260921-01" if linked else ""
    payload["funnel_snapshot_hash"] = funnel_snapshot_hash(payload)
    return payload


def _emit(env_tmp: Path, *, deep: list[str], linked: bool = True) -> None:
    funnel_root = env_tmp / "runtime" / "production_funnel"
    emit_funnel_snapshot(
        funnel_root=funnel_root,
        payload=_funnel_payload(funnel_root, deep=deep, linked=linked),
    )


def test_no_active_epoch_is_safe_skip(tmp_path):
    root = tmp_path / "alpha_v2"
    service, cycle, audits = _service(
        tmp_path,
        now=datetime.combine(DAY, time(22, 30)),
        readiness_allowed=True,
        epoch_root=root,
    )
    result = cycle.run_daily_cycle()
    assert result["_scheduler_success"] is True
    assert result["_scheduler_detail"] == "alpha_v2_no_active_epoch"
    assert audits == []


def test_degraded_data_health_does_not_capture_before_deadline(tmp_path):
    """DH-2..DH-6 共性：任一 S08 输入缺失 → data_health=degraded → 不 capture。"""
    root = tmp_path / "alpha_v2"
    _write_epoch(root)
    _emit(tmp_path, deep=["600001"])
    service, cycle, audits = _service(
        tmp_path,
        now=datetime.combine(DAY, time(22, 30)),
        readiness_allowed=True,
        epoch_root=root,
    )
    runner = _FakeRunner(epoch_root=root, data_health_status="degraded")
    cycle._run_cli = runner  # noqa: SLF001 - 测试注入
    result = cycle.run_daily_cycle()
    assert result["_scheduler_detail"].startswith("alpha_v2_waiting:data_health_not_healthy")
    assert runner.data_health_runs == 1  # 先生成、再验证
    assert not any(script == "alpha_v2_shadow_capture.py" for script, _ in runner.calls)
    assert list(root.rglob("shadow_*.jsonl")) == []
    assert audits == []


def test_degraded_then_healthy_recovers_in_same_day(tmp_path):
    """DH-7：第一次 degraded 不 capture，数据随后变齐 → 第二次 capture。"""
    root = tmp_path / "alpha_v2"
    _write_epoch(root)
    _emit(tmp_path, deep=["600001"])
    service, cycle, audits = _service(
        tmp_path,
        now=datetime.combine(DAY, time(22, 30)),
        readiness_allowed=True,
        epoch_root=root,
    )
    runner = _FakeRunner(epoch_root=root, data_health_status="degraded")
    cycle._run_cli = runner  # noqa: SLF001
    first = cycle.run_daily_cycle()
    assert first["_scheduler_detail"].startswith("alpha_v2_waiting:")
    assert list(root.rglob("shadow_*.jsonl")) == []
    runner.data_health_status = "healthy"
    second = cycle.run_daily_cycle()
    assert second["_scheduler_detail"] == "alpha_v2_cycle_completed"
    assert list(root.rglob("shadow_*.jsonl"))
    assert any(item.get("event_type") == "alpha_v2_cycle_completed" for item in audits)
    reports = root / "validation" / "alpha_v2_epoch_001" / "reports"
    assert list(reports.glob("*.json"))


def test_funnel_missing_waits_without_capture(tmp_path):
    root = tmp_path / "alpha_v2"
    _write_epoch(root)
    service, cycle, _ = _service(
        tmp_path,
        now=datetime.combine(DAY, time(22, 30)),
        readiness_allowed=True,
        epoch_root=root,
    )
    runner = _FakeRunner(epoch_root=root)
    cycle._run_cli = runner  # noqa: SLF001
    result = cycle.run_daily_cycle()
    assert result["_scheduler_detail"].startswith("alpha_v2_waiting:")
    assert not any(script == "alpha_v2_shadow_capture.py" for script, _ in runner.calls)


def test_unlinked_funnel_is_not_ready(tmp_path):
    root = tmp_path / "alpha_v2"
    _write_epoch(root)
    _emit(tmp_path, deep=["600001"], linked=False)
    service, cycle, _ = _service(
        tmp_path,
        now=datetime.combine(DAY, time(22, 30)),
        readiness_allowed=True,
        epoch_root=root,
    )
    runner = _FakeRunner(epoch_root=root)
    cycle._run_cli = runner  # noqa: SLF001
    result = cycle.run_daily_cycle()
    assert result["_scheduler_detail"] == "alpha_v2_waiting:production_funnel_not_linked_to_report"
    assert not any(script == "alpha_v2_shadow_capture.py" for script, _ in runner.calls)


def test_readiness_blocked_waits_without_capture(tmp_path):
    root = tmp_path / "alpha_v2"
    _write_epoch(root)
    _emit(tmp_path, deep=["600001"])
    service, cycle, _ = _service(
        tmp_path,
        now=datetime.combine(DAY, time(22, 30)),
        readiness_allowed=False,
        epoch_root=root,
    )
    runner = _FakeRunner(epoch_root=root)
    cycle._run_cli = runner  # noqa: SLF001
    result = cycle.run_daily_cycle()
    assert result["_scheduler_detail"].startswith("alpha_v2_waiting:")
    assert not any(script == "alpha_v2_shadow_capture.py" for script, _ in runner.calls)


def test_ready_prerequisites_run_capture_then_history_tail(tmp_path):
    root = tmp_path / "alpha_v2"
    _write_epoch(root)
    _emit(tmp_path, deep=["600001"])
    service, cycle, audits = _service(
        tmp_path,
        now=datetime.combine(DAY, time(22, 40)),
        readiness_allowed=True,
        epoch_root=root,
    )
    runner = _FakeRunner(epoch_root=root)
    cycle._run_cli = runner  # noqa: SLF001
    result = cycle.run_daily_cycle()
    assert result["_scheduler_success"] is True
    assert [item["step"] for item in result["steps"]] == ["capture", "mature", "report"]
    scripts = [script for script, _ in runner.calls]
    assert scripts == [
        "alpha_v2_data_health_snapshot.py",
        "alpha_v2_shadow_capture.py",
        "alpha_v2_shadow_mature.py",
        "alpha_v2_validation_report.py",
    ]
    capture_argv = runner.calls[1][1]
    assert capture_argv[capture_argv.index("--cohort-source") + 1] == "production_funnel"
    assert any(item.get("event_type") == "alpha_v2_cycle_completed" for item in audits)


def test_blocked_day_records_missing_and_still_runs_history_tail(tmp_path):
    """§9：到 deadline 仍不健康 → 落 missing，但历史日 mature/KPI 必须继续推进。"""
    root = tmp_path / "alpha_v2"
    epoch = _write_epoch(root)
    _emit(tmp_path, deep=["600001"])
    service, cycle, audits = _service(
        tmp_path,
        now=datetime.combine(DAY, time(23, 56)),
        readiness_allowed=True,
        epoch_root=root,
    )
    runner = _FakeRunner(epoch_root=root, data_health_status="degraded")
    cycle._run_cli = runner  # noqa: SLF001
    result = cycle.run_daily_cycle()
    assert result["missing_recorded"] is True
    assert result["_scheduler_detail"].startswith("alpha_v2_blocked_recorded_missing")
    assert [item["step"] for item in result["history_tail"]] == ["mature", "report"]
    assert not any(script == "alpha_v2_shadow_capture.py" for script, _ in runner.calls)
    from stock_analyzer.alpha_v2.validation.shadow_capture import list_missing_days

    missing = list_missing_days(root, epoch.epoch_id)
    assert [item["signal_date"] for item in missing] == [DAY.isoformat()]
    assert "production_prerequisites_unavailable" in missing[0]["reason"]
    assert any(item.get("event_type") == "alpha_v2_cycle_blocked_day" for item in audits)
    assert (root / "validation" / "alpha_v2_epoch_001" / "reports").exists()


def test_rerun_is_idempotent(tmp_path):
    """Attack E：同一天跑两次，第二次直接幂等返回、不再起任何重活。"""
    root = tmp_path / "alpha_v2"
    _write_epoch(root)
    _emit(tmp_path, deep=["600001"])
    service, cycle, _ = _service(
        tmp_path,
        now=datetime.combine(DAY, time(22, 40)),
        readiness_allowed=True,
        epoch_root=root,
    )
    runner = _FakeRunner(epoch_root=root)
    cycle._run_cli = runner  # noqa: SLF001
    first = cycle.run_daily_cycle()
    assert first["_scheduler_detail"] == "alpha_v2_cycle_completed"
    runner.calls.clear()
    second = cycle.run_daily_cycle()
    assert second["_scheduler_detail"] == "alpha_v2_already_completed"
    assert runner.calls == []


def test_capture_step_failure_returns_failure_with_audit(tmp_path):
    root = tmp_path / "alpha_v2"
    _write_epoch(root)
    _emit(tmp_path, deep=["600001"])
    service, cycle, audits = _service(
        tmp_path,
        now=datetime.combine(DAY, time(22, 40)),
        readiness_allowed=True,
        epoch_root=root,
    )
    runner = _FakeRunner(epoch_root=root, capture_returncode=10)
    cycle._run_cli = runner  # noqa: SLF001
    result = cycle.run_daily_cycle()
    assert result["_scheduler_success"] is False
    assert result["_scheduler_detail"].startswith("alpha_v2_step_failed:capture")
    assert any(item.get("event_type") == "alpha_v2_cycle_step_failed" for item in audits)


def test_missing_day_record_is_idempotent_across_ticks(tmp_path):
    """窗口末尾落账后再次 tick：不重复写 missing、不重复起重活。"""
    root = tmp_path / "alpha_v2"
    epoch = _write_epoch(root)
    service, cycle, _ = _service(
        tmp_path,
        now=datetime.combine(DAY, time(23, 56)),
        readiness_allowed=True,
        epoch_root=root,
    )
    runner = _FakeRunner(epoch_root=root, data_health_status="degraded")
    cycle._run_cli = runner  # noqa: SLF001
    cycle.run_daily_cycle()
    runner.calls.clear()
    cycle.run_daily_cycle()
    from stock_analyzer.alpha_v2.validation.shadow_capture import list_missing_days

    missing = list_missing_days(root, epoch.epoch_id)
    assert len(missing) == 1


# ---------------------------------------------------------------------------
# P1 R1：active Alpha epoch 必须要求**双 delta** readiness（ALPHA-RDY-1 / 3）
#
# 判据本身在 ops.nightly_readiness（一个函数、一个参数）。这里证明的是**接线**：
# active epoch 下的 capture 真的走的严格档，v2 release 一律 wait / missing，而不是
# 悄悄按 Week5 的宽松档放行、在没有执行侧证据的晚上记一个 clean day。
# ---------------------------------------------------------------------------


def _readiness_file(path: Path, *, schema_version: int, target: str = DAY.isoformat()) -> Path:
    payload: dict[str, object] = {
        "schema_version": schema_version,
        "target_trade_date": target,
        "daily": {"ok": True},
        "index": {"ok": True},
        "delta": {"ok": True},
    }
    if schema_version >= 3:
        payload["execution_delta"] = {"ok": True, "role": "execution"}
        payload["symbol_membership"] = {"membership_locked": True}
        payload["raw_delta_baseline"] = {"ok": True}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _service_with_real_readiness(
    tmp_path, *, now: datetime, readiness_path: Path, epoch_root: Path
):
    """与 ``_service`` 同形，但 readiness 判定走真实 gate（见 ``_RealGateAutomation``）。"""
    config = _config(alpha_enabled=True)
    config.alpha_v2.artifact_root = str(epoch_root)
    config.alpha_v2.production_funnel_root = str(tmp_path / "runtime" / "production_funnel")
    service = StockAnalyzerService.__new__(StockAnalyzerService)
    service._config = config
    audits: list[dict[str, object]] = []
    service._record_audit_event = lambda **kwargs: audits.append(kwargs)
    service._job_now = lambda: now
    automation = _RealGateAutomation(path=readiness_path, expected_trade_date=DAY)
    service._week5_automation_service = automation
    cycle = LiveShadowCycleService(service)
    service._live_shadow_cycle = cycle
    return service, cycle, audits, automation


def test_alpha_rdy1_v2_readiness_waits_without_capture(tmp_path):
    """ALPHA-RDY-1：active epoch + v2 readiness + 未到 deadline → waiting，不 capture。"""
    root = tmp_path / "alpha_v2"
    _write_epoch(root)
    _emit(tmp_path, deep=["600001"])
    readiness_path = _readiness_file(tmp_path / "runtime" / "ready.json", schema_version=2)
    service, cycle, audits, automation = _service_with_real_readiness(
        tmp_path,
        now=datetime.combine(DAY, time(22, 30)),
        readiness_path=readiness_path,
        epoch_root=root,
    )
    runner = _FakeRunner(epoch_root=root)
    cycle._run_cli = runner  # noqa: SLF001

    result = cycle.run_daily_cycle()

    assert result["_scheduler_detail"] == ("alpha_v2_waiting:nightly_dual_delta_not_ready"), result
    # 严格档确实被声明了——否则"v2 不放行"可能只是碰巧。
    assert automation.require_dual_delta_seen == [True]
    assert not any(script == "alpha_v2_shadow_capture.py" for script, _ in runner.calls)
    assert list(root.rglob("shadow_*.jsonl")) == []
    # waiting 不是失败：调度器继续按 5 分钟重试，不落 missing、不记 error。
    from stock_analyzer.alpha_v2.validation.shadow_capture import list_missing_days

    assert list_missing_days(root, "alpha_v2_epoch_001") == []
    assert audits == []


def test_alpha_rdy3_v2_until_deadline_records_missing_without_capture(tmp_path):
    """ALPHA-RDY-3：v2 一直到 deadline → 落 missing 台账、不 capture、不涨 clean day。"""
    root = tmp_path / "alpha_v2"
    _write_epoch(root)
    _emit(tmp_path, deep=["600001"])
    readiness_path = _readiness_file(tmp_path / "runtime" / "ready.json", schema_version=2)
    service, cycle, audits, _ = _service_with_real_readiness(
        tmp_path,
        # 23:56 已过 alpha_v2.live_cycle_latest_time（23:55）。
        now=datetime.combine(DAY, time(23, 56)),
        readiness_path=readiness_path,
        epoch_root=root,
    )
    runner = _FakeRunner(epoch_root=root)
    cycle._run_cli = runner  # noqa: SLF001

    result = cycle.run_daily_cycle()

    assert result["_scheduler_detail"].startswith("alpha_v2_blocked_recorded_missing:")
    assert "nightly_dual_delta_not_ready" in result["_scheduler_detail"]
    assert result["missing_recorded"] is True
    assert not any(script == "alpha_v2_shadow_capture.py" for script, _ in runner.calls)
    assert list(root.rglob("shadow_*.jsonl")) == []
    from stock_analyzer.alpha_v2.validation.shadow_capture import list_missing_days

    missing = list_missing_days(root, "alpha_v2_epoch_001")
    assert [item["signal_date"] for item in missing] == [DAY.isoformat()]
    assert "nightly_dual_delta_not_ready" in str(missing[0]["reason"])
    # 台账落账后仍推进历史成熟/KPI（missing 只约束它自己）。
    assert [item["step"] for item in result["history_tail"]] == ["mature", "report"]
    assert any(item.get("event_type") == "alpha_v2_cycle_blocked_day" for item in audits)
