"""M3 Shadow 快照测试（T 日冻结 + 防篡改 + missing day 纪律）。"""

from __future__ import annotations

from datetime import date

import pytest
from _alpha_v2_m3_fixtures import (
    capture_at,
    open_epoch_for_manifest,
    shadow_row_identity,
    write_freeze_manifest,
)

from stock_analyzer.alpha_v2.validation.epoch import (
    EpochRegistryError,
    close_epoch,
)
from stock_analyzer.alpha_v2.validation.shadow_capture import (
    NOT_AVAILABLE,
    ShadowCaptureError,
    ShadowTamperError,
    build_shadow_rows,
    list_missing_days,
    list_shadow_dates,
    read_shadow_rows,
    record_missing_prediction_day,
    signal_date_of,
    write_shadow_snapshot,
)

# FROM 修复轮起：所有写盘路径都先看磁盘冻结清单是否与 epoch 锚定；
# 夹具必须走"写清单 → 开 epoch"的完整链，伪造清单缺位会 fail-closed。


@pytest.fixture
def epoch(tmp_path):
    manifest = write_freeze_manifest(tmp_path)
    return open_epoch_for_manifest(tmp_path, manifest, opened_on_date="2026-09-18")


def _candidate(symbol: str, alpha: float, **overrides):
    base = {
        "symbol": symbol,
        "in_deep_pool": True,
        "deep_rank": 1,
        "alpha_rank": alpha,
        "direction_score_5d": 0.6,
        "p_up_5d": 0.61,
        "p_up_calibration": "isotonic_oos",
        "expected_net_return_5d": 0.02,
        "expected_excess_return_5d": 0.015,
        "risk_score": -0.03,
        "expected_mae_5d": -0.03,
        "fillable": True,
        "v2_top1": False,
        "v2_top3": False,
        "v2_top5": False,
        "legacy_score": 72.0,
        "legacy_final": False,
        "legacy_reject_reasons": [],
        "signal_close_raw": 10.5,
    }
    base.update(overrides)
    return base


def test_build_rows_freeze_identity_and_not_available(epoch, tmp_path):
    ident = shadow_row_identity(epoch)
    rows = build_shadow_rows(
        signal_date=date(2026, 9, 18),
        signal_time="15:35",
        epoch=epoch,
        candidates=[_candidate("600000", 0.9), {"symbol": "600001"}],  # 一全一缺
        identity=ident,
        recorded_at="2026-09-18T15:35:00+08:00",
    )
    assert len(rows) == 2
    assert rows[0]["validation_epoch_id"] == "alpha_v2_epoch_001"
    assert rows[0]["p_up_5d"] == 0.61
    assert rows[1]["alpha_rank"] == NOT_AVAILABLE  # 缺字段不编造
    assert rows[1]["p_up_5d"] == NOT_AVAILABLE


def test_write_is_idempotent_and_preserves_first_freeze_time(epoch, tmp_path):
    day = date(2026, 9, 18)
    ident = shadow_row_identity(epoch)
    rows = build_shadow_rows(
        signal_date=day,
        signal_time="15:35",
        epoch=epoch,
        candidates=[_candidate("600000", 0.9)],
        identity=ident,
        recorded_at="2026-09-18T15:35:00+08:00",
    )
    with capture_at(day):
        write_shadow_snapshot(root=tmp_path, epoch=epoch, signal_date=day, rows=rows)
        # 同一天重跑同样的内容（幂等）：不报错、不改 recorded_at
        write_shadow_snapshot(root=tmp_path, epoch=epoch, signal_date=day, rows=rows)
    stored = read_shadow_rows(tmp_path, epoch.epoch_id, day)
    assert len(stored) == 1
    assert stored[0]["recorded_at"] == "2026-09-18T15:35:00+08:00"


def test_rewrite_with_different_prediction_is_refused(epoch, tmp_path):
    """事后换个 alpha_rank 再写同一天同一只——这就是 M3 §7 明文禁止的行为。"""
    day = date(2026, 9, 18)
    ident = shadow_row_identity(epoch)
    original = build_shadow_rows(
        signal_date=day,
        signal_time="15:35",
        epoch=epoch,
        candidates=[_candidate("600000", 0.9)],
        identity=ident,
    )
    with capture_at(day):
        write_shadow_snapshot(root=tmp_path, epoch=epoch, signal_date=day, rows=original)
    rewritten = build_shadow_rows(
        signal_date=day,
        signal_time="15:35",
        epoch=epoch,
        candidates=[_candidate("600000", 0.95)],  # 只改预测
        identity=ident,
    )
    with capture_at(day), pytest.raises(ShadowTamperError, match="拒绝改写"):
        write_shadow_snapshot(root=tmp_path, epoch=epoch, signal_date=day, rows=rewritten)
    # 原封不动验证：文件里仍是第一次的值
    stored = read_shadow_rows(tmp_path, epoch.epoch_id, day)
    assert stored[0]["alpha_rank"] == 0.9


def test_closed_epoch_refuses_writes(tmp_path):
    manifest = write_freeze_manifest(tmp_path)
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date="2026-09-18")
    close_epoch(root=tmp_path, epoch_id=epoch.epoch_id, reason="代码升级，开新 epoch")
    rows = build_shadow_rows(
        signal_date=date(2026, 9, 18),
        signal_time="15:35",
        epoch=epoch,
        candidates=[_candidate("600000", 0.9)],
        identity=shadow_row_identity(epoch),
    )
    # epoch 已关闭：再写必须抛错
    with capture_at(date(2026, 9, 18)), pytest.raises(EpochRegistryError):
        write_shadow_snapshot(
            root=tmp_path, epoch=epoch, signal_date=date(2026, 9, 18), rows=rows
        )
    # 台账也不能写
    with pytest.raises(EpochRegistryError):
        record_missing_prediction_day(
            root=tmp_path, epoch=epoch, signal_date=date(2026, 9, 18), reason="after close"
        )


def test_wrong_epoch_rows_rejected(epoch, tmp_path):
    day = date(2026, 9, 18)
    ident = shadow_row_identity(epoch)
    rows = build_shadow_rows(
        signal_date=day,
        signal_time="15:35",
        epoch=epoch,
        candidates=[_candidate("600000", 0.9)],
        identity=ident,
    )
    rows[0]["validation_epoch_id"] = "alpha_v2_epoch_999"  # 不是当前 epoch 的行
    with capture_at(day), pytest.raises(ShadowTamperError):
        write_shadow_snapshot(root=tmp_path, epoch=epoch, signal_date=day, rows=rows)


def test_missing_day_recording_is_idempotent_and_audited(epoch, tmp_path):
    day = date(2026, 9, 21)
    record_missing_prediction_day(
        root=tmp_path, epoch=epoch, signal_date=day, reason="upstream_data_missing"
    )
    record_missing_prediction_day(
        root=tmp_path, epoch=epoch, signal_date=day, reason="upstream_data_missing"
    )
    missing = list_missing_days(tmp_path, epoch.epoch_id)
    assert len(missing) == 1
    assert missing[0]["reason"] == "upstream_data_missing"
    # 缺原因不行
    with pytest.raises(ShadowCaptureError):
        record_missing_prediction_day(
            root=tmp_path, epoch=epoch, signal_date=date(2026, 9, 22), reason=" "
        )
    # 没有影子日
    assert list_shadow_dates(tmp_path, epoch.epoch_id) == []


def test_identity_missing_key_fails_closed(epoch, tmp_path):
    ident = shadow_row_identity(epoch)
    broken = {key: value for key, value in ident.items() if key != "model_artifact_hash"}
    with pytest.raises(ShadowCaptureError):
        build_shadow_rows(
            signal_date=date(2026, 9, 18),
            signal_time="15:35",
            epoch=epoch,
            candidates=[_candidate("600000", 0.9)],
            identity=broken,
        )


def test_signal_date_parsing():
    assert signal_date_of("2026-09-18") == date(2026, 9, 18)
    with pytest.raises(ShadowCaptureError):
        signal_date_of("not-a-date")
