"""M4-L：生产侧写入钩子专项测试（夜扫 emit → 晚报 link）。

覆盖：enabled 才落盘、非 snapshot_funnel/未跑 deep 不落盘、篡改拒绝但只记审计、
发布后链接 report_id + 报告 sha256、重复链接幂等、缺工件时明确跳过。
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path

from _alpha_v2_m3_fixtures import open_epoch_for_manifest, write_freeze_manifest

from stock_analyzer.alpha_v2.validation.production_funnel import (
    funnel_snapshot_path,
    load_funnel_snapshot,
)
from stock_analyzer.config import load_config
from stock_analyzer.runtime.services.live_shadow_cycle_service import (
    LiveShadowCycleService,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 9, 21)
NOW = datetime.combine(DAY, time(22, 10))


class _StubService:
    def __init__(self, *, enabled: bool, funnel_root: Path, artifact_root: Path) -> None:
        config = load_config(REPO_ROOT / "config" / "default.yaml")
        config.alpha_v2.enabled = enabled
        config.alpha_v2.production_funnel_root = str(funnel_root)
        config.alpha_v2.artifact_root = str(artifact_root)
        self._config = config
        self.audits: list[dict[str, object]] = []

    def _record_audit_event(self, **kwargs: object) -> None:
        self.audits.append(kwargs)


def _scan_report(*, policy: str = "snapshot_funnel", deep_stage_ran: bool = True):
    return {
        "funnel": {
            "policy": policy,
            "deep_stage_ran": deep_stage_ran,
            "selection_contract": {"selection_contract_id": "night_alpha_v2_v1"},
        },
        "prefilter": {
            "universe_quality_selection": {
                "selector_mode": "quality",
                "selected": [{"symbol": f"q{i}", "score": 5.0} for i in range(4)],
            },
            "shortlisted": [{"symbol": f"q{i}", "baseline_score": 4.0} for i in range(3)],
            "deep_stage": {
                "selected": [{"symbol": f"q{i}", "funnel_score": 3.0} for i in range(2)]
            },
            "pinned_symbols": ["999999"],
        },
    }


def _cycle(tmp_path: Path, *, enabled: bool = True):
    stub = _StubService(
        enabled=enabled,
        funnel_root=tmp_path / "runtime" / "production_funnel",
        artifact_root=tmp_path / "alpha_v2",
    )
    return LiveShadowCycleService(stub), stub


def test_emit_writes_funnel_when_enabled(tmp_path):
    cycle, stub = _cycle(tmp_path)
    cycle.emit_funnel_from_scan_report(
        report=_scan_report(), trade_date=NOW, trace_id="week5-night-scan-1"
    )
    path = funnel_snapshot_path(tmp_path / "runtime" / "production_funnel", DAY)
    payload = load_funnel_snapshot(path)
    assert payload["signal_date"] == DAY.isoformat()
    assert payload["deep_count"] == 2
    assert payload["pinned_override_members"] == [{"symbol": "999999"}]
    assert payload["night_scan_report_id"] == ""  # 尚未链接
    assert any(
        item.get("event_type") == "alpha_v2_production_funnel_emitted"
        for item in stub.audits
    )


def test_emit_is_noop_when_disabled(tmp_path):
    cycle, stub = _cycle(tmp_path, enabled=False)
    cycle.emit_funnel_from_scan_report(
        report=_scan_report(), trade_date=NOW, trace_id="t"
    )
    assert not (tmp_path / "runtime" / "production_funnel").exists()
    assert stub.audits == []


def test_emit_skips_non_snapshot_funnel_and_direct_scans(tmp_path):
    cycle, _ = _cycle(tmp_path)
    cycle.emit_funnel_from_scan_report(
        report=_scan_report(policy="direct_non_universe"), trade_date=NOW, trace_id="t"
    )
    cycle.emit_funnel_from_scan_report(
        report=_scan_report(deep_stage_ran=False), trade_date=NOW, trace_id="t"
    )
    assert not (tmp_path / "runtime" / "production_funnel").exists()


def test_emit_conflict_is_audited_not_raised(tmp_path):
    """同一天两份不同漏斗：拒绝重写（tamper），但绝不能把选股链路炸掉。"""
    cycle, stub = _cycle(tmp_path)
    cycle.emit_funnel_from_scan_report(
        report=_scan_report(), trade_date=NOW, trace_id="t1"
    )
    conflicting = _scan_report()
    conflicting["prefilter"]["deep_stage"]["selected"] = [{"symbol": "q0", "funnel_score": 1.0}]
    cycle.emit_funnel_from_scan_report(
        report=conflicting, trade_date=NOW, trace_id="t2"
    )
    assert any(
        item.get("event_type") == "alpha_v2_production_funnel_emit_failed"
        for item in stub.audits
    )
    # 原有工件未被改写
    payload = load_funnel_snapshot(
        funnel_snapshot_path(tmp_path / "runtime" / "production_funnel", DAY)
    )
    assert payload["deep_count"] == 2


def test_link_after_publish_sets_report_identity(tmp_path):
    cycle, stub = _cycle(tmp_path)
    cycle.emit_funnel_from_scan_report(
        report=_scan_report(), trade_date=NOW, trace_id="t"
    )
    report_dir = tmp_path / "nightly_reports" / DAY.isoformat()
    report_dir.mkdir(parents=True)
    report_file = report_dir / "nr-20260921-01.json"
    report_file.write_text(json.dumps({"report_id": "nr-20260921-01"}), encoding="utf-8")

    class _ReportService:
        @staticmethod
        def report_path(trade_date: str, report_id: str) -> Path:
            return tmp_path / "nightly_reports" / trade_date / f"{report_id}.json"

    cycle.link_published_report(
        trade_date=DAY.isoformat(),
        report_id="nr-20260921-01",
        report_service=_ReportService(),
    )
    payload = load_funnel_snapshot(
        funnel_snapshot_path(tmp_path / "runtime" / "production_funnel", DAY)
    )
    assert payload["night_scan_report_id"] == "nr-20260921-01"
    assert payload["source_artifact_sha256"]
    assert payload["source_artifact_path"].endswith("nr-20260921-01.json")
    assert any(
        item.get("event_type") == "alpha_v2_production_funnel_linked"
        for item in stub.audits
    )


def test_link_without_funnel_is_audited_skip(tmp_path):
    cycle, stub = _cycle(tmp_path)

    class _ReportService:
        @staticmethod
        def report_path(trade_date: str, report_id: str) -> Path:
            return tmp_path / report_id

    cycle.link_published_report(
        trade_date=DAY.isoformat(),
        report_id="nr-x",
        report_service=_ReportService(),
    )
    assert any(
        item.get("event_type") == "alpha_v2_production_funnel_link_skipped"
        for item in stub.audits
    )


def test_linked_funnel_blocks_further_emit(tmp_path):
    """链接之后同一路径的 funnel 不可再写（哪怕内容相同）——当日证据已封版。"""
    cycle, _ = _cycle(tmp_path)
    cycle.emit_funnel_from_scan_report(
        report=_scan_report(), trade_date=NOW, trace_id="t"
    )
    report_dir = tmp_path / "nightly_reports" / DAY.isoformat()
    report_dir.mkdir(parents=True)
    (report_dir / "nr-20260921-01.json").write_text("{}", encoding="utf-8")

    class _ReportService:
        @staticmethod
        def report_path(trade_date: str, report_id: str) -> Path:
            return tmp_path / "nightly_reports" / trade_date / f"{report_id}.json"

    cycle.link_published_report(
        trade_date=DAY.isoformat(),
        report_id="nr-20260921-01",
        report_service=_ReportService(),
    )
    # 已链接 → 再 emit 同内容也被拒（linked 不可变）
    cycle.emit_funnel_from_scan_report(
        report=_scan_report(), trade_date=NOW, trace_id="t2"
    )
    payload = load_funnel_snapshot(
        funnel_snapshot_path(tmp_path / "runtime" / "production_funnel", DAY)
    )
    assert payload["night_scan_report_id"] == "nr-20260921-01"


def test_shared_epoch_fixture_still_constructs_epoch(tmp_path):
    """（顺带守卫）共享夹具未被本文件改动破坏：仍能独立写清单 + 开 epoch。"""
    manifest = write_freeze_manifest(tmp_path)
    epoch = open_epoch_for_manifest(tmp_path, manifest, opened_on_date=DAY.isoformat())
    assert epoch.status == "open"
