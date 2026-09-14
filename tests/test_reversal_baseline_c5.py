"""C5 固定反转基线与成本口径测试。"""

from __future__ import annotations

import numpy as np
import pytest

from stock_analyzer.learning.reversal_baseline import (
    DEFAULT_COST_BPS,
    net_returns,
    reversal_scores,
    summarize_baseline,
    top_quantile_weights,
    turnover,
)


class TestReversalRuleIsPrespecified:
    def test_score_is_negative_past_return(self) -> None:
        # 预先固定：涨得多的得低分、跌得多的得高分（无自由度、无超参）
        scores = reversal_scores({"600000": 0.10, "000001": -0.05, "300001": 0.0})
        assert scores["000001"] == pytest.approx(0.05)
        assert scores["600000"] == pytest.approx(-0.10)
        assert scores["300001"] == pytest.approx(0.0)
        assert max(scores, key=scores.__getitem__) == "000001"

    def test_blank_symbols_and_nan_dropped(self) -> None:
        scores = reversal_scores({"": 0.1, "600000": float("nan"), "000001": 0.02})
        assert scores == {"000001": pytest.approx(-0.02)}

    def test_top_quantile_weights_equal_weight_and_normalized(self) -> None:
        scores = {f"s{i}": float(i) for i in range(10)}
        weights = top_quantile_weights(scores, top_quantile=0.3)
        assert len(weights) == 3
        assert sum(weights.values()) == pytest.approx(1.0)
        assert all(value == pytest.approx(1 / 3) for value in weights.values())
        assert set(weights) == {"s9", "s8", "s7"}  # 分数最高的三个

    def test_empty_cross_section_yields_empty_portfolio(self) -> None:
        assert top_quantile_weights({}) == {}
        with pytest.raises(ValueError):
            top_quantile_weights({"a": 1.0}, top_quantile=0.0)


class TestCostAndTurnover:
    def test_turnover_zero_when_portfolio_unchanged(self) -> None:
        weights = {"a": 0.5, "b": 0.5}
        assert turnover(weights, dict(weights)) == pytest.approx(0.0)

    def test_turnover_one_on_full_switch(self) -> None:
        assert turnover({"a": 1.0}, {"b": 1.0}) == pytest.approx(1.0)

    def test_zero_cost_leaves_returns_unchanged(self) -> None:
        gross = [0.01, -0.02, 0.03]
        assert net_returns(gross, [0.5, 0.5, 0.5], cost_bps=0.0) == pytest.approx(gross)

    def test_cost_deducts_turnover_monotonically(self) -> None:
        gross = [0.01, 0.01, 0.01]
        turns = [1.0, 1.0, 1.0]
        cheap = net_returns(gross, turns, cost_bps=5.0)
        dear = net_returns(gross, turns, cost_bps=20.0)
        assert cheap[0] > dear[0]
        assert dear[0] == pytest.approx(0.01 - 1.0 * 20.0 / 10_000.0)

    def test_length_mismatch_refuses(self) -> None:
        with pytest.raises(ValueError):
            net_returns([0.01], [0.5, 0.5])


class TestBaselineSummary:
    def test_failure_months_listed_not_removed(self) -> None:
        daily = [
            ("2026-01-05", 0.01),
            ("2026-01-06", 0.02),
            ("2026-02-03", -0.01),
            ("2026-02-04", -0.03),
            ("2026-03-02", 0.005),
        ]
        summary = summarize_baseline(daily, cost_bps=DEFAULT_COST_BPS, avg_turnover=0.4)
        assert summary["days"] == 5
        assert summary["failure_months"] == ["2026-02"]
        assert summary["failure_month_count"] == 1
        assert summary["monthly_mean"]["2026-01"] == pytest.approx(0.015)
        assert summary["monthly_mean"]["2026-03"] == pytest.approx(0.005)
        assert summary["cost_bps"] == DEFAULT_COST_BPS
        assert summary["avg_turnover"] == pytest.approx(0.4)

    def test_empty_series_reports_nan_not_zero(self) -> None:
        summary = summarize_baseline([])
        assert summary["days"] == 0
        assert np.isnan(float(summary["mean_net"]))  # 不得谎报 0 收益
        assert summary["failure_months"] == []
