"""C6 合并实验：每日秩标准化网格、端点同源配对、判定规则。

预注册在 ``docs/learning_chain_c6_merge_experiment_preregistration_20260915.md``：
权重网格、主判据、稳定性闸、噪声地板都在跑之前钉死。本文件锁住三条最容易悄悄错的
性质（改实现前先看这里）：

1. **秩标准化单调不变**——端点 w=1 的 IC 必须与原始分数的 IC 逐位相同。这条若破，
   "合并提升"就可能只是分数变换的假象。
2. **五个口径共用同一横截面**——否则配对不成立，差值里混进样本集差异。
3. **判定规则的方向**——主判据必须同时赢过两个端点且超过噪声地板，且三个权重符号一致。
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stock_analyzer.backtest.variants import (
    MERGE_MIN_CROSS_SECTION,
    MERGE_NOISE_FLOOR,
    MERGE_PRIMARY_WEIGHT,
    MERGE_WEIGHTS,
    merge_anchor_labels,
    merge_experiment_definition,
    merge_weight_label,
)
from stock_analyzer.backtest.walk_forward_xsec import (
    FoldResult,
    _daily_merge_diagnostics,
    _merge_report,
    _rank_pct,
)
from stock_analyzer.learning.scoring_eval import compute_rank_ic

# --- 定义锁定 ----------------------------------------------------------------


def test_merge_definition_is_locked() -> None:
    """权重网格/主判据/噪声地板都是预注册量，改这里必须同时改预注册文档。"""
    assert MERGE_WEIGHTS == (0.25, 0.5, 0.75)
    assert MERGE_PRIMARY_WEIGHT == 0.5
    assert MERGE_NOISE_FLOOR == pytest.approx(0.0019)
    assert merge_weight_label(0.5) == "w0.50"
    definition = merge_experiment_definition()
    assert definition["primary_weight"] == 0.5
    assert definition["rank"] == "uniform_index = (average_rank - 0.5) / n"


def test_rank_pct_is_monotone_invariant() -> None:
    values = np.array([3.0, -1.0, 7.5, 7.5, 0.0])
    uniform = _rank_pct(values)
    assert np.all(uniform > 0.0) and np.all(uniform <= 1.0)
    # 任何严格单调变换后秩分数不变（并列保持并列）
    shifted = _rank_pct(values * 1e6 + 42.0)
    assert np.allclose(uniform, shifted)
    reversed_rank = _rank_pct(-values)
    # average 秩满足 rank(x) + rank(-x) = n+1，故两个均匀秩分数之和恒为 1
    assert np.allclose(uniform + reversed_rank, 1.0)


# --- 诊断：端点同源 + 单调不变 ------------------------------------------------


def _labeled(seed: int = 3, days: int = 4, n: int = 40) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for index in range(days):
        day = f"2026-05-{index + 4:02d}"
        score = rng.normal(size=n)
        base = rng.normal(scale=0.05, size=n)
        forward = 0.004 * score - 0.6 * base + rng.normal(scale=0.01, size=n)
        for i in range(n):
            rows.append(
                {
                    "trade_date": day,
                    "symbol": f"{600000 + i:06d}",
                    "score": float(score[i]),
                    "ret_20d": float(base[i]),
                    "fwd_return": float(forward[i]),
                }
            )
    return pd.DataFrame(rows)


def test_endpoint_w1_matches_raw_score_ic_exactly() -> None:
    """w=1 的合并分数是原始分数的单调变换 → 每日 IC 必须逐位相同。"""
    frame = _labeled()
    series, used, excluded, skipped = _daily_merge_diagnostics(frame)
    anchors = merge_anchor_labels()
    assert skipped == 0
    assert excluded == 0
    assert used == len(frame)
    for day, raw_ic in series[anchors["model"]]:
        group = frame[frame["trade_date"] == day]
        expected = compute_rank_ic(
            group["score"].to_numpy(dtype=float), group["fwd_return"].to_numpy(dtype=float)
        )["ic_spearman"]
        assert math.isclose(raw_ic, expected, rel_tol=0.0, abs_tol=1e-12)


def test_reversal_anchor_equals_negated_past_return() -> None:
    frame = _labeled()
    series, *_ = _daily_merge_diagnostics(frame)
    anchors = merge_anchor_labels()
    day, value = series[anchors["reversal"]][0]
    group = frame[frame["trade_date"] == day]
    expected = compute_rank_ic(
        -group["ret_20d"].to_numpy(dtype=float), group["fwd_return"].to_numpy(dtype=float)
    )["ic_spearman"]
    assert math.isclose(value, expected, abs_tol=1e-12)


def test_cross_section_shrink_is_counted_not_silent() -> None:
    """ret_20d 缺失的行必须计入 rows_excluded——否则合并口径的横截面静默变小。"""
    frame = _labeled(seed=5)
    frame.loc[frame.index[:7], "ret_20d"] = np.nan
    series, used, excluded, skipped = _daily_merge_diagnostics(frame)
    assert excluded == 7
    assert used == len(frame) - 7
    assert skipped == 0
    # 五个口径共用同一横截面 → 日数完全相同
    assert len({len(values) for values in series.values()}) == 1


def test_small_cross_section_days_are_skipped() -> None:
    frame = _labeled(seed=9, days=2, n=MERGE_MIN_CROSS_SECTION - 1)
    series, used, excluded, skipped = _daily_merge_diagnostics(frame)
    assert skipped == 2
    assert used == 0
    assert all(not values for values in series.values())


# --- 判定规则 ----------------------------------------------------------------


def _fold(
    *,
    merged_gain: float,
    reversal_gain: float = 0.0,
    fold_id: int = 1,
    days: int = 60,
) -> FoldResult:
    """构造一个 daily_ic：merged(0.5) = 模型 + merged_gain = 反转 + reversal_gain。

    噪声用确定性序列（不是随机数），保证 CI 与判定可复现。
    """
    model = [(f"2026-06-{i % 28 + 1:02d}", 0.05 + 0.01 * math.sin(i)) for i in range(days)]
    reversal = [(day, value + reversal_gain) for day, value in model]
    weights = {
        merge_weight_label(w): [(day, value + merged_gain) for day, value in model]
        for w in MERGE_WEIGHTS
    }
    return FoldResult(
        fold_id=fold_id,
        train_start="2026-01-01",
        train_end="2026-05-01",
        eval_dates=[day for day, _ in model],
        status="completed",
        daily_ic=model,
        merge_daily_ic={
            **weights,
            merge_anchor_labels()["model"]: model,
            merge_anchor_labels()["reversal"]: reversal,
        },
        merge_rows_used=days,
    )


def test_merge_go_requires_winning_both_endpoints_above_noise_floor() -> None:
    report = _merge_report([_fold(merged_gain=0.02, reversal_gain=-0.01)])
    assert report["verdict"] == "MERGE_GO_CANDIDATE"
    assert report["grid_delta_signs_consistent"] is True
    assert report["invalid_reason"] == ""


def test_merge_inconclusive_when_gain_below_noise_floor() -> None:
    """小于噪声地板的增量不可解读为信号——这正是 0.0019 写成硬条件的原因。"""
    report = _merge_report([_fold(merged_gain=MERGE_NOISE_FLOOR / 2, reversal_gain=-0.01)])
    assert report["verdict"] == "MERGE_INCONCLUSIVE"


def test_merge_inconclusive_when_not_beating_reversal() -> None:
    """合并必须同时赢过两个端点；只赢模型不赢反转不算。"""
    report = _merge_report([_fold(merged_gain=0.02, reversal_gain=0.03)])
    assert report["verdict"] == "MERGE_INCONCLUSIVE"


def test_merge_no_go_on_negative_paired_ci() -> None:
    report = _merge_report([_fold(merged_gain=-0.02)])
    assert report["verdict"] == "MERGE_NO_GO"


def test_merge_invalid_when_fold_checkpoint_lost_merge_data() -> None:
    """从旧口径 checkpoint 恢复的 fold 会让日数静默变少 → 必须 fail-closed。"""
    good = _fold(merged_gain=0.02, reversal_gain=-0.01)
    stale = _fold(merged_gain=0.02, reversal_gain=-0.01, fold_id=2, days=10)
    stale.merge_daily_ic = {}
    report = _merge_report([good, stale])
    assert report["verdict"] == "INVALID"
    assert report["folds_without_merge_data"] == 1
    assert report["invalid_reason"].startswith("merge_partial")


def test_merge_grid_not_run_reports_reason() -> None:
    empty = FoldResult(fold_id=1, train_start="a", train_end="b", eval_dates=[], status="completed")
    report = _merge_report([empty])
    assert report["verdict"] == "INVALID"
    assert report["invalid_reason"] == "merge_grid_not_computed"


# --- 环境指纹与开关透传 --------------------------------------------------------


def test_environment_fingerprint_records_thread_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """线程环境必须进指纹。

    2026-09-15 实证：LightGBM 的 params 里没有 ``num_threads``，线程数只由
    ``OMP_NUM_THREADS`` 决定；一次没带线程变量的抛壳运行把 aggregate IC 从
    0.0612/0.0624/0.0634（三次）顶到 **0.0825**，比 0.0019 的复跑噪声地板大一个
    量级。没有指纹，这类漂移完全静默。
    """
    from stock_analyzer.backtest.walk_forward_xsec import _environment_fingerprint

    monkeypatch.setenv("OMP_NUM_THREADS", "8")
    monkeypatch.delenv("OPENBLAS_NUM_THREADS", raising=False)
    fingerprint = _environment_fingerprint()
    assert fingerprint["OMP_NUM_THREADS"] == "8"
    # 未设置的项要留空字符串（而不是缺键），否则"没设置"与"没记录"分不清
    assert fingerprint["OPENBLAS_NUM_THREADS"] == ""
    assert "lightgbm" in fingerprint and "xgboost" in fingerprint


def test_merge_grid_flag_is_forwarded_to_aggregate_report() -> None:
    """CLI 开关必须真的传到 aggregate_report。

    第一版漏了这一跳：fold 级合并数据算对了、checkpoint 也落了盘，但汇总层因为
    merge_grid 默认 False 报 NOT_RUN —— "跑了却没判定"比跑失败更坏（看起来像没做）。
    """
    from stock_analyzer.backtest import walk_forward_xsec as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "merge_grid=bool(args.merge_grid)" in source
