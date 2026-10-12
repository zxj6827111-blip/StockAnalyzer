"""夜扫真实筛选器 → 报告 → 留档的证据链与损坏载荷拒绝路径。"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest

from stock_analyzer.contracts.trend_strategy import DEFAULT_TREND_CONTRACT
from stock_analyzer.research.funnel_trace import FunnelTraceError, verify_trace
from stock_analyzer.research.night_scan_funnel_trace import (
    NIGHT_UNATTRIBUTED_DROP,
    build_night_scan_funnel_trace,
    live_universe_facts,
)
from stock_analyzer.research.selection_funnel_view import build_selection_funnel_view
from stock_analyzer.runtime.service import StockAnalyzerService
from stock_analyzer.runtime.services.trend_tail_shadow_service import TrendTailShadowService
from tests.test_service_week5 import _load_test_config
from tests.test_universe_candidate_selector import (
    _build_batch_frame,
    _default_symbols_spec,
    _FrameWarehouse,
    _make_selector,
)


def _service(predictor=None):
    service = StockAnalyzerService.__new__(StockAnalyzerService)
    service._config = _load_test_config()
    service._pipeline = SimpleNamespace(_predictor=predictor)
    service._model_registry = None
    return service


def _frame(symbols):
    return pd.DataFrame([
        {"symbol": s, "latest_close": 10.0, "suspended": False,
         "is_st": False, "is_delisting_risk": False}
        for s in symbols
    ])


def _report():
    return {
        "timestamp": "2026-10-11T21:45:00+08:00",
        "prefilter": {
            "universe_quality_selection": {
                "selected": [{"symbol": "A"}],
                "selection_evidence": {
                    "version": 1, "input_symbols": ["A", "B", "C"],
                    "rejected_by_reason": {"quality_rank_board_quota_exploration": ["B", "C"]},
                },
            },
            "shortlisted": [{"symbol": "A"}],
            "deep_stage": {"selected": [{"symbol": "A"}]},
        },
    }


def _trace(report, **kwargs):
    return build_night_scan_funnel_trace(
        report=report, trade_date="2026-10-11", contract=DEFAULT_TREND_CONTRACT, **kwargs,
    )


def test_quality_producer_exposes_eligible_pool_before_capacity():
    spec = _default_symbols_spec(count_per_board=20)
    spec["600001"]["is_st"] = True
    selector = _make_selector(_FrameWarehouse(_build_batch_frame(spec)))
    result = selector.select(
        symbols=sorted(spec), target_size=60, trade_date="2026-07-31",
        ruleset_id="a_share_default_v1", board_scope=["SSE", "SZSE", "BSE"],
    )
    quality = result["report"]
    evidence = quality["selection_evidence"]
    inputs = set(evidence["input_symbols"])
    dropped = set(evidence["rejected_by_reason"]["quality_rank_board_quota_exploration"])
    assert "600001" not in inputs  # 硬门拒绝不能冒充质量排序截断。
    assert len(inputs) == quality["hard_eligible_count"] > 60
    assert inputs == set(result["selected"]) | dropped
    assert not dropped & set(result["selected"])
    report = _report()
    report["prefilter"]["universe_quality_selection"] = quality
    report["prefilter"]["shortlisted"] = quality["selected"][:3]
    report["prefilter"]["deep_stage"]["selected"] = quality["selected"][:1]
    trace = _trace(report, universe=live_universe_facts(report))
    assert trace.stage("quality_300").inputs == len(inputs)
    assert trace.stage("quality_300").advanced == 60
    assert verify_trace(trace.as_dict()) == ()


def test_light_producer_keeps_first_real_gate_reason_and_capacity_drop():
    frame = _frame(["600000", "600001", "600002", "600003", "600004", "000001"])
    frame.loc[2, "is_st"] = True
    frame.loc[3, ["suspended", "is_st"]] = True
    frame.loc[4, "is_delisting_risk"] = True
    report = _service()._light_stage_from_snapshot(
        frame=frame, target=1, allowed_exchanges={"SSE"},
    )
    assert [r["symbol"] for r in report["shortlisted"]] == ["600000"]
    assert report["skipped_count"] == 4
    assert report["selection_evidence"]["rejected_by_reason"] == {
        "is_st": ["600002"], "suspended": ["600003"],
        "is_delisting_risk": ["600004"], "out_of_board_scope": ["000001"],
        "light_rank_capacity": ["600001"],
    }


class _Predictor:
    def predict_rows(self, frame):
        values = self.predict_rows_with_raw(frame)
        return {key: values[key] for key in ("lgbm", "xgb", "meta")}

    def predict_rows_with_raw(self, frame):
        return {"lgbm": [0.8, 0.2], "xgb": [0.8, 0.2], "meta": [0.75, 0.25],
                "raw_blend": [0.9, 0.3]}


@pytest.mark.parametrize("predictor", [None, _Predictor()])
def test_deep_distinguishes_missing_snapshot_from_rank_and_model_fallback(predictor):
    light = {"shortlisted": [{"symbol": s, "baseline_score": 50.0}
                              for s in ["600000", "600001", "600002"]]}
    deep = _service(predictor)._deep_stage_from_snapshot(
        frame=_frame(["600000", "600001"]), target=1, light_report=light,
    )
    evidence = deep["selection_evidence"]
    assert [r["symbol"] for r in deep["selected"]] == ["600000"]
    assert evidence["rejected_by_reason"] == {
        "feature_snapshot_missing": ["600002"], "deep_rank_capacity": ["600001"],
    }
    assert len(evidence["ranked_candidates"]) == 2
    if predictor is None:
        assert deep["model_prediction_degraded"] is True
        assert evidence["raw_predictions"] == evidence["calibrated_probabilities"] == {}
    else:
        assert evidence["raw_predictions"] == {"600000": 0.9, "600001": 0.3}
        assert evidence["calibrated_probabilities"] == {"600000": 0.75, "600001": 0.25}
    report = _report()
    report["prefilter"]["shortlisted"] = light["shortlisted"]
    report["prefilter"]["deep_stage"] = deep
    trace = _trace(report)
    stage = trace.stage("deep_50")
    assert (stage.inputs, stage.advanced) == (3, 1)
    assert stage.raw_predictions == evidence["raw_predictions"]
    assert stage.calibrated_probabilities == evidence["calibrated_probabilities"]
    assert verify_trace(trace.as_dict()) == ()


def test_wrapped_production_report_reads_original_selection_evidence():
    report = _report()
    report["source_report"] = deepcopy(report)
    del report["prefilter"]["universe_quality_selection"]["selection_evidence"]
    stage = _trace(report).stage("quality_300")
    assert (stage.inputs, stage.advanced) == (3, 1)
    assert stage.rejected == {"quality_rank_board_quota_exploration": 2}
    report["source_report"]["prefilter"]["universe_quality_selection"]["selected"] = []
    with pytest.raises(FunnelTraceError, match="membership mismatch"):
        _trace(report)


def test_partial_attribution_does_not_claim_all_reasons_available():
    report = _report()
    evidence = report["prefilter"]["universe_quality_selection"]["selection_evidence"]
    evidence["rejected_by_reason"]["quality_rank_board_quota_exploration"] = ["B"]
    trace = _trace(report)
    assert trace.stage("quality_300").rejected_symbols[NIGHT_UNATTRIBUTED_DROP] == ("C",)
    view = build_selection_funnel_view(night_funnel=None, tail_traces=[trace.as_dict()])
    quality = next(r for r in view["layers"] if r["layer"] == "quality_300")
    assert quality["reasons_available"] is False
    assert "truncation_reason_not_recorded" in quality["gaps"]


def test_all_rejected_session_is_recorded_as_zero_advances():
    report = _report()
    quality = report["prefilter"]["universe_quality_selection"]
    quality["selected"] = []
    quality["selection_evidence"]["rejected_by_reason"] = {"rank_capacity": ["A", "B", "C"]}
    report["prefilter"]["shortlisted"] = []
    report["prefilter"]["deep_stage"]["selected"] = []
    trace = _trace(report)
    assert (trace.stage("quality_300").inputs, trace.stage("quality_300").advanced) == (3, 0)
    assert verify_trace(trace.as_dict()) == ()


@pytest.mark.parametrize("damage", ["advanced_rejected", "double_reason", "duplicate_input",
                                   "unknown_version", "unknown_symbol", "unidentified_input",
                                   "nonfinite_prediction"])
def test_corrupt_selection_evidence_fails_closed_without_stopping_scan(tmp_path, damage):
    report = _report()
    evidence = report["prefilter"]["universe_quality_selection"]["selection_evidence"]
    if damage == "advanced_rejected":
        evidence["rejected_by_reason"]["bad"] = ["A"]
    elif damage == "double_reason":
        evidence["rejected_by_reason"]["bad"] = ["B"]
    elif damage == "duplicate_input":
        evidence["input_symbols"].append("B")
    elif damage == "unknown_version":
        evidence["version"] = 2
    elif damage == "unknown_symbol":
        evidence["rejected_by_reason"]["bad"] = ["Z"]
    elif damage == "unidentified_input":
        evidence["unidentified_input_rows"] = 1
    else:
        evidence["raw_predictions"] = {"A": float("nan")}
    result = TrendTailShadowService(object(), report_dir=tmp_path).record_night_scan(
        report, trade_date="2026-10-11",
    )
    assert result["emitted"] is False
    assert result["reason"] == "night_trace_failed:FunnelTraceError"
    assert not list(tmp_path.glob("*.json"))
