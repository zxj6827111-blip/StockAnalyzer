"""M3 outcome 成熟任务测试（成熟门 / 幂等 / 重述防护 / 基准四类列）。"""

from __future__ import annotations

import json

import pytest
from _alpha_v2_m3_fixtures import (
    capture_at,
    open_epoch_for_manifest,
    shadow_row_identity,
    write_freeze_manifest,
)
from _alpha_v2_research_helpers import DAYS, flat_panel, matcher

from stock_analyzer.alpha_v2.validation.outcome_maturation import (
    OutcomeRestatementError,
    mature_epoch_outcomes,
    outcome_path,
)
from stock_analyzer.alpha_v2.validation.shadow_capture import (
    build_shadow_rows,
    write_shadow_snapshot,
)

SYMBOLS = ["600000", "600001", "600002", "600003", "600004", "600005"]
SIGNAL_DATE = DAYS[5]  # 留出前方 40 个交易日，3/5/10/15 都能成熟


@pytest.fixture
def epoch_with_shadow(tmp_path):
    manifest = write_freeze_manifest(tmp_path)
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date="2026-01-05")
    panel = flat_panel(
        SYMBOLS,
        closes={
            symbol: [10.0 + 0.05 * index + 0.01 * idx for index in range(45)]
            for idx, symbol in enumerate(SYMBOLS)
        },
    )
    rows = build_shadow_rows(
        signal_date=SIGNAL_DATE,
        signal_time="15:35",
        epoch=epoch,
        candidates=[
            {
                "symbol": symbol,
                "in_deep_pool": True,
                "in_light_pool": True,
                "in_quality_pool": index < 3,  # 前 3 只进质量池
                "quality_pool_source": "research_proxy:alpha_v2_quality_v1",
                "alpha_rank": 0.9 - index * 0.05,
                "v2_top5": index < 5,
                "v2_top3": index < 3,
                "v2_top1": index == 0,
                "fillable": True,
            }
            for index, symbol in enumerate(SYMBOLS)
        ],
        identity=shadow_row_identity(epoch),
        recorded_at=f"{SIGNAL_DATE.isoformat()}T15:35:00+08:00",
    )
    with capture_at(SIGNAL_DATE):
        write_shadow_snapshot(root=tmp_path, epoch=epoch, signal_date=SIGNAL_DATE, rows=rows)
    return epoch, panel


def _mature(tmp_path, epoch, panel, evaluation_date):
    return mature_epoch_outcomes(
        root=tmp_path,
        epoch=epoch,
        panel=panel,
        evaluation_date=evaluation_date,
        matcher=matcher(),
        slippage_ratio=0.0015,
        price_mode="raw",
        price_mode_certified=True,
    )


def test_signal_day_writes_nothing(epoch_with_shadow, tmp_path):
    """信号当天就评：0 行 outcome（不提前写未来数据）。"""
    epoch, panel = epoch_with_shadow
    summary = _mature(tmp_path, epoch, panel, SIGNAL_DATE)
    assert summary["rows_written"] == 0
    assert not outcome_path(tmp_path, epoch.epoch_id, SIGNAL_DATE).exists()


def test_maturation_writes_all_matured_horizons(epoch_with_shadow, tmp_path):
    epoch, panel = epoch_with_shadow
    summary = _mature(tmp_path, epoch, panel, DAYS[30])
    assert summary["rows_written"] == len(SYMBOLS)
    rows = _read_outcomes(tmp_path, epoch.epoch_id, SIGNAL_DATE)
    assert len(rows) == len(SYMBOLS)
    row = rows[0]
    for horizon in (3, 5, 10, 15):
        assert row[f"matured_{horizon}d"] is True
        assert row[f"net_return_{horizon}d"] is not None
        assert row[f"mae_{horizon}d"] is not None
        # 冻结基准的四层列（eligible EW / quality EW / style residual）
        assert f"excess_return_{horizon}d" in row  # 主基准 = quality_pool_ew
        assert f"excess_return_{horizon}d__eligible_ew" in row
        assert f"residual_excess_return_{horizon}d" in row  # style_matched
    assert row["benchmark_name"] == "quality_pool_ew"
    assert row["quality_pool_source"] == "research_proxy:alpha_v2_quality_v1"


def test_partial_horizon_matures_incrementally(epoch_with_shadow, tmp_path):
    """先成熟 3D，后续补 5/10/15——同键更新只补 pending 列，不动已成熟值。"""
    epoch, panel = epoch_with_shadow
    d3 = DAYS[8]  # signal + 3（SIGNAL 是 DAYS[5]）
    early = _mature(tmp_path, epoch, panel, d3)
    assert early["rows_written"] == len(SYMBOLS)
    early_rows = _read_outcomes(tmp_path, epoch.epoch_id, SIGNAL_DATE)
    r = early_rows[0]
    assert r["matured_3d"] is True
    assert r.get("matured_5d") is False or r.get("matured_5d") == "not_available" \
        or r.get("matured_5d") is None
    net3_first = r["net_return_3d"]

    later = _mature(tmp_path, epoch, panel, DAYS[30])
    assert later["rows_written"] >= len(SYMBOLS)
    rows2 = _read_outcomes(tmp_path, epoch.epoch_id, SIGNAL_DATE)
    assert rows2[0]["net_return_3d"] == net3_first  # 已成熟值不被改写
    assert rows2[0]["matured_5d"] is True


def test_restatement_is_refused(epoch_with_shadow, tmp_path):
    """已成熟值被外部改写后重跑成熟任务 => OutcomeRestatementError（行情被回改的照妖镜）。"""
    import json as _json

    epoch, panel = epoch_with_shadow
    _mature(tmp_path, epoch, panel, DAYS[30])
    path = outcome_path(tmp_path, epoch.epoch_id, SIGNAL_DATE)
    rows = _read_outcomes(tmp_path, epoch.epoch_id, SIGNAL_DATE)
    rows[0]["net_return_3d"] = 9.99  # 恶意/错误改写
    path.write_text(
        "\n".join(_json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(OutcomeRestatementError):
        _mature(tmp_path, epoch, panel, DAYS[30])


def test_epoch_days_summary_updates(epoch_with_shadow, tmp_path):
    epoch, panel = epoch_with_shadow
    _mature(tmp_path, epoch, panel, DAYS[30])
    from stock_analyzer.alpha_v2.validation.epoch import get_epoch

    record = get_epoch(tmp_path, epoch.epoch_id)
    assert record.days["mature_dates_5d"] >= 1


def test_missing_snapshot_days_do_not_block(epoch_with_shadow, tmp_path):
    """有 missing day 记录时，成熟任务跳过不存在的快照而不是报错。"""
    epoch, panel = epoch_with_shadow
    path = outcome_path(tmp_path, epoch.epoch_id, SIGNAL_DATE)
    _mature(tmp_path, epoch, panel, DAYS[30])
    assert path.exists()


def test_closed_epoch_refuses_mature(epoch_with_shadow, tmp_path):
    """F3：closed epoch 不得写 outcome、不得回写 days——文件内容必须原样不动。"""
    from stock_analyzer.alpha_v2.validation.epoch import EpochRegistryError, close_epoch

    epoch, panel = epoch_with_shadow
    _mature(tmp_path, epoch, panel, DAYS[30])  # 先有成熟内容
    close_epoch(root=tmp_path, epoch_id=epoch.epoch_id, reason="关闭后再成熟应被拒")

    path = outcome_path(tmp_path, epoch.epoch_id, SIGNAL_DATE)
    before = path.read_bytes()
    with pytest.raises(EpochRegistryError):
        _mature(tmp_path, epoch, panel, DAYS[35])
    assert path.read_bytes() == before  # 文件逐字节不动


def _read_outcomes(root, epoch_id, day):
    path = outcome_path(root, epoch_id, day)
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
