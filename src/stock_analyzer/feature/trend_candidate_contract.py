"""trend 候选池与特征契约（改进计划 §3.2）。

一句话：**硬门决定谁有资格被交易，预测性规则只决定顺序；截断必须发生在
轻量特征算完之后，而不是靠旧综合分提前淘汰。**

三件事在这里定死：

1. **规则分类**（``classify_rule``）。流动性 / 证券资格 / 停牌与数据新鲜度 /
   风险与过热是 ``hard_gate``，永远保留；旧综合分、板块配额、探索样本、
   恢复买入、模型分歧试探、主题与 completion 加分是 ``predictive``，
   **不参与新 trend 路径的推荐资格**。§2 的逐层消融只允许动 ``predictive``。
2. **四组特征**（``FEATURE_GROUPS``）：市场相对强弱、趋势位置、量价与流动性、
   波动与过热。缺基准指数时整组标为不可用并置 NaN —— 不得填 0，因为
   ``FEATURE_COMPUTE_VERSION`` v1 就是"市场相对族恒为 0"踩过的坑。
3. **as-of 边界**：夜扫只能用到 ``decision_date`` 当日已完整收盘的日线；
   旧 T−1 契约与 Alpha V2 保持独立，本契约不复用也不修改它们。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

TREND_FEATURE_CONTRACT_VERSION = "trend_asof_v1"

GROUP_MARKET_RELATIVE = "market_relative"
GROUP_TREND_POSITION = "trend_position"
GROUP_VOLUME_LIQUIDITY = "volume_liquidity"
GROUP_VOLATILITY_OVERHEAT = "volatility_overheat"

#: 第一轮只检验这四组已有信息（计划 §3.2）。顺序即消融阶梯的加入顺序。
FEATURE_GROUPS: dict[str, tuple[str, ...]] = {
    GROUP_MARKET_RELATIVE: (
        "excess_ret_5", "excess_ret_20", "excess_ret_60",
        "relative_strength", "rs_ma5", "rs_ma20",
        "rolling_beta_60", "excess_vol", "market_trend",
    ),
    GROUP_TREND_POSITION: (
        "ma5", "ma10", "ma20", "ma60", "close_to_ma20", "close_to_ma60",
        "ma20_slope", "range_position_60", "rank_ret_20",
    ),
    GROUP_VOLUME_LIQUIDITY: (
        "volume_ratio_5", "turnover", "avg_turnover_20", "float_market_cap",
        "amount_to_float_cap", "positive_bar_ratio", "last30_volume_share",
    ),
    GROUP_VOLATILITY_OVERHEAT: (
        "atr14_pct", "realized_vol_20", "range_pct", "ret_5", "ret_10",
        "gap_up_pct", "close_position", "tail_volatility_ratio",
    ),
}

#: 计划里点名"必须有独立证据才能加分"的信息源。默认不启用。
ADVISORY_SOURCES: tuple[str, ...] = ("news", "theme", "completion", "sector_quota",
                                     "exploration_sample")

HARD = "hard_gate"
PREDICTIVE = "predictive"

#: 规则名 → 分类。不认识的名字按 ``predictive`` 处理并要求显式登记：
#: 把未知规则当硬门会让消融实验漏掉它，当预测规则则最多是少用一条规则。
_RULE_KIND: dict[str, str] = {
    "is_st": HARD,
    "is_delisting_risk": HARD,
    "board_eligibility": HARD,
    "min_avg_turnover_20": HARD,
    "min_float_market_cap": HARD,
    "suspended": HARD,
    "stale_market_data": HARD,
    "financial_trust_insufficient": HARD,
    "trade_date_not_current": HARD,
    "limit_up_locked": HARD,
    "limit_down_locked": HARD,
    "overextension_risk": HARD,
    "kill_switch": HARD,
    "composite_score_floor": PREDICTIVE,
    "grade_s_a_only": PREDICTIVE,
    "cross_review": PREDICTIVE,
    "sector_quota": PREDICTIVE,
    "exploration_sample": PREDICTIVE,
    "recovery_buy": PREDICTIVE,
    "disagreement_probe": PREDICTIVE,
    "theme_boost": PREDICTIVE,
    "completion_boost": PREDICTIVE,
    "news_boost": PREDICTIVE,
    "execution_aware_rerank": PREDICTIVE,
}

UNKNOWN_RULE_KIND = PREDICTIVE


def classify_rule(name: str) -> str:
    return _RULE_KIND.get(str(name), UNKNOWN_RULE_KIND)


def is_declared_rule(name: str) -> bool:
    return str(name) in _RULE_KIND


def declared_rules(kind: str) -> tuple[str, ...]:
    if kind not in (HARD, PREDICTIVE):
        raise ValueError(f"unknown rule kind {kind!r}")
    return tuple(sorted(name for name, value in _RULE_KIND.items() if value == kind))


@dataclass(frozen=True)
class GateOutcome:
    """硬门判定结果。资格由硬门决定，顺序由模型决定。"""

    eligible: tuple[str, ...]
    rejected: dict[str, tuple[str, ...]]
    unknown_rules: tuple[str, ...] = ()

    @property
    def rejected_counts(self) -> dict[str, int]:
        return {reason: len(symbols) for reason, symbols in self.rejected.items()}

    def assert_only_hard_gates(self) -> None:
        """消融对照的护栏：被拒原因里出现 predictive 就是越界。"""
        offenders = sorted(
            reason for reason in self.rejected
            if classify_rule(reason) == PREDICTIVE
        )
        if offenders:
            raise ValueError(
                "predictive rules must not gate eligibility in the trend path: "
                f"{offenders}. They may only reorder survivors (see ADR-003 / NOTE-002)."
            )


def apply_hard_gates(
    *,
    symbols: Sequence[str],
    gates: Mapping[str, Sequence[str]],
) -> GateOutcome:
    """gates 是 ``{规则名: 被该规则拒绝的符号}``。分类未知但照用（保守拒），
    并把未知规则名单独返回，逼调用方去登记而不是悄悄生效。"""
    rejected: dict[str, tuple[str, ...]] = {}
    unknown: list[str] = []
    blocked: set[str] = set()
    for rule, dropped in gates.items():
        if not is_declared_rule(rule):
            unknown.append(str(rule))
        names = tuple(sorted({str(symbol) for symbol in dropped}))
        rejected[str(rule)] = names
        blocked.update(names)
    eligible = tuple(sorted({str(symbol) for symbol in symbols} - blocked))
    return GateOutcome(eligible=eligible, rejected=rejected,
                       unknown_rules=tuple(sorted(set(unknown))))


def compute_then_truncate(
    *,
    eligible: Sequence[str],
    scored: Mapping[str, float],
    limit: int,
) -> tuple[str, ...]:
    """截断只允许发生在轻量特征/分数都算完之后；分数缺失即出局而不是补 0。"""
    if limit < 0:
        raise ValueError("limit must be >= 0")
    ranked = sorted(
        (symbol for symbol in eligible if symbol in scored),
        key=lambda symbol: (-float(scored[symbol]), symbol),
    )
    return tuple(ranked[:limit]) if limit else tuple(ranked)


# ---------------------------------------------------------------------------
# 特征契约：as-of 边界 + 不可用即 NaN，绝不填零
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FeatureAvailability:
    """哪几组特征真的能算。基准指数缺失**或滞后** → 相对强弱组不可用，不是全 0。

    ``stale_benchmark_days`` 单独成一条判据：生产上 ``index_daily`` 曾整库停在
    2026-08-14 —— 指数"存在但过期"时算出来的 excess_ret_20 是跨窗口错位的假信号。
    """

    benchmark_available: bool
    benchmark_code: str = ""
    benchmark_last_date: date | None = None
    stale_benchmark_days: int = 0
    max_benchmark_staleness_days: int = 5
    notes: tuple[str, ...] = ()

    @property
    def benchmark_usable(self) -> bool:
        return bool(self.benchmark_available) and int(self.stale_benchmark_days or 0) <= int(
            self.max_benchmark_staleness_days
        )

    def unavailable_groups(self) -> tuple[str, ...]:
        if self.benchmark_usable:
            return ()
        return (GROUP_MARKET_RELATIVE,)


@dataclass
class TrendFeatureFrame:
    frame: pd.DataFrame
    columns_by_group: dict[str, tuple[str, ...]] = field(default_factory=dict)
    unavailable_groups: tuple[str, ...] = ()
    contract_version: str = TREND_FEATURE_CONTRACT_VERSION
    as_of: str = ""

    @property
    def feature_columns(self) -> tuple[str, ...]:
        ordered: list[str] = []
        for group in FEATURE_GROUPS:
            for column in self.columns_by_group.get(group, ()):
                if column not in ordered:
                    ordered.append(column)
        return tuple(ordered)

    def to_matrix(self) -> pd.DataFrame:
        """模型输入矩阵。缺组保留 NaN：填 0 会被模型当成"跟指数同步"的有效证据。"""
        return self.frame.loc[:, list(self.feature_columns)].copy()


def build_trend_feature_frame(
    *,
    engineered: pd.DataFrame,
    availability: FeatureAvailability,
    decision_date: date,
) -> TrendFeatureFrame:
    """按契约装配特征矩阵：先卡 as-of 边界，再把不可用组整体置 NaN。"""
    frame = engineered.copy()
    if "date" not in frame.columns:
        raise ValueError("engineered frame must carry a `date` column")
    dates = pd.to_datetime(frame["date"], errors="coerce")
    # as-of：夜扫只能看当日已完整收盘的日线，之后的行一概不参与计算。
    frame = frame.loc[dates <= pd.Timestamp(decision_date)]
    if frame.empty:
        raise ValueError(f"no bars on or before decision date {decision_date}")

    present = set(frame.columns)
    columns_by_group: dict[str, tuple[str, ...]] = {}
    for group, columns in FEATURE_GROUPS.items():
        columns_by_group[group] = tuple(name for name in columns if name in present)

    missing_groups = availability.unavailable_groups()
    for group in missing_groups:
        for column in columns_by_group.get(group, ()):
            # 用 np.nan 而不是 pd.NA：后者会把整个混合 dtype 的帧并成 object 块，
            # 连带污染其它可用组的数值列。
            frame[column] = np.nan

    return TrendFeatureFrame(
        frame=frame,
        columns_by_group=columns_by_group,
        unavailable_groups=missing_groups,
        as_of=str(decision_date),
    )


def assert_no_silent_zero_fill(frame: TrendFeatureFrame) -> None:
    """不可用组里若还有非 NaN 值，就是"把缺失当有效信息"，直接 raise。"""
    for group in frame.unavailable_groups:
        for column in frame.columns_by_group.get(group, ()):
            series = frame.frame[column]
            if series.notna().any():
                raise ValueError(
                    f"group {group!r} is marked unavailable but column {column!r} "
                    "still carries values — missing data must stay NaN, not 0"
                )


def ablation_ladder() -> tuple[tuple[str, ...], ...]:
    """逐组加入的阶梯 + 逐组留一。计划要求"逐组加入并做消融"。"""
    groups = list(FEATURE_GROUPS)
    ladder: list[tuple[str, ...]] = []
    for size in range(1, len(groups) + 1):
        ladder.append(tuple(groups[:size]))
    for dropped in groups:
        combo = tuple(group for group in groups if group != dropped)
        if combo not in ladder:
            ladder.append(combo)
    return tuple(ladder)


def columns_for_groups(groups: Iterable[str]) -> tuple[str, ...]:
    picked: list[str] = []
    for group in groups:
        if group not in FEATURE_GROUPS:
            raise ValueError(f"unknown feature group {group!r}")
        for column in FEATURE_GROUPS[group]:
            if column not in picked:
                picked.append(column)
    return tuple(picked)


def select_ablation_columns(
    frame: TrendFeatureFrame,
    groups: Sequence[str],
) -> tuple[str, ...]:
    """阶梯上一步实际可用的列（缺组/缺列都自然落选，不补占位）。"""
    wanted = set(columns_for_groups(groups))
    return tuple(
        column
        for column in frame.feature_columns
        if column in wanted and column in frame.frame.columns
    )


__all__ = [
    "ADVISORY_SOURCES",
    "FEATURE_GROUPS",
    "HARD",
    "PREDICTIVE",
    "TREND_FEATURE_CONTRACT_VERSION",
    "UNKNOWN_RULE_KIND",
    "FeatureAvailability",
    "GateOutcome",
    "TrendFeatureFrame",
    "ablation_ladder",
    "apply_hard_gates",
    "assert_no_silent_zero_fill",
    "classify_rule",
    "columns_for_groups",
    "compute_then_truncate",
    "declared_rules",
    "is_declared_rule",
    "build_trend_feature_frame",
    "select_ablation_columns",
]
