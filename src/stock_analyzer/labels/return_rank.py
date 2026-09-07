"""收益排序语义标签（方向一'，2026-09-07）。

Phase 2 NO-GO 的核心根因是 label/评估口径错位：TP/SL 路径标签奖励
"彩票性"（高波动票容易先摸 +8%），而选股系统要的是横截面排序收益
（fwd_return IC）。本模块提供与 IC 评估同口径的训练标签：以 T+1 开盘
入场、horizon 日期末收盘的前向收益为基础，在同一交易日横截面内取
分位——top 分位记 1、bottom 分位记 0。

时间语义与 soup 标签完全一致（Phase 0 不变量不动）：
- 入场锚点 = 决策日 T 的下一交易日开盘（next_tradable_open）；
- ``label_mature_trade_date`` = 入场日起第 horizon 个交易日（同 soup）；
- fwd_return 与 Phase 2 IC 评估的 fwd_return 同公式（mature close /
  entry open - 1）。

无前视论证：分位只依赖同一决策日的横截面内 fwd_return。fwd_return 本身
是未来窗口数据，但它只进入 label（训练目标），训练/评估的隔离由
harness 既有的 maturity purge + embargo（horizon + settlement_lag）保证
——与 soup 标签使用未来 high/low 完全同级别的前视需求，不引入新的
时间语义风险。
"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd


def build_return_rank_labels(
    fwd_return: pd.Series,
    *,
    top_quantile: float = 0.3,
    bottom_quantile: float = 0.3,
    drop_middle: bool = False,
    min_cross_section: int = 30,
    trade_dates: pd.Series | None = None,
) -> pd.Series:
    """按同一 trade_date 横截面的 fwd_return 分位生成排序语义标签。

    - top 30% 记 1.0、bottom 30% 记 0.0；
    - 中间段：``drop_middle=True`` 记 NaN（从训练剔除——省样本，且
      保证每个训练行都是明确的排序信号）；``False`` 记 0.5（soft，
      与 soup 冲突软标签同语义）。默认 drop（理由：横截面 ~5000 只下
      中间 40% 是 ~200 万行数据集中最大的沉默多数，把它们标成 0.5 只会
      稀释梯度、让 isotonic 校准器在 0.5 处堆积；明确剔除后 top/bottom
      两端样本仍各 ~50 万行，样本量充足）；
    - 横截面有效样本 < ``min_cross_section``（或 fwd_return 缺失）→ 整日
      NaN（横截面太薄时分位噪声大，宁可不用）；
    - 平秩（ties）用平均秩，分位在秩上计算——与 IC 评估的 Spearman 秩
      口径一致。

    截面分组解析（无跨日泄漏的前提）：``trade_dates`` 显式传入时按其
    分组；否则 MultiIndex 取 level 0、DatetimeIndex 整体单截面；都不
    满足（如单日切片的 RangeIndex）时**整体视为单一截面**——调用方
    （如 ``_apply_return_rank_labels``）已按日切片，此时把全部行当
    一个截面正是语义所需。

    返回 Series（name=``label_return_rank``），index 与输入对齐。
    """

    if not 0.0 < top_quantile < 1.0 or not 0.0 < bottom_quantile < 1.0:
        raise ValueError("top/bottom quantiles must be in (0, 1)")
    if top_quantile + bottom_quantile > 1.0:
        raise ValueError("top_quantile + bottom_quantile must be <= 1.0")
    if min_cross_section < 2:
        raise ValueError("min_cross_section must be >= 2")

    labels = pd.Series(float("nan"), index=fwd_return.index, dtype=float)
    values = pd.to_numeric(fwd_return, errors="coerce")
    if trade_dates is not None:
        groups: Iterable[tuple[object, pd.Series]] = values.groupby(
            pd.Series(trade_dates).to_numpy()
        )
    elif isinstance(values.index, pd.MultiIndex):
        groups = values.groupby(values.index.get_level_values(0))
    elif isinstance(values.index, pd.DatetimeIndex):
        groups = values.groupby(values.index.normalize())
    else:
        groups = ((None, values),)  # 单一截面（调用方已按日切片）
    for _, day_values in groups:
        day_valid = day_values.dropna()
        if len(day_valid) < min_cross_section:
            continue
        ranks = day_valid.rank(method="average", pct=True)  # 平均秩，pct ∈ (0,1]
        top_cut = 1.0 - top_quantile
        bottom_cut = bottom_quantile
        is_top = ranks > top_cut
        is_bottom = ranks <= bottom_cut
        day_labels = pd.Series(float("nan"), index=day_valid.index, dtype=float)
        day_labels[is_top] = 1.0
        day_labels[is_bottom] = 0.0
        if not drop_middle:
            day_labels[day_labels.isna()] = 0.5
        labels.loc[day_labels.index] = day_labels
    return labels.rename("label_return_rank")


def fwd_return_from_bars(
    bars: pd.DataFrame,
    *,
    entry_ts: pd.Timestamp,
    mature_ts: pd.Timestamp,
) -> float | None:
    """IC 评估同口径的前向收益：mature close / entry open - 1。

    与 pit_dataset 生成器中的 fwd_return 公式逐位一致（entry 当日开盘、
    horizon 期末收盘）。入场日不可交易（缺行/open 缺失/<=0）→ None。
    """

    if entry_ts not in bars.index or mature_ts not in bars.index:
        return None
    open_value = bars.at[entry_ts, "open"]
    if pd.isna(open_value) or float(open_value) <= 0.0:  # type: ignore[arg-type]
        return None
    close_value = bars.at[mature_ts, "close"]
    if pd.isna(close_value) or float(close_value) <= 0.0:  # type: ignore[arg-type]
        return None
    return float(close_value) / float(open_value) - 1.0  # type: ignore[arg-type]
