from __future__ import annotations

from pathlib import Path

from stock_analyzer.config import load_config
from stock_analyzer.runtime.scheduler_supervisor import scheduler_group_for_job
from stock_analyzer.runtime.service import StockAnalyzerService


class _CaptureScheduler:
    def __init__(self) -> None:
        self.jobs: list[str] = []
        self.interval_jobs: list[str] = []

    def register(self, *, name: str, **_: object) -> None:
        self.jobs.append(name)

    def register_interval(self, *, name: str, **_: object) -> None:
        self.interval_jobs.append(name)


def test_full_market_automation_registers_expected_scheduler_families() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.week5.full_market_automation_enabled = True
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
    service = StockAnalyzerService.__new__(StockAnalyzerService)
    scheduler = _CaptureScheduler()
    service._config = config
    service._scheduler = scheduler
    service._resolve_idle_queue_enabled = lambda: (False, "")
    service._resolve_idle_queue_auto_run = lambda: (False, "")
    service._register_default_jobs()
    registered = set(scheduler.jobs + scheduler.interval_jobs)
    assert "premarket_scan" not in registered
    assert "auction_report" not in registered
    # 旧 first_board 扫描链（可发 actionable 通知）在全自动模式下必须整体退出。
    assert not any(name.startswith("week5_first_board_") for name in scheduler.interval_jobs)
    assert {"week5_night_scan", "week5_weekend_learning", "week5_automation_auction"} <= registered
    assert {f"week5_automation_market_radar_{index}" for index in range(1, 6)} <= registered
    assert {f"week5_automation_live_runtime_{index}" for index in range(1, 6)} <= registered
    assert scheduler_group_for_job("week5_automation_auction") == "critical"
    assert scheduler_group_for_job("week5_automation_live_runtime_1") == "critical"
    assert scheduler_group_for_job("week5_automation_market_radar_1") == "heavy"
    assert scheduler_group_for_job("week5_night_scan") == "heavy"
    assert scheduler_group_for_job("week5_weekend_learning") == "heavy"


def test_full_market_automation_can_disable_market_radar_jobs() -> None:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "config" / "default.yaml")
    config.week5.full_market_automation_enabled = True
    config.week5.market_radar_full_market_enabled = False
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
    service = StockAnalyzerService.__new__(StockAnalyzerService)
    scheduler = _CaptureScheduler()
    service._config = config
    service._scheduler = scheduler
    service._resolve_idle_queue_enabled = lambda: (False, "")
    service._resolve_idle_queue_auto_run = lambda: (False, "")

    service._register_default_jobs()

    assert not any(
        name.startswith("week5_automation_market_radar_") for name in scheduler.interval_jobs
    )
    assert "week5_automation_auction" in scheduler.jobs
    assert any(
        name.startswith("week5_automation_live_runtime_") for name in scheduler.interval_jobs
    )


def test_full_market_automation_scheduler_marks_failed_report_as_failure() -> None:
    result = StockAnalyzerService._week5_scheduler_result(
        {"ok": False, "status": "fallback", "reason": "readiness_blocked"}
    )

    assert result["_scheduler_success"] is False
    assert result["_scheduler_detail"] == "week5_automation:fallback"


def test_full_market_automation_scheduler_preserves_success_report() -> None:
    result = StockAnalyzerService._week5_scheduler_result({"ok": True, "status": "empty"})

    assert result["_scheduler_success"] is True
    assert result["_scheduler_detail"] == "week5_automation:empty"
