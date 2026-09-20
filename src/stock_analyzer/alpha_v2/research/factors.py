"""Alpha V2 可解释因子族（S15 / 原 P1-05 的"简单因子 baseline"底座）。

**为什么要有一层纯因子**（蓝图 §20）：如果 ML 长期打不过一个没有训练链、没有超参、
没有泄漏面的简单因子组合，那正确答案是**停止堆复杂度**，而不是继续换模型。
因此本模块把"简单基线"做成**可复现、可审计、方向预先登记**的一等公民。

因子选取原则：

1. 只用 S14 判定 ``asof_safe=proven`` 的日线/日历类输入（价格、成交额、指数），
   不需要财务、资金流、分钟数据——所以它天然没有那些组的 PIT 风险；
2. **方向预先登记**：每个因子的多空方向写在 :data:`FACTORS` 里（附理由），
   运行报告同时给出**实测 IC**，任何事后翻方向都会在报告里留下痕迹；
3. 组内先做截面 rank 再合成，避免量纲不同导致某一组主导。

被显式挡住的组：``fundamental_quality``。原因是它的输入（roe / debt_ratio）属于
``financial_pit`` 组，S14 判定为 ``unverified``（公告日 ≤ T 未经逐行复核）——
按蓝图"Fundamental Quality（仅 PIT-safe）"的要求，**未证明就不得使用**，
因此本模块把它登记为 blocked 并给出原因，而不是偷偷用行内值算一个"基本面因子"。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from stock_analyzer.alpha_v2.research.panel import DailyPanel

NOT_AVAILABLE = "not_available"

GROUP_TREND = "trend_pullback"
GROUP_RELATIVE_STRENGTH = "relative_strength"
GROUP_LIQUIDITY = "liquidity"
GROUP_VOLATILITY = "volatility_quality"
GROUP_FUNDAMENTAL = "fundamental_quality"

DIRECTION_LONG = 1
DIRECTION_SHORT = -1

FACTOR_TREND_MA_GAP = "trend_ma_gap_20"
FACTOR_MA_SLOPE = "ma_slope_20"
FACTOR_REVERSAL = "reversal_5d"
FACTOR_RS_EXCESS = "rs_excess_20d"
FACTOR_LIQUIDITY = "liquidity_turnover_20d"
FACTOR_VOL_QUALITY = "vol_quality_20d"


@dataclass(frozen=True, slots=True)
class FactorSpec:
    """一个可解释因子的定义（方向与理由都写在代码里，属于"预先登记"）。"""

    name: str
    group: str
    direction: int
    definition: str
    rationale: str
    source_columns: tuple[str, ...]
    lookback_days: int = 20
    in_baseline: bool = True

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "group": self.group,
            "direction": int(self.direction),
            "definition": self.definition,
            "rationale": self.rationale,
            "source_columns": list(self.source_columns),
            "lookback_days": int(self.lookback_days),
            "in_baseline": bool(self.in_baseline),
        }


FACTORS: tuple[FactorSpec, ...] = (
    FactorSpec(
        name=FACTOR_TREND_MA_GAP,
        group=GROUP_TREND,
        direction=DIRECTION_LONG,
        definition="close / MA20 - 1（收盘相对 20 日均线的偏离度）",
        rationale="中期趋势：站上均线的股票后续相对更强（趋势跟随的最小实现）",
        source_columns=("close",),
        lookback_days=20,
    ),
    FactorSpec(
        name=FACTOR_MA_SLOPE,
        group=GROUP_TREND,
        direction=DIRECTION_LONG,
        definition="MA20(T) / MA20(T-5) - 1（均线的 5 日斜率）",
        rationale="趋势方向：均线本身在抬升，比单点偏离更稳",
        source_columns=("close",),
        lookback_days=25,
    ),
    FactorSpec(
        name=FACTOR_REVERSAL,
        group=GROUP_TREND,
        direction=DIRECTION_LONG,
        definition="-(close / close(T-5) - 1)（5 日收益取负）",
        rationale="短期反转：A 股短周期上，5 日涨幅过大者后续回吐（蓝图 §20 列为短期回踩/反转）",
        source_columns=("close",),
        lookback_days=5,
    ),
    FactorSpec(
        name=FACTOR_RS_EXCESS,
        group=GROUP_RELATIVE_STRENGTH,
        direction=DIRECTION_LONG,
        definition="个股 20 日收益 − 当日合格池等权 20 日收益（截面去均值）",
        rationale=(
            "相对强度：与全市场当日基准无关、只用当日横截面即可算，避免引入指数数据口径"
            "（相对强度高的票在被验证的同一口径下更可能继续跑赢）"
        ),
        source_columns=("close",),
        lookback_days=20,
    ),
    FactorSpec(
        name=FACTOR_LIQUIDITY,
        group=GROUP_LIQUIDITY,
        direction=DIRECTION_LONG,
        definition="log(过去 20 日平均成交额)",
        rationale="可成交性/关注度：流动性过低的名字纸面收益不可实现，本因子把流动性与关注度一起纳入",
        source_columns=("turnover",),
        lookback_days=20,
    ),
    FactorSpec(
        name=FACTOR_VOL_QUALITY,
        group=GROUP_VOLATILITY,
        direction=DIRECTION_LONG,
        definition="-(过去 20 日日收益标准差)",
        rationale="低波动异象：同等收益下波动更低的名字风险调整后更优，且更不容易触发尾部",
        source_columns=("close",),
        lookback_days=20,
    ),
)

BASELINE_FACTORS: tuple[str, ...] = tuple(spec.name for spec in FACTORS if spec.in_baseline)

# 被显式挡住的因子组（不是"忘了做"，而是"没有 PIT 证据前不能做"）。
BLOCKED_FACTOR_GROUPS: dict[str, dict[str, str]] = {
    GROUP_FUNDAMENTAL: {
        "reason": "输入属于 financial_pit 组，S14 判定 asof_safe=unverified",
        "requirement": "需要逐行证明公告日 <= 决策日（financial_as_of）后才能启用",
        "policy": "fundamental_quality_blocked_until_pit_proven",
    }
}


def factor_spec(name: str) -> FactorSpec:
    for spec in FACTORS:
        if spec.name == name:
            return spec
    raise KeyError(f"unknown factor: {name}")


def compute_factor_frame(
    *,
    panel: DailyPanel,
    decisions: Sequence[object],
    factors: Sequence[str] = BASELINE_FACTORS,
) -> pd.DataFrame:
    """逐 (symbol, decision_date) 计算原始因子值（只用 ≤ 决策日的数据）。"""
    specs = [factor_spec(name) for name in factors]
    longest = max([spec.lookback_days for spec in specs] + [5])
    grouped: dict[str, list[object]] = {}
    for item in decisions:
        grouped.setdefault(str(item.symbol), []).append(item)

    rows: list[dict[str, object]] = []
    for symbol in sorted(grouped):
        frame = panel.symbol_bars(symbol)
        if frame is None or frame.empty:
            continue
        dates = [ts.date() for ts in frame.index]
        positions = {day: index for index, day in enumerate(dates)}
        closes = pd.to_numeric(frame["close"], errors="coerce").to_numpy(dtype=float)
        turnovers = pd.to_numeric(frame["turnover"], errors="coerce").to_numpy(dtype=float)
        for item in grouped[symbol]:
            position = positions.get(item.decision_date)
            if position is None:
                continue
            start = max(0, position - longest + 1)
            window = closes[start : position + 1]
            window_turnover = turnovers[start : position + 1]
            rows.append(
                {
                    "decision_date": item.decision_date.isoformat(),
                    "symbol": symbol,
                    FACTOR_TREND_MA_GAP: _ma_gap(window, 20),
                    FACTOR_MA_SLOPE: _ma_slope(window, window_days=20, lag=5),
                    FACTOR_REVERSAL: _reversal(window, 5),
                    FACTOR_RS_EXCESS: _momentum(window, 20),
                    FACTOR_LIQUIDITY: _log_mean(window_turnover, 20),
                    FACTOR_VOL_QUALITY: _neg_volatility(window, 20),
                }
            )
    frame_out = pd.DataFrame(rows)
    if frame_out.empty:
        return pd.DataFrame(columns=["decision_date", "symbol", *factors])
    # 相对强度 = 截面去均值的动量（同日、同池），不使用任何未来信息
    frame_out[FACTOR_RS_EXCESS] = _demean_by_date(frame_out, FACTOR_RS_EXCESS)
    return frame_out


def _demean_by_date(frame: pd.DataFrame, column: str) -> pd.Series:
    values = pd.to_numeric(frame[column], errors="coerce")
    return values - values.groupby(frame["decision_date"]).transform("mean")


def _ma_gap(window: np.ndarray, days: int) -> float:
    """收盘相对 N 日均线的偏离度。至少要有 2 个有效收盘价才算，否则返回 NaN（不猜）。"""
    if window.size < 2 or not math.isfinite(window[-1]):
        return float("nan")
    segment = window[-days:]
    usable = segment[np.isfinite(segment)]
    if usable.size < 2:
        return float("nan")
    mean = float(np.mean(usable))
    if mean <= 0:
        return float("nan")
    return float(window[-1] / mean - 1.0)


def _ma_slope(window: np.ndarray, *, window_days: int, lag: int) -> float:
    if window.size < window_days + lag:
        return float("nan")
    current = window[-window_days:]
    previous = window[-window_days - lag : -lag]
    current_mean = float(np.nanmean(current))
    previous_mean = float(np.nanmean(previous))
    if not math.isfinite(current_mean) or not math.isfinite(previous_mean) or previous_mean <= 0:
        return float("nan")
    return float(current_mean / previous_mean - 1.0)


def _reversal(window: np.ndarray, days: int) -> float:
    momentum = _momentum(window, days)
    return float("nan") if not math.isfinite(momentum) else -momentum


def _momentum(window: np.ndarray, days: int) -> float:
    if window.size < days + 1:
        return float("nan")
    start = window[-(days + 1)]
    end = window[-1]
    if not (math.isfinite(start) and math.isfinite(end)) or start <= 0:
        return float("nan")
    return float(end / start - 1.0)


def _log_mean(values: np.ndarray, days: int) -> float:
    if values.size == 0:
        return float("nan")
    usable = values[-days:]
    usable = usable[np.isfinite(usable) & (usable > 0)]
    if usable.size == 0:
        return float("nan")
    return float(math.log(float(np.mean(usable))))


def _neg_volatility(window: np.ndarray, days: int) -> float:
    if window.size < 3:
        return float("nan")
    segment = window[-max(2, days + 1) :]
    returns = np.diff(segment) / segment[:-1]
    returns = returns[np.isfinite(returns)]
    if returns.size < 2:
        return float("nan")
    return float(-np.std(returns, ddof=0))


def direction_unify(
    frame: pd.DataFrame, *, factors: Sequence[str] = BASELINE_FACTORS
) -> pd.DataFrame:
    """方向统一：让"数值越大越看多"。"""
    result = frame.copy()
    for name in factors:
        spec = factor_spec(name)
        if spec.direction < 0:
            result[name] = -pd.to_numeric(result[name], errors="coerce")
    return result


def cross_sectional_rank(
    frame: pd.DataFrame,
    *,
    factors: Sequence[str] = BASELINE_FACTORS,
    min_cross_section: int = 30,
) -> pd.DataFrame:
    """逐日截面 rank 分位（0~1）；当日有效样本不足的组置为 NaN（不硬算）。"""
    ranks = pd.DataFrame(index=frame.index)
    for name in factors:
        values = pd.to_numeric(frame[name], errors="coerce")
        grouped = values.groupby(frame["decision_date"])
        pct = grouped.rank(pct=True)
        counts = grouped.transform("count")
        ranks[f"rank_{name}"] = pct.where(counts >= max(2, int(min_cross_section)))
    return ranks


def composite_score(
    frame: pd.DataFrame,
    *,
    factors: Sequence[str] = BASELINE_FACTORS,
    weights: Mapping[str, float] | None = None,
    min_cross_section: int = 30,
) -> pd.Series:
    """等权（或给定权重）合成 baseline 分数：方向统一 → 截面 rank → 加权平均。"""
    unified = direction_unify(frame, factors=factors)
    ranks = cross_sectional_rank(unified, factors=factors, min_cross_section=min_cross_section)
    resolved_weights = {name: float((weights or {}).get(name, 1.0)) for name in factors}
    total = sum(resolved_weights.values())
    if total <= 0:
        raise ValueError("baseline weights must sum to a positive value")
    score = pd.Series(0.0, index=frame.index)
    available = pd.Series(0.0, index=frame.index)
    for name in factors:
        column = f"rank_{name}"
        values = ranks[column]
        weight = resolved_weights[name] / total
        score = score + values.fillna(0.0) * weight
        available = available + values.notna().astype(float) * weight
    # 只对"至少有一个可用因子"的行给分；缺因子不按 0 计（那等于把缺失当最差）
    return score.where(available > 0) / available.where(available > 0)


def factor_diagnostics(
    frame: pd.DataFrame, *, factors: Sequence[str] = BASELINE_FACTORS
) -> pd.DataFrame:
    """每个因子的覆盖率（供报告揭示"哪个因子其实没数据"）。"""
    rows: list[dict[str, object]] = []
    total = max(1, len(frame))
    for name in factors:
        spec = factor_spec(name)
        values = (
            pd.to_numeric(frame.get(name), errors="coerce")
            if name in frame
            else pd.Series(dtype=float)
        )
        usable = int(values.notna().sum())
        rows.append(
            {
                "factor": name,
                "group": spec.group,
                "direction": int(spec.direction),
                "non_null_ratio": round(usable / total, 6),
                "rows": total,
            }
        )
    return pd.DataFrame(rows)


def factor_definitions(*, factors: Sequence[str] = BASELINE_FACTORS) -> list[dict[str, object]]:
    return [factor_spec(name).to_payload() for name in factors]


def blocked_groups_payload() -> dict[str, dict[str, str]]:
    return {group: dict(payload) for group, payload in BLOCKED_FACTOR_GROUPS.items()}


__all__ = [
    "BASELINE_FACTORS",
    "BLOCKED_FACTOR_GROUPS",
    "DIRECTION_LONG",
    "DIRECTION_SHORT",
    "FACTORS",
    "FACTOR_LIQUIDITY",
    "FACTOR_MA_SLOPE",
    "FACTOR_REVERSAL",
    "FACTOR_RS_EXCESS",
    "FACTOR_TREND_MA_GAP",
    "FACTOR_VOL_QUALITY",
    "GROUP_FUNDAMENTAL",
    "GROUP_LIQUIDITY",
    "GROUP_RELATIVE_STRENGTH",
    "GROUP_TREND",
    "GROUP_VOLATILITY",
    "FactorSpec",
    "blocked_groups_payload",
    "composite_score",
    "compute_factor_frame",
    "cross_sectional_rank",
    "direction_unify",
    "factor_definitions",
    "factor_diagnostics",
    "factor_spec",
]
