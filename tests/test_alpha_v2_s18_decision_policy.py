"""S18 Final Decision Policy V2 Shadow 阶段验收测试。

守住四件事：不造"新 70 分"、不强制选满、不接管 Legacy、不编造值。
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from stock_analyzer.alpha_v2.research.decision_policy import (
    CANDIDATE_STAGE_DEEP,
    DECISION_FIELDS,
    POLICY_ID,
    POLICY_SCHEMA,
    FinalPolicySpec,
    assert_no_threshold_knobs,
    build_shadow_selection,
    policy_horizon_columns,
    write_shadow_policy_report,
)


def _candidates(
    *,
    count: int = 12,
    day: str = "2026-03-02",
    fillable: dict[str, bool] | None = None,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for index in range(count):
        symbol = f"{600000 + index:06d}"
        rows.append(
            {
                "decision_date": day,
                "symbol": symbol,
                "deep_pool": True,
                "alpha_rank_score": 1.0 - index / max(1, count),
                "expected_excess_return_5d": 0.02 - index * 0.002,
                "expected_net_return_5d": 0.025 - index * 0.002,
                "p_up_net_5d": 0.6 - index * 0.01,
                "p_up_excess_5d": 0.55 - index * 0.01,
                "expected_mae_5d": -0.02 - index * 0.001,
                "p_mae_le_5pct_5d": 0.1 + index * 0.01,
                "executable": (fillable or {}).get(symbol, True),
                "no_fill_reason": "" if (fillable or {}).get(symbol, True) else "limit_up_open",
                "entry_delay_sessions": 1,
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 结构约束
# ---------------------------------------------------------------------------


def test_policy_spec_has_no_threshold_knobs() -> None:
    fields = set(FinalPolicySpec.__dataclass_fields__)
    assert not {field for field in fields if "threshold" in field or "min_score" in field}
    assert_no_threshold_knobs()
    payload = FinalPolicySpec().to_payload()
    assert payload["score_thresholds"] == []
    assert payload["enforced"] is False
    assert payload["allow_zero_signal"] is True


def test_policy_module_never_reads_legacy_thresholds() -> None:
    import pathlib

    source = pathlib.Path("src/stock_analyzer/alpha_v2/research/decision_policy.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "final_signal_min_threshold",
        "p_lgbm_min",
        "p_xgb_min",
        "p_meta_min",
        "max_diff",
    ):
        assert forbidden not in source, forbidden


# ---------------------------------------------------------------------------
# 排序与选取
# ---------------------------------------------------------------------------


def test_ranked_candidates_sorted_by_alpha_rank() -> None:
    frame = _candidates()
    result = build_shadow_selection(frame)
    ranks = result.ranked["alpha_rank_score"].tolist()
    assert ranks == sorted(ranks, reverse=True)
    top1 = result.selections["v2_top1"]
    assert top1["symbol"].iloc[0] == "600000"


def test_selections_have_expected_sizes() -> None:
    frame = _candidates(count=12)
    result = build_shadow_selection(frame)
    assert [len(result.selections[f"v2_top{k}"]) for k in (1, 3, 5)] == [1, 3, 5]


def test_unfillable_candidates_are_skipped_but_still_recorded() -> None:
    frame = _candidates(fillable={"600000": False, "600001": False})
    result = build_shadow_selection(frame)
    # 前两名不可成交 → top1/top3 顺延，但 ranked 里仍能看到它们
    assert result.selections["v2_top1"]["symbol"].iloc[0] == "600002"
    ranked_symbols = set(result.ranked["symbol"])
    assert {"600000", "600001"}.issubset(ranked_symbols)
    payload = result.to_payload()
    assert payload["unfillable_count"] == 2
    assert payload["candidate_count"] == 12


def test_zero_signal_is_valid_and_not_filled_artificially() -> None:
    frame = _candidates(count=3)
    frame["deep_pool"] = False  # 当天没有 Deep50 候选
    result = build_shadow_selection(frame)
    assert result.ranked.empty
    payload = result.to_payload()
    assert payload["status"] == "no_candidates"
    assert payload["allow_zero_signal"] is True
    assert payload["zero_signal_is_valid"] is True
    assert payload["selections"]["v2_top5"] == []


def test_fewer_candidates_than_k_is_accepted() -> None:
    frame = _candidates(count=2)
    result = build_shadow_selection(frame)
    assert len(result.selections["v2_top5"]) == 2
    assert result.to_payload()["selection_sizes"]["v2_top5"] == 2


def test_candidates_missing_rank_are_kept_but_ranked_last() -> None:
    frame = _candidates(count=5)
    frame.loc[0, "alpha_rank_score"] = np.nan
    result = build_shadow_selection(frame)
    assert len(result.ranked) == 5
    assert str(result.ranked["symbol"].iloc[-1]) == "600000"


def test_selection_scoped_to_decision_date() -> None:
    frame = pd.concat(
        [_candidates(day="2026-03-02"), _candidates(day="2026-03-03")], ignore_index=True
    )
    result = build_shadow_selection(frame, decision_date="2026-03-03")
    assert set(result.ranked["decision_date"]) == {"2026-03-03"}
    assert result.to_payload()["decision_date"] == "2026-03-03"


def test_missing_stage_column_is_fail_closed_not_treat_everything_as_candidates() -> None:
    """没有候选阶段标记时不得把全部行当 Deep50（那等于悄悄扩大候选池）。"""
    frame = _candidates(count=5).drop(columns=["deep_pool"])
    result = build_shadow_selection(frame)
    assert result.ranked.empty
    assert result.to_payload()["status"] == "candidate_stage_column_missing"
    # 调用方明确声明"已自行过滤"时才允许跳过该守卫
    relaxed = build_shadow_selection(frame, spec=FinalPolicySpec(require_stage_column=False))
    assert len(relaxed.ranked) == 5


def test_only_deep_pool_rows_are_candidates() -> None:
    frame = _candidates(count=5)
    frame.loc[0, "deep_pool"] = False
    result = build_shadow_selection(frame)
    assert "600000" not in set(result.ranked["symbol"])


def test_require_fillable_can_be_disabled_for_research() -> None:
    frame = _candidates(fillable={"600000": False})
    result = build_shadow_selection(frame, spec=FinalPolicySpec(require_fillable=False))
    assert result.selections["v2_top1"]["symbol"].iloc[0] == "600000"


# ---------------------------------------------------------------------------
# 不编造值
# ---------------------------------------------------------------------------


def test_missing_head_fields_are_not_available_not_fabricated() -> None:
    frame = _candidates(count=4).drop(columns=["p_up_net_5d", "expected_mae_5d"])
    result = build_shadow_selection(frame)
    payload = result.to_payload()
    top1 = payload["selections"]["v2_top1"][0]
    assert top1["p_up_net_5d"] == "not_available"
    assert top1["expected_mae_5d"] == "not_available"
    assert top1["alpha_rank_score"] is not None


def test_nan_values_become_not_available() -> None:
    frame = _candidates(count=3)
    frame.loc[0, "expected_excess_return_5d"] = np.nan
    result = build_shadow_selection(frame)
    top1 = result.to_payload()["selections"]["v2_top1"][0]
    assert top1["expected_excess_return_5d"] == "not_available"


def test_candidate_payload_carries_all_decision_fields() -> None:
    result = build_shadow_selection(_candidates(count=3))
    top1 = result.to_payload()["selections"]["v2_top1"][0]
    for field_name in DECISION_FIELDS:
        assert field_name in top1
    assert top1["fillable"] is True
    assert top1["rank"] == 1


# ---------------------------------------------------------------------------
# 门禁只标注不启用
# ---------------------------------------------------------------------------


def test_gates_are_annotated_but_not_enforced() -> None:
    frame = _candidates(count=6)
    frame["risk_level"] = ["reject"] + ["low"] * 5
    frame["data_health"] = "healthy"
    frame["market_regime"] = "weak"
    result = build_shadow_selection(frame)
    payload = result.to_payload()
    gates = payload["gates"]
    assert gates["enforced"] is False
    assert "600000" in gates["risk"]["would_block"]
    assert gates["would_be_empty"] is False
    # 标注不影响候选列表：不可成交以外的因素都不改变选择
    assert result.selections["v2_top1"]["symbol"].iloc[0] == "600000"


def test_gates_report_not_available_when_columns_absent() -> None:
    result = build_shadow_selection(_candidates(count=3))
    gates = result.to_payload()["gates"]
    assert gates["data_health"]["status"] == "not_available"
    assert gates["market_regime"]["would_block"] == []


def test_would_be_empty_flag_when_top1_is_not_fillable() -> None:
    frame = _candidates(count=2, fillable={"600000": False, "600001": False})
    result = build_shadow_selection(frame)
    assert result.to_payload()["gates"]["would_be_empty"] is True


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------


def test_write_shadow_report_is_atomic_and_dated(tmp_path) -> None:
    result = build_shadow_selection(_candidates(count=4))
    target = write_shadow_policy_report(
        root=tmp_path / "reports", payload=result.to_payload(), decision_date="2026-03-02"
    )
    assert target.name == "shadow_policy_20260302.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["schema"] == POLICY_SCHEMA
    assert payload["spec"]["policy_id"] == POLICY_ID
    assert payload["selections"]["v2_top1"][0]["symbol"] == "600000"
    assert payload["candidate_count"] == 4


def test_write_shadow_report_does_not_leave_temp_files(tmp_path) -> None:
    result = build_shadow_selection(_candidates(count=2))
    write_shadow_policy_report(
        root=tmp_path, payload=result.to_payload(), decision_date="2026-03-02"
    )
    leftovers = [path.name for path in tmp_path.iterdir() if path.name.startswith(".")]
    assert leftovers == []


def test_policy_horizon_columns_include_short_horizon_confirmation() -> None:
    columns = policy_horizon_columns(5)
    assert "expected_excess_return_5d" in columns
    assert "p_up_net_5d" in columns
    assert "p_up_net_3d" in columns
    assert "mae_5d" in columns


def test_candidate_stage_default_is_deep_pool() -> None:
    assert FinalPolicySpec().candidate_stage == CANDIDATE_STAGE_DEEP
    assert FinalPolicySpec().rank_column == "alpha_rank_score"
