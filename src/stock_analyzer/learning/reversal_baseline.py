"""C5 固定反转基线 + 成本/换手口径（并入 C3 的配对比较）。

方案 §C5 要求：把反转当作**基线**纳入 C3 的配对比较；**预先固定**单因子/组合规则
（不在折内拟合、不按结果挑参数）；报告成本、换手、风险暴露与失效月份。并明确
"当前全样本 t≈−4.5 是诊断证据，不能直接当独立显著性证据"——独立显著性只能来自
C3 的配对增量 CI（本模块提供的是**基线分数与成本口径**，判定仍走 C3）。

设计约束：
- 规则**预设定**：``reversal_scores`` 只是过去收益取负，无自由度、无超参；
- 成本口径**显式**：单边成本基点由调用方给定（默认常量），成本后收益 =
  毛收益 − 换手 × 单边成本；
- 失效月份**如实统计**：月均值为负的月份逐个列出，不做"剔除异常月"的处理。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np

# 预设定的单边交易成本（基点）。与执行层实际口径对齐后应显式回填；此处作为
# 基线报告的默认值，任何改动都要在报告里可追溯（不做"按结果调成本"）。
DEFAULT_COST_BPS = 10.0


def reversal_scores(past_returns: Mapping[str, float]) -> dict[str, float]:
    """预先固定的单因子反转基线：分数 = −过去区间收益。

    同一横截面内的符号可比较（数值更大的分数表示"过去跌得更多"）。
    """

    out: dict[str, float] = {}
    for symbol, value in past_returns.items():
        key = str(symbol).strip()
        if not key:
            continue
        numeric = float(value)
        if np.isnan(numeric):
            continue
        out[key] = -numeric
    return out


def top_quantile_weights(
    scores: Mapping[str, float], *, top_quantile: float = 0.3
) -> dict[str, float]:
    """按分数取上尾 ``top_quantile`` 的标的，等权成组合（无自由度）。"""

    if not 0.0 < float(top_quantile) <= 1.0:
        raise ValueError("top_quantile must be in (0, 1]")
    usable = {k: float(v) for k, v in scores.items() if not np.isnan(float(v))}
    if not usable:
        return {}
    ranked = sorted(usable.items(), key=lambda item: (-item[1], item[0]))
    keep = max(1, int(np.ceil(len(ranked) * float(top_quantile))))
    selected = ranked[:keep]
    weight = 1.0 / len(selected)
    return {symbol: weight for symbol, _ in selected}


def turnover(previous: Mapping[str, float], current: Mapping[str, float]) -> float:
    """单边换手 = 0.5 × Σ|w_new − w_old|（两组合权重各自和为 1）。"""

    keys = set(previous) | set(current)
    total = 0.0
    for key in keys:
        total += abs(float(current.get(key, 0.0)) - float(previous.get(key, 0.0)))
    return 0.5 * total


def net_returns(
    gross_returns: Sequence[float],
    turnovers: Sequence[float],
    *,
    cost_bps: float = DEFAULT_COST_BPS,
) -> list[float]:
    """成本后逐期收益 = 毛收益 − 换手 × 单边成本。"""

    if len(gross_returns) != len(turnovers):
        raise ValueError("gross_returns and turnovers must have the same length")
    rate = float(cost_bps) / 10_000.0
    return [
        float(gross) - float(turn) * rate
        for gross, turn in zip(gross_returns, turnovers, strict=True)
    ]


def summarize_baseline(
    daily_net: Sequence[tuple[str, float]],
    *,
    cost_bps: float = DEFAULT_COST_BPS,
    avg_turnover: float = float("nan"),
) -> dict[str, object]:
    """基线的成本后概览：均值、失效月份、覆盖天数（如实列出，不做剔除）。"""

    values = [(str(day), float(value)) for day, value in daily_net]
    if not values:
        return {
            "days": 0,
            "mean_net": float("nan"),
            "failure_months": [],
            "monthly_mean": {},
            "cost_bps": float(cost_bps),
            "avg_turnover": float(avg_turnover),
        }
    monthly: dict[str, list[float]] = {}
    for day, value in values:
        monthly.setdefault(day[:7], []).append(value)
    monthly_mean = {month: float(np.mean(vals)) for month, vals in sorted(monthly.items())}
    failure_months = sorted(month for month, mean in monthly_mean.items() if mean < 0.0)
    return {
        "days": len(values),
        "mean_net": float(np.mean([value for _, value in values])),
        "monthly_mean": monthly_mean,
        "failure_months": failure_months,
        "failure_month_count": len(failure_months),
        "cost_bps": float(cost_bps),
        "avg_turnover": float(avg_turnover),
    }
