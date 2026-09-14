"""C3 同口径配对比较（同一窗口 / 同一资格集 / 同一评估脚本）。

方案要求（v2 §C3）：
- 在同一窗口、同一资格集、同一评估脚本下**配对**比较各变体，报告**配对 OOS 增量**
  而不是并列指标；
- 报告错误相关性、分月稳定性、缺失与常数日覆盖；
- 特征数由 fold 内筛选决定，不预设；
- **不得**用全样本选择后的 IC 当独立证据。

本模块只做"配对统计"这一层：调用方负责在同一 fold/窗口/资格集上跑出各变体的
逐日指标，本模块把它们配对起来给出增量与**相关性稳健 CI**（复用 C1 的连续块
moving-block bootstrap），并把口径问题（缺测日、常数日、选择窗与评估窗重叠）
显式暴露出来而不是静默吞掉。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from stock_analyzer.learning.scoring_eval import (
    DEFAULT_BLOCK_TRADING_DAYS,
    date_block_bootstrap_ci,
)

# 配对增量的判定门槛：CI 下界 > 0 才算"有正向证据"，CI 上界 < 0 才算"有负向证据"，
# 跨 0 一律"证据不足"——与 C1 的 verdict 纪律一致（不用点估计当证据）。
VERDICT_SUPPORTED = "supported"
VERDICT_REJECTED = "rejected"
VERDICT_INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class VariantSeries:
    """一个变体在同一评估窗口内的逐日指标。

    ``constant_days``：该日模型输出为常数（无横截面区分度）→ 该日的 IC 无信息，
    默认从配对中剔除并计数（不是当 0 参与平均）。
    """

    name: str
    daily_metric: list[tuple[str, float]]
    constant_days: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class PairedDelta:
    reference: str
    variant: str
    mean_delta: float
    ci_low: float
    ci_high: float
    ci_block_days: int
    paired_days: int
    missing_days: int
    constant_days: int
    error_correlation: float
    monthly_delta: dict[str, float]
    verdict: str
    verdict_inputs: dict[str, object]


def _index(series: VariantSeries) -> dict[str, float]:
    out: dict[str, float] = {}
    for day, value in series.daily_metric:
        key = str(day)
        numeric = float(value)
        if math.isnan(numeric):
            continue
        out[key] = numeric
    return out


def _monthly(deltas: Mapping[str, float]) -> dict[str, float]:
    grouped: dict[str, list[float]] = {}
    for day, value in deltas.items():
        grouped.setdefault(day[:7], []).append(value)
    return {month: float(np.mean(values)) for month, values in sorted(grouped.items())}


def _correlation(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) < 2:
        return float("nan")
    x = np.asarray(left, dtype=float)
    y = np.asarray(right, dtype=float)
    if float(x.std()) == 0.0 or float(y.std()) == 0.0:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def compare_variants(
    *,
    reference: VariantSeries,
    variants: Sequence[VariantSeries],
    selection_window: tuple[str, str],
    eval_window: tuple[str, str],
    n_boot: int = 2000,
    seed: int = 20260914,
    confidence: float = 0.95,
    block_days: int = DEFAULT_BLOCK_TRADING_DAYS,
) -> list[PairedDelta]:
    """把各变体与参考配对，给出增量、相关性稳健 CI 与口径覆盖明细。

    ``selection_window`` / ``eval_window``：变体的**选择**所用窗口与**评估**窗口。
    两者重叠即意味着评估集已被用于选择 → 该结果不得作为独立证据，verdict 强制为
    ``inconclusive``（并把原因写进 verdict_inputs），这是"不得用全样本选择后的 IC
    当独立证据"的可执行形式。
    """

    ref_index = _index(reference)
    ref_constant = set(reference.constant_days)
    sel_start, sel_end = str(selection_window[0]), str(selection_window[1])
    ev_start, ev_end = str(eval_window[0]), str(eval_window[1])
    overlaps = not (sel_end < ev_start or sel_start > ev_end)

    results: list[PairedDelta] = []
    for variant in variants:
        var_index = _index(variant)
        var_constant = set(variant.constant_days)
        common = sorted(set(ref_index) & set(var_index))
        missing = len(set(ref_index) - set(var_index))
        # 常数日：任一侧当日无区分度即剔除（保留会让增量失真为 0 或噪声）
        usable = [day for day in common if day not in ref_constant and day not in var_constant]
        excluded_constant = len([d for d in common if d in ref_constant or d in var_constant])
        deltas = {day: var_index[day] - ref_index[day] for day in usable}
        ci = date_block_bootstrap_ci(
            [(day, value) for day, value in deltas.items()],
            n_boot=n_boot,
            seed=seed,
            confidence=confidence,
            block_days=block_days,
        )
        ci_low = float(ci["ci_low"])
        ci_high = float(ci["ci_high"])
        mean_delta = float(np.mean(list(deltas.values()))) if deltas else float("nan")
        if not deltas or int(ci["n_blocks"]) < 2:
            verdict = VERDICT_INCONCLUSIVE
        elif overlaps:
            # 选择窗与评估窗重叠 → 不是独立证据（无论 CI 多漂亮）
            verdict = VERDICT_INCONCLUSIVE
        elif ci_low > 0.0:
            verdict = VERDICT_SUPPORTED
        elif ci_high < 0.0:
            verdict = VERDICT_REJECTED
        else:
            verdict = VERDICT_INCONCLUSIVE
        results.append(
            PairedDelta(
                reference=reference.name,
                variant=variant.name,
                mean_delta=mean_delta,
                ci_low=ci_low,
                ci_high=ci_high,
                ci_block_days=int(ci["block_days"]),
                paired_days=len(deltas),
                missing_days=missing,
                constant_days=excluded_constant,
                error_correlation=_correlation(
                    [ref_index[d] for d in usable], [var_index[d] for d in usable]
                ),
                monthly_delta=_monthly(deltas),
                verdict=verdict,
                verdict_inputs={
                    "selection_overlaps_eval": overlaps,
                    "selection_window": [sel_start, sel_end],
                    "eval_window": [ev_start, ev_end],
                    "ci_supports_positive": ci_low > 0.0,
                    "ci_supports_negative": ci_high < 0.0,
                    "paired_days": len(deltas),
                    "missing_days": missing,
                    "constant_days_excluded": excluded_constant,
                    "blocks": int(ci["n_blocks"]),
                },
            )
        )
    return results
