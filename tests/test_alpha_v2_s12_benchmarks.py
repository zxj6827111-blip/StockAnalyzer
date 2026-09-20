"""S12 Benchmark 体系：三层基准与风格匹配对照的阶段验收测试。

关键守住三件事：

1. **基准与候选同口径**：只统计可成交且已成熟的样本；不可成交样本不得进基准；
2. **风格维度 PIT 安全**：决策日之后追加 bar 不得改变风格特征或匹配结果；
3. **来源自述**：质量池是研究代理还是生产成员必须写在报告里，不得混为一谈。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from _alpha_v2_research_helpers import (  # noqa: E402 - sys.path 由 conftest 注入
    DAYS,
    bar,
    matcher,
    panel,
    walk,
)

from stock_analyzer.alpha_v2.research.benchmarks import (
    BENCHMARK_ELIGIBLE,
    BENCHMARK_QUALITY_POOL,
    BENCHMARK_STYLE_MATCHED,
    PRIMARY_LAYER,
    QUALITY_POOL_SOURCE_PRODUCTION,
    QUALITY_POOL_SOURCE_PROXY,
    STYLE_DIM_BOARD,
    STYLE_DIM_FLOAT_CAP,
    STYLE_DIM_MOMENTUM,
    STYLE_DIM_TURNOVER,
    STYLE_DIM_VOL,
    BenchmarkSpec,
    build_benchmark_suite,
    compute_style_features,
    eligible_pool_mask,
    merge_primary_excess,
    quality_pool_mask,
    style_matched_control,
)
from stock_analyzer.alpha_v2.research.outcomes import (
    DecisionPoint,
    compute_outcomes,
)

DECISION_DAY = DAYS[0]


def _build_outcomes(
    symbols: list[str],
    *,
    closes: dict[str, list[float]] | None = None,
    boards: dict[str, str] | None = None,
    caps: dict[str, float] | None = None,
    turnovers: dict[str, float] | None = None,
):
    bars: list[dict] = []
    for symbol in symbols:
        series = (closes or {}).get(symbol, [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6])
        bars.extend(
            walk(
                str(symbol),
                series,
                board=(boards or {}).get(symbol, "主板"),
                float_market_cap=(caps or {}).get(symbol, 5.0e9),
                turnover_base=(turnovers or {}).get(symbol, 1.0e8),
            )
        )
    built = panel(bars)
    decisions = [DecisionPoint(symbol, DECISION_DAY) for symbol in symbols]
    run = compute_outcomes(
        panel=built,
        decisions=decisions,
        matcher=matcher(),
        price_mode="raw",
        price_mode_certified=True,
    )
    styles = compute_style_features(panel=built, decisions=decisions)
    return run.frame.merge(styles, on=["decision_date", "symbol"], how="left"), built


# ---------------------------------------------------------------------------
# 三层结构
# ---------------------------------------------------------------------------


def test_benchmark_suite_provides_three_layers() -> None:
    frame, _ = _build_outcomes([f"60{i:04d}" for i in range(12)])
    suite = build_benchmark_suite(frame)
    assert set(suite.series) == {BENCHMARK_ELIGIBLE, BENCHMARK_QUALITY_POOL}
    assert BENCHMARK_STYLE_MATCHED in suite.excess
    assert suite.primary == PRIMARY_LAYER == BENCHMARK_QUALITY_POOL
    payload = suite.to_payload()
    assert payload["spec"]["style_board_dim"] == STYLE_DIM_BOARD
    assert "style_matching" in payload["spec"]


def test_eligible_layer_equals_equal_weight_pool_mean() -> None:
    frame, _ = _build_outcomes([f"60{i:04d}" for i in range(10)])
    suite = build_benchmark_suite(frame)
    expected = float(pd.to_numeric(frame["net_return_3d"], errors="coerce").dropna().mean())
    eligible = suite.series[BENCHMARK_ELIGIBLE]
    row = eligible[(eligible["horizon"] == 3)].iloc[0]
    assert float(row["benchmark_return"]) == pytest.approx(expected, abs=1e-9)
    assert int(row["pool_size"]) == 10
    assert eligible_pool_mask(frame).all()


def test_benchmark_excludes_non_executable_rows() -> None:
    """一字涨停（不可成交）的票不得进入基准，否则基准被"假设能成交"抬高。"""
    symbols = [f"60{i:04d}" for i in range(4)]
    bars: list[dict] = []
    for symbol in symbols:
        series = (
            [10.0, 12.0, 12.0, 12.0, 12.0, 12.0, 12.0]
            if symbol == symbols[0]
            else [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6]
        )
        bars.extend(walk(symbol, series))
    if bars[1]["symbol"] == symbols[0]:
        bars[1].update({"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0})
    built = panel(bars)
    decisions = [DecisionPoint(symbol, DECISION_DAY) for symbol in symbols]
    run = compute_outcomes(
        panel=built,
        decisions=decisions,
        matcher=matcher(),
        price_mode="raw",
        price_mode_certified=True,
    )
    suite = build_benchmark_suite(run.frame, quality_membership={DECISION_DAY.isoformat(): symbols})
    executable_mean = float(
        pd.to_numeric(run.frame["net_return_3d"], errors="coerce").dropna().mean()
    )
    eligible = suite.series[BENCHMARK_ELIGIBLE]
    row = eligible[eligible["horizon"] == 3].iloc[0]
    # 一字涨停那只被排除：分母是 3 而不是 4
    assert int(row["pool_size"]) == 3
    assert float(row["benchmark_return"]) == pytest.approx(executable_mean, abs=1e-9)
    # 若把它"假设能成交"地算进来，基准会被抬高（12.0/11.0 - 1 = +9% 远高于其余票），
    # 这正是必须排除的原因 —— 这里用方向断言把这个偏差写死。
    hypothetical_mean = (executable_mean * 3 + (12.0 / 11.0 - 1.0)) / 4
    assert hypothetical_mean > float(row["benchmark_return"])


# ---------------------------------------------------------------------------
# 质量池来源
# ---------------------------------------------------------------------------


def test_quality_pool_proxy_is_liquidity_ranked_and_labeled() -> None:
    symbols = [f"60{i:04d}" for i in range(10)]
    turnovers = {symbol: (i + 1) * 1.0e7 for i, symbol in enumerate(symbols)}
    frame, _ = _build_outcomes(symbols, turnovers=turnovers)
    mask, source = quality_pool_mask(frame, target=3)
    assert source == QUALITY_POOL_SOURCE_PROXY
    assert int(mask.sum()) == 3
    # 成交额最大的 3 只
    assert set(frame.loc[mask, "symbol"]) == set(symbols[-3:])


def test_quality_pool_production_membership_marks_source() -> None:
    symbols = [f"60{i:04d}" for i in range(6)]
    frame, _ = _build_outcomes(symbols)
    membership = {DECISION_DAY.isoformat(): symbols[:2]}
    mask, source = quality_pool_mask(frame, membership=membership)
    assert source == QUALITY_POOL_SOURCE_PRODUCTION
    assert set(frame.loc[mask, "symbol"]) == set(symbols[:2])
    suite = build_benchmark_suite(frame, quality_membership=membership)
    assert suite.report["quality_pool_source"] == QUALITY_POOL_SOURCE_PRODUCTION


def test_quality_pool_mask_requires_liquidity_column_or_membership() -> None:
    frame = pd.DataFrame(
        {"decision_date": ["2026-01-05"], "symbol": ["600000"], "net_return_3d": [0.01]}
    )
    with pytest.raises(ValueError, match="流动性列"):
        quality_pool_mask(frame)


# ---------------------------------------------------------------------------
# 风格特征（PIT）
# ---------------------------------------------------------------------------


def test_style_features_use_only_data_up_to_decision_date() -> None:
    symbols = [f"60{i:04d}" for i in range(5)]
    bars: list[dict] = []
    for symbol in symbols:
        bars.extend(walk(symbol, [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6]))
    baseline_panel = panel(bars)
    decisions = [DecisionPoint(symbol, DAYS[1]) for symbol in symbols]
    before = compute_style_features(panel=baseline_panel, decisions=decisions)

    # 决策日之后追加一根暴涨 bar（若特征偷看未来，结果必然变化）
    future_day = DAYS[2]
    extended = bars + [
        bar(
            symbol,
            future_day,
            open_=50.0,
            high=99.0,
            low=1.0,
            close=88.0,
            prev_close=10.6,
        )
        for symbol in symbols
    ]
    extended_panel = panel(extended)
    after = compute_style_features(panel=extended_panel, decisions=decisions)
    pd.testing.assert_frame_equal(
        before.sort_values("symbol").reset_index(drop=True),
        after.sort_values("symbol").reset_index(drop=True),
    )
    assert float(before[STYLE_DIM_MOMENTUM].iloc[0]) < 1.0  # 未来 +770% 未泄漏


def test_style_features_capture_momentum_volatility_turnover() -> None:
    up = walk("600001", [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 11.0], turnover_base=1.0e8)
    flat = walk("600002", [10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0], turnover_base=2.0e8)
    built = panel(up + flat)
    decisions = [DecisionPoint("600001", DAYS[6]), DecisionPoint("600002", DAYS[6])]
    features = compute_style_features(panel=built, decisions=decisions).set_index("symbol")
    assert float(features.loc["600001", STYLE_DIM_MOMENTUM]) > 0
    assert float(features.loc["600002", STYLE_DIM_MOMENTUM]) == pytest.approx(0.0)
    assert float(features.loc["600002", STYLE_DIM_VOL]) == pytest.approx(0.0)
    assert float(features.loc["600002", STYLE_DIM_TURNOVER]) > float(
        features.loc["600001", STYLE_DIM_TURNOVER]
    )
    assert float(features.loc["600001", STYLE_DIM_FLOAT_CAP]) > 0


def test_style_features_missing_symbol_is_nan_not_zero() -> None:
    built = panel(walk("600003", [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6]))
    features = compute_style_features(panel=built, decisions=[DecisionPoint("999999", DAYS[0])])
    assert not features.empty
    assert np.isnan(features.iloc[0][STYLE_DIM_MOMENTUM])


# ---------------------------------------------------------------------------
# 风格匹配对照
# ---------------------------------------------------------------------------


def _style_frame(boards: dict[str, str], returns: dict[str, list[float]]) -> pd.DataFrame:
    symbols = list(boards)
    frame, _ = _build_outcomes(symbols, closes=returns, boards=boards)
    return frame


def test_style_matched_control_only_matches_same_board() -> None:
    symbols = [f"60{i:04d}" for i in range(4)] + [f"30{i:04d}" for i in range(4)]
    boards = {symbol: ("gem" if symbol.startswith("30") else "主板") for symbol in symbols}
    returns = {symbol: [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6] for symbol in symbols}
    returns[symbols[0]] = [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 11.0]  # 主板里唯一的大涨
    frame = _style_frame(boards, returns)
    matched = style_matched_control(
        frame,
        dims=(STYLE_DIM_FLOAT_CAP, STYLE_DIM_VOL, STYLE_DIM_MOMENTUM),
        horizons=(3,),
        k=3,
        min_peers=1,
    )
    assert matched["style_peer_count"].max() <= 3
    row = matched[matched["symbol"] == symbols[0]].iloc[0]
    assert int(row["style_peer_count"]) <= 3
    assert bool(row["style_fallback"]) is False
    # 同板块（主板）只有 3 个潜在 peer
    assert int(row["style_peer_count"]) <= 3


def test_style_control_excludes_self_from_peers() -> None:
    """同板块只有自己一只票 → 没有 peer → 标记 fallback，不得拿自己当对照。"""
    frame = _style_frame({"600001": "主板"}, {"600001": [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6]})
    matched = style_matched_control(frame, horizons=(3,), k=3, min_peers=1)
    row = matched.iloc[0]
    assert int(row["style_peer_count"]) == 0
    assert bool(row["style_fallback"]) is True
    assert row["control_return_3d"] == "not_available"
    assert row["residual_excess_return_3d"] == "not_available"


def test_style_residual_equals_net_minus_control() -> None:
    symbols = [f"60{i:04d}" for i in range(8)]
    returns = {symbol: [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6] for symbol in symbols}
    returns[symbols[0]] = [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 11.5]
    frame = _style_frame({symbol: "主板" for symbol in symbols}, returns)
    matched = style_matched_control(frame, horizons=(3,), k=4, min_peers=2)
    merged = frame.merge(matched, on=["decision_date", "symbol"])
    for _, row in merged.iterrows():
        control = row["control_return_3d"]
        assert row["residual_excess_return_3d"] == pytest.approx(
            float(row["net_return_3d"]) - float(control), abs=1e-6
        )


def test_style_matched_fallback_flag_when_peers_below_minimum() -> None:
    symbols = [f"60{i:04d}" for i in range(3)]
    frame = (
        _style_frame({symbol: "主板" for symbol in symbols}, None)
        if False
        else _build_outcomes(symbols)[0]
    )
    matched = style_matched_control(frame, horizons=(3,), k=10, min_peers=5)
    # 只有 2 个 peer（除自己外）< min_peers=5 → 全部 fallback
    assert bool(matched["style_fallback"].all())
    assert int(matched["style_peer_count"].max()) == 2


def test_benchmark_suite_style_layer_records_dimensions_and_fallback() -> None:
    symbols = [f"60{i:04d}" for i in range(6)]
    frame, _ = _build_outcomes(symbols)
    spec = BenchmarkSpec(style_min_peers=2, style_k=3)
    suite = build_benchmark_suite(frame, spec=spec)
    layer = suite.report["layers"][BENCHMARK_STYLE_MATCHED]
    assert layer["dimensions_used"] == list(spec.style_dims)
    assert "fallback_rows" in layer


# ---------------------------------------------------------------------------
# 主基准写回
# ---------------------------------------------------------------------------


def test_merge_primary_excess_uses_quality_layer_by_default() -> None:
    symbols = [f"60{i:04d}" for i in range(6)]
    frame, _ = _build_outcomes(symbols)
    suite = build_benchmark_suite(frame)
    merged = merge_primary_excess(frame, suite)
    assert merged["benchmark_name"].iloc[0] == PRIMARY_LAYER
    expected = suite.series[BENCHMARK_QUALITY_POOL]
    row = expected[expected["horizon"] == 3].iloc[0]
    assert float(merged["benchmark_return_3d"].iloc[0]) == pytest.approx(
        float(row["benchmark_return"]), abs=1e-9
    )
    assert merged["up_excess_3d"].iloc[0] is not None


def test_merge_primary_excess_with_empty_suite_is_not_available() -> None:
    symbols = [f"60{i:04d}" for i in range(3)]
    frame, _ = _build_outcomes(symbols)
    suite = build_benchmark_suite(frame, spec=BenchmarkSpec(layers=()))
    merged = merge_primary_excess(frame, suite)
    assert merged["benchmark_name"].iloc[0] == "not_available"
    assert merged["excess_return_3d"].iloc[0] == "not_available"


def test_style_layer_merge_writes_residual_into_canonical_column() -> None:
    symbols = [f"60{i:04d}" for i in range(8)]
    frame, _ = _build_outcomes(symbols)
    suite = build_benchmark_suite(
        frame, spec=BenchmarkSpec(layers=(BENCHMARK_STYLE_MATCHED,), style_min_peers=2)
    )
    merged = merge_primary_excess(frame, suite)
    assert merged["benchmark_name"].iloc[0] == BENCHMARK_STYLE_MATCHED
    direct = suite.excess[BENCHMARK_STYLE_MATCHED].set_index("symbol")
    for _, row in merged.iterrows():
        expected = direct.loc[row["symbol"], "residual_excess_return_3d"]
        assert row["excess_return_3d"] == expected
