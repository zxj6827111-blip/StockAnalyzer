"""M3 修复轮对抗测试：覆盖 F1-F7 所有曾被独立验收判 GAP 的入口。

同一个模式：先以"攻击姿态"重现上一轮判定的失败，同时声明现在应当如何被拦下；
补写/排演路径可以存在，但资格标记必须让这两天不再进 clean OOS。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from _alpha_v2_m3_fixtures import (
    capture_at,
    open_epoch_for_manifest,
    shadow_row_identity,
    write_freeze_manifest,
)
from _alpha_v2_research_helpers import DAYS, flat_panel, matcher

from stock_analyzer.alpha_v2.validation.epoch import (
    EpochRegistryError,
    close_epoch,
    epoch_identity_matches,
    open_epoch,
    require_epoch_identity_match,
)
from stock_analyzer.alpha_v2.validation.freeze import (
    build_validation_freeze,
    write_validation_freeze,
)
from stock_analyzer.alpha_v2.validation.freeze_precheck import (
    FreezeGateError,
    assert_build_identity,
    assert_execution_price_raw,
    assert_validation_start_date,
    assert_worktree_clean,
    resolve_code_commit,
    resolve_feature_schema_columns,
)
from stock_analyzer.alpha_v2.validation.outcome_maturation import (
    mature_epoch_outcomes,
    outcome_path,
)
from stock_analyzer.alpha_v2.validation.shadow_capture import (
    ShadowLateWriteError,
    ShadowMissingDayConflictError,
    ShadowTamperError,
    build_shadow_rows,
    list_missing_days,
    read_shadow_rows,
    record_missing_prediction_day,
    write_shadow_snapshot,
)
from stock_analyzer.alpha_v2.validation.validation_kpis import (
    build_validation_kpi,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- F1 写入窗口


def test_late_write_t_plus_1_rejected(tmp_path):
    manifest = write_freeze_manifest(tmp_path)
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date="2026-09-18")
    ident = shadow_row_identity(epoch)
    rows = build_shadow_rows(
        signal_date=date(2026, 9, 18), signal_time="15:35", epoch=epoch,
        candidates=[{"symbol": "600000", "alpha_rank": 0.9}], identity=ident,
    )
    with capture_at(date(2026, 9, 19)), pytest.raises(ShadowLateWriteError):
        write_shadow_snapshot(
            root=tmp_path, epoch=epoch, signal_date=date(2026, 9, 18), rows=rows
        )
    assert read_shadow_rows(tmp_path, epoch.epoch_id, date(2026, 9, 18)) == []


def test_late_write_t_plus_5_rejected(tmp_path):
    manifest = write_freeze_manifest(tmp_path)
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date="2026-09-18")
    ident = shadow_row_identity(epoch)
    rows = build_shadow_rows(
        signal_date=date(2026, 9, 18), signal_time="15:35", epoch=epoch,
        candidates=[{"symbol": "600000", "alpha_rank": 0.9}], identity=ident,
    )
    with capture_at(date(2026, 9, 23)), pytest.raises(ShadowLateWriteError):
        write_shadow_snapshot(
            root=tmp_path, epoch=epoch, signal_date=date(2026, 9, 18), rows=rows
        )


def test_pre_validation_start_rejected(tmp_path):
    manifest = write_freeze_manifest(tmp_path)
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date="2026-09-18")
    ident = shadow_row_identity(epoch)
    rows = build_shadow_rows(
        signal_date=date(2026, 9, 17), signal_time="15:35", epoch=epoch,
        candidates=[{"symbol": "600000", "alpha_rank": 0.9}], identity=ident,
    )
    # signal_date 早于 validation_start_date：同口径拒绝，与"当日"无关
    with capture_at(date(2026, 9, 17)), pytest.raises(ShadowLateWriteError):
        write_shadow_snapshot(
            root=tmp_path, epoch=epoch, signal_date=date(2026, 9, 17), rows=rows
        )


def test_missing_then_backfill_requires_explicit_flag(tmp_path):
    manifest = write_freeze_manifest(tmp_path)
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date="2026-09-18")
    ident = shadow_row_identity(epoch)
    record_missing_prediction_day(
        root=tmp_path, epoch=epoch, signal_date=date(2026, 9, 18),
        reason="上游未备数",
    )
    rows = build_shadow_rows(
        signal_date=date(2026, 9, 18), signal_time="15:35", epoch=epoch,
        candidates=[{"symbol": "600000", "alpha_rank": 0.9}], identity=ident,
    )
    # 默认：同一天的 missing 台账挡住静默补写（即便在窗口内）
    with capture_at(date(2026, 9, 18)), pytest.raises(ShadowMissingDayConflictError):
        write_shadow_snapshot(
            root=tmp_path, epoch=epoch, signal_date=date(2026, 9, 18), rows=rows
        )

    # 显式 backfill：允许——但行落 backfilled=true、clean_oos_eligible=false
    with capture_at(date(2026, 9, 19)):
        path = write_shadow_snapshot(
            root=tmp_path, epoch=epoch, signal_date=date(2026, 9, 18), rows=rows,
            allow_backfill=True, backfill_reason="upstream_late_recovery",
        )
    assert path.exists()
    stored = read_shadow_rows(tmp_path, epoch.epoch_id, date(2026, 9, 18))
    assert stored[0]["backfilled"] is True
    assert stored[0]["backfill_reason"] == "upstream_late_recovery"
    assert stored[0]["clean_oos_eligible"] is False
    # missing 台账仍是同一行，不吞冲突记录
    assert len(list_missing_days(tmp_path, epoch.epoch_id)) == 1


def test_snapshot_then_missing_rejected(tmp_path):
    manifest = write_freeze_manifest(tmp_path)
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date="2026-09-18")
    ident = shadow_row_identity(epoch)
    rows = build_shadow_rows(
        signal_date=date(2026, 9, 18), signal_time="15:35", epoch=epoch,
        candidates=[{"symbol": "600000", "alpha_rank": 0.9}], identity=ident,
    )
    with capture_at(date(2026, 9, 18)):
        write_shadow_snapshot(
            root=tmp_path, epoch=epoch, signal_date=date(2026, 9, 18), rows=rows
        )
    with pytest.raises(ShadowMissingDayConflictError):
        record_missing_prediction_day(
            root=tmp_path, epoch=epoch, signal_date=date(2026, 9, 18),
            reason="事后想把这一天剔除",  # 不允许：快照已冻结
        )


# ---------------------------------------------------------------- F2 身份


def test_manifest_replacement_rejected_on_write(tmp_path):
    manifest = write_freeze_manifest(tmp_path)
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date="2026-09-18")
    # 把磁盘上的清单替换成另一份（同 epoch id，别的内容）
    other = build_validation_freeze(
        validation_epoch_id="alpha_v2_epoch_001",
        code_commit="deadbeef" * 5,
        git_branch="test",
        config_hash="f" * 64,
        config_hash_scope="test",
        model=dict(manifest["model"]),
        feature_columns=list(manifest["feature_schema"]["feature_columns"]),
        feature_group_ids=list(manifest["feature_schema"]["group_ids"]),
        selection_contract=dict(manifest["selection_contract"]),
        execution_price_mode="raw",
        feature_price_mode="qfq",
        validation_start_date="2026-09-18",
        created_at=manifest["created_at"],
    )
    write_validation_freeze(other, root=tmp_path)  # 覆盖了锚定的清单
    ident = shadow_row_identity(epoch)
    rows = build_shadow_rows(
        signal_date=date(2026, 9, 18), signal_time="15:35", epoch=epoch,
        candidates=[{"symbol": "600000", "alpha_rank": 0.9}], identity=ident,
    )
    with capture_at(date(2026, 9, 18)), pytest.raises(EpochRegistryError):
        write_shadow_snapshot(
            root=tmp_path, epoch=epoch, signal_date=date(2026, 9, 18), rows=rows
        )


def test_manifest_hash_spoof_rejected(tmp_path):
    """清单内容留着但把 hash 字段换成 epoch 锚定的那份——锚定过、完整性不放过。"""
    manifest = write_freeze_manifest(tmp_path)
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date="2026-09-18")
    tampered = json.loads(json.dumps(manifest))
    tampered["execution_price_mode"] = "qfq"  # 内容变了…
    tampered["freeze_manifest_hash"] = epoch.freeze_manifest_hash  # …然后冒充锚定值
    # 直接绕过 write_validation_freeze 的本体检查，伪造文件落盘
    from stock_analyzer.alpha_v2.validation.freeze import freeze_manifest_path

    freeze_manifest_path(tmp_path).write_text(
        json.dumps(tampered, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(EpochRegistryError):
        require_epoch_identity_match(root=tmp_path, epoch_id=epoch.epoch_id)


def test_identity_drift_on_shadow_write_rejected(tmp_path):
    manifest = write_freeze_manifest(tmp_path)
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date="2026-09-18")
    ident = shadow_row_identity(epoch)
    for drift_key, drift_value in (
        ("code_commit", "d" * 40),
        ("config_hash", "cfg-other"),
        ("model_artifact_hash", "z" * 64),
        ("selection_contract_id", "other_contract"),
    ):
        drifted = dict(ident, **{drift_key: drift_value})
        rows = build_shadow_rows(
            signal_date=date(2026, 9, 18), signal_time="15:35", epoch=epoch,
            candidates=[{"symbol": "600000", "alpha_rank": 0.9}], identity=drifted,
        )
        with capture_at(date(2026, 9, 18)), pytest.raises(
            (ShadowTamperError, EpochRegistryError)
        ):
            write_shadow_snapshot(
                root=tmp_path, epoch=epoch, signal_date=date(2026, 9, 18), rows=rows
            )


def test_identity_missing_key_fails_on_both_axes(tmp_path):
    manifest = write_freeze_manifest(tmp_path)
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date="2026-09-18")
    ident = shadow_row_identity(epoch)
    # 运行侧缺了 key——strict 判断下必须判违例（修复前的"两边都在才比"已不存在）
    broken = dict(ident)
    broken.pop("label_policy_hash")
    violations = epoch_identity_matches(epoch, broken)
    assert any("label_policy_hash:runtime_identity_missing" in v for v in violations)

    # epoch 记录缺 key 也得判违例——打一个手工开的 epoch（身份键不全）
    raw_record = open_epoch(
        root=tmp_path / "other",
        epoch_id="alpha_v2_epoch_001",
        freeze_manifest_hash="f" * 64,
        identity={"code_commit": "c" * 40},  # 只给了 1 键
        opened_on_date="2026-09-18",
    )
    violations = epoch_identity_matches(raw_record, ident)
    assert any("execution_price_mode:epoch_identity_missing" in v for v in violations)


# ---------------------------------------------------------------- F3 / F4 / F5


def test_closed_epoch_blocks_mature_and_keeps_files(tmp_path):
    manifest = write_freeze_manifest(tmp_path)
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date="2026-01-05")
    panel = flat_panel(
        ["600000", "600001"],
        closes={s: [10.0 + 0.02 * i for i in range(45)] for s in ["600000", "600001"]},
    )
    rows = build_shadow_rows(
        signal_date=DAYS[5], signal_time="15:35", epoch=epoch,
        candidates=[{"symbol": "600000"}, {"symbol": "600001"}],
        identity=shadow_row_identity(epoch),
    )
    with capture_at(DAYS[5]):
        write_shadow_snapshot(root=tmp_path, epoch=epoch, signal_date=DAYS[5], rows=rows)
    mature_epoch_outcomes(
        root=tmp_path, epoch=epoch, panel=panel, evaluation_date=DAYS[30],
        matcher=matcher(), slippage_ratio=0.0015, price_mode="raw",
        price_mode_certified=True,
    )
    path = outcome_path(tmp_path, epoch.epoch_id, DAYS[5])
    before = path.read_bytes()
    close_epoch(root=tmp_path, epoch_id=epoch.epoch_id, reason="closed for test")
    with pytest.raises(EpochRegistryError):
        mature_epoch_outcomes(
            root=tmp_path, epoch=epoch, panel=panel, evaluation_date=DAYS[35],
            matcher=matcher(), slippage_ratio=0.0015, price_mode="raw",
            price_mode_certified=True,
        )
    assert path.read_bytes() == before


def test_non_raw_execution_rejected_by_precheck():
    with pytest.raises(FreezeGateError) as exc:
        assert_execution_price_raw("qfq", validation_mode="production")
    assert exc.value.exit_code == 4
    # rehearsal 模式：让规则失误但不会冒充 clean OOS
    assert_execution_price_raw("qfq", validation_mode="rehearsal")


def test_dirty_worktree_rejected_by_precheck():
    with pytest.raises(FreezeGateError) as exc:
        assert_worktree_clean([" M src/foo.py"], validation_mode="production")
    assert exc.value.exit_code == 5
    # 明确白名单外的未跟踪文件同样算脏
    with pytest.raises(FreezeGateError):
        assert_worktree_clean(["?? untracked_new_module.py"], validation_mode="production")


def _build_identity_kwargs(**overrides):
    base = dict(
        git_head="c" * 40,
        requested_code_commit="c" * 40,
        build_commit_file="c" * 40,
        build_manifest_commit="c" * 40,
        build_manifest_present=True,
        build_manifest_trusted=True,
        build_manifest_dirty=False,
    )
    base.update(overrides)
    return base


def test_build_identity_required_and_must_match():
    # 四值一致 → 放行（返回 code_commit）
    assert assert_build_identity(**_build_identity_kwargs()) == "c" * 40
    # 任一缺失/不一致/未信任/脏构建 → 拒绝（完整 8 例矩阵见 R3 文件）
    for broken in (
        {"build_manifest_present": False},
        {"build_commit_file": ""},
        {"build_manifest_commit": "a" * 40},
        {"build_commit_file": "a" * 40},
        {"git_head": "a" * 40},
        {"build_manifest_trusted": False},
        {"build_manifest_dirty": True},
    ):
        with pytest.raises(FreezeGateError):
            assert_build_identity(**_build_identity_kwargs(**broken))


def test_code_commit_resolution():
    assert resolve_code_commit("", git_head_value="c" * 40) == ("c" * 40, "git_rev_parse")
    assert resolve_code_commit("c" * 40, git_head_value="c" * 40) == (
        "c" * 40,
        "cli_override_verified",
    )
    with pytest.raises(FreezeGateError):
        resolve_code_commit("a" * 40, git_head_value="c" * 40)
    with pytest.raises(FreezeGateError):
        resolve_code_commit("", git_head_value="unknown")


def test_feature_schema_gates():
    with pytest.raises(FreezeGateError):
        resolve_feature_schema_columns(
            file_columns=[], model_columns=[], validation_mode="production"
        )
    with pytest.raises(FreezeGateError):
        resolve_feature_schema_columns(
            file_columns=["a"], model_columns=["b"], validation_mode="production"
        )
    r1 = resolve_feature_schema_columns(
        file_columns=["a", "b"], model_columns=["b", "a"], validation_mode="production"
    )
    assert r1.feature_columns == ("a", "b")
    r2 = resolve_feature_schema_columns(
        file_columns=[], model_columns=["b", "a"], validation_mode="production"
    )
    assert r2.feature_columns == ("a", "b") and r2.source == "model_artifact"
    r3 = resolve_feature_schema_columns(
        file_columns=[], model_columns=[], validation_mode="rehearsal"
    )
    assert r3.feature_columns == ()


def test_statistics_start_date_backdated_rejected():
    with pytest.raises(FreezeGateError):
        assert_validation_start_date(
            "2020-01-01", today=date(2026, 9, 19), validation_mode="production"
        )
    with pytest.raises(FreezeGateError):
        assert_validation_start_date("", today=date(2026, 9, 19), validation_mode="production")
    assert_validation_start_date(
        "2026-09-19", today=date(2026, 9, 19), validation_mode="production"
    )


# ---------------------------------------------------------------- F5 CLI 集成（真实子进程）


@pytest.fixture
def _out_dir(tmp_path):
    out = tmp_path / "out"
    out.mkdir(parents=True)
    return out


def test_freeze_cli_production_refuses_qfq_and_dirty(_out_dir):
    """生产模式 + qfq（本机 default.yaml）→ 非零退出，清单不得落盘。"""
    out = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/alpha_v2_validation_freeze.py"),
         "--epoch-id", "alpha_v2_epoch_997", "--out", str(_out_dir)],
        capture_output=True, text=True, timeout=300,
    )
    assert out.returncode == 4
    assert not list(_out_dir.rglob("validation_freeze_manifest.json"))


def test_freeze_cli_rehearsal_writes_manifest_crossed_by_gate_override(_out_dir):
    """排演模式：允许本机 qfq + 脏树；清单上的 validation_mode 必须是 rehearsal。"""
    out = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/alpha_v2_validation_freeze.py"),
         "--epoch-id", "alpha_v2_epoch_997", "--out", str(_out_dir), "--rehearsal"],
        capture_output=True, text=True, timeout=300,
    )
    assert out.returncode == 0, out.stderr
    written = list(_out_dir.rglob("validation_freeze_manifest.json"))
    assert len(written) == 1
    manifest = json.loads(written[0].read_text(encoding="utf-8"))
    assert manifest["validation_mode"] == "rehearsal"
    assert manifest["execution_price_mode"] == "qfq"  # 本机 tracked default.yaml
    # 排演模式的 feature schema 允许为空，但清单里如实记录来源
    assert manifest["feature_schema_source"] == "empty_rehearsal_only"


# ---------------------------------------------------------------- F6 KPI 分层


def _two_days_with_kpis(tmp_path, *, day2_data_health: str):
    """day1 是完美天（clean 该 +），day2 由参数控制健康标记。"""
    manifest = write_freeze_manifest(tmp_path)
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date="2026-09-01")
    ident = shadow_row_identity(epoch)
    days = [date(2026, 9, 1), date(2026, 9, 2)]
    for index, day in enumerate(days):
        health = "ok" if index == 0 else day2_data_health
        with capture_at(day):
            rows = build_shadow_rows(
                signal_date=day, signal_time="15:35", epoch=epoch,
                candidates=[
                    {"symbol": "600000", "alpha_rank": 0.9, "in_quality_pool": True,
                     "in_light_pool": True, "in_deep_pool": True, "v2_top5": True,
                     "data_health": health},
                ],
                identity=ident,
            )
            write_shadow_snapshot(root=tmp_path, epoch=epoch, signal_date=day, rows=rows)
        # 写手工 outcome：让两天都真正"成熟"（5D 成熟数据用来测门）
        outcome = {
            "signal_date": day.isoformat(), "symbol": "600000", "executable": True,
            "matured_5d": True, "net_return_5d": 0.05, "excess_return_5d": 0.02,
        }
        path = outcome_path(tmp_path, epoch.epoch_id, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(outcome) + "\n", encoding="utf-8")
    return epoch, days


def test_data_health_not_available_excluded_from_clean_gates(tmp_path):
    epoch, days = _two_days_with_kpis(tmp_path, day2_data_health="not_available")
    report = build_validation_kpi(root=tmp_path, epoch=epoch)
    assert report["maturity"]["mature_dates_5d"] == 2           # 全量两天都成熟
    assert report["clean_maturity"]["mature_dates_5d"] == 1     # 但 clean 只算健康天
    governance = report["governance"]
    assert governance["captured_days"] == 2
    assert governance["clean_oos_days"] == 1
    assert governance["excluded_data_health_days"] == 1
    assert governance["coverage_rate"] == pytest.approx(0.5, abs=1e-9)
    # 日期级原因如实落出
    day2 = [r for r in governance["by_date"] if r["signal_date"] == "2026-09-02"][0]
    assert day2["eligible"] is False
    assert "data_health_not_available" in day2["reasons"]


def test_backfilled_day_excluded_from_clean_gates(tmp_path):
    manifest = write_freeze_manifest(tmp_path)
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date="2026-09-01")
    ident = shadow_row_identity(epoch)
    rows = build_shadow_rows(
        signal_date=date(2026, 9, 1), signal_time="15:35", epoch=epoch,
        candidates=[{"symbol": "600000", "alpha_rank": 0.9, "data_health": "ok"}],
        identity=ident,
    )
    # 显式 backfill 记录 9/1（运营承认是补的）
    with capture_at(date(2026, 9, 19)):
        write_shadow_snapshot(
            root=tmp_path, epoch=epoch, signal_date=date(2026, 9, 1), rows=rows,
            allow_backfill=True, backfill_reason="调度窗口超时后补写",
        )
    outcome = {
        "signal_date": "2026-09-01", "symbol": "600000", "executable": True,
        "matured_5d": True, "net_return_5d": 0.05, "excess_return_5d": 0.02,
    }
    op = outcome_path(tmp_path, epoch.epoch_id, date(2026, 9, 1))
    op.parent.mkdir(parents=True, exist_ok=True)
    op.write_text(json.dumps(outcome) + "\n", encoding="utf-8")
    report = build_validation_kpi(root=tmp_path, epoch=epoch)
    governance = report["governance"]
    assert governance["captured_days"] == 1
    assert governance["clean_oos_days"] == 0
    assert governance["backfilled_days"] == 1
    assert report["clean_maturity"]["mature_dates_5d"] == 0


def test_rehearsal_mode_manifest_never_clean(tmp_path):
    manifest = write_freeze_manifest(tmp_path, validation_mode="rehearsal")
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date="2026-09-18")
    ident = shadow_row_identity(epoch)
    rows = build_shadow_rows(
        signal_date=date(2026, 9, 18), signal_time="15:35", epoch=epoch,
        candidates=[{"symbol": "600000", "alpha_rank": 0.9, "data_health": "ok"}],
        identity=ident,
    )
    with capture_at(date(2026, 9, 18)):
        write_shadow_snapshot(
            root=tmp_path, epoch=epoch, signal_date=date(2026, 9, 18), rows=rows
        )
    report = build_validation_kpi(root=tmp_path, epoch=epoch)
    # 排演清单：执行/模式门恒 not clean → 治理块如实记 0
    assert report["governance"]["clean_oos_days"] == 0
    assert report["execution"]["clean_only"].get("rows", 0) == 0


# ---------------------------------------------------------------- F7 证据与口径


def test_strict_json_no_nan_literals(tmp_path):
    from stock_analyzer.alpha_v2.validation.validation_kpis import write_kpi_report

    manifest = write_freeze_manifest(tmp_path)
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date="2026-09-18")
    ident = shadow_row_identity(epoch)
    rows = build_shadow_rows(
        signal_date=date(2026, 9, 18), signal_time="15:35", epoch=epoch,
        candidates=[{"symbol": "600000", "alpha_rank": 0.9, "data_health": "ok"}],
        identity=ident,
    )
    with capture_at(date(2026, 9, 18)):
        write_shadow_snapshot(
            root=tmp_path, epoch=epoch, signal_date=date(2026, 9, 18), rows=rows
        )
    payload = build_validation_kpi(root=tmp_path, epoch=epoch)
    payload["_deliberate_nan"] = float("nan")
    json_path, _md = write_kpi_report(root=tmp_path, epoch=epoch, payload=payload)
    text = json_path.read_text(encoding="utf-8")

    def _forbid(x: str) -> float:
        raise AssertionError(f"strict JSON 不应出现字面量: {x}")

    parsed = json.loads(text, parse_constant=_forbid)
    assert parsed["_deliberate_nan"] == "not_available"
    assert json.dumps(parsed)  # 产物可被任意严格解析器再读写


def test_deep_rank_is_position_and_pct_kept_apart():
    import pandas as pd

    from stock_analyzer.alpha_v2.validation.shadow_capture import deep50_position_records

    frame = pd.DataFrame(
        {
            "symbol": [f"{i:06d}" for i in range(20)],
            "alpha_rank_score": [0.9 - i * 0.01 for i in range(20)],
        }
    )
    records = deep50_position_records(frame)
    assert [r["deep_rank"] for r in records] == list(range(1, 21))
    assert records[0]["deep_rank_pct"] == pytest.approx(0.9, abs=1e-9)
    assert records[-1]["deep_rank_pct"] == pytest.approx(0.71, abs=1e-9)


def test_quality_pool_source_roundtrip_keeps_production(tmp_path):
    """capture 写入的来源字段必须与成熟侧的读取语义一致（不许回退为代理）。"""
    manifest = write_freeze_manifest(tmp_path)
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date="2026-01-05")
    panel = flat_panel(
        ["600000"],
        closes={"600000": [10.0 + 0.01 * i for i in range(45)]},
    )
    rows = build_shadow_rows(
        signal_date=DAYS[5], signal_time="15:35", epoch=epoch,
        candidates=[{"symbol": "600000",
                     "quality_pool_source": "production_selection_engine"}],
        identity=shadow_row_identity(epoch),
    )
    with capture_at(DAYS[5]):
        write_shadow_snapshot(root=tmp_path, epoch=epoch, signal_date=DAYS[5], rows=rows)
    mature_epoch_outcomes(
        root=tmp_path, epoch=epoch, panel=panel, evaluation_date=DAYS[30],
        matcher=matcher(), slippage_ratio=0.0015, price_mode="raw",
        price_mode_certified=True,
    )
    from stock_analyzer.alpha_v2.validation.outcome_maturation import outcome_path as _op

    outcome = [
        json.loads(line)
        for line in _op(tmp_path, epoch.epoch_id, DAYS[5]).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert outcome, "outcome 应至少一行"
    assert outcome[0]["quality_pool_source"] == "production_selection_engine"


def test_git_worktree_dirt_empty_output_means_clean_not_dirty(tmp_path):
    """回流缺陷(G1 在本轮演练中自发现): git status --porcelain = '' 是"干净"，
    旧实现会把它当成"未知/脏"（"空串 or unknown"），必须回归。"""
    import subprocess as _sub

    from stock_analyzer.alpha_v2.validation.runtime_identity import git_worktree_dirt

    repo = tmp_path / "repo"
    repo.mkdir()
    _sub.run(["git", "init", "-q"], cwd=repo)
    (repo / "x.txt").write_text("a", encoding="utf-8")
    _sub.run(["git", "add", "-A"], cwd=repo)
    _sub.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
              "-q", "-m", "x"], cwd=repo)
    # 干净 → 空列表（而不是 ["unknown"] 之类伪脏）
    assert git_worktree_dirt(repo) == []
    # 白名单文件：不计脏
    (repo / ".build_commit").write_text("deadbeef", encoding="utf-8")
    assert git_worktree_dirt(repo) == []
    # 已跟踪文件被改：计脏
    (repo / "x.txt").write_text("b", encoding="utf-8")
    assert git_worktree_dirt(repo)
    # 不可知态：git 拿不到 → None（生产失败关闭）
    assert git_worktree_dirt(tmp_path / "no_such_dir_at_all") is None


def test_assert_worktree_clean_handles_unknown_state():
    with pytest.raises(FreezeGateError) as exc:
        assert_worktree_clean(None, validation_mode="production")
    assert exc.value.exit_code == 5
    assert_worktree_clean(None, validation_mode="rehearsal")  # 排演允许未知态落盘
