"""夜扫半段漏斗留档（改进计划 §2 / NOTE-002 D14 的收口）。

要钉住的是三件容易糊过去的事：

1. 计数恒等式在**成员不嵌套**（板块配额/pinned 注入）时仍然成立，且落差被如实
   算成两层并集，不是假称"上一层就是这些股票"；
2. 夜扫没记逐只拒绝原因（D11），留档就必须以 ``night_truncation_reason_not_recorded``
   的形式把这个缺口写出来，视图不能因此判它"原因可用"；
3. 报告里没有成员时返回 ``None``——不编造一条看起来完整的留档。
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

from stock_analyzer.contracts.trend_strategy import DEFAULT_TREND_CONTRACT
from stock_analyzer.research.funnel_trace import (
    KIND_PREDICTIVE,
    read_trace,
)
from stock_analyzer.research.night_scan_funnel_trace import (
    NIGHT_UNATTRIBUTED_DROP,
    build_night_scan_funnel_trace,
)
from stock_analyzer.research.selection_funnel_view import build_selection_funnel_view
from stock_analyzer.runtime.services.trend_tail_shadow_service import (
    TrendTailShadowService,
)

CONTRACT = DEFAULT_TREND_CONTRACT
DAY = date(2026, 10, 9)


def _rows(symbols: list[str]) -> list[dict]:
    return [{"symbol": symbol, "score": 1.0} for symbol in symbols]


def _report(quality: list[str], light: list[str], deep: list[str]) -> dict:
    return {
        "timestamp": "2026-10-09T20:15:00+08:00",
        "prefilter": {
            "universe_quality_selection": {"selected": _rows(quality)},
            "shortlisted": _rows(light),
            "deep_stage": {"selected": _rows(deep)},
        },
    }


def _trace(report: dict):
    return build_night_scan_funnel_trace(
        report=report, trade_date="2026-10-09", contract=CONTRACT,
        features_used=("avg_turnover_20",),
    )


def test_nested_truncation_records_inputs_advanced_and_placeholder_reason() -> None:
    trace = _trace(_report(["A", "B", "C", "D"], ["A", "B", "C"], ["A", "B"]))
    assert trace is not None
    assert [item.stage for item in trace.stages] == ["quality_300", "light_100", "deep_50"]
    # 三层都是预测性截断：§2 的消融只动这类，硬门不在这里也不许被误标。
    assert all(item.kind == KIND_PREDICTIVE for item in trace.stages)
    quality, light, deep = trace.stages
    # 第一层没有上一层成员可比，inputs 就等于本层成员：没有淘汰可记，不硬推上游规模。
    assert (quality.inputs, quality.advanced) == (4, 4)
    assert quality.rejected == {}
    assert (light.inputs, light.advanced) == (4, 3)
    assert light.rejected[NIGHT_UNATTRIBUTED_DROP] == 1
    assert light.rejected_symbols[NIGHT_UNATTRIBUTED_DROP] == ("D",)
    assert (deep.inputs, deep.advanced) == (3, 2)
    assert deep.rejected_symbols[NIGHT_UNATTRIBUTED_DROP] == ("C",)
    assert all(item.features_used == ("avg_turnover_20",) for item in trace.stages)
    assert trace.trade_date == DAY


def test_non_nested_membership_names_the_symbol_instead_of_hiding_it() -> None:
    trace = _trace(_report(["A", "B"], ["A", "Z"], ["A"]))
    assert trace is not None
    light = trace.stage("light_100")
    assert light is not None
    # inputs 取两层并集 {A,B,Z}，所以 inputs == advanced + dropped 仍然成立。
    assert (light.inputs, light.advanced) == (3, 2)
    assert light.rejected_symbols[NIGHT_UNATTRIBUTED_DROP] == ("B",)
    assert "membership_not_nested" in light.notes
    assert "Z" in light.notes
    deep = trace.stage("deep_50")
    assert deep is not None
    assert deep.rejected_symbols[NIGHT_UNATTRIBUTED_DROP] == ("Z",)


def test_absent_or_empty_prefilter_produces_no_trace() -> None:
    assert build_night_scan_funnel_trace(
        report={"prefilter": {}}, trade_date="2026-10-09", contract=CONTRACT) is None
    assert build_night_scan_funnel_trace(
        report={}, trade_date="2026-10-09", contract=CONTRACT) is None
    empty = {"prefilter": {"universe_quality_selection": {"selected": []},
                           "shortlisted": [], "deep_stage": None}}
    assert build_night_scan_funnel_trace(
        report=empty, trade_date="2026-10-09", contract=CONTRACT) is None


def test_service_writes_the_night_half_as_its_own_file(tmp_path: Path) -> None:
    service = TrendTailShadowService(object(), report_dir=tmp_path)
    result = service.record_night_scan(
        _report(["A", "B", "C"], ["A", "B"], ["A"]), trade_date="2026-10-09"
    )
    assert result["emitted"] is True
    assert result["layers"] == ["quality_300", "light_100", "deep_50"]
    path = Path(result["path"])
    # 与尾盘半段同目录、不同文件：同一个交易日的两份留档不得互相覆盖。
    assert path.name == "funnel_trace_2026-10-09_night.json"
    payload = read_trace(path)
    assert payload["stages"][0]["stage"] == "quality_300"
    assert payload["time_interpretation"]["evidence_eligible"] is True

    # 报告里没成员时不写文件，但把原因回显出来，别让人以为留档成功了。
    quiet = service.record_night_scan({"prefilter": {}}, trade_date="2026-10-10")
    assert quiet == {"emitted": False, "reason": "night_scan_report_has_no_funnel_members"}


def test_view_prefers_night_traces_and_still_flags_missing_reasons(tmp_path: Path) -> None:
    service = TrendTailShadowService(object(), report_dir=tmp_path)
    trace_path = Path(service.record_night_scan(
        _report(["A", "B", "C", "D"], ["A", "B", "C"], ["A", "B"]),
        trade_date="2026-10-09",
    )["path"])
    view = build_selection_funnel_view(
        night_funnel=None, tail_traces=[read_trace(trace_path)]
    )
    layers = {row["layer"]: row for row in view["layers"]}
    assert layers["light_100"]["source"] == "night_scan_funnel_trace"
    assert layers["light_100"]["inputs"] == 4
    assert layers["light_100"]["advanced"] == 3
    # 占位原因不等于有原因：淘汰了股票的层仍然进"答不了原因分布"的清单。
    assert layers["light_100"]["reasons_available"] is False
    assert "truncation_reason_not_recorded" in layers["light_100"]["gaps"]
    assert view["coverage"]["layers_from_trace"] == [
        "quality_300", "light_100", "deep_50"
    ]
    # quality_300 这层没有淘汰任何股票（第一层没有可比上游），所以不算"缺原因"。
    assert view["coverage"]["layers_without_reasons"] == ["light_100", "deep_50"]
    # 只有夜扫半段有留档：universe / hard_eligibility 至今没有生产者，
    # 这一条不许被夜扫留档掩盖；尾盘半段则仍然整段缺记录。
    assert view["coverage"]["layers_unrecorded"] == [
        "universe", "hard_eligibility", "night_watch_pool", "tail_confirmation",
        "final_recommendation", "execution_exit",
    ]


def test_cli_finds_iso_named_traces_by_trade_date(tmp_path: Path) -> None:
    """回归：文件名是 ISO 带横线日期，按紧凑日期匹配会把留档全筛没。"""
    spec = importlib.util.spec_from_file_location(
        "audit_selection_funnel_cli2",
        Path(__file__).resolve().parents[1] / "scripts" / "audit_selection_funnel.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    service = TrendTailShadowService(object(), report_dir=tmp_path)
    service.record_night_scan(_report(["A", "B"], ["A"], ["A"]), trade_date="2026-10-09")
    out = tmp_path / "view.json"
    assert module.main(["--tail-dir", str(tmp_path), "--trade-date", "2026-10-09",
                        "--out", str(out), "--quiet"]) == 3
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["coverage"]["layers_from_trace"] == ["quality_300", "light_100", "deep_50"]
    assert payload["trade_dates"] == ["2026-10-09"]
    # 只按紧凑日期筛（历史上会退 5），现在两种写法都能命中。
    assert module.main(["--tail-dir", str(tmp_path), "--trade-date", "20261009",
                        "--quiet", "--out", str(out)]) == 3
