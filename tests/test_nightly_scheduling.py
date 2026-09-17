"""晚报链路调度：等待预算、尝试上限、漏跑检查与任务分组。

对应 2026-09-16 v2 方案 §3.7/§3.8 与 §4.2 的调度/日期/readiness 三组场景。

全部在隔离临时目录里跑：不触网、不向真实飞书发送、不改动生产配置。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

import pytest

from stock_analyzer.config import StockAnalyzerConfig, load_config
from stock_analyzer.runtime.scheduler_supervisor import (
    scheduler_group_for_job,
    timeout_for_job,
)
from stock_analyzer.runtime.service import StockAnalyzerService
from stock_analyzer.runtime.services import week5_automation_service as automation_module
from stock_analyzer.runtime.services.nightly_report_service import SCAN_STATUS_FAILED

_TRADE_DATE = "2026-09-16"


def _load_config(tmp_path: Path) -> StockAnalyzerConfig:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.command_channel.secret_key = "test-secret"
    config.command_channel.state_persist_enabled = False
    config.command_channel.state_persist_path = str(tmp_path / "runtime_state.json")
    config.command_channel.history_archive_dir = str(tmp_path / "runtime_history")
    config.scheduler.leader_lock_path = str(tmp_path / "scheduler_leader.lock")
    config.scheduler.job_lock_dir = str(tmp_path / "scheduler_job_locks")
    # 夜扫注册需要 week5.enabled + auto_run + 全市场自动化三者同时成立
    config.week5.auto_run = True
    config.week5.auto_notify = False
    config.week5.full_market_automation_enabled = True
    config.week5.first_board_window_intervals = []
    config.week5.first_board_windows = ["09:30-09:31"]
    config.week6.auto_run = False
    config.market_warehouse.enabled = False
    config.acceptance.auto_run = False
    config.training.bootstrap_auto_run_on_first_start = False
    config.training.bootstrap_require_completion_for_runtime = False
    config.training.artifact_path = str(tmp_path / "test_model.json")
    config.training.bootstrap_state_path = str(tmp_path / "bootstrap_state.json")
    offline_root = tmp_path / "missing_offline_package"
    offline_root.mkdir(parents=True, exist_ok=True)
    config.data_source.local_data_root = str(offline_root)
    config.tdx_sync.vipdoc_root = str(offline_root)
    # 目标固定为 console：本文件只验调度与落盘，不碰真实通知通道。
    config.notifications.primary = "console"
    config.nightly.enabled = True
    config.nightly.reports_root = str(tmp_path / "nightly_reports")
    config.nightly.delivery_root = str(tmp_path / "nightly_delivery")
    return config


class _Scanner:
    """可编排的夜扫替身：记录调用参数，按脚本返回结果或抛异常。"""

    def __init__(self, script: list[object] | None = None) -> None:
        self.script = list(script or [])
        self.calls: list[dict[str, object]] = []
        self.default: object = "rows"

    def __call__(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(dict(kwargs))
        step = self.script.pop(0) if self.script else self.default
        if isinstance(step, Exception):
            raise step
        if step == "empty":
            return _night_scan_payload([])
        return _night_scan_payload([_row()])


def _row() -> dict[str, object]:
    return {
        "symbol": "600000",
        "score": 72.0,
        "shortlist_reasons": ["signal_strength"],
        "reasons": ["high_score"],
        "overextension": {
            "level": "none",
            "reject_new_buy": False,
            "evaluation_status": "evaluated",
            "missing_inputs": [],
            "reasons": [],
            "metrics": {},
        },
        "board_risk": {"reject_new_buy": False, "reasons": []},
    }


def _night_scan_payload(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "status": "ok" if rows else "empty",
        "trace_id": "t",
        "night_pool": rows,
        "overnight_top5": rows[:5],
        "candidate_data_gate": {"status": "ok", "reasons": []},
        "fallback": {"applied": False, "reason": ""},
        "readiness": {"status": "ready", "allowed": True},
        "source_report": {
            "data_snapshot_id": _TRADE_DATE,
            "prefilter": {"universe_count": 100, "eligible_count": 90},
            "funnel": {
                "light_count": 20,
                "deep_count": 10,
                "final_count": 0,
                "final_selection": {"selected_count": 0, "rejected": []},
            },
        },
    }


@pytest.fixture()
def service(tmp_path: Path):  # type: ignore[no-untyped-def]
    config = _load_config(tmp_path)
    svc = StockAnalyzerService(config=config)
    svc._scheduler_now_context = datetime.fromisoformat("2026-09-16T21:45:04+08:00")
    yield svc


def _install(
    service: StockAnalyzerService,
    *,
    scanner: _Scanner,
    readiness: dict[str, object] | None = None,
) -> tuple[_Scanner, dict[str, object]]:
    probe_result = dict(readiness or {"status": "ready", "allowed": True})
    service.run_week5_night_scan = scanner  # type: ignore[method-assign]
    service._week5_automation_service.probe_nightly_readiness = (  # type: ignore[method-assign]
        lambda: dict(probe_result)
    )
    return scanner, probe_result


def _at(service: StockAnalyzerService, hhmm: str) -> dict[str, object]:
    service._scheduler_now_context = datetime.fromisoformat(f"{_TRADE_DATE}T{hhmm}:00+08:00")
    return service._job_week5_night_scan()


# --------------------------------------------------------------- 数据等待预算


def test_waiting_data_returns_fast_and_does_not_consume_an_attempt(
    service: StockAnalyzerService,
) -> None:
    """数据未就绪必须快速返回，且**不计入**重型扫描尝试次数。"""
    scanner, _ = _install(
        service,
        scanner=_Scanner(),
        readiness={
            "status": "blocked",
            "allowed": False,
            "reason": "nightly_data_not_ready",
            "expected_trade_date": _TRADE_DATE,
        },
    )
    result = _at(service, "21:45")
    assert result["_scheduler_success"] is True
    assert "waiting_data" in str(result["_scheduler_detail"])
    assert scanner.calls == []

    state = service._nightly_report_service.read_date_state(_TRADE_DATE)
    assert state["scan_phase"] == "waiting_data"
    assert state["waiting_reason"] == "nightly_data_not_ready"
    # readiness 证据被留存，供事后判断"当时到底看到了什么"
    assert state["last_readiness"]["status"] == "blocked"
    assert int(state.get("scan_attempts") or 0) == 0


def test_late_data_still_gets_a_full_scan_budget(service: StockAnalyzerService) -> None:
    """21:45 数据没到、22:00 到了：第二次检查必须能真正起扫。"""
    scanner, readiness = _install(
        service,
        scanner=_Scanner(),
        readiness={"status": "blocked", "allowed": False, "reason": "nightly_data_not_ready"},
    )
    _at(service, "21:45")
    assert scanner.calls == []

    readiness.update({"status": "ready", "allowed": True, "reason": ""})
    result = _at(service, "22:00")
    assert len(scanner.calls) == 1
    assert "nightly_published" in str(result["_scheduler_detail"])
    state = service._nightly_report_service.read_date_state(_TRADE_DATE)
    assert state["scan_phase"] == "published"
    assert int(state["scan_attempts"]) == 1


def test_scan_is_started_without_waiting_and_without_shared_state_idempotency(
    service: StockAnalyzerService,
) -> None:
    """就绪已在检查入口探过：起扫时不再等；且不使用会被盘中任务改写的共享幂等。"""
    scanner, _ = _install(service, scanner=_Scanner())
    _at(service, "21:45")
    assert len(scanner.calls) == 1
    call = scanner.calls[0]
    assert call["readiness_wait_sec"] == 0
    assert call["skip_shared_state_idempotency"] is True
    # 夜间任务仍然禁止走买入类通知链
    assert call["notify_enabled"] is False


def test_quick_probe_does_not_call_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """探测函数本身不得等待（等待策略由调度器负责）。"""
    from stock_analyzer.ops.nightly_readiness import ReadinessGate

    slept: list[float] = []
    monkeypatch.setattr(automation_module, "sleep", lambda seconds: slept.append(seconds))

    class _Svc:
        _config = type("_C", (), {"week5": type("_W", (), {})()})()

        @staticmethod
        def _resolve_nightly_expected_trade_date() -> str:
            return _TRADE_DATE

        @staticmethod
        def _record_audit_event(**_kwargs: object) -> None:
            return None

    monkeypatch.setattr(
        automation_module,
        "check_nightly_readiness",
        lambda *, expected_trade_date: ReadinessGate(
            ready=False,
            reason="nightly_data_not_ready",
            payload={},
            expected_trade_date=expected_trade_date,
        ),
    )
    automation = automation_module.RuntimeWeek5AutomationService.__new__(
        automation_module.RuntimeWeek5AutomationService
    )
    automation._service = _Svc()  # type: ignore[assignment]
    probe = automation.probe_nightly_readiness()
    assert probe["allowed"] is False
    assert probe["waited_sec"] == 0.0
    assert slept == []


# ------------------------------------------------------------------- 尝试上限


def test_scan_stops_after_max_attempts(service: StockAnalyzerService) -> None:
    """同一交易日重型扫描最多执行 max_scan_attempts 次。"""
    scanner, _ = _install(service, scanner=_Scanner([RuntimeError("boom")] * 9))
    first = _at(service, "21:45")
    assert "nightly_retry_pending" in str(first["_scheduler_detail"])
    assert len(scanner.calls) == 1

    second = _at(service, "21:50")
    assert len(scanner.calls) == 2
    # 第 2 次已到上限：仍抛异常 -> 给出明确失败结论，而不是继续重试
    assert "nightly_published" in str(second["_scheduler_detail"])
    published = service._nightly_report_service.published_report(_TRADE_DATE)
    assert published is not None
    assert published["scan_status"] == SCAN_STATUS_FAILED

    third = _at(service, "21:55")
    assert len(scanner.calls) == 2
    assert "already_published" in str(third["_scheduler_detail"])


def test_first_failed_attempt_does_not_publish_a_conclusion(
    service: StockAnalyzerService,
) -> None:
    """还有重试预算时不得发布结论——一次抖动不该钉死当晚。"""
    scanner, _ = _install(service, scanner=_Scanner([RuntimeError("transient")]))
    _at(service, "21:45")
    assert service._nightly_report_service.published_report(_TRADE_DATE) is None
    assert len(scanner.calls) == 1


def test_recovery_after_a_transient_failure_publishes_the_real_result(
    service: StockAnalyzerService,
) -> None:
    scanner, _ = _install(service, scanner=_Scanner([RuntimeError("transient"), "empty"]))
    _at(service, "21:45")
    _at(service, "21:50")
    published = service._nightly_report_service.published_report(_TRADE_DATE)
    assert published is not None
    assert published["scan_status"] == "empty"
    assert len(scanner.calls) == 2


# --------------------------------------------------------------------- 幂等


def test_completed_day_short_circuits_later_triggers(service: StockAnalyzerService) -> None:
    scanner, _ = _install(service, scanner=_Scanner())
    _at(service, "21:45")
    assert len(scanner.calls) == 1

    for hhmm in ("21:50", "22:00", "22:30"):
        result = _at(service, hhmm)
        assert result["idempotent"] is True
        assert "already_published" in str(result["_scheduler_detail"])
    assert len(scanner.calls) == 1


def test_empty_result_also_counts_as_completed(service: StockAnalyzerService) -> None:
    """正常空结果是有效结论：同样短路后续触发，不能反复重扫。"""
    scanner, _ = _install(service, scanner=_Scanner(["empty"]))
    _at(service, "21:45")
    result = _at(service, "21:50")
    assert result["idempotent"] is True
    assert len(scanner.calls) == 1
    assert service._nightly_report_service.published_report(_TRADE_DATE)["scan_status"] == "empty"


# ------------------------------------------------------- 报告与待发记录的落地


def test_publish_creates_delivery_records_and_registers_them(
    service: StockAnalyzerService,
) -> None:
    _install(service, scanner=_Scanner())
    _at(service, "21:45")
    state = service._nightly_report_service.read_date_state(_TRADE_DATE)
    delivery_ids = state["delivery_ids"]
    assert delivery_ids
    for delivery_id in delivery_ids:
        record = service._nightly_delivery_service.load_record(delivery_id)
        assert record is not None
        assert record["state"] == "pending"
        assert record["request_uuid"]


def test_report_records_identity_fields(service: StockAnalyzerService) -> None:
    _install(service, scanner=_Scanner())
    service.current_week5_data_version = "snap-20260916"  # type: ignore[method-assign]
    _at(service, "21:45")
    report = service._nightly_report_service.published_report(_TRADE_DATE)
    assert report is not None
    assert report["data_snapshot_id"] == "snap-20260916"
    assert report["trade_date"] == _TRADE_DATE
    assert report["run_id"]
    assert report["config_hash"]


# ------------------------------------------------------------------ 交付检查


def test_delivery_tick_job_reports_counts(service: StockAnalyzerService) -> None:
    _install(service, scanner=_Scanner())
    _at(service, "21:45")
    result = service._job_nightly_delivery_tick()
    assert result["_scheduler_success"] is True
    assert "nightly_delivery:processed=" in str(result["_scheduler_detail"])
    assert result["report"]["processed"] == 1


def test_delivery_tick_job_is_inert_when_disabled(service: StockAnalyzerService) -> None:
    service._config.nightly.enabled = False
    result = service._job_nightly_delivery_tick()
    assert result["_scheduler_detail"] == "nightly_disabled"


def test_delivery_tick_does_not_run_the_scan(service: StockAnalyzerService) -> None:
    """交付检查只碰交付状态：不得选股、不得更新行情、不得等重型扫描。"""
    scanner, _ = _install(service, scanner=_Scanner())
    service._job_nightly_delivery_tick()
    assert scanner.calls == []


def test_delivery_jobs_are_critical_with_short_timeouts() -> None:
    """交付检查必须在不被 heavy 单槽阻塞的分组里，且有独立短超时。"""
    config = load_config(Path(__file__).resolve().parents[1] / "config" / "default.yaml")
    for job in ("nightly_delivery_tick", "nightly_delivery_resume"):
        group = scheduler_group_for_job(job)
        assert group == "critical"
        assert timeout_for_job(config, group=group, job=job) == 120
    # 夜扫本身仍是 heavy / 1800s：交付检查的短超时不能顺手改掉扫描预算
    assert scheduler_group_for_job("week5_night_scan") == "heavy"
    assert timeout_for_job(config, group="heavy", job="week5_night_scan") == 1800


# ------------------------------------------------------------------ 调度注册


def test_enabled_registers_interval_check_entry(service: StockAnalyzerService) -> None:
    """开关打开时：夜扫改由 21:45—23:00 的 5 分钟检查入口驱动。"""
    interval_jobs = service._scheduler._interval_jobs  # noqa: SLF001
    hourly_jobs = service._scheduler._jobs  # noqa: SLF001
    assert "week5_night_scan" in interval_jobs
    assert "week5_night_scan" not in hourly_jobs
    entry = interval_jobs["week5_night_scan"]
    assert f"{entry.window_start.hour:02d}:{entry.window_start.minute:02d}" == "21:45"
    assert f"{entry.window_end.hour:02d}:{entry.window_end.minute:02d}" == "23:00"
    assert entry.interval_minutes == 5
    assert "nightly_delivery_tick" in interval_jobs
    assert "nightly_delivery_resume" in interval_jobs


def test_disabled_keeps_the_legacy_single_trigger(tmp_path: Path) -> None:
    """开关关闭时必须保留旧调度行为：21:45 单次触发、无交付检查任务。"""
    config = _load_config(tmp_path)
    config.nightly.enabled = False
    svc = StockAnalyzerService(config=config)
    assert "week5_night_scan" in svc._scheduler._jobs  # noqa: SLF001
    assert "week5_night_scan" not in svc._scheduler._interval_jobs  # noqa: SLF001
    assert "nightly_delivery_tick" not in svc._scheduler._interval_jobs  # noqa: SLF001


def test_night_scan_window_ends_at_last_allowed_start(service: StockAnalyzerService) -> None:
    """23:00 之后不得再起新的重型扫描（窗口本身就不覆盖）。"""
    from datetime import time as clock_time

    from stock_analyzer.runtime.scheduler import _due_interval_slot

    entry = service._scheduler._interval_jobs["week5_night_scan"]  # noqa: SLF001
    assert _due_interval_slot(entry, clock_time(21, 45)) is not None
    assert _due_interval_slot(entry, clock_time(23, 0)) is not None
    assert _due_interval_slot(entry, clock_time(23, 5)) is None


# ---------------------------------------------------------------------- 日期


def test_trade_date_comes_from_market_timezone(service: StockAnalyzerService) -> None:
    _install(service, scanner=_Scanner())
    _at(service, "21:45")
    published = service._nightly_report_service.published_report(_TRADE_DATE)
    assert published is not None
    assert published["trade_date"] == _TRADE_DATE


def test_state_is_isolated_per_trade_date(service: StockAnalyzerService) -> None:
    """跨日不得互相污染：前一日状态不影响新一天的判断。"""
    scanner, _ = _install(service, scanner=_Scanner())
    _at(service, "21:45")
    service._nightly_report_service.update_date_state("2026-09-17", {"scan_phase": "pending"})
    assert service._nightly_report_service.read_date_state("2026-09-17")["scan_phase"] == "pending"
    assert service._nightly_report_service.read_date_state(_TRADE_DATE)["scan_phase"] == "published"
    assert len(scanner.calls) == 1


def test_latest_endpoint_fields(service: StockAnalyzerService) -> None:
    """查询接口补上报告与交付摘要，且不泄露接收人/凭据。"""
    _install(service, scanner=_Scanner())
    _at(service, "21:45")
    payload = service.latest_week5_night_scan()
    assert payload["report_id"]
    assert payload["scan_status"] == "completed"
    assert payload["delivery_status"] == "pending"
    assert payload["required_target_delivered"] is False
    blob = str(payload)
    assert "app_secret" not in blob and "Bearer" not in blob


def test_retry_endpoint_is_disabled_when_nightly_off(service: StockAnalyzerService) -> None:
    service._config.nightly.enabled = False
    result = service.retry_nightly_delivery("nr-20260916-01")
    assert result["queued"] is False
    assert result["reason"] == "nightly_disabled"


def test_retry_endpoint_rejects_unknown_report(service: StockAnalyzerService) -> None:
    _install(service, scanner=_Scanner())
    _at(service, "21:45")
    result = service.retry_nightly_delivery(f"nr-{uuid.uuid4().hex}")
    assert result["queued"] is False
    assert result["reason"] == "report_not_found"


def test_zero_signal_scan_still_produces_a_publishable_report(
    service: StockAnalyzerService,
) -> None:
    """没选到票是正常业务结果，同样要有可交付的报告（不允许静默沉默）。"""
    _install(service, scanner=_Scanner(["empty"]))
    _at(service, "21:45")
    report = service._nightly_report_service.published_report(_TRADE_DATE)
    assert report is not None
    assert report["scan_status"] == "empty"
    assert report["observation_candidates"] == []


# ------------------------------------------------- 全局停发开关（旧通知链）


def test_global_notification_switch_suppresses_the_legacy_notify_path(
    service: StockAnalyzerService,
) -> None:
    """notifications.enabled=false 必须让整条通知出口静默，而不只是新晚报链路。

    这个开关在此之前**没有任何消费方**：设成 false 什么都不会发生。既然它表达的是
    "别再往外发消息"，就应让旧链路也真正服从——否则同一个开关在不同链路有不同含义。
    """
    calls: list[object] = []

    class _SpyNotifier:
        def send(self, message: object) -> object:
            calls.append(message)
            from stock_analyzer.notify.channels import NotificationResult

            return NotificationResult(success=True, channel="spy")

    service._config.notifications.enabled = False
    service._notifier = _SpyNotifier()  # type: ignore[assignment]
    payload = service.notify(title="t", content="c", level="info", trace_id="x")
    assert calls == []
    assert payload["success"] is False
    assert payload["channel"] == "notifications_disabled"
    assert payload["suppressed"] is True

    # 打开后恢复发送
    service._config.notifications.enabled = True
    payload = service.notify(title="t", content="c", level="info", trace_id="x")
    assert len(calls) == 1
    assert payload["success"] is True
