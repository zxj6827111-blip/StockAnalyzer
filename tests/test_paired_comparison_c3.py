"""C3 配对比较测试：配对增量、相关性稳健 CI、口径覆盖与"非独立证据"守卫。"""

from __future__ import annotations

import numpy as np
import pytest

from stock_analyzer.learning.paired_comparison import (
    VERDICT_INCONCLUSIVE,
    VERDICT_REJECTED,
    VERDICT_SUPPORTED,
    VariantSeries,
    compare_variants,
)

SEL = ("2024-01-01", "2024-06-30")
EVAL = ("2025-01-01", "2025-12-31")


def _series(
    name: str,
    values: list[float],
    *,
    month_start: int = 1,
    constant: set[str] | None = None,
) -> VariantSeries:
    days = [f"2025-{month_start + i // 20:02d}-{i % 20 + 1:02d}" for i in range(len(values))]
    return VariantSeries(
        name=name,
        daily_metric=list(zip(days, values, strict=True)),
        constant_days=frozenset(constant or ()),
    )


class TestPairedDelta:
    def test_identical_variant_has_zero_delta_and_no_evidence(self) -> None:
        base = [0.02, 0.03, -0.01] * 8
        result = compare_variants(
            reference=_series("ref", base),
            variants=[_series("clone", base)],
            selection_window=SEL,
            eval_window=EVAL,
        )[0]
        assert result.mean_delta == pytest.approx(0.0)
        assert result.ci_low == pytest.approx(0.0)
        assert result.ci_high == pytest.approx(0.0)
        assert result.verdict == VERDICT_INCONCLUSIVE  # 零宽 CI 不给"支持"
        assert result.paired_days == len(base)

    def test_constant_edge_is_supported(self) -> None:
        base = [0.02, 0.03, -0.01] * 8
        better = [value + 0.02 for value in base]
        result = compare_variants(
            reference=_series("ref", base),
            variants=[_series("better", better)],
            selection_window=SEL,
            eval_window=EVAL,
        )[0]
        assert result.mean_delta == pytest.approx(0.02)
        assert result.ci_low > 0.0
        assert result.verdict == VERDICT_SUPPORTED

    def test_worse_variant_is_rejected(self) -> None:
        base = [0.05, 0.04, 0.03] * 8
        worse = [value - 0.03 for value in base]
        result = compare_variants(
            reference=_series("ref", base),
            variants=[_series("worse", worse)],
            selection_window=SEL,
            eval_window=EVAL,
        )[0]
        assert result.ci_high < 0.0
        assert result.verdict == VERDICT_REJECTED

    def test_noisy_edge_is_inconclusive(self) -> None:
        # 差分序列均值略正但噪声大（块内自相关）→ CI 跨 0 → 证据不足。
        # 注意：若差分为**常数**（每天固定 +ε），零方差 CI 判 supported 是正确的
        # ——这里要测的是"均值略正但不可区分于 0"的形态，故差分必须带噪声。
        rng = np.random.default_rng(7)
        base = list(rng.normal(0.0, 0.05, 48))
        delta = rng.normal(0.002, 0.06, 48)
        variant = [value + noise for value, noise in zip(base, delta, strict=True)]
        result = compare_variants(
            reference=_series("ref", base),
            variants=[_series("marginal", variant)],
            selection_window=SEL,
            eval_window=EVAL,
        )[0]
        assert result.mean_delta > 0.0
        assert result.ci_low < 0.0 < result.ci_high
        assert result.verdict == VERDICT_INCONCLUSIVE


class TestCoverageDisclosure:
    def test_missing_days_excluded_and_counted(self) -> None:
        base = [0.01] * 30
        partial = [0.02] * 25  # 少 5 天
        result = compare_variants(
            reference=_series("ref", base),
            variants=[_series("partial", partial)],
            selection_window=SEL,
            eval_window=EVAL,
        )[0]
        # 只按交集配对：5 个缺测日不得当成 0 参与
        assert result.paired_days == 25
        assert result.missing_days == 5
        assert result.mean_delta == pytest.approx(0.01)

    def test_constant_days_excluded_and_counted(self) -> None:
        base = [0.01] * 20
        variant = [0.05] * 20
        constant_day = "2025-01-03"
        result = compare_variants(
            reference=_series("ref", base),
            variants=[_series("flat", variant, constant={constant_day})],
            selection_window=SEL,
            eval_window=EVAL,
        )[0]
        assert result.constant_days == 1
        assert result.paired_days == 19
        assert result.verdict_inputs["constant_days_excluded"] == 1

    def test_error_correlation_reported(self) -> None:
        base = [0.01, -0.02, 0.03, -0.01] * 6
        same_shape = [value * 3 for value in base]  # 完全相关
        result = compare_variants(
            reference=_series("ref", base),
            variants=[_series("scaled", same_shape)],
            selection_window=SEL,
            eval_window=EVAL,
        )[0]
        assert result.error_correlation == pytest.approx(1.0)

    def test_monthly_breakdown(self) -> None:
        base = [0.0] * 40
        variant = [0.01] * 20 + [0.03] * 20  # 跨两个月
        result = compare_variants(
            reference=_series("ref", base),
            variants=[_series("spread", variant)],
            selection_window=SEL,
            eval_window=EVAL,
        )[0]
        assert len(result.monthly_delta) == 2
        assert sorted(result.monthly_delta.values()) == [pytest.approx(0.01), pytest.approx(0.03)]


class TestNonIndependentEvidenceGuard:
    def test_overlapping_selection_window_forces_inconclusive(self) -> None:
        base = [0.01] * 30
        better = [0.05] * 30  # 增量极大且零方差 → 单看 CI 会判 support
        clean = compare_variants(
            reference=_series("ref", base),
            variants=[_series("better", better)],
            selection_window=SEL,
            eval_window=EVAL,
        )[0]
        assert clean.verdict == VERDICT_SUPPORTED

        overlapped = compare_variants(
            reference=_series("ref", base),
            variants=[_series("better", better)],
            selection_window=("2025-01-01", "2025-12-31"),  # 与评估窗完全重叠
            eval_window=EVAL,
        )[0]
        assert overlapped.verdict == VERDICT_INCONCLUSIVE
        assert overlapped.verdict_inputs["selection_overlaps_eval"] is True
        # 数值仍如实报告（不隐藏），但结论不得称"支持"
        assert overlapped.ci_low > 0.0
