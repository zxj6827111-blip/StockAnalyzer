"""M3 Validation KPI 汇总测试（Top1/3/5、Rank IC、样本门、缺失日）。"""

from __future__ import annotations

import json
from datetime import date

import pytest
from _alpha_v2_m3_fixtures import (
    capture_at,
    open_epoch_for_manifest,
    shadow_row_identity,
    write_freeze_manifest,
)

from stock_analyzer.alpha_v2.validation.outcome_maturation import outcome_path
from stock_analyzer.alpha_v2.validation.shadow_capture import (
    build_shadow_rows,
    record_missing_prediction_day,
    write_shadow_snapshot,
)
from stock_analyzer.alpha_v2.validation.validation_kpis import (
    build_validation_kpi,
    load_epoch_frames,
)


@pytest.fixture
def epoch_data(tmp_path):
    """有 3 个已成熟决策日（全部可成交、全部 clean-eligible）的最小可信尖峰。"""
    manifest = write_freeze_manifest(tmp_path)
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date="2026-09-01")
    identity = shadow_row_identity(epoch)
    days = [date(2026, 9, d) for d in (1, 2, 3)]
    for day in days:
        # data_health 用生产形状（结构化 + 同日 as_of），KPI 治理层走的正是这条路径
        health = {
            "schema": "alpha_v2_capture_data_health.v1",
            "status": "ok",
            "source_status": "healthy",
            "as_of": day.isoformat(),
            "generated_at": f"{day.isoformat()}T20:30:00+08:00",
            "coverage": 0.99,
            "source": "test-fixture",
            "aligned_to_signal_date": True,
            "detail": "同日工件可用",
        }
        candidates = [
            {
                "symbol": "600000",
                "alpha_rank": 0.95,
                "in_deep_pool": True,
                "in_light_pool": True,
                "in_quality_pool": True,
                "v2_top1": True,
                "v2_top3": True,
                "v2_top5": True,
                "fillable": True,
                "signal_close_raw": 10.0,
                "data_health": dict(health),
            },
            {
                "symbol": "600001",
                "alpha_rank": 0.70,
                "in_deep_pool": True,
                "in_light_pool": True,
                "in_quality_pool": True,
                "v2_top3": True,
                "v2_top5": True,
                "fillable": True,
                "signal_close_raw": 20.0,
                "data_health": dict(health),
            },
            {
                "symbol": "600002",
                "alpha_rank": 0.40,
                "in_deep_pool": True,
                "in_light_pool": False,
                "in_quality_pool": False,
                "fillable": True,
                "data_health": dict(health),
            },
        ]
        with capture_at(day):
            rows = build_shadow_rows(
                signal_date=day,
                signal_time="15:35",
                epoch=epoch,
                candidates=candidates,
                identity=identity,
            )
            write_shadow_snapshot(root=tmp_path, epoch=epoch, signal_date=day, rows=rows)
        # 手工写 outcome（真实生产由成熟任务产生；这里验证 KPI 读取与统计口径）
        # 夹具必须与生产同形：真实 outcome 行恒带 price_mode / price_mode_certified
        # （成熟任务在 _base_row 里逐行写）；缺了它们 KPI 的第二道闸会全拒。
        outcomes = [
            {
                "signal_date": day.isoformat(),
                "symbol": "600000",
                "executable": True,
                "entry_price_raw": 10.1,
                "entry_price_net": 10.2,
                "entry_delay_sessions": 1,
                "price_mode": "raw",
                "price_mode_certified": True,
                "matured_3d": True,
                "matured_5d": True,
                "net_return_3d": 0.020,
                "net_return_5d": 0.050,
                "excess_return_3d": 0.015,
                "excess_return_5d": 0.040,
                "excess_return_3d__eligible_ew": 0.012,
                "excess_return_5d__eligible_ew": 0.035,
                "mae_5d": -0.010,
                "tp8_before_sl5_10d": 1.0,
            },
            {
                "signal_date": day.isoformat(),
                "symbol": "600001",
                "executable": True,
                "entry_price_raw": 20.1,
                "entry_price_net": 20.3,
                "entry_delay_sessions": 1,
                "price_mode": "raw",
                "price_mode_certified": True,
                "matured_3d": True,
                "matured_5d": True,
                "net_return_3d": 0.010,
                "net_return_5d": 0.020,
                "excess_return_3d": 0.006,
                "excess_return_5d": 0.010,
                "excess_return_3d__eligible_ew": 0.005,
                "excess_return_5d__eligible_ew": 0.008,
                "mae_5d": -0.015,
                "tp8_before_sl5_10d": 1.0,
            },
            {
                "signal_date": day.isoformat(),
                "symbol": "600002",
                "executable": True,
                "price_mode": "raw",
                "price_mode_certified": True,
                "matured_3d": True,
                "matured_5d": True,
                "net_return_3d": -0.010,
                "net_return_5d": -0.030,
                "excess_return_3d": -0.012,
                "excess_return_5d": -0.025,
                "mae_5d": -0.040,
                "tp8_before_sl5_10d": 0.0,
            },
        ]
        path = outcome_path(tmp_path, epoch.epoch_id, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in outcomes)
        path.write_text(text + "\n", encoding="utf-8")
    return epoch, days


def test_kpi_report_full_blocks(epoch_data, tmp_path):
    epoch, days = epoch_data
    report = build_validation_kpi(root=tmp_path, epoch=epoch, report_date=date(2026, 9, 18))
    assert report["status"] == "ok"
    # M3 护栏：alpha_verified / production_promotion 恒为 False/LOCKED
    assert report["alpha_verified"] is False
    assert report["production_promotion"] == "LOCKED"

    # Hit rate：TopK 是"逐日 cohort"——Top1 共 3 行（600000×3 天）全正；
    # Top3 共 6 行（600000+600001 各 3 天）也全部为正。
    hit = report["hit_rate"]
    assert hit["top1"]["hit_rate_5d"] == 1.0
    assert hit["top3"]["sample_rows_5d"] == 6
    assert abs(hit["top3"]["hit_rate_5d"] - 1.0) < 1e-9

    # 平均 + 中位都要在（M3 §10.4）
    ret = report["returns"]
    assert ret["top1"]["net_5d"]["mean"] == pytest.approx(0.05, abs=1e-9)
    assert ret["top3"]["net_5d"]["median"] == pytest.approx(0.035, abs=1e-9)

    # 超额（quality 主基准 + eligible 副基准）
    excess = report["excess_vs_benchmarks"]
    assert excess["top1"]["quality_pool_ew"]["mean"] == pytest.approx(0.04, abs=1e-9)
    assert excess["top1"]["eligible_ew"]["mean"] == pytest.approx(0.035, abs=1e-9)

    # Rank IC：三天样本太少，但至少 status 字段会被给出
    assert "5d" in report["rank_ic"]
    assert report["rank_ic"]["statistical_unit"] == "decision_date"

    # Winner recall：质量池内 600000/600001 为高，600002 不在质量池
    recall = report["winner_recall"]
    assert recall.get("status") in {"ok", "no_data", "no_mature_dates"} or "recall_light" in recall

    # 下行：top1 的 mae 应来自 600000
    downside = report["downside"]
    assert downside["top1"]["mean_mae_5d"] == pytest.approx(-0.010, abs=1e-9)

    # 执行：fill rate 100%（clean 与全快照口径均应给出）
    execution = report["execution"]
    assert execution["all_captured"]["fill_rate"] == 1.0
    assert execution["clean_only"]["fill_rate"] == 1.0

    # 治理分层：3 个采集日全部 clean-eligible（夹具对齐了 data_health=ok）
    governance = report["governance"]
    assert governance["captured_days"] == 3
    assert governance["clean_oos_days"] == 3
    assert governance["backfilled_days"] == 0
    assert governance["coverage_rate"] == pytest.approx(1.0, abs=1e-9)
    assert report["clean_maturity"]["mature_dates_5d"] == 3

    # 样本门：3 个成熟日，20/60 仍 False——这证实"不会因为指标看起来好就越门"
    gates = report["sample_gate_status"]
    assert gates["failure_alert"]["reached"] is False
    assert gates["direction_review"]["primary_5d_reached"] is False


def test_missing_days_are_reported_and_excluded(epoch_data, tmp_path):
    epoch, _days = epoch_data
    record_missing_prediction_day(
        root=tmp_path, epoch=epoch, signal_date=date(2026, 9, 4), reason="code_failure"
    )
    report = build_validation_kpi(root=tmp_path, epoch=epoch)
    assert any(
        item["signal_date"] == "2026-09-04"
        for item in report["missing_prediction_days"]
    )
    # 样本门仍以有 shadow 的天计（不因 missing 而变）
    assert report["maturity"]["signal_dates_total"] == 3


def test_empty_epoch_reports_no_data(tmp_path):
    manifest = write_freeze_manifest(tmp_path)
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date="2026-09-18")
    report = build_validation_kpi(root=tmp_path, epoch=epoch)
    assert report["status"] == "no_shadow_data"
    assert report["alpha_verified"] is False


def test_frames_load_from_disk(epoch_data, tmp_path):
    epoch, _days = epoch_data
    shadow, outcomes = load_epoch_frames(tmp_path, epoch.epoch_id)
    assert len(shadow) == 9
    assert len(outcomes) == 9


def test_report_is_strict_json(epoch_data, tmp_path):
    """N1 修复：含 NaN 的统计字段不得直接写进 JSON——产物必须被严格解析器读开。"""
    from stock_analyzer.alpha_v2.validation.validation_kpis import write_kpi_report

    epoch, _days = epoch_data
    payload = build_validation_kpi(root=tmp_path, epoch=epoch)
    json_path, _md = write_kpi_report(root=tmp_path, epoch=epoch, payload=payload)
    text = json_path.read_text(encoding="utf-8")

    def _forbid(x: str) -> float:
        raise AssertionError(f"strict JSON 不应出现字面量: {x}")

    parsed = json.loads(text, parse_constant=_forbid)
    assert parsed["schema"] == "alpha_v2_validation_kpi.v1"
    assert "NaN" not in text
