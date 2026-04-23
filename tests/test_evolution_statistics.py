from __future__ import annotations

import math

import pytest

from stock_analyzer.evolution.core.statistics import bootstrap_test, fdr_correct, information_ratio


def test_bootstrap_test_uses_adaptive_block_size() -> None:
    sample = [0.01] * 100
    result = bootstrap_test(sample, n_resamples=200)
    assert result.block_size == 10
    assert 0.0 <= result.p_value <= 1.0
    assert result.n_samples == 100


def test_bootstrap_test_detects_positive_mean_signal() -> None:
    sample = [0.01, 0.012, 0.009, 0.011, 0.013] * 20
    result = bootstrap_test(sample, n_resamples=500, block_size=5)
    assert result.observed_mean > 0
    assert result.p_value < 0.2


def test_fdr_correct_bh_returns_expected_adjusted_values() -> None:
    result = fdr_correct([0.01, 0.04, 0.03, 0.2], method="bh")
    assert result.adjusted_p_values == pytest.approx([0.04, 0.0533333333, 0.0533333333, 0.2])
    assert result.rejected == [True, True, True, False]


def test_information_ratio_matches_formula() -> None:
    challenger = [0.03, 0.02, 0.01]
    champion = [0.01, 0.01, 0.01]
    ir = information_ratio(challenger, champion)

    excess = [0.02, 0.01, 0.0]
    expected = (
        (sum(excess) / len(excess))
        / max(
            math.sqrt(
                sum((value - (sum(excess) / len(excess))) ** 2 for value in excess) / len(excess)
            ),
            1e-6,
        )
        * math.sqrt(252)
    )
    assert ir == pytest.approx(expected)
