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


def _pit_snapshot():
    """用真实的 S03 判定生成快照：留档里的原因必须是它算出来的，不是手搓的。"""
    from datetime import timedelta

    from stock_analyzer.data.asof_universe import SymbolPitStats, resolve_asof_universe

    as_of = date(2026, 10, 9)
    stats = {
        "600000.SH": SymbolPitStats(symbol="600000.SH", bars_in_window=120,
                                    bars_in_lookback=5, first_bar_date=as_of - timedelta(days=200),
                                    last_bar_date=as_of),
        "600001.SH": SymbolPitStats(symbol="600001.SH", bars_in_window=120,
                                    bars_in_lookback=0, first_bar_date=as_of - timedelta(days=200),
                                    last_bar_date=as_of - timedelta(days=30)),
        "600002.SH": SymbolPitStats(symbol="600002.SH", bars_in_window=5,
                                    bars_in_lookback=5, first_bar_date=as_of - timedelta(days=10),
                                    last_bar_date=as_of),
    }
    return resolve_asof_universe(
        as_of=as_of,
        index_symbols=["600000.SH", "600001.SH", "600002.SH", "600003.SH"],
        stats=stats, min_history_days=60, expected_active_lookback_days=5,
    )


def test_universe_layers_use_the_snapshot_real_per_symbol_reasons() -> None:
    from stock_analyzer.research.funnel_trace import KIND_HARD_GATE
    from stock_analyzer.research.night_scan_funnel_trace import (
        build_universe_stage_traces,
    )

    stages = build_universe_stage_traces(
        universe=_pit_snapshot(), data_as_of="2026-10-09T15:00:00", contract=CONTRACT,
    )
    assert [item.stage for item in stages] == ["universe", "hard_eligibility"]
    universe, eligibility = stages
    assert universe.kind == KIND_HARD_GATE and eligibility.kind == KIND_HARD_GATE
    # 入口层不淘汰股票，只记下"这次考虑过的全集"与覆盖率口径。
    assert (universe.inputs, universe.advanced) == (4, 4)
    assert universe.rejected == {}
    assert "universe_snapshot_id=" in universe.notes
    assert "delisting_coverage_verified=False" in universe.notes
    # 硬性资格层：晋级的是 as_of 时点真可能存在成交的那批，原因逐只来自快照本身。
    assert (eligibility.inputs, eligibility.advanced) == (4, 1)
    assert eligibility.advanced_symbols == ("600000.SH",)
    # 原因名一律沿用快照里的常量，不由留档层重新命名：
    # 这条断言之所以能抓错，正说明它是从 resolve_asof_universe 真算出来的。
    from stock_analyzer.data.asof_universe import (
        EXCLUDE_FUTURE_LISTED,
        EXCLUDE_INSUFFICIENT_HISTORY,
    )

    assert set(eligibility.rejected) == {
        "known_suspended", EXCLUDE_INSUFFICIENT_HISTORY, EXCLUDE_FUTURE_LISTED,
    }
    assert eligibility.rejected_symbols["known_suspended"] == ("600001.SH",)
    assert "不等于证明停牌" in eligibility.notes


def test_eligibility_layer_records_which_columns_its_gates_read() -> None:
    """§2 要这一层记"用了哪些特征"：清单由生产者按当天跑过的规则给，写入器只转达。

    同时钉住"旧 payload 不猜"：没有那几个键时 features_used 必须留空、notes 里
    也不许冒出版本号 —— 否则就是在替没记录的历史编一份记录。
    """
    from stock_analyzer.research.night_scan_funnel_trace import (
        build_universe_stage_traces,
    )

    def payload(**extra: object) -> dict:
        base = {
            "universe_snapshot_id": "replay:db:2026-10-09",
            "as_of": "2026-10-09",
            "eligible_symbols": ["600000.SH", "600001.SH", "600002.SH"],
            "expected_active_symbols": ["600000.SH"],
            "excluded_reasons": {
                "600001.SH": "min_avg_turnover_20",
                "600002.SH": "insufficient_history_at_asof",
            },
            "known_suspended_symbols": [],
            "survivorship_coverage": "incomplete_or_unknown",
            "delisting_coverage_verified": False,
            "non_evaluable_gates": [],
        }
        base.update(extra)
        return base

    stages = build_universe_stage_traces(
        universe=payload(
            gate_input_columns=["suspended", "avg_turnover_20", "float_market_cap"],
            feature_contract_version="trend_asof_v1",
            float_cap_interpretation_version="unproven_float_cap_placeholder_v1",
        ),
        data_as_of="2026-10-09T15:00:00",
        contract=CONTRACT,
    )
    eligibility = stages[1]
    assert eligibility.features_used == (
        "avg_turnover_20", "float_market_cap", "suspended",
    )
    assert "feature_compute_version=trend_asof_v1" in eligibility.notes
    assert "float_cap_interpretation=unproven_float_cap_placeholder_v1" in eligibility.notes

    bare = build_universe_stage_traces(
        universe=payload(), data_as_of="2026-10-09T15:00:00", contract=CONTRACT,
    )[1]
    assert bare.features_used == ()
    assert "feature_compute_version=" not in bare.notes


def test_universe_layers_refuse_a_day_whose_gate_input_is_a_constant() -> None:
    """硬门输入列被填成常数的那天，前两层不落档。

    那天的 `min_float_market_cap` 阈值等于该列众数，门对任何行都不淘汰；
    落档就等于声称"硬性资格检查判过了"（2026-10-08 float_market_cap=1.2e10 事故）。
    """
    from stock_analyzer.research.night_scan_funnel_trace import build_universe_stage_traces

    universe = {
        "universe_snapshot_id": "snap-1", "as_of": "2026-04-01",
        "eligible_symbols": ["600000", "000001"], "expected_active_symbols": ["600000"],
        "excluded_reasons": {"000001": "min_avg_turnover_20"},
        "known_suspended_symbols": [], "survivorship_coverage": "incomplete_or_unknown",
        "delisting_coverage_verified": False,
        "non_evaluable_gates": ["min_float_market_cap"],
    }
    assert build_universe_stage_traces(
        universe=universe, data_as_of="2026-04-01",
        contract=DEFAULT_TREND_CONTRACT,
    ) == ()
    # 同一份快照去掉这个标记就必须落档：证明拒的是标记本身，不是别的一致性检查。
    universe.pop("non_evaluable_gates")
    assert len(build_universe_stage_traces(
        universe=universe, data_as_of="2026-04-01", contract=DEFAULT_TREND_CONTRACT,
    )) == 2


def test_universe_layers_refuse_counts_only_inputs_and_merge_in_order() -> None:
    from stock_analyzer.research.night_scan_funnel_trace import (
        build_universe_stage_traces,
    )

    # 只有计数的旧 payload：宁可不落这两层，也不拿计数冒充成员。
    counts_only = {"eligible_count": 300, "expected_active_count": 280}
    assert build_universe_stage_traces(
        universe=counts_only, data_as_of="x", contract=CONTRACT) == ()

    trace = build_night_scan_funnel_trace(
        report=_report(["A", "B", "C"], ["A", "B"], ["A"]),
        trade_date="2026-10-09", contract=CONTRACT, universe=_pit_snapshot(),
    )
    assert trace is not None
    assert [item.stage for item in trace.stages] == [
        "universe", "hard_eligibility", "quality_300", "light_100", "deep_50",
    ]

    # 没有 universe 输入时仍然是原来的三层，行为不变。
    plain = build_night_scan_funnel_trace(
        report=_report(["A", "B", "C"], ["A", "B"], ["A"]),
        trade_date="2026-10-09", contract=CONTRACT,
    )
    assert plain is not None
    assert [item.stage for item in plain.stages] == ["quality_300", "light_100", "deep_50"]


def test_live_night_scan_membership_feeds_the_first_two_layers() -> None:
    """线上夜扫自己导出的硬门成员要能撑起 universe + hard_eligibility 两层。

    这一条把 §2 前两层从"只有研究侧重放有留档"变成"生产链路每天自己留"：
    数据来自报告里的 hard_gate_membership（符号级），不是逐原因计数。
    """
    from stock_analyzer.research.night_scan_funnel_trace import (
        build_universe_stage_traces,
        live_universe_facts,
    )

    membership = {
        "considered": ["600000.SH", "600001.SH", "600002.SH", "600003.SH"],
        "advanced": ["600000.SH", "600001.SH"],
        "rejected_symbols": {
            "low_avg_turnover_20": ["600002.SH"],
            "insufficient_history": ["600003.SH"],
        },
        "gate_input_columns": ["avg_turnover_20", "history_days"],
        "feature_contract_version": "trend_asof_v1",
        "float_cap_interpretation_version": "unproven_float_cap_placeholder_v1",
        "non_evaluable_gates": [],
    }
    report = {"prefilter": {"hard_gate_membership": membership}}
    facts = live_universe_facts(report)
    assert facts["expected_active_symbols"] == ["600000.SH", "600001.SH"]
    assert facts["excluded_reasons"] == {
        "600002.SH": "low_avg_turnover_20", "600003.SH": "insufficient_history",
    }
    # 覆盖率没证明过就必须写未证明，不能因为规模像全市场就当全集。
    assert facts["delisting_coverage_verified"] is False
    assert facts["survivorship_coverage"] == "incomplete_or_unknown"

    stages = build_universe_stage_traces(
        universe=facts, data_as_of="2026-10-09T15:00:00", contract=CONTRACT,
    )
    assert [item.stage for item in stages] == ["universe", "hard_eligibility"]
    eligibility = stages[1]
    assert (eligibility.inputs, eligibility.advanced) == (4, 2)
    assert set(eligibility.rejected) == {"low_avg_turnover_20", "insufficient_history"}
    assert eligibility.features_used == ("avg_turnover_20", "history_days")
    assert "feature_compute_version=trend_asof_v1" in eligibility.notes


def test_live_night_scan_counts_only_report_emits_nothing() -> None:
    """旧形状（只有计数）不落这两层：拿计数冒充成员等于留档说谎。"""
    from stock_analyzer.research.night_scan_funnel_trace import (
        build_universe_stage_traces,
        live_universe_facts,
    )

    report = {"prefilter": {"rejected_count_by_reason": {"low_avg_turnover_20": 415}}}
    assert live_universe_facts(report) == {}
    assert build_universe_stage_traces(
        universe=live_universe_facts(report), data_as_of="2026-10-09T15:00:00",
        contract=CONTRACT,
    ) == ()


def test_hard_filter_exports_per_symbol_first_reason_membership() -> None:
    """_hard_filter 现在必须逐只导出成员与第一条命中原因（计数答不出是谁）。"""
    import pandas as pd

    from stock_analyzer.runtime.universe_candidate_selector import (
        UniverseCandidateSelector,
    )

    selector = object.__new__(UniverseCandidateSelector)
    selector._min_history_days = 120
    selector._min_avg_turnover_20 = 1e7
    selector._min_float_market_cap = 2e9
    selector._max_staleness_days = 5
    selector._require_financial_data = False
    selector._min_roe = 0.0
    selector._max_debt_ratio = 100.0

    metrics = pd.DataFrame({
        "symbol": ["600000", "600001", "600002", "600003"],
        "suspended": [False, False, False, False],
        "is_st": [False, True, False, False],
        "is_delisting_risk": [False, False, False, False],
        "history_days": [500, 500, 30, 500],
        "avg_turnover_20": [5e8, 5e8, 5e8, 1e5],
        "float_market_cap": [5e10, 5e10, 5e10, 5e10],
        "latest_close": [10.0, 10.0, 10.0, 10.0],
        "latest_data_date": pd.to_datetime(["2026-10-09"] * 4),
        "financial_data_complete": [True, True, True, True],
        "roe": [0.1, 0.1, 0.1, 0.1],
        "debt_ratio": [0.4, 0.4, 0.4, 0.4],
    })
    eligible, counts, membership = UniverseCandidateSelector._hard_filter(
        selector, metrics, scope_set=set(), reference_date=None,
    )
    assert sorted(eligible["symbol"]) == ["600000"]
    assert membership["considered"] == ["600000", "600001", "600002", "600003"]
    assert membership["advanced"] == ["600000"]
    assert membership["rejected_symbols"] == {
        "is_st": ["600001"], "insufficient_history": ["600002"],
        "low_avg_turnover_20": ["600003"],
    }
    # 一只票只进一个桶，恒等式才能闭合。
    assert (len(membership["advanced"]) + sum(
        len(v) for v in membership["rejected_symbols"].values()
    )) == len(membership["considered"])
    assert counts == {k: len(v) for k, v in membership["rejected_symbols"].items()}
    assert membership["feature_contract_version"] == "trend_asof_v1"
    assert "float_market_cap" in membership["gate_input_columns"]
