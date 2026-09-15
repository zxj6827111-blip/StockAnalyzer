"""C1 修正的对抗性测试：过程级判据必须真正进入 verdict。

覆盖：
1. C1 指定反例「IC=+0.01、CI=[-0.032,+0.052]、lookahead=1」不得判 GO_CANDIDATE，
   且 lookahead 违规与 CI 跨 0 必须是**两个独立的阻断原因**；
2. 强正信号 + CI 下界 > 0 仍须 GO_CANDIDATE（防止新门把好候选一起挡死）；
3. ``date_block_bootstrap_ci`` 为**连续交易日块**重采样（自相关下 CI 必须
   宽于逐日独立重采样），块长预设且可回传，同日重复值按均值合并且顺序无关；
4. fold 数由 ``plan_folds`` 生成，基线对照只在覆盖率一致时标可比。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from stock_analyzer.backtest.walk_forward_xsec import (
    BASELINE_SOUP_LABEL,
    VALIDATION_SCOPE_PROCESS,
    FoldResult,
    aggregate_report,
    baseline_comparison,
)
from stock_analyzer.learning.scoring_eval import (
    DEFAULT_BLOCK_TRADING_DAYS,
    date_block_bootstrap_ci,
)


def _fold(
    fold_id: int,
    *,
    daily_ic: list[float],
    daily_tb: list[float] | None = None,
    lookahead_violations: int = 0,
    month: int = 7,
) -> FoldResult:
    """构造 fold：分数与收益同向（IC>0 语义），日期按月错开避免跨 fold 撞日。"""

    n = max(1, len(daily_ic) * 10)
    rng = np.random.default_rng(fold_id)
    scores = rng.random(n)
    returns = scores * 0.05 - 0.01
    tb = daily_tb if daily_tb is not None else [0.02] * len(daily_ic)
    return FoldResult(
        fold_id=fold_id,
        train_start="2026-01-05",
        train_end="2026-06-30",
        eval_dates=[],
        status="completed",
        daily_ic=[(f"2026-{month:02d}-{i + 1:02d}", v) for i, v in enumerate(daily_ic)],
        daily_top_bottom=[(f"2026-{month:02d}-{i + 1:02d}", v) for i, v in enumerate(tb)],
        pooled_auc=0.6,
        pooled_brier=0.25,
        pooled_n=n,
        quantile_means=[-0.05, -0.02, 0.0, 0.02, 0.05],
        top_minus_bottom=0.10,
        lookahead_violations=lookahead_violations,
        eval_scores=scores,
        eval_returns=returns,
    )


def _report(folds: list[FoldResult]) -> dict[str, object]:
    return aggregate_report(
        folds=folds,  # type: ignore[arg-type]
        dataset_meta_rows=1000,
        train_window=120,
        test_window=20,
        step=20,
        embargo_days=11,
    )


class TestVerdictGatesEnterVerdict:
    """C1 反例：过程级信号（CI / lookahead）必须阻断 GO_CANDIDATE。"""

    def test_c1_counterexample_lookahead_blocks_go(self) -> None:
        # IC 均值 +0.01（正）、日间方差大 → CI 跨 0；同时 fold 内 1 处 lookahead 违规。
        # 修正前：folds/IC/tb/月度都过 → 误判 GO_CANDIDATE。
        folds = [
            _fold(i, daily_ic=[0.51, -0.49] * 5, lookahead_violations=1, month=7 + i)
            for i in range(1, 5)
        ]
        report = _report(folds)
        assert float(report["aggregate_ic_mean"]) == pytest.approx(0.01, abs=1e-9)
        assert report["verdict"] != "GO_CANDIDATE"
        assert report["verdict"] == "NO_GO"
        assert report["verdict_inputs"]["lookahead_gate_pass"] is False  # type: ignore[index]
        assert report["verdict_inputs"]["lookahead_violations_total"] == 4  # type: ignore[index]

    def test_c1_counterexample_ci_crossing_zero_blocks_go(self) -> None:
        # 同一反例但 lookahead 干净：CI 跨 0 ⇒ 证据不足 ⇒ INCONCLUSIVE（不得 GO）。
        folds = [
            _fold(i, daily_ic=[0.51, -0.49] * 5, lookahead_violations=0, month=7 + i)
            for i in range(1, 5)
        ]
        report = _report(folds)
        assert report["verdict"] == "INCONCLUSIVE"
        assert report["verdict_inputs"]["ci_supports_positive"] is False  # type: ignore[index]
        assert report["verdict_inputs"]["ci_excludes_zero"] is False  # type: ignore[index]
        # rule 串必须如实列出这条链（判定可审计，不必反推代码）。
        assert "INCONCLUSIVE" in str(report["verdict_rule"])

    def test_strong_positive_ci_low_above_zero_still_go(self) -> None:
        # 防过度拦截：CI 下界 > 0 的强正信号必须仍判 GO_CANDIDATE。
        folds = [
            _fold(i, daily_ic=[0.2, 0.3], daily_tb=[0.02, 0.04], month=7 + i)
            for i in range(1, 5)
        ]
        report = _report(folds)
        assert report["aggregate_ic_ci95"][0] > 0  # type: ignore[index]
        assert report["verdict"] == "GO_CANDIDATE"
        assert report["verdict_inputs"]["ci_supports_positive"] is True  # type: ignore[index]

    def test_lookahead_is_the_only_difference(self) -> None:
        clean = [
            _fold(i, daily_ic=[0.2, 0.3], daily_tb=[0.02, 0.04], month=7 + i)
            for i in range(1, 5)
        ]
        dirty = [
            _fold(
                i,
                daily_ic=[0.2, 0.3],
                daily_tb=[0.02, 0.04],
                lookahead_violations=1,
                month=7 + i,
            )
            for i in range(1, 5)
        ]
        assert _report(clean)["verdict"] == "GO_CANDIDATE"
        assert _report(dirty)["verdict"] == "NO_GO"

    def test_negative_ci_is_no_go(self) -> None:
        # 基线 soup 的形态：IC<0 且 CI 整体为负 → 有负向证据 → NO_GO（不再 INCONCLUSIVE）。
        folds = [
            _fold(i, daily_ic=[-0.3, -0.2], daily_tb=[-0.04, -0.02], month=7 + i)
            for i in range(1, 5)
        ]
        report = _report(folds)
        assert report["aggregate_ic_ci95"][1] < 0  # type: ignore[index]
        assert report["verdict"] == "NO_GO"
        assert report["verdict_inputs"]["ci_supports_negative"] is True  # type: ignore[index]

    def test_insufficient_folds_takes_precedence(self) -> None:
        report = _report([_fold(1, daily_ic=[0.2], lookahead_violations=3)])
        assert report["verdict"] == "INSUFFICIENT_FOLDS"
        assert report["fold_gate"]["min_required"] == 4  # type: ignore[index]


class TestDateBlockBootstrap:
    """连续块重采样语义（C1 修正点）。"""

    @staticmethod
    def _iid_ci(
        values: list[float], *, n_boot: int = 2000, seed: int = 20260905
    ) -> tuple[float, float]:
        """测试内自算的逐日独立重采样 CI（修正前的行为），用于对照宽度。"""

        arr = np.asarray(values, dtype=float)
        rng = np.random.default_rng(seed)
        draws = [arr[rng.integers(0, arr.shape[0], arr.shape[0])].mean() for _ in range(n_boot)]
        means = np.asarray(draws, dtype=float)
        return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))

    def test_block_ci_wider_than_iid_under_autocorrelation(self) -> None:
        # 两段常数块（各 10 日）：块内无方差、块间方差大 ⇒ 有效样本量远小于 20，
        # 连续块重采样的 CI 必须显著宽于逐日独立重采样。
        values = [0.5] * 10 + [-0.3] * 10
        daily = [(date(2026, 6, i + 1), v) for i, v in enumerate(values)]
        ci = date_block_bootstrap_ci(daily, n_boot=2000, seed=11)
        block_width = float(ci["ci_high"]) - float(ci["ci_low"])
        iid_low, iid_high = self._iid_ci(values)
        iid_width = iid_high - iid_low
        assert ci["method"] == "moving_block"
        assert ci["block_days"] == DEFAULT_BLOCK_TRADING_DAYS
        assert block_width > iid_width * 1.5, (block_width, iid_width)
        assert float(ci["ci_low"]) <= 0.1 <= float(ci["ci_high"])

    def test_block_length_is_prespecified_and_echoed(self) -> None:
        values = [0.5] * 10 + [-0.3] * 10
        daily = [(date(2026, 6, i + 1), v) for i, v in enumerate(values)]
        # 块长 = 全样本长度 ⇒ 每个 bootstrap 样本都等于全序列 ⇒ 零宽 CI。
        ci = date_block_bootstrap_ci(daily, n_boot=200, seed=5, block_days=len(values))
        assert ci["block_days"] == len(values)
        assert ci["n_blocks"] == 1
        assert abs(float(ci["ci_high"]) - float(ci["ci_low"])) < 1e-12
        # 预设块长参与计算：不同块长给出不同 CI（不是摆设参数）。
        narrow = date_block_bootstrap_ci(daily, n_boot=500, seed=5, block_days=1)
        assert abs(float(narrow["ci_high"]) - float(narrow["ci_low"])) > 0.05

    def test_duplicate_days_counted_not_collapsed_and_order_independent(self) -> None:
        # 同一交易日多值**不合并**（合并会静默削减有效样本量，极端情形让 CI
        # 退化成零宽＝假显著），只做计数留痕；且与输入顺序无关。
        scattered = [
            (date(2026, 6, 2), 0.3),
            (date(2026, 6, 1), 0.1),
            (date(2026, 6, 2), 0.5),
            (date(2026, 6, 3), -0.2),
        ]
        reordered = [scattered[1], scattered[2], scattered[0], scattered[3]]
        first = date_block_bootstrap_ci(scattered, n_boot=300, seed=3)
        second = date_block_bootstrap_ci(reordered, n_boot=300, seed=3)
        assert reordered != scattered  # 顺序确实不同
        assert first["valid_days"] == 4  # 全部观测保留
        assert first["distinct_days"] == 3
        assert first["duplicate_days"] == 1
        assert first["ci_low"] == second["ci_low"]
        assert first["ci_high"] == second["ci_high"]

    def test_duplicate_days_do_not_degenerate_ci_to_zero_width(self) -> None:
        # 回归护栏：即便同月同日重复，也不得出现"块覆盖全样本 → 零宽 CI"的假显著。
        daily = [
            (f"2026-07-{i + 1:02d}", v)
            for _ in range(4)
            for i, v in enumerate([0.9, -0.6, 0.8, -0.7])
        ]
        ci = date_block_bootstrap_ci(daily, n_boot=2000, seed=20260905)
        assert ci["valid_days"] == 16
        assert ci["n_blocks"] == 4
        assert float(ci["ci_high"]) - float(ci["ci_low"]) > 0.1

    def test_nan_dropped_and_single_day_nan(self) -> None:
        ci = date_block_bootstrap_ci([(date(2026, 6, 1), 0.1), (date(2026, 6, 2), float("nan"))])
        assert np.isnan(float(ci["ci_low"]))
        assert ci["valid_days"] == 1

    def test_deterministic_with_seed(self) -> None:
        daily = [(date(2026, 6, i + 1), 0.01 * (i - 5)) for i in range(10)]
        assert date_block_bootstrap_ci(daily, n_boot=200, seed=42) == date_block_bootstrap_ci(
            daily, n_boot=200, seed=42
        )


class TestBaselineComparability:
    """fold 数由 plan_folds 生成；基线只在覆盖率一致时可对照。"""

    def test_folds_total_is_generated_from_input(self) -> None:
        folds = [_fold(i, daily_ic=[0.2], month=7 + i) for i in range(1, 7)]
        report = _report(folds)
        assert report["folds_total"] == 6
        assert report["folds_completed"] == 6

    def test_baseline_comparable_only_on_equal_fold_count(self) -> None:
        same = baseline_comparison(int(BASELINE_SOUP_LABEL["folds_total"]))  # type: ignore[arg-type]
        assert same["comparable"] is True
        assert same["current_folds_total"] == 18

        different = baseline_comparison(12)
        assert different["comparable"] is False
        assert different["current_folds_total"] == 12
        note = str(different["comparability_note"])
        assert "12" in note and "18" in note
        # 基线自身的历史数字仍如实保留（写死的只是基线，不是"期望 fold 数"）。
        assert different["folds_total"] == 18


class TestCoverageGateAndScope:
    """覆盖门（结构性）+ 验证范围声明（C1 §2/§3）。"""

    def test_single_block_is_inconclusive_not_go(self) -> None:
        # 有效观测 < 2×块长 ⇒ 块数 = 1 ⇒ 每轮重采样抽到同一块、CI 零宽（假显著），
        # 此时即使 IC 全正也不得判 GO。
        folds = [_fold(i, daily_ic=[0.4], month=7 + i) for i in range(1, 5)]
        report = _report(folds)
        assert report["coverage_gate"]["blocks"] == 1  # type: ignore[index]
        assert report["coverage_gate"]["ok"] is False  # type: ignore[index]
        assert report["verdict_inputs"]["coverage_gate_pass"] is False  # type: ignore[index]
        assert report["verdict"] == "INCONCLUSIVE"

    def test_two_blocks_is_enough_for_go(self) -> None:
        folds = [_fold(i, daily_ic=[0.4, 0.45], month=7 + i) for i in range(1, 5)]
        report = _report(folds)
        assert report["coverage_gate"]["blocks"] >= 2  # type: ignore[index]
        assert report["verdict"] == "GO_CANDIDATE"

    def test_report_declares_process_level_scope(self) -> None:
        assert VALIDATION_SCOPE_PROCESS["kind"] == "process"
        assert "工件" in str(VALIDATION_SCOPE_PROCESS["artifact_level_required"])
        folds = [_fold(i, daily_ic=[0.2, 0.3], month=7 + i) for i in range(1, 5)]
        assert _report(folds)["validation_scope"] == "process"
