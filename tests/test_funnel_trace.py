"""漏斗分层留档验收（改进计划 §2 + §3.4「最终推荐单独留档」）。

核心不是"能写下多少字段"，而是留档不会说谎：计数自洽、层名合法、缺失可见、
硬门与预测性规则可区分（§2 要逐层移除预测性规则做对照，但硬门必须保留）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from stock_analyzer.contracts.trend_strategy import (
    DEFAULT_TREND_CONTRACT,
    NET_PROFIT_PROBABILITY_FIELD,
    ModelIdentity,
    rank_final_recommendations,
)
from stock_analyzer.research.funnel_trace import (
    KIND_HARD_GATE,
    KIND_PREDICTIVE,
    MISSING_FEATURE_SNAPSHOT,
    FunnelTraceError,
    StageTrace,
    archive_final_recommendations,
    build_funnel_trace,
    compare_traces,
    diagnose_funnel,
    read_trace,
    record_stage,
    write_trace,
)

DAY = date(2026, 10, 8)
CONTRACT = DEFAULT_TREND_CONTRACT


def _identity() -> ModelIdentity:
    return ModelIdentity(
        model_id="trend-tail-lgbm-1",
        artifact_content_hash="sha256:deadbeef",
        training_commit="eb691ed",
        runtime_commit="eb691ed",
        feature_compute_version=5,
        label_policy_id="label_policy_v4_abc",
        contract_digest=CONTRACT.digest(),
    )


def _stage(stage="hard_eligibility", kind=KIND_HARD_GATE, *, inputs=5, advanced=3,
           rejected=None, **overrides):
    symbols = [f"6000{i}.SH" for i in range(inputs)]
    kept = symbols[:advanced]
    dropped = symbols[advanced:]
    payload = {
        "stage": stage,
        "kind": kind,
        "input_symbols": symbols,
        "advanced_symbols": kept,
        "rejected": rejected if rejected is not None else {"low_liquidity": dropped},
        "data_as_of": "2026-10-08T15:30:00",
        "contract": CONTRACT,
        "model_identity": _identity(),
        "feature_compute_version": 5,
        "label_policy_id": "label_policy_v4_abc",
        "features_used": ["avg_turnover_20", "float_market_cap"],
    }
    payload.update(overrides)
    return record_stage(**payload)


# ---------------------------------------------------------------------------
# 计数自洽：留档不能谎报
# ---------------------------------------------------------------------------


def test_stage_counts_must_add_up() -> None:
    """直接构造（读回旧留档、外部拼装）时计数说谎就 raise。"""
    with pytest.raises(FunnelTraceError, match="do not add up"):
        _raw_stage(inputs=5, advanced=2, rejected={"low_liquidity": 1},
                   rejected_symbols={"low_liquidity": ("600004.SH",)})


def test_advanced_symbol_list_length_is_checked_against_count() -> None:
    with pytest.raises(FunnelTraceError, match="symbol list size"):
        _raw_stage(inputs=4, advanced=3, rejected={"low_liquidity": 1},
                   rejected_symbols={"low_liquidity": ("600003.SH",)},
                   advanced_symbols=("600000.SH",))


def test_per_reason_symbol_lists_must_match_reason_counts() -> None:
    with pytest.raises(FunnelTraceError, match="count does not match its symbol list"):
        _raw_stage(inputs=4, advanced=2, rejected={"stale_data": 2},
                   rejected_symbols={"stale_data": ("600003.SH",)},
                   advanced_symbols=("600000.SH", "600001.SH"))


def _raw_stage(**overrides) -> StageTrace:
    payload = {
        "stage": "light_100", "kind": KIND_PREDICTIVE, "inputs": 4, "advanced": 2,
        "rejected": {"low_liquidity": 2}, "rejected_symbols": {"low_liquidity": ("a", "b")},
        "advanced_symbols": ("c", "d"), "features_used": (), "raw_predictions": {},
        "calibrated_probabilities": {}, "model_identity": {}, "data_as_of": "2026-10-08T15:30:00",
        "feature_compute_version": 5, "contract_digest": CONTRACT.digest(),
        "label_policy_id": "x",
    }
    payload.update(overrides)
    return StageTrace(**payload)


def test_unknown_stage_and_missing_data_as_of_are_rejected() -> None:
    with pytest.raises(FunnelTraceError, match="unknown funnel stage"):
        _raw_stage(stage="deep_9000")
    with pytest.raises(FunnelTraceError, match="data_as_of"):
        _raw_stage(data_as_of="")


def test_stage_kind_must_be_declared_as_hard_gate_or_predictive() -> None:
    with pytest.raises(FunnelTraceError, match="kind must be one of"):
        _raw_stage(kind="whatever")


def test_factory_is_consistent_by_construction() -> None:
    stage = _stage(inputs=5, advanced=3)
    assert stage.inputs == stage.advanced + sum(stage.rejected.values())
    assert stage.drop_rate == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# 完整漏斗顺序与留档内容
# ---------------------------------------------------------------------------


def test_trace_orders_stages_by_the_declared_funnel() -> None:
    trace = build_funnel_trace(
        trade_date=DAY,
        stages=[
            _stage("night_watch_pool", KIND_PREDICTIVE, inputs=50, advanced=30),
            _stage("universe", inputs=5200, advanced=5000,
                   rejected={"delisted": [f"9{i:4d}" for i in range(200)]}),
            _stage("hard_eligibility", inputs=5000, advanced=300,
                   rejected={"low_liquidity": [f"S{i:4d}" for i in range(4700)]}),
        ],
    )
    assert [item.stage for item in trace.stages] == [
        "universe", "hard_eligibility", "night_watch_pool"
    ]
    assert trace.stage("universe") is not None
    assert trace.digest() == build_funnel_trace(
        trade_date=DAY, stages=trace.stages
    ).digest()


def test_stage_outside_the_declared_funnel_must_be_extended_explicitly() -> None:
    rogue = _stage("night_watch_pool", inputs=5, advanced=3)
    rogue.stage = "secret_boost"
    with pytest.raises(FunnelTraceError, match="extend FUNNEL_LAYERS"):
        build_funnel_trace(trade_date=DAY, stages=[rogue])


def test_round_trip_keeps_predictions_identity_and_timestamp() -> None:
    tail = _stage("tail_confirmation", KIND_PREDICTIVE, inputs=30, advanced=2,
                  rejected={"tail_strength_faded": [f"6000{i}.SH" for i in range(2, 30)]},
                  raw_predictions={"600000.SH": 0.71},
                  calibrated_probabilities={"600000.SH": 0.63})
    trace = build_funnel_trace(trade_date=DAY, stages=[tail])
    path = write_trace(trace, "artifacts/research/funnel_trace_tests")
    payload = read_trace(path)
    stage = payload["stages"][0]
    assert stage["raw_predictions"] == {"600000.SH": 0.71}
    assert stage["calibrated_probabilities"] == {"600000.SH": 0.63}
    assert stage["model_identity"]["artifact_content_hash"] == "sha256:deadbeef"
    assert stage["data_as_of"] == "2026-10-08T15:30:00"
    assert stage["contract_digest"] == CONTRACT.digest()
    assert payload["digest"] == trace.digest()
    path.unlink()


def test_written_at_is_the_contract_timezone_not_the_host_clock(tmp_path) -> None:
    """§3.1"修复新记录的时区"：留档时刻必须带时区并写明是哪个时区。

    裸 ``datetime.now()`` 会跟宿主机偏移走，NAS 上跑的留档与本地对不上，
    而这些留档就是影子验证唯一的证据来源。
    """
    trace = build_funnel_trace(trade_date=DAY, stages=[_stage("universe", inputs=5, advanced=5)])
    payload = read_trace(write_trace(trace, tmp_path))
    assert payload["written_at_timezone"] == CONTRACT.timezone == "Asia/Shanghai"
    stamped = datetime.fromisoformat(payload["written_at"])
    assert stamped.utcoffset() == timedelta(hours=8)


# ---------------------------------------------------------------------------
# 最终推荐单独留档：候选快照代表不了它
# ---------------------------------------------------------------------------


def _recommendation_result(rows=None):
    return rank_final_recommendations(
        trade_date=DAY,
        rows=rows or [
            {"symbol": "600000.SH", NET_PROFIT_PROBABILITY_FIELD: 0.74,
             "data_as_of": "2026-10-08T14:31:00"},
            {"symbol": "600001.SH", NET_PROFIT_PROBABILITY_FIELD: 0.66,
             "data_as_of": "2026-10-08T14:35:00"},
            {"symbol": "600002.SH", NET_PROFIT_PROBABILITY_FIELD: 0.41},
        ],
        model_identity=_identity(),
        contract=CONTRACT,
    )


def test_final_rows_carry_probability_strategy_notional_and_identity() -> None:
    result = _recommendation_result()
    rows, rejected = archive_final_recommendations(
        result=result,
        feature_snapshots={
            "600000.SH": {"rs_excess_ret_20": 0.031, "atr14_pct": 0.022},
            "600001.SH": {"rs_excess_ret_20": 0.017, "atr14_pct": 0.03},
        },
        model_identity=_identity(),
        fills={"600000.SH": {"realized": True, "net_profit": True, "net_return": 0.041}},
    )
    first = rows[0].as_dict()
    assert first["rank"] == 1
    assert first["probability"] == pytest.approx(0.74)
    assert first["probability_field"] == NET_PROFIT_PROBABILITY_FIELD
    assert first["reference_notional"] == pytest.approx(10_000.0)
    assert first["strategy"] == "trend"
    assert first["contract_digest"] == CONTRACT.digest()
    assert first["data_as_of"] == "2026-10-08T14:31:00"
    assert first["feature_snapshot"]["rs_excess_ret_20"] == pytest.approx(0.031)
    assert first["fill"]["net_profit"] is True
    assert first["caveats"] == []
    assert "fill_status_missing" in rows[1].as_dict()["caveats"]
    assert [item["reason"] for item in rejected] == ["below_threshold"]


def test_missing_feature_snapshot_is_a_visible_caveat_not_silence() -> None:
    rows, _ = archive_final_recommendations(
        result=_recommendation_result(), feature_snapshots={}, model_identity=_identity(),
        fills={"600000.SH": {"realized": False}},
    )
    assert all(MISSING_FEATURE_SNAPSHOT in row.caveats for row in rows)
    assert rows[0].feature_snapshot == {}


def test_absent_model_identity_is_recorded_not_assumed() -> None:
    rows, _ = archive_final_recommendations(
        result=_recommendation_result(), model_identity=None,
    )
    assert rows[0].model_identity["identity_recorded"] is False
    assert rows[0].model_identity["reason"] == "model_identity_not_supplied"


# ---------------------------------------------------------------------------
# 诊断与对照
# ---------------------------------------------------------------------------


def _trace(day: date, *, predictive_pass: bool) -> object:
    stages = [_stage("universe", inputs=5200, advanced=5000,
                     rejected={"delisted": [f"0{i:4d}" for i in range(200)]}),
              _stage("hard_eligibility", inputs=5000, advanced=300,
                     rejected={"low_liquidity": [f"L{i:4d}" for i in range(4700)]})]
    if predictive_pass:
        stages.append(_stage("quality_300", KIND_PREDICTIVE, inputs=300, advanced=100,
                             rejected={"legacy_composite_floor": [f"Q{i:3d}" for i in range(200)]}))
    rows, rejected = archive_final_recommendations(
        result=_recommendation_result(),
        feature_snapshots={"600000.SH": {"x": 1}, "600001.SH": {"x": 2}},
        model_identity=_identity(),
        fills={"600000.SH": {"realized": True, "net_profit": True, "net_return": 0.02}},
    )
    return build_funnel_trace(trade_date=day, stages=stages,
                              final_recommendations=rows, rejected_final=rejected)


def test_diagnose_separates_hard_gates_from_predictive_rules() -> None:
    report = diagnose_funnel([_trace(DAY, predictive_pass=True),
                              _trace(date(2026, 10, 9), predictive_pass=False)])
    assert report["days"] == 2
    assert report["hard_gate_stages"] == ["hard_eligibility", "universe"]
    assert report["predictive_stages"] == ["quality_300"]
    assert report["per_stage"]["quality_300"]["top_reject_reasons"][
        "legacy_composite_floor"] == 200
    assert 0.0 < report["per_stage"]["hard_eligibility"]["drop_rate"] < 1.0


def test_diagnose_surfaces_days_where_model_identity_was_not_recorded() -> None:
    stage = _stage("deep_50", KIND_PREDICTIVE, inputs=3, advanced=1,
                   rejected={"cross_review": ["600001.SH", "600002.SH"]},
                   model_identity=None)
    report = diagnose_funnel([build_funnel_trace(trade_date=DAY, stages=[stage])])
    assert report["per_stage"]["deep_50"]["model_identity_missing_days"] == 1


def test_compare_traces_requires_identical_trade_days() -> None:
    with pytest.raises(FunnelTraceError, match="交易日集合一致"):
        compare_traces(baseline=[_trace(DAY, predictive_pass=True)],
                       variant=[_trace(date(2026, 10, 9), predictive_pass=False)])


def test_compare_traces_reports_recommendation_and_profit_delta() -> None:
    outcome = compare_traces(
        baseline=[_trace(DAY, predictive_pass=True)],
        variant=[_trace(DAY, predictive_pass=False)],
    )
    assert outcome["days"] == 1
    assert outcome["delta"]["recommendations"] == 0.0
    assert outcome["baseline"]["net_profit_rate"] == pytest.approx(1.0)
