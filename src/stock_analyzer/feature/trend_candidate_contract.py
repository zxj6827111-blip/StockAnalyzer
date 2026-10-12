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

#: 计划 §3.2 点名"其正向加分必须有独立证据"的信息源，用 ``_RULE_KIND`` 里的真实规则名。
#: 它们是建议性的：不得当硬门（``apply_hard_gates`` 会拒），也不得进训练。
ADVISORY_SOURCES: tuple[str, ...] = ("news_boost", "theme_boost", "completion_boost",
                                     "sector_quota", "exploration_sample")

#: 列名里出现这些词根 = 当时未必拿得到的信息（新闻分、主题热度、completion 进度…）。
#: 它们可以当风险信息看，但**不许进训练**：历史上取不到的东西进特征就是 look-ahead。
ADVISORY_TOKENS: tuple[str, ...] = ("news", "theme", "completion", "sentiment",
                                    "sector_quota", "exploration", "analyst", "boost")

#: 第一轮唯一允许参与训练/打分的列：四组已有行情信息（计划 §3.2）。
REPRODUCIBLE_FEATURE_COLUMNS = frozenset(
    column for columns in FEATURE_GROUPS.values() for column in columns
)

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
    # as-of 那天之前的历史 bar 不够算特征：这是数据完整性硬门，不是预测规则。
    # 历史重放（scripts/replay_tail_candidate_pool.py）用它给被淘汰的 symbol-day 记原因，
    # 名字必须在这里登记，否则留档里会出现契约不认识的规则。
    "insufficient_history_at_asof": HARD,
    # 流通市值列里出现数据供应商的占位常量（见 ``UNPROVEN_FLOAT_MARKET_CAP``）：
    # 那一行的市值**没有被测量过**，所以 ``min_float_market_cap`` 对它无从判定。
    # 这是数据完整性硬门，不是预测规则——把它当"已通过"会让这条门整天不淘汰任何票。
    "unproven_float_market_cap": HARD,
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


#: 硬性资格检查的**判定顺序**：一只票同一天同时踩中多条硬门时，留档只能记一条原因
#: （``StageTrace`` 的恒等式不许一只票进两个桶），归因就按这个顺序取第一条命中项。
#:
#: 它必须是契约里写死的事实，而不是某处 dict 的插入顺序：留档里的"逐原因淘汰了多少只"
#: 会随代码行序变化而悄悄改数。实测例子（94 个重放决策日）``min_float_market_cap``
#: 一共命中 8,527 次，但排在前面的 ``min_avg_turnover_20`` 把绝大多数分走了，
#: 归因表里只剩 132 次——两种读法都对，但不写出顺序就没法知道差值是构造出来的。
#:
#: 末位 ``insufficient_history_at_asof`` 是 PIT/历史长度出局，只在没有任何门命中时才记账。
HARD_GATE_ATTRIBUTION_ORDER: tuple[str, ...] = (
    "board_eligibility",
    "is_st",
    "is_delisting_risk",
    "suspended",
    "min_avg_turnover_20",
    "min_float_market_cap",
    "unproven_float_market_cap",
    "stale_market_data",
    "overextension_risk",
    "insufficient_history_at_asof",
)

#: 线上夜扫的硬门名（``runtime/universe_candidate_selector._hard_filter`` 逐条判定用的名字）。
#: 它们和研究侧重放用的名字**不是同一套词汇**（例如线上的 ``low_float_market_cap``
#: 对应契约的 ``min_float_market_cap``），但都是同一批门。留档按层记原因时用的是
#: 生产者自己的名字，所以这些名字必须登记进 ``_RULE_KIND``：一个没登记的名字会被
#: ``classify_rule`` 判成 ``predictive``，消融实验就把它当成"可以拆掉的预测规则"，
#: 而它是交易资格/数据完整性硬门——这类缺陷在 §3b.3 已经记过一次，不再重演。
LIVE_HARD_GATE_NAMES: tuple[str, ...] = (
    "invalid_code",
    "out_of_board_scope",
    "suspended",
    "is_st",
    "delisting_risk",
    "invalid_history",
    "insufficient_history",
    "invalid_avg_turnover_20",
    "low_avg_turnover_20",
    "invalid_float_market_cap",
    "low_float_market_cap",
    "unproven_float_market_cap",
    "invalid_close",
    "invalid_latest_data_date",
    "stale_market_data",
    "financial_data_incomplete",
    "missing_roe",
    "roe_below_min",
    "missing_debt_ratio",
    "debt_ratio_above_max",
)

#: 线上硬门的**判定顺序** = ``_hard_filter`` 里 mask 逐条收紧的顺序。
#: 写死成契约事实而不是 dict 插入序，理由与 ``HARD_GATE_ATTRIBUTION_ORDER`` 相同：
#: 一只票同时踩中多条门时留档只能记第一条，顺序没定义则"逐原因淘汰多少只"会随代码行序漂移。
LIVE_HARD_GATE_ATTRIBUTION_ORDER: tuple[str, ...] = LIVE_HARD_GATE_NAMES

#: 每条线上硬门读了哪些列 —— 供留档的 ``features_used`` 使用（§2"每层记使用的特征"）。
LIVE_GATE_INPUT_COLUMNS: dict[str, tuple[str, ...]] = {
    "invalid_code": ("symbol",),
    "out_of_board_scope": ("symbol",),
    "suspended": ("suspended",),
    "is_st": ("is_st",),
    "delisting_risk": ("is_delisting_risk",),
    "invalid_history": ("history_days",),
    "insufficient_history": ("history_days",),
    "invalid_avg_turnover_20": ("avg_turnover_20",),
    "low_avg_turnover_20": ("avg_turnover_20",),
    "invalid_float_market_cap": ("float_market_cap",),
    "low_float_market_cap": ("float_market_cap",),
    "unproven_float_market_cap": ("float_market_cap",),
    "invalid_close": ("latest_close",),
    "invalid_latest_data_date": ("latest_data_date",),
    "stale_market_data": ("latest_data_date",),
    "financial_data_incomplete": ("financial_data_complete",),
    "missing_roe": ("roe",),
    "roe_below_min": ("roe",),
    "missing_debt_ratio": ("debt_ratio",),
    "debt_ratio_above_max": ("debt_ratio",),
}

# 线上硬门名一并进分类表：不认识的名字会被 classify_rule 判成 predictive。
_RULE_KIND.update(dict.fromkeys(LIVE_HARD_GATE_NAMES, HARD))

#: 数据供应商取不到流通市值时写进 ``float_market_cap`` 的**占位常量**（元）。
#: tushare / akshare / efinance 三个 provider 用的是同一个字面值，所以它在库里是
#: "这一天的市值没有被测量过"的指纹，而不是一个 120 亿的观测值。
#:
#: 已证实的危害（研究库 ``daily_bars``，逐月统计等于该常量的行数）：
#: 2026-04 108,295/108,615 行、2026-05 92,967/92,989、2026-06 **108,538/108,538（全月只剩
#: 这一个不同取值）**，2026-03 与 2026-07 各约五成；2022-05 起每月 15~46 行、
#: 2025-09~2026-02 升到 2,511~5,608 行——占位符一直都在悄悄写，只是量级不同。
#:
#: 危害机制不是"数值不准"而是**门失效**：阈值取当天该列的 10 分位，列内 99.7% 都是这个
#: 常量时，分位数就等于常量，``value < threshold`` 对占位行恒为假 —— 这条硬门那天对全市场
#: 一个都不淘汰，却照样"跑完了"，留档于是把"没测过"读成"没有一只票市值不达标"。
#:
#: 老记录保留原值不重写（§3.1：带版本的解释规则兼容），读侧按这条规则把它当未知：
#: 市值门对它**无法判定**，于是记 ``unproven_float_market_cap`` 出局，而不是记它通过。
#: 反向误差是刻意选边的——真有某只票市值恰好等于 12,000,000,000.0 元时它也会被当未知，
#: 那只是少留一只可判定样本，不会把未测过的东西当成有效信息。
UNPROVEN_FLOAT_MARKET_CAP = 12_000_000_000.0
FLOAT_CAP_INTERPRETATION_VERSION = "unproven_float_cap_placeholder_v1"


def unproven_float_market_cap_mask(values: pd.Series) -> pd.Series:
    """``float_market_cap`` 里"没被测量过"的那些行（=True）。

    NaN 返回 False —— 那是本来就没这列的值，由读取它的门的既有语义处理；这里只抓
    被占位常量**填过**的格子，因为那才是"看起来像数据"的那一种。
    """
    numbers = pd.to_numeric(values, errors="coerce")
    return numbers.notna() & numbers.eq(UNPROVEN_FLOAT_MARKET_CAP)


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


def assert_training_features(feature_names: Iterable[str]) -> tuple[str, ...]:
    """训练/打分用的列必须都在四组可复现行情信息里，否则拒绝。

    新闻分、主题热度、completion 进度这类东西在**当时**不一定拿得到；把它们喂进
    ``p_net_profit_5d_tail`` 的训练，时间外验证会给出一个无法复现的漂亮数字
    （计划 §3.2"历史不可复现的信息不得混入训练"）。未知列名同样拒绝 ——
    "不认识"不等于"可复现"。
    """
    names = tuple(str(name) for name in feature_names)
    if not names:
        raise ValueError("trend training needs at least one feature column")
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"duplicate feature columns would be silently dropped: {duplicates}")
    outside = [name for name in names if name not in REPRODUCIBLE_FEATURE_COLUMNS]
    if outside:
        advisory = [
            name for name in outside
            if any(token in name.lower() for token in ADVISORY_TOKENS)
        ]
        detail = f"不可复现的信息源: {advisory}" if advisory else f"未登记在特征契约里: {outside}"
        raise ValueError(
            f"feature columns outside the four reproducible trend groups: {detail}; "
            "第一轮只允许 market_relative / trend_position / volume_liquidity / "
            "volatility_overheat 四组已有信息参与训练"
        )
    return names


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
    "ADVISORY_TOKENS",
    "FEATURE_GROUPS",
    "HARD",
    "PREDICTIVE",
    "REPRODUCIBLE_FEATURE_COLUMNS",
    "TREND_FEATURE_CONTRACT_VERSION",
    "UNKNOWN_RULE_KIND",
    "FeatureAvailability",
    "GateOutcome",
    "TrendFeatureFrame",
    "ablation_ladder",
    "apply_hard_gates",
    "assert_no_silent_zero_fill",
    "assert_training_features",
    "classify_rule",
    "columns_for_groups",
    "compute_then_truncate",
    "declared_rules",
    "is_declared_rule",
    "build_trend_feature_frame",
    "select_ablation_columns",
]
