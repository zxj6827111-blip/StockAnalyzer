"""M3 R3 最终 Blocking 修复的对抗测试（BLK-R2-1 / BLK-R2-2 / N-R2-1）。

覆盖上一轮独立复核的两个 Blocking 穿透 + 一个部署前必做的接线：

- **BLK-R2-1**：写入日必须等于真实墙钟；生产 epoch（``deterministic_clock=false``）
  不接受任何"自称写入日"，KPI 另有一道 ``recorded_at`` 兜底闸；
- **BLK-R2-2**：``.build_commit`` 与 ``build_manifest.json`` 双源必须同时存在、
  可解析、trusted、clean 且与 git HEAD / requested code_commit 四值一致；
- **N-R2-1**：capture 读当天 data_health 工件（S08 契约），缺失/陈旧/降级
  一律不进 clean OOS，但**不阻塞** Shadow 记录。
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from _alpha_v2_m3_fixtures import (
    capture_at,
    open_epoch_for_manifest,
    shadow_row_identity,
    write_freeze_manifest,
)

from stock_analyzer.alpha_v2.validation.data_health_capture import (
    capture_data_health_block,
    data_health_gate_ok,
    load_data_health_artifact,
    write_data_health_artifact,
)
from stock_analyzer.alpha_v2.validation.freeze_precheck import (
    FreezeGateError,
    assert_build_identity,
    resolve_code_commit,
)
from stock_analyzer.alpha_v2.validation.outcome_maturation import outcome_path
from stock_analyzer.alpha_v2.validation.runtime_identity import build_identity_block
from stock_analyzer.alpha_v2.validation.shadow_capture import (
    ShadowCaptureError,
    ShadowClockNotAuthorizedError,
    ShadowLateWriteError,
    build_shadow_rows,
    capture_clock_policy,
    read_shadow_rows,
    write_shadow_snapshot,
)
from stock_analyzer.alpha_v2.validation.validation_kpis import build_validation_kpi

REPO_ROOT = Path(__file__).resolve().parents[1]


def _today() -> date:
    from datetime import datetime

    return datetime.now().astimezone().date()


def _production_epoch(tmp_path, *, earliest_days_back: int):
    """生产形态 epoch（deterministic_clock=False）：写入日只能是系统真实日期。"""
    today = _today()
    start = today - timedelta(days=earliest_days_back)
    manifest = write_freeze_manifest(
        tmp_path,
        validation_mode="production",
        deterministic_clock=False,
        validation_start_date=start.isoformat(),
    )
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date=start.isoformat())
    return epoch, today


def _rows(epoch, day: date, *, health: object = None, symbol: str = "600000"):
    candidate: dict[str, object] = {"symbol": symbol, "alpha_rank": 0.9}
    if health is not None:
        candidate["data_health"] = health
    return build_shadow_rows(
        signal_date=day,
        signal_time="15:35",
        epoch=epoch,
        candidates=[candidate],
        identity=shadow_row_identity(epoch),
    )


def _write_outcome(root: Path, epoch_id: str, day: date, symbol: str = "600000") -> None:
    path = outcome_path(root, epoch_id, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "signal_date": day.isoformat(),
                "symbol": symbol,
                "executable": True,
                # 夹具与生产同形：真实 outcome 行恒带这两列（KPI 第二道闸按行判）。
                "price_mode": "raw",
                "price_mode_certified": True,
                "matured_5d": True,
                "net_return_5d": 0.05,
                "excess_return_5d": 0.02,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _ok_health(day: date, *, status: str = "ok") -> dict[str, object]:
    return {
        "schema": "alpha_v2_capture_data_health.v1",
        "status": status,
        "source_status": status,
        "as_of": day.isoformat(),
        "generated_at": f"{day.isoformat()}T20:30:00+08:00",
        "coverage": 0.99,
        "source": "test-fixture",
        "aligned_to_signal_date": True,
        "detail": "同日工件可用",
    }


# ---------------------------------------------------------------------------
# BLK-R2-1 —— 写入日 = 真实墙钟
# ---------------------------------------------------------------------------


def test_write_shadow_snapshot_has_no_self_claimed_capture_date(tmp_path):
    """R2 的攻击面（capture_date 参数）必须从 API 里彻底消失。"""
    signature = inspect.signature(write_shadow_snapshot)
    assert "capture_date" not in signature.parameters
    manifest = write_freeze_manifest(tmp_path)
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date="2026-09-18")
    rows = _rows(epoch, date(2026, 9, 18))
    assert rows
    with capture_at(date(2026, 9, 18)):
        write_shadow_snapshot(
            root=tmp_path, epoch=epoch, signal_date=date(2026, 9, 18), rows=rows
        )
    stored = read_shadow_rows(tmp_path, epoch.epoch_id, date(2026, 9, 18))[0]
    # 真实写入日与时钟来源都落在行上（审计可见）
    assert stored["actual_capture_date"] == "2026-09-18"
    assert stored["deterministic_clock"] is True  # 夹具清单授权了确定性时钟


def test_production_epoch_refuses_clock_injection(tmp_path):
    """函数层：生产 epoch（deterministic_clock=false）不接受被固定的时钟。"""
    manifest = write_freeze_manifest(
        tmp_path, validation_mode="production", deterministic_clock=False
    )
    assert manifest["deterministic_clock"] is False
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date="2026-09-18")
    rows = _rows(epoch, date(2026, 9, 18))
    with capture_at(date(2026, 9, 18)), pytest.raises(ShadowClockNotAuthorizedError):
        write_shadow_snapshot(root=tmp_path, epoch=epoch, signal_date=date(2026, 9, 18), rows=rows)
    assert read_shadow_rows(tmp_path, epoch.epoch_id, date(2026, 9, 18)) == []


def test_production_never_allows_clock_injection_even_if_flag_true(tmp_path):
    """**production 没有任何开关能打开确定性时钟**（即使清单写了 true）。"""
    manifest = write_freeze_manifest(
        tmp_path, validation_mode="production", deterministic_clock=True
    )
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date="2026-09-18")
    rows = _rows(epoch, date(2026, 9, 18))
    assert capture_clock_policy(tmp_path)["clock_injection_allowed"] is False
    with capture_at(date(2026, 9, 18)), pytest.raises(ShadowClockNotAuthorizedError):
        write_shadow_snapshot(root=tmp_path, epoch=epoch, signal_date=date(2026, 9, 18), rows=rows)
    # test 模式才允许（本套测试的 clean 场景走 test 模式；CLI 产不出该模式）
    test_root = tmp_path / "test_mode"
    test_root.mkdir()
    write_freeze_manifest(test_root, validation_mode="test", deterministic_clock=True)
    assert capture_clock_policy(test_root)["clock_injection_allowed"] is True
    # rehearsal 也允许（但 rehearsal 永不计 clean）
    rehearsal_root = tmp_path / "rehearsal_mode"
    rehearsal_root.mkdir()
    write_freeze_manifest(rehearsal_root, validation_mode="rehearsal")
    assert capture_clock_policy(rehearsal_root)["clock_injection_allowed"] is True


def test_production_cli_rejects_capture_date_flag(tmp_path):
    """生产 CLI：--capture-date 直接拒绝（非零退出、不落任何快照）。"""
    manifest = write_freeze_manifest(
        tmp_path,
        validation_mode="production",
        deterministic_clock=False,
        validation_start_date="2026-04-03",
    )
    open_epoch_for_manifest(tmp_path, manifest, opened_on_date="2026-04-03")
    out = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "alpha_v2_shadow_capture.py"),
            "--epoch-id", "alpha_v2_epoch_001",
            "--signal-date", "2026-04-03",
            "--capture-date", "2026-04-03",  # ← R2 的攻击方式
            "--out", str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert out.returncode == 8, (out.returncode, out.stderr[-400:])
    assert "--capture-date" in out.stderr
    assert not list(tmp_path.rglob("shadow_*.jsonl"))


def test_late_write_t_plus_1_rejected(tmp_path):
    """T+1 补写 T 日：生产 epoch 里没有可用的"自称写入日"，直接拒。"""
    epoch, today = _production_epoch(tmp_path, earliest_days_back=6)
    signal = today - timedelta(days=1)
    with pytest.raises(ShadowLateWriteError):
        write_shadow_snapshot(
            root=tmp_path, epoch=epoch, signal_date=signal, rows=_rows(epoch, signal)
        )
    assert read_shadow_rows(tmp_path, epoch.epoch_id, signal) == []


def test_late_write_t_plus_5_rejected(tmp_path):
    epoch, today = _production_epoch(tmp_path, earliest_days_back=10)
    signal = today - timedelta(days=5)
    with pytest.raises(ShadowLateWriteError):
        write_shadow_snapshot(
            root=tmp_path, epoch=epoch, signal_date=signal, rows=_rows(epoch, signal)
        )


def test_future_write_rejected(tmp_path):
    """反向也不行：T 日快照不能在 T 日之前写。"""
    epoch, today = _production_epoch(tmp_path, earliest_days_back=2)
    signal = today + timedelta(days=1)
    with pytest.raises(ShadowLateWriteError):
        write_shadow_snapshot(
            root=tmp_path, epoch=epoch, signal_date=signal, rows=_rows(epoch, signal)
        )


def test_explicit_backfill_is_allowed_and_marked(tmp_path):
    """显式 backfill：允许，但必须落全四个审计字段且永不进 clean。"""
    epoch, today = _production_epoch(tmp_path, earliest_days_back=5)
    signal = today - timedelta(days=3)
    path = write_shadow_snapshot(
        root=tmp_path,
        epoch=epoch,
        signal_date=signal,
        rows=_rows(epoch, signal),
        allow_backfill=True,
        backfill_reason="upstream_late_recovery",
    )
    assert path.exists()
    stored = read_shadow_rows(tmp_path, epoch.epoch_id, signal)[0]
    assert stored["backfilled"] is True
    assert stored["clean_oos_eligible"] is False
    assert stored["backfill_reason"] == "upstream_late_recovery"
    assert stored["actual_capture_date"] == today.isoformat()
    assert stored["signal_date"] == signal.isoformat()
    # 没有 reason 的补写不允许
    other = tmp_path / "other"
    other.mkdir()
    epoch2, today2 = _production_epoch(other, earliest_days_back=5)
    signal2 = today2 - timedelta(days=2)
    with pytest.raises(ShadowCaptureError):
        write_shadow_snapshot(
            root=other,
            epoch=epoch2,
            signal_date=signal2,
            rows=_rows(epoch2, signal2),
            allow_backfill=True,
            backfill_reason="",
        )


def test_forged_recorded_at_is_not_clean(tmp_path):
    """KPI 第二道闸：行 recorded_at 与 signal_date 不同日 → 该日不进 clean。"""
    manifest = write_freeze_manifest(tmp_path)
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date="2026-09-01")
    day = date(2026, 9, 1)
    rows = _rows(epoch, day, health=_ok_health(day))
    # 直接伪造落盘：记录时刻写成别的日期（模拟"绕过捕获层的补写"）
    rows[0]["recorded_at"] = "2026-09-19T21:45:00+08:00"
    rows[0]["actual_capture_date"] = "2026-09-19"
    with capture_at(day):
        write_shadow_snapshot(root=tmp_path, epoch=epoch, signal_date=day, rows=rows)
    report = build_validation_kpi(root=tmp_path, epoch=epoch)
    governance = report["governance"]
    assert governance["clean_oos_days"] == 0
    assert governance["late_recorded_days"] == 1
    assert "late_recorded_at" in governance["by_date"][0]["reasons"]


def test_twenty_forged_historical_days_never_enter_clean_oos(tmp_path):
    """复刻 Codex 上一轮的攻击：20 个历史日"自称当天写入"。"""
    epoch, today = _production_epoch(tmp_path, earliest_days_back=21)
    days = [today - timedelta(days=offset) for offset in range(20, 0, -1)]
    rejected = 0
    for day in days:
        # 攻击 1：把墙上时钟固定回当天（生产 epoch 未授权）→ 必须拒
        with capture_at(day), pytest.raises(ShadowClockNotAuthorizedError):
            write_shadow_snapshot(
                root=tmp_path, epoch=epoch, signal_date=day, rows=_rows(epoch, day)
            )
        rejected += 1
        # 攻击 2：走显式 backfill 落账 —— 允许，但只能是 not-clean
        write_shadow_snapshot(
            root=tmp_path,
            epoch=epoch,
            signal_date=day,
            rows=_rows(epoch, day),
            allow_backfill=True,
            backfill_reason="attack-simulation",
        )
        _write_outcome(tmp_path, epoch.epoch_id, day)
    assert rejected == 20
    stored = read_shadow_rows(tmp_path, epoch.epoch_id, days[0])[0]
    assert stored["backfilled"] is True
    assert stored["actual_capture_date"] == today.isoformat()

    report = build_validation_kpi(root=tmp_path, epoch=epoch)
    governance = report["governance"]
    gates = report["sample_gate_status"]["failure_alert"]
    assert governance["captured_days"] == 20
    assert governance["clean_oos_days"] == 0
    assert governance["backfilled_days"] == 20
    assert governance["coverage_rate"] == 0.0
    assert gates["reached"] is False
    assert gates["primary_5d_reached"] is False
    assert report["clean_maturity"]["mature_dates_5d"] == 0


# ---------------------------------------------------------------------------
# BLK-R2-2 —— 构建身份四值一致
# ---------------------------------------------------------------------------


def _identity_kwargs(**overrides):
    base = dict(
        git_head="a" * 40,
        requested_code_commit="a" * 40,
        build_commit_file="a" * 40,
        build_manifest_commit="a" * 40,
        build_manifest_present=True,
        build_manifest_trusted=True,
        build_manifest_dirty=False,
    )
    base.update(overrides)
    return base


def test_build_identity_four_values_consistent_passes():
    assert assert_build_identity(**_identity_kwargs()) == "a" * 40


@pytest.mark.parametrize(
    "broken",
    [
        {"build_commit_file": ""},          # .build_commit 缺失
        {"build_manifest_present": False},  # build_manifest.json 缺失
        {"build_manifest_commit": ""},      # manifest 里 commit 缺失
        {"build_commit_file": "b" * 40},    # 双源矛盾
        {"build_manifest_commit": "b" * 40},
        {"git_head": "b" * 40},             # git HEAD 与 requested 不符
        {"requested_code_commit": ""},      # requested 缺失
        {"build_manifest_trusted": False},  # 未信任
        {"build_manifest_trusted": "unknown"},
        {"build_manifest_dirty": True},     # 脏构建
        {"build_manifest_dirty": "unknown"},
    ],
)
def test_build_identity_fail_closed_matrix(broken):
    with pytest.raises(FreezeGateError) as excinfo:
        assert_build_identity(**_identity_kwargs(**broken))
    assert excinfo.value.exit_code == 5


def test_build_identity_allows_git_unavailable_only_with_two_sources(tmp_path):
    """容器内 git 不可得：必须靠两源互证（而且 requested 必须显式给）。"""
    (tmp_path / ".build_commit").write_text("c" * 40, encoding="utf-8")
    (tmp_path / "build_manifest.json").write_text(
        json.dumps({"commit": "c" * 40, "dirty": False}), encoding="utf-8"
    )
    block = build_identity_block(str(tmp_path))
    # git 不在这个临时目录里 → unknown；两源都在且一致 → 放行
    assert block["git_head"] in {"", "unknown"}
    assert assert_build_identity(
        git_head=str(block["git_head"]),
        requested_code_commit="c" * 40,
        build_commit_file=str(block["build_commit_file"]),
        build_manifest_commit=str(block["build_manifest_commit"]),
        build_manifest_present=bool(block["build_manifest_present"]),
        build_manifest_trusted=block["build_manifest_trusted"],
        build_manifest_dirty=block["build_manifest_dirty"],
    ) == "c" * 40
    # 把 manifest 改成别的 commit：双源矛盾 → 拒绝
    (tmp_path / "build_manifest.json").write_text(
        json.dumps({"commit": "d" * 40, "dirty": False}), encoding="utf-8"
    )
    block2 = build_identity_block(str(tmp_path))
    with pytest.raises(FreezeGateError):
        assert_build_identity(
            git_head=str(block2["git_head"]),
            requested_code_commit="c" * 40,
            build_commit_file=str(block2["build_commit_file"]),
            build_manifest_commit=str(block2["build_manifest_commit"]),
            build_manifest_present=bool(block2["build_manifest_present"]),
            build_manifest_trusted=block2["build_manifest_trusted"],
            build_manifest_dirty=block2["build_manifest_dirty"],
        )


def test_build_identity_single_source_is_not_enough(tmp_path):
    """只有一源（哪怕它是对的）也不算数——这正是 R2 的漏洞形态。"""
    (tmp_path / ".build_commit").write_text("e" * 40, encoding="utf-8")
    block = build_identity_block(str(tmp_path))
    assert block["build_manifest_present"] is False
    with pytest.raises(FreezeGateError):
        assert_build_identity(
            git_head="unknown",
            requested_code_commit="e" * 40,
            build_commit_file=str(block["build_commit_file"]),
            build_manifest_commit="",
            build_manifest_present=False,
            build_manifest_trusted=False,
            build_manifest_dirty="unknown",
        )
    # 反过来：只有 manifest、缺 .build_commit 一样拒绝
    other = tmp_path / "only_manifest"
    other.mkdir()
    (other / "build_manifest.json").write_text(
        json.dumps({"commit": "f" * 40, "dirty": False}), encoding="utf-8"
    )
    block2 = build_identity_block(str(other))
    with pytest.raises(FreezeGateError):
        assert_build_identity(
            git_head="unknown",
            requested_code_commit="f" * 40,
            build_commit_file=str(block2["build_commit_file"]),
            build_manifest_commit=str(block2["build_manifest_commit"]),
            build_manifest_present=True,
            build_manifest_trusted=block2["build_manifest_trusted"],
            build_manifest_dirty=block2["build_manifest_dirty"],
        )


def test_requested_code_commit_must_match_git_head():
    assert resolve_code_commit("", git_head_value="a" * 40) == ("a" * 40, "git_rev_parse")
    assert resolve_code_commit("a" * 40, git_head_value="a" * 40)[1] == "cli_override_verified"
    with pytest.raises(FreezeGateError):
        resolve_code_commit("b" * 40, git_head_value="a" * 40)
    with pytest.raises(FreezeGateError):
        resolve_code_commit("", git_head_value="unknown")
    # 容器内 git 不可得：允许显式给值，但来源标记必须如实
    assert resolve_code_commit("a" * 40, git_head_value="unknown") == (
        "a" * 40,
        "cli_override_git_unavailable",
    )


# ---------------------------------------------------------------------------
# N-R2-1 —— data_health 捕获接线
# ---------------------------------------------------------------------------


def _epoch_with_rows(tmp_path, *, days, health_factory, deterministic=True):
    manifest = write_freeze_manifest(tmp_path, deterministic_clock=deterministic)
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date=days[0].isoformat())
    for day in days:
        with capture_at(day):
            write_shadow_snapshot(
                root=tmp_path,
                epoch=epoch,
                signal_date=day,
                rows=_rows(epoch, day, health=health_factory(day)),
            )
        _write_outcome(tmp_path, epoch.epoch_id, day)
    return epoch


def test_data_health_same_day_ok_is_clean(tmp_path):
    day = date(2026, 9, 1)
    epoch = _epoch_with_rows(tmp_path, days=[day], health_factory=_ok_health)
    report = build_validation_kpi(root=tmp_path, epoch=epoch)
    assert report["governance"]["clean_oos_days"] == 1
    assert report["governance"]["coverage_rate"] == 1.0
    assert report["clean_maturity"]["mature_dates_5d"] == 1


@pytest.mark.parametrize(
    "status, expected_reason",
    [
        ("degraded", "data_health_not_available"),
        ("broken", "data_health_not_available"),
        ("not_available", "data_health_not_available"),
        ("invalid", "data_health_not_available"),
        ("stale", "data_health_stale"),
    ],
)
def test_data_health_not_ok_blocks_clean_but_not_capture(tmp_path, status, expected_reason):
    day = date(2026, 9, 1)
    epoch = _epoch_with_rows(
        tmp_path, days=[day], health_factory=lambda d: _ok_health(d, status=status)
    )
    stored = read_shadow_rows(tmp_path, epoch.epoch_id, day)
    assert len(stored) == 1, "Shadow 记录本身必须照写（不被 data_health 阻塞）"
    report = build_validation_kpi(root=tmp_path, epoch=epoch)
    governance = report["governance"]
    assert governance["clean_oos_days"] == 0
    assert governance["excluded_data_health_days"] == 1
    assert expected_reason in governance["by_date"][0]["reasons"]
    assert report["sample_gate_status"]["failure_alert"]["reached"] is False


def test_data_health_missing_artifact_is_not_available(tmp_path):
    block = capture_data_health_block(signal_date=date(2026, 9, 1), payload=None)
    assert block["status"] == "not_available"
    assert block["source_status"] == "missing"
    assert block["aligned_to_signal_date"] is False
    ok, reason = data_health_gate_ok(block, date(2026, 9, 1))
    assert ok is False and reason == "data_health_not_available"
    # 文件式读不到也一样
    payload, source = load_data_health_artifact(path=tmp_path / "nope.json")
    assert payload == {} and source == "missing"


def test_data_health_stale_date_is_detected(tmp_path):
    """昨天的 healthy 不能当今天的 ok。"""
    payload = {
        "schema": "alpha_v2_data_health.v1",
        "status": "healthy",
        "as_of": "2026-08-31",
        "generated_at": "2026-08-31T20:30:00+08:00",
        "coverage_ratio": 0.99,
    }
    block = capture_data_health_block(signal_date=date(2026, 9, 1), payload=payload, source="x")
    assert block["status"] == "stale"
    assert block["source_status"] == "healthy"
    assert block["aligned_to_signal_date"] is False
    ok, reason = data_health_gate_ok(block, date(2026, 9, 1))
    assert ok is False and reason == "data_health_stale"
    # 同日同载荷 → ok
    same_day = capture_data_health_block(
        signal_date=date(2026, 8, 31), payload=payload, source="x"
    )
    assert same_day["status"] == "ok"
    assert data_health_gate_ok(same_day, date(2026, 8, 31)) == (True, "")


def test_data_health_artifact_roundtrip_and_schema(tmp_path):
    """工件读写按 S08 契约（status 词表/字段），并保留捕获所需的 5 个字段。"""
    payload = {
        "as_of": "2026-09-01",
        "status": "healthy",
        "coverage_ratio": 0.987,
        "checks": [],
        "missing_artifacts": [],
        "generated_at": "2026-09-01T20:30:00+08:00",
    }
    target = write_data_health_artifact(payload, tmp_path / "runtime" / "data_health.json")
    loaded, source = load_data_health_artifact(path=target)
    assert source == str(target)
    block = capture_data_health_block(
        signal_date=date(2026, 9, 1), payload=loaded, source=source
    )
    for key in ("status", "as_of", "generated_at", "coverage", "source"):
        assert key in block
    assert block["status"] == "ok" and block["coverage"] == pytest.approx(0.987)
    assert block["generated_at"] == "2026-09-01T20:30:00+08:00"


def test_data_health_unknown_status_is_invalid():
    block = capture_data_health_block(
        signal_date=date(2026, 9, 1),
        payload={"status": "weird", "as_of": "2026-09-01"},
        source="x",
    )
    assert block["status"] == "invalid"
    ok, reason = data_health_gate_ok(block, date(2026, 9, 1))
    assert ok is False and reason == "data_health_not_available"


def test_data_health_explicit_not_available_status_is_faithful():
    """工件自己写 not_available 时，有效状态就是 not_available（不是 invalid）。"""
    block = capture_data_health_block(
        signal_date=date(2026, 9, 1),
        payload={"status": "not_available", "as_of": "2026-09-01"},
        source="x",
    )
    assert block["status"] == "not_available"
    assert data_health_gate_ok(block, date(2026, 9, 1)) == (False, "data_health_not_available")
