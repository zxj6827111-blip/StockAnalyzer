"""M4-L §12 第二道闸：KPI 治理层对"cohort 真实性"的写后复核。

`require_production_funnel=true` 的 epoch 里，一个 clean 日必须能由**当日
manifest 内嵌的生产 funnel 证据**证明：哈希复算一致、日期/来源/selector_mode
权威、行集合与名次逐一对账。任何一项对不上 → 该日不计 clean OOS。
"""

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

from stock_analyzer.alpha_v2.validation.production_funnel import (
    FUNNEL_SCHEMA,
    FUNNEL_SOURCE,
    extract_funnel_from_scan_report,
    funnel_snapshot_hash,
)
from stock_analyzer.alpha_v2.validation.shadow_capture import (
    build_shadow_rows,
    write_shadow_day_manifest,
    write_shadow_snapshot,
)
from stock_analyzer.alpha_v2.validation.validation_kpis import build_validation_kpi

DAY = date(2026, 9, 18)
DEEP = ["600004", "600002", "600001"]


def _funnel_payload() -> dict[str, object]:
    report = {
        "funnel": {
            "policy": "snapshot_funnel",
            "deep_stage_ran": True,
            "selection_contract": {"selection_contract_id": "night_alpha_v2_v1"},
        },
        "prefilter": {
            "universe_quality_selection": {
                "selector_mode": "quality",
                "selected": [{"symbol": s, "score": 9.0} for s in DEEP],
            },
            "shortlisted": [{"symbol": s, "baseline_score": 8.0} for s in DEEP],
            "deep_stage": {"selected": [{"symbol": s, "funnel_score": 7.0} for s in DEEP]},
            "pinned_symbols": ["999999"],
        },
    }
    payload = extract_funnel_from_scan_report(
        source_report=report,
        trace_id="t",
        scan_status="night_scan_completed",
        created_at=f"{DAY.isoformat()}T22:00:00+08:00",
    )
    payload["signal_date"] = DAY.isoformat()
    payload["trade_date"] = DAY.isoformat()
    payload["night_scan_report_id"] = "nr-20260918-01"
    payload["funnel_snapshot_hash"] = funnel_snapshot_hash(payload)
    return payload


def _build_epoch(tmp_path, *, require_funnel: bool, funnel: dict[str, object] | None):
    manifest = write_freeze_manifest(
        tmp_path,
        validation_mode="test",
        deterministic_clock=True,
        validation_start_date=DAY.isoformat(),
    )
    if require_funnel:
        manifest["require_production_funnel"] = True
        from stock_analyzer.alpha_v2.validation.freeze import (
            freeze_manifest_hash,
            write_validation_freeze,
        )

        manifest["freeze_manifest_hash"] = freeze_manifest_hash(manifest)
        write_validation_freeze(manifest, root=tmp_path)
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date=DAY.isoformat())
    rows = []
    for rank, symbol in enumerate(DEEP, start=1):
        rows.append(
            {
                "symbol": symbol,
                "in_quality_pool": True,
                "in_light_pool": True,
                "in_deep_pool": True,
                "quality_rank": rank,
                "light_rank": rank,
                "deep_rank": rank,
                "alpha_rank": 1.0 / rank,
                "quality_pool_source": FUNNEL_SOURCE,
                # 结构化的同日 data_health（gate 通过所需）
                "data_health": {"status": "ok", "as_of": DAY.isoformat()},
                "backfilled": False,
                "clean_oos_eligible": True,
                "v2_top1": rank == 1,
                "v2_top3": rank <= 3,
                "v2_top5": True,
            }
        )
    built = build_shadow_rows(
        signal_date=DAY,
        signal_time="15:35",
        epoch=epoch,
        candidates=rows,
        identity=shadow_row_identity(epoch),
        recorded_at=f"{DAY.isoformat()}T22:05:00+08:00",
    )
    with capture_at(DAY):
        write_shadow_snapshot(root=tmp_path, epoch=epoch, signal_date=DAY, rows=built)
    payload: dict[str, object] = {
        "schema": "alpha_v2_shadow_day_manifest.v1",
        "captured_at": f"{DAY.isoformat()}T22:05:00+08:00",
    }
    if funnel is not None:
        payload["funnel"] = funnel
        payload["cohort_source"] = "production_funnel"
    else:
        payload["cohort_source"] = "research_proxy"
    write_shadow_day_manifest(
        root=tmp_path, epoch=epoch, signal_date=DAY, payload=payload
    )
    return epoch


def _governance(tmp_path, epoch) -> dict[str, object]:
    payload = build_validation_kpi(root=tmp_path, epoch=epoch)
    return payload["governance"]


def test_wellformed_funnel_evidence_makes_the_day_clean(tmp_path):
    epoch = _build_epoch(tmp_path, require_funnel=True, funnel=_funnel_payload())
    governance = _governance(tmp_path, epoch)
    assert governance["captured_days"] == 1
    assert governance["clean_oos_days"] == 1, governance["by_date"]


def test_missing_funnel_evidence_excludes_the_day(tmp_path):
    epoch = _build_epoch(tmp_path, require_funnel=True, funnel=None)
    governance = _governance(tmp_path, epoch)
    day = governance["by_date"][0]
    assert day["eligible"] is False
    assert "funnel_evidence_missing" in day["reasons"]


def test_tampered_funnel_members_are_caught(tmp_path):
    """Attack C 的 KPI 侧备份闸：写时蒙混过关，写后复核照样抓到。"""
    funnel = _funnel_payload()
    funnel["deep_members"] = funnel["deep_members"][:2]  # 改成员但不重算 hash
    epoch = _build_epoch(tmp_path, require_funnel=True, funnel=funnel)
    day = _governance(tmp_path, epoch)["by_date"][0]
    assert day["eligible"] is False
    assert "funnel_hash_mismatch" in day["reasons"]


def test_cohort_mismatch_excludes_the_day(tmp_path):
    funnel = _funnel_payload()
    funnel["deep_members"] = funnel["deep_members"][:2]
    funnel["deep_count"] = 2
    funnel["funnel_snapshot_hash"] = funnel_snapshot_hash(funnel)
    epoch = _build_epoch(tmp_path, require_funnel=True, funnel=funnel)
    day = _governance(tmp_path, epoch)["by_date"][0]
    assert day["eligible"] is False
    assert "funnel_cohort_mismatch" in day["reasons"]


def test_rank_mismatch_excludes_the_day(tmp_path):
    funnel = _funnel_payload()
    funnel["deep_members"] = [
        {**member, "rank": 9} for member in funnel["deep_members"]
    ]
    funnel["funnel_snapshot_hash"] = funnel_snapshot_hash(funnel)
    epoch = _build_epoch(tmp_path, require_funnel=True, funnel=funnel)
    day = _governance(tmp_path, epoch)["by_date"][0]
    assert day["eligible"] is False
    assert "funnel_rank_mismatch" in day["reasons"]


def test_non_authoritative_selector_mode_excludes_the_day(tmp_path):
    funnel = _funnel_payload()
    funnel["selector_mode"] = "snapshot_fallback"
    funnel["funnel_snapshot_hash"] = funnel_snapshot_hash(funnel)
    epoch = _build_epoch(tmp_path, require_funnel=True, funnel=funnel)
    day = _governance(tmp_path, epoch)["by_date"][0]
    assert day["eligible"] is False
    assert "funnel_selector_mode_not_authoritative" in day["reasons"]


def test_legacy_manifest_without_funnel_requirement_keeps_m3_semantics(tmp_path):
    """旧清单（无 require_production_funnel 键）：KPI 不做 funnel 复核，口径不变。"""
    epoch = _build_epoch(tmp_path, require_funnel=False, funnel=None)
    governance = _governance(tmp_path, epoch)
    assert governance["clean_oos_days"] == 1, governance["by_date"]


def test_manifest_funnel_block_is_the_complete_artifact(tmp_path):
    """内嵌证据必须是 funnel 工件原文（含 hash 字段本身与 degraded 块）。"""
    epoch = _build_epoch(tmp_path, require_funnel=True, funnel=_funnel_payload())
    manifest_path = (
        tmp_path / "validation" / epoch.epoch_id / "manifests"
        / f"shadow_day_{DAY.strftime('%Y%m%d')}.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    funnel = manifest["funnel"]
    assert funnel["schema"] == FUNNEL_SCHEMA
    assert "degraded" in funnel and "created_at" in funnel
    assert funnel_snapshot_hash(funnel) == funnel["funnel_snapshot_hash"]
    assert funnel["pinned_override_members"] == [{"symbol": "999999"}]
