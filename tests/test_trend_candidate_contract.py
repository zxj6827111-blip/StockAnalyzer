"""候选池与特征契约验收（改进计划 §3.2）。

钉住的三件事：硬门与预测性规则的边界、"算完轻量特征再截断"、
缺数据一律 NaN 而不是填 0（FEATURE_COMPUTE_VERSION v1 的老坑）。
再加一条：新闻/主题/completion 这类历史不可复现的信息不许进训练。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from stock_analyzer.feature.trend_candidate_contract import (
    ADVISORY_SOURCES,
    FEATURE_GROUPS,
    GROUP_MARKET_RELATIVE,
    GROUP_TREND_POSITION,
    GROUP_VOLATILITY_OVERHEAT,
    HARD,
    PREDICTIVE,
    TREND_FEATURE_CONTRACT_VERSION,
    FeatureAvailability,
    TrendFeatureFrame,
    ablation_ladder,
    apply_hard_gates,
    assert_no_silent_zero_fill,
    assert_training_features,
    build_trend_feature_frame,
    classify_rule,
    columns_for_groups,
    compute_then_truncate,
    declared_rules,
    is_declared_rule,
    select_ablation_columns,
)

DECISION = date(2026, 10, 8)


def _frame(rows: list[dict] | None = None) -> pd.DataFrame:
    if rows is not None:
        return pd.DataFrame(rows)
    return pd.DataFrame([
        {"symbol": "600000.SH", "date": DECISION, "excess_ret_20": 0.03,
         "relative_strength": 1.2, "ma20": 10.0, "close_to_ma20": 0.02,
         "avg_turnover_20": 8e6, "atr14_pct": 0.021, "ret_5": 0.04},
        {"symbol": "600001.SH", "date": DECISION, "excess_ret_20": -0.01,
         "relative_strength": 0.9, "ma20": 10.0, "close_to_ma20": -0.01,
         "avg_turnover_20": 2e6, "atr14_pct": 0.03, "ret_5": 0.01},
    ])


def test_predictive_rules_may_not_gate_eligibility() -> None:
    """§2 的消融只许动预测性规则；资格只由硬门决定。"""
    outcome = apply_hard_gates(
        symbols=["A", "B", "C"],
        gates={"min_avg_turnover_20": ["C"], "is_st": ["B"]},
    )
    outcome.assert_only_hard_gates()
    assert outcome.eligible == ("A",)
    assert outcome.rejected_counts == {"min_avg_turnover_20": 1, "is_st": 1}


def test_predictive_rule_used_as_a_gate_is_rejected() -> None:
    outcome = apply_hard_gates(
        symbols=["A", "B"], gates={"composite_score_floor": ["B"]})
    with pytest.raises(ValueError, match="must not gate eligibility"):
        outcome.assert_only_hard_gates()


def test_unknown_rule_is_flagged_but_still_blocks_conservatively() -> None:
    outcome = apply_hard_gates(symbols=["A", "B"], gates={"secret_boost": ["B"]})
    assert outcome.unknown_rules == ("secret_boost",)
    assert outcome.eligible == ("A",)
    assert classify_rule("secret_boost") == PREDICTIVE
    assert not is_declared_rule("secret_boost")


def test_known_rule_split_covers_the_legacy_bonus_list() -> None:
    predictive = set(declared_rules(PREDICTIVE))
    for legacy in ("composite_score_floor", "grade_s_a_only", "cross_review",
                   "sector_quota", "exploration_sample", "recovery_buy",
                   "disagreement_probe", "theme_boost", "completion_boost"):
        assert legacy in predictive, legacy
    assert {"min_avg_turnover_20", "suspended", "stale_market_data",
            "overextension_risk"} <= set(declared_rules(HARD))


def test_advisory_bonus_rules_are_declared_and_never_hard_gates() -> None:
    """新闻/主题/completion/板块配额/探索样本只能做建议性加分，不得决定资格。"""
    hard = set(declared_rules(HARD))
    for source in ADVISORY_SOURCES:
        assert is_declared_rule(source), source
        assert classify_rule(source) == PREDICTIVE, source
        assert source not in hard, source


def test_non_reproducible_information_is_refused_by_training() -> None:
    """§3.2"历史不可复现的信息不得混入训练"：这些列当时未必拿得到，喂进去就是 look-ahead。"""
    for column in ("news_sentiment", "theme_heat", "completion_pct",
                   "sector_quota_share", "exploration_flag", "analyst_rating"):
        with pytest.raises(ValueError, match="不可复现的信息源"):
            assert_training_features([column])


def test_columns_outside_the_four_groups_are_refused_too() -> None:
    """"不认识"不等于"可复现"：未登记的列同样拒绝，理由是"未登记"而不是"不可复现"。"""
    for column in ("signal", "composite_score", "p_meta"):
        with pytest.raises(ValueError, match="未登记在特征契约里"):
            assert_training_features([column])
    with pytest.raises(ValueError, match="at least one"):
        assert_training_features([])
    with pytest.raises(ValueError, match="duplicate"):
        assert_training_features(["ret_5", "ret_5"])


def test_the_four_reproducible_groups_are_training_eligible() -> None:
    """四组已有信息整体放行，逐组也放行 —— 门不能把自己的输入拒掉。"""
    every_group = columns_for_groups(FEATURE_GROUPS)
    assert assert_training_features(every_group) == every_group
    for group, columns in FEATURE_GROUPS.items():
        assert assert_training_features(columns) == columns, group


def test_truncation_happens_after_scoring_and_drops_unscored() -> None:
    survivors = ["A", "B", "C", "D"]
    scored = {"A": 0.9, "B": 0.7, "D": 0.8}  # C 没算出轻量特征
    picked = compute_then_truncate(eligible=survivors, scored=scored, limit=2)
    assert picked == ("A", "D")
    assert compute_then_truncate(eligible=survivors, scored=scored, limit=0) == (
        "A", "D", "B")


def test_missing_benchmark_makes_relative_group_unavailable_not_zero() -> None:
    frame = build_trend_feature_frame(
        engineered=_frame(),
        availability=FeatureAvailability(benchmark_available=False),
        decision_date=DECISION,
    )
    assert GROUP_MARKET_RELATIVE in frame.unavailable_groups
    matrix = frame.to_matrix()
    assert matrix["excess_ret_20"].isna().all()
    assert matrix["relative_strength"].isna().all()
    assert frame.frame["ma20"].notna().all()
    assert_no_silent_zero_fill(frame)


def test_zero_filled_relative_group_is_called_out_as_a_violation() -> None:
    frame = build_trend_feature_frame(
        engineered=_frame(),
        availability=FeatureAvailability(benchmark_available=False),
        decision_date=DECISION,
    )
    frame.frame["excess_ret_20"] = 0.0  # 有人把缺指数填成 0
    with pytest.raises(ValueError, match="must stay NaN"):
        assert_no_silent_zero_fill(frame)


def test_asof_boundary_excludes_bars_after_the_decision_date() -> None:
    rows = [
        {"symbol": "A", "date": DECISION, "ma20": 10.0, "excess_ret_20": 0.01},
        {"symbol": "A", "date": date(2026, 10, 9), "ma20": 12.0, "excess_ret_20": 0.5},
    ]
    frame = build_trend_feature_frame(
        engineered=_frame(rows),
        availability=FeatureAvailability(benchmark_available=True),
        decision_date=DECISION,
    )
    assert len(frame.frame) == 1
    assert frame.as_of == str(DECISION)
    assert frame.contract_version == TREND_FEATURE_CONTRACT_VERSION


def test_stale_benchmark_also_disqualifies_the_relative_group() -> None:
    """指数存在但停在两个月前：存在 ≠ 可用。"""
    fresh = FeatureAvailability(benchmark_available=True, stale_benchmark_days=0)
    assert fresh.unavailable_groups() == ()
    stale = FeatureAvailability(benchmark_available=True, stale_benchmark_days=40)
    assert stale.unavailable_groups() == (GROUP_MARKET_RELATIVE,)
    frame = build_trend_feature_frame(
        engineered=_frame(), availability=stale, decision_date=DECISION,
    )
    assert frame.to_matrix()["excess_ret_20"].isna().all()
    assert frame.frame["ma20"].notna().all()


def test_ablation_ladder_adds_then_leaves_one_out() -> None:
    ladder = ablation_ladder()
    groups = list(FEATURE_GROUPS)
    assert ladder[0] == (groups[0],)
    assert ladder[len(groups) - 1] == tuple(groups)
    # 增量阶梯的最后一步与"留掉最后一组"重合，去重后是 2N-1 级
    assert len(ladder) == 2 * len(groups) - 1
    assert tuple(groups) in ladder
    for dropped in groups:
        assert tuple(g for g in groups if g != dropped) in ladder
    assert columns_for_groups([GROUP_MARKET_RELATIVE]) == FEATURE_GROUPS[GROUP_MARKET_RELATIVE]
    with pytest.raises(ValueError, match="unknown feature group"):
        columns_for_groups(["sentiment"])


def test_ablation_step_only_uses_columns_that_exist() -> None:
    frame = build_trend_feature_frame(
        engineered=_frame(),
        availability=FeatureAvailability(benchmark_available=True),
        decision_date=DECISION,
    )
    picked = select_ablation_columns(frame, [GROUP_TREND_POSITION])
    assert set(picked) <= {"ma20", "close_to_ma20"}
    assert GROUP_VOLATILITY_OVERHEAT not in picked
    assert isinstance(frame, TrendFeatureFrame)


def test_frame_without_date_column_is_rejected() -> None:
    with pytest.raises(ValueError, match="date"):
        build_trend_feature_frame(
            engineered=pd.DataFrame([{"symbol": "A"}]),
            availability=FeatureAvailability(benchmark_available=True),
            decision_date=DECISION,
        )
