"""运行时不变式巡检的判定契约。

这层是"静默故障"的探测器，所以测试重点不是"happy path 绿"，而是**每种静默故障形态
都必须报红**，以及**"等治理决定"不能被染成红灯**（红灯一旦常亮就会被当背景噪音）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from stock_analyzer.ops.runtime_invariants import (
    SEVERITY_DEFECT,
    SEVERITY_INFO,
    SEVERITY_PENDING,
    InvariantResult,
    check_artifact_identity,
    check_freshness,
    check_mounts,
    check_night_scan_artifact,
    check_readiness,
    check_scheduler,
    summarize,
)

CST = timezone(timedelta(hours=8))
MONDAY = date(2026, 9, 14)


def _by_name(results: list[InvariantResult]) -> dict[str, InvariantResult]:
    return {item.name: item for item in results}


# --- 新鲜度：18 天空转的形态必须报红 ----------------------------------------


def test_freshness_ok_when_minute_keeps_up_with_daily() -> None:
    results = _by_name(
        check_freshness(
            daily_max=date(2026, 9, 15),
            minute_max={"intraday_summary_1m": date(2026, 9, 15)},
            today=date(2026, 9, 16),
        )
    )
    assert results["daily_bars_freshness"].ok
    assert results["intraday_summary_1m_freshness"].ok


def test_freshness_flags_lagging_minute_side() -> None:
    """分钟停 8/28、日线到 9/15 —— 正是 2026-08-29~09-15 的实测形态。"""
    results = _by_name(
        check_freshness(
            daily_max=date(2026, 9, 15),
            minute_max={"intraday_summary_1m": date(2026, 8, 28)},
            today=date(2026, 9, 16),
        )
    )
    lagging = results["intraday_summary_1m_freshness"]
    assert not lagging.ok
    assert lagging.severity == SEVERITY_DEFECT
    assert lagging.evidence["lag_days"] == 18
    assert "空转" in lagging.detail


def test_freshness_flags_stale_daily_chain() -> None:
    """日线自己也不动的第二种形态（两条链一起停）。"""
    results = _by_name(
        check_freshness(
            daily_max=date(2026, 9, 10),
            minute_max={"intraday_summary_1m": date(2026, 9, 10)},
            today=date(2026, 9, 16),
        )
    )
    assert not results["daily_bars_freshness"].ok
    # 分钟跟着日线一起停时，自指不变式仍成立（不重复报第二个红灯）
    assert results["intraday_summary_1m_freshness"].ok


def test_freshness_floor_skips_weekend() -> None:
    """周一早上巡检时，底线应是上周五而不是周日。"""
    results = _by_name(
        check_freshness(
            daily_max=date(2026, 9, 11),  # 周五
            minute_max={"intraday_summary_1m": date(2026, 9, 11)},
            today=MONDAY,
        )
    )
    assert results["daily_bars_freshness"].ok
    assert results["daily_bars_freshness"].evidence["floor"] == "2026-09-11"


def test_freshness_flags_empty_tables() -> None:
    results = _by_name(
        check_freshness(
            daily_max=None,
            minute_max={"intraday_summary_1m": None},
            today=date(2026, 9, 16),
        )
    )
    assert not results["daily_bars_freshness"].ok


# --- 挂载：qq_minute_raw 事故的前置条件 --------------------------------------


def test_mounts_flags_missing_path() -> None:
    present = {"/app/artifacts", "/data/intraday_summary", "/data/vendor_history"}
    result = check_mounts(exists=lambda path: path in present)
    assert not result.ok
    assert result.evidence["missing"] == ["/data/qq_minute_raw"]


def test_mounts_ok_when_all_present() -> None:
    assert check_mounts(exists=lambda path: True).ok


# --- 工件身份：等决定 ≠ 坏了 --------------------------------------------------


def test_identity_match_is_ok() -> None:
    result = check_artifact_identity(
        {"status": "match", "loaded_content_hash": "a" * 64, "champion_model_id": "m1"}
    )
    assert result.ok


def test_identity_no_champion_is_pending_not_defect() -> None:
    """registry 没有 champion 是"治理没批准"，不是"系统坏了"——不该常亮红灯。"""
    result = check_artifact_identity(
        {"status": "no_champion", "loaded_content_hash": "a" * 64, "detail": "注册表没有 champion"}
    )
    assert not result.ok
    assert result.severity == SEVERITY_PENDING


def test_identity_mismatch_is_defect() -> None:
    """在服文件与 champion 哈希不同 = 换件/篡改，必须报红。"""
    result = check_artifact_identity({"status": "mismatch", "loaded_content_hash": "a" * 64})
    assert not result.ok
    assert result.severity == SEVERITY_DEFECT


def test_identity_missing_report_is_defect() -> None:
    assert check_artifact_identity(None).severity == SEVERITY_DEFECT


# --- 调度 --------------------------------------------------------------------


def test_scheduler_flags_stuck_and_failing() -> None:
    now = datetime(2026, 9, 16, 1, 0, tzinfo=CST)
    results = _by_name(
        check_scheduler(
            jobs={
                "wedged": {"running_since": "2026-09-15T22:00:00", "next_due_at": ""},
                "broken_now": {
                    "consecutive_failures": 3,
                    "last_failure": "boom",
                    "last_attempt_at": "2026-09-15T09:57:20",
                    "next_due_at": "2026-09-16T00:00:00",
                },
                "healthy": {"consecutive_failures": 0, "next_due_at": "2026-09-16T02:00:00"},
            },
            now=now,
        )
    )
    assert not results["scheduler_stuck_jobs"].ok
    assert "wedged" in results["scheduler_stuck_jobs"].detail
    assert not results["scheduler_failing_jobs"].ok
    assert "broken_now" in results["scheduler_failing_jobs"].detail


def test_scheduler_old_failure_is_pending_not_defect() -> None:
    """红灯必须意味着"正在坏"：月度任务的 16 天前旧失败只算待验证。

    实测形态：`factor_ic_decay_report` 的 9 次失败全在 8/31（月度任务、修复已在其后
    落地），若与"昨天还在失败"的任务同等报红，红灯就会被当背景噪音。
    """
    now = datetime(2026, 9, 16, 1, 0, tzinfo=CST)
    results = _by_name(
        check_scheduler(
            jobs={
                "factor_ic_decay_report": {
                    "consecutive_failures": 9,
                    "last_failure": "protocol binding mismatch",
                    "last_attempt_at": "2026-08-31T23:22:50",
                    "next_due_at": "2026-08-31T23:52:50",
                }
            },
            now=now,
        )
    )
    assert results["scheduler_failing_jobs"].ok
    awaiting = results["scheduler_failures_awaiting_run"]
    assert not awaiting.ok
    assert awaiting.severity == SEVERITY_PENDING
    assert "2026-08-31" in awaiting.evidence["awaiting"][0]
    # 全部一起看：不产生 defect（陈旧条目另算 NOTE）
    assert summarize(list(results.values()))["ok"] is True


def test_scheduler_stale_entry_is_note_not_defect() -> None:
    """first_board 那类"设计内退出但条目还在"的东西：提示，不报红。"""
    now = datetime(2026, 9, 16, 1, 0, tzinfo=CST)
    results = _by_name(
        check_scheduler(
            jobs={
                "week5_first_board_1": {
                    "consecutive_failures": 0,
                    "next_due_at": "2026-08-27T09:30:00",
                }
            },
            now=now,
        )
    )
    stale = results["scheduler_stale_entries"]
    assert not stale.ok
    assert stale.severity == SEVERITY_INFO
    assert summarize(list(results.values()))["ok"] is True


def test_scheduler_clean_is_green() -> None:
    now = datetime(2026, 9, 16, 1, 0, tzinfo=CST)
    results = check_scheduler(
        jobs={"a": {"consecutive_failures": 0, "next_due_at": "2026-09-16T02:00:00"}}, now=now
    )
    assert all(item.ok for item in results)


# --- readiness / 夜扫产物 ----------------------------------------------------


def _readiness(target: str = "2026-09-15", *, daily_ok: bool = True) -> dict[str, object]:
    return {
        "target_trade_date": target,
        "daily": {"ok": daily_ok},
        "index": {"ok": True},
        "delta": {"ok": True},
    }


def test_readiness_missing_inside_update_window_is_not_defect() -> None:
    """19:45 失效、约 20:35 重发——窗口内缺席是设计，不该每天假报警。"""
    result = check_readiness(
        payload=None,
        expected_trade_date=date(2026, 9, 15),
        now=datetime(2026, 9, 15, 20, 0, tzinfo=CST),
    )
    assert result.ok
    assert result.severity == SEVERITY_INFO


def test_readiness_missing_outside_window_is_defect() -> None:
    result = check_readiness(
        payload=None,
        expected_trade_date=date(2026, 9, 15),
        now=datetime(2026, 9, 16, 1, 0, tzinfo=CST),
    )
    assert not result.ok
    assert result.severity == SEVERITY_DEFECT


def test_readiness_target_mismatch_is_defect() -> None:
    result = check_readiness(
        payload=_readiness("2026-09-12"),
        expected_trade_date=date(2026, 9, 15),
        now=datetime(2026, 9, 16, 1, 0, tzinfo=CST),
    )
    assert not result.ok


def test_readiness_slot_failure_is_defect() -> None:
    result = check_readiness(
        payload=_readiness(daily_ok=False),
        expected_trade_date=date(2026, 9, 15),
        now=datetime(2026, 9, 16, 1, 0, tzinfo=CST),
    )
    assert not result.ok
    assert result.evidence["slots_ok"] is False


def test_night_scan_failed_or_stale_is_defect() -> None:
    now = datetime(2026, 9, 16, 1, 0, tzinfo=CST)
    failed = check_night_scan_artifact(
        latest_status="failed",
        latest_detail="week5_automation:failed",
        latest_timestamp=datetime(2026, 9, 15, 22, 0, tzinfo=CST),
        now=now,
    )
    assert not failed.ok
    stale = check_night_scan_artifact(
        latest_status="success",
        latest_detail="",
        latest_timestamp=datetime(2026, 9, 12, 22, 0, tzinfo=CST),
        now=now,
    )
    assert not stale.ok
    fresh = check_night_scan_artifact(
        latest_status="success",
        latest_detail="week5_automation:ok",
        latest_timestamp=datetime(2026, 9, 15, 22, 0, tzinfo=CST),
        now=now,
    )
    assert fresh.ok


# --- 汇总：红/绿只看 defect ---------------------------------------------------


def test_summary_only_defects_turn_red() -> None:
    pending = InvariantResult("p", False, SEVERITY_PENDING, "等决定")
    note = InvariantResult("n", False, SEVERITY_INFO, "提示")
    ok_item = InvariantResult("k", True, SEVERITY_DEFECT, "好")
    green = summarize([pending, note, ok_item])
    assert green["ok"] is True
    assert len(green["pending_decisions"]) == 1
    assert len(green["notes"]) == 1
    red = summarize([pending, InvariantResult("d", False, SEVERITY_DEFECT, "坏了")])
    assert red["ok"] is False
    assert len(red["defects"]) == 1


@pytest.mark.parametrize("severity", [SEVERITY_DEFECT, SEVERITY_PENDING, SEVERITY_INFO])
def test_result_serializes_severity(severity: str) -> None:
    assert InvariantResult("x", False, severity, "d").to_dict()["severity"] == severity


def test_identity_registry_busy_is_pending_not_defect() -> None:
    """写锁被本进程占着 = "这次读不到"，不是"系统坏了"。

    2026-09-16 盘中实测：巡检 10:50 报 registry_unavailable 被当 defect，其实只是 api
    容器正在写 learning_protocol.duckdb。探测器假警报一次就会被当噪音。
    """
    result = check_artifact_identity(
        {"status": "registry_busy", "loaded_content_hash": "a" * 64, "detail": "写锁被占用"}
    )
    assert not result.ok
    assert result.severity == SEVERITY_PENDING
    assert summarize([result])["ok"] is True


def test_identity_match_registered_is_pending_not_defect() -> None:
    """有登记副本但无 champion：身份可验证了，仍然只是"等批准"，不染红。"""
    result = check_artifact_identity(
        {
            "status": "match_registered",
            "loaded_content_hash": "a" * 64,
            "champion_model_id": "m_live",
            "detail": "在服工件 == 登记记录 m_live（trained），但注册表无 champion",
        }
    )
    assert not result.ok
    assert result.severity == SEVERITY_PENDING
    assert summarize([result])["ok"] is True
