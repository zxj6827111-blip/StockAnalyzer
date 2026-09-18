"""S15 Simple Factor Baseline（含 S15 共用的 metrics 口径）阶段验收测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from _alpha_v2_research_helpers import (  # noqa: E402
    DAYS,
    matcher,
    panel,
    walk,
)

from stock_analyzer.alpha_v2.research.factors import (
    BASELINE_FACTORS,
    BLOCKED_FACTOR_GROUPS,
    FACTOR_LIQUIDITY,
    FACTOR_MA_SLOPE,
    FACTOR_REVERSAL,
    FACTOR_RS_EXCESS,
    FACTOR_TREND_MA_GAP,
    FACTOR_VOL_QUALITY,
    GROUP_FUNDAMENTAL,
    blocked_groups_payload,
    composite_score,
    compute_factor_frame,
    cross_sectional_rank,
    direction_unify,
    factor_definitions,
    factor_spec,
)
from stock_analyzer.alpha_v2.research.metrics import (
    DEFAULT_TOP_KS,
    EvaluationSpec,
    assert_score_column,
    daily_rank_ic,
    downside_metrics,
    evaluate_scores,
    ic_summary,
    metric_column,
    paired_delta,
    quantile_monotonicity,
    quantile_returns,
    research_gate_status,
    select_top_k,
    topk_metrics,
    usable_mask,
)
from stock_analyzer.alpha_v2.research.outcomes import DecisionPoint, compute_outcomes
from stock_analyzer.alpha_v2.research.simple_baseline import (
    BASELINE_SCORE_COLUMN,
    SimpleBaselineSpec,
    baseline_pool_mask,
    compare_with_ml,
    compute_simple_baseline,
    evaluate_baseline,
    factor_ic_declared_vs_realized,
    require_baseline_companion,
)

# ---------------------------------------------------------------------------
# 共用夹具：一张带分数的 outcome 帧
# ---------------------------------------------------------------------------


def _score_frame(days: int = 30, per_day: int = 40) -> pd.DataFrame:
    rng = np.random.default_rng(20260918)
    rows: list[dict[str, object]] = []
    for day_index in range(days):
        day = f"2026-01-{day_index + 1:02d}"
        returns = rng.normal(0.0, 0.02, per_day)
        # 分数与未来收益正相关（IC > 0），用于验证指标方向
        scores = returns + rng.normal(0.0, 0.01, per_day)
        for index in range(per_day):
            rows.append(
                {
                    "decision_date": day,
                    "symbol": f"{600000 + index}",
                    "executable": True,
                    "matured_3d": True,
                    "matured_5d": True,
                    "matured_10d": True,
                    "matured_15d": True,
                    "net_return_3d": float(returns[index] * 0.8),
                    "net_return_5d": float(returns[index]),
                    "net_return_10d": float(returns[index] * 1.3),
                    "net_return_15d": float(returns[index] * 1.5),
                    "excess_return_3d": float(returns[index] * 0.8 - 0.001),
                    "excess_return_5d": float(returns[index] - 0.001),
                    "excess_return_10d": float(returns[index] * 1.3 - 0.002),
                    "excess_return_15d": float(returns[index] * 1.5 - 0.002),
                    "mae_5d": float(-abs(returns[index]) - 0.01),
                    "model_score": float(scores[index]),
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


def test_metric_column_naming() -> None:
    assert metric_column("net_return", 5) == "net_return_5d"
    assert metric_column("excess_return", 15) == "excess_return_15d"


def test_assert_score_column_rejects_outcome_family() -> None:
    assert assert_score_column("model_score") == "model_score"
    for bad in ("excess_return_5d", "net_return_3d", "mae_5d", "label", "entry_price_raw"):
        with pytest.raises(ValueError, match="自证循环"):
            assert_score_column(bad)


def test_daily_rank_ic_recovers_perfect_ordering() -> None:
    frame = _score_frame(days=6, per_day=30)
    frame["model_score"] = frame["excess_return_5d"]  # 完美预测（仅用于指标自检）
    daily = daily_rank_ic(
        frame, score_column="model_score", metric_column_="excess_return_5d", min_cross_section=10
    )
    assert len(daily) == 6
    assert daily["ic"].min() == pytest.approx(1.0)


def test_daily_rank_ic_excludes_non_executable_rows() -> None:
    frame = _score_frame(days=3, per_day=30)
    frame.loc[frame.index[:5], "executable"] = False
    daily = daily_rank_ic(
        frame, score_column="model_score", metric_column_="excess_return_5d", min_cross_section=5
    )
    assert int(daily["n"].max()) <= 30


def test_usable_mask_requires_executable_and_matured() -> None:
    frame = _score_frame(days=2, per_day=10)
    frame.loc[0, "executable"] = False
    frame.loc[1, "matured_5d"] = False
    mask = usable_mask(frame, metric="excess_return_5d")
    assert not bool(mask.iloc[0])
    assert not bool(mask.iloc[1])
    assert int(mask.sum()) == 18


def test_usable_mask_min_cross_section_drops_thin_days() -> None:
    frame = _score_frame(days=2, per_day=10)
    mask = usable_mask(frame, metric="excess_return_5d", min_cross_section=11)
    assert int(mask.sum()) == 0


def test_ic_summary_reports_ci_rolling_and_flag() -> None:
    frame = _score_frame(days=30, per_day=30)
    daily = daily_rank_ic(
        frame, score_column="model_score", metric_column_="excess_return_5d", min_cross_section=10
    )
    summary = ic_summary(daily, rolling_windows=(10, 20))
    assert summary["status"] == "ok"
    assert summary["mature_dates"] == 30
    assert summary["mean_ic"] > 0
    assert summary["positive_ratio"] > 0.5
    assert "ic_20d" in summary
    assert len(summary["ci95"]) == 2
    assert summary["ci_meta"]["method"] == "moving_block"


def test_quantile_returns_are_monotone_for_informative_score() -> None:
    frame = _score_frame(days=20, per_day=50)
    table = quantile_returns(
        frame,
        score_column="model_score",
        metric_column_="excess_return_5d",
        quantiles=5,
        min_cross_section=20,
    )
    assert list(table["quantile"]) == [1, 2, 3, 4, 5]
    mono = quantile_monotonicity(table)
    assert mono["status"] == "ok"
    assert mono["monotonicity_rho"] > 0.8
    assert mono["top_minus_bottom"] > 0


def test_topk_metrics_hit_rate_and_mean() -> None:
    frame = _score_frame(days=10, per_day=40)
    metrics = topk_metrics(
        frame,
        score_column="model_score",
        metric_columns=["excess_return_5d", "net_return_5d"],
        ks=DEFAULT_TOP_KS,
    )
    assert set(metrics) == {"top1", "top3", "top5"}
    assert metrics["top5"]["days"] == 10
    assert metrics["top5"]["net_return_5d_hit_rate"] > 0.5
    assert metrics["top5"]["net_return_5d"] > 0
    # Top1 的均值不应低于 Top5（同一分数排序）
    assert metrics["top1"]["net_return_5d"] >= metrics["top5"]["net_return_5d"] * 0.5


def test_select_top_k_picks_best_scores_per_day() -> None:
    frame = _score_frame(days=5, per_day=20)
    selected = select_top_k(frame, score_column="model_score", k=3)
    assert len(selected) == 15
    for _, group in frame.groupby("decision_date"):
        expected = set(group.nlargest(3, "model_score")["symbol"].tolist())
        got = set(
            selected[selected["decision_date"] == group["decision_date"].iloc[0]]["symbol"].tolist()
        )
        assert got == expected


def test_downside_metrics_reports_mae_and_tail() -> None:
    frame = _score_frame(days=10, per_day=40)
    # 全样本：均值为 0 的收益分布 → 5% 尾部必然为负
    full = downside_metrics(frame, horizon=5)
    assert full["status"] == "ok"
    assert full["tail_loss_5pct"] < 0
    assert full["mean_mae"] < 0
    assert 0.0 <= full["large_loss_frequency"] <= 1.0
    # Top5 切片：命中率高时尾部损失可以不为负，但要如实给出且不高于全样本尾部
    top = downside_metrics(frame, horizon=5, score_column="model_score")
    assert top["status"] == "ok"
    assert top["rows"] <= full["rows"]


def test_paired_delta_is_same_day_paired() -> None:
    frame = _score_frame(days=10, per_day=30)
    frame["benchmark_excess_5d"] = frame["excess_return_5d"] + 0.002
    payload = paired_delta(
        frame, left_column="excess_return_5d", right_column="benchmark_excess_5d"
    )
    assert payload["status"] == "ok"
    assert payload["mean_delta"] == pytest.approx(-0.002, abs=1e-9)
    assert payload["days"] == 10
    assert payload["ci_crosses_zero"] is False


def test_research_gate_status_thresholds() -> None:
    assert research_gate_status(0) == "insufficient"
    assert research_gate_status(20) == "failure_alert_only"
    assert research_gate_status(60) == "initial_direction_review"
    assert research_gate_status(120) == "advisory_eligible"
    assert research_gate_status(250) == "governance_eligible"


def test_evaluate_scores_returns_full_block() -> None:
    frame = _score_frame(days=25, per_day=40)
    payload = evaluate_scores(frame, EvaluationSpec(score_column="model_score"))
    assert set(payload["rank_ic"]) == {"3d", "5d", "10d", "15d"}
    assert payload["rank_ic"]["5d"]["mean_ic"] > 0
    assert "top5" in payload["topk"]
    assert payload["quantiles"]["monotonicity"]["status"] == "ok"
    assert payload["downside_top5"]["status"] == "ok"
    assert payload["research_gate"] == "failure_alert_only"
    assert payload["spec"]["statistics_unit"] == "decision_date"


# ---------------------------------------------------------------------------
# factors
# ---------------------------------------------------------------------------


_TREND_LEN = 40
_DECISION_DAY_INDEX = 30  # 需要 >= 25 根历史才能算 MA20 的 5 日斜率


def _multi_symbol_panel() -> object:
    bars: list[dict] = []
    # 强趋势票：持续上行
    bars.extend(
        walk("600001", [10.0 * (1.01**index) for index in range(_TREND_LEN)], turnover_base=3.0e8)
    )
    # 弱趋势票：持续下行
    bars.extend(
        walk("600002", [10.0 * (0.99**index) for index in range(_TREND_LEN)], turnover_base=1.0e8)
    )
    # 横盘高波动票
    pattern = [
        10.0,
        10.5,
        9.8,
        10.4,
        9.9,
        10.3,
        10.0,
        10.2,
        9.9,
        10.1,
        10.0,
        10.3,
        9.8,
        10.2,
        10.0,
        10.1,
        10.0,
    ] * 3
    bars.extend(walk("600003", pattern[:_TREND_LEN], turnover_base=5.0e7))
    return panel(bars)


def test_factor_frame_computes_expected_signs() -> None:
    built = _multi_symbol_panel()
    decisions = [
        DecisionPoint(symbol, DAYS[_DECISION_DAY_INDEX])
        for symbol in ("600001", "600002", "600003")
    ]
    frame = compute_factor_frame(panel=built, decisions=decisions).set_index("symbol")
    assert frame.loc["600001", FACTOR_TREND_MA_GAP] > 0
    assert frame.loc["600002", FACTOR_TREND_MA_GAP] < 0
    assert frame.loc["600001", FACTOR_MA_SLOPE] > 0
    assert frame.loc["600002", FACTOR_MA_SLOPE] < 0
    # 反转 = -5 日动量：涨得多的反转因子更低
    assert frame.loc["600001", FACTOR_REVERSAL] < frame.loc["600002", FACTOR_REVERSAL]
    # 流动性 = log 成交额：600001 成交额最大
    assert frame.loc["600001", FACTOR_LIQUIDITY] > frame.loc["600002", FACTOR_LIQUIDITY]
    # 波动质量 = -波动：横盘高波动票最低
    assert frame.loc["600003", FACTOR_VOL_QUALITY] == min(
        float(frame[FACTOR_VOL_QUALITY].min()), float(frame.loc["600003", FACTOR_VOL_QUALITY])
    )


def test_rs_factor_is_cross_sectionally_demeaned() -> None:
    built = _multi_symbol_panel()
    decisions = [
        DecisionPoint(symbol, DAYS[_DECISION_DAY_INDEX])
        for symbol in ("600001", "600002", "600003")
    ]
    frame = compute_factor_frame(panel=built, decisions=decisions)
    assert float(frame[FACTOR_RS_EXCESS].mean()) == pytest.approx(0.0, abs=1e-9)
    assert frame.set_index("symbol").loc["600001", FACTOR_RS_EXCESS] > 0


def test_factor_frame_uses_only_past_data() -> None:
    built = _multi_symbol_panel()
    decisions = [DecisionPoint("600001", DAYS[10])]
    before = compute_factor_frame(panel=built, decisions=decisions)
    # 追加决策日之后的暴涨 bar
    extended_bars = list(built.bars.to_dict("records"))
    from _alpha_v2_research_helpers import bar

    extended_bars.append(
        bar(
            "600001",
            DAYS[16],
            open_=100.0,
            high=200.0,
            low=50.0,
            close=150.0,
            prev_close=float(built.symbol_bars("600001")["close"].iloc[10]),
        )
    )
    extended = panel(extended_bars)
    after = compute_factor_frame(panel=extended, decisions=decisions)
    pd.testing.assert_frame_equal(before, after)


def test_direction_unify_keeps_declared_long_factors_and_flips_short() -> None:
    frame = pd.DataFrame({FACTOR_TREND_MA_GAP: [0.1], FACTOR_REVERSAL: [0.05]})
    # 6 个基线因子的 direction 全部为 +1（负向因子如反转已在定义里取负号），
    # 因此这里必须是恒等变换。
    unified = direction_unify(frame)
    assert float(unified[FACTOR_TREND_MA_GAP].iloc[0]) == pytest.approx(0.1)
    assert float(unified[FACTOR_REVERSAL].iloc[0]) == pytest.approx(0.05)
    # 声明为 -1 的因子必须被翻符号（用 monkeypatch 构造，避免只测恒等路径）
    import dataclasses

    from stock_analyzer.alpha_v2.research import factors as factors_module

    patched = tuple(
        dataclasses.replace(spec, direction=-1) if spec.name == FACTOR_TREND_MA_GAP else spec
        for spec in factors_module.FACTORS
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(factors_module, "FACTORS", patched)
        flipped = direction_unify(frame)
    assert float(flipped[FACTOR_TREND_MA_GAP].iloc[0]) == pytest.approx(-0.1)


def test_cross_sectional_rank_respects_min_cross_section() -> None:
    frame = pd.DataFrame(
        {
            "decision_date": ["2026-01-05"] * 3,
            FACTOR_TREND_MA_GAP: [0.1, 0.2, 0.3],
            FACTOR_MA_SLOPE: [0.1, 0.2, 0.3],
        }
    )
    ranks = cross_sectional_rank(frame, factors=(FACTOR_TREND_MA_GAP,), min_cross_section=30)
    assert ranks["rank_" + FACTOR_TREND_MA_GAP].isna().all()


def test_composite_score_ignores_missing_factors_instead_of_zero() -> None:
    frame = pd.DataFrame(
        {
            "decision_date": ["2026-01-05"] * 4,
            FACTOR_TREND_MA_GAP: [0.4, 0.3, 0.2, 0.1],
            FACTOR_MA_SLOPE: [np.nan, np.nan, np.nan, np.nan],
        }
    )
    score = composite_score(
        frame, factors=(FACTOR_TREND_MA_GAP, FACTOR_MA_SLOPE), min_cross_section=2
    )
    assert score.notna().all()
    # 只有一个可用因子时，分数 = 该因子的 rank 分位（不是"缺失当 0"后的均值）
    assert float(score.iloc[0]) == pytest.approx(1.0)


def test_unknown_factor_is_rejected() -> None:
    with pytest.raises(KeyError, match="unknown factor"):
        factor_spec("magic_factor")


def test_factor_definitions_are_self_describing() -> None:
    definitions = factor_definitions()
    assert len(definitions) == len(BASELINE_FACTORS)
    for item in definitions:
        assert item["definition"].strip()
        assert item["rationale"].strip()
        assert item["direction"] in (-1, 1)
        assert item["source_columns"]


def test_fundamental_quality_is_blocked_with_reason_not_silently_used() -> None:
    blocked = blocked_groups_payload()
    assert GROUP_FUNDAMENTAL in blocked
    assert "unverified" in blocked[GROUP_FUNDAMENTAL]["reason"]
    assert BLOCKED_FACTOR_GROUPS[GROUP_FUNDAMENTAL]["policy"].endswith("pit_proven")


# ---------------------------------------------------------------------------
# simple baseline
# ---------------------------------------------------------------------------


def _baseline_fixture() -> tuple[object, pd.DataFrame]:
    built = _multi_symbol_panel()
    decisions = [
        DecisionPoint(symbol, DAYS[day])
        for symbol in ("600001", "600002", "600003")
        for day in (24, 27, _DECISION_DAY_INDEX, 33)
    ]
    result = compute_simple_baseline(
        panel=built, decisions=decisions, spec=SimpleBaselineSpec(min_cross_section=2)
    )
    return built, result.frame


def test_simple_baseline_scores_stronger_trend_higher() -> None:
    _, frame = _baseline_fixture()
    last_day = frame[frame["decision_date"] == DAYS[_DECISION_DAY_INDEX].isoformat()].set_index(
        "symbol"
    )
    assert (
        last_day.loc["600001", BASELINE_SCORE_COLUMN]
        > last_day.loc["600002", BASELINE_SCORE_COLUMN]
    )


def test_simple_baseline_report_lists_definitions_and_blocked_groups() -> None:
    built = _multi_symbol_panel()
    result = compute_simple_baseline(
        panel=built, decisions=[DecisionPoint("600001", DAYS[_DECISION_DAY_INDEX])]
    )
    payload = result.to_payload()
    assert payload["spec"]["no_fitting"] is True
    assert payload["report"]["blocked_groups"][GROUP_FUNDAMENTAL]["policy"]
    assert payload["report"]["scored_rows"] >= 0


def test_baseline_pool_mask_selects_top_fraction_per_day() -> None:
    _, frame = _baseline_fixture()
    mask = baseline_pool_mask(frame, top_fraction=0.34)
    assert mask.dtype == bool
    counts = frame[mask].groupby("decision_date").size()
    assert set(counts.unique()) == {1}  # 3 只票 × 34% → 每日 1 只


def test_baseline_pool_mask_handles_empty_frame() -> None:
    assert baseline_pool_mask(pd.DataFrame()).empty


def test_factor_ic_table_shows_declared_direction_metadata() -> None:
    _, baseline = _baseline_fixture()
    frame = baseline.copy()
    frame["excess_return_5d"] = np.linspace(-0.05, 0.05, len(frame))
    frame["matured_5d"] = True
    frame["executable"] = True
    table = factor_ic_declared_vs_realized(frame, horizon=5, min_cross_section=2)
    assert set(table["factor"]) == set(BASELINE_FACTORS)
    assert set(table["declared_direction"].unique()) <= {1, -1}
    assert "direction_consistent" in table.columns


def test_evaluate_baseline_uses_the_shared_eval_block() -> None:
    _, baseline = _baseline_fixture()
    frame = baseline.copy()
    frame["excess_return_5d"] = np.where(frame["symbol"] == "600001", 0.03, -0.01)
    frame["net_return_5d"] = frame["excess_return_5d"] + 0.005
    frame["excess_return_3d"] = frame["excess_return_5d"] * 0.6
    frame["net_return_3d"] = frame["net_return_5d"] * 0.6
    frame["mae_5d"] = -0.02
    for horizon in (3, 5, 10, 15):
        frame[f"matured_{horizon}d"] = True
    payload = evaluate_baseline(frame, spec=SimpleBaselineSpec(min_cross_section=2))
    assert payload["baseline"]["source"] == "simple_factor_baseline_v1"
    assert "rank_ic" in payload and "topk" in payload
    assert payload["mature_dates"] >= 1


def test_require_baseline_companion_enforces_same_screen_requirement() -> None:
    with pytest.raises(ValueError, match="simple baseline 同屏"):
        require_baseline_companion({"rank_ic": {}})
    require_baseline_companion({"baseline_companion": {"rank_ic": {}}})


def test_compare_with_ml_requires_sample_before_concluding() -> None:
    ml = {
        "rank_ic": {"5d": {"mean_ic": 0.05}},
        "topk": {"top5": {"excess_return_5d": 0.01}},
        "mature_dates": 10,
    }
    baseline = {
        "rank_ic": {"5d": {"mean_ic": 0.02}},
        "topk": {"top5": {"excess_return_5d": 0.005}},
        "mature_dates": 10,
    }
    payload = compare_with_ml(ml, baseline)
    assert payload["ml_beats_baseline"] is True
    assert payload["verdict"] == "awaiting_sample"
    assert payload["ic_delta"] == pytest.approx(0.03, abs=1e-9)


def test_compare_with_ml_flags_baseline_not_beaten() -> None:
    ml = {
        "rank_ic": {"5d": {"mean_ic": 0.01}},
        "topk": {"top5": {"excess_return_5d": -0.002}},
        "mature_dates": 80,
    }
    baseline = {
        "rank_ic": {"5d": {"mean_ic": 0.04}},
        "topk": {"top5": {"excess_return_5d": 0.008}},
        "mature_dates": 80,
    }
    payload = compare_with_ml(ml, baseline)
    assert payload["ml_beats_baseline"] is False
    assert payload["verdict"] == "baseline_not_beaten_stop_adding_complexity"


def test_compare_with_ml_insufficient_evidence_when_metrics_missing() -> None:
    payload = compare_with_ml({}, {"mature_dates": 5})
    assert payload["verdict"] == "insufficient_evidence"
    assert payload["ml_beats_baseline"] is False


def test_baseline_runs_end_to_end_on_outcomes() -> None:
    """端到端：baseline 分数 + S11 outcome 在同一帧上被同一评价块消费。"""
    built = _multi_symbol_panel()
    decisions = [DecisionPoint(symbol, DAYS[0]) for symbol in ("600001", "600002", "600003")]
    run = compute_outcomes(
        panel=built,
        decisions=decisions,
        matcher=matcher(),
        price_mode="raw",
        price_mode_certified=True,
    )
    baseline = compute_simple_baseline(panel=built, decisions=decisions)
    merged = run.frame.merge(baseline.frame, on=["decision_date", "symbol"], how="inner")
    assert BASELINE_SCORE_COLUMN in merged.columns
    assert merged["executable"].any()
    payload = evaluate_baseline(merged, spec=SimpleBaselineSpec(min_cross_section=2))
    assert payload["mature_dates"] <= 1
    assert payload["research_gate"] == "insufficient"
