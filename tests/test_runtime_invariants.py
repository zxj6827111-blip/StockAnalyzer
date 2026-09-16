"""运行时不变式巡检的判定契约。

这层是"静默故障"的探测器，所以测试重点不是"happy path 绿"，而是**每种静默故障形态
都必须报红**，以及**"等治理决定"不能被染成红灯**（红灯一旦常亮就会被当背景噪音）。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from stock_analyzer.ops import runtime_invariants
from stock_analyzer.ops.runtime_invariants import (
    SEVERITY_DEFECT,
    SEVERITY_INFO,
    SEVERITY_PENDING,
    InvariantResult,
    check_artifact_identity,
    check_freshness,
    check_mounts,
    check_night_scan_artifact,
    check_overextension_inputs,
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


# --- 身份项取数路径：优先问服务，直连兜底 -------------------------------------


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _patch_health(
    monkeypatch: pytest.MonkeyPatch,
    payload: object = None,
    error: Exception | None = None,
) -> None:
    def fake_urlopen(url: str, timeout: float | None = None) -> _FakeResponse:
        _ = (url, timeout)
        if error is not None:
            raise error
        return _FakeResponse(payload if payload is not None else {})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)


def _forbid_direct_db(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("服务可用时不得再直连学习库（写锁必然冲突）")

    monkeypatch.setattr(runtime_invariants, "_read_identity_from_db", boom)


def test_identity_prefers_service_over_direct_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """服务进程才持着学习库写锁，身份项必须先问它。

    2026-09-16 NAS 实测：直连（api 容器内、scheduler-critical 容器内）都报
    ``Conflicting lock is held``，于是身份项在服务运行期恒答 registry_busy。
    """
    _forbid_direct_db(monkeypatch)
    _patch_health(
        monkeypatch,
        {
            "model_identity": {
                "status": "match_registered",
                "loaded_content_hash": "a" * 64,
                "champion_model_id": "model_v3_deadbeef",
                "detail": "在服工件 == 登记记录 model_v3_deadbeef（trained）",
            }
        },
    )
    got = runtime_invariants._artifact_identity(  # noqa: SLF001
        Path("learning_protocol.duckdb"), ["http://127.0.0.1:8000/health/deep"]
    )
    assert got is not None
    assert got["status"] == "match_registered"
    # 取数来源必须留在证据里：同一状态可能来自"问服务"或"直连兜底"，排查要能分辨。
    assert "服务 API" in str(got["detail"])


def test_identity_falls_back_to_db_when_service_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """服务不可达时才直连——那时写锁已释放，直连反而能成。"""
    _patch_health(monkeypatch, error=OSError("Connection refused"))
    monkeypatch.setattr(
        runtime_invariants,
        "_read_identity_from_db",
        lambda path: {"status": "no_champion", "detail": "兜底路径"},
    )
    got = runtime_invariants._artifact_identity(  # noqa: SLF001
        Path("learning_protocol.duckdb"), ["http://127.0.0.1:8000/health/deep"]
    )
    assert got is not None
    assert got["status"] == "no_champion"


def test_identity_ignores_response_without_identity_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """旧版服务（/health/deep 还没有 model_identity）不许被读成"身份正常"。

    拿不到就是拿不到：必须降级到直连，而不是凭一个缺字段的响应给绿。
    """
    _patch_health(monkeypatch, {"status": "ok"})
    monkeypatch.setattr(
        runtime_invariants,
        "_read_identity_from_db",
        lambda path: {"status": "loaded_hash_missing", "detail": "兜底路径"},
    )
    got = runtime_invariants._artifact_identity(  # noqa: SLF001
        Path("learning_protocol.duckdb"), ["http://127.0.0.1:8000/health/deep"]
    )
    assert got is not None
    assert got["status"] == "loaded_hash_missing"


def test_identity_without_health_urls_uses_direct_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """不传端点（离线/单测）时保持原行为：只直连，且一个 HTTP 请求都不发。"""
    calls: list[str] = []

    def spy(url: str, timeout: float | None = None) -> _FakeResponse:
        calls.append(url)
        return _FakeResponse({})

    monkeypatch.setattr("urllib.request.urlopen", spy)
    monkeypatch.setattr(
        runtime_invariants,
        "_read_identity_from_db",
        lambda path: {"status": "mismatch", "detail": "直连"},
    )
    got = runtime_invariants._artifact_identity(Path("learning_protocol.duckdb"))  # noqa: SLF001
    assert got is not None
    assert got["status"] == "mismatch"
    assert calls == []


# --- 窗口过期：单列一项，不染红也不消失 ---------------------------------------


def test_window_expiry_is_visible_but_never_red() -> None:
    """过期不再计入连败（scheduler.py），所以巡检必须自己盯住它——否则信号会消失。

    实测形态：jobs 里 ``consecutive_failures=0``、``last_expired`` 是今天。
    既不红（既可能是任务已废弃，也可能是 supervisor 已跑而 leader 又记一次），
    也要在输出里看得见。
    """
    now = datetime(2026, 9, 16, 16, 0, tzinfo=CST)
    jobs = {
        "week5_automation_auction": {
            "consecutive_failures": 0,
            "last_failure": "",
            "last_expired": "2026-09-16T09:56:41",
            "last_attempt_at": "2026-09-16T09:25:23",
            "next_due_at": "2026-09-17T09:25:00",
            "running_since": "",
        }
    }
    results = _by_name(check_scheduler(jobs=jobs, now=now))
    expired = results["scheduler_recent_expiries"]
    assert not expired.ok
    assert expired.severity == SEVERITY_INFO
    assert "week5_automation_auction" in expired.detail
    # 不染红：INFO 不进 defects
    assert summarize([expired])["ok"] is True
    # 过期不算连败 → 不得出现在"正在失败"里
    assert results["scheduler_failing_jobs"].ok is True


def test_old_window_expiry_is_not_reported_forever() -> None:
    """过期项只看近 recent_failure_days 天，历史过期不该常亮。"""
    now = datetime(2026, 9, 16, 16, 0, tzinfo=CST)
    jobs = {
        "long_retired_job": {
            "consecutive_failures": 0,
            "last_expired": "2026-08-01T09:56:41",
            "running_since": "",
        }
    }
    results = _by_name(check_scheduler(jobs=jobs, now=now))
    assert results["scheduler_recent_expiries"].ok is True


# --- 过热闸输入退化：静默否决整条买入路径 -------------------------------------


def _overext(bias: float, atr: float, level: str = "reject") -> dict[str, object]:
    return {"overextension": {"level": level, "metrics": {"bias_ma5": bias, "atr_distance": atr}}}


def test_overextension_fallback_inputs_are_a_defect() -> None:
    """生产实测形态：bias = close-1、atr = (close-1)/0.03 → 无条件 reject。

    2026-09-16 实据：12 轮夜扫 600 条候选 level 全是 reject，atr/bias 恒为 33.333
    （即 1/0.03）——喂进去的行既无 ma5 也无 atr14，两处 fallback 同时生效。
    这条探测若不在，故障只会表现成"夜扫长期 0 信号"，被误读成阈值或 alpha 问题。
    """
    candidates = [_overext(5.06, 168.66666666666669), _overext(16.11, 537.0)]
    result = check_overextension_inputs(candidates)
    assert not result.ok
    assert result.severity == SEVERITY_DEFECT
    assert summarize([result])["ok"] is False
    assert result.evidence["implausible_bias"] == 2
    assert result.evidence["ratio_like_fallback"] == 2


def test_overextension_low_vol_row_is_not_a_false_positive() -> None:
    """给真实输入时 atr/bias == ma5/atr14，低波动票也可能正好是 33.33。

    所以主判据必须是"物理上不成立的乖离"，不能只看比值——否则会把正常票报成故障。
    """
    candidates = [_overext(0.02, 0.6666, level="none"), _overext(0.01, 0.3333, level="none")]
    result = check_overextension_inputs(candidates)
    assert result.ok
    assert result.detail == "过热闸输入正常"
    assert result.evidence["ratio_like_fallback"] == 2  # 比值确实像，但不是故障
    assert result.evidence["implausible_bias"] == 0


def test_overextension_detector_silent_when_nothing_to_judge() -> None:
    """没有候选 / 没评估过指标 → 不得报警（否则噪声会把这条真信号淹掉）。"""
    assert check_overextension_inputs([]).ok is True
    assert check_overextension_inputs(None).ok is True
    empty_metrics = [{"overextension": {"level": "none", "metrics": {}}}]
    assert check_overextension_inputs(empty_metrics).ok is True
    assert check_overextension_inputs([{"symbol": "000001"}]).ok is True
