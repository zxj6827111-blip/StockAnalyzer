"""影子验证门槛的**生产者**（发布清单 R12）。

``shadow_readiness(≥60 完整交易日, ≥100 笔成熟成交)`` 这两个输入此前没人算，
门槛于是永远只能靠印象回答。这里钉住的是它容易糊过去的四处：

1. 被阻断的那一轮不算完整观察日；
2. 夜扫半段的 ``_night`` 留档不算"又一个交易日"；
3. 读侧自检不过 / 时间不可证的留档**点名排除**，而不是折算成 0 或悄悄少算；
4. observed 与 replayed 不合并——混在一起的门槛读数没有意义。
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from stock_analyzer.contracts.trend_strategy import DEFAULT_TREND_CONTRACT
from stock_analyzer.research.funnel_trace import (
    FinalRecommendationRow,
    build_funnel_trace,
    write_trace,
)
from stock_analyzer.research.shadow_evidence import (
    CAPTURE_MODE_OBSERVED,
    CAPTURE_MODE_REPLAYED,
    summarize_shadow_evidence,
    trace_paths,
)

CONTRACT = DEFAULT_TREND_CONTRACT
DAY_ONE = date(2026, 10, 9)


def _row(symbol: str, *, realized: bool = True) -> FinalRecommendationRow:
    fill = ({"realized": True, "net_profit": 12.5, "net_return": 0.012}
            if realized else {"realized": False, "deferred": True})
    return FinalRecommendationRow(
        symbol=symbol, rank=1, probability=0.62,
        reference_notional=float(CONTRACT.reference_notional),
        strategy=CONTRACT.strategy, contract_version=CONTRACT.contract_version,
        contract_digest=CONTRACT.digest(), probability_field="p_net_profit_5d_tail",
        data_as_of=f"{DAY_ONE.isoformat()}T14:46:00", model_identity={},
        feature_snapshot={"avg_turnover_20": 1.0}, fill=fill,
    )


def _day(directory: Path, day: date, *, rows=(), blocking: str = "",
         suffix: str = "") -> Path:
    trace = build_funnel_trace(
        trade_date=day, stages=[], final_recommendations=list(rows),
        blocking_reason=blocking, contract=CONTRACT,
    )
    return write_trace(trace, directory, suffix=suffix, contract=CONTRACT)


def _summarize(directory: Path, **kwargs) -> dict:
    return summarize_shadow_evidence(
        trace_paths(directory), capture_mode=CAPTURE_MODE_OBSERVED, **kwargs
    )


def test_counts_only_complete_observation_days(tmp_path: Path) -> None:
    _day(tmp_path, DAY_ONE, rows=[_row("600000.SH"),
                                 _row("600001.SH", realized=False)])
    _day(tmp_path, DAY_ONE + timedelta(days=1))                     # 0 只的一天仍是完整观察日
    _day(tmp_path, DAY_ONE + timedelta(days=2), blocking="tail_serving_manifest_unverified")
    _day(tmp_path, DAY_ONE, suffix="night", rows=[_row("600000.SH")])   # 夜扫半段不算一天

    summary = _summarize(tmp_path)
    assert summary["observed_trade_days"] == 2
    assert summary["days_with_recommendation"] == 1
    assert summary["recommendation_coverage"] == pytest.approx(0.5)
    assert summary["matured_simulated_fills"] == 1
    assert summary["pending_fills"] == 1
    assert summary["blocked_days"] == [str(DAY_ONE + timedelta(days=2))]
    assert summary["trace_files_counted"] == 3
    assert summary["trace_files_seen"] == 3          # _night 那份根本没进文件集合
    assert summary["readiness"]["ready_for_release_review"] is False
    assert len(summary["readiness"]["blockers"]) == 2


def test_untrustworthy_and_time_ineligible_records_are_named_not_silently_skipped(
    tmp_path: Path,
) -> None:
    good = _day(tmp_path, DAY_ONE, rows=[_row("600000.SH")])
    tampered = _day(tmp_path, DAY_ONE + timedelta(days=1), rows=[_row("600002.SH")])
    payload = json.loads(tampered.read_text(encoding="utf-8"))
    payload["blocking_reason"] = "把被阻断的天改成正常天"           # 计数照样对，只能靠摘要抓
    tampered.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    legacy = _day(tmp_path, DAY_ONE + timedelta(days=2), rows=[_row("600003.SH")])
    stale = json.loads(legacy.read_text(encoding="utf-8"))
    stale["written_at"] = "2026-10-11T14:50:00"                    # 修复前的裸时刻
    stale.pop("written_at_timezone", None)
    legacy.write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")

    summary = _summarize(tmp_path)
    assert summary["observed_trade_days"] == 1
    assert summary["matured_simulated_fills"] == 1
    assert len(summary["excluded_untrusted"]) == 1
    assert "trace_digest_mismatch" in summary["excluded_untrusted"][0]
    assert summary["excluded_time_ineligible"] == [legacy.name]
    assert good.name not in summary["excluded_untrusted"]


def test_capture_mode_must_be_declared_and_never_merged() -> None:
    with pytest.raises(ValueError, match="capture_mode must be one of"):
        summarize_shadow_evidence([], capture_mode="mixed")

    # 同一个门槛数只能来自一种口径；报告里必须写着它是哪一种。
    summary = summarize_shadow_evidence([], capture_mode=CAPTURE_MODE_REPLAYED)
    assert summary["capture_mode"] == CAPTURE_MODE_REPLAYED
    assert summary["observed_trade_days"] == 0


def test_the_gate_is_reachable_and_reported_by_the_cli(tmp_path: Path) -> None:
    """门槛不是摆设：证据真攒够 60 天 / 100 笔，命令就退 0。"""
    spec = importlib.util.spec_from_file_location(
        "audit_shadow_evidence_cli",
        Path(__file__).resolve().parents[1] / "scripts" / "audit_shadow_evidence.py",
    )
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    assert cli.main(["--trace-dir", str(tmp_path), "--quiet"]) == 5     # 一条留档都没有
    for index in range(60):
        day = DAY_ONE + timedelta(days=index)
        _day(tmp_path, day, rows=[_row(f"6000{index:02d}.SH"),
                                  _row(f"0000{index:02d}.SZ")])
    out = tmp_path / "shadow_readiness.json"
    assert cli.main(["--trace-dir", str(tmp_path), "--out", str(out), "--quiet"]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["observed_trade_days"] == 60
    assert payload["matured_simulated_fills"] == 120
    assert payload["readiness"]["blockers"] == []

    # 证据不足时退 3 是**真实状态**，不是错误。
    sparse = tmp_path / "sparse"
    sparse.mkdir()
    _day(sparse, DAY_ONE, rows=[_row("600000.SH")])
    assert cli.main(["--trace-dir", str(sparse), "--quiet"]) == 3


def test_service_counts_readiness_from_its_own_trace_dir(tmp_path: Path) -> None:
    """门槛输入跟着留档目录走：影子进行到哪一步不该只躺在命令行里，也不该靠人估。"""
    from stock_analyzer.runtime.services.trend_tail_shadow_service import (
        TrendTailShadowService,
    )

    service = TrendTailShadowService(object(), report_dir=tmp_path)
    empty = service.shadow_readiness_summary()
    assert empty["note"] == "no_funnel_traces_written_yet"
    assert empty["observed_trade_days"] == 0
    assert empty["readiness"]["ready_for_release_review"] is False

    _day(tmp_path, DAY_ONE, rows=[_row("600000.SH")])
    _day(tmp_path, DAY_ONE, suffix="night", rows=[_row("600000.SH")])
    summary = service.shadow_readiness_summary()
    assert summary["capture_mode"] == CAPTURE_MODE_OBSERVED
    assert summary["observed_trade_days"] == 1        # _night 那份不算第二个观察日
    assert summary["matured_simulated_fills"] == 1

    # 留档被改得读不出来时，门槛按 0 计并写明原因——既不 crash 也不假装达标。
    broken = tmp_path / "funnel_trace_broken.json"
    broken.write_text("{", encoding="utf-8")
    unreadable = service.shadow_readiness_summary()
    assert unreadable["note"].startswith("shadow_evidence_unreadable:")
    assert unreadable["observed_trade_days"] == 0
    assert unreadable["readiness"]["ready_for_release_review"] is False
