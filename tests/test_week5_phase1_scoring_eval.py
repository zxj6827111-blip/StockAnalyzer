"""Phase 1 打分评估模块测试（scoring_eval）。

覆盖：时间安全资格判定（交易日 embargo 算术、缺 outcome 判 invalid、
日历耗尽判 invalid）、IC/分位收益/AUC/Precision@K 的构造性用例、
date-block bootstrap 确定性、lookahead 计算值。
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pytest

from stock_analyzer.learning.scoring_eval import (
    ManifestEligibility,
    check_lookahead,
    compute_auc_brier,
    compute_precision_at_k,
    compute_quantile_returns,
    compute_rank_ic,
    date_block_bootstrap_ci,
    resolve_manifest_eligibility,
)

# 2026-06 的真实交易日（周末剔除）：6/1(Mon)~6/5, 6/8~6/12, 6/15~6/19
TRADING_DATES = [
    date(2026, 6, 1),
    date(2026, 6, 2),
    date(2026, 6, 3),
    date(2026, 6, 4),
    date(2026, 6, 5),
    date(2026, 6, 8),
    date(2026, 6, 9),
    date(2026, 6, 10),
    date(2026, 6, 11),
    date(2026, 6, 12),
    date(2026, 6, 15),
    date(2026, 6, 16),
    date(2026, 6, 17),
    date(2026, 6, 18),
    date(2026, 6, 19),
]


def _eligibility(
    *,
    mature: str,
    decisions: list[str] | None = None,
    embargo: int = 3,
    missing: int = 0,
) -> ManifestEligibility:
    return resolve_manifest_eligibility(
        dataset_manifest_id="m_test",
        item_decision_times=decisions or ["2026-05-20T14:55:00+00:00"],
        item_label_mature_times=[mature],
        missing_outcome_items=missing,
        embargo_trading_days=embargo,
        trading_dates=TRADING_DATES,
    )


class TestResolveManifestEligibility:
    def test_embargo_counts_trading_days_after_maturity(self) -> None:
        # 成熟截止 6/5（周五）→ 之后第一个交易日 6/8 → +3 个交易日 = 6/11
        result = _eligibility(mature="2026-06-05T15:00:00+00:00", embargo=3)
        assert result.is_eligible
        assert result.earliest_valid_eval_date == date(2026, 6, 11)
        assert result.training_cutoff == datetime.fromisoformat("2026-05-20T14:55:00+00:00")

    def test_maturity_on_non_trading_day_skips_weekend(self) -> None:
        # 6/6 是周六 → 第一个交易日 6/8 → +0 = 6/8
        result = _eligibility(mature="2026-06-06T15:00:00+00:00", embargo=0)
        assert result.earliest_valid_eval_date == date(2026, 6, 8)

    def test_embargo_beyond_calendar_yields_invalid(self) -> None:
        result = _eligibility(mature="2026-06-19T15:00:00+00:00", embargo=5)
        assert not result.is_eligible
        assert result.invalid_reason == "no_trading_day_after_embargo"
        assert result.earliest_valid_eval_date is None

    def test_missing_outcomes_blocks_eligibility(self) -> None:
        result = _eligibility(mature="2026-06-05T15:00:00+00:00", missing=7)
        assert not result.is_eligible
        assert result.invalid_reason == "outcome_missing_items:7"

    def test_empty_manifest_is_invalid(self) -> None:
        result = resolve_manifest_eligibility(
            dataset_manifest_id="m_empty",
            item_decision_times=[],
            item_label_mature_times=[],
            missing_outcome_items=0,
            embargo_trading_days=1,
            trading_dates=TRADING_DATES,
        )
        assert not result.is_eligible
        assert result.invalid_reason == "empty_manifest"


class TestComputeRankIc:
    def test_perfect_ordering_gives_ic_one(self) -> None:
        scores = np.array([1.0, 2.0, 3.0, 4.0])
        returns = np.array([0.01, 0.02, 0.03, 0.04])
        result = compute_rank_ic(scores, returns)
        assert result["ic_spearman"] == pytest.approx(1.0)
        assert result["ic_pearson"] == pytest.approx(1.0)

    def test_inverse_ordering_gives_ic_minus_one(self) -> None:
        result = compute_rank_ic(np.array([4.0, 3.0, 2.0, 1.0]), np.array([0.01, 0.02, 0.03, 0.04]))
        assert result["ic_spearman"] == pytest.approx(-1.0)

    def test_ties_use_average_rank(self) -> None:
        # [1,1,2] 平均秩 (1.5,1.5,3)；与收益秩 (1,2,3) 的相关 <1（并列使
        # 秩对不再是严格线性关系），Spearman=√3/2≈0.866。
        result = compute_rank_ic(np.array([1.0, 1.0, 2.0]), np.array([0.01, 0.02, 0.03]))
        assert result["ic_spearman"] == pytest.approx(0.8660, abs=1e-3)

    def test_single_sample_is_nan(self) -> None:
        result = compute_rank_ic(np.array([1.0]), np.array([0.01]))
        assert np.isnan(result["ic_spearman"])
        assert result["n"] == 1

    def test_nan_rows_are_dropped(self) -> None:
        result = compute_rank_ic(
            np.array([1.0, np.nan, 3.0]), np.array([0.01, 0.02, 0.03])
        )
        assert result["n"] == 2
        assert result["ic_spearman"] == pytest.approx(1.0)


class TestQuantileReturns:
    def test_perfectly_ordered_spread_positive_and_monotonic(self) -> None:
        scores = np.arange(20, dtype=float)
        returns = np.linspace(0.0, 0.19, 20)
        result = compute_quantile_returns(scores, returns, n_quantiles=4)
        assert result["n"] == 20
        # 等分 4 桶（每桶 5 个）：bottom 均值 0.02、top 均值 0.17 → 价差 0.15
        assert result["top_minus_bottom"] == pytest.approx(0.15, abs=1e-9)
        assert result["monotonic_top_ge_bottom"] is True

    def test_insufficient_samples_returns_nan_spread(self) -> None:
        result = compute_quantile_returns(np.array([1.0, 2.0]), np.array([0.1, 0.2]), n_quantiles=5)
        assert result["n"] == 2
        assert np.isnan(result["top_minus_bottom"])
        assert result["monotonic_top_ge_bottom"] is False


class TestAucBrier:
    def test_separable_probabilities_auc_one(self) -> None:
        result = compute_auc_brier(np.array([0.9, 0.8, 0.2, 0.1]), np.array([1.0, 1.0, 0.0, 0.0]))
        assert result["auc"] == pytest.approx(1.0)

    def test_single_class_auc_nan(self) -> None:
        result = compute_auc_brier(np.array([0.9, 0.1]), np.array([1.0, 1.0]))
        assert np.isnan(result["auc"])

    def test_brier_of_perfect_prediction(self) -> None:
        result = compute_auc_brier(np.array([1.0, 0.0]), np.array([1.0, 0.0]))
        assert result["brier"] == pytest.approx(0.0)


class TestPrecisionAtK:
    def test_top_k_counts_hits(self) -> None:
        result = compute_precision_at_k(
            np.array([0.9, 0.8, 0.1, 0.2]), np.array([1.0, 0.0, 1.0, 0.0]), k=2
        )
        assert result["precision_at_k"] == pytest.approx(0.5)
        assert result["k"] == 2

    def test_k_capped_to_sample_count(self) -> None:
        result = compute_precision_at_k(np.array([0.5]), np.array([1.0]), k=10)
        assert result["k"] == 1
        assert result["precision_at_k"] == pytest.approx(1.0)


class TestDateBlockBootstrap:
    def test_deterministic_with_seed(self) -> None:
        daily = [(date(2026, 6, i), 0.01 * i) for i in range(1, 11)]
        first = date_block_bootstrap_ci(daily, n_boot=200, seed=42)
        second = date_block_bootstrap_ci(daily, n_boot=200, seed=42)
        assert first == second

    def test_strong_signal_ci_excludes_zero(self) -> None:
        daily = [(date(2026, 6, i), 0.10) for i in range(1, 21)]
        ci = date_block_bootstrap_ci(daily, n_boot=500, seed=7)
        assert ci["ci_low"] > 0.0

    def test_single_day_returns_nan(self) -> None:
        ci = date_block_bootstrap_ci([(date(2026, 6, 1), 0.1)])
        assert np.isnan(ci["ci_low"])
        assert ci["valid_days"] == 1


class TestCheckLookahead:
    def test_clean_case(self) -> None:
        eligibility = _eligibility(mature="2026-06-05T15:00:00+00:00", embargo=2)
        result = check_lookahead(
            eligibility=eligibility,
            eval_date=date(2026, 6, 10),
            snapshot_decision_dates=[date(2026, 6, 10)],
        )
        assert result["lookahead_bias"] is False
        assert result["embargo_trading_days"] == 2

    def test_eval_before_earliest_valid_flags(self) -> None:
        eligibility = _eligibility(mature="2026-06-05T15:00:00+00:00", embargo=3)
        result = check_lookahead(
            eligibility=eligibility,
            eval_date=date(2026, 6, 9),
            snapshot_decision_dates=[date(2026, 6, 9)],
        )
        assert result["lookahead_bias"] is True
        assert any(r.startswith("eval_before_earliest_valid") for r in result["lookahead_reasons"])

    def test_future_snapshot_flags(self) -> None:
        eligibility = _eligibility(mature="2026-06-05T15:00:00+00:00", embargo=1)
        result = check_lookahead(
            eligibility=eligibility,
            eval_date=date(2026, 6, 9),
            snapshot_decision_dates=[date(2026, 6, 10)],
        )
        assert result["lookahead_bias"] is True
        assert any(r.startswith("snapshot_after_eval") for r in result["lookahead_reasons"])
